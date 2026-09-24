from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from cortex_relay.core.models import Evidence, TaskResult, TaskSpec
from cortex_relay.runtime.process import ProcessRunner

from .base import ProviderAdapter, ProviderCapabilities


RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string"},
                    "path": {"type": "string"},
                    "symbol": {"type": "string"},
                    "severity": {"type": "string"},
                },
                "required": ["finding"],
                "additionalProperties": False,
            },
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "commands": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "evidence", "changed_files", "commands", "tests", "risks"],
    "additionalProperties": False,
}


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
        )

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
            )
        except subprocess.TimeoutExpired:
            return TaskResult(
                status="timeout",
                provider=self.name,
                model=task.model,
                summary="Antigravity task timed out.",
                error=f"timeout after {task.timeout_seconds} seconds",
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
