"""Embedded search backend — SQLite FTS5 + sqlite-vec.

Provides full-text and vector search without external dependencies like QMD.
Uses a single search.db file stored alongside the vault. The markdown files
remain the source of truth; the database is a derived, disposable index.
"""

import logging
import re
import sqlite3
import struct
import threading
from pathlib import Path

from memento.search_backend import SearchBackend

logger = logging.getLogger(__name__)

_MAX_EMBED_BATCH = 64
_MAX_NOTE_SIZE_FOR_EMBED = 100_000  # 100KB


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


class EmbeddedSearchBackend(SearchBackend):
    """Search backend using SQLite FTS5 for BM25 and sqlite-vec for vectors.

    Stores a search.db index file alongside the vault. The index is derived
    from the markdown files and can be rebuilt at any time via reindex().
    """

    def __init__(self, vault_path: Path | str, db_path: Path | str | None = None, embedding_provider=None):
        self._vault_path = Path(vault_path)
        if db_path is None:
            db_path = self._vault_path / ".search" / "search.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._indexed: bool = False
        self._provider = embedding_provider
        self._vec_available: bool = False
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
                except (ImportError, sqlite3.OperationalError):
                    pass
        return self._conn

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
            dim = self._provider.dimensions()
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec
                USING vec0(path TEXT PRIMARY KEY, embedding float[{dim}])
            """)
            conn.commit()
            self._vec_available = True
        except (ImportError, sqlite3.OperationalError) as exc:
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

    def _ensure_indexed(self) -> None:
        """Auto-index on first search if the database is empty.

        Must be called while holding self._lock.
        """
        if self._indexed:
            return
        try:
            conn = self._get_conn()
            count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            if count == 0:
                self._reindex_unlocked("memento")
            self._indexed = True
        except sqlite3.DatabaseError as exc:
            logger.warning("Corrupt search.db, rebuilding: %s", exc)
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            try:
                self._db_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._init_db()
            self._reindex_unlocked("memento")
            self._indexed = True

    def search(
        self,
        query: str,
        collection: str,
        limit: int = 5,
        semantic: bool = False,
        timeout: int = 10,
        min_score: float = 0.0,
    ) -> list[dict]:
        if not query or not query.strip():
            return []

        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 5

        with self._lock:
            self._ensure_indexed()

            if semantic and self._vec_available:
                return self._vec_search(query, limit, min_score)

            if not semantic and self._vec_available:
                return self._hybrid_search(query, limit, min_score)

            # FTS5, with fallback to simple search for short/symbolic tokens (C++, R)
            results = self._fts5_search(query, limit, min_score)
            if not results:
                results = self._simple_search(query, limit, min_score)
            return results

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
        except sqlite3.OperationalError:
            # FTS5 query syntax error — fall back to simple term search
            return self._simple_search(query, limit, min_score)

        if not rows:
            return []

        # Normalize scores to 0-1 range
        max_score = max(r[3] for r in rows) if rows else 1.0
        if max_score <= 0:
            max_score = 1.0

        results = []
        for path, title, content, score in rows:
            normalized = score / max_score
            if normalized < min_score:
                continue
            results.append({
                "path": path,
                "title": title,
                "score": round(normalized, 4),
                "snippet": _extract_snippet(content, query),
            })

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
            score = matched / len(terms)
            if score < min_score:
                continue
            results.append({
                "path": path,
                "title": title,
                "score": round(score, 4),
                "snippet": _extract_snippet(content, query),
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def _build_fts_query(self, query: str) -> str:
        """Build an FTS5 query from a natural language query.

        Splits into tokens, escapes special chars, joins with OR for
        broad matching. FTS5 ranks by BM25 automatically.
        """
        # Strip FTS5 operators and special chars
        cleaned = re.sub(r'[^\w\s-]', ' ', query)
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
            logger.warning("Vec search failed: %s", exc)
            return self._fts5_search(query, limit, min_score)

        if not rows:
            return []

        # Convert cosine distance to similarity score (0-1)
        results = []
        for path, distance, title, content in rows:
            score = max(0.0, 1.0 - distance)
            if score < min_score:
                continue
            results.append({
                "path": path,
                "title": title,
                "score": round(score, 4),
                "snippet": _extract_snippet(content, query),
            })

        return results[:limit]

    def _hybrid_search(self, query: str, limit: int, min_score: float) -> list[dict]:
        """RRF fusion of FTS5 BM25 + vector search."""
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

        for rank, r in enumerate(fts_results):
            path = r["path"]
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank + 1)
            metadata[path] = r

        for rank, r in enumerate(vec_results):
            path = r["path"]
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank + 1)
            if path not in metadata:
                metadata[path] = r

        # Normalize to 0-1
        max_rrf = max(scores.values()) if scores else 1.0
        if max_rrf <= 0:
            max_rrf = 1.0

        results = []
        for path, rrf_score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            normalized = rrf_score / max_rrf
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
            row = conn.execute(
                "SELECT path, title, content FROM notes WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                return None
            return {
                "path": row[0],
                "title": row[1],
                "content": row[2],
                "score": 0.0,
            }

    def reindex(self, collection: str, embed: bool = True) -> bool:
        """Rebuild the search index from all markdown files in the vault."""
        with self._lock:
            return self._reindex_unlocked(collection, embed)

    def _reindex_unlocked(self, collection: str, embed: bool = True) -> bool:
        try:
            conn = self._get_conn()
            search_dirs = [
                self._vault_path / d
                for d in ("notes", "fleeting", "projects")
                if (self._vault_path / d).exists()
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

            return True

        except (sqlite3.Error, OSError):
            return False

    def _batch_embed(self, conn: sqlite3.Connection, notes: list[tuple[str, str]]) -> None:
        """Embed notes in bounded chunks and upsert into notes_vec."""
        # Truncate oversized notes for embedding (full text stays in FTS5)
        truncated = [
            (path, content[:_MAX_NOTE_SIZE_FOR_EMBED])
            for path, content in notes
        ]

        for i in range(0, len(truncated), _MAX_EMBED_BATCH):
            chunk = truncated[i:i + _MAX_EMBED_BATCH]
            try:
                texts = [content for _, content in chunk]
                vectors = self._provider.embed(texts)

                if len(vectors) != len(chunk):
                    logger.warning("Embedding returned %d vectors for %d texts, skipping chunk", len(vectors), len(chunk))
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

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
