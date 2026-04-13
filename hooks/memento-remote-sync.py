#!/usr/bin/env python3
"""Sync local vault notes to the remote vault via memento_store.

Usage: memento-remote-sync.py <note-path> [<note-path> ...]

Reads each markdown note, parses frontmatter, and calls remote_client.store().
No-op if MEMENTO_VAULT_URL is not set.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memento import sync_ledger  # noqa: E402
from memento.config import get_vault  # noqa: E402
from memento.remote_client import is_remote, store  # noqa: E402


def parse_note(path):
    """Parse a markdown note into title, body, type, and tags."""
    raw = Path(path).read_text()
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None

    fm, body = parts[1], parts[2].strip()
    if not body:
        return None

    title_m = re.search(r"^title:\s*(.+)$", fm, re.MULTILINE)
    title = title_m.group(1).strip().strip("\"'") if title_m else Path(path).stem

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


def main():
    if not is_remote():
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Usage: memento-remote-sync.py <note-path> [...]", file=sys.stderr)
        sys.exit(1)

    try:
        vault = get_vault()
    except Exception:
        vault = None

    for path in sys.argv[1:]:
        if not os.path.exists(path):
            print(f"  Skip (not found): {path}", file=sys.stderr)
            continue

        note = parse_note(path)
        if not note:
            print(f"  Skip (empty/unparseable): {path}", file=sys.stderr)
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
