from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class ProcessCancelledError(RuntimeError):
    """Raised when a delegated provider process is cancelled."""


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Small injectable subprocess boundary for providers and tests.

    A cancellation event is optional so existing CLI/MCP execution remains
    synchronous while A2A can terminate the underlying provider process when a
    remote task is cancelled.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProcessResult:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        started = time.monotonic()

        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate(process)
                raise ProcessCancelledError("provider process cancelled")

            elapsed = time.monotonic() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                self._terminate(process)
                raise subprocess.TimeoutExpired(list(argv), timeout_seconds)

            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                return ProcessResult(
                    argv=tuple(argv),
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
