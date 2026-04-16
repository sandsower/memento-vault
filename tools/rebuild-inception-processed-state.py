#!/usr/bin/env python3
"""Rebuild the Inception processed_notes set from on-disk pattern notes.

Fixes a bug where notes were added to processed_notes even when Inception
failed to consolidate them into a pattern. This script discards the current
set and rebuilds it from ground truth: the union of synthesized_from fields
across every `source: inception` note in the vault.

Run once after pulling the fix. Writes a timestamped backup before modifying
the state file. Holds inception.lock so it cannot overlap with a live run.

Usage:
    python tools/rebuild-inception-processed-state.py [--dry-run] [--force]

    --dry-run   Print the summary and skip the write (still creates a backup
                copy of the current state for reference).
    --force     Proceed even when malformed pattern notes are found.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "hooks"))

from memento.config import get_config  # noqa: E402
from memento.store import (  # noqa: E402
    INCEPTION_STATE_PATH,
    acquire_inception_lock,
    load_inception_state,
    release_inception_lock,
    save_inception_state,
)

# parse_note lives in the inception hook. Load it via the test-style
# importlib shim to avoid depending on the module's dashed filename.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "memento_inception",
    str(_repo_root / "hooks" / "memento-inception.py"),
)
if _spec is None or _spec.loader is None:
    print("error: cannot locate hooks/memento-inception.py", file=sys.stderr)
    sys.exit(2)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
parse_note = _mod.parse_note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Print summary, skip write")
    ap.add_argument("--force", action="store_true", help="Proceed despite malformed patterns")
    args = ap.parse_args()

    config = get_config()
    vault_path = Path(config["vault_path"])
    notes_dir = vault_path / "notes"

    state_path = Path(INCEPTION_STATE_PATH)
    if not state_path.exists():
        print(f"no state file at {state_path} — nothing to rebuild")
        return 0

    if not acquire_inception_lock():
        print("inception.lock held by another process — aborting", file=sys.stderr)
        return 1

    try:
        state = load_inception_state()

        consolidated: set[str] = set()
        malformed: list[str] = []

        for md_file in sorted(notes_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            record = parse_note(md_file)
            if record is None:
                continue
            if record.source != "inception":
                continue
            if not record.synthesized_from:
                malformed.append(md_file.name)
                continue
            for stem in record.synthesized_from:
                if isinstance(stem, str) and stem:
                    consolidated.add(stem)

        if malformed and not args.force:
            print(
                f"error: {len(malformed)} pattern notes have missing/empty "
                "synthesized_from:",
                file=sys.stderr,
            )
            for name in malformed:
                print(f"  - {name}", file=sys.stderr)
            print("rerun with --force to proceed anyway", file=sys.stderr)
            return 2

        before_count = len(state.get("processed_notes", []))
        after_count = len(consolidated)

        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_path = state_path.with_name(state_path.name + f".bak-{ts}")
        shutil.copy2(state_path, backup_path)

        print(f"backup: {backup_path}")
        print(
            f"processed_notes: before={before_count} "
            f"after={after_count} removed={before_count - after_count}"
        )
        if malformed:
            print(f"warning: {len(malformed)} pattern notes had empty synthesized_from (--force)")

        if args.dry_run:
            print("dry-run — state file unchanged")
            return 0

        state["processed_notes"] = sorted(consolidated)
        save_inception_state(state)
        print("state rewritten")
        return 0

    finally:
        release_inception_lock()


if __name__ == "__main__":
    sys.exit(main())
