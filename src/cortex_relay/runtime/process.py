from __future__ import annotations

import os
import shutil
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


def prepare_process_argv(argv: Sequence[str]) -> list[str]:
    """Resolve Windows batch shims without shell interpolation.

    npm installs commands such as Codex/OpenCode as .CMD plus sibling .PS1
    shims. Python's shell=False/CreateProcess path can detect the .CMD through
    shutil.which() but cannot execute that batch file directly. When running on
    Windows, route such shims through their sibling PowerShell launcher while
    preserving every CLI argument as a separate process argument.

    We intentionally do not fall back to cmd.exe /c with a joined command
    string because delegated objectives/prompts may contain shell metacharacters.
    """

    command = list(argv)
    if not command or os.name != "nt":
        return command

    executable = _resolve_windows_executable(command[0])
    suffix = executable.suffix.lower()
    if suffix not in {".cmd", ".bat"}:
        if executable != Path(command[0]):
            command[0] = str(executable)
        return command

    powershell_shim = executable.with_suffix(".ps1")
    if not powershell_shim.is_file():
        raise FileNotFoundError(
            "Windows batch shim cannot be launched safely because no sibling "
            f"PowerShell shim exists: {executable}"
        )

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        raise FileNotFoundError(
            "PowerShell is required to launch Windows .CMD/.BAT provider shims "
            f"safely: {executable}"
        )

    return [
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(powershell_shim),
        *command[1:],
    ]


def _resolve_windows_executable(value: str) -> Path:
    direct = Path(value).expanduser()
    if direct.is_file():
        return direct.resolve()

    resolved = shutil.which(value)
    if resolved:
        return Path(resolved).resolve()
    return direct


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
        original_argv = list(argv)
        process_argv = prepare_process_argv(original_argv)
        process = subprocess.Popen(
            process_argv,
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
                raise subprocess.TimeoutExpired(original_argv, timeout_seconds)

            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                return ProcessResult(
                    argv=tuple(original_argv),
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
