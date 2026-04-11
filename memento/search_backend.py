"""Search backend abstraction — decouples vault search from QMD CLI.

Provides a SearchBackend protocol and a QMDBackend implementation that
wraps the QMD CLI subprocess calls. Other backends (e.g., Meilisearch,
SQLite FTS, Tantivy) can be added by implementing the same interface.
"""

import json
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path


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
    ) -> list[dict]:
        """Search for notes matching a query.

        Returns list of dicts with keys: path, title, score, snippet.
        """
        ...

    @abstractmethod
    def get(self, path: str, collection: str | None = None, timeout: int = 5) -> dict | None:
        """Fetch a single note by path.

        Returns dict with path, title, content, score keys, or None.
        """
        ...

    @abstractmethod
    def reindex(self, collection: str, embed: bool = True) -> bool:
        """Trigger reindexing of the collection.

        Returns True if reindexing was initiated successfully.
        """
        ...


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
    ) -> list[dict]:
        if not query or not query.strip():
            return []

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
                score = item.get("score", 0.0)
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
    ) -> list[dict]:
        if not query or not query.strip():
            return []

        import time

        from memento.config import get_vault

        vault = get_vault()
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

            score = matched / len(terms)
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
            results.append({"path": rel_path, "title": title, "score": score, "snippet": snippet})

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

    def reindex(self, collection: str, embed: bool = True) -> bool:
        # Grep backend has no index to update
        return True


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
        from memento.config import get_config, get_vault

        config = get_config()
        choice = config.get("search_backend", "auto")

        if choice == "embedded":
            _backend = _make_embedded(config)
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

        return EmbeddedSearchBackend(vault_path=vault, db_path=db_path, embedding_provider=provider)
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
