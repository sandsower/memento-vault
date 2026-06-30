#!/usr/bin/env python3
"""Memory-evaluation helpers for the static Rondo benchmark fixture.

The benchmark remains a fixture/evaluation surface, not a run ledger. This module
accepts compact, sanitized outcome summaries from an external runner and produces
memory-specific classifications that can be consumed by retrieval diagnostics.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

RAW_OUTCOME_KEYS = {
    "artifact",
    "artifacts",
    "console",
    "event",
    "events",
    "full_output",
    "ledger",
    "log",
    "logs",
    "output",
    "outputs",
    "proof",
    "proofs",
    "raw",
    "raw_log",
    "raw_logs",
    "raw_output",
    "raw_outputs",
    "run_ledger",
    "run_store",
    "stderr",
    "stdout",
    "terminal",
    "trace",
    "traceback",
    "transcript",
    "transcript_path",
}

MAX_SUMMARY_CHARS = 4_000
MAX_SUMMARY_LINES = 80
INTENT_MATCH_THRESHOLD = 0.35

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]*", re.IGNORECASE)
STOPWORDS = {
    "about",
    "after",
    "agent",
    "agents",
    "and",
    "are",
    "benchmark",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "issue",
    "memory",
    "memento",
    "note",
    "notes",
    "or",
    "repo",
    "retrieval",
    "should",
    "task",
    "that",
    "the",
    "this",
    "to",
    "vault",
    "was",
    "when",
    "with",
}


def load_tasks(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load benchmark tasks keyed by stable task id."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        raise ValueError("tasks file must contain a tasks list")
    return {str(task["id"]): task for task in tasks if isinstance(task, dict) and task.get("id")}


def load_outcomes(path: str | Path) -> list[dict[str, Any]]:
    """Load sanitized outcome summaries from JSON or JSONL.

    Outcome records must be compact summaries. Raw logs, transcripts, proofs,
    ledgers, and very large multiline strings are rejected so Memento does not
    become a run store.
    """
    source = Path(path)
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            outcomes = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            if isinstance(payload, dict):
                outcomes = payload.get("outcomes") or payload.get("summaries") or payload.get("classifications")
            else:
                outcomes = payload
    else:
        outcomes = [json.loads(line) for line in text.splitlines() if line.strip()]

    if not isinstance(outcomes, list):
        raise ValueError("outcomes must be a list, JSONL stream, or object with outcomes/classifications")
    for index, outcome in enumerate(outcomes):
        if not isinstance(outcome, dict):
            raise ValueError(f"outcomes[{index}] must be an object")
        _validate_summary_shape(outcome, path=f"outcomes[{index}]")
    return outcomes


def classify_memory_outcome(task: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Classify one task outcome's memory contribution and failure mode."""
    task_id = str(task.get("id") or outcome.get("task_id") or "")
    retrieval_context = task.get("retrieval_context") if isinstance(task.get("retrieval_context"), dict) else {}
    expected_intent = str(retrieval_context.get("expected_query_intent") or "").strip()
    useful_refs = _string_list(retrieval_context.get("useful_note_refs"))
    expected_token_budget = _safe_int(retrieval_context.get("memory_token_budget"), default=0)
    requires_memory = bool(task.get("requires_vault_knowledge"))

    retrieved_refs = _string_list(
        outcome.get("retrieved_note_refs") or outcome.get("retrieved_memory_refs") or outcome.get("memory_refs")
    )
    retrieval_queries = _string_list(
        outcome.get("retrieval_queries") or outcome.get("memento_queries") or outcome.get("memory_queries")
    )
    memory_used = _safe_bool(outcome.get("memory_used"), default=bool(retrieved_refs or retrieval_queries))
    latency_ms = _safe_int(outcome.get("retrieval_latency_ms") or outcome.get("latency_ms"), default=None)
    token_budget = _safe_int(
        outcome.get("memory_token_budget") or outcome.get("token_budget"), default=expected_token_budget
    )

    evidence_text = " ".join(
        retrieval_queries
        + retrieved_refs
        + _string_list(outcome.get("memory_evidence"))
        + [str(outcome.get("summary") or ""), str(outcome.get("failure_summary") or "")]
    )
    useful_ref_matches = sorted(_normalize_ref(ref) for ref in useful_refs) and sorted(
        set(_normalize_ref(ref) for ref in useful_refs) & set(_normalize_ref(ref) for ref in retrieved_refs)
    )
    intent_score = _intent_overlap(expected_intent, evidence_text)
    relevant_memory = bool(useful_ref_matches) or (bool(expected_intent) and intent_score >= INTENT_MATCH_THRESHOLD)

    if not requires_memory:
        classification = "memory_not_required" if memory_used else "not_applicable"
        failure_type = None
    elif relevant_memory:
        classification = "used_relevant_memory"
        failure_type = None
    elif memory_used or retrieved_refs or retrieval_queries:
        classification = "irrelevant_memory"
        failure_type = "irrelevant_memory"
    else:
        classification = "memory_not_retrieved"
        failure_type = "memory_not_retrieved"

    return {
        "task_id": task_id,
        "requires_vault_knowledge": requires_memory,
        "memory_used": memory_used,
        "memory_classification": classification,
        "memory_failure_type": failure_type,
        "memory_contribution_measurable": bool(relevant_memory),
        "expected_query_intent": expected_intent,
        "intent_match_score": round(intent_score, 3),
        "useful_note_refs": useful_refs,
        "retrieved_note_refs": retrieved_refs,
        "useful_ref_matches": useful_ref_matches or [],
        "retrieval_latency_ms": latency_ms,
        "memory_token_budget": token_budget,
        "result": str(outcome.get("result") or outcome.get("status") or "unknown"),
    }


def summarize_outcomes(tasks: dict[str, dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a compact benchmark memory report from sanitized outcomes."""
    classifications: list[dict[str, Any]] = []
    missing_task_ids: list[str] = []
    for outcome in outcomes:
        task_id = str(outcome.get("task_id") or outcome.get("id") or "")
        task = tasks.get(task_id)
        if task is None:
            missing_task_ids.append(task_id or "<missing>")
            continue
        classifications.append(classify_memory_outcome(task, outcome))

    classification_counts = Counter(item["memory_classification"] for item in classifications)
    failure_counts = Counter(item["memory_failure_type"] for item in classifications if item.get("memory_failure_type"))
    latencies = [
        item["retrieval_latency_ms"] for item in classifications if item.get("retrieval_latency_ms") is not None
    ]
    token_budgets = [
        item["memory_token_budget"] for item in classifications if item.get("memory_token_budget") is not None
    ]

    return {
        "schema": "rondo_benchmark_memory_report/v1",
        "outcome_count": len(outcomes),
        "classified_count": len(classifications),
        "missing_task_ids": missing_task_ids,
        "memory_required_count": sum(1 for item in classifications if item["requires_vault_knowledge"]),
        "memory_used_count": sum(1 for item in classifications if item["memory_used"]),
        "memory_contribution_measurable_count": sum(
            1 for item in classifications if item["memory_contribution_measurable"]
        ),
        "classification_counts": dict(sorted(classification_counts.items())),
        "failure_counts": dict(sorted(failure_counts.items())),
        "latency_ms": _metric_summary(latencies),
        "token_budget": _metric_summary(token_budgets),
        "classifications": classifications,
    }


def _validate_summary_shape(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).strip().lower()
            if key_text in RAW_OUTCOME_KEYS:
                raise ValueError(f"{path}.{key}: raw run store/log fields are not accepted")
            _validate_summary_shape(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_summary_shape(nested, path=f"{path}[{index}]")
    elif isinstance(value, str) and (len(value) > MAX_SUMMARY_CHARS or value.count("\n") > MAX_SUMMARY_LINES):
        raise ValueError(f"{path}: summary strings must be compact")


def _safe_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "used"}:
            return True
        if lowered in {"0", "false", "no", "n", "unused"}:
            return False
    return default


def _safe_int(value: Any, *, default: int | None = 0) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalize_ref(value: str) -> str:
    text = value.strip().replace("\\", "/")
    if text.endswith(".md"):
        text = text[:-3]
    if text.startswith("notes/"):
        text = text[6:]
    return text.lower()


def _intent_overlap(expected_intent: str, evidence_text: str) -> float:
    expected_tokens = _important_tokens(expected_intent)
    if not expected_tokens:
        return 0.0
    evidence_tokens = _important_tokens(evidence_text)
    if not evidence_tokens:
        return 0.0
    return len(expected_tokens & evidence_tokens) / len(expected_tokens)


def _important_tokens(text: str) -> set[str]:
    return {
        token.lower() for token in TOKEN_RE.findall(text.lower()) if len(token) > 2 and token.lower() not in STOPWORDS
    }


def _metric_summary(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "avg": None, "p95": None, "max": None}
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95 + 0.999999) - 1))
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 1),
        "p95": ordered[index],
        "max": ordered[-1],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify Rondo benchmark memory outcomes")
    parser.add_argument("--tasks", default=str(Path(__file__).with_name("tasks.json")), help="Path to tasks.json")
    parser.add_argument("--outcomes", required=True, help="Path to sanitized outcome JSON/JSONL")
    parser.add_argument("--output", help="Write JSON report to this path")
    args = parser.parse_args(argv)

    report = summarize_outcomes(load_tasks(args.tasks), load_outcomes(args.outcomes))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(args.output)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
