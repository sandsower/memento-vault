"""Batch failure synthesis for sanitized external run summaries.

Memento is not a run ledger. This module accepts only compact, sanitized
summary records from external runners and produces deterministic learning/action
candidates. Raw run stores, transcripts, logs, and proof dumps are rejected at
schema-validation time.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from memento.utils import sanitize_secrets

FAILURE_CATEGORIES = ("memory", "process", "agent", "harness", "environment", "requirement")

_RAW_DUMP_KEYS = {
    "artifact",
    "artifacts",
    "event",
    "events",
    "evidence",
    "full_output",
    "console",
    "full_console",
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
    "stacktrace",
    "store",
    "terminal",
    "trace",
    "traceback",
    "transcript",
    "transcript_path",
}

_MAX_SUMMARY_CHARS = 4_000
_MAX_SUMMARY_LINES = 80


@dataclass(frozen=True)
class NormalizedFailure:
    run_ref: str
    category: str
    failure_type: str
    signature: str
    signal: str
    detail: str
    phase: str
    command: str


def synthesize_failure_batch(run_summaries: Any, *, max_candidates: int = 20) -> dict[str, Any]:
    """Return a dry-run synthesis report from sanitized external summaries.

    Accepted input is either a list of run-summary dictionaries or a dictionary
    with a ``summaries`` list. The schema is intentionally summary-shaped: raw
    logs, transcripts, proofs, ledgers, event streams, stdout/stderr dumps, and
    very long multiline strings are rejected.
    """
    summaries = _extract_summaries(run_summaries)
    if isinstance(summaries, dict) and summaries.get("error"):
        return summaries

    if max_candidates < 1:
        max_candidates = 1

    normalized: list[NormalizedFailure] = []
    for index, summary in enumerate(summaries):
        validation_error = _validate_summary(summary, path=f"summaries[{index}]")
        if validation_error:
            return validation_error
        normalized.extend(_normalize_summary_failures(summary, index))

    groups = _group_failures(normalized)
    candidate_lessons = _candidate_lessons(groups, max_candidates=max_candidates)
    candidate_actions = _candidate_actions(groups, max_candidates=max_candidates)
    category_counts = Counter(failure.category for failure in normalized)

    return {
        "dry_run": True,
        "schema": "sanitized_run_summary_batch/v1",
        "input_count": len(summaries),
        "failure_count": len(normalized),
        "category_counts": {category: category_counts.get(category, 0) for category in FAILURE_CATEGORIES},
        "groups": groups[:max_candidates],
        "candidate_lessons": candidate_lessons,
        "candidate_actions": candidate_actions,
        "approval_required": (
            "No vault writes or external repo mutations were performed. Pass approve_writes=true "
            "to store candidate lessons; candidate actions are always advisory."
        ),
    }


def _extract_summaries(payload: Any) -> list[dict[str, Any]] | dict[str, Any]:
    if isinstance(payload, list):
        summaries = payload
    elif isinstance(payload, dict):
        if _has_raw_dump_key(payload):
            return _schema_error("raw run store/log dump fields are not accepted", path=".")
        summaries = payload.get("summaries") or payload.get("run_summaries")
    else:
        return _schema_error("expected a list of sanitized summaries or an object with summaries")

    if not isinstance(summaries, list) or not summaries:
        return _schema_error("summaries must be a non-empty list")
    if not all(isinstance(item, dict) for item in summaries):
        return _schema_error("each summary must be an object")
    return summaries


def _validate_summary(value: Any, *, path: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).strip().lower()
            if key_text in _RAW_DUMP_KEYS:
                return _schema_error("raw run store/log dump fields are not accepted", path=f"{path}.{key}")
            error = _validate_summary(nested, path=f"{path}.{key}")
            if error:
                return error
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            error = _validate_summary(nested, path=f"{path}[{index}]")
            if error:
                return error
    elif isinstance(value, str):
        if len(value) > _MAX_SUMMARY_CHARS or value.count("\n") > _MAX_SUMMARY_LINES:
            return _schema_error("summary strings must be compact; raw dumps are not accepted", path=path)
    return None


def _has_raw_dump_key(value: dict[str, Any]) -> bool:
    return any(str(key).strip().lower() in _RAW_DUMP_KEYS for key in value)


def _schema_error(message: str, *, path: str = "") -> dict[str, Any]:
    return {"error": message, "reason": "invalid_summary_schema", "path": path}


def _normalize_summary_failures(summary: dict[str, Any], index: int) -> list[NormalizedFailure]:
    run_ref = _run_ref(summary, index)
    failures = summary.get("failures") or summary.get("failure_summaries") or summary.get("issues") or []
    if isinstance(failures, dict):
        failures = [failures]
    if not isinstance(failures, list):
        failures = []

    records = [_normalize_failure(summary, failure, run_ref) for failure in failures if isinstance(failure, dict)]
    if not records and _looks_failure_like(summary):
        records.append(_normalize_failure(summary, summary, run_ref))
    return records


def _normalize_failure(summary: dict[str, Any], failure: dict[str, Any], run_ref: str) -> NormalizedFailure:
    command = _first_text(failure, "command", "gate", "check")
    phase = _first_text(failure, "phase", "stage", "step")
    signal = _first_text(failure, "signal", "title", "name", "message", "summary", "type")
    detail = _first_text(failure, "detail", "details", "description", "cause", "lesson")
    if not signal:
        signal = _first_text(summary, "summary", "outcome", "title") or "unspecified failure"

    failure_text = " ".join(part for part in [signal, detail, phase, command] if part)
    text = failure_text or _first_text(summary, "summary", "outcome", "title")
    explicit_category = str(failure.get("category") or failure.get("failure_category") or "").strip().lower()
    category = explicit_category if explicit_category in FAILURE_CATEGORIES else _classify_category(text)
    failure_type = _classify_type(text)
    signature = _signature(failure_type, command or signal or detail)

    return NormalizedFailure(
        run_ref=run_ref,
        category=category,
        failure_type=failure_type,
        signature=signature,
        signal=sanitize_secrets(signal)[:300],
        detail=sanitize_secrets(detail)[:600],
        phase=sanitize_secrets(phase)[:120],
        command=sanitize_secrets(command)[:160],
    )


def _run_ref(summary: dict[str, Any], index: int) -> str:
    for key in ("run_id", "id", "session_id", "ticket", "branch"):
        value = str(summary.get(key) or "").strip()
        if value:
            return sanitize_secrets(value)[:120]
    digest = hashlib.sha256(str(summary).encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"summary-{index + 1}-{digest}"


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_failure_like(summary: dict[str, Any]) -> bool:
    text = " ".join(str(summary.get(key) or "") for key in ("outcome", "summary", "status", "title")).lower()
    return any(word in text for word in ("fail", "failed", "blocked", "missing", "ambiguous", "proof", "error"))


def _classify_category(text: str) -> str:
    lowered = text.lower()
    if _contains(lowered, "memory", "memento", "recall", "retrieval", "context packet", "note not found"):
        return "memory"
    if _contains(lowered, "ambiguous", "requirement", "acceptance", "scope", "spec", "unclear"):
        return "requirement"
    if _contains(lowered, "harness", "orchestrator", "rondo", "beislið", "beislid", "linear", "github api"):
        return "harness"
    if _contains(
        lowered, "environment", "dependency", "install", "network", "permission", "venv", "path", "rate limit"
    ):
        return "environment"
    if _contains(lowered, "agent", "ignored", "loop", "hallucinat", "wrong file", "forgot"):
        return "agent"
    return "process"


def _classify_type(text: str) -> str:
    lowered = text.lower()
    if _contains(lowered, "memory", "memento", "recall", "retrieval", "context packet", "note not found"):
        return "memory_not_retrieved"
    if _contains(lowered, "missing gate", "no gate", "gate not", "not run", "skipped gate", "missing process"):
        return "missing_process_or_gate"
    if _contains(lowered, "proof", "evidence", "validation", "not verified", "no screenshot"):
        return "proof_gap"
    if _contains(lowered, "ambiguous", "requirement", "acceptance", "scope", "spec", "unclear"):
        return "ambiguous_requirements"
    if _contains(lowered, "harness", "orchestrator", "rondo", "beislið", "beislid", "linear", "github api"):
        return "harness_failure"
    if _contains(
        lowered, "environment", "dependency", "install", "network", "permission", "venv", "path", "rate limit"
    ):
        return "environment_failure"
    if _contains(lowered, "gate", "test", "pytest", "ruff", "compileall", "ci", "check failed"):
        return "gate_failure"
    if _contains(lowered, "agent", "ignored", "loop", "hallucinat", "wrong file", "forgot"):
        return "agent_failure"
    return "agent_failure"


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _signature(failure_type: str, text: str) -> str:
    normalized = re.sub(r"\b/tmp/[^\s]+", "<tmp>", text.lower())
    normalized = re.sub(r"\b[0-9a-f]{7,}\b", "<hash>", normalized)
    normalized = re.sub(r"[^a-z0-9_.:/-]+", " ", normalized).strip()
    tokens = normalized.split()[:12]
    if not tokens:
        tokens = ["unspecified"]
    return f"{failure_type}:{' '.join(tokens)}"


def _group_failures(failures: list[NormalizedFailure]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[NormalizedFailure]] = defaultdict(list)
    for failure in failures:
        buckets[(failure.category, failure.failure_type, failure.signature)].append(failure)

    groups = []
    for index, ((category, failure_type, signature), items) in enumerate(buckets.items(), start=1):
        run_refs = sorted({item.run_ref for item in items})
        examples = []
        for item in items[:3]:
            example = {"run_ref": item.run_ref, "signal": item.signal}
            if item.phase:
                example["phase"] = item.phase
            if item.command:
                example["command"] = item.command
            if item.detail:
                example["detail"] = item.detail
            examples.append(example)
        group_id = f"fg-{index:03d}"
        groups.append(
            {
                "id": group_id,
                "category": category,
                "failure_type": failure_type,
                "signature": signature,
                "occurrence_count": len(items),
                "run_count": len(run_refs),
                "repeated": len(items) >= 2 or len(run_refs) >= 2,
                "source_runs": run_refs[:10],
                "examples": examples,
            }
        )

    groups.sort(key=lambda item: (-int(item["occurrence_count"]), str(item["category"]), str(item["signature"])))
    for index, group in enumerate(groups, start=1):
        group["id"] = f"fg-{index:03d}"
    return groups


def _candidate_lessons(groups: list[dict[str, Any]], *, max_candidates: int) -> list[dict[str, Any]]:
    candidates = []
    for group in groups[:max_candidates]:
        note_type = "pattern" if group["repeated"] else "discovery"
        title = _lesson_title(group)
        body = _lesson_body(group)
        candidates.append(
            {
                "id": f"lesson-{group['id']}",
                "group_ids": [group["id"]],
                "title": title,
                "body": body,
                "note_type": note_type,
                "tags": ["automation", "batch-failure", str(group["category"]), str(group["failure_type"])],
                "certainty": 3 if group["repeated"] else 2,
            }
        )
    return candidates


def _lesson_title(group: dict[str, Any]) -> str:
    kind = str(group["failure_type"]).replace("_", " ")
    return f"Automation {kind} pattern: {str(group['signature']).split(':', 1)[-1][:60]}".strip()


def _lesson_body(group: dict[str, Any]) -> str:
    lines = [
        f"Detected {group['occurrence_count']} sanitized failure occurrence(s) across {group['run_count']} run(s).",
        f"Category: {group['category']}",
        f"Failure type: {group['failure_type']}",
        f"Signature: `{group['signature']}`",
        "",
        "## Signals",
    ]
    for example in group.get("examples", []):
        bits = [f"run `{example.get('run_ref')}`", str(example.get("signal") or "").strip()]
        if example.get("command"):
            bits.append(f"command `{example['command']}`")
        if example.get("phase"):
            bits.append(f"phase `{example['phase']}`")
        lines.append(f"- {' — '.join(bit for bit in bits if bit)}")
    lines.extend(
        [
            "",
            "## Suggested learning",
            _suggested_learning(str(group["failure_type"]), str(group["category"])),
            "",
            "Boundary: derived only from sanitized external run summaries; no raw logs, proofs, transcripts, or run-ledger records were stored.",
        ]
    )
    return "\n".join(lines)


def _suggested_learning(failure_type: str, category: str) -> str:
    suggestions = {
        "memory_not_retrieved": "Improve pre-run memory recall prompts or capture a durable retrieval guide for this class of miss.",
        "missing_process_or_gate": "Document the missing process step and consider adding a runner-side gate or checklist item.",
        "proof_gap": "Make expected proof surfaces explicit in the work contract and add a runner-side proof check where possible.",
        "ambiguous_requirements": "Route similar work through specification before implementation and capture clarified acceptance criteria.",
        "harness_failure": "File or update a harness issue with sanitized reproduction context; keep execution evidence outside Memento.",
        "environment_failure": "Document environment prerequisites or add a preflight in the owning runner/repo.",
        "gate_failure": "Capture the durable fix pattern if repeated; add or tune tests/gates in the owning repo, not in Memento as run evidence.",
        "agent_failure": "Capture an agent guidance pattern or checklist that prevents the repeated mistake.",
    }
    return suggestions.get(failure_type, f"Capture a concise {category} lesson if this pattern remains actionable.")


def _candidate_actions(groups: list[dict[str, Any]], *, max_candidates: int) -> list[dict[str, Any]]:
    actions = []
    for group in groups[:max_candidates]:
        action_type, target, description = _action_for_group(group)
        actions.append(
            {
                "id": f"action-{group['id']}",
                "group_ids": [group["id"]],
                "type": action_type,
                "target": target,
                "description": description,
                "requires_external_approval": True,
            }
        )
    return actions


def _action_for_group(group: dict[str, Any]) -> tuple[str, str, str]:
    failure_type = str(group["failure_type"])
    if failure_type == "memory_not_retrieved":
        return ("note", "memory-guidance", "Capture or refine a retrieval/capture lesson for the repeated memory miss.")
    if failure_type == "missing_process_or_gate":
        return ("gate", "runner-or-repo", "Add a runner-side checklist/gate for the missing process step.")
    if failure_type == "proof_gap":
        return ("docs", "work-contract-proof", "Document the proof surface expected for this work class.")
    if failure_type == "ambiguous_requirements":
        return ("issue", "requirements", "Open a scoped requirements/spec clarification issue in the owning project.")
    if failure_type == "harness_failure":
        return ("issue", "harness", "Open a harness issue with sanitized reproduction details.")
    if failure_type == "environment_failure":
        return ("docs", "environment", "Document prerequisite/preflight checks for the failing environment condition.")
    if failure_type == "gate_failure":
        return ("gate", "owning-repo", "Tune or add a deterministic gate/test in the owning repository.")
    return ("note", "agent-guidance", "Capture an agent guidance lesson or checklist for the repeated mistake.")
