from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from cortex_relay.core.models import Evidence, TaskResult, TaskSpec
from cortex_relay.runtime.process import ProcessCancelledError, ProcessRunner

from .base import ProviderAdapter, ProviderCapabilities
from .result_schema import RESULT_SCHEMA


class AntigravityAdapter(ProviderAdapter):
    name = "antigravity"

    def __init__(self, *, binary: str = "agy", runner: ProcessRunner | None = None) -> None:
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
            reasoning_levels=("low", "medium", "high"),
        )

    def discover_models(self) -> dict[str, dict[str, Any]]:
        """Return the live model catalog exposed by `agy models`."""

        capabilities = self.capabilities()
        if not capabilities.available:
            return {}

        try:
            result = self.runner.run(
                [self.binary, "models"],
                cwd=Path.cwd(),
                timeout_seconds=30,
            )
        except (OSError, subprocess.TimeoutExpired, ProcessCancelledError):
            return {}

        if result.returncode != 0:
            return {}
        return self._parse_model_listing(result.stdout)

    @staticmethod
    def _parse_model_listing(stdout: str) -> dict[str, dict[str, Any]]:
        models: dict[str, dict[str, Any]] = {}

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            # Current agy output is human-readable rather than JSON, e.g.
            #   gemini-3.8-flash-high     Gemini 3.8 Flash (High)
            # Be tolerant of bullets and extra spacing so minor CLI formatting
            # changes do not break discovery.
            line = line.lstrip("-*• ").strip()
            if not line:
                continue

            parts = line.split(None, 1)
            slug = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else slug

            if not _looks_like_model_slug(slug):
                continue

            models[slug] = {"label": label}

        return models

    def command_for(self, task: TaskSpec) -> list[str]:
        argv = [
            self.binary,
            "-p",
            self._prompt(task),
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(RESULT_SCHEMA, separators=(",", ":")),
            "--effort",
            task.reasoning,
            "--print-timeout",
            f"{task.timeout_seconds}s",
            "--sandbox",
        ]
        if task.model:
            argv.extend(["--model", task.model])
        return argv

    def execute(self, task: TaskSpec) -> TaskResult:
        capabilities = self.capabilities()
        if task.reasoning not in capabilities.reasoning_levels:
            supported = ", ".join(capabilities.reasoning_levels)
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Antigravity reasoning effort is unsupported.",
                error=f"Supported Antigravity efforts: {supported}",
            )
        if not capabilities.available:
            return TaskResult(
                status="unavailable",
                provider=self.name,
                model=task.model,
                summary="Antigravity CLI is unavailable.",
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

        before = self._git_snapshot(task.workspace) if task.access == "read_only" else None
        try:
            result = self.runner.run(
                self.command_for(task),
                cwd=task.workspace,
                timeout_seconds=task.timeout_seconds + 15,
                cancel_event=task.metadata.get("_cancel_event"),
            )
        except ProcessCancelledError:
            return TaskResult(
                status="cancelled",
                provider=self.name,
                model=task.model,
                summary="Antigravity task was cancelled.",
                error="provider process cancelled",
            )
        except subprocess.TimeoutExpired:
            return TaskResult(
                status="timeout",
                provider=self.name,
                model=task.model,
                summary="Antigravity task timed out.",
                error=f"timeout after {task.timeout_seconds} seconds",
            )

        stderr_text = result.stderr.strip()
        if "print timeout" in stderr_text.lower():
            return TaskResult(
                status="timeout",
                provider=self.name,
                model=task.model,
                summary="Antigravity task timed out before producing a final result.",
                error=stderr_text or f"timeout after {task.timeout_seconds} seconds",
            )

        envelope = self._parse_envelope(result.stdout)
        status = str(envelope.get("status", "")).upper()
        if result.returncode != 0 or status != "SUCCESS":
            error = str(envelope.get("error") or result.stderr or "Antigravity execution failed").strip()
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Antigravity task failed.",
                error=error,
                conversation_id=_optional_string(envelope.get("conversation_id")),
                duration_seconds=_optional_float(envelope.get("duration_seconds")),
                usage=_dict_or_empty(envelope.get("usage")),
            )

        payload = envelope.get("structured_output")
        if not isinstance(payload, dict):
            payload = self._parse_response_payload(envelope.get("response"))

        after = self._git_snapshot(task.workspace) if before is not None else None
        if before is not None and after is not None and before != after:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Read-only delegation changed the workspace.",
                error="Provider violated the read_only contract; inspect git status before continuing.",
                changed_files=tuple(sorted(after.symmetric_difference(before))),
                conversation_id=_optional_string(envelope.get("conversation_id")),
                duration_seconds=_optional_float(envelope.get("duration_seconds")),
                usage=_dict_or_empty(envelope.get("usage")),
            )

        return TaskResult(
            status="success",
            provider=self.name,
            model=task.model,
            summary=str(payload.get("summary", "")).strip(),
            evidence=tuple(
                Evidence.from_dict(item)
                for item in payload.get("evidence", [])
                if isinstance(item, dict)
            ),
            changed_files=_string_tuple(payload.get("changed_files")),
            commands=_string_tuple(payload.get("commands")),
            tests=_string_tuple(payload.get("tests")),
            risks=_string_tuple(payload.get("risks")),
            conversation_id=_optional_string(envelope.get("conversation_id")),
            duration_seconds=_optional_float(envelope.get("duration_seconds")),
            usage=_dict_or_empty(envelope.get("usage")),
            metadata={
                "stderr": result.stderr.strip(),
                "num_turns": envelope.get("num_turns"),
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
            "Return compact evidence. Do not include long transcripts or unrelated findings. "
            "Your final answer must satisfy the enforced JSON schema."
        )

    @staticmethod
    def _parse_envelope(stdout: str) -> dict[str, Any]:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return {"status": "ERROR", "error": "Antigravity returned invalid JSON", "response": stdout}
        return parsed if isinstance(parsed, dict) else {"status": "ERROR", "error": "Antigravity returned a non-object"}

    @staticmethod
    def _parse_response_payload(response: Any) -> dict[str, Any]:
        if isinstance(response, str):
            try:
                parsed = json.loads(response)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            return {
                "summary": response.strip(),
                "evidence": [],
                "changed_files": [],
                "commands": [],
                "tests": [],
                "risks": [],
            }
        return {
            "summary": "",
            "evidence": [],
            "changed_files": [],
            "commands": [],
            "tests": [],
            "risks": [],
        }

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


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _looks_like_model_slug(value: str) -> bool:
    if not value or " " in value:
        return False
    if value.startswith(("[", "{", "#")):
        return False
    return any(char.isdigit() for char in value) and any(char in value for char in ("-", "_"))
