"""Tests for note evolution (supersedes detection)."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

from memento_utils import note_is_superseded


@pytest.fixture
def vault_with_supersedes(tmp_path):
    """Create a vault with superseded notes."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)

    # Old note
    (vault / "notes" / "redis-cache-v1.md").write_text(
        "---\n"
        "title: Redis cache needs TTL\n"
        "type: discovery\n"
        "certainty: 2\n"
        "date: 2026-03-01T10:00\n"
        "---\n\n"
        "Redis keys need explicit TTL.\n"
    )

    # New note that supersedes the old one
    (vault / "notes" / "redis-cache-v2.md").write_text(
        "---\n"
        "title: Redis cache requires explicit TTL per shard\n"
        "type: discovery\n"
        "certainty: 4\n"
        'supersedes: "[[redis-cache-v1]]"\n'
        "date: 2026-03-15T10:00\n"
        "---\n\n"
        "Supersedes [[redis-cache-v1]] -- added shard-specific detail.\n"
    )

    # Unrelated note
    (vault / "notes" / "zustand-reset.md").write_text(
        "---\n"
        "title: Zustand mock resets between tests\n"
        "type: bugfix\n"
        "certainty: 3\n"
        "date: 2026-03-10T10:00\n"
        "---\n\n"
        "Reset Zustand store in beforeEach.\n"
    )

    return vault


class TestNoteIsSuperseded:
    def test_superseded_note_found(self, vault_with_supersedes):
        with patch("memento_utils.get_vault", return_value=vault_with_supersedes):
            result = note_is_superseded("redis-cache-v1")
            assert result == "redis-cache-v2"

    def test_non_superseded_note(self, vault_with_supersedes):
        with patch("memento_utils.get_vault", return_value=vault_with_supersedes):
            result = note_is_superseded("zustand-reset")
            assert result is None

    def test_superseding_note_not_flagged(self, vault_with_supersedes):
        """The newer note itself is not superseded."""
        with patch("memento_utils.get_vault", return_value=vault_with_supersedes):
            result = note_is_superseded("redis-cache-v2")
            assert result is None

    def test_nonexistent_note(self, vault_with_supersedes):
        with patch("memento_utils.get_vault", return_value=vault_with_supersedes):
            result = note_is_superseded("does-not-exist")
            assert result is None

    def test_empty_vault(self, tmp_path):
        vault = tmp_path / "empty-vault"
        (vault / "notes").mkdir(parents=True)
        with patch("memento_utils.get_vault", return_value=vault):
            result = note_is_superseded("anything")
            assert result is None

    def test_no_notes_dir(self, tmp_path):
        vault = tmp_path / "no-notes"
        vault.mkdir()
        with patch("memento_utils.get_vault", return_value=vault):
            result = note_is_superseded("anything")
            assert result is None

    def test_supersedes_without_quotes(self, tmp_path):
        """Handle supersedes field without quotes around the wikilink."""
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)

        (vault / "notes" / "old-note.md").write_text(
            "---\ntitle: Old\ntype: discovery\n---\n\nOld content.\n"
        )
        (vault / "notes" / "new-note.md").write_text(
            "---\ntitle: New\ntype: discovery\nsupersedes: [[old-note]]\n---\n\nNew content.\n"
        )

        with patch("memento_utils.get_vault", return_value=vault):
            result = note_is_superseded("old-note")
            assert result == "new-note"
