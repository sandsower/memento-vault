"""HTTP client for connecting to a remote memento vault server.

Used by hooks when MEMENTO_VAULT_URL is set. Provides the same operations
as the local vault (search, store, get, capture, status) but over HTTP,
calling the remote MCP server's tools via a simple REST-like wrapper.

The MCP streamable-http transport uses JSON-RPC over HTTP POST. This client
speaks that protocol directly — no MCP client library needed.
"""

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
            return parsed[0]
        if parsed:
            return parsed
    return result


def search(query: str, limit: int = 5, semantic: bool = False, min_score: float = 0.0, cwd: str = "") -> list[dict]:
    """Search the remote vault."""
    result = _call_tool(
        "memento_search",
        {"query": query, "limit": limit, "semantic": semantic, "min_score": min_score, "cwd": cwd},
    )
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "error" in result:
        import sys

        print(f"[memento] remote search error: {result['error']}", file=sys.stderr)
        return []
    return []


def get(path: str) -> dict | None:
    """Get a specific note from the remote vault."""
    result = _call_tool("memento_get", {"path": path})
    if isinstance(result, dict) and "error" not in result:
        return result
    return None


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
) -> dict:
    """Store a note in the remote vault."""
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
    return _call_tool("memento_store", args)


def capture(
    session_summary: str,
    cwd: str = "",
    branch: str = "",
    files_edited: list[str] | None = None,
    session_id: str | None = None,
    agent: str = "unknown",
    fleeting_only: bool = False,
) -> dict:
    """Capture a session to the remote vault."""
    args = {"session_summary": session_summary, "cwd": cwd, "branch": branch, "agent": agent}
    if files_edited:
        args["files_edited"] = files_edited
    if session_id:
        args["session_id"] = session_id
    if fleeting_only:
        args["fleeting_only"] = True
    return _call_tool("memento_capture", args)


def status() -> dict:
    """Get status of the remote vault."""
    return _call_tool("memento_status", {})
