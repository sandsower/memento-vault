"""MCP server for memento vault — exposes search, store, status, capture, preserve, and get operations.

Supports both stdio (local) and streamable-http (remote) transports.
When running over HTTP, authentication is enforced via bearer tokens.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - fallback for stripped test envs

    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._tools = {}

        def tool(self, *decorator_args, **decorator_kwargs):
            def decorator(func):
                name = decorator_kwargs.get("name") or getattr(func, "__name__", "tool")
                self._tools[name] = func
                return func

            if decorator_args and callable(decorator_args[0]) and not decorator_kwargs:
                return decorator(decorator_args[0])
            return decorator

        def _jsonrpc_app(self):
            async def app(scope, receive, send):
                if scope["type"] != "http":
                    return
                path = scope.get("path") or ""
                if path.rstrip("/") not in {"/mcp", "mcp"}:
                    body = b"Not Found"
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 404,
                            "headers": [[b"content-type", b"text/plain"], [b"content-length", str(len(body)).encode()]],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return
                if scope.get("method") != "POST":
                    body = b"Method Not Allowed"
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 405,
                            "headers": [[b"content-type", b"text/plain"], [b"content-length", str(len(body)).encode()]],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return

                chunks = []
                while True:
                    event = await receive()
                    if event.get("type") != "http.request":
                        continue
                    if event.get("body"):
                        chunks.append(event["body"])
                    if not event.get("more_body"):
                        break
                try:
                    payload = json.loads(b"".join(chunks).decode() or "{}")
                except json.JSONDecodeError as exc:
                    response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
                else:
                    response = await self._handle_jsonrpc(payload)

                body = json.dumps(response).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})

            return app

        async def _handle_jsonrpc(self, payload):
            if not isinstance(payload, dict):
                return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
            request_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params") or {}
            if method != "tools/call":
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(tool_name, str):
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Missing tool name"}}
            tool = globals().get(tool_name)
            if tool is None:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
            try:
                result = tool(**arguments)
                if inspect.isawaitable(result):
                    result = await result
                safe_result = json.loads(json.dumps(result, default=str))
            except Exception as exc:  # pragma: no cover - defensive fallback
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}
            text_result = json.dumps(safe_result, ensure_ascii=False)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text_result}],
                    "structuredContent": {"result": safe_result},
                },
            }

        def run(self, *args, **kwargs):
            raise RuntimeError("mcp package is required to run the MCP server")

        def streamable_http_app(self):
            return self._jsonrpc_app()

        def sse_app(self):
            return self._jsonrpc_app()


from memento.config import detect_project, get_config, get_vault, get_vault_id, slugify
from memento.graph import build_related_view
from memento.health import build_automation_memory_readiness
from memento.lifecycle import build_briefing, build_recall, build_session_context, build_tool_context
from memento.search import (
    enhance_results,
    expand_result_links,
    filter_by_project,
    has_qmd,
    miss_envelope,
    qmd_get,
    qmd_search_with_extras,
    shape_search_results,
)
from memento.retrieval_policy import ExplicitSearchRequest, ExplicitSearchRuntime, _project_slug_from_value
from memento.query import QueryValidationError, build_metadata_filter, query_notes, read_note_record
from memento.contradictions import inspect_contradictions
from memento.store import (
    acquire_vault_write_lock,
    append_fleeting_session,
    append_project_session_line,
    log_retrieval,
    normalize_note_contract,
    record_access,
    release_vault_write_lock,
    replace_note_at_path,
    update_project_index,
    write_daily_snapshot,
    write_note,
)
from memento.smart_store import write_smart_store_note
from memento.batch_synthesis import synthesize_failure_batch
from memento.automated_run_lessons import (
    capture_automated_run_lesson,
    lesson_candidate_from_batch_candidate,
)
from memento.preserve import preserve_bundle
from memento.utils import sanitize_secrets


# Loopback by default — vaults shouldn't land on a public interface accidentally.
# Docker users explicitly set MEMENTO_HOST=0.0.0.0 in docker-compose.yml; the
# default only affects ad-hoc local HTTP runs.
_DEFAULT_BIND_HOST = "127.0.0.1"


def _bind_host() -> str:
    """HTTP transport bind address. Defaults to loopback.

    Set MEMENTO_HOST=0.0.0.0 to listen on every interface (requires
    MEMENTO_API_KEY — main() refuses non-local binds without it).
    """
    host = os.environ.get("MEMENTO_HOST", _DEFAULT_BIND_HOST).strip()
    return host or _DEFAULT_BIND_HOST


def _meaningful_note_body(body: str) -> str:
    body = body.strip()
    while body.endswith("## Related"):
        body = body[: -len("## Related")].rstrip()
    return body


def _note_payload_matches(
    path: Path,
    *,
    title: str,
    body: str,
    note_type: str,
    tags: list[str],
    certainty: int | None = None,
    source: str | None = None,
    project: str | None = None,
    branch: str | None = None,
    validity_context: str | None = None,
    supersedes: str | None = None,
    session_id: str | None = None,
    origin: str | None = None,
) -> bool:
    """Return True when an existing note represents the same store payload."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return False

    fm, existing_body = parts[1], _meaningful_note_body(parts[2])

    def scalar(key: str) -> str | None:
        match = re.search(rf"^{re.escape(key)}:\s*(.+)$", fm, re.MULTILINE)
        if not match:
            return None
        return match.group(1).strip().strip("\"'")

    def tag_values() -> list[str]:
        match = re.search(r"^tags:\s*\[([^\]]*)\]", fm, re.MULTILINE)
        if not match:
            return []
        return [t.strip().strip("\"'") for t in match.group(1).split(",") if t.strip()]

    comparisons = [
        scalar("title") == title.strip(),
        scalar("type") == note_type,
        tag_values() == (tags or []),
        existing_body == _meaningful_note_body(body),
    ]

    optional_fields = {
        "source": source,
        "certainty": str(int(certainty)) if certainty is not None else None,
        "project": project,
        "branch": branch,
        "validity-context": validity_context,
        "supersedes": supersedes,
        "session_id": session_id,
        "origin": origin,
    }
    for key, expected in optional_fields.items():
        if expected is not None:
            comparisons.append(scalar(key) == str(expected))

    return all(comparisons)


def _build_server() -> FastMCP:
    """Build the FastMCP server, configured from environment variables.

    Environment variables:
        MEMENTO_HOST: Bind address for HTTP transport (default: 127.0.0.1)
        MEMENTO_PORT: Port for HTTP transport (default: 8745)
        MEMENTO_API_KEY: Bearer token for HTTP auth (optional)
    """
    host = _bind_host()
    port = int(os.environ.get("MEMENTO_PORT", "8745"))

    kwargs = {
        "name": "memento-vault",
        "instructions": (
            "Memento Vault is a persistent knowledge store for coding agents.\n\n"
            "General answering path: use memento_search when the user asks about "
            "past decisions, prior fixes, project history, session context, or exact "
            "identifiers. Use memento_contradictions when the user wants to inspect "
            "disagreements, stale conclusions, or supersession chains. Use memento_get "
            "after search when you need the full content for a returned path, or directly "
            "when the user already supplied a note path/name. Use memento_status for vault "
            "health and memento_list for sync/inventory.\n\n"
            "Lifecycle tools (memento_briefing, memento_recall, "
            "memento_session_context, memento_tool_context) are host-adapter primitives for automatic context "
            "injection, not general user-answering tools.\n\n"
            "Writes: if your agent has a `memento` skill or `SessionEnd` hook, "
            "use it — the skill is local-first (writes to the git-backed vault, "
            "commits, then syncs here), which avoids duplicate notes and keeps "
            "the local vault canonical. memento_store, memento_store_smart, memento_capture, and "
            "memento_daily_snapshot are low-level primitives intended for automated "
            "sync/scripts, structured daily-snapshot integrations, and agents "
            "without skill/hook support (Windsurf, some Cursor configs). Do not "
            "call them from interactive Claude Code or Codex sessions — use the "
            "/memento skill instead."
        ),
        "host": host,
        "port": port,
        "stateless_http": True,
        "json_response": True,
    }

    return FastMCP(**kwargs)


mcp = _build_server()

# Set at startup by main() — used by tools to know if they're running over HTTP
_active_transport: str = "stdio"


def _strip_injection(text: str) -> str:
    """Strip instruction-like patterns from content (defense-in-depth)."""
    if not text:
        return text
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text, flags=re.MULTILINE)
    text = re.sub(r"</?s>", "", text)
    return text


def _vault_relative_access_path(vault: Path, path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        candidate = Path(text).expanduser()
        vault_resolved = vault.resolve()
        resolved = candidate.resolve() if candidate.is_absolute() else (vault / candidate).resolve()
        if resolved == vault_resolved:
            return ""
        return str(resolved.relative_to(vault_resolved)).replace(os.sep, "/")
    except Exception:
        return text.replace("\\", "/")


def _search_filters_requested(
    *,
    note_type: str,
    tags: str,
    certainty_min: int | None,
    certainty_max: int | None,
    date_from: str,
    date_to: str,
    branch: str,
    session_id: str,
    project: str,
) -> bool:
    return bool(
        note_type
        or tags
        or certainty_min is not None
        or certainty_max is not None
        or date_from
        or date_to
        or branch
        or session_id
        or project
    )


def _apply_search_metadata_filters(
    result: dict,
    *,
    vault: Path,
    limit: int,
    note_type: str,
    tags: str,
    certainty_min: int | None,
    certainty_max: int | None,
    date_from: str,
    date_to: str,
    branch: str,
    session_id: str,
    project: str,
) -> dict:
    """Post-filter ranked search results using memento_query's filter semantics.

    Reuses ``build_metadata_filter`` (the same predicate ``query_notes`` uses)
    for type/tag/certainty/date/branch/session_id, and layers a slug-based
    project comparison on top via the existing recall project-slug helper
    (``_project_slug_from_value``) so path-like and bare project values match
    consistently -- ``query_notes``'s own project filter stays exact-match and
    is untouched by this.
    """
    try:
        predicate, filters = build_metadata_filter(
            note_type=note_type,
            tag=tags,
            certainty_min=certainty_min,
            certainty_max=certainty_max,
            date_start=date_from,
            date_end=date_to,
            branch=branch,
            session_id=session_id,
        )
    except QueryValidationError as exc:
        return {"error": str(exc), "metadata": {"valid": False}}

    # Copy before echoing project -- `filters` is the exact dict `predicate`
    # closes over, so mutating it in place would silently turn "project" into
    # an active exact-match filter inside the shared _matches() core.
    filters_echo = dict(filters)
    filters_echo["project"] = project or None
    project_slug = _project_slug_from_value(project) if project else None

    def passes(entry: dict) -> bool:
        record = read_note_record(vault, entry.get("path", ""))
        if record is None:
            return False
        if project_slug and _project_slug_from_value(record.get("project")) != project_slug:
            return False
        return predicate(record)

    filtered = [entry for entry in result.get("results", []) if passes(entry)]
    result["results"] = filtered[:limit]
    result.setdefault("metadata", {})["filters_applied"] = filters_echo

    if not result["results"]:
        return miss_envelope(
            "filters_eliminated_all",
            details={"filters_applied": filters_echo},
            metadata=result.get("metadata"),
        )

    return result


@mcp.tool()
def memento_search(
    query: str,
    limit: int = 5,
    semantic: bool = False,
    min_score: float = 0.0,
    cwd: str = "",
    concrete: str | bool = "auto",
    detail_level: str = "summary",
    include_content: bool = False,
    token_budget: int | None = 2000,
    expand_links: bool = False,
    type: str = "",
    tags: str = "",
    certainty_min: int | None = None,
    certainty_max: int | None = None,
    date_from: str = "",
    date_to: str = "",
    branch: str = "",
    session_id: str = "",
    project: str = "",
) -> object:
    """Search vault notes for prior context before answering from memory.

    Use this when the user asks about past decisions, prior bug fixes,
    project/session history, recurring patterns, or where something was
    implemented. Also use it for exact identifier lookup (file names, function
    names, config keys, error strings); leave concrete="auto" so identifier-like
    queries use literal matching.

    Do not use this to read a known note path/name -- call memento_get directly.
    After search, call memento_get when a result's snippet/content is not enough
    and you need the full note for a returned path.

    Optional typed filters (type, tags, certainty_min, certainty_max,
    date_from, date_to, branch, session_id, project) let you combine ranked
    semantic/keyword retrieval with metadata constraints in one call --
    for example "semantically search X, certainty >= 4, type decision, last
    30 days". They reuse memento_query's filter semantics (same validation,
    same match logic) and run as a post-filter around the ranking pipeline:
    with any filter set, candidates are over-fetched (up to limit*3, capped
    at 50), ranked as usual, filtered by frontmatter metadata, then trimmed
    to `limit`; filtered-out candidates never affect the ranking of
    survivors. Omitting all filter params leaves search behavior byte-for-byte
    unchanged. There is no aggregation support here -- use memento_query for
    counts/aggregations, or when you only need metadata rows and no ranking.
    When expand_links is also set, link expansion runs AFTER filtering, on
    the filtered top hits; expanded neighbors are via_link context and are
    NOT themselves subject to the filters.

    Args:
        query: Natural-language question or exact identifier to search for.
        limit: Maximum number of results to return.
        semantic: Use vector (semantic) search instead of BM25 keyword search.
        min_score: Minimum relevance score (0.0-1.0).
        cwd: Current working directory -- used to filter results by project scope.
        concrete: true/false/auto literal search mode. Auto detects identifier-like queries.
        detail_level: Response shape: brief, summary, or full.
        include_content: Include note content alongside the selected detail level.
        token_budget: Approximate token budget for returned content, default 2000.
        expand_links: When true, append 1-hop wikilink neighbors of the top
            direct hits (marked with via_link); they never outrank direct
            hits. Default false -- no behavior change when omitted.
        type: Optional exact note type filter (mirrors memento_query's note_type,
            e.g. discovery, decision, bugfix).
        tags: Optional tag that must be present in the note's frontmatter tags
            (mirrors memento_query's tag; a single tag, not a list).
        certainty_min: Optional minimum certainty 1-5 (mirrors memento_query).
        certainty_max: Optional maximum certainty 1-5 (mirrors memento_query).
        date_from: Optional inclusive ISO date/datetime lower bound (mirrors
            memento_query's date_start).
        date_to: Optional inclusive ISO date/datetime upper bound (mirrors
            memento_query's date_end).
        branch: Optional exact branch frontmatter filter (mirrors memento_query).
        session_id: Optional exact session_id frontmatter filter (mirrors memento_query).
        project: Optional project filter compared by slug (path-like values are
            normalized the same way recall project-scoping does), not exact string match.

    Returns:
        Search envelope with results and metadata. Misses include structured
        miss metadata. When filters are active, metadata also carries
        filters_applied; if filters eliminate every hit, the standard empty
        miss envelope is returned with a filters_applied echo and a hint to
        use memento_query for raw metadata inspection.
    """
    has_filters = _search_filters_requested(
        note_type=type,
        tags=tags,
        certainty_min=certainty_min,
        certainty_max=certainty_max,
        date_from=date_from,
        date_to=date_to,
        branch=branch,
        session_id=session_id,
        project=project,
    )

    try:
        normalized_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        normalized_limit = 5
    search_limit = min(normalized_limit * 3, 50) if has_filters else limit

    runtime = ExplicitSearchRuntime(
        vault_loader=get_vault,
        has_backend=has_qmd,
        qmd_search=qmd_search_with_extras,
        enhance_results=enhance_results,
        filter_by_project=filter_by_project,
        log_retrieval=log_retrieval,
        record_access=record_access,
    )
    result = runtime.search(
        ExplicitSearchRequest(
            query=query,
            limit=search_limit,
            semantic=semantic,
            min_score=min_score,
            cwd=cwd,
            concrete=concrete,
            detail_level=detail_level,
            include_content=include_content,
            token_budget=token_budget,
        )
    )

    if has_filters and result.get("results"):
        result = _apply_search_metadata_filters(
            result,
            vault=get_vault(),
            limit=normalized_limit,
            note_type=type,
            tags=tags,
            certainty_min=certainty_min,
            certainty_max=certainty_max,
            date_from=date_from,
            date_to=date_to,
            branch=branch,
            session_id=session_id,
            project=project,
        )
        if "error" in result or not result.get("results"):
            return result

    if expand_links and result.get("results"):
        expanded_raw = expand_result_links(result["results"], config=get_config())
        if expanded_raw:
            shaped = shape_search_results(
                expanded_raw,
                vault=get_vault(),
                detail_level=detail_level,
                include_content=include_content,
                token_budget=token_budget,
            )
            via_link_by_path = {entry["path"]: entry.get("via_link", "") for entry in expanded_raw}
            for entry in shaped["results"]:
                entry["via_link"] = via_link_by_path.get(entry["path"], "")
            result["results"].extend(shaped["results"])
            result.setdefault("metadata", {})["expand_links"] = True
            result["metadata"]["expanded_count"] = len(shaped["results"])

    return result


@mcp.tool()
def memento_related(
    note: str,
    direction: str = "both",
    depth: int = 1,
) -> dict:
    """Explore the wikilink graph around a note: pure topology, no relevance scoring.

    Use this to answer "what links to X", "what does X link to", walk a
    supersession chain to find the current/superseded version of a note, or
    expand a small neighborhood around a note. Use memento_search instead for
    topical/semantic retrieval; use this only when the question is about
    graph structure.

    Args:
        note: Note stem (e.g. "redis-cache-ttl"), relative path (e.g.
            "notes/redis-cache-ttl.md"), or exact frontmatter title to
            resolve.
        direction: Neighborhood BFS direction: "out" (notes this note links
            to), "in" (notes linking to this note), or "both". Only affects
            the neighborhood field -- outbound/inbound are always returned.
        depth: Neighborhood BFS depth, clamped to 0-3.

    Returns:
        Dict with note/path/title, outbound, inbound, neighborhood
        ({"nodes": [...], "truncated": bool}), and supersession_chain
        (oldest to newest). An unresolved note returns a structured error
        with close-match suggestions; a missing networkx dependency returns
        a structured error instead of raising.
    """
    result = build_related_view(note, direction=direction, depth=depth)

    action = "related_error" if result.get("error") else "related"
    log_retrieval("mcp", action, note=note, reason=result.get("reason", ""))

    if not result.get("error"):
        paths = (
            [result["path"]]
            + [entry["path"] for entry in result.get("outbound", [])]
            + [entry["path"] for entry in result.get("inbound", [])]
        )
        record_access(paths, hook="mcp", tool="related", query=note, result_count=len(paths))

    return result


@mcp.tool()
def memento_query(
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
) -> dict:
    """Run typed metadata filters and aggregations over vault notes.

    Use this for count/list/filter questions that can be answered from note
    frontmatter without retrieving full note bodies. This is not a semantic
    recall tool: use memento_search for topical/past-decision retrieval, and
    memento_get for full content after a known path is selected.

    Args:
        project: Exact project frontmatter value to match.
        note_type: Exact note type to match (for example discovery, decision, bugfix).
        tag: Tag that must be present in frontmatter tags.
        source: Exact source frontmatter value to match.
        certainty_min: Minimum certainty 1-5.
        certainty_max: Maximum certainty 1-5.
        date_start: Inclusive ISO date/datetime lower bound.
        date_end: Inclusive ISO date/datetime upper bound.
        branch: Exact branch frontmatter value to match.
        session_id: Exact session_id frontmatter value to match.
        aggregate_by: Optional count bucket: project, type, tag, source, month, date, branch, or session_id.
        recent_sessions_project: When set, list recent sessions for this exact project instead of note rows.
        limit: Maximum rows/buckets/sessions to return.

    Returns:
        Compact structured results, aggregations, or recent_sessions plus metadata; invalid typed parameters return an error envelope.
    """
    payload = query_notes(
        get_vault(),
        project=project,
        note_type=note_type,
        tag=tag,
        source=source,
        certainty_min=certainty_min,
        certainty_max=certainty_max,
        date_start=date_start,
        date_end=date_end,
        branch=branch,
        session_id=session_id,
        aggregate_by=aggregate_by,
        recent_sessions_project=recent_sessions_project,
        limit=limit,
    )
    action = "query_invalid" if payload.get("error") else "query"
    log_retrieval("mcp", action, results=payload.get("metadata", {}).get("matched_notes", 0))
    paths = [entry["path"] for entry in payload.get("results", []) if entry.get("path")]
    for session in payload.get("recent_sessions", []):
        paths.extend(path for path in session.get("paths", []) if path)
    if paths:
        record_access(
            paths,
            hook="mcp",
            tool="query",
            query=json.dumps(payload.get("metadata", {}).get("filters", {})),
            result_count=len(paths),
        )
    return payload


@mcp.tool()
def memento_contradictions(topic: str, limit: int = 20, min_certainty: int = 2) -> dict:
    """Inspect a topic for disagreements, stale conclusions, and supersession chains.

    Use this when you want to compare competing notes about the same topic,
    surface explicit superseded notes, or understand whether newer notes have
    replaced older conclusions. Output includes source paths plus certainty/date
    context for the inspected notes.
    """
    payload = inspect_contradictions(topic, limit=limit, min_certainty=min_certainty)
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        for entry in payload["results"]:
            entry["title"] = _strip_injection(entry.get("title", ""))
            entry["snippet"] = _strip_injection(entry.get("snippet", ""))
            entry["status"] = _strip_injection(entry.get("status", ""))
            entry["polarity"] = _strip_injection(entry.get("polarity", ""))
            entry["path"] = _strip_injection(entry.get("path", ""))
        for group in payload.get("groups", []):
            group["theme"] = _strip_injection(group.get("theme", ""))
            group["summary"] = _strip_injection(group.get("summary", ""))
            group["note_paths"] = [_strip_injection(path) for path in group.get("note_paths", [])]
        for item in payload.get("contradictions", []):
            item["kind"] = _strip_injection(item.get("kind", ""))
            item["paths"] = [_strip_injection(path) for path in item.get("paths", [])]
            item["titles"] = [_strip_injection(title) for title in item.get("titles", [])]
        for item in payload.get("supersession", []):
            item["older_path"] = _strip_injection(item.get("older_path", ""))
            item["newer_path"] = _strip_injection(item.get("newer_path", ""))
            item["older_title"] = _strip_injection(item.get("older_title", ""))
            item["newer_title"] = _strip_injection(item.get("newer_title", ""))
        if isinstance(payload.get("summary"), str):
            payload["summary"] = _strip_injection(payload["summary"])
    log_retrieval(
        "mcp",
        "contradictions",
        topic=topic,
        results=len(payload.get("results", [])) if isinstance(payload, dict) else 0,
    )
    return payload


@mcp.tool()
def memento_briefing(cwd: str = "", session_id: str = "") -> dict:
    """Host-adapter primitive for automatic first-turn/session briefing.

    This is not a general user-answering search tool. Hosts call it during
    session startup to decide whether to inject compact project-aware vault
    context. For interactive questions about prior work, use memento_search and
    then memento_get if full note content is needed.
    """
    return build_briefing(cwd, session_id, host_id="mcp").to_dict()


@mcp.tool()
def memento_recall(prompt: str, cwd: str = "", session_id: str = "") -> dict:
    """Host-adapter primitive for automatic prompt-time context recall.

    This is not a general user-answering search tool. Hosts call it before an
    agent turn to decide whether to inject related memories. For explicit user
    questions about past decisions, prior fixes, or project history, call
    memento_search and then memento_get if full note content is needed.
    """
    return build_recall(prompt, cwd, session_id, host_id="mcp").to_dict()


@mcp.tool()
def memento_tool_context(tool_name: str, file_path: str, cwd: str = "", session_id: str = "") -> dict:
    """Host-adapter primitive for automatic read-tool context injection.

    This is not a general user-answering search tool. Hosts call it around file
    reads to attach code-area memories. For explicit recall/search requests,
    call memento_search and then memento_get if full note content is needed.
    """
    return build_tool_context(tool_name, file_path, cwd, session_id, host_id="mcp").to_dict()


@mcp.tool()
def memento_session_context(
    cwd: str = "",
    prompt: str = "",
    session_id: str = "",
    token_budget: int = 2000,
    include_status: bool = True,
    include_recent: bool = True,
    include_recall: bool = True,
    include_tool_context_preview: bool = False,
) -> dict:
    """Host-adapter primitive for one-call budgeted session context.

    This is not a general user-answering search tool. Hosts call it during
    session startup or before an agent turn to fetch a compact context packet
    that can replace separate memento_briefing, memento_recall, and status
    calls. For explicit recall/search requests, call memento_search and then
    memento_get if full note content is needed.
    """
    return build_session_context(
        cwd,
        prompt,
        session_id,
        token_budget,
        include_status,
        include_recent,
        include_recall,
        include_tool_context_preview,
        host_id="mcp",
    )


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
    origin: str | None = None,
) -> dict:
    """Store a new note in the memento vault (low-level primitive).

    Prefer the `/memento` skill in interactive Claude Code / Codex sessions —
    the skill writes the note to the local git-backed vault first, commits, and
    then syncs here via memento-remote-sync.py. Calling this tool directly from
    an interactive session skips the local vault and creates orphaned remote
    notes that the skill may later duplicate. This tool is intended for
    automated sync scripts and agents without skill support.

    Args:
        title: Note title (used as the filename slug).
        body: Note body content (markdown).
        note_type: Note type -- one of: discovery, decision, pattern, bugfix, tool, architecture. Legacy debugging/session aliases are normalized.
        tags: List of tags for categorization.
        certainty: Confidence level 1-5 (5 = proven fact, 1 = speculation).
        project: Project path or identifier this note belongs to.
        branch: Git branch this note was created on.
        session_id: Session identifier for traceability.
        validity_context: Conditions under which this note remains valid.
        supersedes: Title of a note this one replaces.
        origin: Optional integration path that created the note.

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
    explicit_origin = origin is not None
    contract = normalize_note_contract(
        note_type=note_type,
        tags=tags or [],
        certainty=certainty,
        source="mcp",
        origin=origin or "mcp_store",
        validity_context=validity_context,
        supersedes=supersedes,
        project=project,
        branch=branch,
        session_id=session_id,
    )

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock (another write in progress)"}

    try:
        target = vault / "notes" / f"{slugify(title.strip())}.md"
        if target.exists() and _note_payload_matches(
            target,
            title=title,
            body=sanitized_body,
            note_type=contract["note_type"],
            tags=contract["tags"],
            certainty=contract["certainty"],
            source=contract["source"],
            project=contract["project"],
            branch=contract["branch"],
            validity_context=contract["validity_context"],
            supersedes=contract["supersedes"],
            session_id=contract["session_id"],
            origin=contract["origin"] if explicit_origin else None,
        ):
            rel_path = str(target.relative_to(vault))
            log_retrieval("mcp", "store_idempotent", title=title, path=rel_path)
            return {
                "path": rel_path,
                "title": title.strip(),
                "created": False,
                "idempotent": True,
            }

        path = write_note(
            vault,
            title=title.strip(),
            body=sanitized_body,
            note_type=contract["note_type"],
            tags=contract["tags"],
            certainty=contract["certainty"],
            source=contract["source"],
            origin=contract["origin"],
            validity_context=contract["validity_context"],
            supersedes=contract["supersedes"],
            project=contract["project"],
            branch=contract["branch"],
            session_id=contract["session_id"],
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


def _project_slug_from_note(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None
    project_match = re.search(r"^project:\s*(.+)$", parts[1], re.MULTILINE)
    if not project_match:
        return None
    project = project_match.group(1).strip().strip("\"'")
    return slugify(Path(project).name) or None


def _remove_project_index_note(vault: Path, project_slug: str, note_name: str) -> None:
    project_file = vault / "projects" / f"{project_slug}.md"
    if not project_file.exists():
        return
    note_line = f"- [[{note_name}]]"
    lines = project_file.read_text(encoding="utf-8").splitlines()
    filtered = [line for line in lines if line.strip() != note_line]
    if filtered != lines:
        project_file.write_text("\n".join(filtered).rstrip() + "\n", encoding="utf-8")


@mcp.tool()
def memento_replace_note(
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
    origin: str | None = None,
) -> dict:
    """Replace an existing note at a known vault-relative path.

    This low-level sync primitive is intentionally path-scoped: callers must
    first identify the remote note they are resolving. It is used by remote
    sync's explicit local-wins conflict resolution path and will not create a
    new note when the path is missing.
    """
    if not path or not path.strip():
        return {"error": "path is required"}
    if not title or not title.strip():
        return {"error": "title is required"}
    if not body or not body.strip():
        return {"error": "body is required"}

    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock (another write in progress)"}

    try:
        target_path = (vault / path.strip()).resolve()
        previous_project_slug = None
        if target_path != vault and vault in target_path.parents:
            previous_project_slug = _project_slug_from_note(target_path)
        target = replace_note_at_path(
            vault,
            path.strip(),
            title=title.strip(),
            body=sanitize_secrets(body),
            note_type=note_type,
            tags=tags or [],
            certainty=certainty,
            source="mcp",
            origin=origin or "mcp_replace_note",
            validity_context=validity_context,
            supersedes=supersedes,
            project=project,
            branch=branch,
            session_id=session_id,
        )
    except (ValueError, FileNotFoundError) as exc:
        release_vault_write_lock()
        return {"error": str(exc)}
    except Exception as exc:
        release_vault_write_lock()
        return {"error": f"replace failed: {exc}"}

    try:
        rel_path = str(target.relative_to(vault))
        project_slug = slugify(Path(project).name) if project else None
        if previous_project_slug and previous_project_slug != project_slug:
            _remove_project_index_note(vault, previous_project_slug, target.stem)
        if project_slug:
            summary = f"MCP replace: {title.strip()[:80]}"
            update_project_index(vault, project_slug, target.stem, summary)
        log_retrieval("mcp", "replace_note", title=title, path=rel_path)
        return {"path": rel_path, "title": title.strip(), "replaced": True}
    finally:
        release_vault_write_lock()


@mcp.tool()
def memento_store_smart(
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
) -> dict:
    """Smart-store a note by searching for close matches before writing.

    Use this when the caller wants duplicate/update/supersede suggestions
    before writing a new note. The tool returns a decision object with the
    best candidate paths and reasons.
    """
    if not title or not title.strip():
        return {"error": "title is required"}
    if not body or not body.strip():
        return {"error": "body is required"}

    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock (another write in progress)"}

    try:
        return write_smart_store_note(
            title=title.strip(),
            body=sanitize_secrets(body),
            note_type=note_type,
            tags=tags,
            certainty=certainty,
            project=project,
            branch=branch,
            session_id=session_id,
            validity_context=validity_context,
            supersedes=supersedes,
            origin=origin,
        )
    finally:
        release_vault_write_lock()


@mcp.tool()
def memento_daily_snapshot(
    date: str,
    repo_slug: str,
    content: str,
    frontmatter_extra: dict | None = None,
    supersede: bool = False,
) -> dict:
    """Write a structured per-repo daily snapshot (low-level write primitive).

    Use this only for integrations that need deterministic path-controlled
    daily snapshot files, such as orra's vault-bridge. Do not use it for
    ordinary notes, interactive memory capture, session triage, or topical
    recall; use the `/memento` skill or memento_capture/memento_store only when
    their own selection guidance applies.

    Writes a deterministic-filename note at notes/daily-<date>-<repo_slug>.md
    rather than a title-slugged note. Unlike memento_store, the filename is
    owned by the caller via date plus repo_slug, so read-back is a plain
    memento_get by path.

    Append-only: re-writing the same (date, repo_slug) pair requires
    supersede=True, which writes daily-<date>-<repo_slug>-v<n>.md with a
    supersedes chain back to the original. Preserves the vault append-only
    invariant.

    Args:
        date: ISO date string YYYY-MM-DD.
        repo_slug: Repo identifier, matches [a-z0-9][a-z0-9_-]*
            (e.g. care_git, fundid, memento-vault).
        content: Markdown body (no frontmatter — the tool manages it).
        frontmatter_extra: Optional dict of extra frontmatter fields to merge.
            Managed keys (title, type, tags, source, certainty, date,
            repo_slug, supersedes) are stripped if present.
        supersede: If True and a snapshot exists for (date, repo_slug), write
            a -v<n>.md variant with a supersedes link. If False and one exists,
            return reason: already_exists.

    Returns:
        On success: {"path": "notes/daily-...", "supersedes": "daily-..." | None,
        "version": 1|N}.
        On error: {"error": "...", "reason": "invalid_date" | "invalid_repo_slug"
        | "empty_content" | "already_exists" | "write_failed"}.
    """
    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}", "reason": "vault_missing"}

    if not acquire_vault_write_lock():
        return {
            "error": "Could not acquire vault write lock (another write in progress)",
            "reason": "lock_timeout",
        }

    try:
        try:
            result = write_daily_snapshot(
                vault_path=vault,
                date=date,
                repo_slug=repo_slug,
                content=content,
                frontmatter_extra=frontmatter_extra,
                supersede=supersede,
            )
        except OSError as exc:
            log_retrieval("mcp", "daily_snapshot_write_failed", error=str(exc))
            return {"error": f"write failed: {exc}", "reason": "write_failed"}

        if "error" in result:
            log_retrieval(
                "mcp",
                "daily_snapshot_rejected",
                date=date,
                repo_slug=repo_slug,
                reason=result.get("reason"),
            )
            return result

        log_retrieval(
            "mcp",
            "daily_snapshot",
            date=date,
            repo_slug=repo_slug,
            path=result["path"],
            version=result["version"],
        )
        return result
    finally:
        release_vault_write_lock()


@mcp.tool()
def memento_status() -> dict:
    """Get vault health/status: note counts, project count, config summary.

    Use this for operational checks, setup debugging, and confirming whether the
    vault/search backend is available. Do not use it to answer questions about
    prior decisions, project history, or note content; use memento_search and
    then memento_get for recall/content.

    Returns:
        Dict with vault_path, note_count, project_count, fleeting_count, and key config values.
    """
    config = get_config()
    vault = get_vault()

    vault_exists = vault.exists()

    # Read vault_id only if vault exists — get_vault_id() creates dirs as a side effect
    vault_id = None
    if vault_exists:
        identity_file = vault / "vault-identity.json"
        if identity_file.exists():
            vault_id = get_vault_id()

    status = {
        "vault_id": vault_id,
        "vault_path": str(vault),
        "vault_exists": vault_exists,
        "qmd_available": has_qmd(),
    }

    if not vault_exists:
        status["automation_memory"] = build_automation_memory_readiness(
            config=config,
            vault=vault,
            qmd_available=status["qmd_available"],
        )
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
    status["automation_memory"] = build_automation_memory_readiness(
        config=config,
        vault=vault,
        qmd_available=status["qmd_available"],
    )

    log_retrieval("mcp", "status")
    return status


@mcp.tool()
def memento_get(path: str) -> dict:
    """Read the full content of a specific vault note by path or name.

    Use this after memento_search when a search result path needs full content,
    or directly when the user or another tool already provided an exact note
    path/name. Do not use it for topical discovery when the note path is
    unknown; search first with memento_search.

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
        content = full_path.read_text(errors="replace")
        # Extract title from frontmatter
        title = Path(path).stem
        title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip().strip('"').strip("'")

        logged_path = _vault_relative_access_path(vault, path)
        result = {
            "path": logged_path or path,
            "title": _strip_injection(title),
            "content": _strip_injection(content),
        }
        record_access([result["path"]], hook="mcp", tool="get", query=path, result_count=1)
        return result

    # Fall back to QMD get
    result = qmd_get(path)
    if result:
        result_payload = {
            "path": _vault_relative_access_path(vault, result.get("path", path)) or result.get("path", path),
            "title": _strip_injection(result.get("title", "")),
            "content": _strip_injection(result.get("content", "")),
        }
        record_access([result_payload["path"]], hook="mcp", tool="get", query=path, result_count=1)
        return result_payload

    # Fall back to remote vault if configured
    from memento.remote_client import is_remote, get as remote_get

    if is_remote():
        remote_result = remote_get(path)
        if remote_result:
            result_payload = {
                "path": _vault_relative_access_path(vault, remote_result.get("path", path))
                or remote_result.get("path", path),
                "title": _strip_injection(remote_result.get("title", "")),
                "content": _strip_injection(remote_result.get("content", "")),
            }
            record_access([result_payload["path"]], hook="mcp", tool="get", query=path, result_count=1)
            return result_payload

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
    fleeting_only: bool = False,
) -> dict:
    """Capture a session's knowledge into the vault (low-level write primitive).

    This is the MCP equivalent of the SessionEnd hook. Use it when your agent
    doesn't have native hook support (Cursor, Windsurf, etc.) or when an
    automation is explicitly implementing session capture. Do not call this for
    ordinary interactive "remember this" requests from Claude Code or Codex —
    those have SessionEnd hooks and the `/memento` skill that handle capture via
    the local-first flow.

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
        agent: Which agent produced this session (claude, opencode, pi, codex, cursor, windsurf).
        fleeting_only: If true, only write a fleeting log entry and project index
            update — do not create a permanent atomic note. Used by remote hooks
            for non-substantial sessions to match local triage semantics.

    Returns:
        Dict with capture results: notes written, project updated, or error.
    """
    if not session_summary and not transcript_path:
        return {"error": "Provide session_summary or transcript_path"}

    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    requested_session_id = session_id
    session_id = session_id or uuid.uuid4().hex[:12]

    # Mode 1: transcript file parsing via adapter (local/stdio transport only)
    if transcript_path:
        # Reject transcript_path over HTTP — remote callers must not trigger
        # server-side file reads. They should send session_summary instead.
        if _active_transport != "stdio":
            return {
                "error": "transcript_path is only supported in local (stdio) mode. Send session_summary for remote capture."
            }

        if not os.path.exists(transcript_path):
            return {"error": f"Transcript file not found: {transcript_path}"}

        # Restrict to known agent transcript directories (proper containment check)
        candidate = Path(transcript_path).resolve()
        xdg_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        allowed_roots = [
            (Path.home() / ".claude").resolve(),
            (Path.home() / ".codex").resolve(),
            (Path.home() / ".cursor").resolve(),
            (Path.home() / ".codeium").resolve(),
            (Path.home() / ".pi" / "agent" / "sessions").resolve(),
            (Path.home() / ".pi" / "agent" / "subagents").resolve(),
            (xdg_data_home / "opencode").resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]
        pi_session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        if pi_session_dir:
            allowed_roots.append(Path(pi_session_dir).expanduser().resolve())
        for raw_root in os.environ.get("MEMENTO_PI_TRANSCRIPT_ROOTS", "").split(os.pathsep):
            if raw_root.strip():
                allowed_roots.append(Path(raw_root.strip()).expanduser().resolve())
        if not any(candidate == root or root in candidate.parents for root in allowed_roots):
            return {"error": "transcript_path must be inside a known agent directory"}

        try:
            from memento.adapters import parse_transcript

            meta = parse_transcript(
                transcript_path,
                agent=agent if agent != "unknown" else None,
                session_id=requested_session_id,
            )
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

    # Idempotency check (read-only, no lock needed): if this session was already
    # captured, return prior result. Prevents duplicate notes on HTTP retry/timeout.
    notes_dir = vault / "notes"
    if notes_dir.exists():
        for existing in notes_dir.glob("*.md"):
            try:
                head = existing.read_text(errors="replace")[:500]
                if f"session_id: {session_id}" in head:
                    return {
                        "session_id": session_id,
                        "note_path": str(existing.relative_to(vault)),
                        "project": project_slug,
                        "deduplicated": True,
                    }
            except OSError:
                continue

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock"}

    try:
        # Write fleeting note (always — matches local triage behavior)
        utc_now = datetime.now(timezone.utc)
        today = utc_now.strftime("%Y-%m-%d")
        fleeting_result = append_fleeting_session(
            vault,
            session_id,
            cwd=cwd,
            branch=branch,
            agent=agent,
            files_edited=files_edited,
            now=utc_now,
        )
        if fleeting_result["already_logged"]:
            return {
                "session_id": session_id,
                "project": project_slug,
                "fleeting": fleeting_result["fleeting"],
                "deduplicated": True,
            }

        if fleeting_only:
            # Ensure project index exists and log session (no [[note]] link)
            if project_slug != "unknown":
                project_dir = vault / "projects"
                project_dir.mkdir(parents=True, exist_ok=True)
                project_file = project_dir / f"{project_slug}.md"
                if not project_file.exists():
                    project_file.write_text(
                        f"---\ntitle: {project_slug}\nproject: {project_slug}\n---\n\n## Notes\n\n## Sessions\n\n"
                    )
                session_line = f"- {today} `{session_id}` — {sanitized_summary[:80]}"
                content = project_file.read_text()
                if session_id not in content:
                    content = append_project_session_line(content, session_line)
                    project_file.write_text(content)

            log_retrieval("mcp", "capture_fleeting", session_id=session_id, agent=agent, project=project_slug)
            return {
                "session_id": session_id,
                "project": project_slug,
                "fleeting": fleeting_result["fleeting"],
            }

        # Write atomic note from summary (substantial sessions only)
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
            origin=f"mcp_capture:{agent}",
            project=cwd or None,
            branch=branch or None,
            session_id=session_id,
        )

        # Update project index with real note link (not for fleeting-only)
        if project_slug != "unknown":
            (vault / "projects").mkdir(parents=True, exist_ok=True)
            summary_line = f"MCP capture ({agent}): {title_text}"
            update_project_index(vault, project_slug, note_path.stem, summary_line)

        log_retrieval("mcp", "capture", session_id=session_id, agent=agent, project=project_slug)

        return {
            "session_id": session_id,
            "note_path": str(note_path.relative_to(vault)),
            "project": project_slug,
            "fleeting": fleeting_result["fleeting"],
        }

    finally:
        release_vault_write_lock()


@mcp.tool()
def memento_capture_run_lesson(candidate: dict, approve_write: bool = False) -> dict:
    """Queue or explicitly write a typed automated-run lesson candidate.

    This is the automation-safe post-run lesson contract. Candidates must be
    compact, sanitized summaries with provenance fields (external system, run
    id, artifact refs, repo/project/branch/ticket/slice, outcome, lesson type,
    evidence summary, certainty, validity context, and related refs). Raw logs,
    transcripts, run ledgers, proofs, stdout/stderr, and patch blobs are
    rejected. By default the candidate is queued for review outside the vault;
    pass approve_write=True only when the caller explicitly approved writing a
    curated lesson note.
    """
    return capture_automated_run_lesson(candidate, approve_write=approve_write)


@mcp.tool()
def memento_synthesize_failures(
    run_summaries: list[dict] | dict,
    approve_writes: bool = False,
    project: str = "",
    branch: str = "",
    session_id: str = "",
    max_candidates: int = 20,
) -> dict:
    """Synthesize lessons/actions from sanitized external run summaries.

    Dry-run is the default and performs no vault writes or external repo
    mutations. The input must be compact Rondo/Beislið-style run summaries;
    raw run stores, logs, transcripts, stdout/stderr, proofs, and ledger dumps
    are rejected. Output groups memory, process, agent, harness, environment,
    and requirement failures and proposes concrete note/issue/gate/docs
    improvements. Pass approve_writes=True only when the caller explicitly
    approves storing the candidate lesson notes via typed automated-run lesson
    capture; advisory actions are never executed by this tool.
    """
    result = synthesize_failure_batch(run_summaries, max_candidates=max_candidates)
    if result.get("error"):
        return result

    if not approve_writes:
        return result

    vault = get_vault()
    if not vault.exists():
        return {**result, "error": f"Vault not found at {vault}", "reason": "vault_missing"}

    write_results = []
    for candidate in result.get("candidate_lessons", []):
        lesson_candidate = lesson_candidate_from_batch_candidate(
            candidate,
            project=project,
            branch=branch,
            session_id=session_id,
        )
        lesson_result = capture_automated_run_lesson(lesson_candidate, approve_write=True)
        if "write_result" in lesson_result:
            write_result = dict(lesson_result["write_result"])
            write_result.setdefault("created", lesson_result.get("created", False))
            write_results.append(write_result)
        else:
            write_results.append(lesson_result)

    return {
        **result,
        "dry_run": False,
        "writes_approved": True,
        "write_results": write_results,
        "approval_required": "Candidate lessons were stored because approve_writes=true; advisory actions were not executed.",
    }


@mcp.tool()
def memento_preserve(
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
    session_id: str = "",
) -> dict:
    """Archive a file or directory bundle under ``archive/<slug>/``.

    Preserve artifact bundles intact instead of decomposing them into atomic
    notes. copy by default; move only when explicitly requested. When running
    over remote HTTP, preserve only paths rooted in the server cwd or vault —
    arbitrary server-side file reads are rejected.

    Args:
        path: File or directory to preserve.
        title: Optional human-readable title for the bundle.
        slug: Optional archive slug.
        project: Optional project slug or path for index linking.
        description: Optional summary/provenance note.
        tags: Optional tags to annotate the bundle index.
        move: When true, move the source instead of copying it.
        include_manifest: Write a manifest with hashes/provenance (default: True).
        link_project_index: Update the relevant project index when a project can be detected.
        cwd: Original cwd for provenance when available.
        branch: Original branch for provenance when available.
        session_id: Original session id for provenance when available.

    Returns:
        Dict with archive, manifest, and optional project-index paths.
    """
    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock"}

    try:
        result = preserve_bundle(
            vault,
            path,
            title=title,
            slug=slug,
            project=project,
            description=description,
            tags=tags,
            move=move,
            include_manifest=include_manifest,
            link_project_index=link_project_index,
            cwd=cwd,
            branch=branch,
            session_id=session_id,
            transport=_active_transport,
        )
        if "error" in result:
            log_retrieval("mcp", "preserve_failed", path=path, error=result["error"])
            return result

        log_retrieval(
            "mcp",
            "preserve",
            path=path,
            archive_path=result.get("archive_path"),
            file_count=result.get("file_count"),
            moved=move,
        )
        return result
    finally:
        release_vault_write_lock()


@mcp.tool()
def memento_list(include_hash: bool = True) -> list[dict]:
    """List all notes in the vault with optional content hashes.

    Use this as a lightweight sync/inventory primitive for clients diffing local
    vs remote vault state without fetching full content. Do not use it for
    topical recall, project history questions, or reading note content; use
    memento_search and memento_get instead.

    Args:
        include_hash: Include sha256 hash of raw file content (default: True).

    Returns:
        List of dicts with path, title, and optionally hash.
    """
    import hashlib

    vault = get_vault()
    notes_dir = vault / "notes"
    if not notes_dir.exists():
        return []

    results = []
    for f in sorted(notes_dir.glob("*.md")):
        entry = {"path": f"notes/{f.name}"}
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue

        title_match = re.search(r"^title:\s*(.+)$", raw, re.MULTILINE)
        entry["title"] = title_match.group(1).strip().strip("\"'") if title_match else f.stem

        if include_hash:
            entry["hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        results.append(entry)

    log_retrieval("mcp", "list", count=len(results))
    return results


@mcp.tool()
def memento_reindex() -> dict:
    """Rebuild the search index from all markdown files in the vault.

    Triggers a full reindex of FTS5 and vector tables. Use this after
    bulk-adding notes outside the normal write path (e.g., git pull,
    Obsidian sync, manual file copy) or when status/search evidence indicates
    a stale index. Do not use it as a normal response to broad or empty search
    misses; refine the query or use memento_get for known paths instead.

    Returns:
        Dict with reindex status and note count.
    """
    from memento.search_backend import get_backend

    try:
        config = get_config()
        vault = get_vault()
        collection = config.get("qmd_collection", "memento")

        backend = get_backend()
        ok = backend.reindex(collection)

        if not ok:
            log_retrieval("mcp", "reindex_failed")
            return {"error": "reindex failed — backend returned false"}

        # Count markdown files across vault content dirs
        count = 0
        for subdir in ("notes", "fleeting", "projects"):
            d = vault / subdir
            if d.exists():
                count += len(list(d.glob("*.md")))

        log_retrieval("mcp", "reindex", notes_indexed=count)
        return {"status": "ok", "notes_indexed": count}

    except Exception as exc:
        log_retrieval("mcp", "reindex_error", error=str(exc))
        return {"error": f"reindex failed: {type(exc).__name__}: {exc}"}


def main():
    """Run the MCP server.

    Transport is selected via --transport flag or MEMENTO_TRANSPORT env var.
    Host/port are configured via MEMENTO_HOST/MEMENTO_PORT env vars or
    passed to the FastMCP constructor at build time.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Memento Vault MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("MEMENTO_TRANSPORT", "stdio"),
        help="Transport protocol (default: stdio, env: MEMENTO_TRANSPORT)",
    )
    args = parser.parse_args()

    # Record the active transport so tools can check it at request time
    global _active_transport
    _active_transport = args.transport

    # Fail closed: refuse to start HTTP transport without auth on non-local interfaces
    if args.transport in ("sse", "streamable-http"):
        host = _bind_host()
        api_key = os.environ.get("MEMENTO_API_KEY") or get_config().get("api_key")
        if not api_key and host not in ("127.0.0.1", "localhost", "::1"):
            print(
                "[memento] FATAL: refusing to start HTTP transport on "
                f"{host} without MEMENTO_API_KEY set.\n"
                "Set MEMENTO_API_KEY or bind to localhost (MEMENTO_HOST=127.0.0.1).",
                file=sys.stderr,
            )
            sys.exit(1)

    # For HTTP transports with auth, we wrap the ASGI app with bearer token
    # middleware. We can't use MCP SDK's token_verifier because it requires
    # OAuth AuthSettings (issuer_url etc.) which doesn't fit simple bearer tokens.
    if args.transport in ("sse", "streamable-http"):
        from memento.auth import create_auth_provider, NoAuth

        auth_provider = create_auth_provider()
        if not isinstance(auth_provider, NoAuth):
            # Get the Starlette app that FastMCP would build, wrap it
            if args.transport == "streamable-http":
                inner_app = mcp.streamable_http_app()
            else:
                inner_app = mcp.sse_app()

            async def auth_app(scope, receive, send):
                if scope["type"] == "http":
                    headers = dict(scope.get("headers", []))
                    auth_header = headers.get(b"authorization", b"").decode()
                    identity = auth_provider.authenticate(auth_header)
                    if identity is None:
                        body = b'{"error": "Unauthorized"}'
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 401,
                                "headers": [
                                    [b"content-type", b"application/json"],
                                    [b"content-length", str(len(body)).encode()],
                                ],
                            }
                        )
                        await send({"type": "http.response.body", "body": body})
                        return
                await inner_app(scope, receive, send)

            import uvicorn

            uvicorn.run(
                auth_app,
                host=_bind_host(),
                port=int(os.environ.get("MEMENTO_PORT", "8745")),
                log_level="warning",
            )
            return

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
