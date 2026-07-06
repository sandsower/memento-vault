"""Embedded search backend — SQLite FTS5 + sqlite-vec.

Provides full-text and vector search without external dependencies like QMD.
Uses a single search.db file stored alongside the vault. The markdown files
remain the source of truth; the database is a derived, disposable index.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import struct
import threading
from datetime import datetime
from pathlib import Path

from memento.search_backend import (
    STALE_INDEX_WARN_SECONDS,
    SearchBackend,
    _literal_score,
    _literal_snippet,
    classify_index_lag,
    newest_note_mtime,
    normalize_grep_term_coverage,
)

logger = logging.getLogger(__name__)

_MAX_EMBED_BATCH = 64
_MAX_NOTE_SIZE_FOR_EMBED = 100_000  # 100KB
_METADATA_TABLE = "index_metadata"
_EMBEDDING_SIGNATURE_KEY = "embedding_signature"
_VEC_DISTANCE_METRIC = "cosine"

# Bounded-transform constant for FTS5 BM25 rank normalization (MEM-127): see
# normalize_fts5_score() below for the empirical rationale. Read from config
# ("fts5_score_k") by callers that construct this backend from the singleton
# (memento.search_backend._make_embedded); this module-level default is only
# the fallback for direct instantiation (e.g. tests, scripts).
_DEFAULT_FTS5_SCORE_K = 2.0


def normalize_fts5_score(raw_score: float, k: float = _DEFAULT_FTS5_SCORE_K) -> float:
    """Map a raw FTS5 BM25 rank value to [0, 1] via a bounded transform (MEM-127).

    Replaces the old ``score / max_score_in_this_batch`` normalization, which
    forced the *top hit in any result batch* to exactly 1.0 regardless of its
    true relevance - a single mediocre FTS5 hit therefore looked like a
    perfect match to the ``single_strong_hit`` / ``confidence_margin`` gates
    in retrieval_policy.py, which incorrectly skipped the deep pipeline
    (PRF/RRF/rerank) on the embedded backend even for weak matches. This is
    the concrete instance of "the deep pipeline never fires on the embedded
    backend" from the MEM-127 ticket.

    ``score / (score + k)`` is monotonic increasing, 0 at raw <= 0, and
    approaches 1 as raw -> infinity - a proper bounded transform rather than
    a batch-relative rescale. ``k`` calibrates how much raw BM25 magnitude
    counts as "strong": the default (2.0) was chosen empirically against a
    small fixture vault (see tests/test_embedded_search.py and the MEM-127
    report) where a single rare, discriminating term match raw-scored
    ~1.0-1.7 (mapping to ~0.4-0.6), stacking toward 0.8+ with multiple
    distinguishing terms, while common-term-only matches (near-zero raw,
    since FTS5's BM25 IDF collapses toward zero when a term appears in most
    indexed documents) stay near 0. Raw BM25 magnitude scales with corpus
    size and term rarity, so ``k`` is a coarse, vault-size-independent
    starting point - tune via ``fts5_score_k`` in config if a specific vault's
    score distribution warrants it (benchmark/optuna_sweep.py is the existing
    harness for that kind of calibration sweep).
    """
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return 0.0
    if score != score or score <= 0:  # NaN or non-positive
        return 0.0
    if k <= 0:
        k = 1e-6
    return max(0.0, min(1.0, score / (score + k)))


def normalize_vec_cosine_distance(distance: float) -> float:
    """Map a sqlite-vec cosine distance to a [0, 1] relevance score (MEM-127).

    ``notes_vec`` is declared with ``distance_metric=cosine`` (MEM-127;
    previously unspecified, which sqlite-vec defaults to L2). Empirically
    (see MEM-127 report), sqlite-vec's default L2 distance for unit vectors
    ranges [0, 2]: identical vectors -> 0, orthogonal (unrelated) -> sqrt(2)
    ~= 1.414, opposite -> 2.0. The old ``max(0.0, 1.0 - distance)`` transform
    therefore collapsed BOTH orthogonal and opposite vectors to a score of
    0, discarding the negative-similarity signal entirely (everything past
    distance=1 read identically as "no relevance"). It also silently assumed
    every embedding provider L2-normalizes its output, which is only
    guaranteed for the local Nomic provider (see memento.embedding
    ``_truncate_and_normalize``), not the remote Voyage/OpenAI/Google
    providers.

    Cosine distance is magnitude-independent and defined as
    ``1 - cosine_similarity``, range [0, 2] regardless of vector norm.
    Rescale to the conventional ``(cos + 1) / 2`` relevance band: identical
    direction -> 1.0, orthogonal (no relationship) -> 0.5, opposite -> 0.0.
    """
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if d != d:  # NaN
        return 0.0
    cos_sim = 1.0 - d
    return max(0.0, min(1.0, (cos_sim + 1.0) / 2.0))


def _is_within_vault(path: Path, vault: Path) -> bool:
    """Check if resolved path is within the vault. Safe against sibling prefix attacks."""
    try:
        resolved = path.resolve()
        vault_resolved = vault.resolve()
        resolved.relative_to(vault_resolved)
        return True
    except (ValueError, OSError):
        return False


def _extract_title(content: str, fallback: str) -> str:
    """Extract title from frontmatter or fall back to filename stem."""
    for line in content.splitlines()[:15]:
        stripped = line.strip()
        if stripped.lower().startswith("title:"):
            return stripped[6:].strip().strip("\"'")
    return fallback


def _extract_snippet(content: str, query: str, max_len: int = 200) -> str:
    """Extract a snippet from content, preferring lines matching the query."""
    terms = query.lower().split()
    # Skip frontmatter
    body_start = 0
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            body_start = end + 3

    body = content[body_start:].strip()
    # Try to find a line matching query terms
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and any(t in stripped.lower() for t in terms):
            return stripped[:max_len]
    # Fall back to first non-empty body line
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:max_len]
    return body[:max_len]


def _vec_to_blob(vec: list[float]) -> bytes:
    """Pack a float vector into a little-endian binary blob for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _sqlite_error_text(exc: BaseException) -> str:
    return str(exc).lower()


def _is_rebuildable_sqlite_error(exc: BaseException) -> bool:
    """Return True when an SQLite error indicates derived-index corruption."""
    text = _sqlite_error_text(exc)
    return any(
        marker in text
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "malformed database schema",
            "no such module: fts5",
            "no such table: notes",
            "no such table: notes_fts",
            "no such table: notes_vec",
        )
    )


def _metadata_json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class EmbeddedSearchBackend(SearchBackend):
    """Search backend using SQLite FTS5 for BM25 and sqlite-vec for vectors.

    Stores a search.db index file alongside the vault. The index is derived
    from the markdown files and can be rebuilt at any time via reindex().
    """

    def __init__(
        self,
        vault_path: Path | str,
        db_path: Path | str | None = None,
        embedding_provider=None,
        fts5_score_k: float = _DEFAULT_FTS5_SCORE_K,
    ):
        self._vault_path = Path(vault_path)
        if db_path is None:
            db_path = self._vault_path / ".search" / "search.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._indexed: bool = False
        self._provider = embedding_provider
        self._fts5_score_k = fts5_score_k if fts5_score_k and fts5_score_k > 0 else _DEFAULT_FTS5_SCORE_K
        self._vec_available: bool = False
        self._needs_reindex: bool = False
        try:
            self._init_db()
        except sqlite3.DatabaseError as exc:
            if not _is_rebuildable_sqlite_error(exc):
                raise
            logger.warning("Corrupt search.db during initialization, rebuilding: %s", exc)
            self._reset_database_file()
            self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Reload sqlite-vec extension on reconnect
            if self._vec_available:
                try:
                    import sqlite_vec

                    self._conn.enable_load_extension(True)
                    try:
                        sqlite_vec.load(self._conn)
                    finally:
                        self._conn.enable_load_extension(False)
                except (ImportError, AttributeError, sqlite3.OperationalError):
                    pass
        return self._conn

    def _reset_database_file(self) -> None:
        """Close and remove the derived SQLite index file."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        for path in (self._db_path, Path(f"{self._db_path}-wal"), Path(f"{self._db_path}-shm")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._vec_available = False
        self._needs_reindex = True

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
        return row is not None

    def _read_metadata(self, conn: sqlite3.Connection, key: str) -> str | None:
        if not self._table_exists(conn, _METADATA_TABLE):
            return None
        row = conn.execute(f"SELECT value FROM {_METADATA_TABLE} WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _write_metadata(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            f"""
            INSERT INTO {_METADATA_TABLE} (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _embedding_signature(self) -> str | None:
        if self._provider is None:
            return None
        provider_type = type(self._provider)
        metadata: dict[str, object] = {
            "provider_class": f"{provider_type.__module__}.{provider_type.__qualname__}",
            "dimensions": int(self._provider.dimensions()),
            # MEM-127: notes_vec now declares distance_metric=cosine (was
            # unspecified, i.e. sqlite-vec's default L2). Folding this into
            # the signature reuses the existing rebuild-on-mismatch path
            # (see _init_vec below) to migrate any pre-MEM-127 index built
            # under the old L2 table without a separate migration step.
            "distance_metric": _VEC_DISTANCE_METRIC,
        }
        for attr in ("_model", "model", "_api_base", "api_base"):
            value = getattr(self._provider, attr, None)
            if value:
                metadata[attr.lstrip("_")] = str(value)
        return _metadata_json(metadata)

    def _drop_vec_table(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("DROP TABLE IF EXISTS notes_vec")
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Failed to drop stale vector table: %s", exc)

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                path TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {_METADATA_TABLE} (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # FTS5 virtual table for BM25 full-text search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
            USING fts5(path, title, content, content=notes, content_rowid=rowid)
        """)
        # Triggers to keep FTS5 in sync with the notes table
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, path, title, content)
                VALUES (new.rowid, new.path, new.title, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, path, title, content)
                VALUES ('delete', old.rowid, old.path, old.title, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, path, title, content)
                VALUES ('delete', old.rowid, old.path, old.title, old.content);
                INSERT INTO notes_fts(rowid, path, title, content)
                VALUES (new.rowid, new.path, new.title, new.content);
            END
        """)
        conn.commit()
        # sqlite-vec virtual table for vector search
        self._init_vec(conn)

    def _init_vec(self, conn: sqlite3.Connection) -> None:
        """Try to create sqlite-vec virtual table. Gracefully degrades if unavailable."""
        if self._provider is None:
            return
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)

            signature = self._embedding_signature()
            existing_signature = self._read_metadata(conn, _EMBEDDING_SIGNATURE_KEY)
            vec_table_existed = self._table_exists(conn, "notes_vec")
            legacy_or_changed = vec_table_existed and existing_signature != signature
            if legacy_or_changed:
                logger.info("Embedding provider metadata changed; rebuilding vector index")
                self._drop_vec_table(conn)
                self._needs_reindex = True
                vec_table_existed = False
            if not vec_table_existed:
                try:
                    existing_notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                except sqlite3.Error:
                    existing_notes = 0
                if existing_notes:
                    self._needs_reindex = True

            dim = self._provider.dimensions()
            try:
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec
                    USING vec0(path TEXT PRIMARY KEY, embedding float[{dim}] distance_metric={_VEC_DISTANCE_METRIC})
                """)
            except sqlite3.OperationalError as exc:
                if not self._table_exists(conn, "notes_vec"):
                    raise
                logger.warning("Vector table incompatible with embedding provider; recreating: %s", exc)
                self._drop_vec_table(conn)
                conn.execute(f"""
                    CREATE VIRTUAL TABLE notes_vec
                    USING vec0(path TEXT PRIMARY KEY, embedding float[{dim}] distance_metric={_VEC_DISTANCE_METRIC})
                """)
                self._needs_reindex = True
            if signature is not None:
                self._write_metadata(conn, _EMBEDDING_SIGNATURE_KEY, signature)
            conn.commit()
            self._vec_available = True
        except (ImportError, AttributeError, sqlite3.OperationalError) as exc:
            logger.debug("sqlite-vec not available: %s", exc)
            self._vec_available = False

    def is_available(self) -> bool:
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute("SELECT 1 FROM notes LIMIT 1")
                return True
            except (sqlite3.Error, OSError):
                return False

    def _rebuild_index_unlocked(self, collection: str, reason: str) -> bool:
        logger.warning("Rebuilding derived search index: %s", reason)
        self._reset_database_file()
        self._init_db()
        ok = self._reindex_unlocked(collection)
        self._indexed = bool(ok)
        self._needs_reindex = not bool(ok)
        return bool(ok)

    def _repair_failure(self, reason: str) -> RuntimeError:
        return RuntimeError(
            "Search index repair failed after "
            f"{reason}. The index is derived and can be rebuilt safely; run memento_reindex or remove "
            f"{self._db_path} and retry."
        )

    def _ensure_indexed(self) -> None:
        """Auto-index on first search if the database is empty.

        Must be called while holding self._lock.
        """
        if self._indexed and not self._needs_reindex:
            return
        try:
            conn = self._get_conn()
            count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            if self._needs_reindex or count == 0:
                reason = "embedding provider metadata changed" if self._needs_reindex else "empty index"
                if not self._reindex_unlocked("memento"):
                    raise self._repair_failure(reason)
                self._needs_reindex = False
            self._indexed = True
        except sqlite3.DatabaseError as exc:
            if not _is_rebuildable_sqlite_error(exc):
                raise
            if not self._rebuild_index_unlocked("memento", f"sqlite corruption detected ({exc})"):
                raise self._repair_failure(str(exc)) from exc

    def search(
        self,
        query: str,
        collection: str,
        limit: int = 5,
        semantic: bool = False,
        timeout: int = 10,
        min_score: float = 0.0,
        concrete: bool = False,
    ) -> list[dict]:
        if not query or not query.strip():
            return []

        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 5

        with self._lock:
            for attempt in (1, 2):
                try:
                    self._ensure_indexed()

                    if concrete:
                        return self._concrete_search(query, limit, min_score)

                    if semantic and self._vec_available:
                        return self._vec_search(query, limit, min_score)

                    if not semantic and self._vec_available:
                        return self._hybrid_search(query, limit, min_score)

                    # FTS5, with fallback to simple search for short/symbolic tokens (C++, R)
                    results = self._fts5_search(query, limit, min_score)
                    if not results:
                        results = self._simple_search(query, limit, min_score)
                    return results
                except sqlite3.DatabaseError as exc:
                    if attempt == 2 or not _is_rebuildable_sqlite_error(exc):
                        raise
                    if not self._rebuild_index_unlocked(collection, f"sqlite error during search ({exc})"):
                        raise self._repair_failure(str(exc)) from exc
            return []

    def _concrete_search(self, query: str, limit: int, min_score: float) -> list[dict]:
        """Literal substring search over indexed paths, titles, and content."""
        conn = self._get_conn()
        rows = conn.execute("SELECT path, title, content FROM notes").fetchall()
        results = []
        for path, title, content in rows:
            score = _literal_score(query, path, title, content)
            if score <= 0 or score < min_score:
                continue
            results.append(
                {
                    "path": path,
                    "title": title,
                    "score": round(score, 4),
                    "snippet": _literal_snippet(query, content),
                    "backend": "embedded-fts",
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def _fts5_search(self, query: str, limit: int, min_score: float) -> list[dict]:
        """BM25 search via FTS5."""
        conn = self._get_conn()
        # Escape FTS5 special characters and build query
        fts_query = self._build_fts_query(query)
        if not fts_query:
            return []

        try:
            rows = conn.execute(
                """
                SELECT n.path, n.title, n.content,
                       -rank AS score
                FROM notes_fts
                JOIN notes n ON notes_fts.rowid = n.rowid
                WHERE notes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit * 2),  # fetch extra for score filtering
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_rebuildable_sqlite_error(exc):
                raise
            # FTS5 query syntax error — fall back to simple term search
            return self._simple_search(query, limit, min_score)

        if not rows:
            return []

        # MEM-127: bounded transform (score / (score + k)), not a
        # batch-relative rescale - see normalize_fts5_score() for why the
        # old `score / max_score_in_this_batch` approach was a bug (it
        # always forced the top hit in any batch to score exactly 1.0,
        # regardless of true relevance, which made single-hit FTS5 results
        # look artificially confident to the deep-pipeline gate).
        results = []
        for path, title, content, score in rows:
            normalized = round(normalize_fts5_score(score, self._fts5_score_k), 4)
            if normalized < min_score:
                continue
            results.append(
                {
                    "path": path,
                    "title": title,
                    "score": normalized,
                    "snippet": _extract_snippet(content, query),
                    "backend": "embedded-fts",
                }
            )

        return results[:limit]

    def _simple_search(self, query: str, limit: int, min_score: float) -> list[dict]:
        """Fallback substring search when FTS5 query fails."""
        conn = self._get_conn()
        rows = conn.execute("SELECT path, title, content FROM notes").fetchall()
        terms = query.lower().split()
        results = []
        for path, title, content in rows:
            lower = content.lower()
            matched = sum(1 for t in terms if t in lower)
            if matched == 0:
                continue
            score = normalize_grep_term_coverage(matched / len(terms))
            if score < min_score:
                continue
            results.append(
                {
                    "path": path,
                    "title": title,
                    "score": round(score, 4),
                    "snippet": _extract_snippet(content, query),
                    "backend": "embedded-fts",
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def _build_fts_query(self, query: str) -> str:
        """Build an FTS5 query from a natural language query.

        Splits into tokens, escapes special chars, joins with OR for
        broad matching. FTS5 ranks by BM25 automatically.
        """
        # Strip FTS5 operators and special chars
        cleaned = re.sub(r"[^\w\s-]", " ", query)
        tokens = [t.strip() for t in cleaned.split() if t.strip() and len(t.strip()) > 1]
        if not tokens:
            return ""
        # Quote each token to prevent FTS5 syntax issues
        escaped = [f'"{t}"' for t in tokens]
        return " OR ".join(escaped)

    def _vec_search(self, query: str, limit: int, min_score: float) -> list[dict]:
        """Vector similarity search via sqlite-vec."""
        if not self._provider or not self._vec_available:
            return self._fts5_search(query, limit, min_score)

        try:
            query_vec = self._provider.embed_query(query)
            query_blob = _vec_to_blob(query_vec)
        except Exception as exc:
            logger.warning("Embedding query failed: %s", exc)
            return self._fts5_search(query, limit, min_score)

        conn = self._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT v.path, v.distance, n.title, n.content
                FROM notes_vec v
                JOIN notes n ON v.path = n.path
                WHERE v.embedding MATCH ?
                    AND k = ?
                ORDER BY v.distance
                """,
                (query_blob, limit * 2),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_rebuildable_sqlite_error(exc):
                raise
            logger.warning("Vec search failed: %s", exc)
            return self._fts5_search(query, limit, min_score)

        if not rows:
            return []

        # MEM-127: notes_vec is declared distance_metric=cosine, so `distance`
        # here is cosine distance (1 - cosine_similarity) in [0, 2], magnitude
        # -independent regardless of whether the embedding provider
        # normalizes its vectors. See normalize_vec_cosine_distance() for why
        # the old `max(0.0, 1.0 - distance)` (assuming L2 distance on unit
        # vectors) collapsed orthogonal and opposite vectors to the same
        # score of 0.
        results = []
        for path, distance, title, content in rows:
            score = round(normalize_vec_cosine_distance(distance), 4)
            if score < min_score:
                continue
            results.append(
                {
                    "path": path,
                    "title": title,
                    "score": score,
                    "snippet": _extract_snippet(content, query),
                    "backend": "embedded-vec",
                }
            )

        return results[:limit]

    def _hybrid_search(self, query: str, limit: int, min_score: float) -> list[dict]:
        """RRF fusion of FTS5 BM25 + vector search.

        RRF's rank-based score is purely positional: a document that is the
        only candidate in both the FTS5 and vector lists always ranks #1 in
        each, so rrf_score/max_rrf normalizes to 1.0 regardless of how weak
        that document's actual score was in either backend (MEM-143). Both
        `_fts5_search` and `_vec_search` already normalize their own scores
        to [0, 1] (MEM-127), so the fused score is capped at the document's
        best underlying normalized score: `fused = rrf_normalized *
        best_quality`. Rank still decides ORDERING; it just can no longer
        manufacture quality above what the underlying backends measured.
        """
        fts_results = self._fts5_search(query, limit * 2, 0.0)
        vec_results = self._vec_search(query, limit * 2, 0.0)

        if not fts_results and not vec_results:
            return []
        if not vec_results:
            return [r for r in fts_results if r["score"] >= min_score][:limit]
        if not fts_results:
            return [r for r in vec_results if r["score"] >= min_score][:limit]

        # Reciprocal Rank Fusion (k=60)
        k = 60
        scores: dict[str, float] = {}
        metadata: dict[str, dict] = {}
        best_quality: dict[str, float] = {}

        for rank, r in enumerate(fts_results):
            path = r["path"]
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank + 1)
            metadata[path] = r
            best_quality[path] = max(best_quality.get(path, 0.0), float(r.get("score", 0) or 0))

        for rank, r in enumerate(vec_results):
            path = r["path"]
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank + 1)
            if path not in metadata:
                metadata[path] = r
            best_quality[path] = max(best_quality.get(path, 0.0), float(r.get("score", 0) or 0))

        # Normalize to 0-1
        max_rrf = max(scores.values()) if scores else 1.0
        if max_rrf <= 0:
            max_rrf = 1.0

        results = []
        for path, rrf_score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            rrf_normalized = rrf_score / max_rrf
            normalized = rrf_normalized * best_quality.get(path, 0.0)
            if normalized < min_score:
                continue
            entry = metadata[path].copy()
            entry["score"] = round(normalized, 4)
            results.append(entry)

        return results[:limit]

    def get(self, path: str, collection: str | None = None, timeout: int = 5) -> dict | None:
        if not _is_within_vault(self._vault_path / path, self._vault_path):
            return None

        with self._lock:
            self._ensure_indexed()
            conn = self._get_conn()
            row = conn.execute("SELECT path, title, content FROM notes WHERE path = ?", (path,)).fetchone()
            if row is None:
                return None
            return {
                "path": row[0],
                "title": row[1],
                "content": row[2],
                "score": 0.0,
            }

    def vector_dimensions(self) -> int | None:
        """Return the embedding dimensionality backing this index, or None.

        Prefers the live provider's own metadata (see ``_embedding_signature``,
        MEM-46) so callers never have to hardcode a dimension. Falls back to
        the persisted ``index_metadata`` signature so dimensionality is still
        known when inspecting an index without a live provider attached
        (e.g. a QMD-less install being read by an external process).
        Returns None if no vector index has been built yet.
        """
        if self._provider is not None:
            try:
                return int(self._provider.dimensions())
            except Exception:
                pass
        with self._lock:
            conn = self._get_conn()
            raw = self._read_metadata(conn, _EMBEDDING_SIGNATURE_KEY)
        if not raw:
            return None
        try:
            dims = json.loads(raw).get("dimensions")
        except (ValueError, AttributeError, json.JSONDecodeError):
            return None
        return int(dims) if dims is not None else None

    def get_note_vectors(self, paths: list[str] | None = None) -> dict[str, list[float]]:
        """Read stored per-note embedding vectors from ``notes_vec``.

        Unlike QMD (which chunks documents and requires mean-pooling), this
        backend stores exactly one whole-document vector per note keyed by
        its vault-relative path (e.g. ``"notes/foo.md"``) -- no pooling
        needed. Returns {} if vector search isn't available (sqlite-vec
        missing, no embedding provider configured, or nothing indexed yet).

        Returns plain float lists rather than numpy arrays so this module
        can stay numpy-free; callers that want arrays should wrap the result.
        """
        if not self._vec_available:
            return {}
        with self._lock:
            conn = self._get_conn()
            try:
                if paths is not None:
                    if not paths:
                        return {}
                    placeholders = ",".join("?" * len(paths))
                    rows = conn.execute(
                        f"SELECT path, embedding FROM notes_vec WHERE path IN ({placeholders})",
                        list(paths),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT path, embedding FROM notes_vec").fetchall()
            except sqlite3.Error as exc:
                logger.warning("Failed to read note vectors: %s", exc)
                return {}

        result: dict[str, list[float]] = {}
        for path, blob in rows:
            if not blob:
                continue
            count = len(blob) // 4
            result[path] = list(struct.unpack(f"<{count}f", blob))
        return result

    def reindex(self, collection: str, embed: bool = True) -> bool:
        """Rebuild the search index from all markdown files in the vault."""
        with self._lock:
            return self._reindex_unlocked(collection, embed)

    def _reindex_unlocked(self, collection: str, embed: bool = True) -> bool:
        try:
            conn = self._get_conn()
            search_dirs = [
                self._vault_path / d for d in ("notes", "fleeting", "projects") if (self._vault_path / d).exists()
            ]

            indexed_paths = set()
            notes_for_embedding: list[tuple[str, str]] = []  # (path, content)

            for search_dir in search_dirs:
                for md_file in search_dir.rglob("*.md"):
                    if md_file.is_symlink():
                        continue
                    if not _is_within_vault(md_file, self._vault_path):
                        continue

                    rel_path = str(md_file.relative_to(self._vault_path))
                    content = md_file.read_text(errors="replace")
                    title = _extract_title(content, md_file.stem)
                    mtime = md_file.stat().st_mtime

                    conn.execute(
                        """
                        INSERT INTO notes (path, title, content, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            title = excluded.title,
                            content = excluded.content,
                            updated_at = excluded.updated_at
                        """,
                        (rel_path, title, content, mtime),
                    )
                    indexed_paths.add(rel_path)
                    notes_for_embedding.append((rel_path, content))

            # Remove notes that no longer exist on disk
            existing = {r[0] for r in conn.execute("SELECT path FROM notes").fetchall()}
            for stale_path in existing - indexed_paths:
                conn.execute("DELETE FROM notes WHERE path = ?", (stale_path,))
                if self._vec_available:
                    try:
                        conn.execute("DELETE FROM notes_vec WHERE path = ?", (stale_path,))
                    except sqlite3.OperationalError:
                        pass

            conn.commit()

            # Batch embed all notes
            if embed and self._vec_available and self._provider and notes_for_embedding:
                self._batch_embed(conn, notes_for_embedding)

            self._needs_reindex = False
            return True

        except (sqlite3.Error, OSError):
            return False

    def _batch_embed(self, conn: sqlite3.Connection, notes: list[tuple[str, str]]) -> None:
        """Embed notes in bounded chunks and upsert into notes_vec."""
        # Truncate oversized notes for embedding (full text stays in FTS5)
        truncated = [(path, content[:_MAX_NOTE_SIZE_FOR_EMBED]) for path, content in notes]

        for i in range(0, len(truncated), _MAX_EMBED_BATCH):
            chunk = truncated[i : i + _MAX_EMBED_BATCH]
            try:
                texts = [content for _, content in chunk]
                vectors = self._provider.embed(texts)

                if len(vectors) != len(chunk):
                    logger.warning(
                        "Embedding returned %d vectors for %d texts, skipping chunk", len(vectors), len(chunk)
                    )
                    continue

                conn.execute("SAVEPOINT embed_chunk")
                for (path, _), vec in zip(chunk, vectors):
                    blob = _vec_to_blob(vec)
                    conn.execute("DELETE FROM notes_vec WHERE path = ?", (path,))
                    conn.execute(
                        "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                        (path, blob),
                    )
                conn.execute("RELEASE embed_chunk")
            except Exception as exc:
                logger.warning("Batch embedding chunk %d failed: %s", i, exc)
                try:
                    conn.execute("ROLLBACK TO embed_chunk")
                    conn.execute("RELEASE embed_chunk")
                except sqlite3.Error:
                    pass

    def index_note(self, rel_path: str) -> bool:
        """Index or update a single note by its vault-relative path."""
        with self._lock:
            self._ensure_indexed()
            return self._index_note_unlocked(rel_path)

    def _index_note_unlocked(self, rel_path: str) -> bool:
        try:
            full_path = self._vault_path / rel_path
            if not full_path.exists():
                return False

            if not _is_within_vault(full_path, self._vault_path):
                return False

            # Canonicalize to prevent duplicate/non-canonical keys
            rel_path = str(full_path.resolve().relative_to(self._vault_path.resolve()))

            content = full_path.read_text(errors="replace")
            title = _extract_title(content, full_path.stem)
            mtime = full_path.stat().st_mtime

            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO notes (path, title, content, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (rel_path, title, content, mtime),
            )

            # Embed vector if provider available
            if self._vec_available and self._provider:
                try:
                    vec = self._provider.embed([content])[0]
                    blob = _vec_to_blob(vec)
                    conn.execute("DELETE FROM notes_vec WHERE path = ?", (rel_path,))
                    conn.execute(
                        "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                        (rel_path, blob),
                    )
                except Exception as exc:
                    logger.warning("Embedding note %s failed: %s", rel_path, exc)

            conn.commit()
            return True

        except (sqlite3.Error, OSError):
            return False

    def repair_index(self, collection: str = "memento") -> dict[str, object]:
        """Repair stale or corrupt derived index state.

        Uses the incremental indexer for normal stale/missing files and falls
        back to a full derived-index rebuild when SQLite corruption is found.
        """
        with self._lock:
            try:
                self._ensure_indexed()
                from memento.indexer import scan_and_index

                stats = scan_and_index(self._vault_path, self)
                self._indexed = True
                return {"ok": True, "mode": "incremental", "stats": stats}
            except sqlite3.DatabaseError as exc:
                if not _is_rebuildable_sqlite_error(exc):
                    raise
                ok = self._rebuild_index_unlocked(collection, f"sqlite error during repair ({exc})")
                return {"ok": ok, "mode": "rebuild", "reason": str(exc)}

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def index_staleness(self, vault: Path, config: dict) -> dict:
        """Return index staleness metadata using content-based lag (WAL-robust).

        Uses SQL ``SELECT MAX(updated_at) FROM notes`` to compare against the
        newest on-disk note mtime. Falls back to file mtime when the notes
        table is empty or unavailable.
        """
        db_path = self._db_path
        metadata: dict = {
            "checked": True,
            "backend": "embedded",
            "db_path": str(db_path),
        }

        if not db_path.exists():
            return {
                **metadata,
                "checked": False,
                "reason": "embedded_index_missing",
                "stale": None,
                "status": "pass",
            }

        # Get newest note mtime from disk (filesystem walk stays outside the
        # lock so writers are not serialized behind it).
        newest_note = newest_note_mtime(vault)
        if newest_note is None:
            return {
                **metadata,
                "reason": "no_notes",
                "stale": False,
                "status": "pass",
            }

        # Prefer content-based updated_at from SQL (WAL-robust). Guard the
        # shared sqlite3.Connection with self._lock, matching every other
        # method on this class, so the read cannot race the debounced
        # reindex()/index_note() writers on the same connection.
        db_max_updated = None
        try:
            with self._lock:
                conn = self._get_conn()
                row = conn.execute("SELECT MAX(updated_at) FROM notes").fetchone()
            db_max_updated = row[0] if row and row[0] is not None else None
        except sqlite3.Error:
            pass

        # Always read db file mtime for backward-compat metadata
        try:
            db_mtime_val = db_path.stat().st_mtime
        except OSError as exc:
            return {
                **metadata,
                "checked": False,
                "reason": type(exc).__name__,
                "error": str(exc),
                "stale": None,
                "status": "warn",
            }

        if db_max_updated is not None:
            lag_seconds = int(max(0, newest_note - db_max_updated))
        else:
            # File-mtime fallback when SQL data is unavailable
            lag_seconds = int(max(0, newest_note - db_mtime_val))

        result: dict = {
            **metadata,
            "stale": lag_seconds > STALE_INDEX_WARN_SECONDS,
            "lag_seconds": lag_seconds,
            "db_mtime": datetime.fromtimestamp(db_mtime_val).isoformat(timespec="seconds"),
            "newest_note_mtime": datetime.fromtimestamp(newest_note).isoformat(timespec="seconds"),
            "status": classify_index_lag(lag_seconds),
        }
        if db_max_updated is not None:
            result["db_max_updated"] = datetime.fromtimestamp(db_max_updated).isoformat(timespec="seconds")
        return result
