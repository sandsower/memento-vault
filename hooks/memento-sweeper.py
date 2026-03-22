#!/usr/bin/env python3
"""
Memento sweeper — finds orphan transcripts that were never triaged.

Designed to run from a tmux session-closed hook or a systemd timer.
Scans recent JSONL transcripts, checks which ones already appear in
fleeting notes, and feeds orphans through memento-triage.py via subprocess.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"
TRIAGE_SCRIPT = Path(__file__).parent / "memento-triage.py"
_RUNTIME = os.environ.get("XDG_RUNTIME_DIR", os.path.join(str(Path.home()), ".cache", "memento-vault"))
os.makedirs(_RUNTIME, mode=0o700, exist_ok=True)
LOCK_FILE = Path(_RUNTIME) / "sweeper.lock"
MAX_AGE_HOURS = 24


def resolve_vault():
    """Resolve vault path from config, falling back to ~/memento."""
    config_files = [
        Path.home() / ".config" / "memento-vault" / "memento.yml",
        Path.home() / ".memento-vault.yml",
    ]
    for cfg in config_files:
        if cfg.exists():
            try:
                with open(cfg) as f:
                    for line in f:
                        if line.strip().startswith("vault_path:"):
                            path = line.split(":", 1)[1].strip().strip("\"'")
                            path = os.path.expanduser(path)
                            if os.path.isdir(path):
                                return Path(path)
            except Exception:
                pass
    return Path.home() / "memento"


VAULT = resolve_vault()
FLEETING = VAULT / "fleeting"


def acquire_lock():
    """Atomic file-based lock to prevent concurrent sweeps."""
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < 300:
                return False
        except OSError:
            pass
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass

    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def collect_known_session_ids():
    """Extract all session UUIDs already recorded in fleeting notes."""
    known = set()
    uuid_pattern = re.compile(r"`([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`")

    if not FLEETING.exists():
        return known

    for f in FLEETING.glob("*.md"):
        try:
            text = f.read_text()
            known.update(uuid_pattern.findall(text))
        except Exception:
            continue

    return known


def collect_active_session_ids():
    """Read ~/.claude/sessions/*.json and return IDs of sessions whose PID is still alive."""
    active = set()
    if not CLAUDE_SESSIONS.exists():
        return active

    for f in CLAUDE_SESSIONS.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            sid = data.get("sessionId")
            if pid and sid:
                try:
                    os.kill(pid, 0)
                    active.add(sid)
                except OSError:
                    pass
        except Exception:
            continue

    return active


def find_recent_transcripts():
    """Find JSONL transcript files modified within MAX_AGE_HOURS."""
    cutoff = time.time() - (MAX_AGE_HOURS * 3600)
    transcripts = {}

    if not CLAUDE_PROJECTS.exists():
        return transcripts

    for jsonl in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            if jsonl.stat().st_mtime >= cutoff:
                session_id = jsonl.stem
                transcripts[session_id] = str(jsonl)
        except Exception:
            continue

    return transcripts


def triage_orphan(session_id, transcript_path):
    """Feed an orphan transcript through memento-triage.py."""
    hook_input = json.dumps({
        "session_id": session_id,
        "transcript_path": transcript_path,
    })

    triage = str(TRIAGE_SCRIPT)
    if not TRIAGE_SCRIPT.exists():
        # Fall back to installed location
        triage = str(Path.home() / ".claude" / "hooks" / "memento-triage.py")

    try:
        subprocess.Popen(
            [sys.executable, triage],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ).communicate(input=hook_input.encode(), timeout=30)
    except (subprocess.TimeoutExpired, Exception):
        pass


def main():
    if not acquire_lock():
        sys.exit(0)

    try:
        known = collect_known_session_ids()
        active = collect_active_session_ids()
        recent = find_recent_transcripts()

        orphans = {sid: path for sid, path in recent.items()
                   if sid not in known and sid not in active}

        if not orphans:
            sys.exit(0)

        for session_id, transcript_path in orphans.items():
            triage_orphan(session_id, transcript_path)
            if len(orphans) > 1:
                time.sleep(2)

    finally:
        release_lock()


if __name__ == "__main__":
    main()
