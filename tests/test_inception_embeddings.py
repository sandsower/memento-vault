"""Tests for QMD embedding extraction in Inception."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from memento_inception import (
    load_active_backend_embeddings,
    load_embedded_vectors,
    load_embeddings,
)


def _sqlite_vec_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        if not hasattr(conn, "enable_load_extension"):
            return False
        try:
            import sqlite_vec
        except ImportError:
            return False
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        conn.execute("CREATE VIRTUAL TABLE vec_probe USING vec0(embedding float[2])")
        return True
    except (AttributeError, sqlite3.Error):
        return False
    finally:
        conn.close()


class TestLoadEmbeddingsBasic:
    def test_load_embeddings_basic(self, mock_qmd_db):
        """Load embeddings for known stems, verify 768-dim vectors returned."""
        stems = ["redis-cache-ttl", "redis-eviction-policy"]
        result = load_embeddings(stems, db_path=mock_qmd_db)

        assert len(result) == 2
        for stem in stems:
            assert stem in result
            assert isinstance(result[stem], np.ndarray)
            assert result[stem].shape == (768,)
            assert result[stem].dtype == np.float32


class TestLoadEmbeddingsMeanPooling:
    def test_load_embeddings_mean_pooling(self, tmp_path):
        """Verify result is the mean of chunks using known vectors."""
        db_path = tmp_path / "pool_test.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL, path TEXT NOT NULL,
                title TEXT NOT NULL, hash TEXT NOT NULL,
                created_at TEXT NOT NULL, modified_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(collection, path)
            )
        """)
        conn.execute("""
            CREATE TABLE content_vectors (
                hash TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
                pos INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL,
                embedded_at TEXT NOT NULL, PRIMARY KEY (hash, seq)
            )
        """)
        conn.execute("""
            CREATE TABLE vectors_vec_rowids (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, chunk_id INTEGER, chunk_offset INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE vectors_vec_vector_chunks00 (
                rowid PRIMARY KEY, vectors BLOB NOT NULL
            )
        """)

        # Insert a document with 2 chunks of known vectors
        doc_hash = "hash_test"
        conn.execute(
            "INSERT INTO documents (collection, path, title, hash, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("memento", "notes/pooling-test.md", "pooling-test", doc_hash, "2026-03-22", "2026-03-22"),
        )

        chunk_0 = np.ones(768, dtype=np.float32) * 2.0
        chunk_1 = np.ones(768, dtype=np.float32) * 4.0
        expected_mean = np.ones(768, dtype=np.float32) * 3.0
        expected_norm = np.linalg.norm(expected_mean)
        expected_normalized = expected_mean / expected_norm

        dim = 768
        vec_size = dim * 4
        chunk_blob = bytearray(1024 * vec_size)

        for seq, vec in enumerate([chunk_0, chunk_1]):
            conn.execute(
                "INSERT INTO content_vectors (hash, seq, model, embedded_at) VALUES (?, ?, ?, ?)",
                (doc_hash, seq, "test-model", "2026-03-22T00:00"),
            )
            vec_id = f"{doc_hash}_{seq}"
            conn.execute(
                "INSERT INTO vectors_vec_rowids (id, chunk_id, chunk_offset) VALUES (?, ?, ?)",
                (vec_id, 1, seq),
            )
            start = seq * vec_size
            chunk_blob[start : start + vec_size] = vec.tobytes()

        conn.execute(
            "INSERT INTO vectors_vec_vector_chunks00 (rowid, vectors) VALUES (?, ?)",
            (1, bytes(chunk_blob)),
        )
        conn.commit()
        conn.close()

        result = load_embeddings(["pooling-test"], db_path=db_path)

        assert "pooling-test" in result
        np.testing.assert_allclose(result["pooling-test"], expected_normalized, atol=1e-6)


class TestLoadEmbeddingsNormalized:
    def test_load_embeddings_normalized(self, mock_qmd_db):
        """Verify returned vectors have L2 norm approximately 1.0."""
        stems = [
            "redis-cache-ttl",
            "redis-eviction-policy",
            "redis-cache-invalidation",
            "zustand-state-reset",
            "react-query-wrapper",
        ]
        result = load_embeddings(stems, db_path=mock_qmd_db)

        assert len(result) == 5
        for stem, vec in result.items():
            norm = np.linalg.norm(vec)
            assert abs(norm - 1.0) < 1e-5, f"{stem} has norm {norm}, expected ~1.0"


class TestLoadEmbeddingsMissingStem:
    def test_load_embeddings_missing_stem(self, mock_qmd_db):
        """Request a stem that does not exist, verify it is absent from result."""
        result = load_embeddings(["nonexistent-note"], db_path=mock_qmd_db)

        assert "nonexistent-note" not in result
        assert result == {}


class TestLoadEmbeddingsNoDb:
    def test_load_embeddings_no_db(self, tmp_path):
        """Pass a nonexistent db_path, verify empty dict returned."""
        bogus_path = tmp_path / "does" / "not" / "exist.sqlite"
        result = load_embeddings(["redis-cache-ttl"], db_path=bogus_path)

        assert result == {}


class TestLoadEmbeddingsSubset:
    def test_load_embeddings_subset(self, mock_qmd_db):
        """Request only 2 of 5 stems, verify only those 2 returned."""
        result = load_embeddings(["zustand-state-reset", "react-query-wrapper"], db_path=mock_qmd_db)

        assert len(result) == 2
        assert "zustand-state-reset" in result
        assert "react-query-wrapper" in result
        # Ensure none of the unrequested stems leak through
        assert "redis-cache-ttl" not in result
        assert "redis-eviction-policy" not in result
        assert "redis-cache-invalidation" not in result


class TestQmdVectorDimDetection:
    """MEM-157: QMD's own schema declares vector width; load_embeddings must
    not assume 768 unconditionally."""

    def test_load_embeddings_reads_non_default_dim_from_schema(self, tmp_path):
        """When QMD's real vec0 table declares float[512] (not 768), the
        returned vectors must be 512-dim, detected from the schema rather
        than the historical hardcoded constant."""
        if not _sqlite_vec_available():
            pytest.skip("sqlite-vec extension loading is unavailable")
        import sqlite_vec

        dim = 512
        db_path = tmp_path / "custom_dim.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL, path TEXT NOT NULL,
                title TEXT NOT NULL, hash TEXT NOT NULL,
                created_at TEXT NOT NULL, modified_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(collection, path)
            )
        """)
        conn.execute("""
            CREATE TABLE content_vectors (
                hash TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
                pos INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL,
                embedded_at TEXT NOT NULL, PRIMARY KEY (hash, seq)
            )
        """)

        # Build the REAL parent vec0 table via the extension so its
        # CREATE VIRTUAL TABLE statement (with "float[512]") lands in
        # sqlite_master -- this is what _detect_qmd_vector_dim reads. The
        # shadow tables it auto-creates (vectors_vec_rowids,
        # vectors_vec_vector_chunks00, ...) are then populated directly,
        # exactly like the hand-rolled mock_qmd_db fixture does, since
        # load_embeddings never goes through the vec0 query engine itself.
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        conn.execute(f"CREATE VIRTUAL TABLE vectors_vec USING vec0(embedding float[{dim}])")

        doc_hash = "hash_custom"
        conn.execute(
            "INSERT INTO documents (collection, path, title, hash, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("memento", "notes/custom-dim-note.md", "custom-dim-note", doc_hash, "2026-03-22", "2026-03-22"),
        )
        conn.execute(
            "INSERT INTO content_vectors (hash, seq, model, embedded_at) VALUES (?, ?, ?, ?)",
            (doc_hash, 0, "test-model", "2026-03-22T00:00"),
        )
        vec_id = f"{doc_hash}_0"
        conn.execute(
            "INSERT INTO vectors_vec_rowids (id, chunk_id, chunk_offset) VALUES (?, ?, ?)",
            (vec_id, 1, 0),
        )
        vec = np.ones(dim, dtype=np.float32)
        vec_size = dim * 4
        conn.execute(
            "INSERT INTO vectors_vec_vector_chunks00 (rowid, vectors) VALUES (?, ?)",
            (1, vec.tobytes()[:vec_size]),
        )
        conn.commit()
        conn.close()

        result = load_embeddings(["custom-dim-note"], db_path=db_path)

        assert "custom-dim-note" in result
        assert result["custom-dim-note"].shape == (dim,)

    def test_load_embeddings_falls_back_to_768_without_schema(self, mock_qmd_db):
        """mock_qmd_db never creates the real vectors_vec parent table (it
        hand-rolls only the shadow tables), so dim detection can't find a
        schema declaration. It must fall back to the previous 768 default
        rather than raising or silently misreading vectors."""
        result = load_embeddings(["redis-cache-ttl"], db_path=mock_qmd_db)
        assert result["redis-cache-ttl"].shape == (768,)


class TestLoadEmbeddedVectors:
    """MEM-157: the default QMD-less install uses the embedded search
    backend, which stores note-level (not chunked) vectors at whatever
    dimension the embedding provider produces -- never a hardcoded 768."""

    def _make_backend(self, tmp_path, dims=8, stems=("alpha-note", "beta-note")):
        if not _sqlite_vec_available():
            pytest.skip("sqlite-vec extension loading is unavailable")
        from memento.embedded_search import EmbeddedSearchBackend

        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        for stem in stems:
            (vault / "notes" / f"{stem}.md").write_text(f"---\ntitle: {stem}\n---\n\nBody for {stem}.\n")

        class _FixedDimsProvider:
            def __init__(self, dims):
                self._dims = dims

            def dimensions(self):
                return self._dims

            def is_available(self):
                return True

            def embed(self, texts):
                # Deterministic per-text vector, L2-normalized.
                vectors = []
                for i, _ in enumerate(texts):
                    vec = np.zeros(self._dims, dtype=np.float32)
                    vec[i % self._dims] = 1.0
                    vectors.append(vec.tolist())
                return vectors

            def embed_query(self, text):
                return self.embed([text])[0]

        db_path = vault / ".search" / "search.db"
        backend = EmbeddedSearchBackend(vault_path=vault, db_path=db_path, embedding_provider=_FixedDimsProvider(dims))
        backend.reindex("memento")
        return backend

    def test_load_embedded_vectors_uses_backend_dim(self, tmp_path):
        """Vectors come back at the provider's own dimensionality (8 here),
        proving dim is read from backend metadata, not a hardcoded constant."""
        backend = self._make_backend(tmp_path, dims=8)
        result = load_embedded_vectors(["alpha-note", "beta-note"], backend)

        assert set(result) == {"alpha-note", "beta-note"}
        for stem, vec in result.items():
            assert isinstance(vec, np.ndarray)
            assert vec.shape == (8,)

    def test_load_embedded_vectors_no_pooling_needed(self, tmp_path):
        """Embedded backend is note-level (one vector per note), so a single
        note's vector should come back unchanged, unlike QMD's chunk mean-pool."""
        backend = self._make_backend(tmp_path, dims=4, stems=("solo-note",))
        result = load_embedded_vectors(["solo-note"], backend)

        assert "solo-note" in result
        assert result["solo-note"].shape == (4,)

    def test_load_embedded_vectors_missing_stem(self, tmp_path):
        """Requesting a stem with no indexed note returns an empty result
        for it, matching load_embeddings' QMD behavior for missing stems."""
        backend = self._make_backend(tmp_path, dims=4, stems=("solo-note",))
        result = load_embedded_vectors(["nonexistent-note"], backend)

        assert result == {}

    def test_load_embedded_vectors_no_provider_returns_empty(self, tmp_path):
        """A backend with no embedding provider (FTS5-only) has no vectors
        to offer -- must return {} rather than error."""
        from memento.embedded_search import EmbeddedSearchBackend

        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "notes" / "solo-note.md").write_text("---\ntitle: solo-note\n---\n\nBody.\n")
        backend = EmbeddedSearchBackend(vault_path=vault, db_path=vault / ".search" / "search.db")
        backend.reindex("memento")

        result = load_embedded_vectors(["solo-note"], backend)
        assert result == {}


class TestLoadActiveBackendEmbeddings:
    """MEM-157: the vector source is resolved from the vault's active search
    backend (QMD -> Embedded -> none), decoupling Inception from a
    QMD-only assumption."""

    def test_explicit_db_path_forces_qmd_regardless_of_active_backend(self, mock_qmd_db, mock_config):
        """Passing db_path (as tests/callers already do) must keep forcing
        the QMD reader against that literal path -- backward compatible with
        every existing caller that relies on this override."""
        embeddings, source, reason = load_active_backend_embeddings(
            ["redis-cache-ttl", "redis-eviction-policy"], mock_config, db_path=mock_qmd_db
        )

        assert source == "qmd"
        assert reason is None
        assert set(embeddings) == {"redis-cache-ttl", "redis-eviction-policy"}

    def test_resolves_embedded_backend_when_qmd_absent(self, tmp_vault, mock_config):
        """QMD-less install (the auto-selected default): get_backend()
        resolves to the embedded backend, so Inception must source vectors
        from it instead of silently finding nothing."""
        if not _sqlite_vec_available():
            pytest.skip("sqlite-vec extension loading is unavailable")
        from memento.embedded_search import EmbeddedSearchBackend

        vault = tmp_vault
        (vault / "notes" / "alpha-note.md").write_text("---\ntitle: alpha-note\n---\n\nBody.\n")

        class _Provider:
            def dimensions(self):
                return 6

            def is_available(self):
                return True

            def embed(self, texts):
                return [[1.0] + [0.0] * 5 for _ in texts]

            def embed_query(self, text):
                return self.embed([text])[0]

        backend = EmbeddedSearchBackend(
            vault_path=vault, db_path=vault / ".search" / "search.db", embedding_provider=_Provider()
        )
        backend.reindex("memento")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento_inception.get_backend", lambda: backend)
            embeddings, source, reason = load_active_backend_embeddings(["alpha-note"], mock_config, db_path=None)

        assert source == "embedded"
        assert reason is None
        assert embeddings["alpha-note"].shape == (6,)

    def test_no_vector_backend_returns_explicit_reason(self, mock_config):
        """Grep-only (or no) backend has no embeddings. This must not
        silently no-op -- callers need an explicit, actionable reason."""
        grep_like = MagicMock()
        grep_like.is_available.return_value = True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento_inception.get_backend", lambda: grep_like)
            embeddings, source, reason = load_active_backend_embeddings(["alpha-note"], mock_config, db_path=None)

        assert embeddings == {}
        assert source == "none"
        assert reason == "no-vector-backend"

    def test_qmd_backend_selected_but_empty_reports_qmd_empty(self, mock_config, tmp_path):
        """Active backend resolves to QMD but the index has nothing for
        these stems -- must report qmd/qmd-empty, not silently succeed.

        Path.home() is patched to an empty tmp dir so this never touches a
        real ~/.cache/qmd/index.sqlite on the machine running the tests.
        """
        from memento.search_backend import QMDBackend

        qmd_like = MagicMock(spec=QMDBackend)
        qmd_like.is_available.return_value = True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento_inception.get_backend", lambda: qmd_like)
            mp.setattr(Path, "home", lambda: tmp_path)
            embeddings, source, reason = load_active_backend_embeddings(["nonexistent-note"], mock_config, db_path=None)

        assert embeddings == {}
        assert source == "qmd"
        assert reason == "qmd-empty"
