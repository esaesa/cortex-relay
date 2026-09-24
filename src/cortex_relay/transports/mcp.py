from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cortex_relay.core.models import TaskSpec
from cortex_relay.core.registry import ProviderRegistry, default_registry
from cortex_relay.runtime.async_tasks import AsyncTaskManager


def create_server(
    registry: ProviderRegistry | None = None,
    *,
    async_tasks: AsyncTaskManager | None = None,
) -> Any:
    """Create the optional MCP v2 server without importing MCP at package import time."""

    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            'MCP support is not installed. Install with: pip install "cortex-relay[mcp]"'
        ) from exc

    runtime = registry or default_registry()
    async_tasks = async_tasks or AsyncTaskManager(runtime)
    server = MCPServer(
        "CortexRelay",
        instructions=(
            "Delegate bounded coding-agent work to registered external providers. "
            "The calling agent remains responsible for planning and final synthesis. "
            "Use delegate_async for longer or parallel work, then task_status, "
            "task_wait, or task_cancel by task ID. Set access=workspace_write "
            "explicitly for implementation tasks. Use status/history to inspect work."
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
        )
        return runtime.execute(task).to_dict()

    @server.tool()
    def delegate_parallel(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute independent delegated tasks concurrently.

        Write-capable tasks are isolated into git worktrees by default.
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
    ) -> dict[str, Any]:
        """Start a delegation and return its task ID immediately."""
        task = _task_from_values(
            objective=objective, role=role, profile=profile, preset=preset,
            provider=provider, workspace=workspace, access=access,
            reasoning=reasoning, model=model,
            acceptance_criteria=acceptance_criteria or [],
            timeout_seconds=timeout_seconds, isolate_write=isolate_write,
        )
        return async_tasks.submit(task)

    @server.tool()
    def task_status(task_id: str) -> dict[str, Any]:
        """Return the latest persisted progress for an async delegation."""
        return async_tasks.status(task_id)

    @server.tool()
    def task_wait(task_id: str, timeout_seconds: float = 0) -> dict[str, Any]:
        """Return the full result when ready, or progress after the wait limit."""
        return async_tasks.wait(task_id, timeout_seconds=timeout_seconds)

    @server.tool()
    def task_cancel(task_id: str) -> dict[str, Any]:
        """Request cancellation of an async delegation."""
        return async_tasks.cancel(task_id)

    @server.tool()
    def tasks(workspace: str = ".") -> list[dict[str, Any]]:
        """List async delegations started by this MCP server for a workspace."""
        return async_tasks.tasks(Path(workspace))

    return server


def run_mcp(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    registry: ProviderRegistry | None = None,
) -> None:
    runtime = registry or default_registry()
    async_tasks = AsyncTaskManager(runtime)
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
    )
