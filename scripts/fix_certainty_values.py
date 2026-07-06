#!/usr/bin/env python3
"""One-shot fixer for out-of-range certainty values in existing notes (MEM-150).

`_coerce_certainty()` in memento/store.py now clamps future writes into the
1-5 range with a logged warning, but a handful of existing notes already have
out-of-range certainty (e.g. `certainty: 95` or `certainty: 97`, presumably
meant to be `5`) baked into their frontmatter from before that guard existed.
This script finds those notes and, optionally, fixes them.

Dry-run by default: prints each offending note's path, current value, and
proposed clamped value, and writes nothing.

Usage:
    python3 scripts/fix_certainty_values.py [--vault PATH]           # dry-run
    python3 scripts/fix_certainty_values.py [--vault PATH] --apply   # writes

Only the `certainty:` line is touched -- every other frontmatter line and the
full note body round-trip byte-for-byte, via the same atomic tmp+rename write
(`_write_text_atomic`) the rest of memento/store.py uses.

Do not run --apply against a shared/real vault without a backup.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_CERTAINTY_LINE_RE = re.compile(r"^certainty:[ \t]*.*$", re.MULTILINE)


def _clamp(value: int) -> int:
    return max(1, min(5, value))


def find_bad_certainty_notes(vault_path):
    """Yield ``(note_path, current_value, clamped_value)`` for every note
    under ``vault_path/notes`` whose frontmatter ``certainty`` falls outside
    1-5. Unreadable files and notes without a certainty line are skipped
    silently -- this is a reporting/fix tool, not a validator.
    """
    from memento.store import _frontmatter_int, split_frontmatter

    notes_dir = Path(vault_path) / "notes"
    if not notes_dir.is_dir():
        return

    for note_path in sorted(notes_dir.glob("*.md")):
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        frontmatter, _ = split_frontmatter(text)
        if not frontmatter:
            continue
        value = _frontmatter_int(frontmatter, "certainty")
        if value is None or 1 <= value <= 5:
            continue
        yield note_path, value, _clamp(value)


def fix_note(note_path: Path, clamped_value: int) -> bool:
    """Rewrite one note's `certainty:` line in place. Returns True if changed.

    Every other line -- frontmatter or body -- is preserved verbatim; only
    the `certainty:` line's value is replaced via a targeted regex
    substitution rather than rebuilding the frontmatter block, so ordering,
    quoting, and unrelated keys are untouched.
    """
    from memento.store import _write_text_atomic

    text = note_path.read_text(encoding="utf-8", errors="replace")
    new_text, count = _CERTAINTY_LINE_RE.subn(f"certainty: {clamped_value}", text, count=1)
    if count == 0 or new_text == text:
        return False
    _write_text_atomic(note_path, new_text)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", default=None, help="Vault path (defaults to the configured vault_path)")
    parser.add_argument("--apply", action="store_true", help="Write fixes (default: dry-run, prints only)")
    args = parser.parse_args(argv)

    if args.vault:
        vault_path = args.vault
    else:
        from memento.config import get_config

        vault_path = get_config()["vault_path"]

    offenders = list(find_bad_certainty_notes(vault_path))

    if not offenders:
        print(f"No out-of-range certainty values found under {vault_path}")
        return 0

    print(f"Found {len(offenders)} note(s) with out-of-range certainty under {vault_path}:")
    for note_path, current, clamped in offenders:
        print(f"  {note_path}: certainty {current} -> {clamped}")

    if not args.apply:
        print("\nDry run only -- no files written. Re-run with --apply to fix.")
        return 0

    fixed = 0
    for note_path, _current, clamped in offenders:
        if fix_note(note_path, clamped):
            fixed += 1
    print(f"\nFixed {fixed}/{len(offenders)} note(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
