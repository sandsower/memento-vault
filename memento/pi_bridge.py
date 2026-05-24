"""JSON CLI adapter for pi's TypeScript extension.

The pi runtime loads TypeScript/JavaScript extensions, so the extension calls
this module as a short-lived Python process. Lifecycle policy remains in
memento.lifecycle; this module only translates CLI JSON to LifecycleResult JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
import subprocess
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from memento.config import detect_project, get_config, get_vault
from memento.lifecycle import build_briefing, build_recall, build_tool_context, strip_injection
from memento.search import (
    enhance_results,
    filter_by_project,
    has_qmd,
    miss_envelope,
    normalize_miss_reason,
    qmd_get,
    qmd_search_with_extras,
    resolve_concrete_mode,
)
from memento.remote_client import get as remote_get
from memento.remote_client import is_remote, search_envelope as remote_search_envelope, status as remote_status
from memento.store import write_note


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _error_payload(source: str, exc: Exception) -> dict[str, Any]:
    return {
        "should_inject": False,
        "content": "",
        "source": source,
        "results": [],
        "reason": "error",
        "metadata": {
            "error": str(exc),
            "error_type": type(exc).__name__,
        },
    }


def _run_lifecycle(source: str, fn, *args: Any) -> int:
    try:
        return _emit(fn(*args).to_dict())
    except Exception as exc:  # pragma: no cover - traceback branch asserted by payload shape
        traceback.print_exc(file=sys.stderr)
        return _emit(_error_payload(source, exc))


def _state_root() -> Path:
    raw = os.environ.get("MEMENTO_PI_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "memento" / "pi"


def _queue_file(vault: Path | None = None) -> Path:
    _migrate_legacy_queue(vault)
    return _state_root() / "queue" / "pi-captures.jsonl"


def _legacy_queue_file(vault: Path | None = None) -> Path:
    return (vault or get_vault()) / "queue" / "pi-captures.jsonl"


def _read_queue_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    captures = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            captures.append(json.loads(line))
        except json.JSONDecodeError:
            captures.append({"id": f"invalid-{len(captures) + 1}", "error": "invalid-json", "raw": line})
    return captures


def _migrate_legacy_queue(vault: Path | None = None) -> dict[str, Any]:
    legacy = _legacy_queue_file(vault)
    if not legacy.exists():
        return {"migrated": False, "reason": "no_legacy_queue"}
    old = _read_queue_file(legacy)
    if not old:
        legacy.unlink()
        return {"migrated": True, "migrated_count": 0, "deleted_legacy_queue": True}
    new_path = _state_root() / "queue" / "pi-captures.jsonl"
    current = _read_queue_file(new_path)
    seen = {capture.get("id") for capture in current if capture.get("id")}
    migrated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    additions = []
    for capture in old:
        capture_id = capture.get("id")
        if capture_id and capture_id in seen:
            continue
        item = dict(capture)
        metadata = dict(item.get("metadata") or {})
        metadata.setdefault("migrated_from", str(legacy))
        metadata.setdefault("migrated_at", migrated_at)
        item["metadata"] = metadata
        additions.append(item)
        if capture_id:
            seen.add(capture_id)
    combined = current + additions
    _write_queue_file(combined, new_path)
    reread_ids = {capture.get("id") for capture in _read_queue_file(new_path)}
    old_ids = {capture.get("id") for capture in old if capture.get("id")}
    if not old_ids.issubset(reread_ids):
        return {
            "migrated": False,
            "reason": "verification_failed",
            "legacy_queue_path": str(legacy),
            "queue_path": str(new_path),
        }
    legacy.unlink()
    try:
        legacy.parent.rmdir()
    except OSError:
        pass
    return {
        "migrated": True,
        "migrated_count": len(additions),
        "deleted_legacy_queue": True,
        "legacy_queue_path": str(legacy),
        "queue_path": str(new_path),
    }


def _load_queue(vault: Path | None = None) -> list[dict[str, Any]]:
    return _read_queue_file(_queue_file(vault))


def _write_queue_file(captures: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(capture, ensure_ascii=False) + "\n" for capture in captures))


def _write_queue(captures: list[dict[str, Any]], vault: Path | None = None) -> None:
    _write_queue_file(captures, _queue_file(vault))


def _queue_count(vault: Path | None = None) -> int:
    return len(_load_queue(vault))


def _git_branch(cwd: str) -> str | None:
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    branch = result.stdout.strip()
    return branch or None


def _status(cwd: str = "") -> dict[str, Any]:
    vault = get_vault()
    project_slug, _ticket = detect_project(cwd, None) if cwd else ("unknown", None)
    notes_dir = vault / "notes"
    projects_dir = vault / "projects"
    remote_available = False
    remote_error = None
    if is_remote():
        try:
            remote = remote_status()
            remote_available = bool(remote and "error" not in remote)
            remote_error = remote.get("error") if isinstance(remote, dict) else None
        except Exception as exc:
            remote_error = str(exc)
    return {
        "vault_path": str(vault),
        "vault_exists": vault.exists(),
        "project_slug": project_slug,
        "qmd_available": has_qmd(),
        "remote_configured": is_remote(),
        "remote_available": remote_available,
        "remote_error": remote_error,
        "note_count": len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0,
        "project_count": len(list(projects_dir.glob("*.md"))) if projects_dir.exists() else 0,
        "queued_capture_count": _queue_count(vault),
        "queue_path": str(_queue_file(vault)),
        "legacy_queue_path": str(_legacy_queue_file(vault)),
        "legacy_queue_exists": _legacy_queue_file(vault).exists(),
        "lifecycle": {
            "briefing": get_config().get("session_briefing", True),
            "prompt_recall": get_config().get("prompt_recall", True),
            "tool_context": get_config().get("tool_context", True),
            "auto_capture": False,
            "capture_queue": True,
        },
    }


def _search_miss(reason: str, details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = miss_envelope(reason, details=details)
    payload["reason"] = reason
    return payload


def _search(query: str, limit: int, cwd: str = "", concrete: object = "auto") -> dict[str, Any]:
    if not query.strip():
        return _search_miss("query_too_broad", {"query": query})
    if not has_qmd():
        if is_remote():
            envelope = remote_search_envelope(query=query, limit=limit, cwd=cwd, concrete=concrete)
            if envelope.get("error"):
                return _search_miss("backend_unavailable", {"error": envelope["error"]})
            results = envelope.get("results", [])
            if results:
                return {"results": results, "source": "remote"}
            if isinstance(envelope.get("miss"), dict):
                payload = {
                    "results": [],
                    "miss": envelope["miss"],
                    "reason": envelope["miss"].get("reason", "no_exact_match"),
                }
                return payload
            return _search_miss("no_exact_match")
        return _search_miss("backend_unavailable")
    limit = max(1, min(int(limit), 20))
    concrete_enabled, _auto_selected = resolve_concrete_mode(concrete, query)
    concrete_auto_mode = concrete is None or (isinstance(concrete, str) and concrete.strip().lower() in ("", "auto"))
    conceptual_miss_reason = normalize_miss_reason("no-results", query) if concrete_auto_mode else "no_exact_match"
    raw_results = qmd_search_with_extras(
        query,
        limit=limit,
        semantic=False,
        timeout=10,
        min_score=0.0,
        concrete=concrete_enabled,
    )
    if raw_results and concrete_enabled:
        results = filter_by_project(raw_results, cwd) if cwd else raw_results
    else:
        results = enhance_results(raw_results, cwd=cwd or None) if raw_results else []
    if not results:
        if raw_results:
            return _search_miss("project_filter_removed_all", {"cwd": cwd} if cwd else None)
        return _search_miss("no_concrete_match" if concrete_enabled else conceptual_miss_reason)
    sanitized = []
    for result in results[:limit]:
        sanitized.append(
            {
                "path": result.get("path", ""),
                "title": strip_injection(result.get("title", "")),
                "score": round(result.get("score", 0.0), 4),
                "snippet": strip_injection(result.get("snippet", "")),
            }
        )
    return {"results": sanitized}


def _get(path: str) -> dict[str, Any]:
    if not path.strip():
        return {"error": "path is required"}
    vault = get_vault()
    note_path = path.strip()
    if not note_path.endswith(".md"):
        note_path = f"notes/{note_path}.md"
    elif not note_path.startswith("notes/") and "/" not in note_path:
        note_path = f"notes/{note_path}"

    full_path = (vault / note_path).resolve()
    vault_resolved = vault.resolve()
    if full_path != vault_resolved and vault_resolved not in full_path.parents:
        return {"error": "Invalid path: traversal outside vault"}
    if full_path.exists():
        content = full_path.read_text(errors="replace")
        title = Path(note_path).stem
        title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip().strip('"').strip("'")
        return {"path": note_path, "title": strip_injection(title), "content": strip_injection(content)}

    result = qmd_get(note_path)
    if result:
        return {
            "path": result.get("path", note_path),
            "title": strip_injection(result.get("title", "")),
            "content": strip_injection(result.get("content", "")),
        }
    if is_remote():
        remote_result = remote_get(note_path)
        if remote_result:
            return {
                "path": remote_result.get("path", note_path),
                "title": strip_injection(remote_result.get("title", "")),
                "content": strip_injection(remote_result.get("content", "")),
                "source": "remote",
            }
    return {"error": f"Note not found: {note_path}"}


def _capture(
    title: str,
    body: str,
    cwd: str,
    session_id: str,
    queue: bool = False,
    reason: str = "manual",
    source_event: str = "manual",
) -> dict[str, Any]:
    if not title.strip():
        return {"error": "title is required"}
    if not body.strip():
        return {"error": "body is required"}
    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}
    project_slug, _ticket = detect_project(cwd, None) if cwd else ("unknown", None)
    branch = _git_branch(cwd)
    if queue:
        capture_id = f"pi-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        capture = {
            "id": capture_id,
            "title": title.strip(),
            "body": body.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "source_event": source_event,
            "metadata": {
                "cwd": cwd,
                "project": project_slug,
                "branch": branch,
                "session_id": session_id,
            },
        }
        captures = _load_queue(vault)
        captures.append(capture)
        _write_queue(captures, vault)
        return {"id": capture_id, "title": title.strip(), "queued": True, "queue_path": str(_queue_file(vault))}

    note_path = write_note(
        vault,
        title.strip(),
        body.strip(),
        "session",
        ["pi", project_slug] if project_slug != "unknown" else ["pi"],
        source="pi",
        project=project_slug if project_slug != "unknown" else None,
        branch=branch,
        session_id=session_id if session_id != "unknown" else None,
    )
    return {"path": str(note_path.relative_to(vault)), "title": title.strip(), "queued": False}


def _queue_list(limit: int = 20, include_body: bool = False) -> dict[str, Any]:
    captures = _load_queue()
    visible = []
    for capture in captures[-max(1, int(limit)) :]:
        item = dict(capture)
        if not include_body:
            item.pop("body", None)
        visible.append(item)
    return {"count": len(captures), "captures": visible, "queue_path": str(_queue_file())}


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return cleaned[:120] or uuid.uuid4().hex


def _processing_root() -> Path:
    return _state_root() / "processing"


def _lock_file() -> Path:
    return _state_root() / "processing.lock"


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_processing_lock(run_id: str, owner_pid: int = 0) -> dict[str, Any] | None:
    path = _lock_file()
    if path.exists():
        try:
            existing = json.loads(path.read_text(errors="replace"))
        except json.JSONDecodeError:
            existing = {"pid": 0, "run_id": "unknown"}
        pid = int(existing.get("pid") or 0)
        created_at = float(existing.get("created_time") or 0)
        stale = (pid and not _is_pid_alive(pid)) or (created_at and time.time() - created_at > 24 * 60 * 60)
        if not stale:
            return {
                "error": "another memento processing run is active",
                "reason": "processing_lock_active",
                "lock": existing,
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "pid": owner_pid or os.getpid(),
        "created_time": time.time(),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return None


def _release_processing_lock(run_id: str) -> None:
    path = _lock_file()
    if not path.exists():
        return
    try:
        existing = json.loads(path.read_text(errors="replace"))
    except json.JSONDecodeError:
        existing = {}
    if existing.get("run_id") == run_id:
        path.unlink()


def _capture_created_at(capture: dict[str, Any]) -> str:
    return str(capture.get("created_at") or capture.get("date") or "")


def _selected_captures(
    captures: list[dict[str, Any]],
    capture_id: str = "",
    project: str = "",
    branch: str = "",
    session_id: str = "",
    newest: bool = False,
) -> list[dict[str, Any]]:
    selected = []
    for capture in captures:
        metadata = capture.get("metadata") or {}
        if capture_id and capture.get("id") != capture_id:
            continue
        if project and metadata.get("project") != project:
            continue
        if branch and metadata.get("branch") != branch:
            continue
        if session_id and metadata.get("session_id") != session_id:
            continue
        if metadata.get("memento_processor") is True:
            continue
        selected.append(capture)
    selected.sort(key=_capture_created_at, reverse=newest)
    return selected


def _group_captures(captures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for capture in captures:
        metadata = capture.get("metadata") or {}
        session_id = metadata.get("session_id") if metadata.get("session_id") != "unknown" else None
        key = f"session:{session_id}" if session_id else f"capture:{capture.get('id')}"
        if key not in groups:
            groups[key] = {
                "group_id": _safe_segment(key),
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


def _render_capture_packet(group: dict[str, Any], transcript_markdown: str = "") -> str:
    lines = [
        f"# Memento processing input: {group['group_id']}",
        "",
        "## Metadata",
        f"- Session ID: {group.get('session_id') or '(none)'}",
        f"- Project: {group.get('project') or '(unknown)'}",
        f"- Branch: {group.get('branch') or '(unknown)'}",
        f"- CWD: {group.get('cwd') or '(unknown)'}",
        f"- Capture IDs: {', '.join(str(x) for x in group.get('capture_ids', []))}",
        "",
        "## Queued captures",
    ]
    for capture in group.get("captures", []):
        metadata = capture.get("metadata") or {}
        lines.extend(
            [
                "",
                f"### Capture {capture.get('id')}",
                f"- Title: {capture.get('title') or ''}",
                f"- Created: {capture.get('created_at') or ''}",
                f"- Reason: {capture.get('reason') or ''}",
                f"- Source event: {capture.get('source_event') or ''}",
                f"- Project: {metadata.get('project') or ''}",
                f"- Branch: {metadata.get('branch') or ''}",
                "",
                str(capture.get("body") or ""),
            ]
        )
    if transcript_markdown:
        lines.extend(["", "## Cleaned session transcript", "", transcript_markdown])
    return "\n".join(lines).rstrip() + "\n"


def _clean_content_parts(parts: Any, per_tool_cap: int) -> list[str]:
    if isinstance(parts, str):
        return [parts]
    if not isinstance(parts, list):
        return []
    output: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            output.append(part["text"])
        elif part_type == "toolCall":
            name = part.get("name") or "tool"
            arguments = part.get("arguments") or {}
            output.append(f"[tool call] {name} {json.dumps(arguments, ensure_ascii=False)[:1000]}")
        elif part_type in {"toolResult", "tool_result"}:
            text = part.get("text") or part.get("content") or ""
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False)
            suffix = "\n[tool result truncated]" if len(text) > per_tool_cap else ""
            output.append(f"[tool result]\n{text[:per_tool_cap]}{suffix}")
        elif part_type in {"image", "input_image"}:
            output.append("[image omitted]")
        elif part_type == "thinking":
            continue
    return output


def _clean_transcript(path: Path, per_tool_cap: int = 3000, total_cap: int = 200000) -> str:
    lines: list[str] = []
    total = 0
    for raw in path.read_text(errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        entry_type = entry.get("type")
        timestamp = entry.get("timestamp") or ""
        rendered: list[str] = []
        if entry_type == "message":
            message = entry.get("message") or {}
            role = message.get("role") or "message"
            content = _clean_content_parts(message.get("content"), per_tool_cap)
            if content:
                rendered = [f"## {role} {timestamp}".rstrip(), "", "\n\n".join(content)]
        elif entry_type == "custom_message":
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                rendered = [f"## custom:{entry.get('customType') or 'message'} {timestamp}".rstrip(), "", content]
        elif entry_type in {"session", "model_change", "thinking_level_change"}:
            continue
        if not rendered:
            continue
        block = "\n".join(rendered).strip()
        if not block:
            continue
        lines.append(block)
        total += len(block)
        if total > total_cap:
            lines.append("\n[transcript truncated by memento processor]")
            break
    return "\n\n".join(lines)


def _queue_process_start(
    capture_id: str = "",
    project: str = "",
    branch: str = "",
    session_id: str = "",
    limit: int = 0,
    newest: bool = False,
    dry_run: bool = False,
    transcript_max_bytes: int = 2 * 1024 * 1024,
    owner_pid: int = 0,
) -> dict[str, Any]:
    vault = get_vault()
    captures = _load_queue(vault)
    selected = _selected_captures(captures, capture_id, project, branch, session_id, newest)
    groups = _group_captures(selected)
    groups.sort(
        key=lambda group: min((_capture_created_at(capture) for capture in group.get("captures", [])), default=""),
        reverse=newest,
    )
    if limit and limit > 0:
        groups = groups[:limit]
        selected_ids = {capture_id for group in groups for capture_id in group.get("capture_ids", [])}
        selected = [capture for capture in selected if capture.get("id") in selected_ids]
    summary_groups = [
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
    if dry_run:
        return {
            "dry_run": True,
            "selected_capture_count": len(selected),
            "group_count": len(groups),
            "groups": summary_groups,
            "queue_path": str(_queue_file(vault)),
        }
    if not groups:
        return {"run_id": None, "run_dir": None, "selected_capture_count": 0, "group_count": 0, "groups": []}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    lock_error = _acquire_processing_lock(run_id, owner_pid)
    if lock_error:
        return lock_error
    run_dir = _processing_root() / run_id
    inputs_dir = run_dir / "inputs"
    results_dir = run_dir / "results"
    logs_dir = run_dir / "logs"
    for directory in (inputs_dir, results_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    manifest_groups = []
    for group in groups:
        transcript_info = {"included": False}
        transcript_markdown = ""
        if group.get("session_id"):
            transcript_path = Path(str(group["session_id"])).expanduser()
            if transcript_path.exists():
                size = transcript_path.stat().st_size
                transcript_info = {
                    "path": str(transcript_path),
                    "size_bytes": size,
                    "included": size <= transcript_max_bytes,
                }
                if size <= transcript_max_bytes:
                    transcript_markdown = _clean_transcript(transcript_path)
            else:
                transcript_info = {"path": str(transcript_path), "included": False, "reason": "missing"}
        group_id = group["group_id"]
        (inputs_dir / f"{group_id}.json").write_text(json.dumps(group, ensure_ascii=False, indent=2))
        (inputs_dir / f"{group_id}.md").write_text(_render_capture_packet(group, transcript_markdown))
        manifest_groups.append(
            {
                "group_id": group_id,
                "capture_ids": group.get("capture_ids", []),
                "session_id": group.get("session_id"),
                "project": group.get("project"),
                "branch": group.get("branch"),
                "cwd": group.get("cwd"),
                "input_json": str(inputs_dir / f"{group_id}.json"),
                "input_markdown": str(inputs_dir / f"{group_id}.md"),
                "result_json": str(results_dir / f"{group_id}.json"),
                "log_markdown": str(logs_dir / f"{group_id}.md"),
                "transcript": transcript_info,
            }
        )
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "queue_path": str(_queue_file(vault)),
        "vault_path": str(vault),
        "state_root": str(_state_root()),
        "selected_capture_count": len(selected),
        "group_count": len(groups),
        "groups": manifest_groups,
        "status": "running",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "selected_capture_count": len(selected),
        "group_count": len(groups),
        "groups": summary_groups,
    }


def _queue_process_finalize(run_id: str) -> dict[str, Any]:
    vault = get_vault()
    run_dir = _processing_root() / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return {"error": f"processing run not found: {run_id}", "reason": "run_not_found"}
    manifest = json.loads(manifest_path.read_text(errors="replace"))
    captures = _load_queue(vault)
    dequeue_ids: set[str] = set()
    group_results = []
    for group in manifest.get("groups", []):
        expected_ids = set(str(x) for x in group.get("capture_ids", []))
        result_path = Path(group.get("result_json", ""))
        if not result_path.exists():
            group_results.append({"group_id": group.get("group_id"), "status": "failed", "reason": "missing_result"})
            continue
        try:
            result = json.loads(result_path.read_text(errors="replace"))
        except json.JSONDecodeError:
            group_results.append(
                {"group_id": group.get("group_id"), "status": "failed", "reason": "invalid_result_json"}
            )
            continue
        processed_ids = set(str(x) for x in result.get("processed_capture_ids", []))
        status = result.get("status")
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
                if not path or not (vault / str(path)).exists():
                    valid = False
                    reason = "missing_created_note"
                    break
        if valid:
            dequeue_ids.update(expected_ids)
            group_results.append(
                {
                    "group_id": group.get("group_id"),
                    "status": status,
                    "reason": reason,
                    "dequeued_capture_ids": sorted(expected_ids),
                }
            )
        else:
            group_results.append({"group_id": group.get("group_id"), "status": "failed", "reason": reason})
    remaining = [capture for capture in captures if str(capture.get("id")) not in dequeue_ids]
    _write_queue(remaining, vault)
    manifest["status"] = "finalized"
    manifest["finalized_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest["dequeued_capture_ids"] = sorted(dequeue_ids)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    _release_processing_lock(run_id)
    return {"run_id": run_id, "dequeued": len(dequeue_ids), "remaining": len(remaining), "groups": group_results}


def _run_json(source: str, fn, *args: Any) -> int:
    try:
        return _emit(fn(*args))
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        traceback.print_exc(file=sys.stderr)
        return _emit({"error": str(exc), "source": source, "reason": "error", "error_type": type(exc).__name__})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Memento pi lifecycle JSON adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    briefing = sub.add_parser("briefing", help="Build first-turn/session briefing context")
    briefing.add_argument("--cwd", default="")
    briefing.add_argument("--session-id", default="unknown")

    recall = sub.add_parser("recall", help="Build prompt recall context")
    recall.add_argument("--prompt", default="")
    recall.add_argument("--cwd", default="")
    recall.add_argument("--session-id", default="unknown")

    tool_context = sub.add_parser("tool-context", help="Build read-tool context")
    tool_context.add_argument("--tool-name", default="")
    tool_context.add_argument("--file-path", default="")
    tool_context.add_argument("--cwd", default="")
    tool_context.add_argument("--session-id", default="unknown")

    status = sub.add_parser("status", help="Show memento status")
    status.add_argument("--cwd", default="")

    search = sub.add_parser("search", help="Search memento notes")
    search.add_argument("--query", default="")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--cwd", default="")
    search.add_argument("--concrete", default="auto", choices=("auto", "true", "false"))

    get = sub.add_parser("get", help="Read a memento note")
    get.add_argument("--path", default="")

    capture = sub.add_parser("capture", help="Manually capture or queue a memento note")
    capture.add_argument("--title", default="")
    capture.add_argument("--body", default="")
    capture.add_argument("--cwd", default="")
    capture.add_argument("--session-id", default="unknown")
    capture.add_argument("--queue", action="store_true", help="Queue for later review instead of writing a note")
    capture.add_argument("--reason", default="manual")
    capture.add_argument("--source-event", default="manual")

    queue = sub.add_parser("queue", help="Inspect or process queued pi captures")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_list = queue_sub.add_parser("list", help="List queued captures")
    queue_list.add_argument("--limit", type=int, default=20)
    queue_list.add_argument("--include-body", action="store_true")
    process_start = queue_sub.add_parser("process-start", help="Create a processing run for selected captures")
    process_start.add_argument("--id", default="")
    process_start.add_argument("--project", default="")
    process_start.add_argument("--branch", default="")
    process_start.add_argument("--session", default="")
    process_start.add_argument("--limit", type=int, default=0)
    order = process_start.add_mutually_exclusive_group()
    order.add_argument("--oldest", action="store_true")
    order.add_argument("--newest", action="store_true")
    process_start.add_argument("--dry-run", action="store_true")
    process_start.add_argument("--transcript-max-bytes", type=int, default=2 * 1024 * 1024)
    process_start.add_argument("--owner-pid", type=int, default=0, help=argparse.SUPPRESS)
    process_finalize = queue_sub.add_parser(
        "process-finalize", help="Finalize a processing run and dequeue validated captures"
    )
    process_finalize.add_argument("--run-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "briefing":
        return _run_lifecycle("briefing", build_briefing, args.cwd, args.session_id)
    if args.command == "recall":
        return _run_lifecycle("recall", build_recall, args.prompt, args.cwd, args.session_id)
    if args.command == "tool-context":
        return _run_lifecycle(
            "tool-context",
            build_tool_context,
            args.tool_name,
            args.file_path,
            args.cwd,
            args.session_id,
        )
    if args.command == "status":
        return _run_json("status", _status, args.cwd)
    if args.command == "search":
        return _run_json("search", _search, args.query, args.limit, args.cwd, args.concrete)
    if args.command == "get":
        return _run_json("get", _get, args.path)
    if args.command == "capture":
        return _run_json(
            "capture",
            _capture,
            args.title,
            args.body,
            args.cwd,
            args.session_id,
            args.queue,
            args.reason,
            args.source_event,
        )
    if args.command == "queue":
        if args.queue_command == "list":
            return _run_json("queue", _queue_list, args.limit, args.include_body)
        if args.queue_command == "process-start":
            return _run_json(
                "queue",
                _queue_process_start,
                args.id,
                args.project,
                args.branch,
                args.session,
                args.limit,
                args.newest,
                args.dry_run,
                args.transcript_max_bytes,
                args.owner_pid,
            )
        if args.queue_command == "process-finalize":
            return _run_json("queue", _queue_process_finalize, args.run_id)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
