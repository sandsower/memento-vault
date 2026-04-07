#!/usr/bin/env python3
"""Wrapper that runs a command and touches a sentinel file on completion.

Usage: agent-wrapper.py <sentinel_path> <cmd> [args...]

Creates sentinel_path when the command finishes (regardless of exit code),
so downstream stages can wait for completion instead of guessing with timers.
"""
import subprocess
import sys
from pathlib import Path

if len(sys.argv) < 3:
    sys.exit(1)

sentinel = Path(sys.argv[1])
cmd = sys.argv[2:]

sentinel.parent.mkdir(parents=True, exist_ok=True)
sentinel.unlink(missing_ok=True)

try:
    subprocess.run(cmd, capture_output=True)
finally:
    sentinel.touch()
