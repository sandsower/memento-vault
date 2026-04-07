"""MCP server for memento vault — exposes search, store, status, capture, and get operations."""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from memento.config import detect_project, get_config, get_vault, slugify
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
        "Use memento_store to write a single knowledge note. "
        "Use memento_capture at session end to triage and capture the full session. "
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
        return {"error": "QMD search engine is not installed or not available"}

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        return {"error": f"Vault not found at {vault}"}

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

    # Path traversal guard (use is_relative_to for proper boundary check)
    full_path = (vault / path).resolve()
    vault_resolved = vault.resolve()
    if full_path != vault_resolved and vault_resolved not in full_path.parents:
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


@mcp.tool()
def memento_capture(
    session_summary: str,
    cwd: str = "",
    branch: str = "",
    files_edited: list[str] | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    agent: str = "unknown",
) -> dict:
    """Capture a session's knowledge into the vault.

    This is the MCP equivalent of the SessionEnd hook. Use it when your agent
    doesn't have native hook support (Cursor, Windsurf, etc.).

    Two modes:
    - Provide session_summary with context fields for direct note creation.
    - Provide transcript_path to parse a transcript file and run full triage.

    Args:
        session_summary: What happened in this session (decisions, discoveries, fixes).
        cwd: Working directory of the session.
        branch: Git branch the session was on.
        files_edited: List of files that were edited.
        session_id: Session identifier for traceability. Auto-generated if omitted.
        transcript_path: Path to a transcript file for full triage parsing.
        agent: Which agent produced this session (claude, codex, cursor, windsurf).

    Returns:
        Dict with capture results: notes written, project updated, or error.
    """
    if not session_summary and not transcript_path:
        return {"error": "Provide session_summary or transcript_path"}

    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    session_id = session_id or uuid.uuid4().hex[:12]

    # Mode 1: transcript file parsing via adapter
    if transcript_path:
        if not os.path.exists(transcript_path):
            return {"error": f"Transcript file not found: {transcript_path}"}

        # Restrict to known agent transcript directories
        abs_path = os.path.abspath(transcript_path)
        allowed_prefixes = [
            os.path.join(str(Path.home()), ".claude"),
            os.path.join(str(Path.home()), ".codex"),
            os.path.join(str(Path.home()), ".cursor"),
            os.path.join(str(Path.home()), ".codeium"),
            "/tmp",
        ]
        if not any(abs_path.startswith(p) for p in allowed_prefixes):
            return {"error": "transcript_path must be inside a known agent directory"}

        try:
            from memento.adapters import parse_transcript

            meta = parse_transcript(transcript_path, agent=agent if agent != "unknown" else None)
            cwd = cwd or meta.get("cwd", "")
            branch = branch or meta.get("git_branch", "")
            files_edited = files_edited or meta.get("files_edited", [])

            if not session_summary:
                parts = []
                if meta.get("first_prompt"):
                    parts.append(meta["first_prompt"])
                if meta.get("last_outcome"):
                    parts.append(meta["last_outcome"])
                session_summary = " ".join(parts) or f"Session with {meta.get('exchange_count', 0)} exchanges"

        except ValueError as exc:
            log_retrieval("mcp", "capture_agent_unsupported", error=str(exc))
            return {"error": str(exc)}
        except (OSError, json.JSONDecodeError) as exc:
            log_retrieval("mcp", "capture_parse_failed", error=f"{type(exc).__name__}: {exc}")
            return {"error": f"Failed to parse transcript ({type(exc).__name__}): {exc}"}
        except Exception as exc:
            log_retrieval("mcp", "capture_unexpected", error=f"{type(exc).__name__}: {exc}")
            return {"error": f"Unexpected error: {type(exc).__name__}: {exc}"}

    # Derive project
    project_slug, ticket = detect_project(cwd, branch) if cwd else ("unknown", None)

    # Write the session note
    sanitized_summary = sanitize_secrets(session_summary)
    files_str = ""
    if files_edited:
        files_str = "\n\n## Files edited\n" + "\n".join(f"- {f}" for f in files_edited[:20])

    body = sanitized_summary + files_str

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock"}

    try:
        # Write fleeting note
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc).strftime("%H:%M")
        fleeting_dir = vault / "fleeting"
        fleeting_dir.mkdir(parents=True, exist_ok=True)
        fleeting_file = fleeting_dir / f"{today}.md"

        if not fleeting_file.exists():
            fleeting_file.write_text(f"# {today}\n\n")

        branch_str = f" ({branch})" if branch else ""
        files_count = f", {len(files_edited)} files" if files_edited else ""
        fleeting_line = f"- {now} `{session_id}` {cwd or '?'}{branch_str} — {agent}{files_count}\n"
        with open(fleeting_file, "a") as f:
            f.write(fleeting_line)

        # Write atomic note from summary
        title_text = sanitized_summary[:80]
        if len(sanitized_summary) > 80:
            title_text = title_text.rsplit(" ", 1)[0] + "..."

        note_path = write_note(
            vault,
            title=title_text,
            body=body,
            note_type="discovery",
            tags=[agent, project_slug] if project_slug != "unknown" else [agent],
            certainty=2,
            source="mcp-capture",
            project=cwd or None,
            branch=branch or None,
            session_id=session_id,
        )

        # Update project index
        if project_slug != "unknown":
            (vault / "projects").mkdir(parents=True, exist_ok=True)
            summary_line = f"MCP capture ({agent}): {title_text}"
            update_project_index(vault, project_slug, note_path.stem, summary_line)

        log_retrieval("mcp", "capture", session_id=session_id, agent=agent, project=project_slug)

        return {
            "session_id": session_id,
            "note_path": str(note_path.relative_to(vault)),
            "project": project_slug,
            "fleeting": str(fleeting_file.relative_to(vault)),
        }

    finally:
        release_vault_write_lock()


def main():
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
