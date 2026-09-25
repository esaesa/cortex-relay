from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cortex_relay.core.models import QualityGates, TaskBudget, TaskSpec
from cortex_relay.core.registry import ProviderRegistry, default_registry
from cortex_relay.runtime.task_protocol import TaskControl
from cortex_relay.runtime.task_service import TaskService


def create_server(
    registry: ProviderRegistry | None = None,
    *,
    async_tasks: TaskControl | None = None,
) -> Any:
    """Create the optional MCP v2 server without importing MCP at package import time."""

    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            'MCP support is not installed. Install with: pip install "cortex-relay[mcp]"'
        ) from exc

    runtime = registry or default_registry()
    async_tasks = async_tasks or TaskService(runtime)
    server = MCPServer(
        "CortexRelay",
        instructions=(
            "Delegate bounded coding-agent work to registered external providers. "
            "The calling agent remains responsible for planning and final synthesis. "
            "Use synchronous delegate when you need the worker's output before you can answer the user's immediate request. "
            "Use delegate_async for parallel work or staged pipelines, then poll task_wait until the task reaches terminal status. "
            "Successful results include final_text with the worker's complete answer; for large answers use task_output to retrieve it in chunks. "
            "Never end a turn with uncompleted async tasks when the user is awaiting the outcome. "
            "Set access=workspace_write explicitly for implementation tasks. Use status/history to inspect work."
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
        require_changed_files: bool = False,
        require_tests: bool = False,
        allowed_paths: list[str] | None = None,
        max_failed_tests: int | None = None,
    ) -> dict[str, Any]:
        """Delegate one bounded task and return a normalized structured result."""
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
        require_changed_files: bool = False,
        require_tests: bool = False,
        require_review: bool = False,
        allowed_paths: list[str] | None = None,
        max_failed_tests: int | None = None,
    ) -> dict[str, Any]:
        """Start a delegation and return its task ID immediately."""
        task = _task_from_values(
            objective=objective, role=role, profile=profile, preset=preset,
            provider=provider, workspace=workspace, access=access,
            reasoning=reasoning, model=model,
            acceptance_criteria=acceptance_criteria or [],
            timeout_seconds=timeout_seconds,
            isolate_write=isolate_write,
            max_tokens=max_tokens,
            max_cost=max_cost,
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
    server = create_server(runtime, async_tasks=async_tasks)
    try:
        if transport == "stdio":
            server.run("stdio")
        elif transport == "streamable-http":
            server.run("streamable-http", host=host, port=port)
        else:
            raise ValueError(f"unsupported MCP transport: {transport}")
    finally:
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
        budget=TaskBudget(max_tokens=max_tokens, max_cost=max_cost),
        quality_gates=QualityGates(
            require_changed_files=require_changed_files,
            require_tests=require_tests,
            require_review=require_review,
            allowed_paths=tuple(allowed_paths or ()),
            max_failed_tests=max_failed_tests,
        ),
    )
