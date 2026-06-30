"""Portable vault archive export/import with tombstone-aware merges."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    return rel.startswith("notes/") and rel.endswith(".md")


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
