"""Capture pipeline health: is the WRITE path working, and what does it cost?

Reads the always-on triage-health telemetry (~/.config/memento-vault/
triage-health.jsonl) over a configurable window and grades failure rates,
throughput, spend, and anomalies. Deterministic and read-only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from evals.common import (
    CheckResult,
    PASS,
    SKIP,
    WARN,
    config_dir,
    grade,
    iter_jsonl,
    parse_event_ts,
    pct,
    threshold,
    window_start,
)

SUITE = "capture_health"

ATTEMPT_ACTIONS = {"structured_notes_attempt", "pi_structured_notes_attempt"}
WRITTEN_ACTIONS = {"structured_notes_written", "pi_structured_notes_written"}
LLM_FAILED_ACTIONS = {"structured_notes_llm_failed", "pi_structured_notes_llm_failed"}
PARSE_EMPTY_ACTIONS = {"structured_notes_parse_empty", "pi_structured_notes_parse_empty"}
TRUNCATED_ACTIONS = {
    "structured_notes_transcript_truncated",
    "pi_structured_notes_transcript_truncated",
}
MISSING_TRANSCRIPT_ACTIONS = {"missing_transcript", "triage_missing_transcript"}
SPAWN_ACTIONS = {"triage_spawned"}
BRIDGE_FAILURE_ACTIONS = {"status_failed", "tool-context_failed", "capture_failed"}


def _telemetry_field(event, key):
    if key in event:
        return event.get(key)
    nested = event.get("telemetry")
    if isinstance(nested, dict):
        return nested.get(key)
    return None


def run(context) -> list[CheckResult]:
    from evals.common import thresholds

    log_path = context.get("triage_health_log") or config_dir() / "triage-health.jsonl"
    # window_days is stored flat in thresholds.yml (not warn/fail).
    window_days = int(thresholds().get(SUITE, {}).get("window_days", 30))
    since = window_start(window_days)

    if not log_path.exists():
        return [
            CheckResult(
                id=f"{SUITE}.telemetry_present",
                suite=SUITE,
                title="Triage-health telemetry log exists",
                status=SKIP,
                details=[f"not found: {log_path}"],
                remediation="Telemetry is always-on; a missing log means no captures ran "
                "on this machine or XDG paths moved.",
            )
        ]

    counts = Counter()
    spawn_days = Counter()
    bridge_fail_days = Counter()
    notes_written = 0
    spend = defaultdict(float)
    spend_events = 0
    backend_models = Counter()
    for event in iter_jsonl(log_path, since=since):
        action = event.get("action", "")
        counts[action] += 1
        ts = parse_event_ts(event)
        day = ts.date().isoformat() if ts else "unknown"
        if action in SPAWN_ACTIONS:
            spawn_days[day] += 1
        if action in BRIDGE_FAILURE_ACTIONS:
            bridge_fail_days[day] += 1
        if action in WRITTEN_ACTIONS:
            notes_written += int(event.get("notes_written") or 0)
        if action in WRITTEN_ACTIONS | LLM_FAILED_ACTIONS:
            got_any = False
            for key in ("prompt_bytes", "output_bytes", "input_tokens", "output_tokens", "duration_ms"):
                value = _telemetry_field(event, key)
                if isinstance(value, (int, float)):
                    spend[key] += value
                    got_any = True
            if got_any:
                spend_events += 1
            backend = _telemetry_field(event, "backend") or "unknown"
            model = _telemetry_field(event, "model") or "default"
            backend_models[f"{backend}/{model}"] += 1

    attempts = sum(counts[a] for a in ATTEMPT_ACTIONS)
    written = sum(counts[a] for a in WRITTEN_ACTIONS)
    failed = sum(counts[a] for a in LLM_FAILED_ACTIONS)
    parse_empty = sum(counts[a] for a in PARSE_EMPTY_ACTIONS)
    truncated = sum(counts[a] for a in TRUNCATED_ACTIONS)
    missing = sum(counts[a] for a in MISSING_TRANSCRIPT_ACTIONS)
    spawned = sum(counts[a] for a in SPAWN_ACTIONS)
    bridge_failures = sum(counts[a] for a in BRIDGE_FAILURE_ACTIONS)

    results = []

    def check(metric, title, value, higher_is_better, unit="rate", details=None, remediation=""):
        warn = threshold(SUITE, metric, "warn", 0.9 if higher_is_better else 0.1)
        fail = threshold(SUITE, metric, "fail", 0.7 if higher_is_better else 0.3)
        results.append(
            CheckResult(
                id=f"{SUITE}.{metric}",
                suite=SUITE,
                title=f"{title} (last {window_days}d)",
                status=grade(value, warn, fail, higher_is_better),
                value=value,
                unit=unit,
                threshold=f"warn {'<' if higher_is_better else '>'} {warn}, "
                f"fail {'<' if higher_is_better else '>'} {fail}",
                details=details or [],
                remediation=remediation,
            )
        )

    if attempts == 0:
        results.append(
            CheckResult(
                id=f"{SUITE}.no_activity",
                suite=SUITE,
                title=f"Structured-note attempts in the last {window_days} days",
                status=WARN,
                value=0,
                unit="count",
                remediation="No note-writing activity in the window; either no sessions ran "
                "or the SessionEnd hook is not firing.",
            )
        )
        return results

    check(
        "llm_failure_rate",
        "Structured-note LLM calls that failed",
        pct(failed, attempts),
        False,
        details=[f"{failed} failed / {attempts} attempts"],
        remediation="Inspect recent structured_notes_llm_failed events for the error field; "
        "the June 2026 failure wave was prompt-too-long before transcript truncation landed.",
    )
    check(
        "parse_empty_rate",
        "LLM responses that parsed to zero notes",
        pct(parse_empty, attempts),
        False,
        details=[f"{parse_empty} empty / {attempts} attempts"],
        remediation="The LLM returned non-JSON or an empty list; those sessions produced no "
        "notes silently. Check the triage prompt and structured-output settings.",
    )
    check(
        "missing_transcript_rate",
        "Triage runs that could not find their transcript",
        pct(missing, max(spawned, attempts)),
        False,
        details=[f"{missing} missing / {max(spawned, attempts)} runs"],
        remediation="Transcript paths moved or were cleaned before the async worker ran.",
    )
    check(
        "truncation_rate",
        "Note-writing prompts that hit the transcript cap",
        pct(truncated, attempts),
        False,
        details=[f"{truncated} truncated / {attempts} attempts"],
        remediation="High truncation means long sessions lose their middle; consider "
        "chunked extraction instead of head+tail truncation.",
    )
    check(
        "notes_per_attempt",
        "Notes written per extraction attempt",
        round(notes_written / attempts, 2) if attempts else None,
        True,
        unit="notes",
        details=[f"{notes_written} notes / {attempts} attempts / {written} successful runs"],
        remediation="Low yield means the pipeline burns LLM calls without producing durable "
        "notes; very high yield (>5) usually means note spam.",
    )

    storm_ratio = None
    if spawn_days:
        daily = sorted(spawn_days.values())
        med = median(daily)
        storm_ratio = round(max(daily) / max(med, 1), 1)
    check(
        "spawn_storm_ratio",
        "Busiest triage day vs median day",
        storm_ratio,
        False,
        unit="x",
        details=[f"{d}: {c} spawns" for d, c in spawn_days.most_common(5)],
        remediation="A spawn storm (like 2026-07-01: 1473 spawns from the pi backlog drain) "
        "floods the vault with backdated ephemeral notes; rate-limit backlog processing.",
    )
    check(
        "bridge_failures_per_day",
        "Pi-bridge status/tool-context/capture failures per day",
        round(bridge_failures / window_days, 2),
        False,
        unit="per day",
        details=[f"{a}: {counts[a]}" for a in BRIDGE_FAILURE_ACTIONS if counts[a]],
        remediation="Recurring bridge failures usually mean a broken python runtime on the "
        "host (see the Python 3.9 tracebacks in triage-health.jsonl).",
    )

    # Spend report: informational, no thresholds. Token counts only exist for
    # API backends; CLI backends report bytes and duration only.
    spend_details = [
        f"attempts: {attempts}, avg/day: {round(attempts / window_days, 1)}",
        f"prompt bytes total: {int(spend['prompt_bytes']):,}",
        f"output bytes total: {int(spend['output_bytes']):,}",
        f"input tokens total: {int(spend['input_tokens']):,} (0 means CLI backend, untracked)",
        f"output tokens total: {int(spend['output_tokens']):,}",
        f"avg LLM duration: {int(spend['duration_ms'] / max(spend_events, 1)):,} ms",
    ] + [f"backend/model {k}: {v} calls" for k, v in backend_models.most_common(5)]
    results.append(
        CheckResult(
            id=f"{SUITE}.spend_report",
            suite=SUITE,
            title=f"Capture LLM spend (last {window_days}d, informational)",
            status=PASS,
            value=attempts,
            unit="LLM calls",
            details=spend_details,
            remediation="Token counts are only recorded for API backends; to track real "
            "spend, move triage to anthropic-api or record CLI token usage.",
        )
    )

    retrieval_log = context.get("retrieval_log") or config_dir() / "retrieval.jsonl"
    recent_retrieval = list(iter_jsonl(retrieval_log, since=window_start(7))) if retrieval_log.exists() else []
    results.append(
        CheckResult(
            id=f"{SUITE}.retrieval_log_enabled",
            suite=SUITE,
            title="Retrieval telemetry is being recorded (last 7d)",
            status=PASS if recent_retrieval else WARN,
            value=len(recent_retrieval),
            unit="events",
            remediation="Injection value cannot be measured without retrieval telemetry; "
            "set retrieval_log: true in memento.yml (or MEMENTO_DEBUG=1) and re-run "
            "memento-vault retrieval-report after a few days.",
        )
    )

    return results
