from __future__ import annotations

import errno
import tempfile
import time
import unittest

from pathlib import Path

from cortex_relay.runtime.state_lock import FileLock, lock_is_held
from cortex_relay.runtime.state_retry import StateLockTimeoutError, StateRetryPolicy


class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

    def test_acquire_release_and_contention_detection(self) -> None:
        path = self.directory / "task.lock"
        first = FileLock(path)
        second = FileLock(path)
        self.assertTrue(first.acquire())
        self.assertTrue(lock_is_held(path))
        self.assertFalse(second.acquire(blocking=False))
        with self.assertRaises(StateLockTimeoutError):
            second.acquire(timeout_seconds=0.2)
        first.release()
        self.assertFalse(lock_is_held(path))
        self.assertTrue(second.acquire(timeout_seconds=1.0))
        second.release()

    def test_acquire_timeout_is_bounded(self) -> None:
        path = self.directory / "bounded.lock"
        holder = FileLock(path)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)

        waiter = FileLock(path, sleeper=lambda _: None)
        started = time.monotonic()
        with self.assertRaises(StateLockTimeoutError) as raised:
            waiter.acquire(timeout_seconds=0.1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertEqual(raised.exception.timeout_seconds, 0.1)
        self.assertGreaterEqual(raised.exception.attempts, 1)
        self.assertIsInstance(raised.exception.cause, OSError)

    def test_reacquire_and_context_manager(self) -> None:
        path = self.directory / "context.lock"
        lock = FileLock(path)
        lock.acquire()
        with self.assertRaises(RuntimeError):
            lock.acquire()
        lock.release()
        with FileLock(path) as held:
            self.assertTrue(lock_is_held(path))
            self.assertEqual(held.path, path)
        self.assertFalse(lock_is_held(path))

    def test_transient_lock_open_failure_is_retried(self) -> None:
        path = self.directory / "retry.lock"
        attempts = {"count": 0}

        def inject(point: str, target: Path) -> None:
            if point == "before_lock_open":
                attempts["count"] += 1
                if attempts["count"] <= 2:
                    raise PermissionError(errno.EACCES, "sharing violation")

        lock = FileLock(
            path,
            retry_policy=StateRetryPolicy(max_attempts=5, initial_delay_seconds=0.0),
            sleeper=lambda _: None,
            fault_injector=inject,
        )
        self.assertTrue(lock.acquire(timeout_seconds=1.0))
        lock.release()
        self.assertEqual(attempts["count"], 3)

    def test_permanent_lock_open_failure_stays_bounded(self) -> None:
        def inject(point: str, target: Path) -> None:
            raise PermissionError(errno.EACCES, "permanent")

        lock = FileLock(
            self.directory / "blocked.lock",
            retry_policy=StateRetryPolicy(max_attempts=3, initial_delay_seconds=0.0),
            sleeper=lambda _: None,
            fault_injector=inject,
        )
        with self.assertRaises(PermissionError):
            lock.acquire()
        self.assertFalse((self.directory / "blocked.lock").exists())

    def test_negative_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FileLock(self.directory / "negative.lock").acquire(timeout_seconds=-1.0)

    def test_missing_lock_file_is_not_held(self) -> None:
        self.assertFalse(lock_is_held(self.directory / "absent.lock"))


if __name__ == "__main__":
    unittest.main()
