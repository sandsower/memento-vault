"""Shared fixtures for Inception tests."""

import json
import os
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

# Add hooks/ to path so we can import memento_utils and memento-inception
sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

# memento-inception.py has a hyphen; register it as memento_inception for imports
import importlib.util as _ilu

_inception_spec = _ilu.spec_from_file_location(
    "memento_inception",
    str(Path(__file__).parent.parent / "hooks" / "memento-inception.py"),
)
if _inception_spec and _inception_spec.loader:
    _inception_mod = _ilu.module_from_spec(_inception_spec)
    sys.modules["memento_inception"] = _inception_mod
    _inception_spec.loader.exec_module(_inception_mod)


@pytest.fixture
def tmp_vault(tmp_path):
    """Create a temporary vault with standard directory structure."""
    vault = tmp_path / "vault"
    for d in ("notes", "fleeting", "projects", "archive"):
        (vault / d).mkdir(parents=True)
    return vault


@pytest.fixture
def sample_notes(tmp_vault):
    """Create sample notes with frontmatter in the tmp vault."""
    notes = {
        "redis-cache-ttl": {
            "title": "Redis cache requires explicit TTL",
            "type": "discovery",
            "tags": ["redis", "caching"],
            "date": "2026-03-10T14:00",
            "certainty": 4,
            "project": "/home/vic/Projects/api-service",
            "body": "Setting explicit TTL on Redis keys prevents stale reads.",
        },
        "redis-eviction-policy": {
            "title": "Redis eviction policy for sessions",
            "type": "discovery",
            "tags": ["redis", "sessions"],
            "date": "2026-03-12T10:00",
            "certainty": 3,
            "project": "/home/vic/Projects/api-service",
            "body": "Use allkeys-lru eviction for session stores.",
        },
        "redis-cache-invalidation": {
            "title": "API response cache invalidation strategy",
            "type": "decision",
            "tags": ["redis", "caching", "api"],
            "date": "2026-03-15T09:00",
            "certainty": 4,
            "project": "/home/vic/Projects/billing",
            "body": "Invalidate on write, not on TTL expiry, for billing endpoints.",
        },
        "zustand-state-reset": {
            "title": "Zustand mock state resets between tests",
            "type": "bugfix",
            "tags": ["zustand", "testing", "react"],
            "date": "2026-03-11T16:00",
            "certainty": 3,
            "project": "/home/vic/Projects/frontend",
            "body": "Zustand store must be reset in beforeEach to avoid test bleed.",
        },
        "react-query-wrapper": {
            "title": "React Query test wrapper required",
            "type": "discovery",
            "tags": ["react-query", "testing", "react"],
            "date": "2026-03-14T11:00",
            "certainty": 3,
            "project": "/home/vic/Projects/frontend",
            "body": "QueryClientProvider wrapper needed in test setup for React Query.",
        },
        "existing-pattern": {
            "title": "Cross-project Redis patterns",
            "type": "pattern",
            "tags": ["redis", "caching"],
            "date": "2026-03-18T08:00",
            "certainty": 3,
            "source": "inception",
            "synthesized_from": ["redis-cache-ttl", "redis-eviction-policy"],
            "body": "Redis caching patterns recur across services.",
        },
        "archived-note": {
            "title": "Old discovery about caching",
            "type": "discovery",
            "tags": ["caching"],
            "date": "2026-01-01T12:00",
            "certainty": 2,
            "body": "Some old caching insight.",
        },
    }

    created = {}
    for stem, data in notes.items():
        # Write note to notes/ (except archived-note goes to archive/)
        if stem == "archived-note":
            path = tmp_vault / "archive" / f"{stem}.md"
        else:
            path = tmp_vault / "notes" / f"{stem}.md"

        lines = ["---"]
        lines.append(f"title: {data['title']}")
        lines.append(f"type: {data['type']}")
        tags_str = "[" + ", ".join(data["tags"]) + "]"
        lines.append(f"tags: {tags_str}")
        lines.append(f"date: {data['date']}")
        if data.get("certainty"):
            lines.append(f"certainty: {data['certainty']}")
        if data.get("project"):
            lines.append(f"project: {data['project']}")
        if data.get("source"):
            lines.append(f"source: {data['source']}")
        if data.get("synthesized_from"):
            lines.append("synthesized_from:")
            for s in data["synthesized_from"]:
                lines.append(f"  - {s}")
        lines.append("---")
        lines.append("")
        lines.append(data["body"])
        lines.append("")
        lines.append("## Related")
        lines.append("")

        path.write_text("\n".join(lines))
        created[stem] = path

    return created


@pytest.fixture
def mock_qmd_db(tmp_path):
    """Create a mock QMD SQLite database matching the real vec0 chunk schema."""
    import numpy as np

    db_path = tmp_path / "qmd" / "index.sqlite"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(str(db_path))

    # Real QMD schema
    conn.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            modified_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(collection, path)
        )
    """)
    conn.execute("""
        CREATE TABLE content_vectors (
            hash TEXT NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0,
            pos INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL,
            embedded_at TEXT NOT NULL,
            PRIMARY KEY (hash, seq)
        )
    """)
    # Mock the vec0 internal tables (can't use the extension in tests)
    conn.execute("""
        CREATE TABLE vectors_vec_rowids (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL,
            chunk_id INTEGER,
            chunk_offset INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE vectors_vec_vector_chunks00 (
            rowid PRIMARY KEY,
            vectors BLOB NOT NULL
        )
    """)
    conn.commit()

    rng = np.random.RandomState(42)
    dim = 768
    vec_size = dim * 4  # float32
    chunk_size = 1024  # max vectors per chunk

    # Pre-allocate a single chunk blob (we'll pack all vectors into chunk 1)
    all_vectors = bytearray(chunk_size * vec_size)
    next_offset = 0

    def add_note_with_base(stem, base_vec, noise_scale=0.1, n_chunks=2):
        nonlocal next_offset
        doc_hash = f"hash_{stem}"
        path = f"notes/{stem}.md"

        conn.execute(
            "INSERT INTO documents (collection, path, title, hash, created_at, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("memento", path, stem, doc_hash, "2026-03-22", "2026-03-22"),
        )

        for seq in range(n_chunks):
            noise = rng.randn(dim).astype(np.float32) * noise_scale
            vec = (base_vec + noise).astype(np.float32)
            vec_bytes = vec.tobytes()

            conn.execute(
                "INSERT INTO content_vectors (hash, seq, model, embedded_at) VALUES (?, ?, ?, ?)",
                (doc_hash, seq, "test-model", "2026-03-22T00:00"),
            )

            vec_id = f"{doc_hash}_{seq}"
            conn.execute(
                "INSERT INTO vectors_vec_rowids (id, chunk_id, chunk_offset) VALUES (?, ?, ?)",
                (vec_id, 1, next_offset),
            )

            # Pack vector into chunk blob
            start = next_offset * vec_size
            all_vectors[start:start + vec_size] = vec_bytes
            next_offset += 1

    # Make redis notes similar, testing notes similar but different cluster
    redis_base = rng.randn(dim).astype(np.float32)
    redis_base = redis_base / np.linalg.norm(redis_base)

    testing_base = rng.randn(dim).astype(np.float32)
    testing_base = testing_base / np.linalg.norm(testing_base)

    add_note_with_base("redis-cache-ttl", redis_base, noise_scale=0.05)
    add_note_with_base("redis-eviction-policy", redis_base, noise_scale=0.05)
    add_note_with_base("redis-cache-invalidation", redis_base, noise_scale=0.05)
    add_note_with_base("zustand-state-reset", testing_base, noise_scale=0.05)
    add_note_with_base("react-query-wrapper", testing_base, noise_scale=0.05)

    # Write the chunk blob
    conn.execute(
        "INSERT INTO vectors_vec_vector_chunks00 (rowid, vectors) VALUES (?, ?)",
        (1, bytes(all_vectors)),
    )

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture
def mock_config(tmp_vault):
    """Return a config dict pointing at the tmp vault."""
    from memento_utils import DEFAULT_CONFIG

    config = dict(DEFAULT_CONFIG)
    config["vault_path"] = str(tmp_vault)
    config["inception_enabled"] = True
    config["inception_backend"] = "codex"
    config["qmd_collection"] = "memento"
    return config


@pytest.fixture
def inception_state_path(tmp_path):
    """Return a temp path for inception state."""
    state_dir = tmp_path / "config" / "memento-vault"
    state_dir.mkdir(parents=True)
    return state_dir / "inception-state.json"
