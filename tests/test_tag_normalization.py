"""Tests for tag normalization in memento_utils."""

import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from memento_utils import normalize_tags, normalize_note_tags


_TEST_CONFIG = {
    "tag_aliases": {
        "k8s": "kubernetes",
        "js": "javascript",
        "ts": "typescript",
        "py": "python",
        "db": "database",
        "postgres": "postgresql",
    },
}


class TestNormalizeTags:
    def _normalize(self, tags):
        with patch("memento_utils.get_config", return_value=_TEST_CONFIG):
            return normalize_tags(tags)

    def test_basic_alias(self):
        assert self._normalize(["k8s", "redis"]) == ["kubernetes", "redis"]

    def test_lowercases(self):
        assert self._normalize(["Redis", "K8S"]) == ["redis", "kubernetes"]

    def test_deduplicates(self):
        assert self._normalize(["k8s", "kubernetes"]) == ["kubernetes"]

    def test_preserves_order(self):
        assert self._normalize(["redis", "js", "api"]) == ["redis", "javascript", "api"]

    def test_strips_whitespace(self):
        assert self._normalize(["  redis  ", " k8s "]) == ["redis", "kubernetes"]

    def test_empty_tags_filtered(self):
        assert self._normalize(["", "redis", ""]) == ["redis"]

    def test_no_aliases_configured(self):
        with patch("memento_utils.get_config", return_value={"tag_aliases": {}}):
            assert normalize_tags(["k8s", "redis"]) == ["k8s", "redis"]


class TestNormalizeNoteTags:
    def _write_note(self, tmp_path, tags_str):
        note = tmp_path / "test-note.md"
        note.write_text(f"---\ntitle: Test\ntype: discovery\ntags: [{tags_str}]\ndate: 2026-01-01\n---\n\nBody text.")
        return note

    def test_normalizes_tags_in_file(self, tmp_path):
        note = self._write_note(tmp_path, "k8s, redis, js")
        with patch("memento_utils.get_config", return_value=_TEST_CONFIG):
            changed = normalize_note_tags(note)
        assert changed is True
        content = note.read_text()
        assert "kubernetes" in content
        assert "javascript" in content
        assert "k8s" not in content.split("---")[1]  # not in frontmatter

    def test_no_change_when_already_normalized(self, tmp_path):
        note = self._write_note(tmp_path, "kubernetes, redis")
        with patch("memento_utils.get_config", return_value=_TEST_CONFIG):
            changed = normalize_note_tags(note)
        assert changed is False

    def test_preserves_body(self, tmp_path):
        note = self._write_note(tmp_path, "k8s, redis")
        with patch("memento_utils.get_config", return_value=_TEST_CONFIG):
            normalize_note_tags(note)
        content = note.read_text()
        assert "Body text." in content

    def test_nonexistent_file(self, tmp_path):
        changed = normalize_note_tags(tmp_path / "nope.md")
        assert changed is False

    def test_title_containing_triple_dash(self, tmp_path):
        """Frontmatter with --- in a value must not corrupt the note."""
        note = tmp_path / "dash-title.md"
        note.write_text(
            "---\n"
            "title: foo --- bar\n"
            "tags: [k8s, redis]\n"
            "date: 2026-01-01\n"
            "---\n\n"
            "Body text."
        )
        with patch("memento_utils.get_config", return_value=_TEST_CONFIG):
            changed = normalize_note_tags(note)
        assert changed is True
        content = note.read_text()
        # Title must survive intact
        assert "foo --- bar" in content
        # Tags normalized
        assert "kubernetes" in content
        # Body preserved
        assert "Body text." in content
        # Frontmatter structure intact (exactly two --- fences)
        parts = content.split("---")
        assert len(parts) >= 3

    def test_body_containing_triple_dash(self, tmp_path):
        """--- in the body must not confuse the parser."""
        note = tmp_path / "body-dash.md"
        note.write_text(
            "---\n"
            "title: Normal title\n"
            "tags: [k8s]\n"
            "date: 2026-01-01\n"
            "---\n\n"
            "Some text\n---\nMore text."
        )
        with patch("memento_utils.get_config", return_value=_TEST_CONFIG):
            changed = normalize_note_tags(note)
        assert changed is True
        content = note.read_text()
        assert "kubernetes" in content
        # Body --- preserved
        assert "Some text\n---\nMore text." in content

    def test_no_frontmatter(self, tmp_path):
        note = tmp_path / "plain.md"
        note.write_text("Just a plain file.")
        changed = normalize_note_tags(note)
        assert changed is False
