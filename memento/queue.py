"""Capture-queue storage for queued Memento captures.

Single owner of the capture-queue boundary: Pi state-home resolution
(MEMENTO_PI_STATE_HOME, then XDG_STATE_HOME, then ~/.local/state),
queue-file and legacy-queue-file path resolution, the JSONL
read/write/lock/migration machinery, and the ``PiQueueStore``
implementation of the ``memento.capture_runtime.QueueStore`` protocol.

Consumers (pi_bridge, lifecycle, health) delegate here instead of
carrying private copies of the resolution logic.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from memento.config import get_vault


def state_root() -> Path:
    """Resolve the Pi state home used for queue and processing state."""
    raw = os.environ.get("MEMENTO_PI_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "memento" / "pi"


def queue_path_source() -> str:
    """Name the environment source that resolved the queue path."""
    if os.environ.get("MEMENTO_PI_STATE_HOME"):
        return "memento_pi_state_home"
    if os.environ.get("XDG_STATE_HOME"):
        return "xdg_state_home"
    return "default_xdg_state"


def resolved_queue_file() -> Path:
    """Return the current queue file path without triggering legacy migration."""
    return state_root() / "queue" / "pi-captures.jsonl"


def queue_file(vault: Path | None = None) -> Path:
    """Return the queue file path, migrating any legacy vault queue first."""
    migrate_legacy_queue(vault)
    return resolved_queue_file()


def legacy_queue_file(vault: Path | None = None) -> Path:
    """Return the legacy in-vault queue file path."""
    return (vault or get_vault()) / "queue" / "pi-captures.jsonl"


def read_queue_file(path: Path) -> list[dict[str, Any]]:
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
    return state_root() / "queue" / "pi-captures.lock"


@contextlib.contextmanager
def queue_lock(vault: Path | None = None) -> Generator[None, None, None]:
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


def atomic_write_text(path: Path, content: str) -> None:
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


def write_queue_file(captures: list[dict[str, Any]], path: Path) -> None:
    """Atomically write the queue file using tmp+fsync+rename."""
    atomic_write_text(path, "".join(json.dumps(capture, ensure_ascii=False) + "\n" for capture in captures))


def write_queue(captures: list[dict[str, Any]], vault: Path | None = None) -> None:
    write_queue_file(captures, queue_file(vault))


def migrate_legacy_queue(vault: Path | None = None) -> dict[str, Any]:
    legacy = legacy_queue_file(vault)
    if not legacy.exists():
        return {"migrated": False, "reason": "no_legacy_queue"}
    old = read_queue_file(legacy)
    if not old:
        legacy.unlink()
        return {"migrated": True, "migrated_count": 0, "deleted_legacy_queue": True}
    new_path = resolved_queue_file()
    with queue_lock(vault):
        current = read_queue_file(new_path)
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
        write_queue_file(combined, new_path)
        reread_ids = {capture.get("id") for capture in read_queue_file(new_path)}
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


def load_queue(vault: Path | None = None) -> list[dict[str, Any]]:
    return read_queue_file(queue_file(vault))


def queue_count(vault: Path | None = None) -> int:
    return len(load_queue(vault))


class PiQueueStore:
    """File-backed ``QueueStore`` over the Pi capture queue."""

    def load(self, vault: Path) -> list[dict[str, Any]]:
        return load_queue(vault)

    def write(self, captures: list[dict[str, Any]], vault: Path) -> None:
        write_queue(captures, vault)

    def path(self, vault: Path) -> Path:
        return queue_file(vault)

    def lock(self):
        return queue_lock()
