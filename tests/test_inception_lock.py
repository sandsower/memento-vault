"""Tests for Inception lock management functions."""

import os
import time

import pytest

from memento_utils import acquire_inception_lock, release_inception_lock


class TestAcquireInceptionLock:
    def test_acquire_clean(self, tmp_path):
        """Acquire on clean state returns True and creates lock file with PID."""
        lock = tmp_path / "inception.lock"
        assert acquire_inception_lock(lock_path=str(lock)) is True
        assert lock.exists()
        assert lock.read_text().strip() == str(os.getpid())

    def test_acquire_held_by_self(self, tmp_path):
        """Acquire when lock exists with current PID and is fresh returns False."""
        lock = tmp_path / "inception.lock"
        lock.write_text(str(os.getpid()))
        # Touch it so mtime is fresh (< 10 min)
        assert acquire_inception_lock(lock_path=str(lock)) is False

    def test_acquire_stale(self, tmp_path):
        """Lock older than 10 minutes is broken and acquire returns True."""
        lock = tmp_path / "inception.lock"
        lock.write_text(str(os.getpid()))
        # Set mtime to 15 minutes ago
        stale_time = time.time() - 900
        os.utime(str(lock), (stale_time, stale_time))
        assert acquire_inception_lock(lock_path=str(lock)) is True

    def test_acquire_dead_pid(self, tmp_path):
        """Lock with dead PID and fresh mtime is broken, acquire returns True."""
        lock = tmp_path / "inception.lock"
        lock.write_text("99999999")  # PID that almost certainly doesn't exist
        assert acquire_inception_lock(lock_path=str(lock)) is True


class TestReleaseInceptionLock:
    def test_release_removes(self, tmp_path):
        """Acquire then release removes the lock file."""
        lock = tmp_path / "inception.lock"
        acquire_inception_lock(lock_path=str(lock))
        assert lock.exists()
        release_inception_lock(lock_path=str(lock))
        assert not lock.exists()

    def test_release_missing_noop(self, tmp_path):
        """Release when no lock file raises no error."""
        lock = tmp_path / "inception.lock"
        assert not lock.exists()
        release_inception_lock(lock_path=str(lock))  # should not raise
