from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


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
        on_stdout_line: Callable[[str], None] | None = None,
        on_stderr_line: Callable[[str], None] | None = None,
        on_heartbeat: Callable[[int, bool], None] | None = None,
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
        self._heartbeat(on_heartbeat, process.pid, True)

        if on_stdout_line is not None or on_stderr_line is not None:
            return self._stream(
                process, original_argv, started, timeout_seconds, cancel_event,
                on_stdout_line, on_stderr_line, on_heartbeat,
            )

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

    def _stream(
        self,
        process: subprocess.Popen[str],
        argv: list[str],
        started: float,
        timeout_seconds: int,
        cancel_event: threading.Event | None,
        on_stdout_line: Callable[[str], None] | None,
        on_stderr_line: Callable[[str], None] | None,
        on_heartbeat: Callable[[int, bool], None] | None,
    ) -> ProcessResult:
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        output: dict[str, list[str]] = {"stdout": [], "stderr": []}

        def read_pipe(name: str) -> None:
            pipe = process.stdout if name == "stdout" else process.stderr
            assert pipe is not None
            try:
                for line in pipe:
                    events.put((name, line))
            finally:
                events.put((name, None))

        readers = [
            threading.Thread(target=read_pipe, args=(name,), daemon=True)
            for name in ("stdout", "stderr")
        ]
        for reader in readers:
            reader.start()

        finished = 0
        exited_at: float | None = None
        last_heartbeat = time.monotonic()
        try:
            while finished < 2:
                if time.monotonic() - last_heartbeat >= 5:
                    self._heartbeat(on_heartbeat, process.pid, process.poll() is None)
                    last_heartbeat = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    raise ProcessCancelledError("provider process cancelled")
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout_seconds)
                try:
                    name, line = events.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    if process.poll() is not None:
                        exited_at = exited_at or time.monotonic()
                        # A provider may leave a descendant holding a pipe open.
                        if time.monotonic() - exited_at >= 0.5:
                            break
                    continue
                if line is None:
                    finished += 1
                    continue
                output[name].append(line)
                callback = on_stdout_line if name == "stdout" else on_stderr_line
                if callback is not None:
                    try:
                        callback(line.rstrip("\r\n"))
                    except Exception:
                        # Observability must not fail provider execution.
                        pass
            while process.poll() is None:
                if time.monotonic() - last_heartbeat >= 5:
                    self._heartbeat(on_heartbeat, process.pid, True)
                    last_heartbeat = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    raise ProcessCancelledError("provider process cancelled")
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout_seconds)
                try:
                    process.wait(timeout=min(0.2, remaining))
                except subprocess.TimeoutExpired:
                    continue
            return ProcessResult(
                argv=tuple(argv), returncode=process.returncode,
                stdout="".join(output["stdout"]), stderr="".join(output["stderr"]),
            )
        except (ProcessCancelledError, subprocess.TimeoutExpired):
            self._terminate(process, communicating=False)
            raise
        finally:
            self._heartbeat(on_heartbeat, process.pid, False)
            for reader in readers:
                reader.join(timeout=0.1)

    @staticmethod
    def _heartbeat(callback: Callable[[int, bool], None] | None, pid: int, alive: bool) -> None:
        if callback is not None:
            try:
                callback(pid, alive)
            except Exception:
                pass

    @staticmethod
    def _terminate(process: subprocess.Popen[str], *, communicating: bool = True) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            if communicating:
                process.communicate(timeout=5)
            else:
                process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            if communicating:
                process.communicate()
            else:
                process.wait()
