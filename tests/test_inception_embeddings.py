"""Tests for QMD embedding extraction in Inception."""

import sqlite3

import numpy as np
import pytest

from memento_inception import load_embeddings


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
            "INSERT INTO documents (collection, path, title, hash, created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
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
            chunk_blob[start:start + vec_size] = vec.tobytes()

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
        result = load_embeddings(
            ["zustand-state-reset", "react-query-wrapper"], db_path=mock_qmd_db
        )

        assert len(result) == 2
        assert "zustand-state-reset" in result
        assert "react-query-wrapper" in result
        # Ensure none of the unrequested stems leak through
        assert "redis-cache-ttl" not in result
        assert "redis-eviction-policy" not in result
        assert "redis-cache-invalidation" not in result
