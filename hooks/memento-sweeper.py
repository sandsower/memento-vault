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

_repo_root = str(Path(__file__).parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from memento.archive import fleeting_lifecycle_sweep, sweep_archive_candidates  # noqa: E402
from memento.store import fold_access_log_into_frontmatter, fold_stale_citations_into_frontmatter  # noqa: E402

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"
PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
PI_SUBAGENTS = Path.home() / ".pi" / "agent" / "subagents"
TRIAGE_SCRIPT = Path(__file__).parent / "memento-triage.py"
_RUNTIME = os.environ.get("XDG_RUNTIME_DIR", os.path.join(str(Path.home()), ".cache", "memento-vault"))
os.makedirs(_RUNTIME, mode=0o700, exist_ok=True)
LOCK_FILE = Path(_RUNTIME) / "sweeper.lock"
MAX_AGE_HOURS = 24
ORPHAN_GRACE_SECONDS = int(os.environ.get("MEMENTO_SWEEPER_ORPHAN_GRACE_SECONDS", "300"))


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
    """Extract session IDs already recorded in fleeting notes."""
    known = set()
    session_pattern = re.compile(r"`([^`\n]{1,240})`")

    if not FLEETING.exists():
        return known

    for f in FLEETING.glob("*.md"):
        try:
            text = f.read_text()
            known.update(session_pattern.findall(text))
        except Exception:
            continue

    return known


def pi_session_dirs():
    """Return Pi transcript roots, including env-configured session directories."""
    roots = [PI_SESSIONS, PI_SUBAGENTS]
    session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if session_dir:
        roots.append(Path(session_dir).expanduser())
    extra = os.environ.get("MEMENTO_PI_TRANSCRIPT_ROOTS", "")
    for raw in extra.split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    deduped = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def session_id_from_transcript(jsonl):
    """Best-effort session id extraction for Claude/Pi JSONL transcripts."""
    try:
        with open(jsonl, encoding="utf-8", errors="replace") as handle:
            for _idx, raw in zip(range(20), handle):
                if not raw.strip():
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for key in ("session_id", "sessionId"):
                    if data.get(key):
                        return str(data[key])
                session = data.get("session") if isinstance(data.get("session"), dict) else {}
                if session.get("id"):
                    return str(session["id"])
                if data.get("type") == "session" and data.get("id"):
                    return str(data["id"])
    except OSError:
        pass
    return Path(jsonl).stem


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
    """Find recent, idle Claude/Pi JSONL transcript files."""
    newest_allowed = time.time() - ORPHAN_GRACE_SECONDS
    cutoff = time.time() - (MAX_AGE_HOURS * 3600)
    transcripts = {}

    if CLAUDE_PROJECTS.exists():
        for jsonl in CLAUDE_PROJECTS.glob("*/*.jsonl"):
            try:
                stat = jsonl.stat()
                if cutoff <= stat.st_mtime <= newest_allowed:
                    session_id = session_id_from_transcript(jsonl)
                    transcripts[session_id] = {"path": str(jsonl), "agent": "claude"}
            except Exception:
                continue

    for root in pi_session_dirs():
        if not root.exists():
            continue
        for jsonl in root.rglob("*.jsonl"):
            try:
                stat = jsonl.stat()
                if cutoff <= stat.st_mtime <= newest_allowed:
                    session_id = session_id_from_transcript(jsonl)
                    transcripts[session_id] = {"path": str(jsonl), "agent": "pi"}
            except Exception:
                continue

    return transcripts


def triage_orphan(session_id, transcript):
    """Feed an orphan transcript through memento-triage.py."""
    transcript_path = transcript.get("path") if isinstance(transcript, dict) else transcript
    agent = transcript.get("agent") if isinstance(transcript, dict) else None
    hook_input = json.dumps(
        {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "agent": agent,
        }
    )

    triage = str(TRIAGE_SCRIPT)
    if not TRIAGE_SCRIPT.exists():
        # Fall back to installed location
        triage = str(Path.home() / ".claude" / "hooks" / "memento-triage.py")

    env = os.environ.copy()
    if agent:
        env["MEMENTO_AGENT"] = str(agent)
    try:
        subprocess.Popen(
            [sys.executable, triage],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        ).communicate(input=hook_input.encode(), timeout=30)
    except (subprocess.TimeoutExpired, Exception):
        pass


def main():
    if not acquire_lock():
        sys.exit(0)

    try:
        try:
            # Durable resurfacing signal (MEM-148): fold the derived
            # access-log write-ahead buffer into note frontmatter so a
            # runtime-dir cache wipe never resets access_log_half_life_days
            # history. This must never block orphan triage below.
            fold_access_log_into_frontmatter(str(VAULT))
        except Exception:
            pass

        try:
            # Auto-archive sweep (MEM-152): reversibly archive notes that are
            # durability_tier "cold", older than archive_sweep_age_days, and
            # certainty < 4 (memento.archive.sweep_archive_candidates). A
            # no-op until archive_sweep_enabled is flipped on. Isolated the
            # same way as the fold above -- a sweep failure must never block
            # orphan triage.
            sweep_archive_candidates(str(VAULT))
        except Exception:
            pass

        try:
            # Fleeting note lifecycle (MEM-153): promote fleeting/*.md notes
            # to notes/ (resurfaced or cited by a session-summary note) and
            # reversibly expire the rest once older than fleeting_expire_days
            # (memento.archive.fleeting_lifecycle_sweep). A no-op until
            # fleeting_lifecycle_enabled is flipped on. Isolated the same way
            # as the fold/archive sweep above -- a failure here must never
            # block orphan triage.
            fleeting_lifecycle_sweep(str(VAULT))
        except Exception:
            pass

        try:
            # Citation staleness fold (MEM-162): verify-at-use (recall and
            # tool-context injection) queues a note path whenever a cited
            # anchor no longer matches its file. This periodic fold is the
            # only writer that turns that queue into durable
            # `citation_stale: true` frontmatter (memento.store.
            # fold_stale_citations_into_frontmatter) -- always-on, cheap
            # no-op when the queue is empty. Isolated the same way as the
            # folds/sweeps above -- a failure here must never block orphan
            # triage.
            fold_stale_citations_into_frontmatter(str(VAULT))
        except Exception:
            pass

        known = collect_known_session_ids()
        active = collect_active_session_ids()
        recent = find_recent_transcripts()

        orphans = {sid: path for sid, path in recent.items() if sid not in known and sid not in active}

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
