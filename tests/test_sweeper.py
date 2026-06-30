"""Tests for the orphan transcript sweeper."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "memento_sweeper",
    str(Path(__file__).parent.parent / "hooks" / "memento-sweeper.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["memento_sweeper"] = _mod
_spec.loader.exec_module(_mod)


def test_find_recent_transcripts_includes_idle_pi_session_dir(tmp_path, monkeypatch):
    claude_projects = tmp_path / "claude" / "projects"
    pi_dir = tmp_path / "pi-sessions"
    claude_projects.mkdir(parents=True)
    pi_dir.mkdir()

    claude_session_dir = claude_projects / "repo"
    claude_session_dir.mkdir()
    claude = claude_session_dir / "claude-1.jsonl"
    claude.write_text(json.dumps({"sessionId": "claude-1", "type": "user", "message": {"content": "hi"}}) + "\n")

    pi = pi_dir / "pi-file.jsonl"
    pi.write_text(
        json.dumps({"type": "session", "id": "pi-session-1", "cwd": "/repo"})
        + "\n"
        + json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}})
        + "\n"
    )

    fresh = pi_dir / "fresh.jsonl"
    fresh.write_text(json.dumps({"type": "session", "id": "fresh-pi"}) + "\n")

    old = time.time() - 600
    os.utime(claude, (old, old))
    os.utime(pi, (old, old))

    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", claude_projects)
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "missing-default-pi")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "missing-default-subagents")
    monkeypatch.setattr(_mod, "ORPHAN_GRACE_SECONDS", 300)
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(pi_dir))

    transcripts = _mod.find_recent_transcripts()

    assert transcripts["claude-1"] == {"path": str(claude), "agent": "claude"}
    assert transcripts["pi-session-1"] == {"path": str(pi), "agent": "pi"}
    assert "fresh-pi" not in transcripts


def test_triage_orphan_sets_pi_agent_env(monkeypatch, tmp_path):
    transcript = tmp_path / "pi-session.jsonl"
    transcript.write_text(json.dumps({"type": "session", "id": "pi-session-1"}) + "\n")
    popen_calls = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.input = None
            self.timeout = None
            popen_calls.append((args, kwargs, self))

        def communicate(self, input=None, timeout=None):
            self.input = input
            self.timeout = timeout
            return b"", b""

    with patch.object(_mod.subprocess, "Popen", FakePopen):
        _mod.triage_orphan("pi-session-1", {"path": str(transcript), "agent": "pi"})

    kwargs = popen_calls[0][1]
    proc = popen_calls[0][2]
    assert kwargs["env"]["MEMENTO_AGENT"] == "pi"
    assert kwargs["stdin"] == _mod.subprocess.PIPE
    payload = json.loads(proc.input.decode())
    assert payload["agent"] == "pi"
    assert payload["session_id"] == "pi-session-1"
    assert payload["transcript_path"] == str(transcript)
