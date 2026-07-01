"""Shared operational telemetry parsing and readiness helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HEALTH_WINDOW_HOURS = 24
HEALTH_MIN_EVENTS_FOR_RATE = 3
HEALTH_WARN_FAILURE_RATIO = 0.5

TRIAGE_HEALTH_SUCCESS_ACTIONS = frozenset({"structured_notes_written"})
TRIAGE_HEALTH_FAILURE_ACTIONS = frozenset(
    {
        "hook_input_failed",
        "missing_transcript",
        "parse_transcript_failed",
        "structured_notes_failed",
        "structured_notes_llm_failed",
        "structured_notes_lock_timeout",
        "structured_notes_parse_empty",
        "structured_notes_payload_unreadable",
        "structured_notes_transcript_unreadable",
    }
)

PI_BRIDGE_FAILURE_ACTIONS = frozenset(
    {
        "triage_missing_transcript",
        "triage_disallowed_transcript",
        "pi_missing_transcript",
        "pi_structured_notes_parse_empty",
        "pi_structured_notes_transcript_unreadable",
        "pi_structured_notes_lock_timeout",
    }
)

RECENT_FAILURE_ACTION_MARKERS = ("failed", "failure", "error", "unexpected", "unavailable")
RETRIEVAL_SKIP_ACTIONS = frozenset(
    {
        "broad-project-query",
        "deferred-ready",
        "low-signal-prompt",
        "query_too_broad",
        "skipped-prompt",
    }
)
RETRIEVAL_NO_RESULT_REASONS = frozenset(
    {
        "dedup-skip",
        "duplicate",
        "filtered-empty",
        "literal_mode_auto_selected",
        "no-results",
        "no_concrete_match",
        "no_exact_match",
        "project-mismatch-filtered-empty",
        "project_filter_removed_all",
        "threshold_too_high",
    }
)
RETRIEVAL_BACKEND_UNAVAILABLE_REASONS = frozenset(
    {
        "backend_unavailable",
        "empty_vault",
        "index_stale_or_missing",
        "qmd-unavailable",
        "semantic_mode_not_available",
    }
)
RETRIEVAL_BACKEND_EXCEPTION_ACTIONS = frozenset(
    {"extra_collection_failed", "qmd_get_unexpected", "qmd_search_unexpected"}
)
RETRIEVAL_REASON_ALIASES = {
    "broad-project-query": "query_too_broad",
    "filtered-empty": "filtered-empty",
    "low-signal-prompt": "low-signal-prompt",
    "no-results": "no-results",
    "project-mismatch-filtered-empty": "project-mismatch-filtered-empty",
    "qmd-unavailable": "backend_unavailable",
    "skipped-prompt": "skipped-prompt",
    "vault-unavailable": "empty_vault",
}
DASHBOARD_RETRIEVAL_REASON_ALIASES = {
    "broad-project-query": "query_too_broad",
    "filtered-empty": "no_exact_match",
    "low-signal-prompt": "query_too_broad",
    "no-results": "no_exact_match",
    "project-mismatch-filtered-empty": "project_filter_removed_all",
    "skipped-prompt": "query_too_broad",
}
RETRIEVAL_ERROR_DETAIL_LIMIT = 500
SECRET_PATTERNS = (
    (r"(sk-[a-zA-Z0-9]{20,})", "[REDACTED_API_KEY]"),
    (r"(sk-proj-[a-zA-Z0-9_-]{20,})", "[REDACTED_API_KEY]"),
    (r"(ghp_[a-zA-Z0-9]{36,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(gho_[a-zA-Z0-9]{36,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(github_pat_[a-zA-Z0-9_]{20,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(xox[bp]-[a-zA-Z0-9\-]+)", "[REDACTED_SLACK_TOKEN]"),
    (r"(AKIA[0-9A-Z]{16})", "[REDACTED_AWS_KEY]"),
    (r"(eyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,})", "[REDACTED_JWT]"),
    (r'((?:postgres|mysql|mongodb|redis)://[^\s"\'`]+)', "[REDACTED_CONNECTION_STRING]"),
    (r"(Bearer\s+[a-zA-Z0-9_\-.]{20,})", "Bearer [REDACTED_TOKEN]"),
    (r'(?:_KEY|_SECRET|_TOKEN|_PASSWORD|_PASS)\s*[=:]\s*["\']?([a-zA-Z0-9_\-/.]{20,})["\']?', "[REDACTED_SECRET]"),
)
_COMPILED_SECRET_PATTERNS = tuple(
    (re.compile(pattern, re.IGNORECASE), replacement) for pattern, replacement in SECRET_PATTERNS
)


def coerce_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime, treating naive datetimes as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp_utc(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize offset-naive/aware values to UTC."""
    if isinstance(value, datetime):
        return coerce_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return coerce_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def parse_timestamp_naive_utc(value: Any) -> datetime | None:
    """Parse a timestamp as UTC and return a naive datetime for legacy callers."""
    parsed = parse_timestamp_utc(value)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=None)


def format_timestamp_utc(value: Any) -> str:
    parsed = parse_timestamp_utc(value)
    if parsed is None:
        return str(value or "?")
    return parsed.strftime("%Y-%m-%d %H:%M:%SZ")


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def iter_recent_jsonl(path: Path, cutoff: datetime) -> Iterator[dict[str, Any]]:
    cutoff_utc = coerce_utc(cutoff)
    for rec in iter_jsonl(path):
        ts = parse_timestamp_utc(rec.get("ts"))
        if ts is None or ts < cutoff_utc:
            continue
        yield rec


def failure_rate(failures: int, total: int) -> float:
    return round(failures / total, 4) if total else 0.0


def failure_rate_warns(failures: int, total: int) -> bool:
    return total >= HEALTH_MIN_EVENTS_FOR_RATE and failure_rate(failures, total) >= HEALTH_WARN_FAILURE_RATIO


def is_pi_bridge_failure_action(action: Any) -> bool:
    text = str(action or "")
    return text.endswith("_failed") or text in PI_BRIDGE_FAILURE_ACTIONS


def is_pi_bridge_failure_record(rec: dict[str, Any]) -> bool:
    return is_pi_bridge_failure_action(rec.get("action"))


def normalize_retrieval_reason(reason: str) -> str:
    return RETRIEVAL_REASON_ALIASES.get(reason, reason)


def normalize_dashboard_retrieval_reason(reason: str) -> str:
    text = str(reason or "")
    return DASHBOARD_RETRIEVAL_REASON_ALIASES.get(text, text)


def classify_retrieval_record(rec: dict[str, Any]) -> str | None:
    hook = str(rec.get("hook") or "")
    action = str(rec.get("action") or "")
    reason = str(rec.get("reason") or "")
    if hook not in {"recall", "search", "mcp"}:
        return None
    if hook == "mcp" and action not in {"search", "search_miss"}:
        return None
    if action.startswith("diagnostic-"):
        return None

    reason_or_action = reason if action == "search_miss" and reason else action
    normalized = normalize_retrieval_reason(reason_or_action)
    if normalized in RETRIEVAL_SKIP_ACTIONS:
        return "low_signal_skip"
    if action in {"inject", "search"} and normalized not in RETRIEVAL_BACKEND_UNAVAILABLE_REASONS:
        return "success"
    if normalized in RETRIEVAL_BACKEND_UNAVAILABLE_REASONS:
        return "backend_unavailable"
    if action in RETRIEVAL_BACKEND_EXCEPTION_ACTIONS:
        return "backend_exception"
    if normalized in RETRIEVAL_NO_RESULT_REASONS:
        return "no_result"
    if any(marker in action for marker in RECENT_FAILURE_ACTION_MARKERS):
        return "backend_exception" if hook == "search" else "other_failure"
    return None


def redact_text(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    for pattern, replacement in _COMPILED_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_text(value: Any, limit: int = 1000) -> str:
    text = "" if value is None else str(redact_text(value))
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def safe_error(value: Any, *, limit: int = RETRIEVAL_ERROR_DETAIL_LIMIT) -> tuple[str, bool]:
    text = safe_text(" ".join(str(value or "").split()), limit=10_000)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + "..."
    return text, truncated


def sanitize_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_obj(v) for v in value]
    if isinstance(value, tuple):
        return [sanitize_obj(v) for v in value]
    if isinstance(value, str):
        return safe_text(value)
    return value
