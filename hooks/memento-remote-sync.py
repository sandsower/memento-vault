#!/usr/bin/env python3
"""Sync local vault notes to the remote vault via memento_store.

Usage:
  memento-remote-sync.py <note-path> [<note-path> ...]
  memento-remote-sync.py --catch-up [--dry-run] [--batch N]

Reads each markdown note, parses frontmatter, and calls remote_client.store().
No-op if MEMENTO_VAULT_URL is not set.

--catch-up walks all notes/*.md in the vault and syncs any that the ledger
hasn't recorded as successfully pushed. Pairs with --dry-run to preview
and --batch N to limit how many notes to sync per run (default: all).
"""

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memento import sync_ledger  # noqa: E402
from memento.config import get_vault, slugify  # noqa: E402
from memento.remote_client import get, is_remote, store  # noqa: E402


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


def _dry_run_note(note: dict) -> str:
    """Return the dry-run disposition for a note without writing remotely."""
    remote_path = _remote_note_path(note)
    remote = get(remote_path)
    if not remote:
        return "create"

    remote_note = parse_note_text(remote.get("content", ""), Path(remote_path).stem)
    if remote_note and sync_ledger.content_hash(
        _sync_payload(remote_note)
    ) == sync_ledger.content_hash(_sync_payload(note)):
        return "skip"
    return "conflict"


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


def catch_up(vault, dry_run=False, batch=0):
    """Walk local notes and push anything missing from the remote.

    Uses two layers to determine what needs pushing:
    1. Remote inventory (filename match) — catches notes pushed by other means
    2. Sync ledger (source + content hash) — catches notes whose remote
       filename differs from the local one (slugification, dedupe suffixes)

    Hash mismatches are checked against the ledger before flagging as
    conflicts. write_note() on the remote reconstructs metadata (source,
    date), so raw file hashes always differ from the local original. When
    the ledger confirms the semantic content was already pushed (matching
    _sync_payload content hash), the mismatch is expected and skipped.
    Unrecognized mismatches remain CONFLICTS — store() is append-only and
    pushing would create duplicates.

    Inventory fetch failures abort the run. Treating a failed list_notes()
    as an empty remote would bulk-push the entire vault as duplicates.
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

    ledger_by_source, ledger_remote_paths = _build_ledger_index(vault)

    local_files = sorted(notes_dir.glob("*.md"))
    to_push = []
    conflicts = []
    ledger_skipped = 0

    for f in local_files:
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue

        local_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        remote = remote_by_name.get(f.name)

        source = str(f.relative_to(vault))

        if remote is not None:
            if remote.get("hash") == local_hash:
                continue  # identical by filename match

            # Hash mismatch — check if the ledger explains it.
            # write_note() on the remote reconstructs metadata (source, date),
            # so raw hashes always differ. The ledger's content_hash uses
            # _sync_payload() which excludes metadata, so a match means the
            # semantic content is identical and the mismatch is expected.
            ledger_entry = ledger_by_source.get(source)
            if ledger_entry and ledger_entry.get("status") == "ok":
                note = parse_note(f)
                if note:
                    chash = sync_ledger.content_hash(_sync_payload(note))
                    if chash == ledger_entry.get("content_hash"):
                        ledger_skipped += 1
                        continue

            conflicts.append(f)
            continue

        # No filename match — check the ledger for a prior successful push
        ledger_entry = ledger_by_source.get(source)
        if ledger_entry and ledger_entry.get("status") == "ok":
            # Ledger says we pushed this before. Verify content hasn't changed.
            note = parse_note(f)
            if note:
                chash = sync_ledger.content_hash(_sync_payload(note))
                if chash == ledger_entry.get("content_hash"):
                    ledger_skipped += 1
                    continue
                # Content changed since last push — check if remote has the
                # note under its slugified name (it may be a conflict).
                remote_name = Path(ledger_entry.get("remote_path", "")).name
                if remote_name and remote_name in remote_by_name:
                    conflicts.append(f)
                    continue
            else:
                ledger_skipped += 1
                continue

        to_push.append(f)

    for f in conflicts:
        print(f"  Conflict (hash mismatch, skipped): {f.name}")

    if batch > 0:
        to_push = to_push[:batch]

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
        chash = sync_ledger.content_hash(_sync_payload(note))

        result = store(**note)

        if isinstance(result, dict) and "error" in result:
            print(f"  Error: {note['title']} -> {result['error']}", file=sys.stderr)
            sync_ledger.record(
                vault, "note", source,
                status="error", content_hash=chash, error=result["error"],
            )
            errors += 1
        else:
            remote_path = result.get("path", "?")
            sync_ledger.record(
                vault, "note", source,
                status="ok", content_hash=chash, remote_path=remote_path,
            )
            print(f"  Synced: {note['title']} -> {remote_path}")
            pushed += 1

    action = "Would push" if dry_run else "Pushed"
    print(
        f"  Catch-up: {action} {pushed}, conflicts {len(conflicts)}, "
        f"skipped {skipped}, ledger-matched {ledger_skipped}, errors {errors} "
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

    batch = 0
    if "--batch" in args:
        idx = args.index("--batch")
        if idx + 1 < len(args):
            batch = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        else:
            args = args[:idx]

    if catch_up_mode:
        try:
            vault = get_vault()
        except Exception:
            print("Could not determine vault path.", file=sys.stderr)
            sys.exit(1)
        catch_up(vault, dry_run=dry_run, batch=batch)
        return

    if not args:
        print("Usage: memento-remote-sync.py [--dry-run] <note-path> [...]", file=sys.stderr)
        print("       memento-remote-sync.py --catch-up [--dry-run] [--batch N]", file=sys.stderr)
        sys.exit(1)

    try:
        vault = get_vault()
    except Exception:
        vault = None

    for path in args:
        if not os.path.exists(path):
            print(f"  Skip (not found): {path}", file=sys.stderr)
            continue

        note = parse_note(path)
        if not note:
            print(f"  Skip (empty/unparseable): {path}", file=sys.stderr)
            continue

        if dry_run:
            disposition = _dry_run_note(note)
            if disposition == "skip":
                print(f"  Would skip (remote exists, same content): {note['title']}")
            elif disposition == "conflict":
                print(f"  Would conflict (remote exists, different content): {note['title']}")
            else:
                print(f"  Would create: {note['title']}")
            continue

        # Stable source key (relative to vault when possible, so moving the
        # vault root doesn't break idempotency).
        source = path
        if vault:
            try:
                source = str(Path(path).resolve().relative_to(vault.resolve()))
            except ValueError:
                pass

        chash = sync_ledger.content_hash(_sync_payload(note))

        # Skip if this exact payload was already acknowledged by the remote.
        if vault and sync_ledger.last_success_hash(vault, "note", source) == chash:
            print(f"  Skip (already synced): {note['title']}")
            continue

        result = store(**note)

        if vault:
            if isinstance(result, dict) and "error" in result:
                spool_path = sync_ledger.spool_payload(
                    vault, "note", source, _sync_payload(note)
                )
                sync_ledger.record(
                    vault,
                    "note",
                    source,
                    status="error",
                    content_hash=chash,
                    error=result["error"],
                    spool_path=str(spool_path),
                )
            else:
                sync_ledger.record(
                    vault,
                    "note",
                    source,
                    status="ok",
                    content_hash=chash,
                    remote_path=result.get("path"),
                )

        remote_path = result.get("path", result.get("error", "unknown"))
        print(f"  Synced: {note['title']} -> {remote_path}")


if __name__ == "__main__":
    main()
