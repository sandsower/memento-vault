"""Search backend abstraction — decouples vault search from QMD CLI.

Provides a SearchBackend protocol and a QMDBackend implementation that
wraps the QMD CLI subprocess calls. Other backends (e.g., Meilisearch,
SQLite FTS, Tantivy) can be added by implementing the same interface.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

# Index staleness thresholds (seconds). Canonical source shared by every
# backend and imported by health.py, so the pass/warn/fail boundaries stay
# consistent if they are ever tuned.
STALE_INDEX_WARN_SECONDS = 60
STALE_INDEX_FAIL_SECONDS = 3600

_STALENESS_SCAN_DIRS = ("notes", "fleeting", "projects")


def newest_note_mtime(vault: Path) -> float | None:
    """Return the newest note mtime across the vault, or None when empty."""
    newest: float | None = None
    for dirname in _STALENESS_SCAN_DIRS:
        root = vault / dirname
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    return newest


def classify_index_lag(lag_seconds: int) -> str:
    """Map an index lag (seconds) to a health status string."""
    if lag_seconds > STALE_INDEX_FAIL_SECONDS:
        return "fail"
    if lag_seconds > STALE_INDEX_WARN_SECONDS:
        return "warn"
    return "pass"


def _clamp01(value: float) -> float:
    """Clamp a float to [0, 1], guarding NaN/None/non-numeric input."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    return max(0.0, min(1.0, score))


def normalize_qmd_score(raw_score: float) -> float:
    """Clamp the QMD CLI's own relevance score to [0, 1] (MEM-127).

    QMD is an external binary we do not control the internals of. Observed
    production scores (docs/quality-analysis-2026-07-02.md) already sit in a
    bounded band: BM25 hits commonly land 0.9-0.98, semantic (vsearch) hits
    0.5-0.7. Unlike FTS5's unbounded BM25 rank or sqlite-vec's raw distance,
    QMD's own scale doesn't need a rescale to reach [0, 1] - only a defensive
    clamp against an out-of-range or malformed value from the subprocess.
    """
    return _clamp01(raw_score)


def normalize_grep_term_coverage(raw_score: float) -> float:
    """Clamp the grep backend's term-coverage fraction to [0, 1] (MEM-127).

    Already bounded by construction (matched_terms / total_terms), so this
    is a defensive clamp rather than a rescale - kept as a named function so
    every backend has an explicit, unit-testable normalization boundary.
    """
    return _clamp01(raw_score)


def _literal_terms(query: str) -> tuple[str, list[str]]:
    literal = (query or "").strip()
    quoted = re.search(r'"(.+?)"', literal) or re.search(r"(?<!\w)'(.+?)'(?!\w)", literal)
    if quoted:
        literal = quoted.group(1).strip()
    elif len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in "'\"":
        literal = literal[1:-1].strip()
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_.:/-]+", literal) if t]
    return literal, terms


def _frontmatter_title(content: str, fallback: str) -> str:
    for line in content.splitlines()[:10]:
        stripped = line.strip()
        if stripped.lower().startswith("title:"):
            return stripped[6:].strip().strip("\"'")
    return fallback


def _has_identifier_boundary_match(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])"
    return re.search(pattern, haystack) is not None


def _literal_score(query: str, path: str, title: str, content: str) -> float:
    literal, terms = _literal_terms(query)
    literal_lower = literal.lower()
    path_lower = path.lower()
    title_lower = title.lower()
    content_lower = content.lower()

    if literal_lower:
        if literal_lower == path_lower or literal_lower == Path(path_lower).stem:
            return 1.0
        if literal_lower in path_lower:
            return 0.98
        if literal_lower == title_lower:
            return 0.96
        if _has_identifier_boundary_match(title_lower, literal_lower):
            return 0.94
        if literal_lower in title_lower:
            return 0.91
        if _has_identifier_boundary_match(content_lower, literal_lower):
            return 0.93
        if literal_lower in content_lower:
            return 0.9

    if terms:
        haystack = f"{path_lower}\n{title_lower}\n{content_lower}"
        matched = sum(1 for term in terms if term in haystack)
        if matched == len(terms):
            return 0.7
    return 0.0


def _literal_snippet(query: str, content: str) -> str:
    literal, terms = _literal_terms(query)
    needles = [literal.lower()] if literal else []
    needles.extend(terms)
    for line in content.splitlines():
        lower = line.lower()
        if any(needle and needle in lower for needle in needles):
            return line.strip()[:200]
    return _clean_snippet(content)


def _literal_file_search(
    vault: Path, query: str, limit: int, timeout: int = 10, min_score: float = 0.0, backend: str = "grep"
) -> list[dict]:
    """Literal substring search over vault markdown files."""
    if not query or not query.strip() or not vault.exists():
        return []

    import time

    deadline = time.monotonic() + timeout
    search_dirs = [vault / d for d in ("notes", "fleeting", "projects") if (vault / d).exists()]
    vault_resolved = vault.resolve()
    md_files: list[Path] = []
    for directory in search_dirs:
        for md_file in directory.rglob("*.md"):
            if md_file.is_symlink():
                continue
            resolved = md_file.resolve()
            if resolved != vault_resolved and vault_resolved not in resolved.parents:
                continue
            md_files.append(md_file)
    md_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    results = []
    for md_file in md_files:
        if time.monotonic() >= deadline:
            break
        try:
            content = md_file.read_text(errors="replace")
        except OSError:
            continue
        rel_path = str(md_file.relative_to(vault))
        title = _frontmatter_title(content, md_file.stem)
        score = _literal_score(query, rel_path, title, content)
        if score <= 0 or score < min_score:
            continue
        results.append(
            {
                "path": rel_path,
                "title": title,
                "score": round(score, 4),
                "snippet": _literal_snippet(query, content),
                "backend": backend,
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


class SearchBackend(ABC):
    """Abstract search backend for vault note retrieval."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this backend is ready to serve queries."""
        ...

    @abstractmethod
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
        """Search for notes matching a query.

        Returns list of dicts with keys: path, title, score, snippet, backend.
        ``score`` is normalized to [0, 1], monotonic in the backend's native
        relevance ordering (MEM-127) - callers may compare scores across
        backends as a coarse signal, though the normalization is per-backend
        and not guaranteed to carry identical meaning at every point in the
        range (see memento.search_backend / memento.embedded_search
        normalize_* functions for each backend's mapping). ``backend`` is one
        of "qmd", "embedded-fts", "embedded-vec", or "grep".
        """
        ...

    @abstractmethod
    def get(self, path: str, collection: str | None = None, timeout: int = 5) -> dict | None:
        """Fetch a single note by path.

        Returns dict with path, title, content, score keys, or None.
        """
        ...

    def index_note(self, rel_path: str, collection: str | None = None) -> bool:
        """Index a single vault-relative note after an official write.

        Backends that cannot update one note may conservatively mark/update the
        whole collection. The write path treats failures as non-blocking, but
        official writers should call this hook so MCP, CLI, and hookless users
        see fresh notes without needing a manual reindex.
        """
        from memento.config import get_config

        return self.reindex(collection or get_config().get("qmd_collection", "memento"), embed=False)

    @abstractmethod
    def reindex(self, collection: str, embed: bool = True) -> bool:
        """Trigger reindexing of the collection.

        Returns True if reindexing was initiated successfully.
        """
        ...

    def index_staleness(self, vault: Path, config: dict) -> dict:
        """Return index staleness metadata for the active backend.

        Subclasses should override this to provide backend-specific staleness
        detection. The default implementation returns a no-index pass.

        Returns dict with keys:
            checked (bool): Whether an index was found to check.
            backend (str): Backend name.
            stale (bool | None): Whether the index is stale (None if
                not applicable).
            status (str): "pass", "warn", or "fail".
            reason (str): Optional early-return reason.
        """
        return {"checked": False, "reason": "backend_no_index", "stale": False, "status": "pass"}


class QMDBackend(SearchBackend):
    """Search backend that wraps the QMD CLI tool."""

    def is_available(self) -> bool:
        if not shutil.which("qmd"):
            return False
        # Verify the configured collection actually exists
        from memento.config import get_config

        collection = get_config().get("qmd_collection", "memento")
        try:
            result = subprocess.run(
                ["qmd", "search", "test", "-c", collection, "-n", "1"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

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

        if concrete:
            from memento.config import get_vault

            return _literal_file_search(get_vault(), query, limit, timeout=timeout, min_score=min_score, backend="qmd")

        if not self.is_available():
            return []

        cmd_name = "vsearch" if semantic else "search"
        cmd = ["qmd", cmd_name, query, "-c", collection, "-n", str(limit), "--json"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                return []

            stdout = result.stdout
            json_start = stdout.find("[")
            if json_start == -1:
                json_start = stdout.find("{")
            if json_start == -1:
                return []
            data = json.loads(stdout[json_start:])
            results = []

            items = data if isinstance(data, list) else data.get("results", [])
            for item in items:
                score = normalize_qmd_score(item.get("score", 0.0))
                if score < min_score:
                    continue

                raw_path = item.get("file", item.get("path", ""))
                if "://" in raw_path:
                    raw_path = raw_path.split("://", 1)[1]
                    parts = raw_path.split("/", 1)
                    if len(parts) > 1:
                        raw_path = parts[1]
                file_title = Path(raw_path).stem
                qmd_title = item.get("title", "")
                if qmd_title and qmd_title not in ("Related", "Notes", "Sessions", ""):
                    title = qmd_title
                else:
                    title = file_title

                results.append(
                    {
                        "path": raw_path,
                        "title": title,
                        "score": score,
                        "snippet": _clean_snippet(item.get("snippet", item.get("content", ""))),
                        "backend": "qmd",
                    }
                )

            return results[:limit]

        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return []
        except Exception:
            return []

    def get(self, path: str, collection: str | None = None, timeout: int = 5) -> dict | None:
        if not self.is_available():
            return None

        from memento.config import get_config

        collection = collection or get_config().get("qmd_collection", "memento")
        cmd = ["qmd", "get", path, "-c", collection, "--json"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                return None

            stdout = result.stdout
            json_start = stdout.find("{")
            if json_start == -1:
                return None

            data = json.loads(stdout[json_start:])
            raw_path = data.get("file", data.get("path", path))
            if "://" in raw_path:
                raw_path = raw_path.split("://", 1)[1]
                parts = raw_path.split("/", 1)
                if len(parts) > 1:
                    raw_path = parts[1]

            return {
                "path": raw_path,
                "title": data.get("title", Path(raw_path).stem),
                "content": data.get("content", ""),
                "score": 0.0,
            }

        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return None
        except Exception:
            return None

    def reindex(self, collection: str, embed: bool = True) -> bool:
        if not self.is_available():
            return False

        try:
            result = subprocess.run(
                ["qmd", "update", "-c", collection],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return False

            if embed:
                subprocess.run(
                    ["qmd", "embed"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

            return True
        except (subprocess.TimeoutExpired, OSError):
            return False

    @staticmethod
    def _resolve_index_path(vault: Path) -> Path:
        """Resolve the active QMD SQLite index path.

        Resolution order mirrors qmd itself so configured setups are not
        misreported as ``qmd_index_unresolved``:

        1. ``INDEX_PATH`` env override (explicit, unambiguous).
        2. Project-local ``<vault>/.qmd/index.sqlite`` created by ``qmd init``.
        3. Global ``$XDG_CACHE_HOME/qmd/index.sqlite`` (default ``~/.cache``).
        """
        override = os.environ.get("INDEX_PATH")
        if override:
            return Path(override).expanduser()
        local = vault / ".qmd" / "index.sqlite"
        if local.exists():
            return local
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return cache_home / "qmd" / "index.sqlite"

    def index_staleness(self, vault: Path, config: dict) -> dict:
        """Compare resolved QMD index mtime vs newest note mtime."""
        qmd_index = self._resolve_index_path(vault)

        if not qmd_index.exists():
            return {
                "checked": False,
                "backend": "qmd",
                "reason": "qmd_index_unresolved",
                "stale": False,
                "status": "pass",
            }

        newest = newest_note_mtime(vault)
        if newest is None:
            return {
                "checked": True,
                "backend": "qmd",
                "db_path": str(qmd_index),
                "reason": "no_notes",
                "stale": False,
                "status": "pass",
            }

        try:
            db_mtime = qmd_index.stat().st_mtime
        except OSError as exc:
            return {
                "checked": False,
                "backend": "qmd",
                "reason": type(exc).__name__,
                "stale": None,
                "status": "warn",
            }

        lag_seconds = int(max(0, newest - db_mtime))
        return {
            "checked": True,
            "backend": "qmd",
            "db_path": str(qmd_index),
            "db_mtime": datetime.fromtimestamp(db_mtime).isoformat(timespec="seconds"),
            "newest_note_mtime": datetime.fromtimestamp(newest).isoformat(timespec="seconds"),
            "lag_seconds": lag_seconds,
            "stale": lag_seconds > STALE_INDEX_WARN_SECONDS,
            "status": classify_index_lag(lag_seconds),
        }


class GrepBackend(SearchBackend):
    """Simple grep-based fallback search for when QMD is not available.

    Searches vault markdown files using substring matching. Does not support
    semantic search but provides basic keyword search out of the box with
    no external dependencies or indexing pipeline.
    """

    def is_available(self) -> bool:
        from memento.config import get_vault

        vault = get_vault()
        return vault.exists() and any((vault / d).exists() for d in ("notes", "fleeting", "projects"))

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

        import time

        from memento.config import get_vault

        vault = get_vault()
        if concrete:
            return _literal_file_search(vault, query, limit, timeout=timeout, min_score=min_score, backend="grep")
        if not vault.exists():
            return []

        deadline = time.monotonic() + timeout

        # Search notes/, fleeting/, and projects/ for full coverage
        search_dirs = [vault / d for d in ("notes", "fleeting", "projects") if (vault / d).exists()]
        if not search_dirs:
            return []

        vault_resolved = vault.resolve()
        md_files = []
        for d in search_dirs:
            for f in d.rglob("*.md"):
                # Skip symlinks and paths that resolve outside the vault
                if f.is_symlink():
                    continue
                resolved = f.resolve()
                if resolved != vault_resolved and vault_resolved not in resolved.parents:
                    continue
                md_files.append(f)
        md_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        query_lower = query.lower()
        terms = query_lower.split()
        results = []
        perfect_count = 0  # track how many perfect-score results we have

        for md_file in md_files:
            # Enforce timeout — return best results found so far
            if time.monotonic() >= deadline:
                break

            try:
                content = md_file.read_text(errors="replace")
            except OSError:
                continue

            content_lower = content.lower()
            # Score: fraction of query terms found in the file
            matched = sum(1 for t in terms if t in content_lower)
            if matched == 0:
                continue

            score = normalize_grep_term_coverage(matched / len(terms))
            if score < min_score:
                continue

            # Extract title from frontmatter or filename
            title = md_file.stem
            for line in content.splitlines()[:10]:
                stripped = line.strip()
                if stripped.lower().startswith("title:"):
                    title = stripped[6:].strip().strip("\"'")
                    break

            # Build snippet from first matching line
            snippet = ""
            for line in content.splitlines():
                if any(t in line.lower() for t in terms):
                    snippet = line.strip()[:200]
                    break

            rel_path = str(md_file.relative_to(vault))
            results.append({"path": rel_path, "title": title, "score": score, "snippet": snippet, "backend": "grep"})

            if score >= 1.0:
                perfect_count += 1
                # Early exit: enough perfect matches to fill the limit
                if perfect_count >= limit:
                    break

        # Sort by score descending, then recency (already sorted by mtime)
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def get(self, path: str, collection: str | None = None, timeout: int = 5) -> dict | None:
        from memento.config import get_vault

        vault = get_vault()
        full_path = (vault / path).resolve()
        vault_resolved = vault.resolve()
        if full_path != vault_resolved and vault_resolved not in full_path.parents:
            return None
        if not full_path.exists():
            return None

        try:
            content = full_path.read_text(errors="replace")
        except OSError:
            return None

        title = full_path.stem
        for line in content.splitlines()[:10]:
            stripped = line.strip()
            if stripped.lower().startswith("title:"):
                title = stripped[6:].strip().strip("\"'")
                break

        return {"path": path, "title": title, "content": content, "score": 0.0}

    def index_note(self, rel_path: str, collection: str | None = None) -> bool:
        # Grep backend has no index to update; the file is immediately visible.
        return True

    def reindex(self, collection: str, embed: bool = True) -> bool:
        # Grep backend has no index to update
        return True

    def index_staleness(self, vault: Path, config: dict) -> dict:
        """Grep backend has no index to check."""
        return {"checked": False, "reason": "no_index", "stale": False, "status": "pass"}


def _clean_snippet(raw):
    """Clean QMD snippet: strip chunk markers, frontmatter, and collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"@@ [^@]+ @@\s*\([^)]*\)\s*", "", raw)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---" or (": " in stripped and not stripped.startswith("-")):
            continue
        if stripped:
            lines.append(stripped)
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


# --- Singleton backend ---

_backend: SearchBackend | None = None


def get_backend() -> SearchBackend:
    """Get the configured search backend (singleton).

    Detection order (search_backend: auto):
        QMD → Embedded → Grep

    Config override: search_backend: qmd | embedded | grep
    """
    global _backend
    if _backend is None:
        from memento.config import get_config

        config = get_config()
        choice = config.get("search_backend", "auto")

        if choice == "embedded":
            _backend = _make_embedded(config) or GrepBackend()
        elif choice == "grep":
            _backend = GrepBackend()
        elif choice == "qmd":
            qmd = QMDBackend()
            _backend = qmd if qmd.is_available() else GrepBackend()
        else:
            # auto: QMD → Embedded → Grep
            qmd = QMDBackend()
            if qmd.is_available():
                _backend = qmd
            else:
                embedded = _make_embedded(config)
                if embedded is not None and embedded.is_available():
                    _backend = embedded
                else:
                    _backend = GrepBackend()
    return _backend


def _make_embedded(config: dict) -> "SearchBackend | None":
    """Try to create an EmbeddedSearchBackend. Returns None on failure."""
    try:
        from memento.config import get_vault
        from memento.embedded_search import EmbeddedSearchBackend

        vault = get_vault()
        if not vault.exists():
            return None
        db_rel = config.get("search_db_path", ".search/search.db")
        db_path = vault / db_rel

        # Try to build an embedding provider for vector search
        provider = None
        try:
            from memento.embedding import get_embedding_provider

            provider = get_embedding_provider(config)
            if not provider.is_available():
                import logging

                logging.getLogger(__name__).info("Embedding provider not available, running FTS5-only")
                provider = None
        except Exception:
            pass

        fts5_score_k = config.get("fts5_score_k", 2.0)
        return EmbeddedSearchBackend(
            vault_path=vault, db_path=db_path, embedding_provider=provider, fts5_score_k=fts5_score_k
        )
    except Exception:
        return None


def set_backend(backend: SearchBackend) -> None:
    """Override the search backend (for testing or alternative implementations)."""
    global _backend
    _backend = backend


def reset_backend() -> None:
    """Reset to default backend. Useful for testing."""
    global _backend
    _backend = None
