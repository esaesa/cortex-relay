from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from dataclasses import replace
from pathlib import Path
from typing import Any

from cortex_relay.core.models import Evidence, TaskResult, TaskSpec
from cortex_relay.runtime.process import ProcessCancelledError, ProcessRunner, prepare_process_argv
from cortex_relay.runtime.progress import runner_progress_kwargs

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
            session_mode="native",
            persistent_sessions=True,
            streaming_events=True,
            native_subagents=True,
            child_messaging=False,
        )

    def command_for(self, task: TaskSpec) -> list[str]:
        native_session = bool(task.metadata.get("_opencode_session_mode"))
        argv = [self.binary]
        if not native_session:
            argv.append("--pure")
        argv.extend([
            "run",
            "--format",
            "json",
            "--dir",
            str(task.workspace),
        ])
        provider_session_id = task.metadata.get("_resume_provider_session_id")
        if native_session and isinstance(provider_session_id, str) and provider_session_id.strip():
            argv.extend(["--session", provider_session_id.strip()])
        if task.model:
            argv.extend(["--model", task.model])
        if task.reasoning and task.reasoning != "default":
            argv.extend(["--variant", task.reasoning])

        options = _profile_options(task)
        agent = options.get("agent")
        if isinstance(agent, str) and agent.strip():
            argv.extend(["--agent", agent.strip()])

        argv.append(self._prompt(task, allow_subagents=native_session))
        return argv

    def host_command(self, task: TaskSpec) -> list[str]:
        """Build an interactive OpenCode host command from an orchestrator profile."""

        if not task.model:
            raise ValueError("OpenCode orchestrator profile requires a model")

        argv = [
            self.binary,
            str(task.workspace),
            "--model",
            task.model,
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

        # The TUI reads its variant from model.json; --model accepts only provider/model.
        # Isolate that state so concurrent hosts can choose different variants.
        with tempfile.TemporaryDirectory(prefix="cortex-relay-opencode-") as state_home:
            env = self.host_environment(task)
            env["XDG_STATE_HOME"] = state_home
            _write_host_model_state(Path(state_home), task)
            completed = subprocess.run(
                prepare_process_argv(self.host_command(task)),
                cwd=task.workspace,
                env=env,
                check=False,
            )
            return completed.returncode

    def execute_session(self, task: TaskSpec) -> TaskResult:
        return self.execute(
            replace(
                task,
                metadata={**task.metadata, "_opencode_session_mode": True},
            )
        )

    def continue_session(self, task: TaskSpec, provider_session_id: str) -> TaskResult:
        return self.execute(
            replace(
                task,
                metadata={
                    **task.metadata,
                    "_opencode_session_mode": True,
                    "_resume_provider_session_id": provider_session_id,
                },
            )
        )

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
            self._runtime_config(
                task,
                allow_subagents=bool(task.metadata.get("_opencode_session_mode")),
            ),
            separators=(",", ":"),
        )

        try:
            result = self.runner.run(
                self.command_for(task),
                cwd=task.workspace,
                timeout_seconds=task.timeout_seconds + 15,
                env=env,
                cancel_event=task.metadata.get("_cancel_event"),
                **runner_progress_kwargs(task, self.name),
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
            conversation_id=session_id,
            duration_seconds=time.monotonic() - started,
            usage=usage,
            metadata={
                "stderr": result.stderr.strip(),
                "event_count": len(events),
                "runtime_permissions": "cortex-relay-inline",
                "transport": (
                    "session"
                    if task.metadata.get("_opencode_session_mode")
                    else "closed_end"
                ),
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

    def _runtime_config(
        self,
        task: TaskSpec,
        *,
        allow_subagents: bool = False,
    ) -> dict[str, Any]:
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

        # The interactive CortexRelay host injects this MCP server, and user
        # opencode.json files may define external MCP servers whose schemas
        # OpenCode eagerly injects on every step. Delegated workers forbid MCP
        # usage, so disable all external MCP servers in the worker overlay.
        mcp = config.get("mcp")
        mcp_config = dict(mcp) if isinstance(mcp, dict) else {}
        for server_name in _discover_configured_mcp_servers(task.workspace):
            mcp_config[server_name] = {"enabled": False}

        if isinstance(mcp_config.get("cortex-relay"), dict):
            mcp_config["cortex-relay"] = {"enabled": False}

        servers = mcp_config.get("servers")
        if isinstance(servers, dict) and "cortex-relay" in servers:
            server_config = dict(servers)
            cortex_server = server_config.get("cortex-relay")
            if isinstance(cortex_server, dict):
                disabled_server = dict(cortex_server)
                disabled_server["disabled"] = True
                server_config["cortex-relay"] = disabled_server
                mcp_config["servers"] = server_config

        if mcp_config:
            config["mcp"] = mcp_config

        config["permission"] = _permission_policy(
            task.access,
            allow_subagents=allow_subagents,
        )
        return config

    def _prompt(
        self,
        task: TaskSpec,
        *,
        allow_subagents: bool = False,
    ) -> str:
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
        if not criteria:
            criteria = "- Satisfy the objective exactly."

        access_instruction = (
            "Do not modify files. This is a strictly read-only task."
            if task.access == "read_only"
            else "You may modify files inside the active workspace only. Do not broaden scope."
        )
        schema = json.dumps(RESULT_SCHEMA, separators=(",", ":"))
        delegation = (
            "You may use OpenCode native subagents for useful bounded parallel work. "
            "Their child sessions are observable by CortexRelay. Do not call CortexRelay MCP "
            "from a child to recursively re-delegate the same task."
            if allow_subagents
            else "Do not delegate to subagents, MCP tools, or other external agents."
        )
        return (
            "You are a delegated CortexRelay worker. "
            f"{delegation}\n\n"
            f"Role: {task.role}\n"
            f"Objective: {task.objective.strip()}\n"
            f"Access: {task.access}\n"
            f"{access_instruction}\n\n"
            "Acceptance criteria:\n"
            f"{criteria}\n\n"
            "Return one JSON object and no markdown fences. Put a compact synopsis in summary, "
            "but put your COMPLETE answer to the parent in final_text. Do not shorten final_text "
            "merely to fit the summary; it must preserve all material findings, reasoning conclusions, "
            "implementation details, caveats, and recommendations needed to satisfy the objective. "
            "Use the remaining fields as machine-readable indexes over that answer. Your FINAL text "
            "response must match this schema:\n"
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
        step_parts: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") != "step_finish":
                continue
            part = event.get("part")
            if isinstance(part, dict):
                step_parts.append(part)
        if not step_parts:
            return {}

        if len(step_parts) == 1:
            part = step_parts[0]
            usage: dict[str, Any] = {}
            for key in ("tokens", "cost", "modelID", "providerID", "reason"):
                if key in part:
                    usage[key] = part[key]
            return usage

        total_tokens: dict[str, Any] = {}
        cache_totals: dict[str, int] = {}
        has_tokens = False
        total_cost = 0.0
        has_cost = False

        for part in step_parts:
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                has_tokens = True
                for key in ("input", "output", "reasoning", "total"):
                    val = tokens.get(key)
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        total_tokens[key] = int(total_tokens.get(key, 0)) + int(val)
                cache = tokens.get("cache")
                if isinstance(cache, dict):
                    for ckey in ("read", "write"):
                        cval = cache.get(ckey)
                        if isinstance(cval, (int, float)) and not isinstance(cval, bool):
                            cache_totals[ckey] = int(cache_totals.get(ckey, 0)) + int(cval)
            cost = part.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                total_cost += float(cost)
                has_cost = True

        if cache_totals:
            total_tokens["cache"] = cache_totals

        usage = {}
        if has_tokens:
            usage["tokens"] = total_tokens
        if has_cost:
            usage["cost"] = total_cost
        last_part = step_parts[-1]
        for key in ("modelID", "providerID", "reason"):
            if key in last_part:
                usage[key] = last_part[key]
        return usage

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


def _discover_configured_mcp_servers(workspace: Path | None = None) -> tuple[str, ...]:
    candidates: list[Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "opencode" / "opencode.json")
    candidates.append(Path.home() / ".config" / "opencode" / "opencode.json")
    if workspace is not None:
        candidates.append(workspace / "opencode.json")
        candidates.append(workspace / ".opencode" / "opencode.json")

    names: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        mcp = parsed.get("mcp")
        if not isinstance(mcp, dict):
            continue
        for key, value in mcp.items():
            if key == "servers" or not isinstance(value, dict):
                continue
            if key not in seen:
                seen.add(key)
                names.append(key)
    return tuple(names)


def _permission_policy(
    access: str,
    *,
    allow_subagents: bool = False,
) -> dict[str, Any]:
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
        "task": "allow" if allow_subagents else "deny",
        "skill": "deny",
        "todowrite": "deny",
        "todoread": "deny",
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
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _write_host_model_state(state_home: Path, task: TaskSpec) -> None:
    """Give this host launch its own model/variant selection state."""

    source_root = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state"
    )
    source_path = Path(source_root) / "opencode" / "model.json"
    try:
        loaded = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = {}

    model_state = dict(loaded) if isinstance(loaded, dict) else {}
    variants = model_state.get("variant")
    variant_state = dict(variants) if isinstance(variants, dict) else {}
    if task.reasoning == "default":
        variant_state.pop(task.model, None)
    else:
        variant_state[task.model] = task.reasoning
    model_state["variant"] = variant_state

    destination = state_home / "opencode" / "model.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(model_state, separators=(",", ":")),
        encoding="utf-8",
    )


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
