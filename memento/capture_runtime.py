"""Host-neutral runtime for queued Memento capture processing.

The Pi bridge owns CLI/extension adaptation, but this module owns the queued
capture processing state machine: selection, grouping, run setup, result
validation/finalization, and retry planning. Production code supplies small
ports for filesystem/queue/vault side effects; tests exercise the runtime
through those same ports.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Protocol


class QueueStore(Protocol):
    def load(self, vault: Path) -> list[dict[str, Any]]: ...

    def write(self, captures: list[dict[str, Any]], vault: Path) -> None: ...

    def path(self, vault: Path) -> Path: ...

    def lock(self) -> ContextManager[None]: ...


class ProcessingStore(Protocol):
    def root(self) -> Path: ...

    def acquire_lock(self, run_id: str, owner_pid: int = 0) -> dict[str, Any] | None: ...

    def release_lock(self, run_id: str) -> None: ...

    def write_text(self, path: Path, content: str) -> None: ...

    def read_json(self, path: Path) -> dict[str, Any]: ...


class GroupPreparer(Protocol):
    def prepare(
        self, run_dir: Path, vault: Path, group: dict[str, Any], transcript_max_bytes: int
    ) -> dict[str, Any]: ...


class VaultWriter(Protocol):
    def reported_note_exists(self, vault: Path, path: str) -> bool: ...

    def on_dequeued(self, vault: Path, run_id: str, dequeue_ids: set[str]) -> None: ...


@dataclass(frozen=True)
class CaptureProcessRequest:
    capture_id: str | list[str] | tuple[str, ...] = ""
    project: str = ""
    branch: str = ""
    session_id: str = ""
    limit: int = 0
    newest: bool = False
    dry_run: bool = False
    transcript_max_bytes: int = 2 * 1024 * 1024
    owner_pid: int = 0


@dataclass(frozen=True)
class RuntimeClock:
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    uuid_hex: Callable[[], str] = lambda: uuid.uuid4().hex


@dataclass
class CaptureRuntime:
    vault: Callable[[], Path]
    queue: QueueStore
    processing: ProcessingStore
    preparer: GroupPreparer
    writer: VaultWriter
    transcript_counter: Callable[[list[dict[str, Any]]], dict[str, Any]] = lambda _groups: {}
    clock: RuntimeClock = RuntimeClock()

    def process(self, request: CaptureProcessRequest) -> dict[str, Any]:
        """Create a processing run, or preview it when ``dry_run`` is set."""
        vault = self.vault()
        captures = self.queue.load(vault)
        selected = select_captures(
            captures,
            request.capture_id,
            request.project,
            request.branch,
            request.session_id,
            request.newest,
            skip_processor_captures=True,
        )
        groups = group_captures(selected)
        groups.sort(
            key=lambda group: min((capture_created_at(capture) for capture in group.get("captures", [])), default=""),
            reverse=request.newest,
        )
        selected, groups = apply_capture_limit(selected, groups, request.limit)
        summary_groups = summarize_groups(groups)
        if request.dry_run:
            return {
                "dry_run": True,
                "selected_capture_count": len(selected),
                "group_count": len(groups),
                "groups": summary_groups,
                "queue_path": str(self.queue.path(vault)),
            }
        if not groups:
            return {"run_id": None, "run_dir": None, "selected_capture_count": 0, "group_count": 0, "groups": []}

        run_id = self._new_run_id()
        lock_error = self.processing.acquire_lock(run_id, request.owner_pid)
        if lock_error:
            return lock_error
        try:
            run_dir = self.processing.root() / run_id
            for directory in (run_dir / "inputs", run_dir / "results", run_dir / "logs"):
                directory.mkdir(parents=True, exist_ok=True)

            manifest_groups = [
                self.preparer.prepare(run_dir, vault, dict(group), request.transcript_max_bytes) for group in groups
            ]
            transcript_counts = self.transcript_counter(manifest_groups)
            manifest = {
                "run_id": run_id,
                "created_at": self.clock.now().replace(microsecond=0).isoformat(),
                "queue_path": str(self.queue.path(vault)),
                "vault_path": str(vault),
                "state_root": str(self.processing.root().parent),
                "selected_capture_count": len(selected),
                "group_count": len(groups),
                "groups": manifest_groups,
                "status": "running",
                **transcript_counts,
            }
            self.processing.write_text(run_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            return {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "selected_capture_count": len(selected),
                "group_count": len(groups),
                "groups": summary_groups,
                **transcript_counts,
            }
        except Exception:
            self.processing.release_lock(run_id)
            raise

    def finalize(self, run_id: str) -> dict[str, Any]:
        vault = self.vault()
        run_dir = self.processing.root() / run_id
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            return {"error": f"processing run not found: {run_id}", "reason": "run_not_found"}

        manifest = json.loads(manifest_path.read_text(errors="replace"))
        dequeue_ids: set[str] = set()
        group_results: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        try:
            with self.queue.lock():
                captures = self.queue.load(vault)
                for group in manifest.get("groups", []):
                    group_result, valid_ids = self._finalize_group(vault, group)
                    group_results.append(group_result)
                    dequeue_ids.update(valid_ids)

                remaining = [capture for capture in captures if str(capture.get("id")) not in dequeue_ids]
                self.queue.write(remaining, vault)
                manifest["status"] = "finalized"
                manifest["finalized_at"] = self.clock.now().replace(microsecond=0).isoformat()
                manifest["dequeued_capture_ids"] = sorted(dequeue_ids)
                self.processing.write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
                if dequeue_ids:
                    self.writer.on_dequeued(vault, run_id, dequeue_ids)
        finally:
            self.processing.release_lock(run_id)
        return {"run_id": run_id, "dequeued": len(dequeue_ids), "remaining": len(remaining), "groups": group_results}

    def plan_retry(
        self, status: dict[str, Any], run_id: str = "", group_ids: list[str] | None = None
    ) -> dict[str, Any]:
        target_run_id = str(status.get("run_id") or run_id or "")
        groups = status.get("groups") if isinstance(status.get("groups"), list) else []
        if status.get("status") == "idle" or not target_run_id:
            return {"error": "processing run not found", "reason": "run_not_found"}

        requested = {str(group_id).strip() for group_id in (group_ids or []) if str(group_id).strip()}
        retry_groups = [
            group
            for group in groups
            if str(group.get("status") or "") == "failed"
            and (not requested or str(group.get("group_id") or "") in requested)
        ]
        if not retry_groups:
            return {
                "error": "no failed groups to retry",
                "reason": "no_failed_groups",
                "run_id": target_run_id,
                "group_count": len(groups),
                "groups": groups,
            }

        selected_capture_ids: list[str] = []
        for group in retry_groups:
            for capture_id in group.get("capture_ids", []):
                capture = str(capture_id).strip()
                if capture and capture not in selected_capture_ids:
                    selected_capture_ids.append(capture)

        return {
            "run_id": target_run_id,
            "source_run_id": target_run_id,
            "group_count": len(groups),
            "retry_group_count": len(retry_groups),
            "selected_group_ids": [
                str(group.get("group_id") or "") for group in retry_groups if str(group.get("group_id") or "")
            ],
            "selected_capture_count": len(selected_capture_ids),
            "selected_capture_ids": selected_capture_ids,
            "groups": retry_groups,
            "queue_path": str(self.queue.path(self.vault())),
        }

    def _new_run_id(self) -> str:
        stamp = self.clock.now().strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{self.clock.uuid_hex()[:8]}"

    def _finalize_group(self, vault: Path, group: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        expected_ids = {str(x) for x in group.get("capture_ids", [])}
        result_path = Path(str(group.get("result_json") or ""))
        if not result_path.exists():
            return {"group_id": group.get("group_id"), "status": "failed", "reason": "missing_result"}, set()
        try:
            result = json.loads(result_path.read_text(errors="replace"))
        except json.JSONDecodeError:
            return {"group_id": group.get("group_id"), "status": "failed", "reason": "invalid_result_json"}, set()

        processed_ids = {str(x) for x in result.get("processed_capture_ids", [])}
        status = str(result.get("status") or "")
        result_state = str(result.get("result_state") or "")
        if status == "failed":
            return (
                {
                    "group_id": group.get("group_id"),
                    "status": "failed",
                    "reason": result_state or str(result.get("reason") or "failed_result"),
                    "result_state": result_state or "failed",
                },
                set(),
            )

        valid = processed_ids == expected_ids and status in {"processed", "processed_no_notes"}
        reason = "ok"
        if not valid:
            reason = "invalid_status_or_capture_ids"
        elif status == "processed_no_notes" and not str(result.get("discard_reason") or "").strip():
            valid = False
            reason = "missing_discard_reason"
        elif status == "processed":
            created = result.get("created", [])
            if not created:
                valid = False
                reason = "missing_created_note"
            for note in created:
                path = note.get("path") if isinstance(note, dict) else note
                if not path or not self.writer.reported_note_exists(vault, str(path)):
                    valid = False
                    reason = "missing_created_note"
                    break

        if valid:
            return (
                {
                    "group_id": group.get("group_id"),
                    "status": status,
                    "reason": reason,
                    "result_state": result_state or "success",
                    "dequeued_capture_ids": sorted(expected_ids),
                },
                expected_ids,
            )
        return (
            {
                "group_id": group.get("group_id"),
                "status": "failed",
                "reason": reason,
                "result_state": result_state or "invalid_result",
            },
            set(),
        )


def capture_created_at(capture: dict[str, Any]) -> str:
    return str(capture.get("created_at") or capture.get("date") or "")


def normalize_capture_ids(capture_id: str | list[str] | tuple[str, ...] = "") -> set[str]:
    if isinstance(capture_id, (list, tuple)):
        return {str(item) for item in capture_id if str(item)}
    return {str(capture_id)} if capture_id else set()


def select_captures(
    captures: list[dict[str, Any]],
    capture_id: str | list[str] | tuple[str, ...] = "",
    project: str = "",
    branch: str = "",
    session_id: str = "",
    newest: bool = False,
    *,
    skip_processor_captures: bool = True,
) -> list[dict[str, Any]]:
    selected = []
    capture_ids = normalize_capture_ids(capture_id)
    for capture in captures:
        metadata = capture.get("metadata") or {}
        if capture_ids and str(capture.get("id")) not in capture_ids:
            continue
        if project and metadata.get("project") != project:
            continue
        if branch and metadata.get("branch") != branch:
            continue
        if session_id and metadata.get("session_id") != session_id:
            continue
        if skip_processor_captures and metadata.get("memento_processor") is True:
            continue
        selected.append(capture)
    selected.sort(key=capture_created_at, reverse=newest)
    return selected


def safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return cleaned[:120] or uuid.uuid4().hex


def group_captures(captures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for capture in captures:
        metadata = capture.get("metadata") or {}
        session_id = metadata.get("session_id") if metadata.get("session_id") != "unknown" else None
        key = f"session:{session_id}" if session_id else f"capture:{capture.get('id')}"
        if key not in groups:
            groups[key] = {
                "group_id": safe_segment(key),
                "group_key": key,
                "session_id": session_id,
                "project": metadata.get("project"),
                "branch": metadata.get("branch"),
                "cwd": metadata.get("cwd"),
                "capture_ids": [],
                "captures": [],
            }
        groups[key]["capture_ids"].append(capture.get("id"))
        groups[key]["captures"].append(capture)
    return list(groups.values())


def apply_capture_limit(
    selected: list[dict[str, Any]], groups: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not limit or limit <= 0:
        return selected, groups
    limited_ids: list[str] = []
    for group in groups:
        for capture in group.get("captures", []):
            if len(limited_ids) >= limit:
                break
            limited_ids.append(str(capture.get("id")))
        if len(limited_ids) >= limit:
            break
    selected_ids = set(limited_ids)
    limited_selected = [capture for capture in selected if str(capture.get("id")) in selected_ids]
    limited_groups = []
    for group in groups:
        captures_in_limit = [capture for capture in group.get("captures", []) if str(capture.get("id")) in selected_ids]
        if captures_in_limit:
            limited_groups.append(
                {
                    **group,
                    "captures": captures_in_limit,
                    "capture_ids": [capture.get("id") for capture in captures_in_limit],
                }
            )
    return limited_selected, limited_groups


def summarize_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "group_id": group["group_id"],
            "session_id": group.get("session_id"),
            "project": group.get("project"),
            "branch": group.get("branch"),
            "capture_ids": group.get("capture_ids", []),
            "capture_count": len(group.get("capture_ids", [])),
        }
        for group in groups
    ]


class NullVaultWriter:
    def reported_note_exists(self, vault: Path, path: str) -> bool:
        return False

    def on_dequeued(self, vault: Path, run_id: str, dequeue_ids: set[str]) -> None:
        return None


class MemoryQueueStore:
    """Small test adapter for runtime unit tests."""

    def __init__(self, captures: list[dict[str, Any]], queue_path: Path):
        self.captures = captures
        self.queue_path = queue_path

    def load(self, vault: Path) -> list[dict[str, Any]]:
        return list(self.captures)

    def write(self, captures: list[dict[str, Any]], vault: Path) -> None:
        self.captures = list(captures)

    def path(self, vault: Path) -> Path:
        return self.queue_path

    def lock(self) -> ContextManager[None]:
        return contextlib.nullcontext()
