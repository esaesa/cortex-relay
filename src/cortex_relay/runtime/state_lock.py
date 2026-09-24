"""Small cross-process file locks for durable task state."""

from __future__ import annotations

import errno
import os
import time

from pathlib import Path
from typing import BinaryIO


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: BinaryIO | None = None

    def acquire(self, blocking: bool = True) -> bool:
        if self._handle is not None:
            raise RuntimeError(f"lock already acquired: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
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
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if not blocking:
                        return False
                    time.sleep(0.05)
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
    if probe.acquire(blocking=False):
        probe.release()
        return False
    return True
