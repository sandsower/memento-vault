"""Tests for smart-store duplicate and supersession suggestions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from memento.smart_store import _split_note_text, suggest_store_action, write_smart_store_note
from memento.store import acquire_vault_write_lock, release_vault_write_lock, write_note


@pytest.fixture(autouse=True)
def isolated_vault_write_lock(monkeypatch, tmp_path):
    """Point the vault write lock at a per-test file so tests never touch the real runtime dir."""
    lock_file = tmp_path / "locks" / "vault-write.lock"
    monkeypatch.setattr("memento.store.VAULT_WRITE_LOCK_PATH", str(lock_file))
    return lock_file


def _seed_note(vault: Path, *, title: str, body: str, tags: list[str] | None = None) -> str:
    result = write_note(
        vault,
        title=title,
        body=body,
        note_type="discovery",
        tags=tags or ["sync"],
        certainty=4,
        source="mcp",
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )
    return str(result.relative_to(vault))


def test_exact_duplicate_is_reported_as_already_covered(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    existing_path = _seed_note(tmp_vault, title="Duplicate safe note", body="Same body.")

    result = suggest_store_action(
        title="Duplicate safe note",
        body="Same body.",
        tags=["sync"],
        certainty=4,
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )

    assert result["decision"] == "already_covered"
    assert result["path"] == existing_path
    assert result["created"] is False


def test_near_duplicate_is_reported_as_candidate_update(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    existing_path = _seed_note(tmp_vault, title="Redis cache guidance", body="Use the shared cache for shard reads.")

    result = suggest_store_action(
        title="Redis cache guidance",
        body="Use the shared cache for shard reads. Add the TTL defaults and rollout note.",
        tags=["sync"],
        certainty=4,
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )

    assert result["decision"] == "candidate_update"
    assert result["path"] == existing_path
    assert result["best_candidate"]["path"] == existing_path


def test_supersession_cue_is_reported(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    existing_path = _seed_note(tmp_vault, title="Redis cache guidance", body="Use the shared cache for all shards.")

    result = suggest_store_action(
        title="Redis cache guidance",
        body="Replace the shared cache guidance: no longer use the shared cache; switch to shard-specific TTL.",
        tags=["sync"],
        certainty=4,
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )

    assert result["decision"] == "supersedes_suggested"
    assert result["path"] == existing_path
    assert result["best_candidate"]["path"] == existing_path


def test_write_smart_store_note_creates_when_no_close_match(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    result = write_smart_store_note(
        title="Fresh note",
        body="An entirely new idea.",
        tags=["new"],
    )

    assert result["decision"] == "created"
    assert result["created"] is True
    assert result["path"]
    assert (tmp_vault / result["path"]).exists()


class TestSplitNoteText:
    """Only a LEADING '---' block is frontmatter; body dashes must never fabricate one (audit M6)."""

    def test_body_dashes_without_frontmatter_do_not_fabricate_frontmatter(self):
        text = "Intro paragraph.\n\n---\n\nMiddle section.\n\n---\n\nEnd of note."

        frontmatter, body = _split_note_text(text)

        assert frontmatter == ""
        assert "Intro paragraph." in body
        assert "Middle section." in body
        assert "End of note." in body

    def test_leading_frontmatter_is_parsed_and_body_dashes_are_kept(self):
        text = "---\ntitle: Real note\ntype: discovery\n---\n\nFirst half.\n\n---\n\nSecond half."

        frontmatter, body = _split_note_text(text)

        assert "title: Real note" in frontmatter
        assert "type: discovery" in frontmatter
        assert "First half." in body
        assert "---" in body
        assert "Second half." in body

    def test_text_without_any_dashes_is_all_body(self):
        frontmatter, body = _split_note_text("Just a plain body.")

        assert frontmatter == ""
        assert body == "Just a plain body."

    def test_unclosed_leading_dashes_are_treated_as_body(self):
        text = "---\ntitle: broken note without closing delimiter"

        frontmatter, body = _split_note_text(text)

        assert frontmatter == ""
        assert "broken note without closing delimiter" in body

    def test_exact_duplicate_detection_survives_body_dashes(self, tmp_vault, monkeypatch):
        """Dedup must compare the full body even when it contains '---' sections."""
        monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
        monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
        monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

        body = "Setup section.\n\n---\n\nRollout section with the real detail."
        existing_path = _seed_note(tmp_vault, title="Dashed body note", body=body)

        result = suggest_store_action(
            title="Dashed body note",
            body=body,
            tags=["sync"],
            certainty=4,
            project="/home/vic/Projects/memento-vault",
            branch="main",
        )

        assert result["decision"] == "already_covered"
        assert result["path"] == existing_path


class TestWriteSmartStoreLock:
    """The dedup check plus write must be atomic under the vault write lock (audit M6)."""

    def _patch_vault(self, tmp_vault, monkeypatch):
        monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
        monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
        monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    def test_write_refused_while_another_process_holds_the_lock(self, tmp_vault, monkeypatch, isolated_vault_write_lock):
        self._patch_vault(tmp_vault, monkeypatch)
        # Keep the test fast: the real acquire, just with a short timeout.
        monkeypatch.setattr(
            "memento.smart_store.acquire_vault_write_lock",
            lambda **kwargs: acquire_vault_write_lock(timeout=0.2, poll_interval=0.05),
            raising=False,
        )

        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            isolated_vault_write_lock.parent.mkdir(parents=True, exist_ok=True)
            isolated_vault_write_lock.write_text(str(other.pid))

            result = write_smart_store_note(
                title="Locked out note",
                body="Must not be written while another process holds the lock.",
                tags=["lock"],
            )
        finally:
            other.kill()
            other.wait()

        assert result.get("error")
        assert list((tmp_vault / "notes").glob("*.md")) == []

    def test_lock_is_acquired_and_released_around_the_write(self, tmp_vault, monkeypatch, isolated_vault_write_lock):
        self._patch_vault(tmp_vault, monkeypatch)

        result = write_smart_store_note(
            title="Standalone locked write",
            body="A brand new idea that needs the lock only briefly.",
            tags=["lock"],
        )

        assert result["created"] is True
        assert not isolated_vault_write_lock.exists()

    def test_reentrant_when_caller_already_holds_the_lock(self, tmp_vault, monkeypatch, isolated_vault_write_lock):
        """Callers like the MCP server already hold the lock; the write path must not deadlock or steal it."""
        self._patch_vault(tmp_vault, monkeypatch)

        assert acquire_vault_write_lock() is True
        try:
            result = write_smart_store_note(
                title="Reentrant note",
                body="Written while the caller holds the vault write lock.",
                tags=["lock"],
            )

            assert result["created"] is True
            assert (tmp_vault / result["path"]).exists()
            # The callee must leave the caller's lock in place.
            assert isolated_vault_write_lock.exists()
            assert isolated_vault_write_lock.read_text().strip() == str(os.getpid())
        finally:
            release_vault_write_lock()
