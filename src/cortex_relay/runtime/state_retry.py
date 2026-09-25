"""Bounded retries for transient state-store failures.

CortexRelay persists workflow state in JSON files protected by cross-process
file locks and agent state in SQLite/WAL. Both backends can surface short-lived
OS contention (notably Windows ``PermissionError``/sharing violations) when
several workers touch the same state root concurrently.

Only known transient conditions are retried. Permanent failures such as
``sqlite3.IntegrityError``, malformed records, invalid schemas, or validation
errors propagate immediately.
"""

from __future__ import annotations

import errno
import sqlite3
import time

from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class StateRetryPolicy:
    """Deterministic exponential backoff configuration for state access."""

    max_attempts: int = 6
    initial_delay_seconds: float = 0.025
    max_delay_seconds: float = 0.4
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be non-negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must be >= initial_delay_seconds")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")

    def delay_for(self, attempt: int) -> float:
        """Return the deterministic backoff delay before retry ``attempt`` (1-based)."""
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        delay = self.initial_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay_seconds)


DEFAULT_STATE_RETRY_POLICY = StateRetryPolicy()


class StateAccessError(RuntimeError, OSError):
    """A state operation failed after exhausting its bounded retry budget.

    It inherits from :class:`OSError` as well as :class:`RuntimeError` so that
    pre-existing ``except OSError`` handlers around state operations keep
    behaving as they did before bounded retries were introduced.
    """

    def __init__(self, operation_name: str, attempts: int, cause: BaseException) -> None:
        super().__init__(
            f"{operation_name} failed after {attempts} attempt(s): "
            f"{type(cause).__name__}: {cause}"
        )
        self.operation_name = operation_name
        self.attempts = attempts
        self.cause = cause


class StateLockTimeoutError(StateAccessError):
    """A cross-process state lock could not be acquired within its deadline."""

    def __init__(
        self,
        operation_name: str,
        attempts: int,
        cause: BaseException,
        *,
        timeout_seconds: float,
    ) -> None:
        super().__init__(operation_name, attempts, cause)
        self.timeout_seconds = timeout_seconds


# errno values that only appear while another process holds a resource.
_TRANSIENT_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "ETXTBSY", None),
    )
    if value is not None
)

# Windows: 5 = ERROR_ACCESS_DENIED, 32 = ERROR_SHARING_VIOLATION,
# 33 = ERROR_LOCK_VIOLATION.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})

_SQLITE_TRANSIENT_CODES = frozenset(
    value
    for value in (
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
        getattr(sqlite3, "SQLITE_PROTOCOL", 15),
    )
    if value is not None
)

_SQLITE_TRANSIENT_MESSAGES = (
    "database is locked",
    "database is busy",
    "database table is locked",
    "database schema is locked",
    "disk i/o error",
    "unable to open database file",
)

# Conditions that must never be retried, even when they are raised as
# OperationalError-like wrappers.
_PERMANENT_SQLITE_MESSAGES = (
    "database disk image is malformed",
    "not a database",
    "no such table",
    "no such column",
    "malformed",
    "unsupported file format",
)


def _os_errno(exc: BaseException) -> int | None:
    value = getattr(exc, "errno", None)
    if isinstance(value, int):
        return value
    return None


def _windows_error_code(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    return None


def is_transient_state_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is a known transient state-store condition."""

    if isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError)):
        return False

    # Integrity failures are logical rejections, never retryable.
    if isinstance(exc, sqlite3.IntegrityError):
        return False

    if isinstance(exc, sqlite3.Error):
        error_code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(error_code, int) and (error_code & 0xFF) in {
            getattr(sqlite3, "SQLITE_CORRUPT", 11),
            getattr(sqlite3, "SQLITE_NOTADB", 26),
            getattr(sqlite3, "SQLITE_CONSTRAINT", 19),
            getattr(sqlite3, "SQLITE_MISMATCH", 20),
            getattr(sqlite3, "SQLITE_SCHEMA", 17),
            getattr(sqlite3, "SQLITE_READONLY", 8),
            getattr(sqlite3, "SQLITE_AUTH", 23),
        }:
            return False
        if isinstance(error_code, int) and (error_code & 0xFF) in _SQLITE_TRANSIENT_CODES:
            return True
        message = str(exc).lower()
        if any(marker in message for marker in _PERMANENT_SQLITE_MESSAGES):
            return False
        if any(marker in message for marker in _SQLITE_TRANSIENT_MESSAGES):
            return True
        if isinstance(error_code, int) and (error_code & 0xFF) in {
            getattr(sqlite3, "SQLITE_IOERR", 10),
            getattr(sqlite3, "SQLITE_CANTOPEN", 14),
        }:
            # OS-level open/access contention (e.g. a sharing violation while
            # another process is copying or replacing the database files).
            return True
        return False

    if isinstance(exc, (PermissionError, OSError)):
        winerror = _windows_error_code(exc)
        if winerror in _TRANSIENT_WINERRORS:
            return True
        code = _os_errno(exc)
        if code in _TRANSIENT_ERRNOS:
            return True
        # Filesystem-level contention without a specific errno (Windows reports
        # ERROR_ACCESS_DENIED through several code paths).
        return isinstance(exc, PermissionError)

    return False


def retry_state_operation(
    operation: Callable[[], T],
    *,
    policy: StateRetryPolicy = DEFAULT_STATE_RETRY_POLICY,
    operation_name: str,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation`` with deterministic bounded exponential backoff.

    ``sleep`` is injectable so tests can assert the backoff schedule without
    waiting on real time. No random jitter is used.
    """

    attempt = 0
    delay = policy.delay_for(1)
    while True:
        attempt += 1
        try:
            return operation()
        except Exception as exc:
            transient = is_transient_state_error(exc)
            if not transient:
                raise
            if attempt >= policy.max_attempts:
                raise StateAccessError(operation_name, attempt, exc) from exc
            if delay > 0:
                sleep(delay)
            delay = min(delay * policy.multiplier, policy.max_delay_seconds)
