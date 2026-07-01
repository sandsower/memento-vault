"""JSON CLI adapter for pi's TypeScript extension.

The pi runtime loads TypeScript/JavaScript extensions, so the extension calls
this module as a short-lived Python process. Lifecycle policy remains in
memento.lifecycle; this module only translates CLI JSON to LifecycleResult JSON.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from memento import telemetry
from memento.capture_runtime import CaptureProcessRequest, CaptureRuntime
from memento.config import detect_project, get_config, get_vault
from memento.lifecycle import build_briefing, build_recall, build_session_context, build_tool_context, strip_injection
from memento.search_backend import get_backend
from memento.store import acquire_vault_write_lock, release_vault_write_lock, write_note
from memento.search import (
    enhance_results,
    filter_by_project,
    has_qmd,
    miss_envelope,
    normalize_miss_reason,
    qmd_get,
    qmd_search_with_extras,
    resolve_concrete_mode,
    shape_search_results,
)
from memento.contradictions import inspect_contradictions
from memento.query import query_notes
from memento import store as store_module
from memento.remote_client import get as remote_get
from memento.remote_client import is_remote, search_envelope as remote_search_envelope, status as remote_status
from memento.store import log_triage_health
from memento.utils import sanitize_secrets


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _pi_capture_queue_enabled() -> bool:
    env_value = _env_bool("MEMENTO_PI_CAPTURE_QUEUE")
    return env_value if env_value is not None else False


def _bridge_config_summary() -> dict[str, Any]:
    config = get_config()
    return {
        "enabled": bool(
            config.get("session_briefing", True)
            or config.get("prompt_recall", True)
            or config.get("tool_context", True)
        ),
        "session_briefing": bool(config.get("session_briefing", True)),
        "prompt_recall": bool(config.get("prompt_recall", True)),
        "tool_context": bool(config.get("tool_context", True)),
        "project_maps_enabled": bool(config.get("project_maps_enabled", True)),
        "search_backend": config.get("search_backend", "auto"),
    }


def _bridge_project_slug(cwd: str) -> str:
    if not cwd:
        return "unknown"
    try:
        project_slug, _ticket = detect_project(cwd, None)
        return project_slug or "unknown"
    except Exception:
        return "unknown"


def _log_bridge_health(
    operation: str, *, cwd: str = "", session_id: str = "unknown", error: object, **details: Any
) -> None:
    payload: dict[str, Any] = {
        "operation": operation,
        "backend": details.pop("backend", "python3"),
        "config": details.pop("config", _bridge_config_summary()),
        "cwd": cwd,
        "project": details.pop("project", _bridge_project_slug(cwd)),
        "session_id": session_id,
        "error": str(error),
        "error_type": type(error).__name__ if isinstance(error, BaseException) else "Error",
    }
    payload.update(details)
    try:
        log_triage_health(f"{operation}_failed", hook="pi-bridge", **payload)
    except Exception:
        pass


def _emit_error(operation: str, error: Exception) -> int:
    traceback.print_exc(file=sys.stderr)
    return _emit(_error_payload(operation, error))


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


def _run_lifecycle(
    source: str,
    fn,
    *args: Any,
    health_metadata: dict[str, Any] | None = None,
) -> int:
    try:
        return _emit(fn(*args).to_dict())
    except Exception as exc:  # pragma: no cover - traceback branch asserted by payload shape
        traceback.print_exc(file=sys.stderr)
        metadata = dict(health_metadata or {})
        _log_bridge_health(source, error=exc, **metadata)
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


_QUEUE_LOCK_STATE = threading.local()


def _queue_lock_file(vault: Path | None = None) -> Path:
    return _state_root() / "queue" / "pi-captures.lock"


@contextlib.contextmanager
def _queue_lock(vault: Path | None = None) -> Generator[None, None, None]:
    """Acquire an exclusive flock on the queue lock file.

    The lock is blocking and re-entrant within a thread so nested queue
    migrations can safely reuse the same critical section.
    """
    path = _queue_lock_file(vault)
    depth = getattr(_QUEUE_LOCK_STATE, "depth", 0)
    if depth:
        _QUEUE_LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _QUEUE_LOCK_STATE.depth = depth
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        yield
        return

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _QUEUE_LOCK_STATE.depth = 1
        try:
            yield
        finally:
            _QUEUE_LOCK_STATE.depth = 0
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        fd = os.open(str(tmp_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(path))
        dir_flag = getattr(os, "O_DIRECTORY", None)
        if dir_flag is not None:
            try:
                dir_fd = os.open(str(path.parent), dir_flag)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_queue_file(captures: list[dict[str, Any]], path: Path) -> None:
    """Atomically write the queue file using tmp+fsync+rename."""
    _atomic_write_text(path, "".join(json.dumps(capture, ensure_ascii=False) + "\n" for capture in captures))


def _write_queue(captures: list[dict[str, Any]], vault: Path | None = None) -> None:
    _write_queue_file(captures, _queue_file(vault))


def _migrate_legacy_queue(vault: Path | None = None) -> dict[str, Any]:
    legacy = _legacy_queue_file(vault)
    if not legacy.exists():
        return {"migrated": False, "reason": "no_legacy_queue"}
    old = _read_queue_file(legacy)
    if not old:
        legacy.unlink()
        return {"migrated": True, "migrated_count": 0, "deleted_legacy_queue": True}
    new_path = _state_root() / "queue" / "pi-captures.jsonl"
    with _queue_lock(vault):
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


def _queue_count(vault: Path | None = None) -> int:
    return len(_load_queue(vault))


_LIFECYCLE_SOURCE_EVENTS = {"agent_end", "session_shutdown", "session_before_compact", "session_compact"}
_MEANINGFUL_KEYWORDS = re.compile(
    r"\b(bug|debug|fix|fixed|error|issue|root cause|decision|decided|design|approach|tradeoff|defer|in scope|out of scope|should live|ship it)\b",
    re.IGNORECASE,
)
_FILE_EDIT_SIGNAL = re.compile(r"\b(files? edited|edit(?:ed)?|write|patch|modified|changed)\b", re.IGNORECASE)
_EXCHANGE_LINE = re.compile(r"^\s*-\s*(user|assistant):", re.IGNORECASE | re.MULTILINE)
_MANUAL_SUPPRESSION_EXCHANGE_THRESHOLD = 5
_SUBSTANTIAL_TAIL_CHARS = 1200


def _session_state_key(session_id: str, cwd: str) -> str:
    raw = f"{session_id or 'unknown'}\0{str(Path(cwd).expanduser()) if cwd else 'unknown'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _capture_session_state_file(session_id: str, cwd: str) -> Path:
    return _state_root() / "capture-sessions" / f"{_session_state_key(session_id, cwd)}.json"


def _load_capture_session_state(session_id: str, cwd: str) -> dict[str, Any]:
    path = _capture_session_state_file(session_id, cwd)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_capture_session_state(session_id: str, cwd: str, state: dict[str, Any]) -> None:
    path = _capture_session_state_file(session_id, cwd)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        # Session state is an optimization for lifecycle queue suppression; capture commands must still succeed.
        print(f"[memento] warning: could not write pi capture session state: {exc}", file=sys.stderr)


def _triage_payload_dir() -> Path:
    return _state_root() / "triage" / "payloads"


def _triage_hook_script() -> Path:
    return Path(__file__).resolve().parents[1] / "hooks" / "memento-triage.py"


def _write_triage_payload(payload: dict[str, Any]) -> Path:
    payload_dir = _triage_payload_dir()
    payload_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload_path = payload_dir / f"pi-triage-{stamp}-{uuid.uuid4().hex[:8]}.json"
    _atomic_write_text(payload_path, json.dumps(payload, ensure_ascii=False) + "\n")
    return payload_path


_TRIAGE_ACTIVE_TTL_SECONDS = 10 * 60


def _safe_hook_session_id(session_id: str, transcript_path: str) -> str:
    """Return a session id safe to pass to the shared hook, or empty to let the adapter decide."""
    candidate = str(session_id or "").strip()
    if not candidate or candidate == "unknown" or candidate == transcript_path:
        return ""
    if Path(candidate).is_absolute() or "/" in candidate or "\\" in candidate or candidate in {".", ".."}:
        return ""
    return candidate


def _parse_utc_timestamp(value: object) -> datetime | None:
    return telemetry.parse_timestamp_utc(value)


def _triage_active_until(started_at: str) -> str:
    started = _parse_utc_timestamp(started_at) or datetime.now(timezone.utc)
    return (started + timedelta(seconds=_TRIAGE_ACTIVE_TTL_SECONDS)).replace(microsecond=0).isoformat()


def _triage_transcript_fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _same_triage_transcript_version(state: dict[str, Any], path: Path, fingerprint: dict[str, int]) -> bool:
    return (
        state.get("pi_triage_transcript_path") == str(path)
        and state.get("pi_triage_transcript_size_bytes") == fingerprint["size_bytes"]
        and state.get("pi_triage_transcript_mtime_ns") == fingerprint["mtime_ns"]
    )


def _triage_state_status(state: dict[str, Any]) -> str:
    status = str(state.get("pi_triage_status") or "").strip().lower()
    if status:
        return status
    return "active" if state.get("pi_triage_started_at") and not state.get("pi_triage_completed_at") else ""


def _triage_state_active(state: dict[str, Any], now: datetime | None = None) -> bool:
    if _triage_state_status(state) != "active":
        return False
    now = now or datetime.now(timezone.utc)
    active_until = _parse_utc_timestamp(state.get("pi_triage_active_until"))
    if active_until is None and state.get("pi_triage_started_at"):
        active_until = _parse_utc_timestamp(_triage_active_until(str(state["pi_triage_started_at"])))
    return bool(active_until and now <= active_until)


def _capture_audit_file(vault: Path | None = None) -> Path:
    return _state_root() / "audit" / "pi-lifecycle-audit.jsonl"


def _append_capture_audit(record: dict[str, Any], vault: Path | None = None) -> None:
    path = _capture_audit_file(vault)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[memento] warning: could not write pi capture audit: {exc}", file=sys.stderr)


def _capture_lifecycle_metadata(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or value == "":
            continue
        metadata[str(key)] = value
    return metadata


def _parse_lifecycle_metadata(raw: object) -> tuple[dict[str, Any], str | None]:
    if raw in (None, ""):
        return {}, None
    if isinstance(raw, dict):
        return _capture_lifecycle_metadata(raw), None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"invalid lifecycle metadata: {exc.msg}"
        if isinstance(parsed, dict):
            return _capture_lifecycle_metadata(parsed), None
        return {}, "lifecycle metadata must be a JSON object"
    return {}, f"unsupported lifecycle metadata type: {type(raw).__name__}"


def _body_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title.strip()}\n{body.strip()}".encode("utf-8")).hexdigest()[:16]


def _is_lifecycle_source(source_event: str) -> bool:
    return source_event in _LIFECYCLE_SOURCE_EVENTS


def _meaningful_lifecycle_signal(body: str, source_event: str) -> tuple[bool, str]:
    if not body.strip():
        return False, "empty_body"
    if _FILE_EDIT_SIGNAL.search(body):
        return True, "file_edit_signal"
    if _MEANINGFUL_KEYWORDS.search(body):
        return True, "keyword_signal"
    exchange_count = len(_EXCHANGE_LINE.findall(body))
    if exchange_count >= _MANUAL_SUPPRESSION_EXCHANGE_THRESHOLD:
        return True, "exchange_threshold"
    if source_event in {"session_shutdown", "session_before_compact"} and len(body.strip()) >= _SUBSTANTIAL_TAIL_CHARS:
        return True, "substantial_tail"
    return False, "manual_capture_suppressed_lifecycle"


def _mark_manual_capture_state(
    session_id: str,
    cwd: str,
    project_slug: str,
    branch: str | None,
    title: str,
    body: str,
    now: datetime,
    lifecycle_metadata: dict[str, Any] | None = None,
) -> None:
    state = _load_capture_session_state(session_id, cwd)
    metadata = _capture_lifecycle_metadata(lifecycle_metadata)
    state.update(
        {
            "session_id": session_id,
            "cwd": cwd,
            "project": project_slug,
            "branch": branch,
            "manual_capture_at": now.isoformat(timespec="seconds"),
            "manual_capture_body_hash": _body_hash(title, body),
            "manual_capture_body_excerpt": _body_excerpt(body),
            "manual_capture_body_char_count": len(body),
            "manual_capture_lifecycle_metadata": metadata,
            "last_lifecycle_decision": "manual_capture_recorded",
        }
    )
    _write_capture_session_state(session_id, cwd, state)
    _append_capture_audit(
        {
            "ts": now.isoformat(timespec="seconds"),
            "decision": "manual_capture_recorded",
            "queued": False,
            "skipped": False,
            "session_id": session_id,
            "cwd": cwd,
            "project": project_slug,
            "branch": branch,
            "title": title.strip(),
            "body_hash": _body_hash(title, body),
            "body_excerpt": _body_excerpt(body),
            "body_char_count": len(body),
            "lifecycle": metadata,
        },
        None,
    )


def _lifecycle_queue_decision(
    session_id: str, cwd: str, body: str, source_event: str, lifecycle_metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    state = _load_capture_session_state(session_id, cwd)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metadata = _capture_lifecycle_metadata(lifecycle_metadata)
    if not body.strip():
        decision = {"queue": False, "reason": "empty_body"}
    elif not state.get("manual_capture_at"):
        decision = {"queue": True, "reason": "no_manual_capture_baseline"}
    else:
        meaningful, reason = _meaningful_lifecycle_signal(body, source_event)
        decision = {"queue": meaningful, "reason": reason}

    state.update(
        {
            "session_id": session_id,
            "cwd": cwd,
            "last_lifecycle_decision_at": now,
            "last_lifecycle_source_event": source_event,
            "last_lifecycle_decision": "queued" if decision["queue"] else "skipped",
            "last_lifecycle_reason": decision["reason"],
            "last_lifecycle_body_hash": _body_hash(source_event or "lifecycle", body),
            "last_lifecycle_body_excerpt": _body_excerpt(body),
            "last_lifecycle_lifecycle_metadata": metadata,
        }
    )
    if decision["queue"]:
        state["lifecycle_queue_count"] = int(state.get("lifecycle_queue_count") or 0) + 1
    _write_capture_session_state(session_id, cwd, state)
    _append_capture_audit(
        {
            "ts": now,
            "decision": "queued" if decision["queue"] else "skipped",
            "queued": bool(decision["queue"]),
            "skipped": not bool(decision["queue"]),
            "session_id": session_id,
            "cwd": cwd,
            "source_event": source_event,
            "reason": decision["reason"],
            "body_hash": _body_hash(source_event or "lifecycle", body),
            "body_excerpt": _body_excerpt(body),
            "body_char_count": len(body),
            "manual_capture_present": bool(state.get("manual_capture_at")),
            "manual_capture_body_hash": state.get("manual_capture_body_hash"),
            "manual_capture_at": state.get("manual_capture_at"),
            "lifecycle": metadata,
        },
        None,
    )
    return decision


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


@contextlib.contextmanager
def _vault_write_lock(timeout: float = 10.0):
    acquired = acquire_vault_write_lock(timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            release_vault_write_lock()


def _vault_commit_script() -> Path | None:
    repo_script = Path(__file__).resolve().parents[1] / "hooks" / "vault-commit.sh"
    if repo_script.exists():
        return repo_script
    home_script = Path.home() / ".claude" / "hooks" / "vault-commit.sh"
    if home_script.exists():
        return home_script
    return None


def _commit_and_reindex_locked(vault: Path, commit_message: str, collection: str | None = None) -> dict[str, Any]:
    """Commit vault changes and reindex search after a confirmed Pi write.

    The caller must hold the vault write lock before calling this helper.
    """
    config = get_config()
    scheduled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload: dict[str, Any] = {
        "scheduled_at": scheduled_at,
        "vault": str(vault),
        "commit_message": commit_message,
        "commit": {
            "attempted": False,
            "ok": False,
            "reason": "auto_commit_disabled" if not config.get("auto_commit", True) else "not_run",
        },
        "reindex": {
            "attempted": False,
            "ok": False,
            "reason": "not_run",
        },
    }

    try:
        configured_vault = Path(config.get("vault_path", str(vault))).expanduser().resolve()
    except Exception:
        configured_vault = vault
    try:
        vault_resolved = vault.resolve()
    except Exception:
        vault_resolved = vault
    if configured_vault != vault_resolved:
        payload["commit"]["reason"] = "vault_mismatch"
        payload["reindex"]["reason"] = "vault_mismatch"
        payload["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return payload

    notes_dir = vault / "notes"
    if config.get("auto_commit", True) and (vault / ".git").exists() and notes_dir.exists():
        payload["commit"]["attempted"] = True
        try:
            subprocess.run(
                ["git", "-C", str(vault), "add", "-A", "notes"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            diff_check = subprocess.run(
                ["git", "-C", str(vault), "diff", "--cached", "--quiet", "--", "notes"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if diff_check.returncode == 0:
                payload["commit"]["ok"] = True
                payload["commit"]["reason"] = "no_changes"
            else:
                completed = subprocess.run(
                    ["git", "-C", str(vault), "commit", "-m", commit_message],
                    cwd=str(vault),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                payload["commit"]["ok"] = completed.returncode == 0
                payload["commit"]["returncode"] = completed.returncode
                if completed.returncode != 0:
                    payload["commit"]["reason"] = "commit_failed"
                    stderr = (completed.stderr or completed.stdout or "").strip()
                    if stderr:
                        payload["commit"]["stderr"] = stderr[:500]
                else:
                    payload["commit"]["reason"] = "ok"
        except (OSError, subprocess.TimeoutExpired) as exc:
            payload["commit"]["reason"] = type(exc).__name__
            payload["commit"]["error"] = str(exc)
    else:
        payload["commit"]["reason"] = "auto_commit_disabled" if not config.get("auto_commit", True) else "git_missing"

    payload["reindex"]["attempted"] = True
    target_collection = collection or config.get("qmd_collection", "memento")
    try:
        backend = get_backend()
        if hasattr(backend, "repair_index"):
            repair = backend.repair_index(target_collection)
            ok = bool(repair.get("ok")) if isinstance(repair, dict) else bool(repair)
            payload["reindex"]["repair"] = repair
        else:
            ok = backend.reindex(target_collection)
        payload["reindex"]["ok"] = bool(ok)
        payload["reindex"]["collection"] = target_collection
        payload["reindex"]["reason"] = "ok" if ok else "backend_reindex_failed"
    except Exception as exc:
        payload["reindex"]["reason"] = type(exc).__name__
        payload["reindex"]["error"] = str(exc)
    payload["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return payload


def _iter_jsonl(path: Path):
    yield from telemetry.iter_jsonl(path)


def _bridge_health_status() -> dict[str, Any]:
    log_path = Path(store_module.TRIAGE_HEALTH_LOG_PATH)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    failures: list[dict[str, Any]] = []
    latest: dict[str, Any] | None = None
    latest_ts: datetime | None = None
    if log_path.exists():
        for rec in _iter_jsonl(log_path):
            if rec.get("hook") != "pi-bridge":
                continue
            ts_raw = rec.get("ts")
            ts = _parse_utc_timestamp(ts_raw)
            if ts is None:
                continue
            if ts < cutoff:
                continue
            action = str(rec.get("action") or "")
            if not telemetry.is_pi_bridge_failure_action(action):
                continue
            failure = {
                "ts": rec.get("ts"),
                "operation": rec.get("operation") or rec.get("action"),
                "backend": rec.get("backend"),
                "cwd": rec.get("cwd"),
                "project": rec.get("project"),
                "session_id": rec.get("session_id"),
                "error": sanitize_secrets(str(rec.get("error") or "")),
                "error_type": rec.get("error_type"),
                "reason": rec.get("reason"),
            }
            failures.append(failure)
            if latest_ts is None or ts >= latest_ts:
                latest_ts = ts
                latest = rec
    if not failures:
        return {"status": "pass", "window_hours": 24, "recent_failure_count": 0, "log_path": str(log_path)}
    latest_error = sanitize_secrets(str((latest or {}).get("error") or ""))
    latest_error = strip_injection(" ".join(latest_error.split()))[:140]
    return {
        "status": "warn",
        "window_hours": 24,
        "recent_failure_count": len(failures),
        "log_path": str(log_path),
        "last_failure": {
            "operation": (latest or {}).get("operation") or (latest or {}).get("action"),
            "backend": (latest or {}).get("backend"),
            "cwd": (latest or {}).get("cwd"),
            "project": (latest or {}).get("project"),
            "session_id": (latest or {}).get("session_id"),
            "error": latest_error,
            "error_type": (latest or {}).get("error_type"),
        },
        "recent_failures": failures[:5],
    }


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
    audit_path = _capture_audit_file(vault)
    audit_count = 0
    last_audit: dict[str, Any] | None = None
    if audit_path.exists():
        for rec in _iter_jsonl(audit_path):
            audit_count += 1
            last_audit = rec
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
        "capture_audit_path": str(audit_path),
        "capture_audit_count": audit_count,
        "last_capture_audit": {
            "decision": (last_audit or {}).get("decision"),
            "queued": (last_audit or {}).get("queued"),
            "skipped": (last_audit or {}).get("skipped"),
            "reason": (last_audit or {}).get("reason"),
            "source_event": (last_audit or {}).get("source_event"),
            "manual_capture_present": (last_audit or {}).get("manual_capture_present"),
            "manual_capture_at": (last_audit or {}).get("manual_capture_at"),
        },
        "pi_bridge_health": _bridge_health_status(),
        "lifecycle": {
            "briefing": get_config().get("session_briefing", True),
            "prompt_recall": get_config().get("prompt_recall", True),
            "tool_context": get_config().get("tool_context", True),
            "auto_capture": True,
            "capture_queue": _pi_capture_queue_enabled(),
        },
    }


def _search_miss(
    reason: str,
    details: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = miss_envelope(reason, details=details, metadata=metadata)
    payload["reason"] = reason
    return payload


def _search(
    query: str,
    limit: int,
    cwd: str = "",
    concrete: object = "auto",
    detail_level: str = "summary",
    include_content: bool = False,
    token_budget: int | None = 2000,
) -> dict[str, Any]:
    if not query.strip():
        metadata = shape_search_results(
            [], vault=get_vault(), detail_level=detail_level, include_content=include_content, token_budget=token_budget
        )["metadata"]
        return _search_miss("query_too_broad", {"query": query}, metadata=metadata)
    if not has_qmd():
        if is_remote():
            envelope = remote_search_envelope(
                query=query,
                limit=limit,
                cwd=cwd,
                concrete=concrete,
                detail_level=detail_level,
                include_content=include_content,
                token_budget=token_budget,
            )
            if envelope.get("error"):
                metadata = shape_search_results(
                    [],
                    vault=get_vault(),
                    detail_level=detail_level,
                    include_content=include_content,
                    token_budget=token_budget,
                )["metadata"]
                return _search_miss("backend_unavailable", {"error": envelope["error"]}, metadata=metadata)
            results = envelope.get("results", [])
            if results:
                sanitized = []
                for result in results:
                    if isinstance(result, dict):
                        sanitized.append(
                            {
                                "path": strip_injection(str(result.get("path", ""))),
                                "title": strip_injection(str(result.get("title", ""))),
                                "score": round(result.get("score", 0.0), 4),
                                **(
                                    {"snippet": strip_injection(str(result.get("snippet", "")))}
                                    if result.get("snippet")
                                    else {}
                                ),
                                **(
                                    {"content": strip_injection(str(result.get("content", "")))}
                                    if result.get("content")
                                    else {}
                                ),
                            }
                        )
                    else:
                        sanitized.append(result)
                envelope["results"] = sanitized
                return envelope
            if isinstance(envelope.get("miss"), dict):
                miss = dict(envelope["miss"])
                if isinstance(miss.get("details"), dict):
                    miss["details"] = {
                        key: strip_injection(str(value)) if isinstance(value, str) else value
                        for key, value in miss["details"].items()
                    }
                payload = {
                    "results": [],
                    "miss": miss,
                    "reason": miss.get("reason", "no_exact_match"),
                }
                if isinstance(envelope.get("metadata"), dict):
                    payload["metadata"] = envelope["metadata"]
                return payload
            metadata = shape_search_results(
                [],
                vault=get_vault(),
                detail_level=detail_level,
                include_content=include_content,
                token_budget=token_budget,
            )["metadata"]
            return _search_miss("no_exact_match", metadata=metadata)
        metadata = shape_search_results(
            [], vault=get_vault(), detail_level=detail_level, include_content=include_content, token_budget=token_budget
        )["metadata"]
        return _search_miss("backend_unavailable", metadata=metadata)
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
        metadata = shape_search_results(
            [], vault=get_vault(), detail_level=detail_level, include_content=include_content, token_budget=token_budget
        )["metadata"]
        if raw_results:
            return _search_miss("project_filter_removed_all", {"cwd": cwd} if cwd else None, metadata=metadata)
        return _search_miss("no_concrete_match" if concrete_enabled else conceptual_miss_reason, metadata=metadata)
    shaped = shape_search_results(
        results[:limit],
        vault=get_vault(),
        detail_level=detail_level,
        include_content=include_content,
        token_budget=token_budget,
    )
    return shaped


def _query(
    project: str,
    note_type: str,
    tag: str,
    source: str,
    certainty_min: int | None,
    certainty_max: int | None,
    date_start: str,
    date_end: str,
    branch: str,
    session_id: str,
    aggregate_by: str,
    recent_sessions_project: str,
    limit: int,
) -> dict[str, Any]:
    return query_notes(
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


def _contradictions(topic: str, limit: int, min_certainty: int = 2) -> dict[str, Any]:
    payload = inspect_contradictions(topic, limit, min_certainty)
    if isinstance(payload, dict):
        for result in payload.get("results", []):
            result["title"] = strip_injection(result.get("title", ""))
            result["snippet"] = strip_injection(result.get("snippet", ""))
            result["status"] = strip_injection(result.get("status", ""))
            result["polarity"] = strip_injection(result.get("polarity", ""))
            result["path"] = strip_injection(result.get("path", ""))
        for group in payload.get("groups", []):
            group["theme"] = strip_injection(group.get("theme", ""))
            group["summary"] = strip_injection(group.get("summary", ""))
            group["note_paths"] = [strip_injection(path) for path in group.get("note_paths", [])]
        for item in payload.get("contradictions", []):
            item["kind"] = strip_injection(item.get("kind", ""))
            item["paths"] = [strip_injection(path) for path in item.get("paths", [])]
            item["titles"] = [strip_injection(title) for title in item.get("titles", [])]
        for item in payload.get("supersession", []):
            item["older_path"] = strip_injection(item.get("older_path", ""))
            item["newer_path"] = strip_injection(item.get("newer_path", ""))
            item["older_title"] = strip_injection(item.get("older_title", ""))
            item["newer_title"] = strip_injection(item.get("newer_title", ""))
        if isinstance(payload.get("summary"), str):
            payload["summary"] = strip_injection(payload["summary"])
    return payload


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


def _capture_note_tags(tags: list[str], project_slug: str, source_event: str = "manual") -> list[str]:
    """Merge caller-provided tags with Pi/project tags while preserving order."""
    merged = ["pi"]
    if source_event in {"manual", "tool"}:
        merged.append("manual")
    if project_slug != "unknown":
        merged.append(project_slug)
    for tag in tags:
        clean = str(tag).strip()
        if clean:
            merged.append(clean)
    deduped = []
    seen = set()
    for tag in merged:
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(tag)
    return deduped


def _capture_certainty(certainty: int | str | None) -> int | dict[str, str]:
    """Normalize Pi capture certainty, rejecting values outside the 1-5 contract."""
    if certainty in (None, ""):
        return 2
    try:
        value = int(certainty)
    except (TypeError, ValueError):
        return {"error": "certainty must be an integer from 1 to 5"}
    if 1 <= value <= 5:
        return value
    return {"error": "certainty must be an integer from 1 to 5"}


def _capture(
    title: str,
    body: str,
    cwd: str,
    session_id: str,
    queue: bool = False,
    reason: str = "manual",
    source_event: str = "manual",
    note_type: str = "session",
    tags: list[str] | None = None,
    certainty: int | str | None = None,
    branch_override: str | None = None,
    lifecycle_metadata: object | None = None,
) -> dict[str, Any]:
    if not title.strip():
        return {"error": "title is required"}
    if not body.strip():
        return {"error": "body is required"}
    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}
    project_slug, _ticket = detect_project(cwd, None) if cwd else ("unknown", None)
    branch = str(branch_override).strip() if branch_override else _git_branch(cwd)
    clean_note_type = str(note_type or "session").strip() or "session"
    merged_tags = _capture_note_tags(tags or [], project_slug, source_event)
    clean_certainty = _capture_certainty(certainty)
    lifecycle_metadata_value, lifecycle_metadata_error = _parse_lifecycle_metadata(lifecycle_metadata)
    if isinstance(clean_certainty, dict):
        return clean_certainty
    if queue and os.environ.get("MEMENTO_PI_PROCESSOR") == "true" and _is_lifecycle_source(source_event):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _append_capture_audit(
            {
                "ts": now,
                "decision": "processor_session_skipped",
                "queued": False,
                "skipped": True,
                "session_id": session_id,
                "cwd": cwd,
                "source_event": source_event,
                "reason": "processor_session",
                "body_hash": _body_hash(source_event or "lifecycle", body),
                "body_excerpt": _body_excerpt(body),
                "body_char_count": len(body),
                "lifecycle": lifecycle_metadata_value,
            },
            None,
        )
        return {
            "queued": False,
            "skipped": True,
            "reason": "processor_session",
            "source_event": source_event,
            "session_id": session_id,
        }
    if queue:
        if _is_lifecycle_source(source_event):
            decision = _lifecycle_queue_decision(session_id, cwd, body, source_event, lifecycle_metadata_value)
            if not decision["queue"]:
                return {
                    "queued": False,
                    "skipped": True,
                    "reason": decision["reason"],
                    "source_event": source_event,
                    "session_id": session_id,
                }
        capture_id = f"pi-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        metadata = {
            "cwd": cwd,
            "project": project_slug,
            "branch": branch,
            "session_id": session_id,
            "note_type": clean_note_type,
            "tags": merged_tags,
            "certainty": clean_certainty,
        }
        if os.environ.get("MEMENTO_PI_PROCESSOR") == "true":
            metadata["memento_processor"] = True
        if lifecycle_metadata_value:
            metadata["lifecycle"] = lifecycle_metadata_value
        if lifecycle_metadata_error:
            metadata["lifecycle_metadata_error"] = lifecycle_metadata_error
        capture = {
            "id": capture_id,
            "title": title.strip(),
            "body": body.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "source_event": source_event,
            "metadata": metadata,
        }
        with _queue_lock(vault):
            captures = _load_queue(vault)
            captures.append(capture)
            _write_queue(captures, vault)
        return {"id": capture_id, "title": title.strip(), "queued": True, "queue_path": str(_queue_file(vault))}

    clean_title = title.strip()
    clean_body = body.strip()
    with _vault_write_lock() as acquired:
        if not acquired:
            return {"error": "vault write lock unavailable"}
        note_path = write_note(
            vault,
            clean_title,
            clean_body,
            clean_note_type,
            merged_tags,
            certainty=clean_certainty,
            source="pi-capture",
            origin=f"pi_bridge:{source_event or reason or 'manual'}",
            project=cwd or None,
            branch=branch,
            session_id=session_id if session_id != "unknown" else None,
        )
        if reason == "manual" or source_event in {"manual", "tool"}:
            _mark_manual_capture_state(
                session_id,
                cwd,
                project_slug,
                branch,
                clean_title,
                clean_body,
                datetime.now(timezone.utc),
                lifecycle_metadata_value,
            )
        _commit_and_reindex_locked(vault, f"pi: capture {clean_title[:80]}")
    return {"path": str(note_path.relative_to(vault)), "title": clean_title, "queued": False}


def _triage(
    transcript_path: str,
    cwd: str = "",
    session_id: str = "",
    reason: str = "session_shutdown",
    source_event: str = "session_shutdown",
    detached: bool = True,
) -> dict[str, Any]:
    """Start SessionEnd-style triage for a persisted Pi transcript.

    The TypeScript extension calls this command at session boundaries. The
    bridge validates that the path is a local Pi transcript, then invokes the
    existing Claude SessionEnd triage hook with ``MEMENTO_AGENT=pi`` so both
    agents share substantiality scoring, fleeting/project writes, structured
    note extraction, health logging, auto-commit, reindex, and Inception hooks.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    raw_path = str(transcript_path or "").strip()
    effective_session_id = str(session_id or "").strip() or raw_path or "unknown"
    metadata = {
        "operation": "triage",
        "backend": "python3",
        "cwd": cwd,
        "project": _bridge_project_slug(cwd),
        "session_id": effective_session_id,
        "source_event": source_event,
        "reason": reason,
    }
    if not raw_path or raw_path == "unknown":
        log_triage_health("triage_missing_transcript", hook="pi-bridge", error="missing transcript path", **metadata)
        return {
            "queued": False,
            "skipped": True,
            "reason": "missing_transcript",
            "source_event": source_event,
            "session_id": effective_session_id,
        }

    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve()
    except (OSError, ValueError) as exc:
        log_triage_health(
            "triage_disallowed_transcript", hook="pi-bridge", error=exc, transcript_path=raw_path, **metadata
        )
        return {"queued": False, "skipped": True, "reason": "invalid_transcript_path", "error": str(exc)}

    if not resolved.exists():
        log_triage_health(
            "triage_missing_transcript",
            hook="pi-bridge",
            error="transcript file not found",
            transcript_path=str(resolved),
            **metadata,
        )
        return {
            "queued": False,
            "skipped": True,
            "reason": "missing_transcript",
            "source_event": source_event,
            "session_id": effective_session_id,
            "transcript_path": str(resolved),
        }
    if not _transcript_path_allowed(resolved):
        log_triage_health(
            "triage_disallowed_transcript",
            hook="pi-bridge",
            error="transcript_path outside allowed Pi roots",
            transcript_path=str(resolved),
            **metadata,
        )
        return {
            "queued": False,
            "skipped": True,
            "reason": "transcript_path_not_allowed",
            "source_event": source_event,
            "session_id": effective_session_id,
            "transcript_path": str(resolved),
        }

    fingerprint = _triage_transcript_fingerprint(resolved)
    state = _load_capture_session_state(effective_session_id, cwd)
    state_status = _triage_state_status(state)
    same_transcript = _same_triage_transcript_version(state, resolved, fingerprint)
    if same_transcript and (
        _triage_state_active(state) or (state_status == "completed" and state.get("pi_triage_returncode") == 0)
    ):
        reason_code = "already_completed" if state_status == "completed" else "already_started"
        log_triage_health(
            "triage_duplicate_skipped",
            hook="pi-bridge",
            transcript_path=str(resolved),
            started_at=state.get("pi_triage_started_at"),
            status=state_status,
            duplicate_reason=reason_code,
            **metadata,
        )
        return {
            "queued": False,
            "skipped": True,
            "reason": reason_code,
            "source_event": source_event,
            "session_id": effective_session_id,
            "transcript_path": str(resolved),
        }

    hook_script = _triage_hook_script()
    if not hook_script.exists():
        log_triage_health(
            "triage_spawn_failed",
            hook="pi-bridge",
            error="memento-triage.py not found",
            transcript_path=str(resolved),
            **metadata,
        )
        return {"queued": False, "error": f"triage hook not found: {hook_script}"}

    hook_session_id = _safe_hook_session_id(session_id, raw_path)
    payload = {
        "transcript_path": str(resolved),
        "cwd": cwd,
        "agent": "pi",
        "source_event": source_event,
        "reason": reason,
    }
    if hook_session_id:
        payload["session_id"] = hook_session_id
    env = os.environ.copy()
    env["MEMENTO_AGENT"] = "pi"
    env.setdefault("MEMENTO_PI_TRANSCRIPT_ROOTS", os.environ.get("MEMENTO_PI_TRANSCRIPT_ROOTS", ""))

    try:
        if detached:
            payload_path = _write_triage_payload(payload)
            stdin_handle = payload_path.open("rb")
            try:
                process = subprocess.Popen(
                    [sys.executable, str(hook_script)],
                    stdin=stdin_handle,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=str(hook_script.parent.parent),
                    env=env,
                    start_new_session=True,
                )
            finally:
                stdin_handle.close()
            state.update(
                {
                    "session_id": effective_session_id,
                    "cwd": cwd,
                    "pi_triage_status": "active",
                    "pi_triage_started_at": now,
                    "pi_triage_active_until": _triage_active_until(now),
                    "pi_triage_completed_at": None,
                    "pi_triage_transcript_path": str(resolved),
                    "pi_triage_transcript_size_bytes": fingerprint["size_bytes"],
                    "pi_triage_transcript_mtime_ns": fingerprint["mtime_ns"],
                    "pi_triage_payload_path": str(payload_path),
                    "pi_triage_pid": process.pid,
                    "pi_triage_source_event": source_event,
                    "pi_triage_reason": reason,
                    "last_lifecycle_decision": "pi_triage_spawned",
                }
            )
            _write_capture_session_state(effective_session_id, cwd, state)
            _append_capture_audit(
                {
                    "ts": now,
                    "decision": "triage_spawned",
                    "queued": False,
                    "skipped": False,
                    "session_id": effective_session_id,
                    "cwd": cwd,
                    "source_event": source_event,
                    "reason": reason,
                    "transcript_path": str(resolved),
                    "payload_path": str(payload_path),
                    "pid": process.pid,
                }
            )
            log_triage_health(
                "triage_spawned",
                hook="pi-bridge",
                transcript_path=str(resolved),
                payload_path=str(payload_path),
                pid=process.pid,
                **metadata,
            )
            return {
                "queued": False,
                "started": True,
                "detached": True,
                "pid": process.pid,
                "session_id": effective_session_id,
                "source_event": source_event,
                "reason": reason,
                "transcript_path": str(resolved),
                "payload_path": str(payload_path),
            }

        completed = subprocess.run(
            [sys.executable, str(hook_script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(hook_script.parent.parent),
            env=env,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        log_triage_health("triage_spawn_failed", hook="pi-bridge", error=exc, transcript_path=str(resolved), **metadata)
        return {"queued": False, "error": str(exc), "reason": "spawn_failed"}

    state.update(
        {
            "session_id": effective_session_id,
            "cwd": cwd,
            "pi_triage_status": "completed" if completed.returncode == 0 else "failed",
            "pi_triage_started_at": now,
            "pi_triage_active_until": None,
            "pi_triage_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "pi_triage_transcript_path": str(resolved),
            "pi_triage_transcript_size_bytes": fingerprint["size_bytes"],
            "pi_triage_transcript_mtime_ns": fingerprint["mtime_ns"],
            "pi_triage_source_event": source_event,
            "pi_triage_reason": reason,
            "pi_triage_returncode": completed.returncode,
            "last_lifecycle_decision": "pi_triage_completed" if completed.returncode == 0 else "pi_triage_failed",
        }
    )
    _write_capture_session_state(effective_session_id, cwd, state)
    action = "triage_completed" if completed.returncode == 0 else "triage_failed"
    log_triage_health(
        action, hook="pi-bridge", transcript_path=str(resolved), returncode=completed.returncode, **metadata
    )
    return {
        "queued": False,
        "detached": False,
        "returncode": completed.returncode,
        "session_id": effective_session_id,
        "source_event": source_event,
        "reason": reason,
        "transcript_path": str(resolved),
    }


def _body_excerpt(body: str, max_chars: int = 180) -> str:
    compact = re.sub(r"\s+", " ", body).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max(1, max_chars - 1)].rstrip() + "…"


def _capture_review_metadata(capture: dict[str, Any]) -> dict[str, Any]:
    body = str(capture.get("body") or "")
    size_bytes = len(body.encode("utf-8"))
    return {
        "body_excerpt": _body_excerpt(body),
        "body_char_count": len(body),
        "body_size_bytes": size_bytes,
        "body_kb": round(size_bytes / 1024, 1),
    }


def _summary_cache_file() -> Path:
    return _state_root() / "queue" / "pi-capture-summaries.json"


def _read_summary_cache() -> dict[str, Any]:
    path = _summary_cache_file()
    if not path.exists():
        return {"version": 1, "captures": {}}
    try:
        payload = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "captures": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "captures": {}}
    captures = payload.get("captures")
    if not isinstance(captures, dict):
        captures = {}
    return {"version": 1, "captures": captures}


def _write_summary_cache(cache: dict[str, Any]) -> None:
    _atomic_write_text(_summary_cache_file(), json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _capture_summary_digest(capture: dict[str, Any]) -> str:
    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    payload = {
        "title": str(capture.get("title") or ""),
        "body": str(capture.get("body") or ""),
        "reason": str(capture.get("reason") or ""),
        "source_event": str(capture.get("source_event") or ""),
        "metadata": metadata,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _summary_cache_key(capture: dict[str, Any], digest: str) -> str:
    capture_id = str(capture.get("id") or "").strip()
    return capture_id or digest


def _summary_prompt_value(value: Any) -> str:
    return sanitize_secrets(str(value or ""))


def _summary_prompt(capture: dict[str, Any]) -> str:
    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    lifecycle = metadata.get("lifecycle") if isinstance(metadata.get("lifecycle"), dict) else {}
    body = sanitize_secrets(str(capture.get("body") or ""))
    if len(body) > 4000:
        body = body[:4000].rstrip() + "\n[truncated]"
    context = {
        "title": _summary_prompt_value(capture.get("title")),
        "reason": _summary_prompt_value(capture.get("reason")),
        "source_event": _summary_prompt_value(capture.get("source_event")),
        "project": _summary_prompt_value(metadata.get("project") or metadata.get("project_slug")),
        "branch": _summary_prompt_value(metadata.get("branch")),
        "turn_count": lifecycle.get("turn_count"),
        "tool_call_count": lifecycle.get("tool_call_count"),
        "file_edit_count": lifecycle.get("file_edit_count"),
    }
    return (
        "Summarize this queued Memento capture candidate for a human deciding whether to process it. "
        "Return one concise plain-text sentence, at most 35 words. State the durable memory signal if one is present; "
        "if it looks low-value, say so briefly. Do not use Markdown, bullets, labels, or quote secrets.\n\n"
        f"Context JSON:\n{json.dumps(context, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Capture body:\n{body}"
    )


def _clean_generated_summary(text: str) -> str:
    summary = sanitize_secrets(re.sub(r"\s+", " ", text).strip())
    summary = re.sub(r"^[-*•\d.)\s]+", "", summary).strip()
    summary = summary.strip('`"')
    if len(summary) > 280:
        summary = summary[:279].rstrip() + "…"
    return summary


def _capture_generated_summary(capture: dict[str, Any], cache: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    digest = _capture_summary_digest(capture)
    key = _summary_cache_key(capture, digest)
    records = cache.setdefault("captures", {})
    cached = records.get(key) if isinstance(records, dict) else None
    if isinstance(cached, dict) and cached.get("digest") == digest and cached.get("text"):
        cleaned_text = _clean_generated_summary(str(cached.get("text") or ""))
        record = {**cached, "text": cleaned_text}
        changed = cleaned_text != cached.get("text")
        if changed:
            records[key] = record
        return {**record, "cached": True}, changed

    from memento.llm import llm_complete

    result = llm_complete(_summary_prompt(capture), timeout=10)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not result.ok:
        return {
            "status": "error",
            "error": str(result.error or "summary generation failed")[:500],
            "digest": digest,
            "generated_at": generated_at,
            "backend": result.backend,
            "model": result.model,
            "cached": False,
        }, False

    summary = _clean_generated_summary(result.text)
    if not summary:
        return {
            "status": "error",
            "error": "summary generation returned empty text",
            "digest": digest,
            "generated_at": generated_at,
            "backend": result.backend,
            "model": result.model,
            "cached": False,
        }, False

    record = {
        "status": "ok",
        "text": summary,
        "digest": digest,
        "generated_at": generated_at,
        "backend": result.backend,
        "model": result.model,
        "prompt_bytes": result.prompt_bytes,
        "output_bytes": result.output_bytes,
        "duration_ms": result.duration_ms,
    }
    records[key] = record
    return {**record, "cached": False}, True


def _capture_lifecycle_snapshot(capture: dict[str, Any]) -> dict[str, Any]:
    metadata = capture.get("metadata") or {}
    lifecycle = metadata.get("lifecycle") if isinstance(metadata, dict) else {}
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    return {
        "source_event": str(capture.get("source_event") or lifecycle.get("source_event") or ""),
        "reason": str(capture.get("reason") or lifecycle.get("reason") or ""),
        "event_timestamp": lifecycle.get("event_timestamp"),
        "event_index": lifecycle.get("event_index"),
        "turn_count": lifecycle.get("turn_count"),
        "user_message_count": lifecycle.get("user_message_count"),
        "assistant_message_count": lifecycle.get("assistant_message_count"),
        "tool_call_count": lifecycle.get("tool_call_count"),
        "file_edit_count": lifecycle.get("file_edit_count"),
        "file_read_count": lifecycle.get("file_read_count"),
        "session_entry_count": lifecycle.get("session_entry_count"),
        "session_last_entry_at": lifecycle.get("session_last_entry_at"),
        "summary": lifecycle.get("summary"),
        "body_digest": lifecycle.get("body_digest"),
    }


def _queue_list(
    limit: int = 20, include_body: bool = False, include_generated_summaries: bool = False
) -> dict[str, Any]:
    captures = _load_queue()
    visible = []
    cache = _read_summary_cache() if include_generated_summaries else None
    cache_changed = False
    summary_errors = 0
    for capture in captures[-max(1, int(limit)) :]:
        item = {**capture, **_capture_review_metadata(capture)}
        if include_generated_summaries and cache is not None:
            summary, changed = _capture_generated_summary(capture, cache)
            item["generated_summary"] = summary
            cache_changed = cache_changed or changed
            if summary.get("status") == "error":
                summary_errors += 1
        if not include_body:
            item.pop("body", None)
        visible.append(item)
    if cache_changed and cache is not None:
        _write_summary_cache(cache)
    payload: dict[str, Any] = {"count": len(captures), "captures": visible, "queue_path": str(_queue_file())}
    if include_generated_summaries:
        payload["generated_summaries"] = {
            "enabled": True,
            "cache_path": str(_summary_cache_file()),
            "visible_count": len(visible),
            "error_count": summary_errors,
        }
    return payload


_THINKING_DUMP_SIGNAL = '"type":"thinking"'
_RAW_DUMP_PREFIXES = ("- assistant: [{", "- toolResult: [{", "- user: [{")
_CLEANUP_DISCARDABLE_CLASSES = ("raw_dump", "low_value", "invalid")
_CLEANUP_DEFAULT_DISCARD_CLASSES = ("raw_dump", "invalid")


def _classify_queued_capture(capture: dict[str, Any]) -> tuple[str, str]:
    """Cheap, deterministic classification of a queued capture.

    Classes: manual (always preserved), durable_candidate (retained),
    raw_dump / low_value / invalid (discardable). No LLM calls — this must
    stay much faster than full curator processing.
    """
    if capture.get("error") == "invalid-json":
        return "invalid", "unparseable queue line"
    source_event = str(capture.get("source_event") or "")
    if source_event not in _LIFECYCLE_SOURCE_EVENTS:
        return "manual", f"non-lifecycle source_event {source_event or 'unknown'!r}"
    body = str(capture.get("body") or "")
    if _THINKING_DUMP_SIGNAL in body:
        return "raw_dump", "body embeds raw thinking-block JSON"
    if body.lstrip().startswith(_RAW_DUMP_PREFIXES):
        return "raw_dump", "body is a raw lifecycle message dump"
    if not _MEANINGFUL_KEYWORDS.search(body):
        return "low_value", "lifecycle capture without durable-signal keywords"
    return "durable_candidate", "lifecycle capture with durable-signal keywords"


def _queue_cleanup(
    apply: bool = False,
    discard_classes: list[str] | None = None,
    samples: int = 3,
    vault: Path | None = None,
) -> dict[str, Any]:
    """Classify queued captures and optionally archive the low-value ones.

    Dry-run by default: nothing is written unless apply=True. Applying never
    deletes data — discarded captures move to a timestamped archive JSONL
    next to the queue (full original entry plus discard provenance), and the
    pre-cleanup queue is kept as a .bak copy.
    """
    discard_set = set(discard_classes or _CLEANUP_DEFAULT_DISCARD_CLASSES)
    unknown = discard_set.difference(_CLEANUP_DISCARDABLE_CLASSES)
    if unknown:
        return {"error": f"non-discardable classes requested: {sorted(unknown)}"}

    queue_path = _queue_file(vault)
    captures = _load_queue(vault)
    by_class: dict[str, int] = {}
    sample_by_class: dict[str, list[dict[str, Any]]] = {}
    retained: list[dict[str, Any]] = []
    discarded: list[tuple[dict[str, Any], str, str]] = []
    for capture in captures:
        klass, reason = _classify_queued_capture(capture)
        by_class[klass] = by_class.get(klass, 0) + 1
        bucket = sample_by_class.setdefault(klass, [])
        if len(bucket) < max(0, int(samples)):
            bucket.append(
                {
                    "id": capture.get("id"),
                    "title": capture.get("title"),
                    "reason": reason,
                    "created_at": capture.get("created_at"),
                    "body_excerpt": _body_excerpt(str(capture.get("body") or "")),
                }
            )
        if klass in discard_set:
            discarded.append((capture, klass, reason))
        else:
            retained.append(capture)

    result: dict[str, Any] = {
        "queue_path": str(queue_path),
        "dry_run": not apply,
        "total": len(captures),
        "retained": len(retained),
        "discarded": len(discarded),
        "by_class": by_class,
        "discard_classes": sorted(discard_set),
        "samples": sample_by_class,
    }
    if not apply or not discarded:
        return result

    lock_path = _lock_file()
    if lock_path.exists():
        lock = _read_processing_lock(lock_path)
        if _is_pid_alive(int(lock.get("pid", 0))):
            result["blocked"] = f"processing run active (pid {lock.get('pid')}, run {lock.get('run_id')})"
            result["dry_run"] = True
            return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = queue_path.with_name(f"{queue_path.name}.bak-{stamp}-cleanup")
    backup_path.write_text(queue_path.read_text(errors="replace") if queue_path.exists() else "")
    archive_path = queue_path.with_name(f"pi-captures-discarded-{stamp}.jsonl")
    discarded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with archive_path.open("a") as handle:
        for capture, klass, reason in discarded:
            entry = dict(capture)
            entry["cleanup"] = {"discarded_at": discarded_at, "class": klass, "reason": reason}
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with _queue_lock(vault):
        current = _load_queue(vault)
        still_discardable = []
        for capture in current:
            klass, _reason = _classify_queued_capture(capture)
            if klass in discard_set:
                still_discardable.append(capture)
        final_retained = [c for c in current if c not in still_discardable]
        _write_queue(final_retained, vault)
    result["backup_path"] = str(backup_path)
    result["archive_path"] = str(archive_path)
    return result


def _capture_queue_snapshot(capture: dict[str, Any]) -> dict[str, Any]:
    metadata = capture.get("metadata") or {}
    body = str(capture.get("body") or "")
    size_bytes = len(body.encode("utf-8"))
    lifecycle = _capture_lifecycle_snapshot(capture)
    return {
        "id": capture.get("id"),
        "title": capture.get("title"),
        "created_at": capture.get("created_at"),
        "reason": capture.get("reason"),
        "source_event": capture.get("source_event"),
        "project": metadata.get("project"),
        "branch": metadata.get("branch"),
        "session_id": metadata.get("session_id"),
        "body_excerpt": _body_excerpt(body),
        "body_char_count": len(body),
        "body_size_bytes": size_bytes,
        "body_kb": round(size_bytes / 1024, 1),
        "lifecycle": lifecycle,
        "lifecycle_reason": lifecycle.get("reason"),
        "turn_count": lifecycle.get("turn_count"),
        "tool_call_count": lifecycle.get("tool_call_count"),
        "file_edit_count": lifecycle.get("file_edit_count"),
        "file_read_count": lifecycle.get("file_read_count"),
    }


def _queue_discard(
    capture_id: str | list[str] | tuple[str, ...],
    apply: bool = False,
    reason: str = "manual_discard",
    source: str = "queue-discard",
    vault: Path | None = None,
) -> dict[str, Any]:
    if isinstance(capture_id, (list, tuple)):
        capture_ids = [str(item).strip() for item in capture_id if str(item).strip()]
    else:
        raw_capture_id = str(capture_id).strip()
        capture_ids = [raw_capture_id] if raw_capture_id else []
    if not capture_ids:
        return {"error": "at least one capture id is required", "reason": "missing_capture_ids"}

    queue_path = _queue_file(vault)
    captures = _load_queue(vault)
    capture_id_set = set(capture_ids)
    found = [capture for capture in captures if str(capture.get("id")) in capture_id_set]
    found_ids = {str(capture.get("id")) for capture in found}
    missing_ids = [capture_id for capture_id in capture_ids if capture_id not in found_ids]
    result: dict[str, Any] = {
        "queue_path": str(queue_path),
        "dry_run": not apply,
        "discarded": len(found),
        "remaining": len(captures) - len(found),
        "captures": [_capture_queue_snapshot(capture) for capture in found],
        "reason": reason,
        "source": source,
    }
    if missing_ids:
        result["error"] = "capture ids not found"
        result["missing_ids"] = missing_ids
        return result
    if not apply:
        return result

    lock_path = _lock_file()
    if lock_path.exists():
        lock = _read_processing_lock(lock_path)
        if _is_pid_alive(int(lock.get("pid", 0))):
            result["blocked"] = f"processing run active (pid {lock.get('pid')}, run {lock.get('run_id')})"
            result["dry_run"] = True
            return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = queue_path.with_name(f"{queue_path.name}.bak-{stamp}-discard")
    backup_path.write_text(queue_path.read_text(errors="replace") if queue_path.exists() else "")
    archive_path = queue_path.with_name(f"pi-captures-discarded-{stamp}.jsonl")
    discarded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _queue_lock(vault):
        current = _load_queue(vault)
        current_ids = {str(capture.get("id")) for capture in current}
        if not capture_id_set.issubset(current_ids):
            result["error"] = "capture ids not found"
            result["missing_ids"] = sorted(capture_id_set.difference(current_ids))
            return result
        current_map = {str(capture.get("id")): capture for capture in current}
        retained = [capture for capture in current if str(capture.get("id")) not in capture_id_set]
        with archive_path.open("a") as handle:
            for capture_id in capture_ids:
                capture = current_map[capture_id]
                entry = dict(capture)
                entry["discard"] = {"discarded_at": discarded_at, "reason": reason, "source": source}
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _write_queue(retained, vault)
    result["backup_path"] = str(backup_path)
    result["archive_path"] = str(archive_path)
    result["retained"] = len(retained)
    result["remaining"] = len(retained)
    return result


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


def _read_processing_lock(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {"pid": 0, "run_id": "unknown"}


def _processing_lock_stale(existing: dict[str, Any]) -> bool:
    pid = int(existing.get("pid") or 0)
    created_at = float(existing.get("created_time") or 0)
    return bool((pid and not _is_pid_alive(pid)) or (created_at and time.time() - created_at > 24 * 60 * 60))


def _acquire_processing_lock(run_id: str, owner_pid: int = 0) -> dict[str, Any] | None:
    path = _lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "pid": owner_pid or os.getpid(),
        "created_time": time.time(),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    for _attempt in range(2):
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            return None
        except FileExistsError:
            existing = _read_processing_lock(path)
            if not _processing_lock_stale(existing):
                return {
                    "error": "another memento processing run is active",
                    "reason": "processing_lock_active",
                    "lock": existing,
                }
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    existing = _read_processing_lock(path)
    return {
        "error": "another memento processing run is active",
        "reason": "processing_lock_active",
        "lock": existing,
    }


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


def _normalize_capture_ids(capture_id: str | list[str] | tuple[str, ...] = "") -> set[str]:
    if isinstance(capture_id, (list, tuple)):
        return {str(item) for item in capture_id if str(item)}
    return {str(capture_id)} if capture_id else set()


def _selected_captures(
    captures: list[dict[str, Any]],
    capture_id: str | list[str] | tuple[str, ...] = "",
    project: str = "",
    branch: str = "",
    session_id: str = "",
    newest: bool = False,
) -> list[dict[str, Any]]:
    selected = []
    capture_ids = _normalize_capture_ids(capture_id)
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
        if metadata.get("memento_processor") is True:
            continue
        selected.append(capture)
    selected.sort(key=_capture_created_at, reverse=newest)
    return selected


def _selected_queue_captures(
    captures: list[dict[str, Any]],
    capture_id: str | list[str] | tuple[str, ...] = "",
    project: str = "",
    branch: str = "",
    session_id: str = "",
    newest: bool = False,
) -> list[dict[str, Any]]:
    selected = []
    capture_ids = _normalize_capture_ids(capture_id)
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


def _dedup_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""


def _frontmatter_tags(text: str) -> list[str]:
    match = re.search(r"^tags:\s*\[([^\]]*)\]", text, re.MULTILINE)
    if not match:
        return []
    return [item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()]


def _dedup_context_for_group(vault: Path, group: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """Return deterministic existing-note context for queued-capture curation.

    The curator runs in a separate Pi session, so it must receive note titles and
    paths up front rather than relying on voluntary search calls to discover
    duplicates. Ranking is deterministic: project matches first, then lexical
    overlap against queued capture titles/bodies, then path.
    """
    notes_dir = vault / "notes"
    if not notes_dir.exists():
        return []

    project = str(group.get("project") or "").strip().lower()
    query_parts: list[str] = []
    for capture in group.get("captures", []):
        query_parts.append(str(capture.get("title") or ""))
        query_parts.append(str(capture.get("body") or "")[:1000])
    query_tokens = _dedup_tokens("\n".join(query_parts))

    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    for note_path in sorted(notes_dir.glob("*.md"), key=lambda path: path.name):
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = _frontmatter_value(text, "title") or note_path.stem
        note_type = _frontmatter_value(text, "type")
        note_project = _frontmatter_value(text, "project")
        tags = _frontmatter_tags(text)
        haystack = " ".join([title, note_type, note_project, " ".join(tags)])
        overlap = len(query_tokens & _dedup_tokens(haystack))
        note_tag_set = {tag.lower() for tag in tags}
        project_match = int(bool(project) and (note_project.lower() == project or project in note_tag_set))
        if overlap <= 0:
            continue
        rel_path = str(note_path.relative_to(vault))
        item = {
            "path": rel_path,
            "title": title,
            "type": note_type,
            "tags": tags,
            "project": note_project,
        }
        ranked.append((project_match, overlap, rel_path, item))

    ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [item for *_rank, item in ranked[: max(0, int(limit))]]


def _transcript_context_lines(transcript_info: dict[str, Any]) -> list[str]:
    included = bool(transcript_info.get("included"))
    reason = str(transcript_info.get("reason") or ("included" if included else "unknown"))
    lines = ["", "## Transcript context"]
    if included:
        if reason == "over_size_cap":
            lines.extend(
                [
                    "- Mode: partial cleaned transcript from an oversize source.",
                    f"- Raw size: {transcript_info.get('size_bytes', 'unknown')} bytes; cleaned excerpt: {transcript_info.get('cleaned_char_count', 'unknown')} chars capped at {transcript_info.get('cleaned_cap_chars', 'unknown')} chars.",
                    "- Quality limit: transcript evidence is capped; prefer durable facts corroborated by queued captures, file context, or deduplication context.",
                    "- Curator instruction: if the capped transcript and queued captures do not provide enough context for a high-quality note, return processed_no_notes with a discard_reason explaining the oversize partial transcript.",
                ]
            )
        elif transcript_info.get("partial"):
            lines.extend(
                [
                    "- Mode: partial cleaned transcript capped during transcript cleaning.",
                    f"- Raw size: {transcript_info.get('size_bytes', 'unknown')} bytes; cleaned excerpt: {transcript_info.get('cleaned_char_count', 'unknown')} chars capped at {transcript_info.get('cleaned_cap_chars', 'unknown')} chars.",
                    "- Quality limit: transcript evidence is capped; prefer durable facts corroborated by queued captures, file context, or deduplication context.",
                    "- Curator instruction: if the capped transcript and queued captures do not provide enough context for a high-quality note, return processed_no_notes with a discard_reason explaining the partial transcript.",
                ]
            )
        else:
            lines.extend(
                [
                    "- Mode: cleaned session transcript included.",
                    f"- Raw size: {transcript_info.get('size_bytes', 'unknown')} bytes; cleaned chars: {transcript_info.get('cleaned_char_count', 'unknown')}.",
                ]
            )
        return lines

    if reason == "no_session_id":
        detail = "the queued captures did not include a session transcript path"
    elif reason == "missing":
        detail = "the recorded session transcript path does not exist"
    elif reason == "outside_allowed_roots":
        detail = "the recorded transcript path is outside the configured Pi transcript roots"
    elif reason == "empty_cleaned_transcript":
        detail = "the transcript contained no renderable user/assistant content after cleaning"
    else:
        detail = reason.replace("_", " ")
    lines.extend(
        [
            "- Mode: explicit fallback; no cleaned session transcript is available.",
            f"- Reason: {detail}.",
            "- Quality limit: queued captures are lifecycle fragments and may omit final decisions, rejected alternatives, and command outcomes.",
            "- Curator instruction: create notes only when durable facts are explicit in queued captures, lifecycle context, or deduplication context; otherwise defer by returning processed_no_notes with a discard_reason explaining the transcript limitation.",
        ]
    )
    return lines


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
    ]
    lines.extend(_transcript_context_lines(group.get("transcript") or {"included": False, "reason": "unknown"}))
    lines.extend(
        [
            "",
            "## Deduplication context",
        ]
    )
    dedup_context = group.get("dedup_context") or []
    if dedup_context:
        lines.append(
            "Existing notes selected deterministically from the vault. Treat matching titles/topics as likely duplicates; read a candidate with memento_get before deciding to create overlapping notes."
        )
        for item in dedup_context:
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            tag_text = f" tags=[{', '.join(str(tag) for tag in tags)}]" if tags else ""
            type_text = f" type={item.get('type')}" if item.get("type") else ""
            lines.append(f"- {item.get('path')}: {item.get('title')}{type_text}{tag_text}")
    else:
        lines.append("- No existing note candidates matched this group deterministically.")
    lines.extend(["", "## Queued captures"])
    for capture in group.get("captures", []):
        metadata = capture.get("metadata") or {}
        lifecycle = metadata.get("lifecycle") if isinstance(metadata, dict) else {}
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        lifecycle_lines = []
        if lifecycle:
            file_edits = lifecycle.get("file_edits") if isinstance(lifecycle.get("file_edits"), list) else []
            file_reads = lifecycle.get("file_reads") if isinstance(lifecycle.get("file_reads"), list) else []
            file_edits_text = ", ".join(str(path) for path in file_edits[:5]) if file_edits else ""
            file_reads_text = ", ".join(str(path) for path in file_reads[:5]) if file_reads else ""
            lifecycle_lines = [
                "",
                "- Lifecycle context:",
                f"  - Source event: {lifecycle.get('source_event') or capture.get('source_event') or ''}",
                f"  - Reason: {lifecycle.get('reason') or capture.get('reason') or ''}",
                f"  - Event timestamp: {lifecycle.get('event_timestamp') or ''}",
                f"  - Event index: {lifecycle.get('event_index') or ''}",
                f"  - Turns: {lifecycle.get('turn_count') or ''}",
                f"  - Tool calls: {lifecycle.get('tool_call_count') or ''}",
                f"  - File edits: {file_edits_text}",
                f"  - File reads: {file_reads_text}",
                f"  - Session entries: {lifecycle.get('session_entry_count') or ''}",
                f"  - Last session entry: {lifecycle.get('session_last_entry_at') or ''}",
            ]
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
            ]
            + lifecycle_lines
            + [
                "",
                str(capture.get("body") or ""),
            ]
        )
    if transcript_markdown:
        lines.extend(["", "## Cleaned session transcript", "", transcript_markdown])
    return "\n".join(lines).rstrip() + "\n"


def _transcript_context_for_group(
    group: dict[str, Any], transcript_max_bytes: int, cleaned_cap_chars: int = 200000
) -> tuple[dict[str, Any], str]:
    if not group.get("session_id"):
        return {"included": False, "reason": "no_session_id"}, ""

    transcript_path = Path(str(group["session_id"])).expanduser()
    base_info: dict[str, Any] = {"path": str(transcript_path), "included": False}
    if not _transcript_path_allowed(transcript_path):
        return {**base_info, "reason": "outside_allowed_roots"}, ""

    if not transcript_path.exists():
        return {**base_info, "reason": "missing"}, ""

    try:
        size = transcript_path.stat().st_size
    except OSError as exc:
        return {**base_info, "reason": "unreadable", "error": str(exc)}, ""
    base_info["size_bytes"] = size

    try:
        transcript_markdown = _clean_transcript(transcript_path, total_cap=cleaned_cap_chars)
    except OSError as exc:
        return {**base_info, "reason": "unreadable", "error": str(exc)}, ""
    if not transcript_markdown.strip():
        return {**base_info, "reason": "empty_cleaned_transcript"}, ""

    partial = size > int(transcript_max_bytes) or "[transcript truncated by memento processor]" in transcript_markdown
    reason = "over_size_cap" if size > int(transcript_max_bytes) else "included"
    return {
        **base_info,
        "included": True,
        "reason": reason,
        "partial": partial,
        "cleaned_char_count": len(transcript_markdown),
        "cleaned_cap_chars": cleaned_cap_chars,
    }, transcript_markdown


def _transcript_status_counts(groups: list[dict[str, Any]]) -> dict[str, Any]:
    by_reason: dict[str, int] = {}
    included = 0
    partial = 0
    oversize = 0
    missing = 0
    fallback = 0
    for group in groups:
        transcript = group.get("transcript") if isinstance(group.get("transcript"), dict) else {}
        is_included = bool(transcript.get("included"))
        reason = str(transcript.get("reason") or ("included" if is_included else "unknown"))
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if is_included:
            included += 1
        else:
            fallback += 1
        if transcript.get("partial") or reason == "over_size_cap":
            partial += 1
        if reason == "over_size_cap":
            oversize += 1
        if reason in {"missing", "no_session_id", "empty_cleaned_transcript", "unreadable", "outside_allowed_roots"}:
            missing += 1
    return {
        "transcript_included_group_count": included,
        "transcript_partial_group_count": partial,
        "transcript_fallback_group_count": fallback,
        "oversize_transcript_group_count": oversize,
        "missing_transcript_group_count": missing,
        "transcript_reason_counts": by_reason,
    }


def _allowed_transcript_roots() -> list[Path]:
    roots = [Path.home() / ".pi" / "agent" / "sessions", Path.home() / ".pi" / "agent" / "subagents"]
    session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if session_dir:
        roots.append(Path(session_dir).expanduser())
    extra_roots = os.environ.get("MEMENTO_PI_TRANSCRIPT_ROOTS", "")
    for raw in extra_roots.split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    resolved = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except (OSError, ValueError):
            continue
    return resolved


def _transcript_path_allowed(path: Path) -> bool:
    try:
        candidate = path.resolve()
    except (OSError, ValueError):
        return False
    return any(candidate == root or root in candidate.parents for root in _allowed_transcript_roots())


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
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
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
            if total_cap > 0:
                remaining = total_cap - total
                if remaining <= 0:
                    lines.append("\n[transcript truncated by memento processor]")
                    break
                if len(block) > remaining:
                    trimmed = block[:remaining].rstrip()
                    if trimmed:
                        lines.append(trimmed)
                    lines.append("\n[transcript truncated by memento processor]")
                    total = total_cap
                    break
            lines.append(block)
            total += len(block)
    return "\n\n".join(lines)


class _PiQueueStore:
    def load(self, vault: Path) -> list[dict[str, Any]]:
        return _load_queue(vault)

    def write(self, captures: list[dict[str, Any]], vault: Path) -> None:
        _write_queue(captures, vault)

    def path(self, vault: Path) -> Path:
        return _queue_file(vault)

    def lock(self):
        return _queue_lock()


class _PiProcessingStore:
    def root(self) -> Path:
        return _processing_root()

    def acquire_lock(self, run_id: str, owner_pid: int = 0) -> dict[str, Any] | None:
        return _acquire_processing_lock(run_id, owner_pid)

    def release_lock(self, run_id: str) -> None:
        _release_processing_lock(run_id)

    def write_text(self, path: Path, content: str) -> None:
        _atomic_write_text(path, content)

    def read_json(self, path: Path) -> dict[str, Any]:
        return _read_json_file(path)


class _PiGroupPreparer:
    def prepare(self, run_dir: Path, vault: Path, group: dict[str, Any], transcript_max_bytes: int) -> dict[str, Any]:
        inputs_dir = run_dir / "inputs"
        results_dir = run_dir / "results"
        logs_dir = run_dir / "logs"
        group["dedup_context"] = _dedup_context_for_group(vault, group)
        transcript_info, transcript_markdown = _transcript_context_for_group(group, transcript_max_bytes)
        group["transcript"] = transcript_info
        group_id = group["group_id"]
        _atomic_write_text(inputs_dir / f"{group_id}.json", json.dumps(group, ensure_ascii=False, indent=2))
        _atomic_write_text(inputs_dir / f"{group_id}.md", _render_capture_packet(group, transcript_markdown))
        return {
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
            "dedup_context": group.get("dedup_context", []),
            "transcript": transcript_info,
        }


class _PiVaultWriter:
    def reported_note_exists(self, vault: Path, path: str) -> bool:
        return _reported_note_exists_in_vault(vault, path)

    def on_dequeued(self, vault: Path, run_id: str, dequeue_ids: set[str]) -> None:
        if not dequeue_ids:
            return
        with _vault_write_lock() as acquired:
            if acquired:
                _commit_and_reindex_locked(vault, f"pi: process-finalize {run_id[:8]}")


def _capture_runtime() -> CaptureRuntime:
    return CaptureRuntime(
        vault=get_vault,
        queue=_PiQueueStore(),
        processing=_PiProcessingStore(),
        preparer=_PiGroupPreparer(),
        writer=_PiVaultWriter(),
        transcript_counter=_transcript_status_counts,
    )


def _queue_process_start(
    capture_id: str | list[str] = "",
    project: str = "",
    branch: str = "",
    session_id: str = "",
    limit: int = 0,
    newest: bool = False,
    dry_run: bool = False,
    transcript_max_bytes: int = 2 * 1024 * 1024,
    owner_pid: int = 0,
) -> dict[str, Any]:
    return _capture_runtime().process(
        CaptureProcessRequest(
            capture_id=capture_id,
            project=project,
            branch=branch,
            session_id=session_id,
            limit=limit,
            newest=newest,
            dry_run=dry_run,
            transcript_max_bytes=transcript_max_bytes,
            owner_pid=owner_pid,
        )
    )


def _latest_processing_run_id() -> str:
    root = _processing_root()
    if not root.exists():
        return ""
    runs = [path for path in root.iterdir() if path.is_dir()]
    if not runs:
        return ""
    runs.sort(key=lambda path: path.name, reverse=True)
    return runs[0].name


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


PROCESS_LOG_TAIL_CHARS = 2400
PROCESS_LOG_TAIL_LINES = 12


def _process_log_tail(path_value: Any) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(str(path_value))
    try:
        stat = path.stat()
    except OSError:
        return {"log_error": "log file unavailable"}
    if not path.is_file():
        return {"log_error": "log file unavailable"}
    try:
        read_size = min(stat.st_size, PROCESS_LOG_TAIL_CHARS * 4)
        with path.open("rb") as handle:
            if stat.st_size > read_size:
                handle.seek(-read_size, os.SEEK_END)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return {"log_error": f"log read failed: {exc}"}
    tail = text[-PROCESS_LOG_TAIL_CHARS:]
    lines = tail.splitlines()
    line_truncated = len(lines) > PROCESS_LOG_TAIL_LINES
    if line_truncated:
        lines = lines[-PROCESS_LOG_TAIL_LINES:]
    log_tail = sanitize_secrets("\n".join(lines).strip())
    payload: dict[str, Any] = {}
    if log_tail:
        payload["log_tail"] = log_tail
    if stat.st_size > read_size or len(tail) < len(text) or line_truncated:
        payload["log_tail_truncated"] = True
    return payload


def _attach_process_log_tails(payload: dict[str, Any]) -> dict[str, Any]:
    groups = payload.get("groups") if isinstance(payload.get("groups"), list) else []
    current_group_id = str(payload.get("current_group_id") or "")
    for group in groups:
        if not isinstance(group, dict):
            continue
        status = str(group.get("status") or "")
        if status in {"running", "failed"} or (
            current_group_id and str(group.get("group_id") or "") == current_group_id
        ):
            group.update(_process_log_tail(group.get("log_markdown")))
    return payload


def _group_status_from_result(group: dict[str, Any]) -> dict[str, Any]:
    result_path = Path(str(group.get("result_json") or ""))
    result = _read_json_file(result_path) if result_path.exists() else {}
    status = str(result.get("status") or ("pending" if not result_path.exists() else "failed"))
    item = {
        "group_id": group.get("group_id"),
        "status": status,
        "capture_ids": group.get("capture_ids", []),
        "capture_count": len(group.get("capture_ids", [])),
        "session_id": group.get("session_id"),
        "project": group.get("project"),
        "branch": group.get("branch"),
        "input_markdown": group.get("input_markdown"),
        "result_json": group.get("result_json"),
        "log_markdown": group.get("log_markdown"),
    }
    if isinstance(group.get("transcript"), dict):
        item["transcript"] = group["transcript"]
    for key in (
        "created",
        "skipped_duplicates",
        "discard_reason",
        "error",
        "reason",
        "result_state",
        "result_protocol",
    ):
        if key in result:
            item[key] = result[key]
    return item


def _summarize_process_status(payload: dict[str, Any], active: bool) -> dict[str, Any]:
    groups = payload.get("groups") if isinstance(payload.get("groups"), list) else []
    completed_statuses = {"processed", "processed_no_notes"}
    completed = sum(1 for group in groups if group.get("status") in completed_statuses)
    failed = sum(1 for group in groups if group.get("status") == "failed")
    retryable_capture_count = sum(
        len(group.get("capture_ids", [])) for group in groups if group.get("status") == "failed"
    )
    pending = max(0, len(groups) - completed - failed)
    payload["active"] = active
    payload["completed_group_count"] = completed
    payload["failed_group_count"] = failed
    payload["retryable_group_count"] = failed
    payload["retryable_capture_count"] = retryable_capture_count
    payload["pending_group_count"] = pending
    payload.setdefault("group_count", len(groups))
    payload.setdefault("selected_capture_count", sum(len(group.get("capture_ids", [])) for group in groups))
    for key, value in _transcript_status_counts(groups).items():
        payload.setdefault(key, value)
    return payload


def _queue_process_status(run_id: str = "") -> dict[str, Any]:
    lock = _read_processing_lock(_lock_file()) if _lock_file().exists() else None
    target_run_id = run_id or (str(lock.get("run_id")) if lock else "") or _latest_processing_run_id()
    if not target_run_id:
        return {
            "status": "idle",
            "active": False,
            "groups": [],
            "completed_group_count": 0,
            "failed_group_count": 0,
            "pending_group_count": 0,
            **_transcript_status_counts([]),
        }
    run_dir = _processing_root() / target_run_id
    progress_path = run_dir / "progress.json"
    progress = _read_json_file(progress_path) if progress_path.exists() else {}
    lock_active = bool(lock and lock.get("run_id") == target_run_id and not _processing_lock_stale(lock))
    if progress:
        payload = dict(progress)
        payload.setdefault("run_id", target_run_id)
        payload.setdefault("run_dir", str(run_dir))
        if lock_active:
            payload["status"] = "running"
        elif payload.get("status") == "running":
            payload["status"] = "interrupted"
        _attach_process_log_tails(payload)
        return _summarize_process_status(payload, lock_active)
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json_file(manifest_path)
    if not manifest:
        return {
            "status": "unknown",
            "active": lock_active,
            "run_id": target_run_id,
            "run_dir": str(run_dir),
            "groups": [],
            **_transcript_status_counts([]),
        }
    groups = [_group_status_from_result(group) for group in manifest.get("groups", [])]
    status = str(manifest.get("status") or "unknown")
    if lock_active:
        status = "running"
    elif status == "running" and any(group.get("status") == "pending" for group in groups):
        status = "interrupted"
    payload = {
        "run_id": target_run_id,
        "run_dir": str(run_dir),
        "status": status,
        "created_at": manifest.get("created_at"),
        "finalized_at": manifest.get("finalized_at"),
        "selected_capture_count": manifest.get("selected_capture_count"),
        "group_count": manifest.get("group_count", len(groups)),
        "groups": groups,
        "dequeued_capture_ids": manifest.get("dequeued_capture_ids", []),
    }
    _attach_process_log_tails(payload)
    return _summarize_process_status(payload, lock_active)


def _valid_processing_run_id(run_id: str) -> bool:
    value = str(run_id or "").strip()
    return bool(value) and value == Path(value).name and value not in {".", ".."}


def _queue_process_retry(run_id: str = "", group_ids: list[str] | None = None) -> dict[str, Any]:
    if run_id and not _valid_processing_run_id(run_id):
        return {"error": f"invalid processing run id: {run_id}", "reason": "invalid_run_id"}
    return _capture_runtime().plan_retry(_queue_process_status(run_id), run_id, group_ids)


def _reported_note_exists_in_vault(vault: Path, path: str) -> bool:
    if not path or Path(path).is_absolute():
        return False
    vault_resolved = vault.resolve()
    candidate = (vault / path).resolve()
    if not (candidate == vault_resolved or vault_resolved in candidate.parents):
        return False
    return candidate.exists()


def _queue_process_finalize(run_id: str) -> dict[str, Any]:
    if not _valid_processing_run_id(run_id):
        return {"error": f"invalid processing run id: {run_id}", "reason": "invalid_run_id"}
    return _capture_runtime().finalize(run_id)


def _run_json(
    source: str,
    fn,
    *args: Any,
    health_metadata: dict[str, Any] | None = None,
) -> int:
    try:
        payload = fn(*args)
        if health_metadata and isinstance(payload, dict) and payload.get("error"):
            metadata = dict(health_metadata or {})
            _log_bridge_health(source, error=payload.get("error"), reason=payload.get("reason", "error"), **metadata)
        return _emit(payload)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        traceback.print_exc(file=sys.stderr)
        metadata = dict(health_metadata or {})
        _log_bridge_health(source, error=exc, **metadata)
        return _emit({"error": str(exc), "source": source, "reason": "error", "error_type": type(exc).__name__})


def _build_pi_briefing(cwd: str, session_id: str) -> Any:
    """Build Pi briefing without spawning Claude-style deferred work Pi cannot consume."""
    return build_briefing(cwd, session_id, allow_deferred=False, host_id="pi")


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

    session_context = sub.add_parser("session-context", help="Build budgeted session context packet")
    session_context.add_argument("--cwd", default="")
    session_context.add_argument("--prompt", default="")
    session_context.add_argument("--session-id", default="unknown")
    session_context.add_argument("--token-budget", type=int, default=2000)
    session_context.add_argument("--include-status", action=argparse.BooleanOptionalAction, default=True)
    session_context.add_argument("--include-recent", action=argparse.BooleanOptionalAction, default=True)
    session_context.add_argument("--include-recall", action=argparse.BooleanOptionalAction, default=True)
    session_context.add_argument("--include-tool-context-preview", action="store_true")

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
    search.add_argument("--detail-level", default="summary", choices=("brief", "summary", "full"))
    search.add_argument("--include-content", action="store_true")
    search.add_argument("--token-budget", type=int, default=2000)

    query = sub.add_parser("query", help="Run typed metadata filters and aggregations")
    query.add_argument("--project", default="")
    query.add_argument("--note-type", default="")
    query.add_argument("--tag", default="")
    query.add_argument("--source", default="")
    query.add_argument("--certainty-min", type=int, default=None)
    query.add_argument("--certainty-max", type=int, default=None)
    query.add_argument("--date-start", default="")
    query.add_argument("--date-end", default="")
    query.add_argument("--branch", default="")
    query.add_argument("--session-id", default="")
    query.add_argument("--aggregate-by", default="")
    query.add_argument("--recent-sessions-project", default="")
    query.add_argument("--limit", type=int, default=20)

    contradictions = sub.add_parser("contradictions", help="Inspect contradictory or superseded notes")
    contradictions.add_argument("--topic", default="")
    contradictions.add_argument("--limit", type=int, default=20)
    contradictions.add_argument("--min-certainty", type=int, default=2)

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
    capture.add_argument("--note-type", default="session")
    capture.add_argument("--tag", action="append", default=[])
    capture.add_argument("--certainty", default=None)
    capture.add_argument("--branch", default=None)
    capture.add_argument(
        "--lifecycle-metadata",
        default=None,
        help="Optional JSON object with richer Pi lifecycle context and audit metadata",
    )

    triage = sub.add_parser("triage", help="Run Pi SessionEnd-style triage from a persisted session JSONL")
    triage.add_argument("--transcript-path", required=True)
    triage.add_argument("--cwd", default="")
    triage.add_argument("--session-id", default="")
    triage.add_argument("--reason", default="session_shutdown")
    triage.add_argument("--source-event", default="session_shutdown")
    triage.add_argument(
        "--foreground",
        action="store_true",
        help="Run the triage hook synchronously; intended for tests and diagnostics",
    )

    queue = sub.add_parser("queue", help="Inspect or process queued pi captures")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_list = queue_sub.add_parser("list", help="List queued captures")
    queue_list.add_argument("--limit", type=int, default=20)
    queue_list.add_argument("--include-body", action="store_true")
    queue_list.add_argument(
        "--include-generated-summaries",
        action="store_true",
        help="Opt in to model-generated queued-capture summaries backed by a local content-digest cache",
    )
    process_start = queue_sub.add_parser("process-start", help="Create a processing run for selected captures")
    process_start.add_argument("--id", action="append", default=[])
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
    process_status = queue_sub.add_parser("process-status", help="Show queued-capture processing progress")
    process_status.add_argument("--run-id", default="")
    process_finalize = queue_sub.add_parser(
        "process-finalize", help="Finalize a processing run and dequeue validated captures"
    )
    process_finalize.add_argument("--run-id", required=True)
    process_retry = queue_sub.add_parser(
        "process-retry", help="Resolve failed groups from a prior processing run into a retry selection"
    )
    process_retry.add_argument("--run-id", default="")
    process_retry.add_argument(
        "--group-id",
        action="append",
        default=[],
        help="Failed group id to retry (repeatable; default: all failed groups)",
    )
    cleanup = queue_sub.add_parser(
        "cleanup", help="Classify queued captures and archive low-value ones (dry-run by default)"
    )
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite the queue, archiving discarded captures; without this flag nothing is written",
    )
    cleanup.add_argument(
        "--discard-class",
        action="append",
        choices=list(_CLEANUP_DISCARDABLE_CLASSES),
        dest="discard_classes",
        help="Class to discard (repeatable; default: raw_dump, invalid). Manual captures are never discarded.",
    )
    cleanup.add_argument("--samples", type=int, default=3, help="Sample entries shown per class")
    discard = queue_sub.add_parser("discard", help="Archive queued capture(s) without processing")
    discard.add_argument("--id", action="append", default=[], help="Queued capture id to discard")
    discard.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite the queue, archiving the discarded capture(s); without this flag nothing is written",
    )
    discard.add_argument("--reason", default="manual_discard")
    discard.add_argument("--source", default="queue-discard")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "briefing":
        return _run_lifecycle(
            "briefing",
            _build_pi_briefing,
            args.cwd,
            args.session_id,
            health_metadata={"cwd": args.cwd, "session_id": args.session_id},
        )
    if args.command == "recall":
        return _run_lifecycle(
            "recall",
            lambda prompt, cwd, session_id: build_recall(prompt, cwd, session_id, host_id="pi"),
            args.prompt,
            args.cwd,
            args.session_id,
            health_metadata={"cwd": args.cwd, "session_id": args.session_id},
        )
    if args.command == "session-context":
        return _run_json(
            "session-context",
            lambda cwd, prompt, session_id, token_budget, include_status, include_recent, include_recall, include_preview: (
                build_session_context(
                    cwd,
                    prompt,
                    session_id,
                    token_budget,
                    include_status,
                    include_recent,
                    include_recall,
                    include_preview,
                    host_id="pi",
                )
            ),
            args.cwd,
            args.prompt,
            args.session_id,
            args.token_budget,
            args.include_status,
            args.include_recent,
            args.include_recall,
            args.include_tool_context_preview,
        )
    if args.command == "tool-context":
        return _run_lifecycle(
            "tool-context",
            lambda tool_name, file_path, cwd, session_id: build_tool_context(
                tool_name, file_path, cwd, session_id, host_id="pi"
            ),
            args.tool_name,
            args.file_path,
            args.cwd,
            args.session_id,
            health_metadata={"cwd": args.cwd, "session_id": args.session_id},
        )
    if args.command == "status":
        return _run_json("status", _status, args.cwd)
    if args.command == "search":
        return _run_json(
            "search",
            _search,
            args.query,
            args.limit,
            args.cwd,
            args.concrete,
            args.detail_level,
            args.include_content,
            args.token_budget,
        )
    if args.command == "query":
        return _run_json(
            "query",
            _query,
            args.project,
            args.note_type,
            args.tag,
            args.source,
            args.certainty_min,
            args.certainty_max,
            args.date_start,
            args.date_end,
            args.branch,
            args.session_id,
            args.aggregate_by,
            args.recent_sessions_project,
            args.limit,
        )
    if args.command == "contradictions":
        return _run_json("contradictions", _contradictions, args.topic, args.limit, args.min_certainty)
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
            args.note_type,
            args.tag,
            args.certainty,
            args.branch,
            args.lifecycle_metadata,
            health_metadata={"cwd": args.cwd, "session_id": args.session_id},
        )
    if args.command == "triage":
        return _run_json(
            "triage",
            _triage,
            args.transcript_path,
            args.cwd,
            args.session_id,
            args.reason,
            args.source_event,
            not args.foreground,
            health_metadata={"cwd": args.cwd, "session_id": args.session_id or args.transcript_path},
        )
    if args.command == "queue":
        if args.queue_command == "list":
            return _run_json("queue", _queue_list, args.limit, args.include_body, args.include_generated_summaries)
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
        if args.queue_command == "process-status":
            return _run_json("queue", _queue_process_status, args.run_id)
        if args.queue_command == "process-finalize":
            return _run_json("queue", _queue_process_finalize, args.run_id)
        if args.queue_command == "process-retry":
            return _run_json("queue", _queue_process_retry, args.run_id, args.group_id)
        if args.queue_command == "cleanup":
            return _run_json("queue", _queue_cleanup, args.apply, args.discard_classes, args.samples)
        if args.queue_command == "discard":
            return _run_json("queue", _queue_discard, args.id, args.apply)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
