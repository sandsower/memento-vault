"""Portable vault archive export/import with tombstone-aware merges."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

ARCHIVE_FORMAT = "memento-portable-archive"
ARCHIVE_VERSION = 1
PAYLOAD_PREFIX = "payload/"
CONTENT_ROOTS = ("notes", "fleeting", "projects", "archive")
STATE_FILES = ("vault-identity.json", ".sync/ledger.jsonl", ".memento/tombstones.jsonl")
TOMBSTONES_REL = ".memento/tombstones.jsonl"


class ArchiveError(RuntimeError):
    """Base class for portable archive failures."""


class ArchiveConflictError(ArchiveError):
    """Raised when an import would overwrite local state unsafely."""

    def __init__(self, conflicts: Iterable[str]):
        self.conflicts = sorted(set(conflicts))
        super().__init__("portable archive import conflicts: " + ", ".join(self.conflicts))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_rel(path: str | os.PathLike) -> str:
    rel = PurePosixPath(str(path).replace(os.sep, "/"))
    if rel.is_absolute() or ".." in rel.parts or not str(rel) or str(rel) == ".":
        raise ValueError(f"unsafe vault-relative path: {path}")
    return rel.as_posix()


def _safe_dest(vault: Path, rel: str) -> Path:
    normalized = _normalize_rel(rel)
    root = vault.resolve()
    dest = root
    for part in PurePosixPath(normalized).parts:
        dest = dest / part
        if dest.is_symlink():
            raise ArchiveError(f"archive path uses symlinked destination: {rel}")
    if not _is_relative_to(dest.resolve(), root):
        raise ArchiveError(f"archive path escapes vault: {rel}")
    return dest


def _identity_path(vault: Path) -> Path:
    return vault / "vault-identity.json"


def _ensure_identity(vault: Path) -> dict:
    path = _identity_path(vault)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("vault_id"):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    vault.mkdir(parents=True, exist_ok=True)
    data = {"vault_id": uuid.uuid4().hex, "created": _utcnow_iso()}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def tombstones_path(vault: Path) -> Path:
    """Return the vault-local tombstone log path."""
    return Path(vault) / TOMBSTONES_REL


def _is_allowed_tombstone_rel(rel: str) -> bool:
    # MEM-153: fleeting/ is a second allowed tombstone source (expiring
    # fleeting notes reuses this same ledger) -- never a parallel mechanism.
    return rel.endswith(".md") and (rel.startswith("notes/") or rel.startswith("fleeting/"))


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def iter_tombstones(vault: Path):
    """Yield valid tombstone records from a vault."""
    for record in _iter_jsonl(tombstones_path(vault)):
        path = record.get("path")
        if not path:
            continue
        try:
            record = dict(record)
            record["path"] = _normalize_rel(path)
        except ValueError:
            continue
        if not _is_allowed_tombstone_rel(record["path"]):
            continue
        yield record


def _latest_log_records(vault: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for record in iter_tombstones(vault):
        path = record["path"]
        if path not in latest or str(record.get("ts", "")) >= str(latest[path].get("ts", "")):
            latest[path] = record
    return latest


def latest_active_tombstones(vault: Path) -> dict[str, dict]:
    """Return latest deletion tombstones, ignoring newer restore records."""
    return {path: record for path, record in _latest_log_records(vault).items() if record.get("reason") != "restored"}


def _latest_tombstones(vault: Path) -> dict[str, dict]:
    return latest_active_tombstones(vault)


def _append_jsonl(path: Path, records: Iterable[dict]) -> int:
    records = list(records)
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    return len(records)


def record_tombstone(
    vault: Path,
    rel_path: str | os.PathLike,
    *,
    reason: str = "deleted",
    content_hash: str | None = None,
    ts: str | None = None,
) -> dict:
    """Append a tombstone for a vault-relative path.

    If ``content_hash`` is omitted and the file still exists, the current raw
    file hash is captured so future imports can safely remove only that exact
    resurrected content.
    """
    vault = Path(vault)
    rel = _normalize_rel(rel_path)
    if not _is_allowed_tombstone_rel(rel):
        raise ValueError(f"unsupported tombstone path: {rel}")
    path = _safe_dest(vault, rel)
    if content_hash is None and path.exists() and path.is_file():
        content_hash = _sha256_file(path)
    record = {"ts": ts or _utcnow_iso(), "path": rel, "reason": reason}
    if content_hash:
        record["content_hash"] = content_hash
    _append_jsonl(tombstones_path(vault), [record])
    return record


def _restore_record(vault: Path, rel_path: str | os.PathLike, *, ts: str | None = None) -> dict:
    rel = _normalize_rel(rel_path)
    if not _is_allowed_tombstone_rel(rel):
        raise ValueError(f"unsupported tombstone path: {rel}")
    record = {"ts": ts or _utcnow_iso(), "path": rel, "reason": "restored"}
    _append_jsonl(tombstones_path(vault), [record])
    return record


def _active_tombstones(vault: Path) -> list[dict]:
    """Return latest deletion tombstones that still apply to absent paths."""
    active = []
    for record in latest_active_tombstones(vault).values():
        try:
            dest = _safe_dest(vault, record["path"])
        except (ArchiveError, ValueError):
            continue
        if dest.exists():
            continue
        active.append(record)
    return active


def _iter_export_files(vault: Path, *, exclude_paths: Iterable[Path] = ()):
    seen: set[str] = set()
    excluded = {p.resolve() for p in exclude_paths}
    for root_name in CONTENT_ROOTS:
        root = vault / root_name
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if path.is_symlink() or path.resolve() in excluded:
                continue
            rel = path.relative_to(vault).as_posix()
            seen.add(rel)
            yield rel, path
    for rel in STATE_FILES:
        if rel == TOMBSTONES_REL:
            continue
        path = vault / rel
        if path.is_symlink() or path.resolve() in excluded:
            continue
        if path.exists() and path.is_file() and rel not in seen:
            seen.add(rel)
            yield rel, path


def export_archive(vault: Path, archive_path: Path, *, include_manifest_hashes: bool = True) -> dict:
    """Create a portable zip archive for a vault.

    Markdown and vault files remain canonical; derived search indexes and
    embeddings are intentionally excluded.
    """
    vault = Path(vault)
    archive_path = Path(archive_path)
    identity = _ensure_identity(vault)
    files = []
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root_name in CONTENT_ROOTS:
            zf.writestr(f"{PAYLOAD_PREFIX}{root_name}/", b"")
        for rel, path in _iter_export_files(vault, exclude_paths=(archive_path,)):
            data = path.read_bytes()
            info = {"path": rel, "size": len(data)}
            if include_manifest_hashes:
                info["sha256"] = _sha256_bytes(data)
            files.append(info)
            zf.writestr(f"{PAYLOAD_PREFIX}{rel}", data)

        active_tombstones = _active_tombstones(vault)
        if active_tombstones:
            data = "".join(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n" for r in active_tombstones)
            encoded = data.encode("utf-8")
            info = {"path": TOMBSTONES_REL, "size": len(encoded)}
            if include_manifest_hashes:
                info["sha256"] = _sha256_bytes(encoded)
            files.append(info)
            zf.writestr(f"{PAYLOAD_PREFIX}{TOMBSTONES_REL}", encoded)

        manifest = {
            "archive_format": ARCHIVE_FORMAT,
            "archive_version": ARCHIVE_VERSION,
            "created_at": _utcnow_iso(),
            "vault_id": identity.get("vault_id"),
            "content_roots": list(CONTENT_ROOTS),
            "state_files": list(STATE_FILES),
            "files": files,
            "tombstones": active_tombstones,
            "derived_metadata": {"embeddings_included": False, "search_indexes_included": False},
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    return {"archive_path": str(archive_path), "vault_id": identity.get("vault_id"), "file_count": len(files)}


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except KeyError as exc:
        raise ArchiveError("archive missing manifest.json") from exc
    except json.JSONDecodeError as exc:
        raise ArchiveError("archive manifest is not valid JSON") from exc
    if manifest.get("archive_format") != ARCHIVE_FORMAT:
        raise ArchiveError("not a memento portable archive")
    if manifest.get("archive_version") != ARCHIVE_VERSION:
        raise ArchiveError(f"unsupported archive version: {manifest.get('archive_version')}")
    return manifest


def _is_allowed_payload_rel(rel: str) -> bool:
    return rel in STATE_FILES or any(rel.startswith(f"{root}/") for root in CONTENT_ROOTS)


def _payload_members(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if not info.filename.startswith(PAYLOAD_PREFIX) or info.is_dir():
            continue
        rel = info.filename[len(PAYLOAD_PREFIX) :]
        try:
            normalized = _normalize_rel(rel)
        except ValueError:
            raise ArchiveError(f"archive contains unsafe path: {info.filename}")
        if not _is_allowed_payload_rel(normalized):
            raise ArchiveError(f"archive contains unsupported payload path: {normalized}")
        members[normalized] = info
    return members


def _manifest_content_roots(manifest: dict) -> list[str]:
    roots = manifest.get("content_roots") or list(CONTENT_ROOTS)
    normalized = []
    for root in roots:
        try:
            rel = _normalize_rel(root)
        except (TypeError, ValueError) as exc:
            raise ArchiveError(f"archive manifest contains unsafe content root: {root}") from exc
        if rel not in CONTENT_ROOTS:
            raise ArchiveError(f"archive manifest contains unsupported content root: {rel}")
        normalized.append(rel)
    return normalized


def _read_payload_tombstones(zf: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]) -> list[dict]:
    info = members.get(TOMBSTONES_REL)
    if not info:
        return []
    records = []
    for line in zf.read(info).decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            record["path"] = _normalize_rel(record["path"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if not _is_allowed_tombstone_rel(record["path"]):
            raise ArchiveError(f"archive contains unsupported tombstone path: {record['path']}")
        records.append(record)
    return records


def _merge_jsonl_file(dest: Path, incoming: str) -> bool:
    existing = set(dest.read_text(encoding="utf-8").splitlines()) if dest.exists() else set()
    lines = [line for line in incoming.splitlines() if line and line not in existing]
    if not lines:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return True


def _write_file(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dest.parent,
            prefix=f".{dest.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, dest)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _local_tombstone_action(
    vault: Path, rel: str, incoming_hash: str, local_tombstones: dict[str, dict], conflict: str
) -> str:
    """Return write/skip/conflict/restored for a payload covered by a local tombstone."""
    record = local_tombstones.get(rel)
    if not record:
        return "write"
    deleted_hash = record.get("content_hash")
    if deleted_hash:
        dest = _safe_dest(vault, rel)
        if dest.exists() and dest.is_file() and _sha256_file(dest) != deleted_hash:
            return "restored"
    if conflict == "overwrite":
        return "write"
    if not deleted_hash:
        return "skip"
    if deleted_hash == incoming_hash:
        return "skip"
    if conflict == "skip":
        return "skip"
    return "conflict"


def _stage_import(
    zf: zipfile.ZipFile, vault: Path, members: dict[str, zipfile.ZipInfo], conflict: str
) -> tuple[list[str], list[str], list[str]]:
    local_tombstones = _latest_tombstones(vault)
    incoming_tombstones = _read_payload_tombstones(zf, members)
    conflicts: list[str] = []
    tombstone_deletes: list[str] = []
    blocked_tombstones: list[str] = []

    for record in incoming_tombstones:
        rel = record["path"]
        if rel in members:
            blocked_tombstones.append(rel)
            continue
        dest = _safe_dest(vault, rel)
        if not dest.exists():
            continue
        expected_hash = record.get("content_hash")
        if expected_hash and dest.is_file() and _sha256_file(dest) == expected_hash:
            tombstone_deletes.append(rel)
        elif conflict == "overwrite" and dest.is_file():
            tombstone_deletes.append(rel)
        elif conflict == "skip":
            blocked_tombstones.append(rel)
        else:
            conflicts.append(rel)

    incoming_tombstone_paths = {record["path"] for record in incoming_tombstones if record["path"] not in members}
    for rel, info in members.items():
        if rel in incoming_tombstone_paths:
            continue
        if rel == TOMBSTONES_REL or rel == ".sync/ledger.jsonl":
            continue
        if rel == "vault-identity.json" and (vault / rel).exists():
            continue
        incoming_hash = _sha256_bytes(zf.read(info))
        tombstone_action = _local_tombstone_action(vault, rel, incoming_hash, local_tombstones, conflict)
        if tombstone_action == "conflict":
            conflicts.append(rel)
            continue
        if tombstone_action == "skip":
            continue
        dest = _safe_dest(vault, rel)
        if not dest.exists() or not dest.is_file():
            continue
        local_hash = _sha256_file(dest)
        if incoming_hash != local_hash and conflict == "error":
            conflicts.append(rel)

    return conflicts, tombstone_deletes, blocked_tombstones


def import_archive(
    archive_path: Path,
    vault: Path,
    *,
    conflict: str = "error",
    apply_tombstones: bool = True,
) -> dict:
    """Import a portable archive into ``vault``.

    ``conflict`` controls existing divergent files: ``error`` (default),
    ``skip``, or ``overwrite``. Tombstones are always merged; when safe they are
    applied before payload writes so deleted notes do not reappear.
    """
    if conflict not in {"error", "skip", "overwrite"}:
        raise ValueError("conflict must be one of: error, skip, overwrite")

    vault = Path(vault)
    archive_path = Path(archive_path)
    vault.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path) as zf:
        manifest = _read_manifest(zf)
        members = _payload_members(zf)
        conflicts, tombstone_deletes, blocked_tombstones = _stage_import(zf, vault, members, conflict)
        if conflicts and conflict == "error":
            raise ArchiveConflictError(conflicts)

        for root_name in _manifest_content_roots(manifest):
            _safe_dest(vault, root_name).mkdir(parents=True, exist_ok=True)

        incoming_tombstones = _read_payload_tombstones(zf, members)
        blocked_tombstone_paths = set(blocked_tombstones)
        incoming_tombstone_paths = {
            record["path"] for record in incoming_tombstones if record["path"] not in blocked_tombstone_paths
        }
        local_tombstones = _latest_tombstones(vault)

        written = skipped = overwritten = tombstone_skipped = tombstoned = merged = 0
        restored_paths: set[str] = set()

        if apply_tombstones:
            for rel in tombstone_deletes:
                dest = _safe_dest(vault, rel)
                if dest.exists() and dest.is_file():
                    dest.unlink()
                    tombstoned += 1

        if incoming_tombstones:
            existing_records = {json.dumps(r, separators=(",", ":"), sort_keys=True) for r in iter_tombstones(vault)}
            to_append = [
                r
                for r in incoming_tombstones
                if r["path"] not in blocked_tombstone_paths
                and json.dumps(r, separators=(",", ":"), sort_keys=True) not in existing_records
            ]
            merged += _append_jsonl(tombstones_path(vault), to_append)

        for rel, info in members.items():
            data = zf.read(info)
            dest = _safe_dest(vault, rel)

            if rel in incoming_tombstone_paths:
                tombstone_skipped += 1
                continue

            if rel == TOMBSTONES_REL:
                continue
            if rel == ".sync/ledger.jsonl":
                if _merge_jsonl_file(dest, data.decode("utf-8")):
                    merged += 1
                else:
                    skipped += 1
                continue
            if rel == "vault-identity.json" and dest.exists():
                skipped += 1
                continue

            incoming_hash = _sha256_bytes(data)
            tombstone_action = _local_tombstone_action(vault, rel, incoming_hash, local_tombstones, conflict)
            if tombstone_action == "skip":
                tombstone_skipped += 1
                continue
            if tombstone_action == "conflict":
                raise ArchiveConflictError([rel])
            if tombstone_action == "restored":
                restored_paths.add(rel)

            if dest.exists() and dest.is_file():
                local_hash = _sha256_file(dest)
                if local_hash == incoming_hash:
                    if rel in restored_paths:
                        _restore_record(vault, rel)
                        merged += 1
                    skipped += 1
                    continue
                if conflict == "skip":
                    if rel in restored_paths:
                        _restore_record(vault, rel)
                        merged += 1
                    skipped += 1
                    continue
                if conflict == "overwrite":
                    overwritten += 1
                else:
                    raise ArchiveConflictError([rel])

            _write_file(dest, data)
            written += 1
            if rel in local_tombstones:
                _restore_record(vault, rel)
                merged += 1

    return {
        "archive_path": str(archive_path),
        "vault": str(vault),
        "vault_id": manifest.get("vault_id"),
        "written": written,
        "skipped": skipped,
        "overwritten": overwritten,
        "merged": merged,
        "tombstone_skipped": tombstone_skipped,
        "tombstoned": tombstoned,
    }


# --- Auto-archive sweep (MEM-152) ---
#
# Nothing in the vault ages out on its own: 95.5% of notes never resurface
# and there was no archival pressure. This scheduled sweep finds notes that
# are durably cold (never resurfaced -- see memento.store.durability_tier),
# old, and low-certainty, and archives them using the SAME tombstone
# machinery export_archive()/import_archive() already rely on for portable
# deletions -- never a second archive mechanism, never a hard delete.

DEFAULT_ARCHIVE_SWEEP_AGE_DAYS = 90
DEFAULT_ARCHIVE_SWEEP_MAX_PER_RUN = 50

# Certainty must be strictly below this to qualify (criterion 3). Unlike
# archive_sweep_age_days this is not config-driven: the acceptance criteria
# fix it at "certainty < 4" (below "shipped"/"established"). Revisit as a
# config knob if that turns out to need per-vault tuning.
ARCHIVE_SWEEP_CERTAINTY_CEILING = 4


def _archive_rel_for(rel: str) -> str:
    """Map a ``notes/...`` or ``fleeting/...`` vault-relative path to its ``archive/...`` counterpart.

    ``notes/...`` keeps the original MEM-152 flat mapping (``archive/<name>``)
    unchanged for backward compatibility. ``fleeting/...`` (MEM-153) nests
    under ``archive/fleeting/<name>`` instead of also flattening -- fleeting
    stems are dates (``2026-05-19``) and a flat mapping risks colliding with
    an archived ``notes/`` file that happens to share a stem.
    """
    if rel.startswith("notes/"):
        return "archive/" + rel[len("notes/") :]
    if rel.startswith("fleeting/"):
        return "archive/fleeting/" + rel[len("fleeting/") :]
    raise ValueError(f"expected a notes/ or fleeting/ relative path, got: {rel}")


def archive_note(vault: Path, notes_rel_path: str, *, reason: str = "archived", ts: str | None = None) -> dict:
    """Move a note from ``notes/`` (or ``fleeting/``, MEM-153) to ``archive/`` and record a reversible tombstone.

    Reuses the same tombstone ledger (:func:`record_tombstone`) that portable
    export/import already use to track "this path used to exist and was
    removed" -- no new archive mechanism. The file is moved, not deleted, so
    its content survives under ``archive/`` and :func:`restore_note` can move
    it back. Raises :class:`ArchiveError` if the note does not exist.
    """
    vault = Path(vault)
    rel = _normalize_rel(notes_rel_path)
    if not _is_allowed_tombstone_rel(rel):
        raise ValueError(f"unsupported archive path: {rel}")
    src = _safe_dest(vault, rel)
    if not src.exists() or not src.is_file():
        raise ArchiveError(f"note not found: {rel}")

    archive_rel = _archive_rel_for(rel)
    dest = _safe_dest(vault, archive_rel)
    content_hash = _sha256_file(src)

    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    record = record_tombstone(vault, rel, reason=reason, content_hash=content_hash, ts=ts)
    return {"path": rel, "archive_path": archive_rel, "tombstone": record}


def restore_note(vault: Path, notes_rel_path: str, *, ts: str | None = None) -> dict:
    """Reverse :func:`archive_note`: move a note back from ``archive/`` to its original location.

    ``notes_rel_path`` is the note's ORIGINAL ``notes/...`` or ``fleeting/...``
    path (as passed to ``archive_note``), not its ``archive/...`` location.
    Appends a ``reason="restored"`` tombstone record via the same restore
    path (:func:`_restore_record`) the portable-import merge logic uses, so a
    later portable export/import correctly sees the path as live again.
    """
    vault = Path(vault)
    rel = _normalize_rel(notes_rel_path)
    if not _is_allowed_tombstone_rel(rel):
        raise ValueError(f"unsupported restore path: {rel}")

    archive_rel = _archive_rel_for(rel)
    src = _safe_dest(vault, archive_rel)
    if not src.exists() or not src.is_file():
        raise ArchiveError(f"archived note not found: {archive_rel}")

    dest = _safe_dest(vault, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    record = _restore_record(vault, rel, ts=ts)
    return {"path": rel, "archive_path": archive_rel, "tombstone": record}


def _iter_vault_notes(vault: Path):
    notes_dir = Path(vault) / "notes"
    if not notes_dir.exists():
        return
    for path in sorted(p for p in notes_dir.rglob("*.md") if p.is_file() and not p.is_symlink()):
        yield path.relative_to(vault).as_posix()


def _age_days_from_date(date_str: str | None, now: datetime) -> float | None:
    """Age in days from a raw ``date:`` frontmatter scalar, or None if absent/unparseable."""
    if not date_str:
        return None
    try:
        note_date = datetime.fromisoformat(date_str)
    except ValueError:
        return None
    if note_date.tzinfo is None:
        note_date = note_date.replace(tzinfo=timezone.utc)
    return (now - note_date).total_seconds() / 86400.0


def sweep_archive_candidates(vault, *, config=None, now=None, dry_run: bool = False) -> dict:
    """Scan ``notes/`` and archive cold, aged, low-certainty notes (MEM-152).

    A note qualifies only when ALL of the following hold:

    1. Its durability tier (:func:`memento.store.read_durability_tier`) is
       ``"cold"`` -- pinned, hot, and warm notes (anything ever/recently
       resurfaced, or manually pinned) are never touched.
    2. Its ``date`` frontmatter age exceeds ``archive_sweep_age_days`` (config,
       default :data:`DEFAULT_ARCHIVE_SWEEP_AGE_DAYS`). Notes without a
       parseable ``date`` are skipped -- age cannot be proven, so the fail-safe
       is to leave them alone.
    3. Its ``certainty`` frontmatter is present and strictly below
       :data:`ARCHIVE_SWEEP_CERTAINTY_CEILING`. Notes without a parseable
       certainty are skipped for the same fail-safe reason.

    Gated by ``archive_sweep_enabled`` (config, default ``False``): when
    disabled this is a no-op that logs one line and returns immediately
    without scanning the vault. ``archive_sweep_max_per_run`` (config, default
    :data:`DEFAULT_ARCHIVE_SWEEP_MAX_PER_RUN`) caps how many notes get
    archived in one call regardless of how many candidates qualify --
    overflow candidates are reported as skipped, never archived.

    ``dry_run=True`` runs the full scan and returns the same report shape,
    but never moves a file or writes a tombstone.

    Mutations (the actual archive step) happen under the vault write lock,
    re-entrant like :func:`memento.store.fold_access_log_into_frontmatter` --
    acquires only if not already held, never releases a lock a caller holds.

    Returns a report dict with keys ``enabled``, ``dry_run``,
    ``age_days_threshold``, ``max_per_run``, ``candidates`` (list of
    ``{path, tier, age_days, certainty}`` for every note matching all three
    criteria), ``archived`` (list of the same shape plus ``archive_path`` for
    notes actually archived), and ``skipped`` (list of ``{path, reason}``).
    """
    from memento.config import get_config as _get_config
    from memento.store import (
        _frontmatter_int,
        _frontmatter_scalar,
        acquire_vault_write_lock,
        owns_vault_write_lock,
        read_durability_tier,
        release_vault_write_lock,
        split_frontmatter,
    )

    vault = Path(vault)
    if config is None:
        config = _get_config()
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    enabled = bool(config.get("archive_sweep_enabled", False))
    age_threshold = config.get("archive_sweep_age_days", DEFAULT_ARCHIVE_SWEEP_AGE_DAYS)
    max_per_run = config.get("archive_sweep_max_per_run", DEFAULT_ARCHIVE_SWEEP_MAX_PER_RUN)

    report = {
        "enabled": enabled,
        "dry_run": dry_run,
        "age_days_threshold": age_threshold,
        "max_per_run": max_per_run,
        "candidates": [],
        "archived": [],
        "skipped": [],
    }

    if not enabled:
        print("[memento] archive sweep disabled (archive_sweep_enabled: false) -- skipping", file=sys.stderr)
        return report

    for rel in _iter_vault_notes(vault):
        try:
            text = (vault / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report["skipped"].append({"path": rel, "reason": f"unreadable: {exc}"})
            continue

        frontmatter, _ = split_frontmatter(text)

        tier = read_durability_tier(vault, rel, config=config, now=now)
        if tier != "cold":
            continue  # criterion 1: pinned/hot/warm are never candidates

        age_days = _age_days_from_date(_frontmatter_scalar(frontmatter, "date"), now)
        if age_days is None or age_days <= age_threshold:
            continue  # criterion 2

        certainty = _frontmatter_int(frontmatter, "certainty")
        if certainty is None or certainty >= ARCHIVE_SWEEP_CERTAINTY_CEILING:
            continue  # criterion 3

        report["candidates"].append({"path": rel, "tier": tier, "age_days": round(age_days, 1), "certainty": certainty})

    if dry_run or not report["candidates"]:
        return report

    to_archive = report["candidates"][:max_per_run]
    overflow = report["candidates"][max_per_run:]
    for candidate in overflow:
        report["skipped"].append(
            {"path": candidate["path"], "reason": f"archive_sweep_max_per_run cap ({max_per_run}) reached"}
        )

    if not to_archive:
        return report

    already_held = owns_vault_write_lock()
    if not already_held and not acquire_vault_write_lock():
        for candidate in to_archive:
            report["skipped"].append({"path": candidate["path"], "reason": "vault write lock unavailable"})
        return report

    try:
        for candidate in to_archive:
            try:
                result = archive_note(vault, candidate["path"])
            except (ArchiveError, ValueError, OSError) as exc:
                report["skipped"].append({"path": candidate["path"], "reason": f"archive failed: {exc}"})
                continue
            report["archived"].append({**candidate, "archive_path": result["archive_path"]})
            print(
                f"[memento] archived {candidate['path']} "
                f"(tier={candidate['tier']}, age={candidate['age_days']}d, certainty={candidate['certainty']})",
                file=sys.stderr,
            )
    finally:
        if not already_held:
            release_vault_write_lock()

    return report


# --- Fleeting note lifecycle (MEM-153) ---
#
# fleeting/<YYYY-MM-DD>.md notes (memento.store.append_fleeting_session)
# accumulate forever today -- nothing promotes them to a durable notes/
# entry and nothing expires them. This closes that gap by reusing the SAME
# archive_note()/restore_note() tombstone machinery as the MEM-152 sweep
# above (extended, not forked, via the notes/-or-fleeting/ branches in
# _is_allowed_tombstone_rel/_archive_rel_for) -- never a second archive
# path.

DEFAULT_FLEETING_PROMOTE_MIN_RESURFACED = 2
DEFAULT_FLEETING_EXPIRE_DAYS = 14

# docs/frontmatter-schema.md's "Source values" table describes `source:
# mcp-capture` as "`memento_capture` session-summary note writer" -- there is
# no distinct `type: session-summary` frontmatter enum value anywhere in the
# schema (Note types are decision/discovery/pattern/bugfix/tool/architecture/
# daily; mcp-capture notes are written with `type: discovery`). This is the
# literal, evidenced signal used below to recognize "a session-summary note"
# for fleeting-note promotion-by-citation.
SESSION_SUMMARY_SOURCE = "mcp-capture"

# Matches `[[stem]]` and `[[stem|alias]]`, not `[[stem#heading]]` -- section
# links point at a heading within a note, not necessarily citing the whole
# thing, so they are deliberately excluded from the citation signal. The
# trailing `(?:\]\]|\|)` lookahead-by-match is what rejects `#heading`: once
# the captured run hits `#` it can't reach a closing `]]` or `|`, so the
# whole pattern fails to match at that position instead of silently
# truncating the capture at the `#`.
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\]\]|\|)")


def _iter_fleeting_notes(vault: Path):
    fleeting_dir = Path(vault) / "fleeting"
    if not fleeting_dir.exists():
        return
    for path in sorted(p for p in fleeting_dir.rglob("*.md") if p.is_file() and not p.is_symlink()):
        yield path.relative_to(vault).as_posix()


def _is_cited_by_session_summary(vault: Path, stem: str) -> bool:
    """True if any ``notes/`` session-summary note wikilinks ``stem``.

    A plain content scan (regex over the raw body), not the wikilink graph --
    MEM-153 is explicitly scoped to avoid a networkx/graph.py dependency for
    this check.
    """
    notes_dir = Path(vault) / "notes"
    if not notes_dir.exists():
        return False

    from memento.store import _frontmatter_scalar, split_frontmatter

    for path in sorted(p for p in notes_dir.rglob("*.md") if p.is_file() and not p.is_symlink()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        frontmatter, body = split_frontmatter(text)
        if _frontmatter_scalar(frontmatter, "source") != SESSION_SUMMARY_SOURCE:
            continue
        if any(match.group(1).strip() == stem for match in _WIKILINK_RE.finditer(body)):
            return True
    return False


def _fleeting_age_days(path: Path, frontmatter: str, now: datetime):
    """Age in days: ``date`` frontmatter first, else the file's mtime, else ``None``.

    ``None`` means neither signal was available -- callers must skip, never
    guess an age to archive against.
    """
    from memento.store import _frontmatter_scalar

    age = _age_days_from_date(_frontmatter_scalar(frontmatter, "date"), now)
    if age is not None:
        return age
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return (now - mtime).total_seconds() / 86400.0


def _fleeting_notes_target(vault: Path, rel: str) -> tuple[str, Path]:
    """Compute the ``notes/`` destination for promoting ``fleeting/<name>.md``.

    Mirrors ``memento.store.write_note``'s numeric-suffix-on-collision
    behavior so a promoted fleeting note never silently overwrites an
    existing notes/ file with the same stem.
    """
    if not rel.startswith("fleeting/"):
        raise ValueError(f"expected a fleeting/ relative path, got: {rel}")
    name = rel[len("fleeting/") :]
    notes_dir = Path(vault) / "notes"
    dest = notes_dir / name
    if not dest.exists():
        return f"notes/{name}", dest

    stem = Path(name).stem
    suffix = Path(name).suffix
    for i in range(2, 100):
        candidate_name = f"{stem}-{i}{suffix}"
        candidate_dest = notes_dir / candidate_name
        if not candidate_dest.exists():
            return f"notes/{candidate_name}", candidate_dest
    raise ArchiveError(f"could not find a free notes/ destination for {rel}")


def promote_fleeting_note(vault: Path, fleeting_rel: str, *, now: datetime | None = None) -> dict:
    """Promote ``fleeting/<x>.md`` to ``notes/<x>.md``, stamping ``promoted_at`` (MEM-153).

    Adds a ``promoted_at: <ISO date>`` frontmatter line while preserving
    every other frontmatter line verbatim (fleeting notes as written by
    :func:`memento.store.append_fleeting_session` carry no YAML frontmatter
    block at all today, so there is usually nothing to preserve -- but this
    round-trips any that do exist, the same way
    ``memento.store.replace_note_at_path``/``_fold_note_frontmatter``
    preserve unmanaged keys elsewhere). Atomic move (``os.replace``) plus a
    single atomic rewrite of the destination via ``_write_text_atomic`` --
    never a full ``write_note()`` re-render that would rebuild the body.
    """
    from memento.store import _write_text_atomic, split_frontmatter

    vault = Path(vault)
    rel = _normalize_rel(fleeting_rel)
    if not rel.startswith("fleeting/") or not rel.endswith(".md"):
        raise ValueError(f"unsupported promotion path: {rel}")
    src = _safe_dest(vault, rel)
    if not src.exists() or not src.is_file():
        raise ArchiveError(f"fleeting note not found: {rel}")

    target_rel, dest = _fleeting_notes_target(vault, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    promoted_at = now.strftime("%Y-%m-%d")

    text = src.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(text)
    preserved = [line for line in (frontmatter or "").splitlines() if line.strip()]
    preserved.append(f"promoted_at: {promoted_at}")
    new_text = "---\n" + "\n".join(preserved) + "\n---\n" + body

    os.replace(src, dest)
    _write_text_atomic(dest, new_text)

    return {"path": rel, "notes_path": target_rel, "promoted_at": promoted_at}


def fleeting_lifecycle_sweep(vault, *, config=None, now=None, dry_run: bool = False) -> dict:
    """Promote or expire ``fleeting/`` notes on the sweep cadence (MEM-153).

    Promotion (``fleeting/<x>.md`` -> ``notes/<x>.md`` via
    :func:`promote_fleeting_note`) happens when EITHER:

    1. ``resurfaced_count`` frontmatter is >= ``fleeting_promote_min_resurfaced``
       (config, default :data:`DEFAULT_FLEETING_PROMOTE_MIN_RESURFACED`) --
       the same durable resurfacing signal MEM-148 folds from the access log
       (:func:`memento.store.fold_access_log_into_frontmatter`). A fleeting
       note with no frontmatter block reads as 0 -- not an error, just "no
       signal yet."
    2. It is cited by a session-summary note: a ``[[stem]]`` wikilink to the
       fleeting note's filename stem, appearing in the body of any
       ``notes/*.md`` note whose ``source`` frontmatter is
       :data:`SESSION_SUMMARY_SOURCE` (see :func:`_is_cited_by_session_summary`).

    Expiry (:func:`archive_note` with ``reason="fleeting_expired"``,
    reversible via :func:`restore_note`, same tombstone ledger) happens for
    anything left over whose age -- ``date`` frontmatter first, file mtime
    otherwise (:func:`_fleeting_age_days`) -- exceeds ``fleeting_expire_days``
    (config, default :data:`DEFAULT_FLEETING_EXPIRE_DAYS`). A note with
    neither a parseable ``date`` nor a readable mtime is skipped, never
    archived on ambiguity -- and a note under the expiry threshold that also
    didn't qualify for promotion is left untouched, not reported.

    Gated by ``fleeting_lifecycle_enabled`` (config, default ``False``): when
    disabled this is a no-op that logs one line and returns immediately.
    ``dry_run=True`` runs the full scan and returns the same report shape
    without moving, rewriting, or archiving anything.

    Mutations happen under the vault write lock, re-entrant like
    :func:`sweep_archive_candidates` -- acquires only if not already held,
    never releases a lock a caller holds.

    Returns a report dict: ``{enabled, dry_run, promoted, expired, skipped}``
    -- ``promoted``/``expired`` are lists of ``{path, ...}`` dicts, ``skipped``
    is a list of ``{path, reason}``.
    """
    from memento.config import get_config as _get_config
    from memento.store import _frontmatter_int, split_frontmatter

    vault = Path(vault)
    if config is None:
        config = _get_config()
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    enabled = bool(config.get("fleeting_lifecycle_enabled", False))
    min_resurfaced = config.get("fleeting_promote_min_resurfaced", DEFAULT_FLEETING_PROMOTE_MIN_RESURFACED)
    expire_days = config.get("fleeting_expire_days", DEFAULT_FLEETING_EXPIRE_DAYS)

    report: dict = {
        "enabled": enabled,
        "dry_run": dry_run,
        "promoted": [],
        "expired": [],
        "skipped": [],
    }

    if not enabled:
        print(
            "[memento] fleeting lifecycle disabled (fleeting_lifecycle_enabled: false) -- skipping",
            file=sys.stderr,
        )
        return report

    to_promote = []
    to_expire = []

    for rel in _iter_fleeting_notes(vault):
        path = vault / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report["skipped"].append({"path": rel, "reason": f"unreadable: {exc}"})
            continue

        frontmatter, _ = split_frontmatter(text)
        stem = Path(rel).stem

        resurfaced_count = _frontmatter_int(frontmatter, "resurfaced_count") or 0
        if resurfaced_count >= min_resurfaced:
            to_promote.append({"path": rel, "reason": f"resurfaced_count >= {min_resurfaced}"})
            continue
        if _is_cited_by_session_summary(vault, stem):
            to_promote.append({"path": rel, "reason": "cited by session-summary note"})
            continue

        age_days = _fleeting_age_days(path, frontmatter, now)
        if age_days is None:
            report["skipped"].append({"path": rel, "reason": "no date frontmatter and no readable mtime"})
            continue
        if age_days > expire_days:
            to_expire.append({"path": rel, "age_days": round(age_days, 1)})
        # else: under the expiry threshold and not promoted -- left alone.

    if dry_run:
        report["promoted"] = to_promote
        report["expired"] = to_expire
        return report

    if not to_promote and not to_expire:
        return report

    from memento.store import acquire_vault_write_lock, owns_vault_write_lock, release_vault_write_lock

    already_held = owns_vault_write_lock()
    if not already_held and not acquire_vault_write_lock():
        for candidate in [*to_promote, *to_expire]:
            report["skipped"].append({"path": candidate["path"], "reason": "vault write lock unavailable"})
        return report

    try:
        for candidate in to_promote:
            try:
                result = promote_fleeting_note(vault, candidate["path"], now=now)
            except (ArchiveError, ValueError, OSError) as exc:
                report["skipped"].append({"path": candidate["path"], "reason": f"promotion failed: {exc}"})
                continue
            report["promoted"].append({**candidate, **result})
            print(
                f"[memento] promoted {candidate['path']} -> {result['notes_path']} ({candidate['reason']})",
                file=sys.stderr,
            )

        for candidate in to_expire:
            try:
                result = archive_note(vault, candidate["path"], reason="fleeting_expired")
            except (ArchiveError, ValueError, OSError) as exc:
                report["skipped"].append({"path": candidate["path"], "reason": f"expire failed: {exc}"})
                continue
            report["expired"].append({**candidate, "archive_path": result["archive_path"]})
            print(
                f"[memento] expired {candidate['path']} (age={candidate['age_days']}d)",
                file=sys.stderr,
            )
    finally:
        if not already_held:
            release_vault_write_lock()

    return report


def _json_print(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export/import portable memento vault archives.")
    sub = parser.add_subparsers(dest="command", required=True)

    export_p = sub.add_parser("export", help="Export a vault to a portable zip archive")
    export_p.add_argument("archive", type=Path, help="Destination archive path")
    export_p.add_argument("--vault", type=Path, default=None, help="Vault path (defaults to configured vault)")

    import_p = sub.add_parser("import", help="Import a portable zip archive into a vault")
    import_p.add_argument("archive", type=Path, help="Archive to import")
    import_p.add_argument("--vault", type=Path, default=None, help="Vault path (defaults to configured vault)")
    import_p.add_argument("--conflict", choices=("error", "skip", "overwrite"), default="error")
    import_p.add_argument(
        "--no-apply-tombstones", action="store_true", help="Merge tombstones without deleting matches"
    )
    return parser


def _default_vault() -> Path:
    from memento.config import get_vault

    return get_vault()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    vault = args.vault or _default_vault()
    try:
        if args.command == "export":
            _json_print(export_archive(vault, args.archive))
        elif args.command == "import":
            _json_print(
                import_archive(
                    args.archive, vault, conflict=args.conflict, apply_tombstones=not args.no_apply_tombstones
                )
            )
        else:
            parser.error(f"unknown command: {args.command}")
    except ArchiveError as exc:
        print(f"memento archive error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
