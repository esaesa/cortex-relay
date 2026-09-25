from __future__ import annotations

import errno
import sqlite3
import unittest

from cortex_relay.runtime.state_retry import (
    DEFAULT_STATE_RETRY_POLICY,
    StateAccessError,
    StateRetryPolicy,
    is_transient_state_error,
    retry_state_operation,
)


class StateRetryPolicyTests(unittest.TestCase):
    def test_backoff_is_deterministic_and_bounded(self) -> None:
        policy = StateRetryPolicy(
            max_attempts=6,
            initial_delay_seconds=0.1,
            max_delay_seconds=0.4,
            multiplier=2.0,
        )
        self.assertEqual(
            [policy.delay_for(attempt) for attempt in range(1, 7)],
            [0.1, 0.2, 0.4, 0.4, 0.4, 0.4],
        )

    def test_invalid_policies_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StateRetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            StateRetryPolicy(initial_delay_seconds=-1.0)
        with self.assertRaises(ValueError):
            StateRetryPolicy(initial_delay_seconds=1.0, max_delay_seconds=0.5)
        with self.assertRaises(ValueError):
            StateRetryPolicy(multiplier=0.5)
        with self.assertRaises(ValueError):
            DEFAULT_STATE_RETRY_POLICY.delay_for(0)


class TransientClassificationTests(unittest.TestCase):
    @staticmethod
    def _windows_os_error(message: str, winerror: int) -> OSError:
        # On Windows the 4th OSError argument populates ``.winerror``; on
        # POSIX it is only kept in ``.args``, so attach it explicitly to
        # exercise the Windows classification path on every platform.
        exc = OSError(0, message, None, winerror)
        if getattr(exc, "winerror", None) is None:
            exc.winerror = winerror
        return exc

    def test_transient_windows_and_filesystem_conditions(self) -> None:
        self.assertTrue(is_transient_state_error(PermissionError(errno.EACCES, "denied")))
        self.assertTrue(is_transient_state_error(OSError(errno.EBUSY, "busy")))
        self.assertTrue(is_transient_state_error(self._windows_os_error("sharing", 32)))
        self.assertTrue(is_transient_state_error(self._windows_os_error("lock", 33)))
        self.assertTrue(
            is_transient_state_error(sqlite3.OperationalError("database is locked"))
        )
        self.assertTrue(
            is_transient_state_error(sqlite3.OperationalError("disk I/O error"))
        )

    def test_permanent_errors_are_never_retried(self) -> None:
        self.assertFalse(is_transient_state_error(sqlite3.IntegrityError("UNIQUE")))
        self.assertFalse(is_transient_state_error(sqlite3.IntegrityError("FOREIGN KEY")))
        self.assertFalse(is_transient_state_error(FileNotFoundError(errno.ENOENT, "gone")))
        self.assertFalse(is_transient_state_error(ValueError("bad json")))
        self.assertFalse(is_transient_state_error(KeyError("task_id")))
        self.assertFalse(
            is_transient_state_error(
                sqlite3.OperationalError("database disk image is malformed")
            )
        )
        self.assertFalse(is_transient_state_error(KeyboardInterrupt()))


class RetryStateOperationTests(unittest.TestCase):
    def test_success_after_transient_failures_uses_bounded_backoff(self) -> None:
        slept: list[float] = []
        attempts = {"count": 0}

        def operation() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise PermissionError(errno.EACCES, "sharing violation")
            return "ok"

        result = retry_state_operation(
            operation,
            policy=StateRetryPolicy(
                max_attempts=5, initial_delay_seconds=0.1, multiplier=2.0,
                max_delay_seconds=1.0,
            ),
            operation_name="write record",
            sleep=slept.append,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(slept, [0.1, 0.2])

    def test_exhausted_retries_raise_state_access_error_with_cause(self) -> None:
        slept: list[float] = []
        original = PermissionError(errno.EACCES, "denied")

        def operation() -> None:
            raise original

        with self.assertRaises(StateAccessError) as raised:
            retry_state_operation(
                operation,
                policy=StateRetryPolicy(
                    max_attempts=3, initial_delay_seconds=0.01, multiplier=2.0,
                    max_delay_seconds=1.0,
                ),
                operation_name="publish task",
                sleep=slept.append,
            )
        error = raised.exception
        self.assertEqual(error.operation_name, "publish task")
        self.assertEqual(error.attempts, 3)
        self.assertIs(error.cause, original)
        self.assertIs(error.__cause__, original)
        self.assertEqual(slept, [0.01, 0.02])
        self.assertIn("publish task", str(error))

    def test_permanent_failures_propagate_untouched(self) -> None:
        slept: list[float] = []
        original = sqlite3.IntegrityError("UNIQUE constraint failed")

        def operation() -> None:
            raise original

        with self.assertRaises(sqlite3.IntegrityError) as raised:
            retry_state_operation(
                operation,
                policy=StateRetryPolicy(max_attempts=4),
                operation_name="insert",
                sleep=slept.append,
            )
        self.assertIs(raised.exception, original)
        self.assertEqual(slept, [])

    def test_retry_budget_is_never_exceeded(self) -> None:
        calls = {"count": 0}

        def operation() -> None:
            calls["count"] += 1
            raise PermissionError(errno.EACCES, "denied")

        policy = StateRetryPolicy(max_attempts=7, initial_delay_seconds=0.0)
        with self.assertRaises(StateAccessError):
            retry_state_operation(
                operation, policy=policy, operation_name="op", sleep=lambda _: None
            )
        self.assertEqual(calls["count"], policy.max_attempts)


if __name__ == "__main__":
    unittest.main()
