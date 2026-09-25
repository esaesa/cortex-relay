from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cortex_relay import __version__
from cortex_relay.core.models import TaskAccess, TaskResult, TaskSpec
from cortex_relay.core.registry import ProviderRegistry, default_registry
from cortex_relay.runtime.progress import ProgressEvent


_AGENT_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")

try:  # Optional runtime dependency.
    import uvicorn
    from fastapi import FastAPI

    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.events.event_queue import EventQueue
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (
        add_a2a_routes_to_fastapi,
        create_agent_card_routes,
        create_jsonrpc_routes,
        create_rest_routes,
    )
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.server.tasks.task_updater import TaskUpdater
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentSkill,
        Part,
        Task,
        TaskState,
        TaskStatus,
    )

    _A2A_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised by base-install tests.
    uvicorn = None
    FastAPI = Any  # type: ignore[assignment]
    AgentExecutor = object  # type: ignore[assignment,misc]
    RequestContext = Any  # type: ignore[assignment]
    EventQueue = Any  # type: ignore[assignment]
    DefaultRequestHandler = None
    InMemoryTaskStore = None
    TaskUpdater = None
    AgentCapabilities = None
    AgentCard = None
    AgentInterface = None
    AgentSkill = None
    Part = None
    Task = None
    TaskState = None
    TaskStatus = None
    _A2A_IMPORT_ERROR = exc


@dataclass(frozen=True)
class A2AServerPolicy:
    """Server-side delegation policy applied to every inbound A2A task."""

    profile: str | None = None
    preset: str | None = None
    provider: str = "auto"
    model: str | None = None
    reasoning: str = "high"
    role: str = "reviewer"
    workspace: Path = field(default_factory=Path.cwd)
    access: TaskAccess = "read_only"
    timeout_seconds: int = 300
    isolate_write: bool = True

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        object.__setattr__(
            self,
            "profile",
            self.profile.strip()
            if isinstance(self.profile, str) and self.profile.strip()
            else None,
        )
        object.__setattr__(
            self,
            "preset",
            self.preset.strip()
            if isinstance(self.preset, str) and self.preset.strip()
            else None,
        )
        if not self.role.strip():
            raise ValueError("role must not be empty")
        if self.access not in {"read_only", "workspace_write"}:
            raise ValueError("access must be read_only or workspace_write")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        object.__setattr__(self, "reasoning", self.reasoning.strip().lower())
        object.__setattr__(
            self,
            "isolate_write",
            self.access == "workspace_write" and bool(self.isolate_write),
        )

    def task_spec_for(
        self,
        objective: str,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: Any | None = None,
    ) -> TaskSpec:
        metadata: dict[str, Any] = {
            "transport": "a2a",
        }
        if cancel_event is not None:
            metadata["_cancel_event"] = cancel_event
        if callable(progress_callback):
            metadata["_external_progress"] = progress_callback
        return TaskSpec(
            objective=objective,
            role=self.role,
            profile=self.profile,
            preset=self.preset,
            provider=self.provider,
            workspace=self.workspace,
            access=self.access,
            reasoning=self.reasoning,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            isolate_write=self.isolate_write,
            metadata=metadata,
        )


class CortexRelayA2AExecutor(AgentExecutor):  # type: ignore[misc]
    """A2A executor that maps text messages to the CortexRelay runtime."""

    def __init__(
        self,
        policy: A2AServerPolicy,
        *,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.policy = policy
        self.registry = registry or default_registry()
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        _require_a2a()

        user_message = context.message
        task_id = context.task_id
        context_id = context.context_id
        if not user_message or not task_id or not context_id:
            return

        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[user_message],
            )
        )

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task_id,
            context_id=context_id,
        )

        objective = context.get_user_input().strip()
        if not objective:
            message = updater.new_agent_message(
                parts=[Part(text="CortexRelay requires a non-empty text task.")]
            )
            await updater.reject(message=message)
            return

        await updater.start_work(
            message=updater.new_agent_message(
                parts=[
                    Part(
                        text=(
                            f"Delegating through CortexRelay profile={self.policy.profile or 'auto'} "
                            f"provider={self.policy.provider} role={self.policy.role} "
                            f"access={self.policy.access}."
                        )
                    )
                ]
            )
        )

        cancel_event = threading.Event()
        with self._cancel_lock:
            self._cancel_events[task_id] = cancel_event

        progress_events: queue.Queue[ProgressEvent] = queue.Queue()

        def on_progress(event: ProgressEvent) -> None:
            progress_events.put(event)

        spec = self.policy.task_spec_for(
            objective,
            cancel_event=cancel_event,
            progress_callback=on_progress,
        )

        worker = asyncio.create_task(asyncio.to_thread(self.registry.execute, spec))
        try:
            while not worker.done():
                emitted = 0
                while emitted < 8:
                    try:
                        progress = progress_events.get_nowait()
                    except queue.Empty:
                        break
                    emitted += 1
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        message=updater.new_agent_message(
                            parts=[Part(text=_a2a_progress_text(progress))]
                        ),
                    )
                await asyncio.sleep(0.25)
            result = await worker
            while True:
                try:
                    progress = progress_events.get_nowait()
                except queue.Empty:
                    break
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=updater.new_agent_message(
                        parts=[Part(text=_a2a_progress_text(progress))]
                    ),
                )
        except asyncio.CancelledError:
            cancel_event.set()
            worker.cancel()
            raise
        except Exception as exc:
            message = updater.new_agent_message(
                parts=[Part(text=f"CortexRelay A2A execution failed: {exc}")]
            )
            await updater.failed(message=message)
            return
        finally:
            with self._cancel_lock:
                self._cancel_events.pop(task_id, None)

        artifact_text = json.dumps(
            result.to_dict(),
            indent=2,
            ensure_ascii=False,
        )
        await updater.add_artifact(
            parts=[Part(text=artifact_text)],
            name="cortex-relay-result.json",
            last_chunk=True,
        )

        status_message = updater.new_agent_message(
            parts=[
                Part(
                    text=(
                        result.summary
                        or result.error
                        or f"CortexRelay task finished with status {result.status}."
                    )
                )
            ]
        )

        if result.status == "success":
            await updater.complete(message=status_message)
        elif result.status == "cancelled":
            await updater.cancel(message=status_message)
        else:
            await updater.failed(message=status_message)

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        _require_a2a()
        task_id = context.task_id or ""
        context_id = context.context_id or ""

        with self._cancel_lock:
            cancel_event = self._cancel_events.get(task_id)
        if cancel_event is not None:
            cancel_event.set()

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task_id,
            context_id=context_id,
        )
        message = updater.new_agent_message(
            parts=[Part(text="CortexRelay cancellation requested.")]
        )
        with contextlib.suppress(RuntimeError):
            await updater.cancel(message=message)


def _a2a_progress_text(event: ProgressEvent) -> str:
    parts = [event.activity]
    if event.tool:
        parts.append(f"tool={event.tool}")
    if event.command:
        parts.append(f"command={event.command}")
    elif event.path:
        parts.append(f"path={event.path}")
    if event.exit_code is not None:
        parts.append(f"exit={event.exit_code}")
    if event.error_preview:
        parts.append(f"diagnostic={event.error_preview}")
    elif event.output_preview:
        parts.append(f"result={event.output_preview}")
    return "CortexRelay progress: " + " | ".join(parts)


def a2a_available() -> bool:
    return _A2A_IMPORT_ERROR is None


def create_agent_card(
    *,
    public_url: str,
    policy: A2AServerPolicy,
    name: str = "CortexRelay",
) -> Any:
    _require_a2a()
    base = _normalize_public_url(public_url)
    if policy.profile:
        route_label = f"profile={policy.profile}"
    elif policy.preset:
        route_label = f"preset={policy.preset}, role={policy.role}"
    else:
        provider_label = (
            policy.provider if policy.provider != "auto" else "configured provider"
        )
        route_label = f"provider={provider_label}, role={policy.role}"
    description = (
        "Provider-neutral coding-agent delegation through CortexRelay. "
        f"Inbound text tasks use server-fixed {route_label} and access={policy.access}."
    )
    input_modes = ["text/plain"]
    output_modes = ["application/json", "text/plain"]

    return AgentCard(
        name=name,
        description=description,
        version=__version__,
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
        ),
        default_input_modes=input_modes,
        default_output_modes=output_modes,
        skills=[
            AgentSkill(
                id="delegate_coding_task",
                name="Delegate coding task",
                description=(
                    "Execute one bounded coding, repository exploration, testing, "
                    "architecture, or review task through CortexRelay."
                ),
                tags=["coding", "delegation", "review", "testing"],
                examples=[
                    "Review the authentication change for regressions.",
                    "Map the files involved in cache invalidation.",
                ],
                input_modes=input_modes,
                output_modes=output_modes,
            )
        ],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url=f"{base}/a2a/jsonrpc",
            ),
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="0.3",
                url=f"{base}/a2a/jsonrpc",
            ),
            AgentInterface(
                protocol_binding="HTTP+JSON",
                protocol_version="1.0",
                url=f"{base}/a2a/rest",
            ),
            AgentInterface(
                protocol_binding="HTTP+JSON",
                protocol_version="0.3",
                url=f"{base}/a2a/rest",
            ),
        ],
    )


def create_a2a_app(
    *,
    policy: A2AServerPolicy,
    registry: ProviderRegistry | None = None,
    public_url: str = "http://127.0.0.1:8765",
    name: str = "CortexRelay",
) -> Any:
    """Create a FastAPI A2A v1 server with v0.3 request compatibility."""

    _require_a2a()
    card = create_agent_card(
        public_url=public_url,
        policy=policy,
        name=name,
    )
    handler = DefaultRequestHandler(
        agent_executor=CortexRelayA2AExecutor(policy, registry=registry),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    app = FastAPI(
        title=name,
        version=__version__,
        description="CortexRelay A2A delegation server",
    )

    @app.get("/healthz")
    async def _healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "profile": policy.profile,
            "preset": policy.preset,
            "provider": policy.provider,
            "access": policy.access,
        }

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card=card),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler=handler,
            rpc_url="/a2a/jsonrpc",
            enable_v0_3_compat=True,
        ),
        rest_routes=create_rest_routes(
            request_handler=handler,
            path_prefix="/a2a/rest",
            enable_v0_3_compat=True,
        ),
    )
    return app


def run_a2a(
    *,
    policy: A2AServerPolicy,
    host: str = "127.0.0.1",
    port: int = 8765,
    public_url: str | None = None,
    name: str = "CortexRelay",
    allow_remote: bool = False,
    registry: ProviderRegistry | None = None,
) -> None:
    _require_a2a()
    validate_bind_host(host, allow_remote=allow_remote)
    resolved_public_url = resolve_public_url(
        host=host,
        port=port,
        public_url=public_url,
    )
    app = create_a2a_app(
        policy=policy,
        registry=registry,
        public_url=resolved_public_url,
        name=name,
    )
    uvicorn.run(app, host=host, port=port)


def validate_bind_host(host: str, *, allow_remote: bool) -> None:
    if is_loopback_host(host):
        return
    if not allow_remote:
        raise RuntimeError(
            "Refusing to expose the unauthenticated A2A server on a non-loopback "
            "interface. Bind to 127.0.0.1/localhost, or pass --a2a-allow-remote "
            "only when you intentionally accept the network exposure."
        )


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def resolve_public_url(
    *,
    host: str,
    port: int,
    public_url: str | None,
) -> str:
    if public_url:
        return _normalize_public_url(public_url)

    advertised_host = host.strip()
    if advertised_host in {"0.0.0.0", "::"}:
        advertised_host = "127.0.0.1"
    if ":" in advertised_host and not advertised_host.startswith("["):
        advertised_host = f"[{advertised_host}]"
    return f"http://{advertised_host}:{port}"


def agent_card_url(public_url: str) -> str:
    return f"{_normalize_public_url(public_url)}/.well-known/agent-card.json"


def gemini_remote_agent_markdown(
    *,
    name: str,
    agent_card_url: str,
    description: str = "Delegate cross-provider coding tasks through CortexRelay.",
) -> str:
    """Render a Gemini CLI remote-subagent definition for CortexRelay."""

    if not _AGENT_SLUG_RE.fullmatch(name):
        raise ValueError(
            "name must contain only lowercase letters, numbers, '-' or '_'"
        )
    if not agent_card_url.startswith(("http://", "https://")):
        raise ValueError("agent_card_url must be an HTTP(S) URL")

    return (
        "---\n"
        "kind: remote\n"
        f"name: {name}\n"
        f"agent_card_url: {agent_card_url}\n"
        "---\n\n"
        f"{description.strip()}\n"
    )


def inline_agent_card_json(card: dict[str, object]) -> str:
    return json.dumps(card, separators=(",", ":"), ensure_ascii=False)


def _normalize_public_url(url: str) -> str:
    value = url.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("public_url must be an HTTP(S) URL")
    return value


def _require_a2a() -> None:
    if _A2A_IMPORT_ERROR is not None:
        raise RuntimeError(
            'A2A support is not installed. Install with: pip install "cortex-relay[a2a]"'
        ) from _A2A_IMPORT_ERROR
