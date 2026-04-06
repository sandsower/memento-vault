#!/usr/bin/env python3
"""
Stale reference detector for memento vault notes.

Scans notes for file path references and checks whether those files
still exist in the referenced project directories. Annotates notes
with stale references.

Usage:
  stale-refs.py [--fix] [--verbose]

Without --fix: prints a report of stale references.
With --fix: adds a blockquote annotation to notes with stale refs.
"""

import glob
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memento_utils import get_vault


# Patterns that look like repo-relative file paths
FILE_REF_PATTERN = re.compile(
    r'(?:frontend|backend|infrastructure|e2e|knowledge|tools)/'
    r'[\w/\-\.]+\.\w+'
)


def extract_project_dir(note_text):
    """Extract the project directory from frontmatter."""
    m = re.search(r'^project:\s*(.+)', note_text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


def scan_note(note_path):
    """Scan a note for file path references and check if they exist.

    Returns list of (path, exists) tuples.
    """
    text = Path(note_path).read_text(encoding="utf-8")
    project_dir = extract_project_dir(text)

    refs = FILE_REF_PATTERN.findall(text)
    if not refs or not project_dir:
        return []

    results = []
    for ref in set(refs):
        full_path = os.path.join(project_dir, ref)
        results.append((ref, os.path.exists(full_path)))

    return results


def annotate_note(note_path, missing_paths):
    """Add a stale reference annotation to the note body."""
    text = Path(note_path).read_text(encoding="utf-8")
    today = datetime.now().strftime("%Y-%m-%d")

    # Don't double-annotate
    if "**Stale reference:**" in text:
        return False

    annotation = "\n".join(
        f"> **Stale reference:** `{p}` no longer exists as of {today}."
        for p in missing_paths
    )

    # Insert before ## Related section if it exists, otherwise append
    if "## Related" in text:
        text = text.replace("## Related", f"{annotation}\n\n## Related")
    else:
        text = text.rstrip("\n") + f"\n\n{annotation}\n"

    Path(note_path).write_text(text, encoding="utf-8")
    return True


def main():
    fix_mode = "--fix" in sys.argv
    verbose = "--verbose" in sys.argv

    vault = get_vault()
    notes_dir = vault / "notes"

    stale_notes = []
    total_checked = 0
    total_refs = 0

    for note_path in sorted(notes_dir.glob("*.md")):
        results = scan_note(note_path)
        if not results:
            continue

        total_checked += 1
        total_refs += len(results)

        missing = [path for path, exists in results if not exists]
        if missing:
            stale_notes.append((note_path, missing))

    if verbose or not fix_mode:
        print(f"Scanned {total_checked} notes with file references ({total_refs} refs total)")
        print(f"Found {len(stale_notes)} notes with stale references:\n")

        for note_path, missing in stale_notes:
            print(f"  {note_path.stem}")
            for p in missing:
                print(f"    missing: {p}")
            print()

    if fix_mode and stale_notes:
        patched = 0
        for note_path, missing in stale_notes:
            if annotate_note(note_path, missing):
                patched += 1
                if verbose:
                    print(f"  annotated: {note_path.stem}")

        print(f"Annotated {patched} notes with stale reference warnings")

    return 1 if stale_notes else 0


if __name__ == "__main__":
    sys.exit(main())
