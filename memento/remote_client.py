"""HTTP client for connecting to a remote memento vault server.

Used by hooks when MEMENTO_VAULT_URL is set. Provides the same operations
as the local vault (search, contradictions, store, smart_store, get, capture, preserve, status)
but over HTTP, calling the remote MCP server's tools via a simple REST-like wrapper.

The MCP streamable-http transport uses JSON-RPC over HTTP POST. This client
speaks that protocol directly — no MCP client library needed.
"""

from __future__ import annotations

import json
import os
from urllib import request
from urllib.error import HTTPError, URLError


def _vault_url() -> str | None:
    """Get the remote vault URL from environment, or None for local mode."""
    return os.environ.get("MEMENTO_VAULT_URL")


def _api_key() -> str | None:
    """Get the API key for remote vault auth."""
    return os.environ.get("MEMENTO_API_KEY")


def is_remote() -> bool:
    """Return True if the vault is configured for remote access."""
    return bool(_vault_url())


def _call_tool(tool_name: str, arguments: dict, timeout: int = 30) -> dict:
    """Call an MCP tool on the remote vault via JSON-RPC over HTTP.

    The MCP streamable-http transport accepts JSON-RPC requests at the /mcp endpoint.
    We send a tools/call request and parse the response.
    """
    url = _vault_url()
    if not url:
        raise RuntimeError("MEMENTO_VAULT_URL not set")

    # Ensure URL ends with the MCP endpoint
    base = url.rstrip("/")
    if not base.endswith("/mcp"):
        base += "/mcp"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(payload).encode()
    req = request.Request(base, data=data, method="POST", headers=headers)

    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except (URLError, HTTPError, OSError, json.JSONDecodeError) as exc:
        return {"error": f"Remote vault request failed: {exc}"}

    # Parse JSON-RPC response
    if "error" in body:
        return {"error": body["error"].get("message", str(body["error"]))}

    result = body.get("result", {})
    # Newer FastMCP streamable-http responses include typed structuredContent.
    # Prefer it so list-returning tools do not get collapsed into a single dict
    # when content is rendered as one text block per item.
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]

    # MCP tools/call returns {content: [{type: "text", text: "..."}]}
    # FastMCP serializes list results as multiple text content blocks,
    # so we collect all parseable items and return a list if there are many.
    content = result.get("content", [])
    if content and isinstance(content, list):
        parsed = []
        for item in content:
            if item.get("type") == "text":
                try:
                    parsed.append(json.loads(item["text"]))
                except (json.JSONDecodeError, KeyError):
                    parsed.append({"text": item.get("text", "")})
        if len(parsed) == 1:
            item = parsed[0]
            if tool_name in {"memento_search", "memento_list"} and isinstance(item, dict) and "path" in item:
                return parsed
            return item
        if parsed:
            return parsed
    return result


def list_notes(include_hash: bool = True, timeout: int = 30) -> list[dict] | None:
    """List all notes on the remote vault with optional content hashes.

    Returns None on error (network failure, server error, malformed response).
    Callers must distinguish None (error) from [] (genuinely empty remote) —
    treating an error as an empty vault would cause bulk-push of duplicates.
    """
    result = _call_tool("memento_list", {"include_hash": include_hash}, timeout=timeout)
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "error" in result:
        import sys

        print(f"[memento] remote list error: {result['error']}", file=sys.stderr)
        return None
    return None


def search_envelope(
    query: str,
    limit: int = 5,
    semantic: bool = False,
    min_score: float = 0.0,
    cwd: str = "",
    concrete: object = "auto",
    detail_level: str = "summary",
    include_content: bool = False,
    token_budget: int = 2000,
    timeout: int = 30,
) -> dict:
    """Search the remote vault, preserving structured miss metadata when present."""
    result = _call_tool(
        "memento_search",
        {
            "query": query,
            "limit": limit,
            "semantic": semantic,
            "min_score": min_score,
            "cwd": cwd,
            "concrete": concrete,
            "detail_level": detail_level,
            "include_content": include_content,
            "token_budget": token_budget,
        },
        timeout=timeout,
    )
    if isinstance(result, list):
        return {"results": result}
    if isinstance(result, dict):
        if "error" in result:
            import sys

            print(f"[memento] remote search error: {result['error']}", file=sys.stderr)
            return {"results": [], "error": result["error"]}
        envelope_results = result.get("results")
        if isinstance(envelope_results, list):
            return result
    return {"results": []}


def search(
    query: str,
    limit: int = 5,
    semantic: bool = False,
    min_score: float = 0.0,
    cwd: str = "",
    concrete: object = "auto",
    detail_level: str = "summary",
    include_content: bool = False,
    token_budget: int = 2000,
    timeout: int = 30,
) -> list[dict]:
    """Search the remote vault, returning only results for legacy callers."""
    envelope = search_envelope(
        query=query,
        limit=limit,
        semantic=semantic,
        min_score=min_score,
        cwd=cwd,
        concrete=concrete,
        detail_level=detail_level,
        include_content=include_content,
        token_budget=token_budget,
        timeout=timeout,
    )
    results = envelope.get("results")
    return results if isinstance(results, list) else []


def query(
    project: str = "",
    note_type: str = "",
    tag: str = "",
    source: str = "",
    certainty_min: int | None = None,
    certainty_max: int | None = None,
    date_start: str = "",
    date_end: str = "",
    branch: str = "",
    session_id: str = "",
    aggregate_by: str = "",
    recent_sessions_project: str = "",
    limit: int = 20,
    timeout: int = 30,
) -> dict:
    """Run a typed metadata query against the remote vault."""
    args = {
        "project": project,
        "note_type": note_type,
        "tag": tag,
        "source": source,
        "certainty_min": certainty_min,
        "certainty_max": certainty_max,
        "date_start": date_start,
        "date_end": date_end,
        "branch": branch,
        "session_id": session_id,
        "aggregate_by": aggregate_by,
        "recent_sessions_project": recent_sessions_project,
        "limit": limit,
    }
    result = _call_tool("memento_query", args, timeout=timeout)
    return result if isinstance(result, dict) else {"results": []}


def contradictions(topic: str, limit: int = 20, min_certainty: int = 2, timeout: int = 30) -> dict:
    """Inspect remote notes for disagreement and supersession candidates."""
    result = _call_tool(
        "memento_contradictions",
        {"topic": topic, "limit": limit, "min_certainty": min_certainty},
        timeout=timeout,
    )
    return result if isinstance(result, dict) else {"results": []}


def get(path: str, timeout: int = 30) -> dict | None:
    """Get a specific note from the remote vault."""
    result = _call_tool("memento_get", {"path": path}, timeout=timeout)
    if isinstance(result, dict) and "error" not in result:
        return result
    return None


def _note_args(
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
    args = {"title": title, "body": body, "note_type": note_type}
    if tags:
        args["tags"] = tags
    if certainty is not None:
        args["certainty"] = certainty
    if project:
        args["project"] = project
    if branch:
        args["branch"] = branch
    if session_id:
        args["session_id"] = session_id
    if validity_context:
        args["validity_context"] = validity_context
    if supersedes:
        args["supersedes"] = supersedes
    return args


def store(
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
    timeout: int = 30,
) -> dict:
    """Store a note in the remote vault."""
    args = _note_args(
        title,
        body,
        note_type,
        tags,
        certainty,
        project,
        branch,
        session_id,
        validity_context,
        supersedes,
    )
    return _call_tool("memento_store", args, timeout=timeout)


def replace_note(
    path: str,
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
    timeout: int = 30,
) -> dict:
    """Replace an existing remote note at a known path.

    This is the explicit conflict-resolution primitive for remote sync. Unlike
    store(), it does not append a dedupe suffix when the note already exists.
    """
    args = _note_args(
        title,
        body,
        note_type,
        tags,
        certainty,
        project,
        branch,
        session_id,
        validity_context,
        supersedes,
    )
    args["path"] = path
    return _call_tool("memento_replace_note", args, timeout=timeout)


def smart_store(
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
    origin: str | None = None,
    timeout: int = 30,
) -> dict:
    """Smart-store a note in the remote vault."""
    args = {"title": title, "body": body, "note_type": note_type}
    if tags:
        args["tags"] = tags
    if certainty is not None:
        args["certainty"] = certainty
    if project:
        args["project"] = project
    if branch:
        args["branch"] = branch
    if session_id:
        args["session_id"] = session_id
    if validity_context:
        args["validity_context"] = validity_context
    if supersedes:
        args["supersedes"] = supersedes
    if origin:
        args["origin"] = origin
    return _call_tool("memento_store_smart", args, timeout=timeout)


def capture_run_lesson(
    candidate: dict,
    approve_write: bool = False,
    timeout: int = 30,
) -> dict:
    """Queue or explicitly write a typed automated-run lesson candidate."""
    return _call_tool(
        "memento_capture_run_lesson",
        {"candidate": candidate, "approve_write": approve_write},
        timeout=timeout,
    )


def synthesize_failures(
    run_summaries: list[dict] | dict,
    approve_writes: bool = False,
    project: str = "",
    branch: str = "",
    session_id: str | None = None,
    max_candidates: int = 20,
    timeout: int = 30,
) -> dict:
    """Synthesize batch failures from sanitized external run summaries."""
    args = {
        "run_summaries": run_summaries,
        "approve_writes": approve_writes,
        "max_candidates": max_candidates,
    }
    if project:
        args["project"] = project
    if branch:
        args["branch"] = branch
    if session_id:
        args["session_id"] = session_id
    return _call_tool("memento_synthesize_failures", args, timeout=timeout)


def capture(
    session_summary: str,
    cwd: str = "",
    branch: str = "",
    files_edited: list[str] | None = None,
    session_id: str | None = None,
    agent: str = "unknown",
    fleeting_only: bool = False,
    timeout: int = 30,
) -> dict:
    """Capture a session to the remote vault."""
    args = {"session_summary": session_summary, "cwd": cwd, "branch": branch, "agent": agent}
    if files_edited:
        args["files_edited"] = files_edited
    if session_id:
        args["session_id"] = session_id
    if fleeting_only:
        args["fleeting_only"] = True
    return _call_tool("memento_capture", args, timeout=timeout)


def preserve(
    path: str,
    title: str | None = None,
    slug: str | None = None,
    project: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    move: bool = False,
    include_manifest: bool = True,
    link_project_index: bool = True,
    cwd: str = "",
    branch: str = "",
    session_id: str | None = None,
    timeout: int = 30,
) -> dict:
    """Preserve a file or directory bundle in the remote archive."""
    args = {"path": path, "move": move, "include_manifest": include_manifest, "link_project_index": link_project_index}
    if title:
        args["title"] = title
    if slug:
        args["slug"] = slug
    if project:
        args["project"] = project
    if description:
        args["description"] = description
    if tags:
        args["tags"] = tags
    if cwd:
        args["cwd"] = cwd
    if branch:
        args["branch"] = branch
    if session_id:
        args["session_id"] = session_id
    return _call_tool("memento_preserve", args, timeout=timeout)


def status(timeout: int = 30) -> dict:
    """Get status of the remote vault."""
    return _call_tool("memento_status", {}, timeout=timeout)
