from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from cortex_relay.core.models import Evidence, TaskResult, TaskSpec
from cortex_relay.runtime.process import ProcessCancelledError, ProcessRunner
from cortex_relay.runtime.progress import runner_progress_kwargs

from .base import ProviderAdapter, ProviderCapabilities
from .codex_app_server import CodexAppServerClient, CodexAppServerError
from .codex_models import compatibility_error, known_model_ids
from .result_schema import RESULT_SCHEMA


CODEX_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class CodexAdapter(ProviderAdapter):
    name = "codex"

    def __init__(self, *, binary: str = "codex", runner: ProcessRunner | None = None) -> None:
        self.binary = binary
        self.runner = runner or ProcessRunner()

    def capabilities(self) -> ProviderCapabilities:
        path = shutil.which(self.binary)
        return ProviderCapabilities(
            name=self.name,
            binary=self.binary,
            available=path is not None,
            structured_output=True,
            model_selection=True,
            reasoning_control=True,
            read_only_policy=True,
            workspace_write=True,
            detail=path or f"{self.binary} was not found on PATH",
            known_models=known_model_ids(),
            reasoning_levels=CODEX_REASONING_LEVELS,
            session_mode="native",
            persistent_sessions=True,
            streaming_events=True,
            native_subagents=True,
            child_messaging=False,
        )

    def command_for(
        self,
        task: TaskSpec,
        *,
        schema_path: Path,
        last_message_path: Path,
    ) -> list[str]:
        sandbox = "read-only" if task.access == "read_only" else "workspace-write"
        argv = [
            self.binary,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(last_message_path),
        ]
        if task.model:
            argv.extend(["--model", task.model])
        if task.reasoning:
            argv.extend(["--config", f'model_reasoning_effort="{task.reasoning}"'])
        if bool(task.metadata.get("skip_git_repo_check", False)):
            argv.append("--skip-git-repo-check")
        if bool(task.metadata.get("ephemeral", False)):
            argv.append("--ephemeral")
        argv.append(self._prompt(task))
        return argv

    def execute_session(self, task: TaskSpec) -> TaskResult:
        return self._execute_app_server(task, provider_session_id=None)

    def continue_session(self, task: TaskSpec, provider_session_id: str) -> TaskResult:
        return self._execute_app_server(task, provider_session_id=provider_session_id)

    def _execute_app_server(
        self,
        task: TaskSpec,
        *,
        provider_session_id: str | None,
    ) -> TaskResult:
        capabilities = self.capabilities()
        if not capabilities.available:
            return TaskResult(
                status="unavailable",
                provider=self.name,
                model=task.model,
                summary="Codex CLI is unavailable.",
                error=capabilities.detail,
            )
        if not task.workspace.exists():
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Delegated workspace does not exist.",
                error=str(task.workspace),
            )
        incompatibility = compatibility_error(task.model, task.reasoning)
        if incompatibility:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Codex model/reasoning configuration is incompatible.",
                error=incompatibility,
            )

        cancel_event = task.metadata.get("_cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            return TaskResult(
                status="cancelled",
                provider=self.name,
                model=task.model,
                summary="Codex task was cancelled before the session turn started.",
                error="provider session cancelled",
            )

        before = self._git_snapshot(task.workspace) if task.access == "read_only" else None
        started = time.monotonic()
        progress = task.metadata.get("_progress_line")

        def on_event(line: str) -> None:
            if callable(progress):
                progress(self.name, line, "stdout")

        client = CodexAppServerClient(
            binary=self.binary,
            cwd=task.workspace,
            timeout_seconds=task.timeout_seconds + 15,
            on_event=on_event,
        )
        try:
            turn = client.run_turn(
                prompt=self._prompt(task),
                thread_id=provider_session_id,
                model=task.model,
                reasoning=task.reasoning,
                sandbox="read-only" if task.access == "read_only" else "workspace-write",
                output_schema=RESULT_SCHEMA,
            )
        except subprocess.TimeoutExpired:
            return TaskResult(
                status="timeout",
                provider=self.name,
                model=task.model,
                summary="Codex session turn timed out.",
                error=f"timeout after {task.timeout_seconds} seconds",
                conversation_id=provider_session_id,
                duration_seconds=time.monotonic() - started,
                metadata={"transport": "app-server"},
            )
        except CodexAppServerError as exc:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Codex app-server session failed.",
                error=str(exc),
                conversation_id=provider_session_id,
                duration_seconds=time.monotonic() - started,
                metadata={"transport": "app-server"},
            )

        payload = _parse_json_object(turn.final_text)
        if payload is None:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Codex app-server did not produce the required structured final result.",
                error="final assistant message was not a JSON object matching the result contract",
                conversation_id=turn.thread_id,
                duration_seconds=time.monotonic() - started,
                usage=turn.usage,
                metadata={
                    "transport": "app-server",
                    "turn_id": turn.turn_id,
                    "event_count": len(turn.events),
                    "stderr": turn.stderr,
                },
            )

        after = self._git_snapshot(task.workspace) if before is not None else None
        if before is not None and after is not None and before != after:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Read-only Codex delegation changed the workspace.",
                error="Provider violated the read_only contract; inspect git status before continuing.",
                changed_files=tuple(sorted(after.symmetric_difference(before))),
                conversation_id=turn.thread_id,
                duration_seconds=time.monotonic() - started,
                usage=turn.usage,
                metadata={"transport": "app-server", "turn_id": turn.turn_id},
            )

        return TaskResult(
            status="success",
            provider=self.name,
            model=task.model,
            summary=str(payload.get("summary", "")).strip(),
            final_text=str(payload.get("final_text", "")).strip(),
            evidence=tuple(
                Evidence.from_dict(item)
                for item in payload.get("evidence", [])
                if isinstance(item, dict)
            ),
            changed_files=_string_tuple(payload.get("changed_files")),
            commands=_string_tuple(payload.get("commands")),
            tests=_string_tuple(payload.get("tests")),
            risks=_string_tuple(payload.get("risks")),
            conversation_id=turn.thread_id,
            duration_seconds=time.monotonic() - started,
            usage=turn.usage,
            metadata={
                "transport": "app-server",
                "turn_id": turn.turn_id,
                "event_count": len(turn.events),
                "stderr": turn.stderr,
            },
        )

    def execute(self, task: TaskSpec) -> TaskResult:
        capabilities = self.capabilities()
        if not capabilities.available:
            return TaskResult(
                status="unavailable",
                provider=self.name,
                model=task.model,
                summary="Codex CLI is unavailable.",
                error=capabilities.detail,
            )

        if not task.workspace.exists():
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Delegated workspace does not exist.",
                error=str(task.workspace),
            )

        incompatibility = compatibility_error(task.model, task.reasoning)
        if incompatibility:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Codex model/reasoning configuration is incompatible.",
                error=incompatibility,
            )

        before = self._git_snapshot(task.workspace) if task.access == "read_only" else None
        started = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="cortex-relay-codex-") as tmp:
            temp_dir = Path(tmp)
            schema_path = temp_dir / "result-schema.json"
            last_message_path = temp_dir / "last-message.json"
            schema_path.write_text(
                json.dumps(RESULT_SCHEMA, separators=(",", ":")),
                encoding="utf-8",
            )

            try:
                result = self.runner.run(
                    self.command_for(
                        task,
                        schema_path=schema_path,
                        last_message_path=last_message_path,
                    ),
                    cwd=task.workspace,
                    timeout_seconds=task.timeout_seconds + 15,
                    cancel_event=task.metadata.get("_cancel_event"),
                    **runner_progress_kwargs(task, self.name),
                )
            except ProcessCancelledError:
                return TaskResult(
                    status="cancelled",
                    provider=self.name,
                    model=task.model,
                    summary="Codex task was cancelled.",
                    error="provider process cancelled",
                    duration_seconds=time.monotonic() - started,
                )
            except subprocess.TimeoutExpired:
                return TaskResult(
                    status="timeout",
                    provider=self.name,
                    model=task.model,
                    summary="Codex task timed out.",
                    error=f"timeout after {task.timeout_seconds} seconds",
                    duration_seconds=time.monotonic() - started,
                )

            events = self._parse_events(result.stdout)
            thread_id = self._thread_id(events)
            usage = self._usage(events)
            event_error = self._event_error(events)

            if result.returncode != 0:
                error = (event_error or result.stderr or "Codex execution failed").strip()
                return TaskResult(
                    status="error",
                    provider=self.name,
                    model=task.model,
                    summary="Codex task failed.",
                    error=error,
                    conversation_id=thread_id,
                    duration_seconds=time.monotonic() - started,
                    usage=usage,
                )

            payload = self._read_payload(last_message_path)
            if payload is None:
                return TaskResult(
                    status="error",
                    provider=self.name,
                    model=task.model,
                    summary="Codex did not produce the required structured final result.",
                    error=event_error or result.stderr.strip() or "missing or invalid --output-last-message JSON",
                    conversation_id=thread_id,
                    duration_seconds=time.monotonic() - started,
                    usage=usage,
                )

        after = self._git_snapshot(task.workspace) if before is not None else None
        if before is not None and after is not None and before != after:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Read-only Codex delegation changed the workspace.",
                error="Provider violated the read_only contract; inspect git status before continuing.",
                changed_files=tuple(sorted(after.symmetric_difference(before))),
                conversation_id=thread_id,
                duration_seconds=time.monotonic() - started,
                usage=usage,
            )

        return TaskResult(
            status="success",
            provider=self.name,
            model=task.model,
            summary=str(payload.get("summary", "")).strip(),
            final_text=str(payload.get("final_text", "")).strip(),
            evidence=tuple(
                Evidence.from_dict(item)
                for item in payload.get("evidence", [])
                if isinstance(item, dict)
            ),
            changed_files=_string_tuple(payload.get("changed_files")),
            commands=_string_tuple(payload.get("commands")),
            tests=_string_tuple(payload.get("tests")),
            risks=_string_tuple(payload.get("risks")),
            conversation_id=thread_id,
            duration_seconds=time.monotonic() - started,
            usage=usage,
            metadata={
                "stderr": result.stderr.strip(),
                "event_count": len(events),
            },
        )

    def _prompt(self, task: TaskSpec) -> str:
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria) or "- Satisfy the objective exactly."
        access_instruction = (
            "Do not modify files. This is a strictly read-only task."
            if task.access == "read_only"
            else "You may modify files inside the active workspace, but do not broaden scope."
        )
        return (
            "You are a delegated CortexRelay worker.\n\n"
            f"Role: {task.role}\n"
            f"Objective: {task.objective.strip()}\n"
            f"Access: {task.access}\n"
            f"{access_instruction}\n\n"
            "Acceptance criteria:\n"
            f"{criteria}\n\n"
            "Return structured output only. Keep summary compact, but put your COMPLETE answer "
            "to the parent in final_text. Do not compress final_text into a synopsis; preserve all "
            "material findings, conclusions, implementation details, caveats, and recommendations "
            "needed to satisfy the objective. The remaining fields are machine-readable indexes. "
            "Your final answer must satisfy the output schema supplied by CortexRelay."
        )

    @staticmethod
    def _parse_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    @staticmethod
    def _thread_id(events: list[dict[str, Any]]) -> str | None:
        for event in events:
            if event.get("type") == "thread.started":
                value = event.get("thread_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _usage(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in reversed(events):
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                return event["usage"]
        return {}

    @staticmethod
    def _event_error(events: list[dict[str, Any]]) -> str | None:
        for event in reversed(events):
            if event.get("type") in {"error", "turn.failed"}:
                for key in ("message", "error"):
                    value = event.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                    if isinstance(value, dict):
                        message = value.get("message")
                        if isinstance(message, str) and message.strip():
                            return message.strip()
        return None

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _git_snapshot(workspace: Path) -> set[str] | None:
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return {line.rstrip() for line in completed.stdout.splitlines() if line.strip()}


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _parse_json_object(value: str) -> dict[str, Any] | None:
    text = value.strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == fence:
            text = "\n".join(lines[1:-1]).strip()
            if text.startswith("json\n"):
                text = text[5:].lstrip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
