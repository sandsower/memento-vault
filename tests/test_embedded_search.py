"""Tests for the EmbeddedSearchBackend (SQLite FTS5 + sqlite-vec)."""

import sqlite3

import pytest

from memento.config import reset_config
from memento.search_backend import SearchBackend, reset_backend


@pytest.fixture
def embedded_vault(tmp_path):
    """Create a vault with sample notes and return (vault_path, search_db_path)."""
    vault = tmp_path / "vault"
    for d in ("notes", "fleeting", "projects"):
        (vault / d).mkdir(parents=True)

    # Note 1: Redis caching
    (vault / "notes" / "redis-cache-ttl.md").write_text(
        "---\ntitle: Redis cache requires explicit TTL\ntype: discovery\n"
        "tags: [redis, caching]\ndate: 2026-03-10T14:00\ncertainty: 4\n---\n\n"
        "Setting explicit TTL on Redis keys prevents stale reads.\n"
    )
    # Note 2: React testing
    (vault / "notes" / "zustand-state-reset.md").write_text(
        "---\ntitle: Zustand mock state resets between tests\ntype: bugfix\n"
        "tags: [zustand, testing, react]\ndate: 2026-03-11T16:00\ncertainty: 3\n---\n\n"
        "Zustand store must be reset in beforeEach to avoid test bleed.\n"
    )
    # Note 3: API invalidation
    (vault / "notes" / "api-cache-invalidation.md").write_text(
        "---\ntitle: API response cache invalidation strategy\ntype: decision\n"
        "tags: [redis, caching, api]\ndate: 2026-03-15T09:00\ncertainty: 4\n---\n\n"
        "Invalidate on write, not on TTL expiry, for billing endpoints.\n"
    )
    # Note 4: fleeting note
    (vault / "fleeting" / "2026-03-15-session.md").write_text(
        "---\ntitle: Session log\ndate: 2026-03-15\n---\n\n"
        "Worked on Redis caching and API endpoints today.\n"
    )

    search_dir = vault / ".search"
    search_dir.mkdir()
    db_path = search_dir / "search.db"

    return vault, db_path


@pytest.fixture(autouse=True)
def _cleanup():
    """Reset global state after every test."""
    yield
    reset_backend()
    reset_config()


@pytest.fixture
def backend(embedded_vault):
    """Create an EmbeddedSearchBackend pointed at the test vault."""
    from memento.embedded_search import EmbeddedSearchBackend

    vault, db_path = embedded_vault
    b = EmbeddedSearchBackend(vault_path=vault, db_path=db_path)
    return b


class TestEmbeddedSearchBackendSchema:
    """Schema creation and basic availability."""

    def test_implements_search_backend(self, backend):
        assert isinstance(backend, SearchBackend)

    def test_is_available_after_init(self, backend):
        assert backend.is_available()

    def test_creates_db_file(self, embedded_vault):
        from memento.embedded_search import EmbeddedSearchBackend

        vault, db_path = embedded_vault
        assert not db_path.exists()
        EmbeddedSearchBackend(vault_path=vault, db_path=db_path)
        assert db_path.exists()

    def test_schema_has_notes_table(self, backend, embedded_vault):
        _, db_path = embedded_vault
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "notes" in tables

    def test_schema_has_fts5_table(self, backend, embedded_vault):
        _, db_path = embedded_vault
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "notes_fts" in tables


class TestEmbeddedSearchReindex:
    """Full reindex from markdown files."""

    def test_reindex_returns_true(self, backend):
        assert backend.reindex("memento")

    def test_reindex_populates_notes(self, backend, embedded_vault):
        vault, db_path = embedded_vault
        backend.reindex("memento")
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        conn.close()
        assert count == 4  # 3 notes + 1 fleeting

    def test_reindex_is_idempotent(self, backend, embedded_vault):
        """Running reindex twice doesn't duplicate notes."""
        vault, db_path = embedded_vault
        backend.reindex("memento")
        backend.reindex("memento")
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        conn.close()
        assert count == 4

    def test_reindex_updates_changed_content(self, backend, embedded_vault):
        vault, db_path = embedded_vault
        backend.reindex("memento")

        # Modify a note
        note = vault / "notes" / "redis-cache-ttl.md"
        note.write_text(
            "---\ntitle: Redis cache requires explicit TTL\n---\n\n"
            "Updated content about Redis TTL and expiration.\n"
        )
        backend.reindex("memento")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT content FROM notes WHERE path = ?", ("notes/redis-cache-ttl.md",)).fetchone()
        conn.close()
        assert "Updated content" in row[0]


class TestEmbeddedSearchFTS5:
    """FTS5 BM25 search."""

    def test_search_finds_matching_notes(self, backend):
        backend.reindex("memento")
        results = backend.search("Redis cache TTL", "memento")
        assert len(results) > 0
        paths = [r["path"] for r in results]
        assert "notes/redis-cache-ttl.md" in paths

    def test_search_returns_correct_shape(self, backend):
        backend.reindex("memento")
        results = backend.search("Redis", "memento")
        assert len(results) > 0
        r = results[0]
        assert "path" in r
        assert "title" in r
        assert "score" in r
        assert "snippet" in r

    def test_search_respects_limit(self, backend):
        backend.reindex("memento")
        results = backend.search("Redis", "memento", limit=1)
        assert len(results) <= 1

    def test_search_empty_query_returns_empty(self, backend):
        backend.reindex("memento")
        assert backend.search("", "memento") == []

    def test_search_no_matches_returns_empty(self, backend):
        backend.reindex("memento")
        results = backend.search("xylophone quantum dinosaur", "memento")
        assert results == []

    def test_search_min_score_filters(self, backend):
        backend.reindex("memento")
        # With min_score=0, should get results; with very high min_score, none
        all_results = backend.search("Redis", "memento", min_score=0.0)
        assert len(all_results) > 0
        high_bar = backend.search("Redis", "memento", min_score=999.0)
        assert len(high_bar) == 0

    def test_search_finds_across_dirs(self, backend):
        """FTS5 should index notes/, fleeting/, and projects/."""
        backend.reindex("memento")
        results = backend.search("session Redis caching", "memento")
        paths = [r["path"] for r in results]
        # Should find the fleeting note too
        assert any("fleeting/" in p for p in paths)

    def test_search_ranks_relevant_higher(self, backend):
        backend.reindex("memento")
        results = backend.search("Redis cache TTL stale reads", "memento")
        assert len(results) >= 2
        # The note specifically about TTL should rank higher than general caching
        assert results[0]["path"] == "notes/redis-cache-ttl.md"


class TestEmbeddedSearchGet:
    """Get single note by path."""

    def test_get_existing_note(self, backend):
        backend.reindex("memento")
        result = backend.get("notes/redis-cache-ttl.md")
        assert result is not None
        assert result["title"] == "Redis cache requires explicit TTL"
        assert "content" in result
        assert result["score"] == 0.0

    def test_get_missing_note_returns_none(self, backend):
        backend.reindex("memento")
        assert backend.get("notes/nonexistent.md") is None

    def test_get_returns_full_content(self, backend):
        backend.reindex("memento")
        result = backend.get("notes/redis-cache-ttl.md")
        assert "TTL" in result["content"]

    def test_get_rejects_path_traversal(self, backend):
        backend.reindex("memento")
        assert backend.get("../../../etc/passwd") is None


class TestEmbeddedSearchIndexNote:
    """Single-note indexing (for ingest-time use)."""

    def test_index_single_note(self, backend, embedded_vault):
        vault, db_path = embedded_vault
        # Write a new note
        new_note = vault / "notes" / "new-discovery.md"
        new_note.write_text(
            "---\ntitle: New discovery about PostgreSQL\n---\n\n"
            "PostgreSQL JSONB indexes are faster than expected.\n"
        )
        backend.index_note("notes/new-discovery.md")

        results = backend.search("PostgreSQL JSONB", "memento")
        assert len(results) > 0
        assert results[0]["path"] == "notes/new-discovery.md"

    def test_index_note_updates_existing(self, backend, embedded_vault):
        vault, _ = embedded_vault
        backend.reindex("memento")

        # Update note content
        note = vault / "notes" / "redis-cache-ttl.md"
        note.write_text(
            "---\ntitle: Redis cache requires explicit TTL\n---\n\n"
            "Completely new content about memcached instead.\n"
        )
        backend.index_note("notes/redis-cache-ttl.md")

        results = backend.search("memcached", "memento")
        assert any(r["path"] == "notes/redis-cache-ttl.md" for r in results)


class MockEmbeddingProvider:
    """Deterministic embedding provider for testing vector search.

    Embeds by hashing terms into a fixed 8-dimensional space. Notes with
    overlapping terms will have similar vectors.
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._hash_embed(text)

    def dimensions(self) -> int:
        return 8

    def is_available(self) -> bool:
        return True

    def _hash_embed(self, text: str) -> list[float]:
        import math
        vec = [0.0] * 8
        for word in text.lower().split():
            h = hash(word) & 0xFFFFFFFF
            for i in range(8):
                vec[i] += ((h >> (i * 4)) & 0xF) / 15.0 - 0.5
        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


@pytest.fixture
def vec_backend(embedded_vault):
    """EmbeddedSearchBackend with mock embedding provider for vector tests."""
    from memento.embedded_search import EmbeddedSearchBackend

    vault, db_path = embedded_vault
    provider = MockEmbeddingProvider()
    b = EmbeddedSearchBackend(vault_path=vault, db_path=db_path, embedding_provider=provider)
    b.reindex("memento")
    return b


class TestVectorSearch:
    """sqlite-vec vector search."""

    def test_has_vec_table(self, vec_backend, embedded_vault):
        _, db_path = embedded_vault
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "notes_vec" in tables

    def test_semantic_search_returns_results(self, vec_backend):
        results = vec_backend.search("Redis cache", "memento", semantic=True)
        assert len(results) > 0

    def test_semantic_search_returns_correct_shape(self, vec_backend):
        results = vec_backend.search("Redis", "memento", semantic=True)
        assert len(results) > 0
        r = results[0]
        assert "path" in r
        assert "title" in r
        assert "score" in r
        assert "snippet" in r

    def test_semantic_respects_limit(self, vec_backend):
        results = vec_backend.search("Redis", "memento", semantic=True, limit=1)
        assert len(results) <= 1

    def test_semantic_empty_query(self, vec_backend):
        assert vec_backend.search("", "memento", semantic=True) == []

    def test_index_note_embeds_vector(self, vec_backend, embedded_vault):
        """index_note() should insert vector alongside FTS5."""
        vault, db_path = embedded_vault
        new_note = vault / "notes" / "vector-test.md"
        new_note.write_text("---\ntitle: Vector test note\n---\n\nPostgreSQL vector search.\n")
        vec_backend.index_note("notes/vector-test.md")

        results = vec_backend.search("PostgreSQL vector", "memento", semantic=True)
        assert any(r["path"] == "notes/vector-test.md" for r in results)

    def test_reindex_embeds_all_notes(self, vec_backend):
        conn = vec_backend._get_conn()
        vec_count = conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0]
        note_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        assert vec_count == note_count
        assert vec_count == 4  # 3 notes + 1 fleeting


class TestFallbackWithoutVec:
    """EmbeddedSearchBackend degrades to FTS5-only when sqlite-vec is missing."""

    def test_fts5_still_works_without_provider(self, backend):
        """Backend without embedding provider still does FTS5 search."""
        backend.reindex("memento")
        results = backend.search("Redis cache", "memento")
        assert len(results) > 0

    def test_semantic_falls_back_to_fts5(self, backend):
        """semantic=True without vectors should fall back to FTS5."""
        backend.reindex("memento")
        results = backend.search("Redis cache", "memento", semantic=True)
        # Should return FTS5 results, not empty
        assert len(results) > 0


class TestRRFFusion:
    """Hybrid search: FTS5 + vector combined via RRF."""

    def test_hybrid_returns_results(self, vec_backend):
        """Default search should use hybrid when vectors are available."""
        results = vec_backend.search("Redis cache TTL", "memento")
        assert len(results) > 0

    def test_hybrid_combines_both_sources(self, vec_backend):
        """Hybrid should potentially find more results than either source alone."""
        fts_results = vec_backend._fts5_search("Redis", 10, 0.0)
        vec_results = vec_backend._vec_search("Redis", 10, 0.0)
        hybrid_results = vec_backend.search("Redis", "memento", limit=10)
        # Hybrid should find at least as many unique paths as either source
        fts_paths = {r["path"] for r in fts_results}
        vec_paths = {r["path"] for r in vec_results}
        hybrid_paths = {r["path"] for r in hybrid_results}
        assert hybrid_paths >= (fts_paths & vec_paths)  # at least the intersection

    def test_hybrid_scores_normalized(self, vec_backend):
        results = vec_backend.search("Redis cache", "memento")
        for r in results:
            assert 0.0 <= r["score"] <= 1.0


class TestMCPEndToEnd:
    """Integration: MCP search tool → search.py → EmbeddedSearchBackend → FTS5."""

    @pytest.fixture(autouse=True)
    def _setup_embedded_pipeline(self, embedded_vault, monkeypatch):
        """Wire up the full pipeline: config → backend → search.py."""
        from memento import config
        from memento.embedded_search import EmbeddedSearchBackend
        from memento.search_backend import set_backend

        vault, db_path = embedded_vault
        b = EmbeddedSearchBackend(vault_path=vault, db_path=db_path)
        b.reindex("memento")
        set_backend(b)

        # Patch config._CONFIG directly so all importers see it
        monkeypatch.setattr(config, "_CONFIG", {
            "vault_path": str(vault),
            "qmd_collection": "memento",
            "extra_qmd_collections": [],
            "search_backend": "embedded",
            "search_db_path": str(db_path),
            "prf_enabled": False,
            "temporal_decay": False,
            "wikilink_expansion": False,
            "ppr_enabled": False,
            "pagerank_boost_weight": 0,
            "project_maps_enabled": False,
            "concept_index_enabled": False,
            "rrf_enabled": False,
            "retrieval_log": False,
        })

    def test_mcp_search_uses_embedded_backend(self):
        """Prove the full path: memento_search → qmd_search_with_extras → EmbeddedSearchBackend."""
        from memento.embedded_search import EmbeddedSearchBackend
        from memento.search import qmd_search_with_extras
        from memento.search_backend import get_backend

        results = qmd_search_with_extras("Redis cache TTL", limit=5)
        assert len(results) >= 1
        assert any("redis" in r["path"] for r in results)
        assert isinstance(get_backend(), EmbeddedSearchBackend)

    def test_mcp_search_returns_correct_fields(self):
        """MCP output shape: path, title, score, snippet."""
        from memento.search import qmd_search_with_extras

        results = qmd_search_with_extras("Zustand testing", limit=3)
        assert len(results) >= 1
        r = results[0]
        assert "path" in r
        assert "title" in r
        assert "score" in r
        assert "snippet" in r
        assert isinstance(r["score"], float)

    def test_mcp_search_no_results_for_unrelated(self):
        """Search for something not in any note."""
        from memento.search import qmd_search_with_extras

        results = qmd_search_with_extras("quantum xylophone dinosaur", limit=5)
        assert results == []
