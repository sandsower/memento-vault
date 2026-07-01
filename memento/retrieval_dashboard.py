"""Retrieval debug dashboard/report generation.

Turns retrieval.jsonl telemetry into a lightweight local report that explains
why recall/tool-context/briefing/inception decisions were made and surfaces
sanitized behavior recommendations, while keeping raw transcripts and note
bodies out of view unless explicitly requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from memento import telemetry
from memento.config import get_vault
from memento.retrieval_policy import (
    DEEP_PIPELINE_MARKERS,
    SEARCH_MISS_REASONS,
    TOOL_CONTEXT_MISS_REASONS,
)
from memento.store import ACCESS_LOG_PATH, RETRIEVAL_LOG_PATH, TRIAGE_HEALTH_LOG_PATH, load_access_log_stats
from memento.utils import sanitize_secrets

DEFAULT_EVENT_LIMIT = 25
DEFAULT_NOTE_LIMIT = 10
DEFAULT_RETRIEVAL_LOG = Path(RETRIEVAL_LOG_PATH)
DEFAULT_TRIAGE_HEALTH_LOG = Path(TRIAGE_HEALTH_LOG_PATH)
DEFAULT_ACCESS_LOG = Path(ACCESS_LOG_PATH)

_TRIAGE_HEALTH_SUCCESS = telemetry.TRIAGE_HEALTH_SUCCESS_ACTIONS
_TRIAGE_HEALTH_FAILURE = telemetry.TRIAGE_HEALTH_FAILURE_ACTIONS

_CONCRETE_PATTERNS = (
    re.compile(r"(?:[A-Za-z]:)?[\\/][^\s]+"),
    re.compile(r"\b[\w.-]+\.(?:py|md|yaml|yml|json|toml|ts|js|go|rs|rb|sh|cfg|ini|txt)\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2,}-\d+\b"),
)

_PROJECT_HISTORY_MARKERS = (
    "project history",
    "what happened",
    "what changed",
    "catch me up",
    "timeline",
    "recap",
    "status update",
    "summarize",
    "give me context",
    "why did",
    "how did",
    "current state",
    "roadmap",
)

_RECOMMENDATION_MIN_COUNT = 3
_RECOMMENDATION_MIN_SHARE = 0.3
_RECOMMENDATION_LATENCY_MS = 250
_SEARCH_GET_FOLLOWUP_WINDOW_SECONDS = 15 * 60


def _parse_ts(value: Any) -> datetime | None:
    return telemetry.parse_timestamp_utc(value)


def _format_ts(value: Any) -> str:
    return telemetry.format_timestamp_utc(value)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _avg(values: list[int | float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _p95(values: list[int | float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return float(ordered[index])


def _pct(part: int, total: int) -> str:
    return f"{part / total * 100:.0f}%" if total else "n/a"


def _load_jsonl(path: Path, since_days: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days) if since_days is not None else None
    if cutoff is not None:
        return list(telemetry.iter_recent_jsonl(path, cutoff))
    return list(telemetry.iter_jsonl(path))


def load_retrieval_entries(log_path: str | Path | None = None, since_days: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(Path(log_path or DEFAULT_RETRIEVAL_LOG), since_days=since_days)


def load_triage_health_entries(
    log_path: str | Path | None = None, since_days: int | None = None
) -> list[dict[str, Any]]:
    return _load_jsonl(Path(log_path or DEFAULT_TRIAGE_HEALTH_LOG), since_days=since_days)


def load_access_entries(log_path: str | Path | None = None, since_days: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(Path(log_path or DEFAULT_ACCESS_LOG), since_days=since_days)


def load_benchmark_outcomes(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load sanitized benchmark outcome/classification summaries for report enrichment."""
    if path is None:
        return []
    source = Path(path).expanduser()
    if not source.exists():
        return []
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        if text.startswith("[") or text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                values = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                if isinstance(payload, dict):
                    values = payload.get("classifications") or payload.get("outcomes") or payload.get("summaries") or []
                else:
                    values = payload
        else:
            values = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _query_digest(entry: dict[str, Any]) -> str | None:
    for key in ("query", "prompt", "topic", "body"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            normalized = " ".join(value.split())
            return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return None


def _query_preview(entry: dict[str, Any], limit: int = 160) -> str | None:
    for key in ("query", "prompt", "topic", "body"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_secrets(" ".join(value.split()))[:limit]
    return None


def _safe_error(value: Any) -> str | None:
    if value is None:
        return None
    text = sanitize_secrets(str(value))
    return text[:500] if len(text) > 500 else text


def _candidate_summary(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    return {
        "path": str(candidate.get("path") or ""),
        "title": str(candidate.get("title") or ""),
        "score": round(float(candidate.get("score") or 0.0), 4),
        "decision": str(candidate.get("decision") or "candidate"),
    }


def _collect_note_keys(entries: list[dict[str, Any]], access_stats: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        key = value.strip()
        if not key or key in seen:
            return
        seen.add(key)
        keys.append(key)

    for entry in entries:
        for field in ("path", "file_path", "top_path"):
            add(entry.get(field))
        for field in ("injected_paths", "injected_titles"):
            values = entry.get(field)
            if isinstance(values, list):
                for value in values:
                    add(value)
        candidates = entry.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    add(candidate.get("path"))
    for path in access_stats.keys():
        add(path)
    return keys


def _candidate_note_paths(vault: Path, value: str) -> list[Path]:
    raw = value.strip().replace("\\", "/")
    stem = Path(raw).stem if raw else ""
    names = [raw]
    if raw.endswith(".md"):
        names.append(raw[:-3])
    if stem:
        names.extend([stem, f"{stem}.md", f"notes/{stem}.md"])
    if raw:
        names.extend([f"notes/{raw}", f"notes/{raw}.md"])

    paths: list[Path] = []
    seen: set[str] = set()
    for name in names:
        if not name:
            continue
        candidate = Path(name)
        resolved = candidate if candidate.is_absolute() else vault / candidate
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    return paths


def _resolve_note_path(vault: Path, value: str) -> Path | None:
    for candidate in _candidate_note_paths(vault, value):
        if candidate.exists():
            return candidate
    return None


def _read_note_frontmatter(note_path: Path) -> dict[str, Any]:
    date = None
    certainty: int | None = None
    project = None
    note_type = None
    tags: list[str] = []
    try:
        with note_path.open(encoding="utf-8") as f:
            in_frontmatter = False
            for line in f:
                stripped = line.strip()
                if stripped == "---":
                    if not in_frontmatter:
                        in_frontmatter = True
                        continue
                    break
                if not in_frontmatter:
                    continue
                if stripped.startswith("date:"):
                    date = stripped[5:].strip().strip('"').strip("'")
                elif stripped.startswith("certainty:"):
                    raw_certainty = stripped[10:].strip()
                    try:
                        certainty = int(raw_certainty)
                    except ValueError:
                        certainty = None
                elif stripped.startswith("project:"):
                    project = stripped[8:].strip().strip('"').strip("'")
                elif stripped.startswith("type:"):
                    note_type = stripped[5:].strip().strip('"').strip("'")
                elif stripped.startswith("tags:"):
                    raw_tags = stripped[5:].strip()
                    if raw_tags.startswith("[") and raw_tags.endswith("]"):
                        tags = [t.strip().strip('"').strip("'") for t in raw_tags[1:-1].split(",") if t.strip()]
    except OSError:
        pass

    parsed_date = None
    if date:
        parsed_date = _parse_ts(date)
    if parsed_date is None:
        try:
            parsed_date = datetime.fromtimestamp(note_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            parsed_date = None

    return {
        "date": date,
        "parsed_date": parsed_date,
        "certainty": certainty,
        "project": project,
        "type": note_type,
        "tags": tags,
    }


def _age_days(parsed_date: datetime | None) -> int | None:
    if parsed_date is None:
        return None
    delta = datetime.now(timezone.utc) - parsed_date.astimezone(timezone.utc)
    return max(0, delta.days)


def _note_insights(vault: Path, entries: list[dict[str, Any]], access_stats: dict[str, Any]) -> list[dict[str, Any]]:
    note_keys = _collect_note_keys(entries, access_stats)
    insights: list[dict[str, Any]] = []

    for key in note_keys:
        resolved = _resolve_note_path(vault, key)
        if resolved is None:
            continue
        meta = _read_note_frontmatter(resolved)
        rel_path = resolved.relative_to(vault).as_posix() if resolved.is_relative_to(vault) else str(resolved)
        lookup_keys = (key, rel_path, resolved.name, resolved.stem)
        bucket = next((access_stats[k] for k in lookup_keys if k in access_stats), None)
        events = bucket.get("events", []) if isinstance(bucket, dict) else []
        parsed_accesses = [ts for ts in (_parse_ts(event.get("ts")) for event in events) if ts is not None]
        last_access = max(parsed_accesses, default=None)
        insights.append(
            {
                "path": rel_path,
                "title": resolved.stem,
                "project": meta.get("project") or "",
                "certainty": meta.get("certainty"),
                "age_days": _age_days(meta.get("parsed_date")),
                "access_count": len(events),
                "last_access": _format_ts(last_access) if last_access else "",
            }
        )

    insights.sort(
        key=lambda item: (
            -item["access_count"],
            -(item["certainty"] if item["certainty"] is not None else -1),
            item["age_days"] if item["age_days"] is not None else 10**9,
            item["path"],
        )
    )
    return insights


def _summarize_entry(entry: dict[str, Any], *, include_sensitive: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ts": _format_ts(entry.get("ts")),
        "hook": entry.get("hook", ""),
        "action": entry.get("action", ""),
    }
    for field in (
        "decision",
        "reason",
        "source",
        "stage",
        "pipeline",
        "project",
        "session_id",
        "dir_key",
        "top_path",
        "file_path",
        "min_score",
        "latency_ms",
        "results_before",
        "results_after",
        "injected_chars",
        "multi_hop_gate",
        "multi_hop_added",
        "deep_recall_spawned",
        "raw_result_count",
        "enhanced_result_count",
        "filtered_count",
        "selected_count",
        "notes_written",
        "threshold",
        "new_notes",
        "exchanges",
        "agent_spawned",
        "substantial",
        "new_insight",
    ):
        if field in entry:
            summary[field] = entry[field]

    if "error" in entry:
        summary["error"] = _safe_error(entry.get("error"))

    digest = _query_digest(entry)
    if digest:
        summary["query_digest"] = digest
    if include_sensitive:
        preview = _query_preview(entry)
        if preview:
            summary["query_preview"] = preview
        raw_preview = entry.get("raw_preview")
        if isinstance(raw_preview, str):
            summary["raw_preview"] = sanitize_secrets(raw_preview[:240])

    for field in ("injected_paths", "injected_titles"):
        value = entry.get(field)
        if isinstance(value, list):
            summary[field] = [sanitize_secrets(str(item))[:160] for item in value if str(item).strip()]

    candidates = entry.get("candidates")
    if isinstance(candidates, list):
        safe_candidates = [candidate for candidate in (_candidate_summary(item) for item in candidates) if candidate]
        if safe_candidates:
            summary["candidates"] = safe_candidates

    return summary


def _hook_stats(entries: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(entry.get("hook") or "unknown") for entry in entries))


def _recall_section(entries: list[dict[str, Any]]) -> dict[str, Any]:
    recall = [entry for entry in entries if entry.get("hook") == "recall"]
    inject = [entry for entry in recall if entry.get("action") == "inject"]
    no_results = [entry for entry in recall if entry.get("action") == "no-results"]
    dedup = [entry for entry in recall if entry.get("action") == "dedup-skip"]
    latencies = [_safe_int(entry.get("latency_ms")) for entry in inject if entry.get("latency_ms") is not None]
    pipelines = Counter(str(entry.get("pipeline") or "unknown") for entry in inject)
    return {
        "calls": len(recall),
        "injected": len(inject),
        "injection_rate": _pct(len(inject), len(recall)),
        "no_results": len(no_results),
        "dedup_skipped": len(dedup),
        "latency_avg_ms": round(_avg(latencies), 1) if latencies else None,
        "latency_p95_ms": round(_p95(latencies), 1) if latencies else None,
        "pipelines": dict(pipelines),
    }


def _tool_context_section(entries: list[dict[str, Any]]) -> dict[str, Any]:
    tool_context = [entry for entry in entries if entry.get("hook") == "tool-context"]
    decisions = [entry for entry in tool_context if entry.get("action") == "decision"]
    population = decisions or tool_context
    injected = [entry for entry in population if entry.get("decision") == "injected" or entry.get("action") == "inject"]
    reasons = Counter(
        str(entry.get("decision") or entry.get("reason") or entry.get("action") or "unknown") for entry in population
    )
    sources = Counter(str(entry.get("source") or "unknown") for entry in decisions if entry.get("source"))
    latencies = [_safe_int(entry.get("latency_ms")) for entry in population if entry.get("latency_ms") is not None]

    injected_paths: Counter[str] = Counter()
    for entry in injected:
        value = entry.get("injected_paths")
        if isinstance(value, list):
            injected_paths.update(path for path in value if isinstance(path, str) and path)

    candidate_snapshots: list[dict[str, Any]] = []
    for entry in decisions[-20:]:
        value = entry.get("candidates")
        if isinstance(value, list):
            for candidate in value:
                safe = _candidate_summary(candidate)
                if safe:
                    candidate_snapshots.append(safe)

    return {
        "calls": len(population),
        "mode": "terminal decisions" if decisions else "legacy events",
        "injected": len(injected),
        "injection_rate": _pct(len(injected), len(population)),
        "reasons": dict(reasons),
        "sources": dict(sources),
        "latency_avg_ms": round(_avg(latencies), 1) if latencies else None,
        "latency_p95_ms": round(_p95(latencies), 1) if latencies else None,
        "injected_paths": dict(injected_paths.most_common(10)),
        "candidate_snapshots": candidate_snapshots[:20],
    }


def _briefing_section(entries: list[dict[str, Any]]) -> dict[str, Any]:
    briefing = [entry for entry in entries if entry.get("hook") == "briefing"]
    inject = [entry for entry in briefing if entry.get("action") == "inject"]
    latencies = [_safe_int(entry.get("latency_ms")) for entry in inject if entry.get("latency_ms") is not None]
    return {
        "calls": len(briefing),
        "injected": len(inject),
        "latency_avg_ms": round(_avg(latencies), 1) if latencies else None,
        "latency_p95_ms": round(_p95(latencies), 1) if latencies else None,
    }


def _triage_section(entries: list[dict[str, Any]]) -> dict[str, Any]:
    triage = [entry for entry in entries if entry.get("hook") == "triage"]
    decisions = [entry for entry in triage if entry.get("action") == "decision"]
    spawned = [entry for entry in decisions if entry.get("agent_spawned")]
    skipped = [entry for entry in decisions if not entry.get("agent_spawned")]
    exchanges = [_safe_int(entry.get("exchanges")) for entry in decisions if entry.get("exchanges") is not None]
    return {
        "calls": len(decisions),
        "agent_spawned": len(spawned),
        "fleeting_only": len(skipped),
        "avg_exchanges": round(_avg(exchanges), 1) if exchanges else None,
        "max_exchanges": max(exchanges) if exchanges else None,
    }


def _inception_section(entries: list[dict[str, Any]]) -> dict[str, Any]:
    inception = [entry for entry in entries if entry.get("hook") == "inception"]
    triggers = [entry for entry in inception if entry.get("action") == "trigger"]
    skips = [entry for entry in inception if entry.get("action") == "skip"]
    new_notes = [_safe_int(entry.get("new_notes")) for entry in triggers if entry.get("new_notes") is not None]
    return {
        "calls": len(inception),
        "triggered": len(triggers),
        "skipped": len(skips),
        "avg_new_notes": round(_avg(new_notes), 1) if new_notes else None,
    }


def _triage_health_section(entries: list[dict[str, Any]]) -> dict[str, Any]:
    success = [entry for entry in entries if entry.get("action") in _TRIAGE_HEALTH_SUCCESS]
    failure = [entry for entry in entries if entry.get("action") in _TRIAGE_HEALTH_FAILURE]
    last_failure = failure[-1] if failure else None
    return {
        "events": len(success) + len(failure),
        "successes": len(success),
        "failures": len(failure),
        "last_failure": _summarize_entry(last_failure) if last_failure else None,
    }


def _entry_text(entry: dict[str, Any]) -> str:
    for key in ("query", "prompt", "topic", "body"):
        value = entry.get(key)
        if isinstance(value, str):
            text = " ".join(value.split())
            if text:
                return text
    return ""


def _normalize_retrieval_reason(reason: Any) -> str:
    return telemetry.normalize_dashboard_retrieval_reason(str(reason or "").strip())


def _entry_reason(entry: dict[str, Any]) -> str:
    action = str(entry.get("action") or "")
    reason = str(entry.get("reason") or "")
    if action == "search_miss" and reason:
        return _normalize_retrieval_reason(reason)
    return _normalize_retrieval_reason(reason or action)


def _is_concrete_query(text: str) -> bool:
    lowered = text.lower()
    return any(pattern.search(text) for pattern in _CONCRETE_PATTERNS) or any(
        marker in lowered
        for marker in (
            "config",
            "configuration",
            "schema",
            "manifest",
            "memento.yml",
            "pyproject.toml",
            "workflow.md",
            "readme.md",
            "package.json",
        )
    )


def _is_project_history_query(text: str) -> bool:
    lowered = text.lower()
    return not _is_concrete_query(text) and any(marker in lowered for marker in _PROJECT_HISTORY_MARKERS)


def _is_miss_like(entry: dict[str, Any]) -> bool:
    hook = str(entry.get("hook") or "")
    action = str(entry.get("action") or "")
    reason = _entry_reason(entry)
    if hook in {"recall", "search", "mcp"}:
        if action in {"search_miss", "no-results", "no_results"}:
            return True
        return reason in SEARCH_MISS_REASONS
    if hook == "tool-context":
        decision = str(entry.get("decision") or reason or action)
        return decision in TOOL_CONTEXT_MISS_REASONS or reason in TOOL_CONTEXT_MISS_REASONS
    return False


def _is_deep_pipeline(entry: dict[str, Any]) -> bool:
    pipeline = str(entry.get("pipeline") or "")
    if pipeline:
        parts = {part.strip().lower() for part in pipeline.split("+") if part.strip()}
        if parts & DEEP_PIPELINE_MARKERS:
            return True
        if len(parts) > 1:
            return True
    return bool(entry.get("multi_hop_added")) or bool(entry.get("deep_recall_spawned"))


def _search_get_followups(access_entries: list[dict[str, Any]]) -> dict[str, Any]:
    searches: list[tuple[datetime, str, str]] = []
    gets: list[tuple[datetime, str]] = []

    for entry in access_entries:
        path = str(entry.get("path") or "").strip()
        ts = _parse_ts(entry.get("ts"))
        if not path or ts is None:
            continue
        tool = str(entry.get("tool") or "")
        hook = str(entry.get("hook") or "")
        if tool == "get":
            gets.append((ts, path))
        elif tool == "search" or (not tool and hook == "mcp" and entry.get("result_count") is not None):
            search_key = str(entry.get("query_hash") or entry.get("query_summary") or ts.isoformat())
            searches.append((ts, path, search_key))

    if not searches or not gets:
        return {
            "followups": 0,
            "searches": len({key for _, _, key in searches}),
            "top_path": "unknown",
            "top_path_followups": 0,
        }

    searches_by_path: dict[str, list[tuple[datetime, str]]] = {}
    for ts, path, search_key in sorted(searches):
        searches_by_path.setdefault(path, []).append((ts, search_key))

    matched_search_keys: set[str] = set()
    path_counts: Counter[str] = Counter()
    for get_ts, path in sorted(gets):
        for search_ts, search_key in reversed(searches_by_path.get(path, [])):
            delta = (get_ts - search_ts).total_seconds()
            if 0 <= delta <= _SEARCH_GET_FOLLOWUP_WINDOW_SECONDS:
                matched_search_keys.add(search_key)
                path_counts[sanitize_secrets(path)] += 1
                break
            if delta > _SEARCH_GET_FOLLOWUP_WINDOW_SECONDS:
                break

    top_path, top_path_count = ("unknown", 0)
    if path_counts:
        top_path, top_path_count = path_counts.most_common(1)[0]
    return {
        "followups": sum(path_counts.values()),
        "searches": len({key for _, _, key in searches}),
        "matched_searches": len(matched_search_keys),
        "top_path": top_path,
        "top_path_followups": top_path_count,
    }


def _recommendation(title: str, summary: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    rec = {"title": title, "summary": summary}
    if evidence:
        rec["evidence"] = evidence
    return rec


def _retrieval_recommendations(
    entries: list[dict[str, Any]], access_entries: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    miss_entries = [entry for entry in entries if _is_miss_like(entry)]
    query_entries = [entry for entry in entries if _entry_text(entry)]

    def add_recommendation(
        title: str,
        summary: str,
        *,
        evidence: dict[str, Any],
        count: int,
        total: int,
    ) -> None:
        share = count / total if total else 0.0
        if count < _RECOMMENDATION_MIN_COUNT or share < _RECOMMENDATION_MIN_SHARE:
            return
        recommendations.append(_recommendation(title, summary, evidence=evidence))

    concrete_misses = [entry for entry in miss_entries if _is_concrete_query(_entry_text(entry))]
    add_recommendation(
        "Enable or tune concrete search",
        (
            f"{len(concrete_misses)} of {len(miss_entries)} misses ({_pct(len(concrete_misses), len(miss_entries))}) "
            "contain paths, file names, or config identifiers; a concrete-search path should handle them better."
        ),
        evidence={"concrete_misses": len(concrete_misses), "misses": len(miss_entries)},
        count=len(concrete_misses),
        total=len(miss_entries),
    )

    history_misses = [entry for entry in miss_entries if _is_project_history_query(_entry_text(entry))]
    add_recommendation(
        "Add a project-history/query tool",
        (
            f"{len(history_misses)} broad project-history prompts were skipped or missed ({_pct(len(history_misses), len(miss_entries))} of misses); "
            "a purpose-built history/query tool would be a better default than generic retrieval."
        ),
        evidence={"history_misses": len(history_misses), "misses": len(miss_entries)},
        count=len(history_misses),
        total=len(miss_entries),
    )

    threshold_misses = [
        entry for entry in miss_entries if _entry_reason(entry) in {"threshold_too_high", "project_filter_removed_all"}
    ]
    project_counts = Counter(
        sanitize_secrets(str(entry.get("project") or entry.get("topic") or entry.get("dir_key") or "unknown"))
        for entry in threshold_misses
    )
    top_project, top_project_count = ("unknown", 0)
    if project_counts:
        top_project, top_project_count = project_counts.most_common(1)[0]
    add_recommendation(
        "Lower the recall threshold or improve note tags",
        (
            f"{len(threshold_misses)} threshold/project-filter misses concentrate on {top_project!r}; "
            "either the cutoff is too strict for this project area or the notes need stronger tags/metadata."
        ),
        evidence={
            "threshold_misses": len(threshold_misses),
            "top_project": top_project,
            "top_project_misses": top_project_count,
        },
        count=top_project_count,
        total=len(threshold_misses),
    )

    tool_context_misses = [
        entry for entry in entries if str(entry.get("hook") or "") == "tool-context" and _is_miss_like(entry)
    ]
    area_counts = Counter(
        sanitize_secrets(
            str(entry.get("dir_key") or entry.get("file_path") or entry.get("path") or entry.get("source") or "unknown")
        )
        for entry in tool_context_misses
    )
    top_area, top_area_count = ("unknown", 0)
    if area_counts:
        top_area, top_area_count = area_counts.most_common(1)[0]
    add_recommendation(
        "Add a code-area tool or project map",
        (
            f"tool-context missed the same area {top_area_count} times ({top_area!r}); recurring code-area lookups may deserve a purpose-built tool instead of generic injection."
        ),
        evidence={
            "tool_context_misses": len(tool_context_misses),
            "top_area": top_area,
            "top_area_misses": top_area_count,
        },
        count=top_area_count,
        total=len(tool_context_misses),
    )

    followups = _search_get_followups(access_entries or [])
    followup_count = int(followups.get("followups") or 0)
    search_count = int(followups.get("searches") or 0)
    followup_share = followup_count / search_count if search_count else 0.0
    if followup_count >= _RECOMMENDATION_MIN_COUNT and followup_share >= _RECOMMENDATION_MIN_SHARE:
        recommendations.append(
            _recommendation(
                "Return fuller search results or tune detail_level",
                (
                    f"{followup_count} recent memento_get calls followed memento_search results within 15 minutes; "
                    f"{followups.get('top_path')!r} was the most common follow-up. Consider returning fuller content by default for repeated targets."
                ),
                evidence=followups,
            )
        )

    deep_entries = [entry for entry in entries if _is_deep_pipeline(entry)]
    latencies = [_safe_int(entry.get("latency_ms")) for entry in entries if entry.get("latency_ms") is not None]
    avg_latency = round(_avg(latencies), 1) if latencies else 0.0
    deep_share = len(deep_entries) / len(query_entries) if query_entries else 0.0
    if (avg_latency >= _RECOMMENDATION_LATENCY_MS or deep_share >= _RECOMMENDATION_MIN_SHARE) and len(
        query_entries
    ) >= _RECOMMENDATION_MIN_COUNT:
        recommendations.append(
            _recommendation(
                "Prefer a purpose-built tool for common deep-retrieval prompts",
                (
                    f"average retrieval latency is {avg_latency}ms and {len(deep_entries)} of {len(query_entries)} logged prompts use multi-stage/deep retrieval; "
                    "a specialized tool would reduce repeated search→rerank→follow-up work."
                ),
                evidence={
                    "avg_latency_ms": avg_latency,
                    "deep_entries": len(deep_entries),
                    "queries": len(query_entries),
                },
            )
        )

    return recommendations


def _benchmark_outcomes_section(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    classifications = Counter(
        str(item.get("memory_classification") or item.get("classification") or "unknown") for item in outcomes
    )
    failures = Counter(
        str(item.get("memory_failure_type") or item.get("failure_type"))
        for item in outcomes
        if item.get("memory_failure_type") or item.get("failure_type")
    )
    latencies = [
        _safe_int(item.get("retrieval_latency_ms") or item.get("latency_ms"))
        for item in outcomes
        if item.get("retrieval_latency_ms") is not None or item.get("latency_ms") is not None
    ]
    token_budgets = [
        _safe_int(item.get("memory_token_budget") or item.get("token_budget"))
        for item in outcomes
        if item.get("memory_token_budget") is not None or item.get("token_budget") is not None
    ]
    memory_used = sum(1 for item in outcomes if item.get("memory_used") is True)
    measurable = sum(1 for item in outcomes if item.get("memory_contribution_measurable") is True)
    return {
        "count": len(outcomes),
        "memory_used": memory_used,
        "memory_contribution_measurable": measurable,
        "classifications": dict(sorted(classifications.items())),
        "failures": dict(sorted(failures.items())),
        "latency_avg_ms": round(_avg(latencies), 1) if latencies else None,
        "latency_p95_ms": round(_p95(latencies), 1) if latencies else None,
        "token_budget_avg": round(_avg(token_budgets), 1) if token_budgets else None,
        "token_budget_p95": round(_p95(token_budgets), 1) if token_budgets else None,
        "recent_failures": [
            _summarize_benchmark_outcome(item)
            for item in outcomes
            if item.get("memory_failure_type") or item.get("failure_type")
        ][-DEFAULT_NOTE_LIMIT:],
    }


def _summarize_benchmark_outcome(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(item.get("task_id") or item.get("id") or "?"),
        "classification": str(item.get("memory_classification") or item.get("classification") or "unknown"),
        "failure_type": str(item.get("memory_failure_type") or item.get("failure_type") or ""),
        "memory_used": bool(item.get("memory_used")),
        "retrieval_latency_ms": item.get("retrieval_latency_ms") or item.get("latency_ms"),
        "memory_token_budget": item.get("memory_token_budget") or item.get("token_budget"),
    }


def build_report(
    entries: list[dict[str, Any]],
    *,
    include_sensitive: bool = False,
    vault_path: str | Path | None = None,
    access_stats: dict[str, Any] | None = None,
    access_entries: list[dict[str, Any]] | None = None,
    triage_health_entries: list[dict[str, Any]] | None = None,
    benchmark_outcomes: list[dict[str, Any]] | None = None,
    event_limit: int = DEFAULT_EVENT_LIMIT,
    note_limit: int = DEFAULT_NOTE_LIMIT,
) -> dict[str, Any]:
    vault = Path(vault_path or get_vault()).expanduser()
    access_stats = access_stats if access_stats is not None else load_access_log_stats()
    triage_health = triage_health_entries if triage_health_entries is not None else load_triage_health_entries()
    benchmark_outcomes = benchmark_outcomes or []
    tool_context = _tool_context_section(entries)
    recent_entries = entries[-max(1, event_limit) :]

    return {
        "window": {
            "start": _format_ts(entries[0].get("ts")) if entries else "?",
            "end": _format_ts(entries[-1].get("ts")) if entries else "?",
            "count": len(entries),
        },
        "privacy": {"include_sensitive": include_sensitive, "default_redacted": not include_sensitive},
        "hooks": _hook_stats(entries),
        "triage": _triage_section(entries),
        "triage_health": {
            **_triage_health_section(triage_health),
            "recent_failures": [
                _summarize_entry(e) for e in triage_health if e.get("action") in _TRIAGE_HEALTH_FAILURE
            ][-note_limit:],
        },
        "recall": _recall_section(entries),
        "tool_context": tool_context,
        "recommendations": _retrieval_recommendations(entries, access_entries=access_entries),
        "briefing": _briefing_section(entries),
        "inception": _inception_section(entries),
        "recent_events": [_summarize_entry(entry, include_sensitive=include_sensitive) for entry in recent_entries],
        "candidate_snapshots": tool_context["candidate_snapshots"],
        "benchmark_outcomes": _benchmark_outcomes_section(benchmark_outcomes),
        "top_notes": _note_insights(vault, entries, access_stats)[:note_limit],
    }


def render_text_report(report: dict[str, Any]) -> str:
    window = report["window"]
    lines = [
        "=== Memento retrieval debug report ===",
        f"Period: {window['start']} to {window['end']}",
        f"Total log entries: {window['count']}",
        "Privacy: redacted by default; use --include-sensitive to show sanitized query previews.",
        "",
    ]

    triage = report["triage"]
    lines += [
        "--- Triage ---",
        f"  Sessions triaged: {triage['calls']}",
        f"  Agent spawned: {triage['agent_spawned']} ({_pct(triage['agent_spawned'], triage['calls'])})",
        f"  Fleeting only: {triage['fleeting_only']} ({_pct(triage['fleeting_only'], triage['calls'])})",
    ]
    if triage.get("avg_exchanges") is not None:
        lines.append(f"  Exchanges: avg {triage['avg_exchanges']}, max {triage['max_exchanges']}")
    lines.append("")

    recall = report["recall"]
    lines += [
        "--- Retrieval (recall) ---",
        f"  Total calls: {recall['calls']}",
        f"  Injected: {recall['injected']} ({recall['injection_rate']})",
        f"  No results: {recall['no_results']}",
        f"  Dedup skipped: {recall['dedup_skipped']}",
    ]
    if recall.get("latency_avg_ms") is not None:
        lines.append(f"  Latency: avg {recall['latency_avg_ms']}ms, p95 {recall['latency_p95_ms']}ms")
    if recall.get("pipelines"):
        lines.append("  Pipeline distribution:")
        for pipeline, count in sorted(recall["pipelines"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"    {pipeline}: {count}")
    lines.append("")

    tool_context = report["tool_context"]
    lines += [
        "--- Tool context ---",
        f"  Total calls: {tool_context['calls']} ({tool_context['mode']})",
        f"  Injected: {tool_context['injected']} ({tool_context['injection_rate']})",
    ]
    if tool_context.get("latency_avg_ms") is not None:
        lines.append(f"  Latency: avg {tool_context['latency_avg_ms']}ms, p95 {tool_context['latency_p95_ms']}ms")
    if tool_context.get("reasons"):
        lines.append("  Decision distribution:")
        for reason, count in sorted(tool_context["reasons"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"    {reason}: {count}")
    if tool_context.get("injected_paths"):
        lines.append("  Top injected paths:")
        for path, count in tool_context["injected_paths"].items():
            lines.append(f"    {path}: {count}")
    lines.append("")

    recommendations = report.get("recommendations", [])
    lines += ["--- Recommendations ---"]
    if not recommendations:
        lines.append("  No strong retrieval-pattern recommendations yet.")
    else:
        for rec in recommendations:
            lines.append(f"  • {rec['title']}: {rec['summary']}")
    lines.append("")

    benchmark = report.get("benchmark_outcomes", {})
    lines += ["--- Benchmark memory outcomes ---"]
    if not benchmark or benchmark.get("count", 0) == 0:
        lines.append("  No benchmark outcome file supplied.")
    else:
        lines += [
            f"  Outcomes: {benchmark['count']}",
            f"  Memory used: {benchmark['memory_used']} ({_pct(benchmark['memory_used'], benchmark['count'])})",
            f"  Measurable contribution: {benchmark['memory_contribution_measurable']} ({_pct(benchmark['memory_contribution_measurable'], benchmark['count'])})",
        ]
        if benchmark.get("latency_avg_ms") is not None:
            lines.append(
                f"  Retrieval latency: avg {benchmark['latency_avg_ms']}ms, p95 {benchmark['latency_p95_ms']}ms"
            )
        if benchmark.get("token_budget_avg") is not None:
            lines.append(f"  Token budget: avg {benchmark['token_budget_avg']}, p95 {benchmark['token_budget_p95']}")
        if benchmark.get("classifications"):
            lines.append("  Classification distribution:")
            for name, count in sorted(benchmark["classifications"].items(), key=lambda item: (-item[1], item[0])):
                lines.append(f"    {name}: {count}")
        if benchmark.get("failures"):
            lines.append("  Memory failure distribution:")
            for name, count in sorted(benchmark["failures"].items(), key=lambda item: (-item[1], item[0])):
                lines.append(f"    {name}: {count}")
    lines.append("")

    briefing = report["briefing"]
    lines += [
        "--- Briefing ---",
        f"  Sessions briefed: {briefing['injected']}",
    ]
    if briefing.get("latency_avg_ms") is not None:
        lines.append(f"  Latency: avg {briefing['latency_avg_ms']}ms, p95 {briefing['latency_p95_ms']}ms")
    lines.append("")

    triage_health = report["triage_health"]
    lines += [
        "--- Triage health ---",
        f"  Events: {triage_health['events']}",
        f"  Successes: {triage_health['successes']}",
        f"  Failures: {triage_health['failures']}",
    ]
    if triage_health.get("last_failure"):
        failure = triage_health["last_failure"]
        lines.append(f"  Last failure: {failure['action']} ({failure.get('error') or 'no error'})")
    lines.append("")

    inception = report["inception"]
    lines += [
        "--- Inception ---",
        f"  Trigger checks: {inception['calls']}",
        f"  Triggered: {inception['triggered']}",
        f"  Skipped: {inception['skipped']}",
        "",
        "--- Top notes ---",
    ]
    top_notes = report["top_notes"]
    if not top_notes:
        lines.append("  No note metadata found from the current log window.")
    else:
        for note in top_notes:
            certainty = note["certainty"] if note["certainty"] is not None else "?"
            age = f"{note['age_days']}d" if note["age_days"] is not None else "?"
            lines.append(
                f"  {note['path']} | project={note['project'] or '?'} | certainty={certainty} | age={age} | accesses={note['access_count']}"
            )
    lines.append("")

    lines.append("--- Recent decisions ---")
    for event in report["recent_events"]:
        bits = [f"{event['ts']}", event["hook"], event["action"]]
        for field in ("decision", "reason", "source", "pipeline", "stage", "query_digest", "latency_ms"):
            value = event.get(field)
            if value not in (None, ""):
                bits.append(f"{field}={value}")
        if event.get("injected_paths"):
            bits.append(f"paths={len(event['injected_paths'])}")
        if event.get("candidates"):
            bits.append(f"candidates={len(event['candidates'])}")
        lines.append("  - " + " · ".join(bits))
        if event.get("query_preview"):
            lines.append(f"    query: {event['query_preview']}")
    return "\n".join(lines)


def _html_grid(items: list[tuple[str, Any]]) -> str:
    parts = ['<div class="kv-grid">']
    for label, value in items:
        parts.append('<div class="kv">')
        parts.append(f'<div class="label">{escape(label)}</div>')
        parts.append(f'<div class="value">{escape(str(value))}</div>')
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def _html_table(headers: list[str], rows: list[list[Any]], empty: str = "No rows") -> str:
    if not rows:
        return f'<p class="empty">{escape(empty)}</p>'
    parts = ["<table><thead><tr>"]
    for header in headers:
        parts.append(f"<th>{escape(header)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            if isinstance(cell, list):
                rendered = "<br>".join(escape(str(item)) for item in cell)
                parts.append(f"<td>{rendered}</td>")
            else:
                parts.append(f"<td>{escape(str(cell))}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _html_bullets(items: list[str], empty: str = "No strong recommendations yet") -> str:
    if not items:
        return f'<p class="empty">{escape(empty)}</p>'
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


def _section(title: str, body: str, subtitle: str | None = None) -> str:
    subtitle_html = f'<p class="subtitle">{escape(subtitle)}</p>' if subtitle else ""
    return f'<section class="card"><h2>{escape(title)}</h2>{subtitle_html}{body}</section>'


def render_html_report(report: dict[str, Any]) -> str:
    window = report["window"]
    privacy = report["privacy"]
    triage = report["triage"]
    recall = report["recall"]
    tool_context = report["tool_context"]
    recommendations = report.get("recommendations", [])
    briefing = report["briefing"]
    triage_health = report["triage_health"]
    inception = report["inception"]
    benchmark = report.get("benchmark_outcomes", {})

    summary = _html_grid(
        [
            ("Window", f"{window['start']} → {window['end']}"),
            ("Entries", window["count"]),
            ("Triage spawned", f"{triage['agent_spawned']} / {triage['calls']}")
            if triage["calls"]
            else ("Triage spawned", "0 / 0"),
            ("Recall injected", f"{recall['injected']} / {recall['calls']}")
            if recall["calls"]
            else ("Recall injected", "0 / 0"),
            ("Tool context injected", f"{tool_context['injected']} / {tool_context['calls']}")
            if tool_context["calls"]
            else ("Tool context injected", "0 / 0"),
            ("Privacy", "sensitive previews enabled" if privacy["include_sensitive"] else "redacted by default"),
            ("Benchmark outcomes", benchmark.get("count", 0)),
        ]
    )

    hook_rows = [[hook, count] for hook, count in sorted(report["hooks"].items(), key=lambda item: (-item[1], item[0]))]
    top_notes_rows = [
        [
            note["path"],
            note["project"] or "",
            note["certainty"] if note["certainty"] is not None else "",
            note["age_days"] if note["age_days"] is not None else "",
            note["access_count"],
            note["last_access"],
        ]
        for note in report["top_notes"]
    ]
    candidate_rows = [
        [c["path"], c["title"], c["score"], c["decision"]] for c in tool_context.get("candidate_snapshots", [])
    ]
    benchmark_rows = [
        [name, count]
        for name, count in sorted(benchmark.get("classifications", {}).items(), key=lambda item: (-item[1], item[0]))
    ]
    benchmark_failure_rows = [
        [name, count]
        for name, count in sorted(benchmark.get("failures", {}).items(), key=lambda item: (-item[1], item[0]))
    ]
    event_rows = []
    for event in report["recent_events"]:
        detail_parts = [
            f"{key}={event[key]}"
            for key in ("decision", "reason", "source", "pipeline", "stage", "query_digest", "latency_ms")
            if event.get(key) not in (None, "")
        ]
        if event.get("query_preview"):
            detail_parts.append(f"query={event['query_preview']}")
        event_rows.append([event["ts"], f"{event['hook']} / {event['action']}", " · ".join(detail_parts)])

    if triage_health.get("last_failure"):
        failure = triage_health["last_failure"]
        failure_rows = [[k, v] for k, v in failure.items()]
        triage_health_body = _html_grid(
            [
                ("Events", triage_health["events"]),
                ("Successes", triage_health["successes"]),
                ("Failures", triage_health["failures"]),
            ]
        ) + _html_table(["Last failure field", "Value"], failure_rows)
    else:
        triage_health_body = (
            _html_grid(
                [
                    ("Events", triage_health["events"]),
                    ("Successes", triage_health["successes"]),
                    ("Failures", triage_health["failures"]),
                ]
            )
            + '<p class="empty">No recent triage failures</p>'
        )

    body = "".join(
        [
            _section("Hook volume", _html_table(["Hook", "Count"], hook_rows)),
            _section(
                "Triage",
                _html_grid(
                    [
                        ("Sessions triaged", triage["calls"]),
                        ("Agent spawned", triage["agent_spawned"]),
                        ("Fleeting only", triage["fleeting_only"]),
                        ("Avg exchanges", triage.get("avg_exchanges") or ""),
                        ("Max exchanges", triage.get("max_exchanges") or ""),
                    ]
                ),
            ),
            _section(
                "Retrieval (recall)",
                _html_grid(
                    [
                        ("Calls", recall["calls"]),
                        ("Injected", f"{recall['injected']} ({recall['injection_rate']})"),
                        ("No results", recall["no_results"]),
                        ("Dedup skipped", recall["dedup_skipped"]),
                        (
                            "Latency avg / p95",
                            f"{recall.get('latency_avg_ms') or '-'} / {recall.get('latency_p95_ms') or '-'} ms",
                        ),
                    ]
                ),
            ),
            _section(
                "Tool context",
                _html_grid(
                    [
                        ("Calls", f"{tool_context['calls']} ({tool_context['mode']})"),
                        ("Injected", f"{tool_context['injected']} ({tool_context['injection_rate']})"),
                        (
                            "Latency avg / p95",
                            f"{tool_context.get('latency_avg_ms') or '-'} / {tool_context.get('latency_p95_ms') or '-'} ms",
                        ),
                    ]
                )
                + _html_table(
                    ["Path", "Title", "Score", "Decision"], candidate_rows, empty="No candidate snapshots logged"
                ),
            ),
            _section(
                "Recommendations", _html_bullets([f"{rec['title']}: {rec['summary']}" for rec in recommendations])
            ),
            _section(
                "Benchmark memory outcomes",
                _html_grid(
                    [
                        ("Outcomes", benchmark.get("count", 0)),
                        ("Memory used", f"{benchmark.get('memory_used', 0)} / {benchmark.get('count', 0)}"),
                        (
                            "Measurable contribution",
                            f"{benchmark.get('memory_contribution_measurable', 0)} / {benchmark.get('count', 0)}",
                        ),
                        (
                            "Latency avg / p95",
                            f"{benchmark.get('latency_avg_ms') or '-'} / {benchmark.get('latency_p95_ms') or '-'} ms",
                        ),
                        (
                            "Token budget avg / p95",
                            f"{benchmark.get('token_budget_avg') or '-'} / {benchmark.get('token_budget_p95') or '-'}",
                        ),
                    ]
                )
                + _html_table(["Classification", "Count"], benchmark_rows, empty="No benchmark outcomes supplied")
                + _html_table(["Failure type", "Count"], benchmark_failure_rows, empty="No benchmark memory failures"),
            ),
            _section("Triage health", triage_health_body),
            _section(
                "Top notes",
                _html_table(
                    ["Path", "Project", "Certainty", "Age (days)", "Accesses", "Last access"],
                    top_notes_rows,
                    empty="No note metadata found",
                ),
            ),
            _section(
                "Recent decisions",
                _html_table(["Timestamp", "Hook / action", "Details"], event_rows, empty="No recent entries"),
            ),
            _section(
                "Briefing",
                _html_grid(
                    [
                        ("Sessions briefed", briefing["injected"]),
                        (
                            "Latency avg / p95",
                            f"{briefing.get('latency_avg_ms') or '-'} / {briefing.get('latency_p95_ms') or '-'} ms",
                        ),
                    ]
                ),
            ),
            _section(
                "Inception",
                _html_grid(
                    [
                        ("Trigger checks", inception["calls"]),
                        ("Triggered", inception["triggered"]),
                        ("Skipped", inception["skipped"]),
                        ("Avg new notes", inception.get("avg_new_notes") or ""),
                    ]
                ),
            ),
        ]
    )

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Memento retrieval debug report</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; margin: 0; padding: 24px; background: #0b1020; color: #e5e7eb; }}
    h1 {{ margin: 0 0 8px; font-size: 1.7rem; }}
    p {{ line-height: 1.45; }}
    .subtitle, .empty, .muted {{ color: #9ca3af; }}
    .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); margin: 16px 0 24px; }}
    .card {{ background: #111827; border: 1px solid #1f2937; border-radius: 14px; padding: 16px; margin-bottom: 16px; box-shadow: 0 1px 0 rgba(255,255,255,0.03) inset; }}
    .card h2 {{ margin: 0 0 8px; font-size: 1.05rem; }}
    .kv-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
    .kv {{ background: #0f172a; border-radius: 10px; padding: 10px 12px; border: 1px solid #243041; }}
    .label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: #94a3b8; margin-bottom: 4px; }}
    .value {{ font-size: 1rem; word-break: break-word; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid #1f2937; }}
    th {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; color: #94a3b8; }}
    tr:hover td {{ background: rgba(255,255,255,0.02); }}
    .layout {{ max-width: 1320px; margin: 0 auto; }}
    .header {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: baseline; justify-content: space-between; }}
    .footer {{ margin-top: 18px; color: #94a3b8; font-size: 0.9rem; }}
    .stack {{ display: grid; gap: 16px; }}
  </style>
</head>
<body>
  <div class=\"layout\">
    <div class=\"header\">
      <div>
        <h1>Memento retrieval debug report</h1>
        <p class=\"subtitle\">Window {escape(window["start"])} → {escape(window["end"])} · {window["count"]} entries</p>
      </div>
      <div class=\"subtitle\">Privacy: {"sensitive previews enabled" if privacy["include_sensitive"] else "redacted by default"}</div>
    </div>
    <div class=\"grid\">{summary}</div>
    <div class=\"stack\">{body}</div>
    <p class=\"footer\">Use this as a local debugging surface. Raw transcript/body previews stay hidden unless explicitly requested.</p>
  </div>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a local retrieval debug report from retrieval.jsonl")
    parser.add_argument("--since", type=int, default=None, help="Only include entries from the last N days")
    parser.add_argument("--limit", type=int, default=DEFAULT_EVENT_LIMIT, help="Max recent entries to show")
    parser.add_argument("--note-limit", type=int, default=DEFAULT_NOTE_LIMIT, help="Max notes to show in top-notes")
    parser.add_argument("--html", action="store_true", help="Render a self-contained HTML dashboard instead of text")
    parser.add_argument("--output", type=str, default=None, help="Write the report to this file instead of stdout")
    parser.add_argument("--include-sensitive", action="store_true", help="Include sanitized query/body previews")
    parser.add_argument("--retrieval-log", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--triage-health-log", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--benchmark-outcomes",
        type=str,
        default=None,
        help="Optional sanitized Rondo benchmark outcome report (JSON/JSONL) to summarize",
    )
    parser.add_argument("--access-log", type=str, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    retrieval_log = Path(args.retrieval_log) if args.retrieval_log else DEFAULT_RETRIEVAL_LOG
    triage_health_log = Path(args.triage_health_log) if args.triage_health_log else DEFAULT_TRIAGE_HEALTH_LOG
    access_log = Path(args.access_log) if args.access_log else DEFAULT_ACCESS_LOG
    entries = load_retrieval_entries(retrieval_log, since_days=args.since)
    triage_health_entries = load_triage_health_entries(triage_health_log, since_days=args.since)
    access_entries = load_access_entries(access_log, since_days=args.since)
    benchmark_outcomes = load_benchmark_outcomes(args.benchmark_outcomes)
    report = build_report(
        entries,
        include_sensitive=args.include_sensitive,
        triage_health_entries=triage_health_entries,
        access_entries=access_entries,
        benchmark_outcomes=benchmark_outcomes,
        event_limit=max(1, args.limit),
        note_limit=max(1, args.note_limit),
    )

    output = render_html_report(report) if args.html else render_text_report(report)
    if args.output:
        Path(args.output).expanduser().write_text(output, encoding="utf-8")
        print(args.output)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
