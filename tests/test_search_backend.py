"""Tests for the search backend abstraction layer."""

import pytest
from memento.search_backend import (
    GrepBackend,
    QMDBackend,
    SearchBackend,
    _clean_snippet,
    get_backend,
    reset_backend,
    set_backend,
)


class MockBackend(SearchBackend):
    """Test backend that returns canned results."""

    def __init__(self, available=True, results=None):
        self._available = available
        self._results = results or []
        self.search_calls = []
        self.get_calls = []
        self.reindex_calls = []

    def is_available(self):
        return self._available

    def search(self, query, collection, limit=5, semantic=False, timeout=10, min_score=0.0):
        self.search_calls.append(
            {"query": query, "collection": collection, "limit": limit, "semantic": semantic}
        )
        return [r for r in self._results if r.get("score", 1.0) >= min_score][:limit]

    def get(self, path, collection=None, timeout=5):
        self.get_calls.append({"path": path, "collection": collection})
        for r in self._results:
            if r.get("path") == path:
                return r
        return None

    def reindex(self, collection, embed=True):
        self.reindex_calls.append({"collection": collection, "embed": embed})
        return True


@pytest.fixture(autouse=True)
def reset():
    """Reset the global backend after each test."""
    yield
    reset_backend()


class TestCleanSnippet:
    def test_strips_chunk_markers(self):
        raw = "@@ -3,4 @@ (2 before, 12 after) Some content here"
        assert "Some content here" in _clean_snippet(raw)

    def test_strips_frontmatter(self):
        raw = "---\ntitle: Hello\n---\nActual content"
        assert "Actual content" in _clean_snippet(raw)
        assert "title:" not in _clean_snippet(raw)

    def test_truncates_to_200(self):
        raw = "x" * 300
        assert len(_clean_snippet(raw)) == 200

    def test_empty_input(self):
        assert _clean_snippet("") == ""
        assert _clean_snippet(None) == ""


class TestBackendSingleton:
    def test_default_is_qmd_or_grep(self):
        backend = get_backend()
        # QMD when available, GrepBackend as fallback
        assert isinstance(backend, (QMDBackend, GrepBackend))

    def test_set_and_get(self):
        mock = MockBackend()
        set_backend(mock)
        assert get_backend() is mock

    def test_reset(self):
        mock = MockBackend()
        set_backend(mock)
        reset_backend()
        assert isinstance(get_backend(), (QMDBackend, GrepBackend))


class TestMockBackend:
    def test_search_delegates_to_backend(self):
        results = [
            {"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": "Foo content"},
            {"path": "notes/bar.md", "title": "Bar", "score": 0.5, "snippet": "Bar content"},
        ]
        mock = MockBackend(results=results)
        set_backend(mock)

        # Use the search.py wrapper
        from memento.search import qmd_search

        found = qmd_search("test query", collection="memento", limit=5)
        assert len(found) == 2
        assert mock.search_calls[0]["query"] == "test query"

    def test_search_respects_min_score(self):
        results = [
            {"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": ""},
            {"path": "notes/bar.md", "title": "Bar", "score": 0.3, "snippet": ""},
        ]
        mock = MockBackend(results=results)
        set_backend(mock)

        from memento.search import qmd_search

        found = qmd_search("test", collection="memento", min_score=0.5)
        assert len(found) == 1
        assert found[0]["title"] == "Foo"

    def test_unavailable_backend_returns_empty(self):
        mock = MockBackend(available=False)
        set_backend(mock)

        from memento.search import qmd_search, has_qmd

        assert not has_qmd()
        assert qmd_search("test", collection="memento") == []

    def test_get_delegates_to_backend(self):
        results = [
            {"path": "notes/foo.md", "title": "Foo", "content": "Body", "score": 0.0},
        ]
        mock = MockBackend(results=results)
        set_backend(mock)

        from memento.search import qmd_get

        note = qmd_get("notes/foo.md")
        assert note is not None
        assert note["title"] == "Foo"
        assert mock.get_calls[0]["path"] == "notes/foo.md"

    def test_get_returns_none_for_missing(self):
        mock = MockBackend(results=[])
        set_backend(mock)

        from memento.search import qmd_get

        assert qmd_get("notes/missing.md") is None


class TestEmbeddedBackendDetection:
    """EmbeddedSearchBackend detection in get_backend()."""

    def test_embedded_used_when_qmd_unavailable(self, tmp_path):
        """When QMD is not available but vault exists, EmbeddedSearchBackend is used."""
        from memento.embedded_search import EmbeddedSearchBackend

        vault = tmp_path / "vault"
        for d in ("notes", "fleeting", "projects"):
            (vault / d).mkdir(parents=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.search_backend.QMDBackend.is_available", lambda self: False)
            mp.setattr("memento.config.get_vault", lambda: vault)
            mp.setattr("memento.config.get_config", lambda: {"vault_path": str(vault), "search_backend": "auto", "search_db_path": ".search/search.db"})
            reset_backend()
            backend = get_backend()
            assert isinstance(backend, EmbeddedSearchBackend)

    def test_qmd_preferred_over_embedded(self, tmp_path):
        """When QMD is available, it should be used over EmbeddedSearchBackend."""
        vault = tmp_path / "vault"
        for d in ("notes", "fleeting", "projects"):
            (vault / d).mkdir(parents=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.search_backend.QMDBackend.is_available", lambda self: True)
            mp.setattr("memento.config.get_vault", lambda: vault)
            mp.setattr("memento.config.get_config", lambda: {"vault_path": str(vault), "search_backend": "auto", "search_db_path": ".search/search.db"})
            reset_backend()
            backend = get_backend()
            assert isinstance(backend, QMDBackend)

    def test_config_override_forces_embedded(self, tmp_path):
        """search_backend: embedded in config forces EmbeddedSearchBackend."""
        from memento.embedded_search import EmbeddedSearchBackend

        vault = tmp_path / "vault"
        for d in ("notes", "fleeting", "projects"):
            (vault / d).mkdir(parents=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.search_backend.QMDBackend.is_available", lambda self: True)
            mp.setattr("memento.config.get_vault", lambda: vault)
            mp.setattr("memento.config.get_config", lambda: {"vault_path": str(vault), "search_backend": "embedded", "search_db_path": ".search/search.db"})
            reset_backend()
            backend = get_backend()
            assert isinstance(backend, EmbeddedSearchBackend)

    def test_config_override_forces_grep(self, tmp_path):
        """search_backend: grep in config forces GrepBackend."""
        vault = tmp_path / "vault"
        for d in ("notes", "fleeting", "projects"):
            (vault / d).mkdir(parents=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.config.get_vault", lambda: vault)
            mp.setattr("memento.config.get_config", lambda: {"vault_path": str(vault), "search_backend": "grep", "search_db_path": ".search/search.db"})
            reset_backend()
            backend = get_backend()
            assert isinstance(backend, GrepBackend)

    def test_grep_fallback_when_no_vault(self, tmp_path):
        """When vault doesn't exist, fall back to GrepBackend."""
        vault = tmp_path / "nonexistent"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.search_backend.QMDBackend.is_available", lambda self: False)
            mp.setattr("memento.config.get_vault", lambda: vault)
            mp.setattr("memento.config.get_config", lambda: {"vault_path": str(vault), "search_backend": "auto", "search_db_path": ".search/search.db"})
            reset_backend()
            backend = get_backend()
            assert isinstance(backend, GrepBackend)


class TestGrepBackendPathTraversal:
    """Ensure GrepBackend.get rejects paths that escape the vault."""

    def test_traversal_rejected(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "notes").mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret")

        backend = GrepBackend()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.config.get_vault", lambda: vault)
            result = backend.get("../secret.txt")
        assert result is None

    def test_valid_path_allowed(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "notes" / "test.md"
        note.parent.mkdir()
        note.write_text("---\ntitle: Test\n---\nContent here")

        backend = GrepBackend()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.config.get_vault", lambda: vault)
            result = backend.get("notes/test.md")
        assert result is not None
        assert result["title"] == "Test"
