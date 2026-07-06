"""Tests for the search backend abstraction layer."""

import pytest

from memento.search import is_literal_like_query, resolve_concrete_mode
from memento.search_backend import (
    GrepBackend,
    QMDBackend,
    SearchBackend,
    _clean_snippet,
    _literal_score,
    get_backend,
    normalize_grep_term_coverage,
    normalize_qmd_score,
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

    def search(self, query, collection, limit=5, semantic=False, timeout=10, min_score=0.0, concrete=False):
        self.search_calls.append(
            {"query": query, "collection": collection, "limit": limit, "semantic": semantic, "concrete": concrete}
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
def isolate_default_backend(monkeypatch, tmp_path):
    """Keep default backend selection isolated from developer environment state."""
    isolated_vault = tmp_path / "isolated-vault"
    monkeypatch.setattr(
        "memento.config.get_config",
        lambda: {"vault_path": str(isolated_vault), "search_backend": "auto", "search_db_path": ".search/search.db"},
    )
    monkeypatch.setattr("memento.config.get_vault", lambda: isolated_vault)
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


class TestNormalizeQmdScore:
    """MEM-127: QMD's own score is already ~bounded, clamp defensively only."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.97, 0.97),  # observed BM25 hit band (0.9-0.98)
            (0.6, 0.6),  # observed semantic vsearch band (0.5-0.7)
            (0.0, 0.0),
            (1.0, 1.0),
        ],
    )
    def test_within_range_passes_through(self, raw, expected):
        assert normalize_qmd_score(raw) == pytest.approx(expected)

    def test_clamps_above_one(self):
        assert normalize_qmd_score(1.5) == 1.0

    def test_clamps_negative(self):
        assert normalize_qmd_score(-0.3) == 0.0

    def test_non_numeric_is_zero(self):
        assert normalize_qmd_score("not-a-number") == 0.0
        assert normalize_qmd_score(None) == 0.0


class TestNormalizeGrepTermCoverage:
    """MEM-127: grep's matched/total fraction is already bounded by construction."""

    @pytest.mark.parametrize(("raw", "expected"), [(1.0, 1.0), (0.5, 0.5), (0.0, 0.0)])
    def test_within_range_passes_through(self, raw, expected):
        assert normalize_grep_term_coverage(raw) == pytest.approx(expected)

    def test_clamps_above_one(self):
        assert normalize_grep_term_coverage(1.2) == 1.0

    def test_clamps_negative(self):
        assert normalize_grep_term_coverage(-0.1) == 0.0


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


class TestConcreteDetection:
    @pytest.mark.parametrize(
        "query",
        [
            '"exact phrase"',
            'find "exact phrase"',
            "notes about 'exact phrase'",
            "MEMENTO_VAULT_PATH",
            "550e8400-e29b-41d4-a716-446655440000",
            "src/server/authMiddleware.ts",
            "some_process.name",
            "memento_search",
        ],
    )
    def test_literal_like_queries_detected(self, query):
        assert is_literal_like_query(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "what did we decide about cache invalidation",
            "Redis TTL policy",
            "AWS deployment notes",
            "what's the team's Redis TTL policy",
        ],
    )
    def test_conceptual_query_not_literal_like(self, query):
        assert is_literal_like_query(query) is False

    @pytest.mark.parametrize(
        ("value", "query", "expected_concrete", "expected_auto"),
        [
            (True, "cache", True, False),
            (False, "MEMENTO_VAULT_PATH", False, False),
            ("auto", "MEMENTO_VAULT_PATH", True, True),
            (None, "normal cache policy", False, False),
            ("flase", "MEMENTO_VAULT_PATH", False, False),
        ],
    )
    def test_resolve_concrete_mode(self, value, query, expected_concrete, expected_auto):
        concrete, auto_selected = resolve_concrete_mode(value, query)
        assert concrete is expected_concrete
        assert auto_selected is expected_auto

    def test_embedded_single_quotes_extract_literal_phrase_not_apostrophes(self):
        exact = _literal_score("notes about 'blue comet protocol'", "notes/phrase.md", "Phrase", "blue comet protocol")
        prose = _literal_score("what's the team's Redis TTL policy", "notes/policy.md", "Policy", "Redis TTL policy")

        assert exact > 0
        assert prose == 0

    def test_literal_score_prefers_exact_content_identifier_over_substring(self):
        exact = _literal_score("MEMENTO_VAULT_PATH", "notes/exact.md", "Exact", "Set MEMENTO_VAULT_PATH here")
        substring = _literal_score(
            "MEMENTO_VAULT_PATH",
            "notes/substring.md",
            "Substring",
            "Set MY_MEMENTO_VAULT_PATH_SUFFIX here",
        )

        assert exact > substring


class TestBackendIndexHook:
    def test_qmd_index_note_conservatively_reindexes_collection(self, monkeypatch):
        backend = QMDBackend()
        calls = []
        monkeypatch.setattr(
            backend, "reindex", lambda collection, embed=True: calls.append((collection, embed)) or True
        )

        assert backend.index_note("notes/new.md", collection="memento") is True
        assert calls == [("memento", False)]


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

        found = qmd_search("test query", collection="memento", limit=5, concrete=True)
        assert len(found) == 2
        assert mock.search_calls[0]["query"] == "test query"
        assert mock.search_calls[0]["concrete"] is True

    def test_concrete_search_with_extras_skips_duplicate_fanout(self, monkeypatch):
        results = [
            {"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": "MEMENTO_VAULT_PATH"},
        ]
        mock = MockBackend(results=results)
        set_backend(mock)
        monkeypatch.setattr(
            "memento.search.get_config",
            lambda: {"qmd_collection": "memento", "extra_qmd_collections": ["archive"]},
        )

        from memento.search import qmd_search_with_extras

        found = qmd_search_with_extras("MEMENTO_VAULT_PATH", concrete=True)

        assert found == results
        assert len(mock.search_calls) == 1
        assert mock.search_calls[0]["collection"] == "memento"

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

    def test_default_index_note_uses_reindex_without_embeddings(self):
        mock = MockBackend()

        assert mock.index_note("notes/new.md", collection="memento") is True
        assert mock.reindex_calls == [{"collection": "memento", "embed": False}]

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
            mp.setattr(
                "memento.config.get_config",
                lambda: {"vault_path": str(vault), "search_backend": "auto", "search_db_path": ".search/search.db"},
            )
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
            mp.setattr(
                "memento.config.get_config",
                lambda: {"vault_path": str(vault), "search_backend": "auto", "search_db_path": ".search/search.db"},
            )
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
            mp.setattr(
                "memento.config.get_config",
                lambda: {"vault_path": str(vault), "search_backend": "embedded", "search_db_path": ".search/search.db"},
            )
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
            mp.setattr(
                "memento.config.get_config",
                lambda: {"vault_path": str(vault), "search_backend": "grep", "search_db_path": ".search/search.db"},
            )
            reset_backend()
            backend = get_backend()
            assert isinstance(backend, GrepBackend)

    def test_grep_fallback_when_no_vault(self, tmp_path):
        """When vault doesn't exist, fall back to GrepBackend."""
        vault = tmp_path / "nonexistent"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.search_backend.QMDBackend.is_available", lambda self: False)
            mp.setattr("memento.config.get_vault", lambda: vault)
            mp.setattr(
                "memento.config.get_config",
                lambda: {"vault_path": str(vault), "search_backend": "auto", "search_db_path": ".search/search.db"},
            )
            reset_backend()
            backend = get_backend()
            assert isinstance(backend, GrepBackend)


class TestGrepBackendPathTraversal:
    """Ensure GrepBackend.get rejects paths that escape the vault."""

    def test_index_note_is_noop_success(self):
        backend = GrepBackend()
        assert backend.index_note("notes/new.md", collection="memento") is True

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


class TestBackendResultTagging:
    """MEM-127: every backend.search() result carries a `backend` field."""

    def _make_vault(self, tmp_path):
        vault = tmp_path / "vault"
        notes = vault / "notes"
        notes.mkdir(parents=True)
        (notes / "redis.md").write_text("---\ntitle: Redis TTL\n---\n\nRedis cache TTL discovery.\n")
        return vault

    def test_grep_search_tags_backend(self, tmp_path):
        vault = self._make_vault(tmp_path)
        backend = GrepBackend()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.config.get_vault", lambda: vault)
            results = backend.search("Redis TTL", "memento")
        assert results
        assert all(r["backend"] == "grep" for r in results)

    def test_grep_concrete_search_tags_backend(self, tmp_path):
        vault = self._make_vault(tmp_path)
        backend = GrepBackend()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.config.get_vault", lambda: vault)
            results = backend.search("Redis TTL", "memento", concrete=True)
        assert results
        assert all(r["backend"] == "grep" for r in results)

    def test_qmd_concrete_search_tags_backend(self, tmp_path, monkeypatch):
        vault = self._make_vault(tmp_path)
        monkeypatch.setattr("memento.config.get_vault", lambda: vault)
        backend = QMDBackend()
        results = backend.search("Redis TTL", "memento", concrete=True)
        assert results
        assert all(r["backend"] == "qmd" for r in results)


class TestArchiveExclusion:
    """Archived notes are retired from active retrieval."""

    def test_qmd_search_filters_archive_paths(self):
        from memento.search import qmd_search

        backend = MockBackend(
            results=[
                {"path": "notes/good-note.md", "title": "Good", "score": 0.9, "snippet": ""},
                {
                    "path": "archive/pi-candidate-captures-2026-05-24/pi-session-candidate-capture-3.md",
                    "title": "Dump",
                    "score": 0.95,
                    "snippet": "",
                },
                {
                    "path": "archive/beislid-main-plans-2026-05-03/old-plan.md",
                    "title": "Old plan",
                    "score": 0.8,
                    "snippet": "",
                },
            ]
        )
        set_backend(backend)
        try:
            results = qmd_search("candidate capture")
        finally:
            reset_backend()

        assert [r["path"] for r in results] == ["notes/good-note.md"]

    def test_qmd_search_keeps_notes_with_archive_in_name(self):
        from memento.search import qmd_search

        backend = MockBackend(
            results=[{"path": "notes/archive-export-design.md", "title": "Archive export", "score": 0.9, "snippet": ""}]
        )
        set_backend(backend)
        try:
            results = qmd_search("archive export")
        finally:
            reset_backend()

        assert [r["path"] for r in results] == ["notes/archive-export-design.md"]
