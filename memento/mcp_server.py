"""MCP server for memento vault — exposes search, store, status, and get operations."""

import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from memento.config import get_config, get_vault, slugify
from memento.search import enhance_results, has_qmd, qmd_search_with_extras, qmd_get
from memento.store import (
    acquire_vault_write_lock,
    log_retrieval,
    release_vault_write_lock,
    update_project_index,
    write_note,
)
from memento.utils import sanitize_secrets

mcp = FastMCP(
    "memento-vault",
    instructions=(
        "Memento Vault is a persistent knowledge store for coding agents. "
        "Use memento_search to find past decisions, discoveries, and session notes. "
        "Use memento_store to capture new knowledge from the current session. "
        "Use memento_get to read a specific note by path. "
        "Use memento_status to check vault health and stats."
    ),
)


def _strip_injection(text: str) -> str:
    """Strip instruction-like patterns from content (defense-in-depth)."""
    if not text:
        return text
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text, flags=re.MULTILINE)
    text = re.sub(r"</?s>", "", text)
    return text


@mcp.tool()
def memento_search(
    query: str,
    limit: int = 5,
    semantic: bool = False,
    min_score: float = 0.0,
    cwd: str = "",
) -> list[dict]:
    """Search the memento vault for relevant notes.

    Args:
        query: Search query string.
        limit: Maximum number of results to return.
        semantic: Use vector (semantic) search instead of BM25 keyword search.
        min_score: Minimum relevance score (0.0-1.0).
        cwd: Current working directory -- used to filter results by project scope.

    Returns:
        List of matching notes with path, title, score, and snippet.
    """
    if not query or not query.strip():
        return []

    if not has_qmd():
        return [{"error": "QMD search engine is not installed or not available"}]

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        return [{"error": f"Vault not found at {vault}"}]

    results = qmd_search_with_extras(
        query,
        limit=limit + 3,
        semantic=semantic,
        timeout=10,
        min_score=min_score,
    )

    if results:
        results = enhance_results(results, cwd=cwd or None)

    output = []
    for r in results[:limit]:
        output.append(
            {
                "path": r.get("path", ""),
                "title": _strip_injection(r.get("title", "")),
                "score": round(r.get("score", 0.0), 4),
                "snippet": _strip_injection(r.get("snippet", "")),
            }
        )

    log_retrieval("mcp", "search", query=query, results=len(output))
    return output


@mcp.tool()
def memento_store(
    title: str,
    body: str,
    note_type: str = "discovery",
    tags: list[str] | None = None,
    certainty: int | None = None,
    project: str | None = None,
    branch: str | None = None,
    session_id: str | None = None,
    validity_context: str | None = None,
    supersedes: str | None = None,
) -> dict:
    """Store a new note in the memento vault.

    Args:
        title: Note title (used as the filename slug).
        body: Note body content (markdown).
        note_type: Note type -- one of: discovery, decision, pattern, debugging, architecture.
        tags: List of tags for categorization.
        certainty: Confidence level 1-5 (5 = proven fact, 1 = speculation).
        project: Project path or identifier this note belongs to.
        branch: Git branch this note was created on.
        session_id: Session identifier for traceability.
        validity_context: Conditions under which this note remains valid.
        supersedes: Title of a note this one replaces.

    Returns:
        Dict with the path of the written note, or an error.
    """
    if not title or not title.strip():
        return {"error": "title is required"}
    if not body or not body.strip():
        return {"error": "body is required"}

    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    sanitized_body = sanitize_secrets(body)

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock (another write in progress)"}

    try:
        path = write_note(
            vault,
            title=title.strip(),
            body=sanitized_body,
            note_type=note_type,
            tags=tags or [],
            certainty=certainty,
            source="mcp",
            validity_context=validity_context,
            supersedes=supersedes,
            project=project,
            branch=branch,
            session_id=session_id,
        )

        # Update project index if we can derive a project slug
        project_slug = None
        if project:
            project_slug = slugify(Path(project).name) or None
        if project_slug:
            summary = f"MCP store: {title.strip()[:80]}"
            update_project_index(vault, project_slug, path.stem, summary)

        log_retrieval("mcp", "store", title=title, path=str(path))
        return {"path": str(path.relative_to(vault)), "title": title.strip()}

    finally:
        release_vault_write_lock()


@mcp.tool()
def memento_status() -> dict:
    """Get vault status: note count, project count, config summary.

    Returns:
        Dict with vault_path, note_count, project_count, fleeting_count, and key config values.
    """
    config = get_config()
    vault = get_vault()

    status = {
        "vault_path": str(vault),
        "vault_exists": vault.exists(),
        "qmd_available": has_qmd(),
    }

    if not vault.exists():
        return status

    notes_dir = vault / "notes"
    projects_dir = vault / "projects"
    fleeting_dir = vault / "fleeting"

    status["note_count"] = len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0
    status["project_count"] = len(list(projects_dir.glob("*.md"))) if projects_dir.exists() else 0
    status["fleeting_count"] = len(list(fleeting_dir.glob("*.md"))) if fleeting_dir.exists() else 0

    # Key config values (no secrets)
    status["config"] = {
        "qmd_collection": config.get("qmd_collection", "memento"),
        "llm_backend": config.get("llm_backend", "claude"),
        "prf_enabled": config.get("prf_enabled", True),
        "rrf_enabled": config.get("rrf_enabled", True),
        "reranker_enabled": config.get("reranker_enabled", True),
        "inception_enabled": config.get("inception_enabled", False),
    }

    log_retrieval("mcp", "status")
    return status


@mcp.tool()
def memento_get(path: str) -> dict:
    """Get a specific note by path or name.

    Args:
        path: Note path relative to vault (e.g. "notes/my-note.md") or just the note name
              (e.g. "my-note"). Also accepts full vault paths.

    Returns:
        Dict with path, title, and content of the note, or an error.
    """
    if not path or not path.strip():
        return {"error": "path is required"}

    vault = get_vault()
    path = path.strip()

    # Normalize: if it's just a name, try notes/<name>.md
    if not path.endswith(".md"):
        path = f"notes/{path}.md"
    elif not path.startswith("notes/") and "/" not in path:
        path = f"notes/{path}"

    # Path traversal guard
    full_path = (vault / path).resolve()
    if not str(full_path).startswith(str(vault.resolve())):
        return {"error": "Invalid path: traversal outside vault"}
    if full_path.exists():
        content = full_path.read_text()
        # Extract title from frontmatter
        title = Path(path).stem
        title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip().strip('"').strip("'")

        return {
            "path": path,
            "title": _strip_injection(title),
            "content": _strip_injection(content),
        }

    # Fall back to QMD get
    result = qmd_get(path)
    if result:
        return {
            "path": result.get("path", path),
            "title": _strip_injection(result.get("title", "")),
            "content": _strip_injection(result.get("content", "")),
        }

    return {"error": f"Note not found: {path}"}


def main():
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
