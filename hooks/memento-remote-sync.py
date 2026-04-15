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


def catch_up(vault, dry_run=False, batch=0):
    """Walk local notes and push anything missing from the remote."""
    import hashlib
    from memento.remote_client import list_notes

    notes_dir = vault / "notes"
    if not notes_dir.exists():
        print("  No local notes directory.", file=sys.stderr)
        return

    print("  Fetching remote inventory...")
    remote_notes = list_notes(include_hash=True)
    remote_by_name = {Path(r["path"]).name: r for r in remote_notes}

    local_files = sorted(notes_dir.glob("*.md"))
    to_push = []

    for f in local_files:
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue

        local_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        remote = remote_by_name.get(f.name)

        if remote and remote.get("hash") == local_hash:
            continue  # identical

        to_push.append((f, "changed" if remote else "missing"))

    if batch > 0:
        to_push = to_push[:batch]

    if not to_push:
        print(f"  Catch-up: {len(local_files)} local, {len(remote_notes)} remote, 0 to push.")
        return

    pushed = 0
    skipped = 0
    errors = 0

    for f, reason in to_push:
        note = parse_note(f)
        if not note:
            skipped += 1
            continue

        if dry_run:
            print(f"  Would push ({reason}): {note['title']}")
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
            print(f"  Synced ({reason}): {note['title']} -> {remote_path}")
            pushed += 1

    action = "Would push" if dry_run else "Pushed"
    print(f"  Catch-up: {action} {pushed}, skipped {skipped}, errors {errors} (of {len(local_files)} local, {len(remote_notes)} remote)")


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
