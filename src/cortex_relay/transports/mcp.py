from __future__ import annotations

import asyncio

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cortex_relay.core.models import QualityGates, TaskBudget, TaskSpec
from cortex_relay.core.registry import ProviderRegistry, default_registry
from cortex_relay.runtime.agent_service import AgentService
from cortex_relay.runtime.task_protocol import TaskControl
from cortex_relay.runtime.task_service import TaskService


def create_server(
    registry: ProviderRegistry | None = None,
    *,
    async_tasks: TaskControl | None = None,
    agent_control: AgentService | None = None,
) -> Any:
    """Create the optional MCP v2 server without importing MCP at package import time."""

    try:
        from mcp.server.mcpserver import Context, MCPServer
        # This module uses postponed annotations. Expose Context in module globals
        # only after the optional MCP dependency is successfully imported so MCP's
        # runtime type-hint inspection can inject it into agent_watch.
        globals()["Context"] = Context
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            'MCP support is not installed. Install with: pip install "cortex-relay[mcp]"'
        ) from exc

    runtime = registry or default_registry()
    async_tasks = async_tasks or TaskService(runtime)
    agent_control = agent_control or AgentService(runtime)
    server = MCPServer(
        "CortexRelay",
        instructions=(
            "CortexRelay is a cross-provider agent control plane. "
            "Keep planning, arbitration, routing decisions, and final synthesis in the calling host. "
            "Prefer persistent provider-native agent sessions when available. Use agent_start_async for parallel direct specialists, agent_start when the first result is immediately required, and delegate/delegate_async only for DAG, scheduler, budget, worktree, quality-gate, or artifact workflows. Direct-agent access defaults to the selected profile via access=auto. Treat closed-end execution as an explicit fallback for one-shot or unsupported cases. "
            "Every delegated agent and provider-native child must remain observable through CortexRelay with durable session identity, semantic events, messages, state, and complete final output. "
            "Use incremental cursors for live activity instead of repeatedly re-reading full state. "
            "Provider-native subagents are allowed, but they remain subject to CortexRelay depth, budget, access, scheduling, workspace, and quality policies. "
            "Do not conclude while worker results required for the answer are still pending; consume the required agent outputs before final synthesis. "
            "Preserve immutable dependency/workspace lineage for staged writes, inspect changes before applying them, and never apply or discard worker work without explicit user intent. "
            "Structured summaries and progress are supplementary: the complete worker final result is authoritative."
        ),
    )

    @server.tool()
    def providers() -> list[dict[str, object]]:
        """List registered runtime providers and their current capabilities."""
        return runtime.capabilities()

    @server.tool()
    def profiles(workspace: str = ".", preset: str | None = None) -> dict[str, Any]:
        """Show merged execution profiles and effective role assignments."""
        return runtime.profile_config(Path(workspace), preset=preset)

    @server.tool()
    def status(
        workspace: str = ".",
        limit: int = 12,
        active_only: bool = False,
    ) -> dict[str, Any]:
        """Show active/recent CortexRelay delegation state for a workspace."""
        return runtime.status_snapshot(
            Path(workspace),
            limit=max(1, limit),
            active_only=active_only,
        )

    @server.tool()
    def history(
        workspace: str = ".",
        limit: int = 30,
    ) -> dict[str, Any]:
        """Show completed CortexRelay delegation history for a workspace."""
        return runtime.status_snapshot(
            Path(workspace),
            limit=max(1, limit),
            completed_only=True,
        )

    @server.tool()
    def delegate(
        objective: str,
        role: str = "reviewer",
        profile: str | None = None,
        preset: str | None = None,
        provider: str = "auto",
        workspace: str = ".",
        access: str = "read_only",
        reasoning: str = "high",
        model: str | None = None,
        acceptance_criteria: list[str] | None = None,
        timeout_seconds: int = 300,
        isolate_write: bool = True,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        max_tool_calls: int | None = None,
        max_repeated_calls: int | None = None,
        max_idle_seconds: int | None = None,
        max_runtime_seconds: int | None = None,
        max_child_agents: int | None = None,
        require_changed_files: bool = False,
        require_tests: bool = False,
        allowed_paths: list[str] | None = None,
        max_failed_tests: int | None = None,
    ) -> dict[str, Any]:
        """Run one bounded workflow/closed-end task and return its normalized result.

        For a persistent specialist that may receive follow-up messages or resume,
        use agent_start instead.
        """
        task = _task_from_values(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=workspace,
            access=access,
            reasoning=reasoning,
            model=model,
            acceptance_criteria=acceptance_criteria or [],
            timeout_seconds=timeout_seconds,
            isolate_write=isolate_write,
            max_tokens=max_tokens,
            max_cost=max_cost,
            max_tool_calls=max_tool_calls,
            max_repeated_calls=max_repeated_calls,
            max_idle_seconds=max_idle_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_child_agents=max_child_agents,
            require_changed_files=require_changed_files,
            require_tests=require_tests,
            allowed_paths=allowed_paths or [],
            max_failed_tests=max_failed_tests,
        )
        return runtime.execute(task).to_dict()

    @server.tool()
    def delegate_parallel(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Synchronously execute short, independent delegated tasks concurrently.

        The MCP call waits for every task. A client-side timeout does not cancel
        launched workers and may prevent their task results from reaching the caller.
        For long-running or parallel workflows, use delegate_async for each task,
        then inspect them by task ID or group ID. Write-capable tasks are isolated
        into git worktrees by default.
        """
        specs = [_task_from_mapping(item) for item in tasks]
        if not specs:
            return []
        max_workers = min(8, len(specs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(runtime.execute, specs))
        return [result.to_dict() for result in results]

    @server.tool()
    def delegate_async(
        objective: str,
        role: str = "reviewer",
        profile: str | None = None,
        preset: str | None = None,
        provider: str = "auto",
        workspace: str = ".",
        access: str = "read_only",
        reasoning: str = "high",
        model: str | None = None,
        acceptance_criteria: list[str] | None = None,
        timeout_seconds: int = 300,
        isolate_write: bool = True,
        group_id: str | None = None,
        depends_on: list[str] | None = None,
        inherit_workspace_from: str | None = None,
        priority: int = 0,
        parent_task_id: str | None = None,
        max_depth: int = 8,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        max_tool_calls: int | None = None,
        max_repeated_calls: int | None = None,
        max_idle_seconds: int | None = None,
        max_runtime_seconds: int | None = None,
        max_child_agents: int | None = None,
        require_changed_files: bool = False,
        require_tests: bool = False,
        require_review: bool = False,
        allowed_paths: list[str] | None = None,
        max_failed_tests: int | None = None,
    ) -> dict[str, Any]:
        """Start an asynchronous workflow task and return its task ID immediately.

        Use this only when task-level workflow guarantees are needed: DAG dependencies,
        scheduler/priority, budgets, worktree isolation, quality gates, or artifact
        lineage. For independent parallel persistent specialists, use agent_start_async
        instead. Never use delegate_async as a visibility/retry fallback for a still-
        running direct AgentSession; watch it with agent_watch and inspect agent_result.
        """
        task = _task_from_values(
            objective=objective, role=role, profile=profile, preset=preset,
            provider=provider, workspace=workspace, access=access,
            reasoning=reasoning, model=model,
            acceptance_criteria=acceptance_criteria or [],
            timeout_seconds=timeout_seconds,
            isolate_write=isolate_write,
            max_tokens=max_tokens,
            max_cost=max_cost,
            max_tool_calls=max_tool_calls,
            max_repeated_calls=max_repeated_calls,
            max_idle_seconds=max_idle_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_child_agents=max_child_agents,
            require_changed_files=require_changed_files,
            require_tests=require_tests,
            require_review=require_review,
            allowed_paths=allowed_paths or [],
            max_failed_tests=max_failed_tests,
        )
        return async_tasks.submit(
            task,
            group_id=group_id,
            depends_on=tuple(depends_on or ()),
            inherit_workspace_from=inherit_workspace_from,
            priority=priority,
            parent_task_id=parent_task_id,
            max_depth=max_depth,
        )

    @server.tool()
    def task_status(task_id: str) -> dict[str, Any]:
        """Return the latest persisted progress for an async delegation."""
        return async_tasks.status(task_id)

    @server.tool()
    def task_events(
        task_id: str, after_sequence: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        """Return observable child activity after a sequence cursor (up to 100 events)."""
        return async_tasks.events(task_id, after_sequence=after_sequence, limit=limit)

    @server.tool()
    def task_wait(task_id: str, timeout_seconds: float = 0) -> dict[str, Any]:
        """Wait for task progress or result (up to 30 seconds per call). If the task is still running, call task_wait again until terminal status is reached."""
        return async_tasks.wait(task_id, timeout_seconds=timeout_seconds)

    @server.tool()
    def task_cancel(task_id: str) -> dict[str, Any]:
        """Request cancellation of an async delegation."""
        return async_tasks.cancel(task_id)

    @server.tool()
    def tasks(workspace: str = ".", group_id: str | None = None) -> list[dict[str, Any]]:
        """List persisted async delegations for a workspace, optionally by group."""
        return async_tasks.tasks(Path(workspace), group_id=group_id)

    @server.tool()
    def task_artifact(task_id: str, create: bool = True) -> dict[str, Any]:
        """Return or create the immutable patch artifact produced by a task."""
        return async_tasks.artifact(task_id, create=create)

    @server.tool()
    def task_output(
        task_id: str,
        offset: int = 0,
        max_chars: int = 65536,
    ) -> dict[str, Any]:
        """Return the complete child final answer, optionally in bounded chunks."""
        return async_tasks.output(
            task_id,
            offset=offset,
            max_chars=max_chars,
        )

    @server.tool()
    def task_worktree(task_id: str, attempt: int | None = None) -> dict[str, Any]:
        """Inspect an isolated worktree recorded for a delegated task."""
        return async_tasks.worktree(task_id, attempt)

    @server.tool()
    def task_diff(task_id: str, attempt: int | None = None,
                  max_bytes: int = 65536) -> dict[str, Any]:
        """Preview a saved worker patch or an inspection-only legacy comparison."""
        return async_tasks.diff(task_id, attempt, max_bytes)

    @server.tool()
    def task_apply(task_id: str, attempt: int | None = None) -> dict[str, Any]:
        """Apply a completed worker's full patch to a clean source checkout."""
        return async_tasks.apply(task_id, attempt)

    @server.tool()
    def task_discard(task_id: str, attempt: int | None = None,
                     confirmation_token: str | None = None) -> dict[str, Any]:
        """Prepare or confirm removal of a completed managed worktree."""
        return async_tasks.discard(task_id, attempt, confirmation_token)

    @server.tool()
    def agent_start(
        objective: str,
        role: str = "reviewer",
        profile: str | None = None,
        preset: str | None = None,
        provider: str = "auto",
        workspace: str = ".",
        access: str = "auto",
        reasoning: str = "high",
        model: str | None = None,
        timeout_seconds: int = 300,
        parent_session_id: str | None = None,
        max_tool_calls: int | None = None,
        max_repeated_calls: int | None = None,
        max_idle_seconds: int | None = None,
        max_runtime_seconds: int | None = None,
        max_child_agents: int | None = None,
    ) -> dict[str, Any]:
        """Start a direct persistent/resumable agent session and await its first turn.

        access=auto inherits the selected profile's configured access. This bypasses
        task DAGs, scheduler queues, worktree isolation, task budgets, quality gates,
        and artifact handoff. Use delegate/delegate_async when those workflow
        guarantees are required. Closed-end providers are rejected here.
        """
        return agent_control.start(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=workspace,
            access=access,
            reasoning=reasoning,
            model=model,
            timeout_seconds=timeout_seconds,
            parent_session_id=parent_session_id,
            max_tool_calls=max_tool_calls,
            max_repeated_calls=max_repeated_calls,
            max_idle_seconds=max_idle_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_child_agents=max_child_agents,
        )

    @server.tool()
    def agent_start_async(
        objective: str,
        role: str = "reviewer",
        profile: str | None = None,
        preset: str | None = None,
        provider: str = "auto",
        workspace: str = ".",
        access: str = "auto",
        reasoning: str = "high",
        model: str | None = None,
        timeout_seconds: int = 300,
        parent_session_id: str | None = None,
        max_tool_calls: int | None = None,
        max_repeated_calls: int | None = None,
        max_idle_seconds: int | None = None,
        max_runtime_seconds: int | None = None,
        max_child_agents: int | None = None,
    ) -> dict[str, Any]:
        """Start a direct persistent/resumable agent concurrently.

        Returns agent_session_id as soon as the Cortex session exists, without
        creating a workflow task. Use this for parallel direct specialists and
        consume agent_events with sequence cursors plus agent_wait for completion.
        access=auto inherits the selected profile's configured access. Use
        delegate_async instead when DAG scheduling, task budgets, worktree
        isolation, quality gates, or artifact lineage are required.
        """
        return agent_control.start_async(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=workspace,
            access=access,
            reasoning=reasoning,
            model=model,
            timeout_seconds=timeout_seconds,
            parent_session_id=parent_session_id,
            max_tool_calls=max_tool_calls,
            max_repeated_calls=max_repeated_calls,
            max_idle_seconds=max_idle_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_child_agents=max_child_agents,
        )

    @server.tool()
    def agent_wait(
        session_id: str,
        timeout_seconds: float = 0,
    ) -> dict[str, Any]:
        """Poll or briefly wait (at most five seconds) for an agent turn.

        Returns durable session state and the complete normalized turn result once
        available. Prefer agent_events(after_sequence=...) for live progress.
        """
        return agent_control.wait(
            session_id,
            timeout_seconds=timeout_seconds,
        )

    @server.tool()
    async def agent_watch(
        session_id: str,
        ctx: Context,
        after_sequence: int = 0,
        timeout_seconds: float = 10,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Watch one active direct agent and surface visible progress.

        While this call is open, new semantic events are forwarded through MCP
        progress notifications when the client renders them. The returned updates[]
        always contains the same human-readable activity for clients that do not.
        A running direct session is authoritative: do not replace it with a new
        delegate/delegate_async task merely because no update arrived yet.
        """
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")

        deadline = asyncio.get_running_loop().time() + min(timeout_seconds, 30.0)
        cursor = after_sequence
        raw_events: list[dict[str, Any]] = []
        updates: list[str] = []

        while True:
            payload = agent_control.events(
                session_id,
                after_sequence=cursor,
                limit=max(1, limit - len(raw_events)),
            )
            new_events = payload["events"]
            if new_events:
                raw_events.extend(new_events)
                cursor = payload["next_sequence"]
                for event in new_events:
                    update = agent_control.format_event(event)
                    if update:
                        updates.append(update)
                        try:
                            await ctx.report_progress(
                                progress=float(cursor),
                                total=None,
                                message=update,
                            )
                        except Exception:
                            pass

            session = agent_control.get(session_id)
            complete = session["state"] not in {"starting", "running"}
            if complete or len(raw_events) >= limit:
                break
            if timeout_seconds == 0 or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.15)

        if not updates and not complete:
            updates.append("Agent is still running; no new semantic event yet.")

        return {
            "agent_session_id": session_id,
            "status": session["state"],
            "complete": complete,
            "authoritative_session": True,
            "replacement_recommended": False,
            "events": raw_events,
            "updates": updates,
            "next_sequence": cursor,
            "result": agent_control.result(session_id) if complete else None,
        }

    @server.tool()
    def agent_result(session_id: str) -> dict[str, Any] | None:
        """Return the latest durable completed-turn result for an agent session."""
        return agent_control.result(session_id)

    @server.tool()
    def agents(
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List durable CortexRelay agent sessions, optionally by parent or root."""
        return agent_control.list(
            parent_session_id=parent_session_id,
            root_session_id=root_session_id,
        )

    @server.tool()
    def agent_get(session_id: str) -> dict[str, Any]:
        """Return one provider-backed agent session and its native session handle."""
        return agent_control.get(session_id)

    @server.tool()
    def agent_events(
        session_id: str,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return durable semantic agent events including text, tools and child updates."""
        return agent_control.events(
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    @server.tool()
    def agent_messages(
        session_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return durable host/agent messages using a monotonic sequence cursor."""
        return agent_control.messages(
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    @server.tool()
    def agent_children(session_id: str) -> list[dict[str, Any]]:
        """Return provider-native and Cortex-managed child sessions."""
        return agent_control.children(session_id)

    @server.tool()
    def agent_send(
        session_id: str,
        message: str,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Send a follow-up to a resumable provider session and return its full result."""
        return agent_control.send(
            session_id,
            message,
            timeout_seconds=timeout_seconds,
        )

    @server.tool()
    def agent_cancel(session_id: str) -> dict[str, Any]:
        """Request provider-backed cancellation of the active direct agent turn.

        Cancellation is delivered to the owning provider process/app-server when
        this CortexRelay runtime owns the turn. After a runtime restart, the
        cancellation intent is persisted and lease expiry/reconciliation prevents
        the session from remaining a zombie indefinitely.
        """
        return agent_control.cancel(session_id)

    @server.tool()
    def agent_close(session_id: str) -> dict[str, Any]:
        """Close the CortexRelay handle for an agent session."""
        return agent_control.close(session_id)

    return server


def run_mcp(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    registry: ProviderRegistry | None = None,
) -> None:
    runtime = registry or default_registry()
    async_tasks = TaskService(runtime)
    agent_control = AgentService(runtime)
    server = create_server(
        runtime,
        async_tasks=async_tasks,
        agent_control=agent_control,
    )
    try:
        if transport == "stdio":
            server.run("stdio")
        elif transport == "streamable-http":
            server.run("streamable-http", host=host, port=port)
        else:
            raise ValueError(f"unsupported MCP transport: {transport}")
    finally:
        agent_control.shutdown()
        async_tasks.shutdown()


def _task_from_mapping(data: dict[str, Any]) -> TaskSpec:
    return _task_from_values(
        objective=str(data.get("objective", "")),
        role=str(data.get("role", "reviewer")),
        profile=data.get("profile") if isinstance(data.get("profile"), str) else None,
        preset=data.get("preset") if isinstance(data.get("preset"), str) else None,
        provider=str(data.get("provider", "auto")),
        workspace=str(data.get("workspace", ".")),
        access=str(data.get("access", "read_only")),
        reasoning=str(data.get("reasoning", "high")),
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        acceptance_criteria=[
            str(item)
            for item in data.get("acceptance_criteria", [])
            if isinstance(item, str)
        ],
        timeout_seconds=int(data.get("timeout_seconds", 300)),
        isolate_write=bool(data.get("isolate_write", True)),
        max_tokens=(
            int(data["max_tokens"]) if data.get("max_tokens") is not None else None
        ),
        max_cost=(
            float(data["max_cost"]) if data.get("max_cost") is not None else None
        ),
        max_tool_calls=(
            int(data["max_tool_calls"]) if data.get("max_tool_calls") is not None else None
        ),
        max_repeated_calls=(
            int(data["max_repeated_calls"]) if data.get("max_repeated_calls") is not None else None
        ),
        max_idle_seconds=(
            int(data["max_idle_seconds"]) if data.get("max_idle_seconds") is not None else None
        ),
        max_runtime_seconds=(
            int(data["max_runtime_seconds"]) if data.get("max_runtime_seconds") is not None else None
        ),
        max_child_agents=(
            int(data["max_child_agents"]) if data.get("max_child_agents") is not None else None
        ),
        require_changed_files=bool(data.get("require_changed_files", False)),
        require_tests=bool(data.get("require_tests", False)),
        require_review=bool(data.get("require_review", False)),
        allowed_paths=[
            str(item) for item in data.get("allowed_paths", [])
            if isinstance(item, str)
        ],
        max_failed_tests=(
            int(data["max_failed_tests"])
            if data.get("max_failed_tests") is not None else None
        ),
    )


def _task_from_values(
    *,
    objective: str,
    role: str,
    profile: str | None,
    preset: str | None,
    provider: str,
    workspace: str,
    access: str,
    reasoning: str,
    model: str | None,
    acceptance_criteria: list[str],
    timeout_seconds: int,
    isolate_write: bool,
    max_tokens: int | None = None,
    max_cost: float | None = None,
    max_tool_calls: int | None = None,
    max_repeated_calls: int | None = None,
    max_idle_seconds: int | None = None,
    max_runtime_seconds: int | None = None,
    max_child_agents: int | None = None,
    require_changed_files: bool = False,
    require_tests: bool = False,
    require_review: bool = False,
    allowed_paths: list[str] | None = None,
    max_failed_tests: int | None = None,
) -> TaskSpec:
    if access not in {"read_only", "workspace_write"}:
        raise ValueError("access must be read_only or workspace_write")
    return TaskSpec(
        objective=objective,
        role=role,
        profile=profile,
        preset=preset,
        provider=provider,
        workspace=Path(workspace),
        access=access,  # type: ignore[arg-type]
        reasoning=reasoning,  # type: ignore[arg-type]
        model=model,
        acceptance_criteria=tuple(acceptance_criteria),
        timeout_seconds=timeout_seconds,
        isolate_write=access == "workspace_write" and isolate_write,
        budget=TaskBudget(
            max_tokens=max_tokens,
            max_cost=max_cost,
            max_tool_calls=max_tool_calls,
            max_repeated_calls=max_repeated_calls,
            max_idle_seconds=max_idle_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_child_agents=max_child_agents,
        ),
        quality_gates=QualityGates(
            require_changed_files=require_changed_files,
            require_tests=require_tests,
            require_review=require_review,
            allowed_paths=tuple(allowed_paths or ()),
            max_failed_tests=max_failed_tests,
        ),
    )
