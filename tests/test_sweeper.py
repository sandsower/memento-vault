"""Tests for the orphan transcript sweeper."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_main_folds_access_log_into_frontmatter_before_triage(tmp_path, monkeypatch):
    """MEM-148: the sweeper is the periodic trigger for the resurfacing-signal fold."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)

    fold_calls = []
    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", lambda vault_path: fold_calls.append(vault_path))

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0
    assert fold_calls == [str(vault)]
    assert not _mod.LOCK_FILE.exists()  # released after the run


def test_main_still_triages_when_fold_raises(tmp_path, monkeypatch):
    """A fold failure must never block orphan triage -- it's caught and swallowed."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)

    def _boom(vault_path):
        raise RuntimeError("runtime dir unavailable")

    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", _boom)

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0


def test_main_runs_archive_sweep_after_fold(tmp_path, monkeypatch):
    """MEM-152: the sweeper is also the periodic trigger for the archive sweep."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)

    sweep_calls = []
    monkeypatch.setattr(_mod, "sweep_archive_candidates", lambda vault_path: sweep_calls.append(vault_path))

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0
    assert sweep_calls == [str(vault)]


def test_main_still_triages_when_archive_sweep_raises(tmp_path, monkeypatch):
    """A sweep failure must never block orphan triage -- isolated like the MEM-148 fold."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)

    def _boom(vault_path):
        raise RuntimeError("archive sweep exploded")

    monkeypatch.setattr(_mod, "sweep_archive_candidates", _boom)

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0


def test_main_runs_fleeting_lifecycle_sweep_after_archive_sweep(tmp_path, monkeypatch):
    """MEM-153: the sweeper is also the periodic trigger for the fleeting lifecycle sweep."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)
    monkeypatch.setattr(_mod, "sweep_archive_candidates", lambda vault_path: None)

    fleeting_calls = []
    monkeypatch.setattr(_mod, "fleeting_lifecycle_sweep", lambda vault_path: fleeting_calls.append(vault_path))

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0
    assert fleeting_calls == [str(vault)]


def test_main_still_triages_when_fleeting_lifecycle_sweep_raises(tmp_path, monkeypatch):
    """A fleeting-lifecycle failure must never block orphan triage -- isolated like fold/archive sweep."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)
    monkeypatch.setattr(_mod, "sweep_archive_candidates", lambda vault_path: None)

    def _boom(vault_path):
        raise RuntimeError("fleeting lifecycle exploded")

    monkeypatch.setattr(_mod, "fleeting_lifecycle_sweep", _boom)

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0


def test_main_runs_supersession_backlinks_after_fleeting_lifecycle_sweep(tmp_path, monkeypatch):
    """MEM-163: the sweeper is also the periodic trigger for the supersession backlink pass."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)
    monkeypatch.setattr(_mod, "sweep_archive_candidates", lambda vault_path: None)
    monkeypatch.setattr(_mod, "fleeting_lifecycle_sweep", lambda vault_path: None)

    backlink_calls = []
    monkeypatch.setattr(_mod, "apply_supersession_backlinks", lambda vault_path: backlink_calls.append(vault_path))

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0
    assert backlink_calls == [str(vault)]


def test_main_still_triages_when_supersession_backlinks_raises(tmp_path, monkeypatch):
    """A backlink-pass failure must never block orphan triage -- isolated like the other sweeps."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(_mod, "VAULT", vault)
    monkeypatch.setattr(_mod, "FLEETING", vault / "fleeting")
    monkeypatch.setattr(_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
    monkeypatch.setattr(_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
    monkeypatch.setattr(_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
    monkeypatch.setattr(_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
    monkeypatch.setattr(_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)
    monkeypatch.setattr(_mod, "sweep_archive_candidates", lambda vault_path: None)
    monkeypatch.setattr(_mod, "fleeting_lifecycle_sweep", lambda vault_path: None)

    def _boom(vault_path):
        raise RuntimeError("backlink pass exploded")

    monkeypatch.setattr(_mod, "apply_supersession_backlinks", _boom)

    with pytest.raises(SystemExit) as exc:
        _mod.main()

    assert exc.value.code == 0
