from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from pathlib import Path
from typing import Any

from cortex_relay.core.models import Evidence, TaskResult, TaskSpec
from cortex_relay.runtime.process import ProcessCancelledError, ProcessRunner

from .base import ProviderAdapter, ProviderCapabilities
from .result_schema import RESULT_SCHEMA


class OpenCodeAdapter(ProviderAdapter):
    """OpenCode/Zen runtime adapter using non-interactive JSON event output."""

    name = "opencode"

    def __init__(
        self,
        *,
        binary: str = "opencode",
        runner: ProcessRunner | None = None,
    ) -> None:
        self.binary = binary
        self.runner = runner or ProcessRunner()
        self._model_cache: dict[str, dict[str, Any]] | None = None

    def capabilities(self) -> ProviderCapabilities:
        path = shutil.which(self.binary)
        return ProviderCapabilities(
            name=self.name,
            binary=self.binary,
            available=path is not None,
            structured_output=False,
            model_selection=True,
            reasoning_control=True,
            read_only_policy=True,
            workspace_write=True,
            detail=path or f"{self.binary} was not found on PATH",
        )

    def command_for(self, task: TaskSpec) -> list[str]:
        argv = [
            self.binary,
            "--pure",
            "run",
            "--format",
            "json",
            "--dir",
            str(task.workspace),
        ]
        if task.model:
            argv.extend(["--model", task.model])
        if task.reasoning and task.reasoning != "default":
            argv.extend(["--variant", task.reasoning])

        options = _profile_options(task)
        agent = options.get("agent")
        if isinstance(agent, str) and agent.strip():
            argv.extend(["--agent", agent.strip()])

        argv.append(self._prompt(task))
        return argv

    def host_command(self, task: TaskSpec) -> list[str]:
        """Build an interactive OpenCode host command from an orchestrator profile."""

        if not task.model:
            raise ValueError("OpenCode orchestrator profile requires a model")

        selector = task.model
        if task.reasoning and task.reasoning != "default":
            selector = f"{selector}#{task.reasoning}"

        argv = [
            self.binary,
            str(task.workspace),
            "--model",
            selector,
        ]
        options = _profile_options(task)
        agent = options.get("agent")
        if isinstance(agent, str) and agent.strip():
            argv.extend(["--agent", agent.strip()])

        prompt = task.metadata.get("host_prompt")
        if isinstance(prompt, str) and prompt.strip():
            argv.extend(["--prompt", prompt.strip()])
        return argv

    def host_environment(self, task: TaskSpec) -> dict[str, str]:
        """Inject CortexRelay MCP into an interactive OpenCode host session."""

        env = dict(os.environ)
        env["OPENCODE_CLIENT"] = "cortex-relay-orchestrator"

        session_id = task.metadata.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            env["CORTEX_RELAY_SESSION_ID"] = session_id.strip()
        if task.profile:
            env["CORTEX_RELAY_HOST_PROFILE"] = task.profile
        if task.model:
            env["CORTEX_RELAY_HOST_MODEL"] = task.model
        env["CORTEX_RELAY_HOST_REASONING"] = task.reasoning
        env["CORTEX_RELAY_WORKSPACE"] = str(task.workspace)

        config: dict[str, Any] = {}
        raw = os.environ.get("OPENCODE_CONFIG_CONTENT")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    config = parsed
            except json.JSONDecodeError:
                pass

        mcp = config.get("mcp")
        mcp_config = dict(mcp) if isinstance(mcp, dict) else {}
        servers = mcp_config.get("servers")
        server_config = dict(servers) if isinstance(servers, dict) else {}
        server_config["cortex-relay"] = {
            "type": "local",
            "command": [
                sys.executable,
                "-m",
                "cortex_relay.cli",
                "serve",
                "--transport",
                "mcp",
            ],
            "cwd": str(task.workspace),
        }
        mcp_config["servers"] = server_config
        config["mcp"] = mcp_config

        permission = config.get("permission")
        permission_config = dict(permission) if isinstance(permission, dict) else {}
        permission_config["task"] = "deny"
        permission_config["external_directory"] = "deny"
        if task.access == "read_only":
            permission_config["edit"] = "deny"
        config["permission"] = permission_config

        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            config,
            separators=(",", ":"),
        )
        return env

    def launch_host(self, task: TaskSpec) -> int:
        """Launch an interactive OpenCode orchestrator with CortexRelay MCP."""

        capabilities = self.capabilities()
        if not capabilities.available:
            raise RuntimeError(capabilities.detail)
        if not task.workspace.exists():
            raise RuntimeError(f"workspace does not exist: {task.workspace}")

        variant_error = self._variant_error(task)
        if variant_error:
            raise RuntimeError(variant_error)

        completed = subprocess.run(
            self.host_command(task),
            cwd=task.workspace,
            env=self.host_environment(task),
            check=False,
        )
        return completed.returncode

    def execute(self, task: TaskSpec) -> TaskResult:
        capabilities = self.capabilities()
        if not capabilities.available:
            return TaskResult(
                status="unavailable",
                provider=self.name,
                model=task.model,
                summary="OpenCode CLI is unavailable.",
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

        variant_error = self._variant_error(task)
        if variant_error:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="OpenCode model/variant configuration is incompatible.",
                error=variant_error,
            )

        before = self._git_snapshot(task.workspace) if task.access == "read_only" else None
        started = time.monotonic()
        env = dict(os.environ)
        env["OPENCODE_CLIENT"] = "cortex-relay"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            self._runtime_config(task),
            separators=(",", ":"),
        )

        try:
            result = self.runner.run(
                self.command_for(task),
                cwd=task.workspace,
                timeout_seconds=task.timeout_seconds + 15,
                env=env,
                cancel_event=task.metadata.get("_cancel_event"),
            )
        except ProcessCancelledError:
            return TaskResult(
                status="cancelled",
                provider=self.name,
                model=task.model,
                summary="OpenCode task was cancelled.",
                error="provider process cancelled",
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired:
            return TaskResult(
                status="timeout",
                provider=self.name,
                model=task.model,
                summary="OpenCode task timed out.",
                error=f"timeout after {task.timeout_seconds} seconds",
                duration_seconds=time.monotonic() - started,
            )

        events = self._parse_events(result.stdout)
        session_id = self._session_id(events)
        event_error = self._event_error(events)
        usage = self._usage(events)

        if result.returncode != 0 or event_error:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="OpenCode task failed.",
                error=(event_error or result.stderr or "OpenCode execution failed").strip(),
                conversation_id=session_id,
                duration_seconds=time.monotonic() - started,
                usage=usage,
            )

        payload = self._payload(events)
        if payload is None:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="OpenCode did not produce the required structured final result.",
                error=result.stderr.strip() or "missing or invalid final JSON text event",
                conversation_id=session_id,
                duration_seconds=time.monotonic() - started,
                usage=usage,
            )

        after = self._git_snapshot(task.workspace) if before is not None else None
        if before is not None and after is not None and before != after:
            return TaskResult(
                status="error",
                provider=self.name,
                model=task.model,
                summary="Read-only OpenCode delegation changed the workspace.",
                error="Provider violated the read_only contract; inspect git status before continuing.",
                changed_files=tuple(sorted(after.symmetric_difference(before))),
                conversation_id=session_id,
                duration_seconds=time.monotonic() - started,
                usage=usage,
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
            conversation_id=session_id,
            duration_seconds=time.monotonic() - started,
            usage=usage,
            metadata={
                "stderr": result.stderr.strip(),
                "event_count": len(events),
                "runtime_permissions": "cortex-relay-inline",
            },
        )

    def discover_models(
        self,
        *,
        refresh: bool = False,
        verbose: bool = False,
    ) -> dict[str, dict[str, Any]]:
        capabilities = self.capabilities()
        if not capabilities.available:
            return {}

        argv = [self.binary, "models"]
        if refresh:
            argv.append("--refresh")
        if verbose:
            argv.append("--verbose")

        try:
            result = self.runner.run(
                argv,
                cwd=Path.cwd(),
                timeout_seconds=45,
            )
        except (subprocess.TimeoutExpired, ProcessCancelledError):
            return {}
        if result.returncode != 0:
            return {}

        parsed = self._parse_model_listing(result.stdout, verbose=verbose)
        if verbose:
            self._model_cache = parsed
        return parsed

    def _variant_error(self, task: TaskSpec) -> str | None:
        if (
            not task.model
            or not task.reasoning
            or task.reasoning == "default"
            or not bool(_profile_options(task).get("validate_variant", True))
        ):
            return None

        if self._model_cache is None:
            self.discover_models(verbose=True)
        if not self._model_cache:
            return None

        metadata = self._model_cache.get(task.model)
        if not metadata:
            return None
        variants = _variant_names(metadata)
        if variants and task.reasoning not in variants:
            return (
                f"OpenCode model {task.model!r} does not advertise variant "
                f"{task.reasoning!r}. Available variants: {', '.join(sorted(variants))}"
            )
        return None

    def _runtime_config(self, task: TaskSpec) -> dict[str, Any]:
        existing: dict[str, Any] = {}
        raw = os.environ.get("OPENCODE_CONFIG_CONTENT")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    existing = parsed
            except json.JSONDecodeError:
                pass

        config = dict(existing)

        # The interactive CortexRelay host injects this MCP server. A delegated
        # OpenCode worker must not inherit it or it could recursively delegate
        # back into CortexRelay.
        mcp = config.get("mcp")
        if isinstance(mcp, dict):
            mcp_config = dict(mcp)
            servers = mcp_config.get("servers")
            if isinstance(servers, dict) and "cortex-relay" in servers:
                server_config = dict(servers)
                cortex_server = server_config.get("cortex-relay")
                if isinstance(cortex_server, dict):
                    disabled_server = dict(cortex_server)
                    disabled_server["disabled"] = True
                    server_config["cortex-relay"] = disabled_server
                    mcp_config["servers"] = server_config
                    config["mcp"] = mcp_config

        config["permission"] = _permission_policy(task.access)
        return config

    def _prompt(self, task: TaskSpec) -> str:
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
        if not criteria:
            criteria = "- Satisfy the objective exactly."

        access_instruction = (
            "Do not modify files. This is a strictly read-only task."
            if task.access == "read_only"
            else "You may modify files inside the active workspace only. Do not broaden scope."
        )
        schema = json.dumps(RESULT_SCHEMA, separators=(",", ":"))
        return (
            "You are a delegated CortexRelay worker. Do not delegate to subagents, MCP tools, "
            "or other external agents.\n\n"
            f"Role: {task.role}\n"
            f"Objective: {task.objective.strip()}\n"
            f"Access: {task.access}\n"
            f"{access_instruction}\n\n"
            "Acceptance criteria:\n"
            f"{criteria}\n\n"
            "Return compact evidence and no unrelated material. Your FINAL text response "
            "must be ONLY one JSON object matching this schema, with no markdown fences:\n"
            f"{schema}"
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
    def _session_id(events: list[dict[str, Any]]) -> str | None:
        for event in events:
            value = event.get("sessionID")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _event_error(events: list[dict[str, Any]]) -> str | None:
        for event in reversed(events):
            if event.get("type") != "error":
                continue
            error = event.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            if isinstance(error, dict):
                data = error.get("data")
                if isinstance(data, dict):
                    message = data.get("message")
                    if isinstance(message, str) and message.strip():
                        return message.strip()
                name = error.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return None

    @staticmethod
    def _payload(events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            if event.get("type") != "text":
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            value = part.get("text")
            if not isinstance(value, str) or not value.strip():
                continue
            parsed = _parse_json_object(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _usage(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in reversed(events):
            if event.get("type") != "step_finish":
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            usage: dict[str, Any] = {}
            for key in ("tokens", "cost", "modelID", "providerID", "reason"):
                if key in part:
                    usage[key] = part[key]
            return usage
        return {}

    @staticmethod
    def _parse_model_listing(
        stdout: str,
        *,
        verbose: bool,
    ) -> dict[str, dict[str, Any]]:
        if not verbose:
            return {
                line.strip(): {}
                for line in stdout.splitlines()
                if _looks_like_model_id(line.strip())
            }

        result: dict[str, dict[str, Any]] = {}
        lines = stdout.splitlines()
        index = 0
        while index < len(lines):
            candidate = lines[index].strip()
            if not _looks_like_model_id(candidate):
                index += 1
                continue
            model_id = candidate
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index >= len(lines) or not lines[index].lstrip().startswith("{"):
                result[model_id] = {}
                continue

            buffer: list[str] = []
            depth = 0
            started = False
            while index < len(lines):
                line = lines[index]
                buffer.append(line)
                depth += _brace_delta(line)
                if "{" in line:
                    started = True
                index += 1
                if started and depth <= 0:
                    break

            try:
                parsed = json.loads("\n".join(buffer))
            except json.JSONDecodeError:
                parsed = {}
            result[model_id] = parsed if isinstance(parsed, dict) else {}
        return result

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


def _profile_options(task: TaskSpec) -> dict[str, Any]:
    value = task.metadata.get("profile_options")
    return value if isinstance(value, dict) else {}


def _permission_policy(access: str) -> dict[str, Any]:
    safe_bash = {
        "*": "ask",
        "git status*": "allow",
        "git diff*": "allow",
        "git log*": "allow",
        "git show*": "allow",
        "git grep*": "allow",
    }
    if access == "workspace_write":
        safe_bash.update(
            {
                "python -m unittest*": "allow",
                "python -m pytest*": "allow",
                "pytest *": "allow",
                "npm test*": "allow",
                "npm run test*": "allow",
                "npm run lint*": "allow",
                "npm run typecheck*": "allow",
                "npm run build*": "allow",
                "pnpm test*": "allow",
                "pnpm run test*": "allow",
                "yarn test*": "allow",
                "go test*": "allow",
                "cargo test*": "allow",
                "dotnet test*": "allow",
            }
        )

    return {
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "lsp": "allow",
        "edit": "deny" if access == "read_only" else "allow",
        "external_directory": "deny",
        "task": "deny",
        "skill": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "bash": safe_bash,
    }


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


def _variant_names(metadata: dict[str, Any]) -> set[str]:
    for key in ("variants", "reasoning_options", "reasoningOptions"):
        value = metadata.get(key)
        if isinstance(value, dict):
            return {str(name) for name in value}
        if isinstance(value, list):
            names: set[str] = set()
            for item in value:
                if isinstance(item, str):
                    names.add(item)
                elif isinstance(item, dict):
                    name = item.get("id") or item.get("name")
                    if isinstance(name, str):
                        names.add(name)
            return names
    return set()


def _looks_like_model_id(value: str) -> bool:
    return bool(value and "/" in value and not value.startswith(("{", "[", "#")))


def _brace_delta(line: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    for char in line:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())
