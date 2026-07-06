"""Tests for the fail-closed capture-queue lock (MEM-123).

memento/queue.py owns the capture-queue flock (``queue_lock``). MEM-129
moved this machinery here verbatim from pi_bridge.py, carrying along a
fail-open bug: on failure to open or acquire the lock, it used to swallow
the OSError and ``yield`` unlocked, letting a concurrent hook and sweeper
race on the same queue file. These tests pin down the fixed, fail-closed
contract: ``queue_lock`` never yields without holding the lock, and raises
``QueueLockUnavailable`` instead - while re-entrancy within a thread (the
mechanism ``migrate_legacy_queue`` relies on to nest inside an
already-held lock) keeps working exactly as before.
"""

from __future__ import annotations

import fcntl
import os
import threading

import pytest

from memento import queue as capture_queue
from memento.queue import QueueLockUnavailable, queue_lock


def test_queue_lock_raises_when_lock_file_cannot_be_opened(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    entered = {"value": False}
    original_open = os.open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith("pi-captures.lock"):
            raise OSError("simulated open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("os.open", failing_open)

    with pytest.raises(QueueLockUnavailable):
        with queue_lock():
            entered["value"] = True

    assert entered["value"] is False, "queue_lock must never yield without holding the lock"


def test_queue_lock_raises_when_flock_cannot_be_acquired(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    entered = {"value": False}
    original_flock = fcntl.flock

    def failing_flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            raise OSError("simulated flock failure")
        return original_flock(fd, operation)

    monkeypatch.setattr("fcntl.flock", failing_flock)

    with pytest.raises(QueueLockUnavailable):
        with queue_lock():
            entered["value"] = True

    assert entered["value"] is False, "queue_lock must never yield without holding the lock"


def test_queue_lock_never_leaks_the_file_descriptor_when_flock_fails(tmp_path, monkeypatch):
    """A failed flock() must still close the fd opened just before it."""
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    closed = {"count": 0}
    original_close = os.close
    original_flock = fcntl.flock

    def counting_close(fd):
        closed["count"] += 1
        return original_close(fd)

    def failing_flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            raise OSError("simulated flock failure")
        return original_flock(fd, operation)

    monkeypatch.setattr("os.close", counting_close)
    monkeypatch.setattr("fcntl.flock", failing_flock)

    with pytest.raises(QueueLockUnavailable):
        with queue_lock():
            pass

    assert closed["count"] == 1


def test_queue_lock_is_reentrant_within_the_same_thread(tmp_path, monkeypatch):
    """Nested queue_lock() calls in the same thread must not reopen/reflock."""
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    open_calls = {"count": 0}
    original_open = os.open

    def counting_open(path, *args, **kwargs):
        if str(path).endswith("pi-captures.lock"):
            open_calls["count"] += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("os.open", counting_open)

    depths = []
    with queue_lock():
        depths.append(capture_queue._QUEUE_LOCK_STATE.depth)
        with queue_lock():
            depths.append(capture_queue._QUEUE_LOCK_STATE.depth)
            with queue_lock():
                depths.append(capture_queue._QUEUE_LOCK_STATE.depth)
            depths.append(capture_queue._QUEUE_LOCK_STATE.depth)
        depths.append(capture_queue._QUEUE_LOCK_STATE.depth)

    assert depths == [1, 2, 3, 2, 1]
    assert open_calls["count"] == 1, "nested queue_lock calls must not reopen/reacquire the flock"
    assert getattr(capture_queue._QUEUE_LOCK_STATE, "depth", 0) == 0


def test_queue_lock_reentrant_body_failure_still_restores_depth(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(RuntimeError):
        with queue_lock():
            with queue_lock():
                raise RuntimeError("boom")
    assert getattr(capture_queue._QUEUE_LOCK_STATE, "depth", 0) == 0


def test_queue_lock_blocks_a_second_thread_until_the_first_releases(tmp_path, monkeypatch):
    """Real flock contention: the second acquirer blocks, it does not fail closed spuriously."""
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    first_holds = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()
    errors: list[BaseException] = []

    def hold_first():
        try:
            with queue_lock():
                first_holds.set()
                release_first.wait(5)
        except BaseException as exc:  # pragma: no cover - surfaced via assertions
            errors.append(exc)

    def acquire_second():
        try:
            with queue_lock():
                second_acquired.set()
        except BaseException as exc:  # pragma: no cover - surfaced via assertions
            errors.append(exc)

    first_thread = threading.Thread(target=hold_first)
    first_thread.start()
    assert first_holds.wait(5)

    second_thread = threading.Thread(target=acquire_second)
    second_thread.start()
    assert not second_acquired.wait(0.3), "second thread must block while the first holds the lock"

    release_first.set()
    first_thread.join(5)
    second_thread.join(5)

    assert not errors
    assert second_acquired.is_set()
