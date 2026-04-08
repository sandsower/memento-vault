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
        return bool(shutil.which("qmd"))

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
    """Get the configured search backend (singleton)."""
    global _backend
    if _backend is None:
        _backend = QMDBackend()
    return _backend


def set_backend(backend: SearchBackend) -> None:
    """Override the search backend (for testing or alternative implementations)."""
    global _backend
    _backend = backend


def reset_backend() -> None:
    """Reset to default backend. Useful for testing."""
    global _backend
    _backend = None
