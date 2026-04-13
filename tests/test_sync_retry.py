"""Regression tests for hooks/memento-sync-retry.py — the retry replay path.

Covers the two Codex adversarial findings:
  1. Note retry must not crash on a broken dynamic import when there are
     pending note entries.
  2. Capture retry must replay substantive failures with their original
     metadata (cwd, branch, files_edited, fleeting_only) — not downgrade
     them to fleeting.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memento import sync_ledger  # noqa: E402


def _load_retry_module():
    """Load hooks/memento-sync-retry.py as an importable module.

    The hook is dash-cased, so we use spec_from_file_location to load it
    for tests.
    """
    spec = importlib.util.spec_from_file_location(
        "memento_sync_retry_test",
        str(REPO_ROOT / "hooks" / "memento-sync-retry.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def retry_mod():
    return _load_retry_module()


# ---------------------------------------------------------------------------
# Note retry — must not crash on missing module import
# ---------------------------------------------------------------------------


class TestNoteRetryNoBrokenImport:
    def test_note_retry_parses_inline_without_importing_sibling(
        self, tmp_path, retry_mod
    ):
        """The retry path must parse notes using its own inline parser.
        Any attempt to import a `memento_remote_sync` module would raise
        ModuleNotFoundError, which would abort the whole run."""
        vault = tmp_path / "vault"
        vault.mkdir()
        note_dir = vault / "notes"
        note_dir.mkdir()
        note_file = note_dir / "test-note.md"
        note_file.write_text(
            "---\n"
            "title: Test Note\n"
            "type: discovery\n"
            "tags: [a, b]\n"
            "certainty: 3\n"
            "---\n\n"
            "Body of the note.\n"
        )

        # Record the prior failure the retry should pick up.
        sync_ledger.record(
            vault, "note", "notes/test-note.md",
            status="error", error="connection refused", content_hash="h1",
        )

        # Patch the remote store so we don't hit the network.
        captured_args = {}

        def fake_store(**kwargs):
            captured_args.update(kwargs)
            return {"path": "remote/notes/test-note.md"}

        with patch.object(retry_mod, "store", fake_store):
            outcome = retry_mod._retry_note(
                vault, sync_ledger.pending_retries(vault)[0]
            )

        assert outcome["status"] == "ok"
        assert captured_args["title"] == "Test Note"
        assert captured_args["note_type"] == "discovery"
        assert captured_args["tags"] == ["a", "b"]
        assert captured_args["certainty"] == 3

    def test_main_does_not_crash_when_note_retry_pending(
        self, tmp_path, retry_mod, monkeypatch
    ):
        """End-to-end: with a pending note entry in the ledger, main()
        must complete without ModuleNotFoundError."""
        vault = tmp_path / "vault"
        vault.mkdir()
        notes = vault / "notes"
        notes.mkdir()
        (notes / "pending.md").write_text(
            "---\ntitle: X\ntype: discovery\n---\n\nbody\n"
        )
        sync_ledger.record(
            vault, "note", "notes/pending.md",
            status="error", error="boom", content_hash="h",
        )

        monkeypatch.setattr(retry_mod, "is_remote", lambda: True)
        monkeypatch.setattr(retry_mod, "get_vault", lambda: vault)
        monkeypatch.setattr(retry_mod, "store", lambda **_: {"path": "remote/x.md"})

        monkeypatch.setattr(sys, "argv", ["memento-sync-retry.py"])
        rc = retry_mod.main()

        assert rc == 0
        # Ledger now shows success.
        assert sync_ledger.pending_retries(vault) == []


# ---------------------------------------------------------------------------
# Capture retry — must preserve original metadata, not downgrade to fleeting
# ---------------------------------------------------------------------------


class TestCaptureRetryPreservesMetadata:
    def _seed_envelope_failure(self, vault, *, fleeting_only, **meta):
        """Simulate a failed substantive capture by writing the envelope +
        ledger entry the way hooks/memento-triage.py would."""
        envelope = {
            "version": 1,
            "session_summary": "body text",
            "cwd": meta.get("cwd", "/repo/a"),
            "branch": meta.get("branch", "main"),
            "files_edited": meta.get("files_edited", ["a.py", "b.py"]),
            "session_id": "abc-123",
            "agent": "claude",
            "fleeting_only": fleeting_only,
        }
        spool_path = sync_ledger.spool_payload(
            vault, "capture", "session:abc-123", json.dumps(envelope)
        )
        sync_ledger.record(
            vault, "capture", "session:abc-123",
            status="error", error="timeout", content_hash="h",
            spool_path=str(spool_path),
        )
        return envelope

    def test_substantive_capture_replayed_with_full_metadata(
        self, tmp_path, retry_mod
    ):
        """The core Codex finding: a failed substantive capture must NOT
        be replayed as fleeting, and metadata must be preserved."""
        vault = tmp_path / "vault"
        vault.mkdir()

        original = self._seed_envelope_failure(
            vault,
            fleeting_only=False,  # substantive!
            cwd="/repo/feature",
            branch="feature-x",
            files_edited=["src/main.py", "tests/test_main.py"],
        )

        captured = {}

        def fake_capture(**kwargs):
            captured.update(kwargs)
            return {"path": "notes/substantive.md"}

        with patch.object(retry_mod, "capture", fake_capture):
            entry = sync_ledger.pending_retries(vault)[0]
            outcome = retry_mod._retry_capture(vault, entry)

        assert outcome["status"] == "ok"
        # Every piece of metadata the original capture used must be replayed.
        assert captured["session_summary"] == original["session_summary"]
        assert captured["cwd"] == "/repo/feature"
        assert captured["branch"] == "feature-x"
        assert captured["files_edited"] == ["src/main.py", "tests/test_main.py"]
        assert captured["session_id"] == "abc-123"
        assert captured["agent"] == "claude"
        # Crucially: fleeting_only must NOT have been hardcoded to True.
        assert captured["fleeting_only"] is False

    def test_fleeting_capture_stays_fleeting(self, tmp_path, retry_mod):
        """Sanity check: if the original call was fleeting, retry keeps
        it fleeting (not that it changes, but no regression in the
        opposite direction either)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        self._seed_envelope_failure(vault, fleeting_only=True)

        captured = {}

        def fake_capture(**kwargs):
            captured.update(kwargs)
            return {"path": "fleeting/x.md"}

        with patch.object(retry_mod, "capture", fake_capture):
            entry = sync_ledger.pending_retries(vault)[0]
            retry_mod._retry_capture(vault, entry)

        assert captured["fleeting_only"] is True

    def test_envelope_loader_distinguishes_envelope_from_legacy(
        self, tmp_path, retry_mod
    ):
        """The loader must identify the envelope format and NOT misread a
        legacy markdown spool as an envelope."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Write an envelope via spool_payload.
        env_path = sync_ledger.spool_payload(
            vault, "capture", "session:env",
            json.dumps({"session_summary": "x", "cwd": "/a", "fleeting_only": False}),
        )
        # Write a legacy markdown spool by hand.
        legacy_dir = vault / "spool" / "remote-failures"
        legacy_dir.mkdir(parents=True)
        legacy_file = legacy_dir / "legacy.md"
        legacy_file.write_text("---\nsession_id: legacy\n---\n\nlegacy body\n")

        env, kind = retry_mod._load_capture_envelope(str(env_path))
        assert kind == "envelope"
        assert env["session_summary"] == "x"

        leg, kind = retry_mod._load_capture_envelope(str(legacy_file))
        assert kind == "legacy"
        assert leg["session_summary"] == "legacy body"
        assert leg.get("_legacy") is True

        missing, kind = retry_mod._load_capture_envelope(
            str(tmp_path / "nonexistent")
        )
        assert kind == "missing"
        assert missing is None

    def test_legacy_spool_warns_and_falls_back_to_fleeting(
        self, tmp_path, retry_mod, capsys
    ):
        """Legacy spools pre-date the envelope — replay as fleeting but
        warn the user that classification is degraded. This is better
        than silently misclassifying (the Codex concern), because the
        user is told exactly what's lost."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Legacy format: markdown with frontmatter, no JSON envelope.
        legacy_dir = vault / "spool" / "remote-failures"
        legacy_dir.mkdir(parents=True)
        legacy_file = legacy_dir / "legacy.md"
        legacy_file.write_text("---\nsession_id: legacy\n---\n\nsome summary\n")

        sync_ledger.record(
            vault, "capture", "session:legacy",
            status="error", error="old failure",
            spool_path=str(legacy_file),
        )

        captured = {}

        def fake_capture(**kwargs):
            captured.update(kwargs)
            return {"path": "fleeting/legacy.md"}

        with patch.object(retry_mod, "capture", fake_capture):
            entry = sync_ledger.pending_retries(vault)[0]
            retry_mod._retry_capture(vault, entry)

        stderr = capsys.readouterr().err
        assert "legacy spool" in stderr
        assert captured["session_summary"] == "some summary"
        assert captured["fleeting_only"] is True


# ---------------------------------------------------------------------------
# Mixed-run scenario — a note failure must not prevent later captures from
# retrying (one of Codex's listed impacts).
# ---------------------------------------------------------------------------


class TestMixedRun:
    def test_note_failure_does_not_abort_capture_retry(
        self, tmp_path, retry_mod, monkeypatch
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        notes = vault / "notes"
        notes.mkdir()
        (notes / "bad.md").write_text("not a valid note without frontmatter")
        sync_ledger.record(
            vault, "note", "notes/bad.md",
            status="error", error="boom",
        )

        env = {
            "version": 1,
            "session_summary": "s",
            "cwd": "",
            "branch": "",
            "files_edited": [],
            "session_id": "after",
            "agent": "claude",
            "fleeting_only": True,
        }
        spool = sync_ledger.spool_payload(
            vault, "capture", "session:after", json.dumps(env)
        )
        sync_ledger.record(
            vault, "capture", "session:after",
            status="error", error="boom", spool_path=str(spool),
        )

        capture_calls = []

        def fake_capture(**kwargs):
            capture_calls.append(kwargs)
            return {"path": "remote/after.md"}

        monkeypatch.setattr(retry_mod, "is_remote", lambda: True)
        monkeypatch.setattr(retry_mod, "get_vault", lambda: vault)
        monkeypatch.setattr(retry_mod, "capture", fake_capture)
        monkeypatch.setattr(sys, "argv", ["memento-sync-retry.py"])

        retry_mod.main()

        # The capture retry ran even though the note retry (for an unparseable
        # file) produced a fresh error.
        assert len(capture_calls) == 1
        assert capture_calls[0]["session_id"] == "after"
