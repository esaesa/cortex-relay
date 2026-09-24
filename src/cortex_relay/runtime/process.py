from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Small injectable subprocess boundary for providers and tests."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return ProcessResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
