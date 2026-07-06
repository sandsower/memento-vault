"""Typed automated-run lesson capture contract.

Memento is not a run ledger. This module accepts one compact, sanitized lesson
candidate from an external runner and either queues it for human review or,
when explicitly approved, writes one curated note with provenance references.
Raw logs, transcripts, ledgers, proof dumps, and patch blobs are rejected before
anything reaches the vault.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from memento.config import RUNTIME_DIR, get_vault
from memento.smart_store import write_smart_store_note
from memento.store import acquire_vault_write_lock, release_vault_write_lock
from memento.utils import sanitize_secrets

AUTOMATED_RUN_LESSON_SCHEMA = "automated_run_lesson_candidate/v1"

OUTCOME_CLASSIFICATIONS = {
    "success",
    "failure",
    "blocked",
    "partial",
    "cancelled",
    "canceled",
    "unknown",
}
LESSON_TYPES = {
    "memory",
    "process",
    "agent",
    "harness",
    "environment",
    "requirement",
    "quality",
    "architecture",
    "tooling",
}
NOTE_TYPES = {"decision", "discovery", "pattern", "bugfix", "tool", "architecture"}

_RAW_DUMP_KEYS = {
    "artifact",
    "artifacts",
    "console",
    "diff",
    "event",
    "events",
    "evidence",
    "full_output",
    "ledger",
    "log",
    "logs",
    "output",
    "outputs",
    "patch",
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
    "unified_diff",
}
_MAX_TEXT_CHARS = 2_000
_MAX_TEXT_LINES = 40
_MAX_REF_CHARS = 300
_PATCH_PATTERNS = (
    re.compile(r"(?m)^diff --git\s"),
    re.compile(r"(?m)^@@\s+-\d"),
    re.compile(r"(?m)^\+\+\+\s+[ab]/"),
    re.compile(r"(?m)^---\s+[ab]/"),
)


class AutomatedRunLessonCandidate(TypedDict, total=False):
    schema: str
    external_system: str
    run_id: str
    artifact_refs: list[str]
    repo: str
    project: str
    branch: str
    ticket: str
    slice: str
    outcome: str
    lesson_type: str
    title: str
    body: str
    note_type: str
    evidence_summary: str
    certainty: int
    validity_context: str
    related_refs: list[str]
    extra_tags: list[str]


def queue_path() -> Path:
    """Return the local review queue path for automation lesson candidates."""

    return Path(RUNTIME_DIR) / "automation-run-lessons.jsonl"


def capture_automated_run_lesson(candidate: dict[str, Any], *, approve_write: bool = False) -> dict[str, Any]:
    """Queue or write one automated-run lesson candidate.

    ``approve_write`` is the explicit boundary: false queues the normalized
    candidate outside the vault for review; true writes one curated note through
    smart-store. In both modes unsafe raw artifacts are rejected and all strings
    get defense-in-depth secret redaction.
    """

    normalized = normalize_lesson_candidate(candidate)
    if normalized.get("error"):
        return normalized

    if not approve_write:
        return queue_lesson_candidate(normalized["candidate"])

    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}", "reason": "vault_missing"}

    if not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock", "reason": "lock_timeout"}

    try:
        lesson = normalized["candidate"]
        write = lesson_to_note_payload(lesson)
        result = write_smart_store_note(**write)
        return {
            "schema": AUTOMATED_RUN_LESSON_SCHEMA,
            "created": bool(result.get("created")),
            "write_result": result,
            **({"path": result["path"]} if result.get("path") else {}),
            "queued": False,
        }
    finally:
        release_vault_write_lock()


def queue_lesson_candidate(candidate: AutomatedRunLessonCandidate) -> dict[str, Any]:
    """Append a normalized candidate to the local review queue, not the vault."""

    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    queued_id = f"arl-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    record = {
        "id": queued_id,
        "schema": AUTOMATED_RUN_LESSON_SCHEMA,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "candidate": candidate,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"id": queued_id, "schema": AUTOMATED_RUN_LESSON_SCHEMA, "queued": True, "queue_path": str(path)}


def normalize_lesson_candidate(value: Any) -> dict[str, Any]:
    """Validate and sanitize an automated-run lesson candidate."""

    if not isinstance(value, dict):
        return _schema_error("candidate must be an object")
    error = _reject_unsafe_shape(value, path="candidate")
    if error:
        return error

    def text(*keys: str) -> str:
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return _clean_text(raw)
        return ""

    external_system = text("external_system", "system", "runner")
    run_id = text("run_id", "run_ref", "id", "session_id")
    title = text("title")
    evidence_summary = text("evidence_summary", "summary", "lesson", "body")
    lesson_type = _normalize_enum(text("lesson_type", "category"), LESSON_TYPES, "process")
    outcome = _normalize_enum(text("outcome", "outcome_classification", "status"), OUTCOME_CLASSIFICATIONS, "unknown")
    note_type = _normalize_enum(text("note_type"), NOTE_TYPES, "discovery")

    missing = []
    if not external_system:
        missing.append("external_system")
    if not run_id:
        missing.append("run_id")
    if not title:
        missing.append("title")
    if not evidence_summary:
        missing.append("evidence_summary")
    if missing:
        return _schema_error(f"missing required field(s): {', '.join(missing)}", path="candidate")

    certainty_raw = value.get("certainty", 2)
    try:
        certainty = int(certainty_raw)
    except (TypeError, ValueError):
        return _schema_error("certainty must be an integer from 1 to 5", path="candidate.certainty")
    if not 1 <= certainty <= 5:
        return _schema_error("certainty must be an integer from 1 to 5", path="candidate.certainty")

    artifact_refs = _string_list(value.get("artifact_refs") or value.get("artifact_references") or [])
    related_refs = _string_list(value.get("related_refs") or value.get("related") or [])
    extra_tags = _string_list(value.get("extra_tags") or value.get("tags") or [])

    candidate: AutomatedRunLessonCandidate = {
        "schema": AUTOMATED_RUN_LESSON_SCHEMA,
        "external_system": external_system,
        "run_id": run_id,
        "artifact_refs": artifact_refs,
        "repo": text("repo", "repository"),
        "project": text("project", "cwd"),
        "branch": text("branch"),
        "ticket": text("ticket", "issue"),
        "slice": text("slice", "work_slice"),
        "outcome": outcome,
        "lesson_type": lesson_type,
        "title": title,
        "body": _clean_text(value.get("body") if isinstance(value.get("body"), str) else evidence_summary),
        "note_type": note_type,
        "evidence_summary": evidence_summary,
        "certainty": certainty,
        "validity_context": text("validity_context", "validity-context"),
        "related_refs": related_refs,
        "extra_tags": extra_tags,
    }
    return {"candidate": candidate}


def lesson_to_note_payload(candidate: AutomatedRunLessonCandidate) -> dict[str, Any]:
    """Convert a normalized candidate into a managed-frontmatter note payload."""

    tags = ["automation", "automated-run", candidate["lesson_type"], candidate["outcome"]]
    if candidate.get("ticket"):
        tags.append(candidate["ticket"])
    for extra in candidate.get("extra_tags") or []:
        if extra not in tags:
            tags.append(extra)

    lines = [
        candidate.get("body") or candidate["evidence_summary"],
        "",
        "## Automated run provenance",
        f"- External system: {candidate['external_system']}",
        f"- Run ID: `{candidate['run_id']}`",
    ]
    for key, label in (
        ("repo", "Repository"),
        ("project", "Project"),
        ("branch", "Branch"),
        ("ticket", "Ticket"),
        ("slice", "Slice"),
    ):
        if candidate.get(key):
            lines.append(f"- {label}: {candidate[key]}")
    lines.extend(
        [
            f"- Outcome: {candidate['outcome']}",
            f"- Lesson type: {candidate['lesson_type']}",
            "",
            "## Evidence summary",
            candidate["evidence_summary"],
        ]
    )
    if candidate.get("artifact_refs"):
        lines.append("")
        lines.append("## Artifact refs")
        lines.extend(f"- {ref}" for ref in candidate["artifact_refs"])
    if candidate.get("related_refs"):
        lines.append("")
        lines.append("## Related refs")
        lines.extend(f"- {ref}" for ref in candidate["related_refs"])
    lines.append("")
    lines.append(
        "Boundary: captured from a typed automated-run lesson candidate; raw logs, transcripts, proofs, run ledgers, and patch blobs were not stored."
    )

    validity = (
        candidate.get("validity_context") or "Valid for the referenced automated run context and similar future runs."
    )
    return {
        "title": candidate["title"],
        "body": "\n".join(lines).strip(),
        "note_type": candidate.get("note_type") or "discovery",
        "tags": tags,
        "certainty": candidate.get("certainty") or 2,
        "project": candidate.get("project") or None,
        "branch": candidate.get("branch") or None,
        "session_id": candidate.get("run_id") or None,
        "validity_context": validity,
        "origin": f"automated_run_lesson:{candidate['external_system']}",
    }


def lesson_candidate_from_batch_candidate(
    candidate: dict[str, Any], *, project: str = "", branch: str = "", session_id: str = ""
) -> dict[str, Any]:
    """Adapt a batch-synthesis lesson candidate to the typed lesson schema."""

    group_ids = candidate.get("group_ids") or []
    body = str(candidate.get("body") or "")
    tags = [str(tag) for tag in candidate.get("tags") or []]
    lesson_type = next((tag for tag in tags if tag in LESSON_TYPES), "process")
    return {
        "external_system": "memento_synthesize_failures",
        "run_id": session_id or ",".join(str(item) for item in group_ids) or str(candidate.get("id") or "batch"),
        "artifact_refs": [str(item) for item in group_ids],
        "project": project,
        "branch": branch,
        "outcome": "failure",
        "lesson_type": lesson_type,
        "title": str(candidate.get("title") or "").strip(),
        "body": body,
        "note_type": str(candidate.get("note_type") or "discovery"),
        "evidence_summary": body.split("## Suggested learning", 1)[0].strip() or body,
        "certainty": int(candidate.get("certainty") or 2),
        "validity_context": "Derived from sanitized batch failure summaries; revisit if source run grouping changes.",
        "related_refs": [],
    }


def _schema_error(message: str, *, path: str = "candidate") -> dict[str, Any]:
    return {"error": message, "reason": "invalid_automated_run_lesson", "path": path}


def _reject_unsafe_shape(value: Any, *, path: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).strip().lower()
            if key_text in _RAW_DUMP_KEYS:
                return _schema_error(
                    "raw run artifacts/logs/transcripts/patches are not accepted", path=f"{path}.{key}"
                )
            error = _reject_unsafe_shape(nested, path=f"{path}.{key}")
            if error:
                return error
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            error = _reject_unsafe_shape(nested, path=f"{path}[{index}]")
            if error:
                return error
    elif isinstance(value, str):
        if _looks_like_patch(value):
            return _schema_error("patch/diff blobs are not accepted", path=path)
        if len(value) > _MAX_TEXT_CHARS or value.count("\n") > _MAX_TEXT_LINES:
            return _schema_error("candidate strings must be compact summaries, not raw dumps", path=path)
    return None


def _looks_like_patch(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PATCH_PATTERNS)


def _clean_text(value: object) -> str:
    text = sanitize_secrets(str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()[:_MAX_TEXT_CHARS]


def _string_list(value: object) -> list[str]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else [value]
    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        text = _clean_text(raw)
        if not text or "\n" in text or len(text) > _MAX_REF_CHARS:
            continue
        if text in seen:
            continue
        items.append(text)
        seen.add(text)
    return items


def _normalize_enum(value: str, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if normalized == "cancelled":
        normalized = "canceled"
    return normalized if normalized in allowed else default
