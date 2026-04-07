#!/usr/bin/env python3
"""Wait for a sentinel file, normalize vault tags, then commit.

Usage: wait-and-commit.py <sentinel> <max_wait_s> <hooks_dir> <commit_script> <message>

Polls for the sentinel file every 2 seconds. Once found (or after max_wait_s),
normalizes all note tags and runs the commit script.
"""

import subprocess
import sys
import time
from pathlib import Path

sentinel = Path(sys.argv[1])
max_wait = int(sys.argv[2])
hooks_dir = sys.argv[3]
commit_script = sys.argv[4]
message = sys.argv[5]

# Poll for sentinel
elapsed = 0
while elapsed < max_wait:
    if sentinel.exists():
        break
    time.sleep(2)
    elapsed += 2

# Normalize tags on all notes
sys.path.insert(0, str(Path(hooks_dir).parent))
sys.path.insert(0, hooks_dir)
from memento.config import get_vault  # noqa: E402
from memento.utils import normalize_note_tags  # noqa: E402

vault = get_vault()
notes_dir = vault / "notes"
if notes_dir.exists():
    for note_path in notes_dir.glob("*.md"):
        normalize_note_tags(note_path)

# Clean up sentinel
sentinel.unlink(missing_ok=True)

# Commit
subprocess.run([commit_script, message], capture_output=True)
