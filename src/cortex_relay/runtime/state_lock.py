"""Small cross-process file locks for durable task state."""

from __future__ import annotations

import errno
import os
import time

from pathlib import Path
from typing import BinaryIO, Callable

from cortex_relay.runtime.state_retry import (
    DEFAULT_STATE_RETRY_POLICY,
    StateAccessError,
    StateLockTimeoutError,
    StateRetryPolicy,
    retry_state_operation,
)


# Windows sharing/lock errors observed while another process holds the lock.
_LOCK_CONTENTION_WINERRORS = frozenset({5, 32, 33})
_LOCK_CONTENTION_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "EWOULDBLOCK", None),
    )
    if value is not None
)
_LOCK_POLL_SECONDS = 0.05


def _is_lock_contention(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int) and winerror in _LOCK_CONTENTION_WINERRORS:
        return True
    return getattr(exc, "errno", None) in _LOCK_CONTENTION_ERRNOS


class FileLock:
    """Advisory cross-process lock over a marker file.

    ``acquire(blocking=True, timeout_seconds=None)`` keeps the historical
    behaviour of waiting indefinitely. Passing ``timeout_seconds`` bounds the
    wait and raises :class:`StateLockTimeoutError` instead of blocking forever.
    """

    def __init__(
        self,
        path: Path,
        *,
        retry_policy: StateRetryPolicy = DEFAULT_STATE_RETRY_POLICY,
        sleeper: Callable[[float], None] = time.sleep,
        fault_injector: Callable[[str, Path], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._handle: BinaryIO | None = None
        self._retry_policy = retry_policy
        self._sleeper = sleeper
        self._fault_injector = fault_injector

    def _inject(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point, self.path)

    def _open(self) -> BinaryIO:
        def open_handle() -> BinaryIO:
            self._inject("before_lock_open")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return self.path.open("a+b")

        return retry_state_operation(
            open_handle,
            policy=self._retry_policy,
            operation_name=f"open state lock {self.path.name}",
            sleep=self._sleeper,
        )

    def acquire(
        self,
        blocking: bool = True,
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        if self._handle is not None:
            raise RuntimeError(f"lock already acquired: {self.path}")
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")

        try:
            handle = self._open()
        except StateAccessError as exc:
            # Preserve the original OSError type for callers that only handle
            # filesystem failures; permanent permission errors are never hidden.
            raise exc.cause from exc

        deadline = (
            None if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        attempts = 0
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                attempts += 1
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._handle = handle
                    return True
                except OSError as exc:
                    if not _is_lock_contention(exc):
                        raise
                    if not blocking:
                        return False
                    if deadline is not None and time.monotonic() >= deadline:
                        raise StateLockTimeoutError(
                            f"acquire state lock {self.path.name}",
                            attempts,
                            exc,
                            timeout_seconds=float(timeout_seconds or 0.0),
                        ) from exc
                    self._sleeper(_LOCK_POLL_SECONDS)
        finally:
            if self._handle is not handle:
                handle.close()

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def lock_is_held(path: Path) -> bool:
    """Return whether a different file handle currently owns this lock."""

    if not path.exists():
        return False
    probe = FileLock(path)
    try:
        acquired = probe.acquire(blocking=False)
    except OSError:
        # The lock file exists but cannot be opened/probed; treat it as held so
        # callers keep conservative ownership assumptions.
        return True
    if acquired:
        probe.release()
        return False
    return True
