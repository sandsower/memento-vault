"""Bounded tool-using retrieval agent (MEM-161).

One-shot top-k injection is the retrieval pipeline's ceiling: a single
qmd_search + enhance_results pass either finds the right notes or it doesn't.
This module upgrades that with a small ReAct-style loop driven through
``memento.llm.llm_complete`` (any configured backend, including cheaper
routed models via the ``pi`` provider): the model calls search/query/related/
get tools in-process, sees the observation, and repeats until it signals
``done`` with a list of note paths -- or a hard bound is hit.

This tier is strictly additive. ``agentic_retrieve`` never raises; on any
protocol or provider failure (malformed JSON after one retry, timeout,
provider error, tool-call cap exceeded, or empty results) it returns ``[]``
so the caller falls back to the existing one-shot pipeline unchanged.

Wired into memento.lifecycle.run_deferred_briefing_search, gated by the
``agentic_retrieval_enabled`` config flag (default False).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from memento.config import get_config, get_vault
from memento.graph import build_related_view
from memento.llm import LLMResult, llm_complete
from memento.query import QueryValidationError, build_metadata_filter, query_notes, read_note_record
from memento.retrieval_policy import ExplicitSearchRequest, ExplicitSearchRuntime
from memento.search import qmd_get

MAX_TOOL_CALLS = 6
WALL_CLOCK_SECONDS = 60
OBSERVATION_TRUNCATE_BYTES = 2048
MAX_MALFORMED_RETRIES = 1
# Per-LLM-call timeout inside the overall wall clock -- generous enough for a
# cheap routed model to think, but never allowed to blow the loop budget.
PER_CALL_TIMEOUT_SECONDS = 30

SYSTEM_PROMPT = """You are a bounded retrieval agent for the Memento vault, a persistent knowledge store of markdown notes.

Given a query, find the vault note paths most relevant to it by calling tools, then stop. Prefer fewer, precise calls over broad exploration.

Respond with EXACTLY ONE JSON object per turn. No prose, no markdown code fences, nothing else. Two possible shapes:

1. A tool call:
   {"tool": "search", "args": {...}}

2. A completion signal, once you have enough evidence (this ends the loop):
   {"done": true, "results": ["notes/some-note.md", "notes/other-note.md"]}

Available tools:

- search: {"query": str, "limit": int (default 5), "semantic": bool (default false), "type": str, "tag": str, "certainty_min": int, "certainty_max": int, "date_start": str, "date_end": str}
  Ranked keyword/semantic search over vault notes, with optional metadata filters.

- query: {"project": str, "note_type": str, "tag": str, "source": str, "certainty_min": int, "certainty_max": int, "date_start": str, "date_end": str, "branch": str, "session_id": str, "limit": int}
  Typed metadata scan over vault notes (no ranking) -- use for exact filter-based lookups (e.g. "all decision notes from project X").

- related: {"note": str, "direction": "out"|"in"|"both" (default "both"), "depth": int (default 1)}
  Wikilink graph neighborhood and supersession chain for one already-known note.

- get: {"path": str}
  Read the full content of one already-known note path or name.

You have at most 6 tool calls total. If a tool call returns an error, adjust your next call -- do not repeat the exact same call. When you are done, "results" must be vault note paths (e.g. "notes/foo.md"), not titles or summaries.
"""


def _truncate(text: str, limit: int = OBSERVATION_TRUNCATE_BYTES) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "...[truncated]"


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Leniently extract a single JSON object from an LLM turn.

    Mirrors memento.lifecycle._parse_deep_recall_response's lenient
    extraction (direct parse, then markdown code fence, then bare pattern) --
    small/cheap routed models are not always disciplined about staying
    inside the requested response shape.
    """
    if not raw:
        return None

    def _try(text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    direct = _try(raw.strip())
    if direct is not None:
        return direct

    import re

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        parsed = _try(match.group(1))
        if parsed is not None:
            return parsed

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        parsed = _try(match.group(0))
        if parsed is not None:
            return parsed

    return None


def _normalize_note_path(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return path
    if not path.endswith(".md"):
        return f"notes/{path}.md"
    if not path.startswith("notes/") and "/" not in path:
        return f"notes/{path}"
    return path


def _guarded_full_path(vault: Path, path: str) -> Path | None:
    """Resolve *path* under *vault*, refusing traversal outside it."""
    full_path = (vault / path).resolve()
    vault_resolved = vault.resolve()
    if full_path != vault_resolved and vault_resolved not in full_path.parents:
        return None
    return full_path


def _tool_search(args: dict[str, Any]) -> dict[str, Any]:
    """search: ranked keyword/semantic search with optional typed filters."""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}

    try:
        limit = max(1, min(int(args.get("limit", 5)), 20))
    except (TypeError, ValueError):
        limit = 5

    request = ExplicitSearchRequest(query=query, limit=limit, semantic=bool(args.get("semantic", False)))
    result = ExplicitSearchRuntime().search(request)

    filter_kwargs = {
        "note_type": str(args.get("type") or ""),
        "tag": str(args.get("tag") or ""),
        "certainty_min": args.get("certainty_min"),
        "certainty_max": args.get("certainty_max"),
        "date_start": str(args.get("date_start") or ""),
        "date_end": str(args.get("date_end") or ""),
    }
    has_filters = any(v not in ("", None) for v in filter_kwargs.values())
    if has_filters and result.get("results"):
        try:
            predicate, filters_echo = build_metadata_filter(**filter_kwargs)
        except QueryValidationError as exc:
            return {"error": str(exc)}
        vault = get_vault()
        filtered = []
        for entry in result.get("results", []):
            record = read_note_record(vault, entry.get("path", ""))
            if record is not None and predicate(record):
                filtered.append(entry)
        result = dict(result)
        result["results"] = filtered[:limit]
        result.setdefault("metadata", {})["filters_applied"] = filters_echo

    return result


def _tool_query(args: dict[str, Any]) -> dict[str, Any]:
    """query: typed metadata scan (no ranking)."""
    vault = get_vault()
    try:
        limit = max(1, min(int(args.get("limit", 20)), 50))
    except (TypeError, ValueError):
        limit = 20
    return query_notes(
        vault,
        project=str(args.get("project") or ""),
        note_type=str(args.get("note_type") or ""),
        tag=str(args.get("tag") or ""),
        source=str(args.get("source") or ""),
        certainty_min=args.get("certainty_min"),
        certainty_max=args.get("certainty_max"),
        date_start=str(args.get("date_start") or ""),
        date_end=str(args.get("date_end") or ""),
        branch=str(args.get("branch") or ""),
        session_id=str(args.get("session_id") or ""),
        limit=limit,
    )


def _tool_related(args: dict[str, Any]) -> dict[str, Any]:
    """related: wikilink graph neighborhood + supersession chain."""
    note = str(args.get("note") or "").strip()
    if not note:
        return {"error": "note is required"}
    direction = args.get("direction") or "both"
    try:
        depth = int(args.get("depth", 1))
    except (TypeError, ValueError):
        depth = 1
    return build_related_view(note, direction=direction, depth=depth)


def _tool_get(args: dict[str, Any]) -> dict[str, Any]:
    """get: read the full content of one known note path/name.

    Reimplements memento.mcp_server.memento_get's normalization/traversal-
    guard/fallback logic rather than importing it -- mcp_server imports
    memento.lifecycle at module load, and lifecycle is what wires this
    module in, so importing mcp_server here would be circular.
    """
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return {"error": "path is required"}

    vault = get_vault()
    normalized = _normalize_note_path(raw_path)
    full_path = _guarded_full_path(vault, normalized)
    if full_path is None:
        return {"error": "Invalid path: traversal outside vault"}

    if full_path.exists():
        content = full_path.read_text(errors="replace")
        return {"path": normalized, "title": Path(normalized).stem, "content": content}

    fallback = qmd_get(raw_path)
    if fallback:
        return fallback
    return {"error": f"note not found: {raw_path}"}


@dataclass
class AgenticRetrievalDeps:
    """Injectable dependencies for :func:`agentic_retrieve` -- real
    implementations by default, overridden by tests with fakes/scripts."""

    llm_complete: Callable[..., LLMResult] = llm_complete
    search: Callable[[dict[str, Any]], dict[str, Any]] = _tool_search
    query: Callable[[dict[str, Any]], dict[str, Any]] = _tool_query
    related: Callable[[dict[str, Any]], dict[str, Any]] = _tool_related
    get: Callable[[dict[str, Any]], dict[str, Any]] = _tool_get
    vault_loader: Callable[[], Path] = get_vault
    max_tool_calls: int = MAX_TOOL_CALLS
    wall_clock_seconds: float = WALL_CLOCK_SECONDS
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)


_TOOL_DISPATCH_NAMES = ("search", "query", "related", "get")


def _dispatch_tool(name: str, args: dict[str, Any], deps: AgenticRetrievalDeps) -> dict[str, Any]:
    if name not in _TOOL_DISPATCH_NAMES:
        return {"error": f"unknown tool: {name!r}. Valid tools: {', '.join(_TOOL_DISPATCH_NAMES)}"}
    tool_fn = getattr(deps, name)
    if not isinstance(args, dict):
        args = {}
    try:
        return tool_fn(args)
    except Exception as exc:  # tool-level failures are observations, not loop failures
        return {"error": str(exc)}


def _llm_config_for_agent(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "llm_backend": config.get("retrieval_agent_provider") or config.get("llm_backend", "claude"),
        "llm_model": config.get("retrieval_agent_model") or config.get("llm_model"),
    }


def _hydrate_results(paths: list[Any], deps: AgenticRetrievalDeps) -> list[dict[str, Any]]:
    """Turn the model's final path list into qmd_search-shaped result dicts.

    format_qmd_result/enhance_results (the deferred-briefing consumers) both
    expect dicts with path/title/score/snippet, not bare path strings.
    """
    vault = deps.vault_loader()
    hydrated: list[dict[str, Any]] = []
    seen: set[str] = set()
    count = len(paths) if isinstance(paths, list) else 0
    for index, raw_path in enumerate(paths if isinstance(paths, list) else []):
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        normalized = _normalize_note_path(raw_path)
        if normalized in seen:
            continue
        record = read_note_record(vault, normalized)
        if record is None:
            continue
        seen.add(normalized)
        # Synthetic descending score: preserves the model's own ranking
        # (first result = most relevant) for downstream sort/decay logic
        # that expects a score field, without pretending to be a real
        # retrieval score.
        score = 1.0 - (index / max(count, 1)) * 0.5
        hydrated.append(
            {
                "path": normalized,
                "title": record.get("title") or Path(normalized).stem,
                "snippet": "",
                "score": score,
            }
        )
    return hydrated


def agentic_retrieve(
    query: str,
    config: dict[str, Any] | None = None,
    deps: AgenticRetrievalDeps | None = None,
) -> list[dict[str, Any]]:
    """Run the bounded ReAct retrieval loop for *query*.

    Returns a list of qmd_search-shaped result dicts on success, or ``[]`` on
    ANY failure (malformed JSON after one retry, provider error/timeout,
    tool-call cap exceeded, or no results survive hydration) -- callers
    should treat an empty return as "fall back to the existing pipeline",
    never as a hard error.
    """
    query = (query or "").strip()
    if not query:
        return []

    resolved_config = dict(config) if config else dict(get_config())
    deps = deps or AgenticRetrievalDeps()
    llm_config = _llm_config_for_agent(resolved_config)

    started = deps._clock()
    transcript = f"{SYSTEM_PROMPT}\n\nQuery: {query}\n"
    tool_calls = 0
    malformed_retries = 0

    while tool_calls < deps.max_tool_calls:
        remaining = deps.wall_clock_seconds - (deps._clock() - started)
        if remaining <= 0:
            return []

        call_timeout = max(1, min(PER_CALL_TIMEOUT_SECONDS, remaining))
        result = deps.llm_complete(transcript, llm_config, timeout=call_timeout)
        if not result.ok:
            return []

        parsed = _extract_json_object(result.text)
        if parsed is None:
            if malformed_retries >= MAX_MALFORMED_RETRIES:
                return []
            malformed_retries += 1
            transcript += (
                f"\n\nASSISTANT: {result.text}\n"
                "OBSERVATION: ERROR - your last response was not a single valid JSON object. "
                "Reply with exactly one JSON object matching the protocol.\n"
            )
            continue

        if parsed.get("done"):
            return _hydrate_results(parsed.get("results"), deps)

        tool_name = parsed.get("tool")
        args = parsed.get("args") or {}
        observation = _dispatch_tool(tool_name, args, deps)
        tool_calls += 1
        observation_text = _truncate(json.dumps(observation, default=str))
        transcript += f"\n\nASSISTANT: {json.dumps(parsed)}\nOBSERVATION: {observation_text}\n"

    return []
