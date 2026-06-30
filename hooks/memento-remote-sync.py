#!/usr/bin/env python3
"""Sync local vault notes to the remote vault via memento_store.

Usage:
  memento-remote-sync.py [--resolve-conflicts local] <note-path> [<note-path> ...]
  memento-remote-sync.py --catch-up [--dry-run] [--batch N] [--resolve-conflicts local]

Reads each markdown note, parses frontmatter, and calls remote_client.store().
No-op if MEMENTO_VAULT_URL is not set.

--catch-up walks all notes/*.md in the vault and syncs any that the ledger
hasn't recorded as successfully pushed. Pairs with --dry-run to preview
and --batch N to limit how many notes to sync per run (default: all).

Conflicts are skipped by default to avoid append-only duplicates. Passing
--resolve-conflicts local replaces the identified remote note with the local
note and records a fresh ok ledger entry.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memento import sync_ledger  # noqa: E402
from memento.archive import latest_active_tombstones  # noqa: E402
from memento.config import get_vault, slugify  # noqa: E402
from memento.remote_client import get, is_remote, replace_note, store  # noqa: E402


def _meaningful_body(body):
    """Drop empty trailing Related headings added by note writers."""
    body = body.strip()
    while body.endswith("## Related"):
        body = body[: -len("## Related")].rstrip()
    return body


def parse_note_text(raw, fallback_title):
    """Parse markdown note text into title, body, type, and tags."""
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None

    fm, body = parts[1], _meaningful_body(parts[2])
    if not body:
        return None

    title_m = re.search(r"^title:\s*(.+)$", fm, re.MULTILINE)
    title = title_m.group(1).strip().strip("\"'") if title_m else fallback_title

    type_m = re.search(r"^type:\s*(.+)$", fm, re.MULTILINE)
    note_type = type_m.group(1).strip() if type_m else "discovery"

    tags_m = re.search(r"^tags:\s*\[(.+)\]", fm, re.MULTILINE)
    tags = []
    if tags_m:
        tags = [t.strip().strip("\"'") for t in tags_m.group(1).split(",")]

    certainty_m = re.search(r"^certainty:\s*(\d+)", fm, re.MULTILINE)
    certainty = int(certainty_m.group(1)) if certainty_m else None

    project_m = re.search(r"^project:\s*(.+)$", fm, re.MULTILINE)
    project = project_m.group(1).strip() if project_m else None

    branch_m = re.search(r"^branch:\s*(.+)$", fm, re.MULTILINE)
    branch = branch_m.group(1).strip() if branch_m else None

    validity_m = re.search(r"^validity-context:\s*(.+)$", fm, re.MULTILINE)
    validity = validity_m.group(1).strip() if validity_m else None

    return {
        "title": title,
        "body": body,
        "note_type": note_type,
        "tags": tags,
        "certainty": certainty,
        "project": project,
        "branch": branch,
        "validity_context": validity,
    }


def parse_note(path):
    """Parse a markdown note into title, body, type, and tags."""
    return parse_note_text(Path(path).read_text(), Path(path).stem)


def _sync_payload(note: dict) -> str:
    """Stable string fed to content_hash — changes here invalidate prior hashes."""
    return "\n".join(
        [
            note.get("title", ""),
            note.get("note_type", ""),
            ",".join(note.get("tags") or []),
            str(note.get("certainty") or ""),
            note.get("project") or "",
            note.get("branch") or "",
            note.get("validity_context") or "",
            note.get("body", ""),
        ]
    )


def _remote_note_path(note: dict) -> str:
    return f"notes/{slugify(note.get('title', ''))}.md"


def _payload_hash(note: dict) -> str:
    return sync_ledger.content_hash(_sync_payload(note))


_FETCH_FAILED = object()


def _safe_get_remote(path: str) -> dict | None | object:
    try:
        return get(path)
    except Exception as exc:
        print(f"  Warning: could not fetch remote note {path}: {exc}", file=sys.stderr)
        return _FETCH_FAILED


def _remote_fetch_failed(remote: object) -> bool:
    return remote is _FETCH_FAILED


def _remote_note_payload_hash(remote: dict) -> str | None:
    remote_note = parse_note_text(remote.get("content", ""), Path(remote.get("path", "remote")).stem)
    return _payload_hash(remote_note) if remote_note else None


def _same_remote_payload(remote: dict, note: dict) -> bool:
    return _remote_note_payload_hash(remote) == _payload_hash(note)


def _title_key(title: str | None) -> str:
    return " ".join((title or "").casefold().split())


def _remote_title_matches(remote_notes: list[dict], title: str) -> list[dict]:
    key = _title_key(title)
    return [r for r in remote_notes if _title_key(r.get("title")) == key]


def _dry_run_note(note: dict, resolve_conflicts: str = "") -> str:
    """Return the dry-run disposition for a note without writing remotely."""
    remote_path = _remote_note_path(note)
    remote = _safe_get_remote(remote_path)
    if _remote_fetch_failed(remote):
        return "fetch-error"
    if not remote:
        return "create"

    if _same_remote_payload(remote, note):
        return "skip"
    if resolve_conflicts == "local":
        return "resolve"
    return "conflict"


def _replace_remote_note(vault, source, remote_path, note, chash, dry_run=False) -> bool:
    """Replace remote_path with note. Returns True on success/dry-run success."""
    if dry_run:
        print(f"  Would resolve (local overwrites remote): {note['title']} -> {remote_path}")
        return True

    result = replace_note(remote_path, **note)
    if isinstance(result, dict) and "error" in result:
        print(f"  Error resolving {note['title']} -> {remote_path}: {result['error']}", file=sys.stderr)
        if vault:
            sync_ledger.record(
                vault,
                "note",
                source,
                status="error",
                content_hash=chash,
                error=result["error"],
            )
        return False

    resolved_path = result.get("path", remote_path) if isinstance(result, dict) else remote_path
    if vault:
        sync_ledger.record(
            vault,
            "note",
            source,
            status="ok",
            content_hash=chash,
            remote_path=resolved_path,
        )
    print(f"  Resolved: {note['title']} -> {resolved_path}")
    return True


def _build_ledger_index(vault):
    """Build lookup indexes from the sync ledger.

    Returns:
        by_source: {source_path: entry} — latest entry per local source
        remote_paths: set of remote_path values from successful pushes
    """
    by_source = {}
    remote_paths = set()
    for entry in sync_ledger.iter_entries(vault):
        if entry.get("kind") != "note":
            continue
        source = entry.get("source", "")
        if source:
            by_source[source] = entry
        if entry.get("status") == "ok" and entry.get("remote_path"):
            remote_paths.add(Path(entry["remote_path"]).name)
    return by_source, remote_paths


def _latest_tombstones_by_path(vault):
    """Return latest active local deletion tombstones keyed by source path."""
    return latest_active_tombstones(vault)


def _is_tombstoned_file(vault, source, path, tombstones):
    """True when local tombstone state says this note must not be synced."""
    record = tombstones.get(source)
    if not record:
        return False
    expected_hash = record.get("content_hash")
    if not expected_hash:
        return True
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return True
    return sync_ledger.content_hash(raw) == expected_hash


def catch_up(vault, dry_run=False, batch=0, resolve_conflicts=""):
    """Walk local notes and push anything missing from the remote.

    Uses three layers to determine what needs pushing:
    1. Remote inventory by filename — catches notes pushed by other means.
    2. Sync ledger by source + content hash — catches notes whose remote
       filename differs from the local one (slugification, dedupe suffixes).
    3. Remote inventory by unique title + content fetch — recovers from a lost
       local ledger without re-pushing title-equivalent remote notes.

    Unrecognized mismatches remain conflicts by default because store() is
    append-only and would create duplicates. Passing resolve_conflicts="local"
    replaces the identified remote path with the local note and records a fresh
    ok ledger entry.
    """
    import hashlib
    from memento.remote_client import list_notes

    notes_dir = vault / "notes"
    if not notes_dir.exists():
        print("  No local notes directory.", file=sys.stderr)
        return

    print("  Fetching remote inventory...")
    remote_notes = list_notes(include_hash=True)
    if remote_notes is None:
        print("  Catch-up aborted: could not fetch remote inventory.", file=sys.stderr)
        print("  Check MEMENTO_VAULT_URL, network, and that the server supports memento_list.", file=sys.stderr)
        sys.exit(2)

    remote_by_name = {Path(r["path"]).name: r for r in remote_notes}

    ledger_by_source, _ledger_remote_paths = _build_ledger_index(vault)
    tombstones_by_source = _latest_tombstones_by_path(vault)

    local_files = sorted(notes_dir.glob("*.md"))
    to_push = []
    conflicts = []
    ledger_skipped = 0
    recovered_ledger = 0
    tombstone_skipped = 0

    for f in local_files:
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue

        local_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        remote = remote_by_name.get(f.name)

        source = str(f.relative_to(vault))

        if _is_tombstoned_file(vault, source, f, tombstones_by_source):
            tombstone_skipped += 1
            continue

        note = parse_note(f)
        if not note:
            to_push.append(f)
            continue
        chash = _payload_hash(note)

        if remote is not None:
            remote_path = remote.get("path", f"notes/{f.name}")
            if remote.get("hash") == local_hash:
                if sync_ledger.last_success_hash(vault, "note", source) != chash and not dry_run:
                    sync_ledger.record(vault, "note", source, status="ok", content_hash=chash, remote_path=remote_path)
                    recovered_ledger += 1
                continue  # identical by filename match

            ledger_entry = ledger_by_source.get(source)
            if ledger_entry and ledger_entry.get("status") == "ok" and chash == ledger_entry.get("content_hash"):
                ledger_skipped += 1
                continue

            title_matches = _remote_title_matches(remote_notes, note.get("title", ""))
            if title_matches:
                matched_path = None
                candidate_paths = []
                for title_match in title_matches:
                    candidate_path = title_match.get("path")
                    if not candidate_path:
                        continue
                    candidate_paths.append(candidate_path)
                    remote_full = _safe_get_remote(candidate_path)
                    if _remote_fetch_failed(remote_full):
                        continue
                    if remote_full and _same_remote_payload(remote_full, note):
                        matched_path = candidate_path
                        break
                if matched_path:
                    if not dry_run:
                        sync_ledger.record(
                            vault, "note", source, status="ok", content_hash=chash, remote_path=matched_path
                        )
                        recovered_ledger += 1
                    ledger_skipped += 1
                    continue
                if len(candidate_paths) > 1:
                    conflicts.append((f, ", ".join(candidate_paths)))
                    continue

            conflicts.append((f, remote_path))
            continue

        # No filename match — check the ledger for a prior successful push.
        ledger_entry = ledger_by_source.get(source)
        if ledger_entry and ledger_entry.get("status") == "ok":
            if chash == ledger_entry.get("content_hash"):
                ledger_skipped += 1
                continue
            remote_name = Path(ledger_entry.get("remote_path", "")).name
            if remote_name and remote_name in remote_by_name:
                conflicts.append((f, ledger_entry.get("remote_path")))
                continue

        # Missing-ledger recovery: a remote note with the same unique title may
        # be the previously pushed copy under a different filename. Fetch it and
        # compare semantic payloads before deciding to append a new remote note.
        title_matches = _remote_title_matches(remote_notes, note.get("title", ""))
        if title_matches:
            matched_path = None
            candidate_paths = []
            for title_match in title_matches:
                remote_path = title_match.get("path")
                if not remote_path:
                    continue
                candidate_paths.append(remote_path)
                remote_full = _safe_get_remote(remote_path)
                if _remote_fetch_failed(remote_full):
                    continue
                if remote_full and _same_remote_payload(remote_full, note):
                    matched_path = remote_path
                    break
            if matched_path:
                if not dry_run:
                    sync_ledger.record(vault, "note", source, status="ok", content_hash=chash, remote_path=matched_path)
                    recovered_ledger += 1
                ledger_skipped += 1
                continue
            conflicts.append((f, ", ".join(candidate_paths) or "same-title remote"))
            continue

        to_push.append(f)

    resolved = 0
    resolution_attempts = 0
    if resolve_conflicts == "local":
        remaining_conflicts = []
        resolution_batch = conflicts if batch <= 0 else conflicts[:batch]
        deferred_conflicts = [] if batch <= 0 else conflicts[batch:]
        for f, remote_path in resolution_batch:
            note = parse_note(f)
            if not note or ", " in remote_path or not remote_path.startswith("notes/"):
                remaining_conflicts.append((f, remote_path))
                continue
            source = str(f.relative_to(vault))
            chash = _payload_hash(note)
            resolution_attempts += 1
            if _replace_remote_note(vault, source, remote_path, note, chash, dry_run=dry_run):
                resolved += 1
            else:
                remaining_conflicts.append((f, remote_path))
        conflicts = remaining_conflicts + deferred_conflicts

    for f, remote_path in conflicts:
        print(f"  Conflict (remote differs, skipped): {f.name} -> {remote_path}")

    if batch > 0:
        remaining_batch = max(batch - resolution_attempts, 0) if resolve_conflicts == "local" else batch
        to_push = to_push[:remaining_batch]

    pushed = 0
    skipped = 0
    errors = 0

    for f in to_push:
        note = parse_note(f)
        if not note:
            skipped += 1
            continue

        if dry_run:
            print(f"  Would push: {note['title']}")
            pushed += 1
            continue

        source = str(f.relative_to(vault))
        chash = _payload_hash(note)

        result = store(**note)

        if isinstance(result, dict) and "error" in result:
            print(f"  Error: {note['title']} -> {result['error']}", file=sys.stderr)
            sync_ledger.record(
                vault,
                "note",
                source,
                status="error",
                content_hash=chash,
                error=result["error"],
            )
            errors += 1
        else:
            remote_path = result.get("path", "?")
            sync_ledger.record(
                vault,
                "note",
                source,
                status="ok",
                content_hash=chash,
                remote_path=remote_path,
            )
            print(f"  Synced: {note['title']} -> {remote_path}")
            pushed += 1

    action = "Would push" if dry_run else "Pushed"
    print(
        f"  Catch-up: {action} {pushed}, conflicts {len(conflicts)}, resolved {resolved}, "
        f"skipped {skipped}, tombstoned {tombstone_skipped}, "
        f"ledger-matched {ledger_skipped}, ledger-recovered {recovered_ledger}, errors {errors} "
        f"(of {len(local_files)} local, {len(remote_notes)} remote)"
    )


def main():
    if not is_remote():
        sys.exit(0)

    args = sys.argv[1:]
    dry_run = False
    if "--dry-run" in args:
        dry_run = True
        args = [arg for arg in args if arg != "--dry-run"]

    catch_up_mode = False
    if "--catch-up" in args:
        catch_up_mode = True
        args = [arg for arg in args if arg != "--catch-up"]

    resolve_conflicts = ""
    if "--resolve-conflicts" in args:
        idx = args.index("--resolve-conflicts")
        if idx + 1 < len(args):
            resolve_conflicts = args[idx + 1]
            args = args[:idx] + args[idx + 2 :]
        else:
            print("Missing value for --resolve-conflicts. Use --resolve-conflicts local.", file=sys.stderr)
            sys.exit(1)
    else:
        for arg in list(args):
            if arg.startswith("--resolve-conflicts="):
                resolve_conflicts = arg.split("=", 1)[1]
                args.remove(arg)
                break
    if resolve_conflicts in {"local-wins", "local-overwrite"}:
        resolve_conflicts = "local"
    if resolve_conflicts and resolve_conflicts != "local":
        print("Unsupported conflict resolution. Use --resolve-conflicts local.", file=sys.stderr)
        sys.exit(1)

    batch = 0
    if "--batch" in args:
        idx = args.index("--batch")
        if idx + 1 < len(args):
            batch = int(args[idx + 1])
            args = args[:idx] + args[idx + 2 :]
        else:
            args = args[:idx]

    if catch_up_mode:
        try:
            vault = get_vault()
        except Exception:
            print("Could not determine vault path.", file=sys.stderr)
            sys.exit(1)
        catch_up(vault, dry_run=dry_run, batch=batch, resolve_conflicts=resolve_conflicts)
        return

    if not args:
        print(
            "Usage: memento-remote-sync.py [--dry-run] [--resolve-conflicts local] <note-path> [...]",
            file=sys.stderr,
        )
        print(
            "       memento-remote-sync.py --catch-up [--dry-run] [--batch N] [--resolve-conflicts local]",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        vault = get_vault()
    except Exception:
        vault = None

    tombstones_by_source = _latest_tombstones_by_path(vault) if vault else {}
    failures = 0

    for path in args:
        if not os.path.exists(path):
            print(f"  Error (not found): {path}", file=sys.stderr)
            failures += 1
            continue

        # Stable source key (relative to vault when possible, so moving the
        # vault root doesn't break idempotency).
        source = path
        if vault:
            try:
                source = str(Path(path).resolve().relative_to(vault.resolve()))
            except ValueError:
                pass

        if vault and _is_tombstoned_file(vault, source, path, tombstones_by_source):
            print(f"  Skip (tombstoned): {source}")
            continue

        note = parse_note(path)
        if not note:
            print(f"  Error (empty/unparseable): {path}", file=sys.stderr)
            failures += 1
            continue

        if dry_run:
            disposition = _dry_run_note(note, resolve_conflicts=resolve_conflicts)
            if disposition == "skip":
                print(f"  Would skip (remote exists, same content): {note['title']}")
            elif disposition == "conflict":
                print(f"  Would conflict (remote exists, different content): {note['title']}")
            elif disposition == "resolve":
                print(f"  Would resolve (local overwrites remote): {note['title']}")
            elif disposition == "fetch-error":
                print(f"  Would fail (remote fetch failed): {note['title']}", file=sys.stderr)
                failures += 1
            else:
                print(f"  Would create: {note['title']}")
            continue

        chash = _payload_hash(note)

        # Skip if this exact payload was already acknowledged by the remote.
        if vault and sync_ledger.last_success_hash(vault, "note", source) == chash:
            print(f"  Skip (already synced): {note['title']}")
            continue

        remote_path = _remote_note_path(note)
        if vault:
            ledger_entry = sync_ledger.fold_state(vault).get(("note", source))
            if ledger_entry and ledger_entry.get("remote_path"):
                remote_path = ledger_entry["remote_path"]
        remote = _safe_get_remote(remote_path)
        if _remote_fetch_failed(remote):
            print(f"  Error (remote fetch failed): {note['title']} -> {remote_path}", file=sys.stderr)
            failures += 1
            continue
        if not remote and remote_path != _remote_note_path(note):
            remote_path = _remote_note_path(note)
            remote = _safe_get_remote(remote_path)
        if _remote_fetch_failed(remote):
            print(f"  Error (remote fetch failed): {note['title']} -> {remote_path}", file=sys.stderr)
            failures += 1
            continue
        if remote:
            if _same_remote_payload(remote, note):
                if vault:
                    sync_ledger.record(
                        vault,
                        "note",
                        source,
                        status="ok",
                        content_hash=chash,
                        remote_path=remote.get("path", remote_path),
                    )
                print(f"  Skip (remote exists, same content): {note['title']}")
                continue
            if resolve_conflicts == "local":
                ok = _replace_remote_note(
                    vault,
                    source,
                    remote.get("path", remote_path),
                    note,
                    chash,
                    dry_run=False,
                )
                failures += 0 if ok else 1
                continue

            print(
                f"  Conflict (remote exists, different content): {note['title']} -> {remote.get('path', remote_path)}",
                file=sys.stderr,
            )
            failures += 1
            continue

        result = store(**note)

        if isinstance(result, dict) and "error" in result:
            if vault:
                spool_path = sync_ledger.spool_payload(vault, "note", source, _sync_payload(note))
                sync_ledger.record(
                    vault,
                    "note",
                    source,
                    status="error",
                    content_hash=chash,
                    error=result["error"],
                    spool_path=str(spool_path),
                )
            print(f"  Error: {note['title']} -> {result['error']}", file=sys.stderr)
            failures += 1
            continue

        if vault:
            sync_ledger.record(
                vault,
                "note",
                source,
                status="ok",
                content_hash=chash,
                remote_path=result.get("path"),
            )

        remote_path = result.get("path", "unknown")
        print(f"  Synced: {note['title']} -> {remote_path}")

    if failures:
        print(f"  Sync failed for {failures} note(s).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
