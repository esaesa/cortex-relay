from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cortex_relay.core.models import TaskSpec
from cortex_relay.core.registry import ProviderRegistry, default_registry


def create_server(registry: ProviderRegistry | None = None) -> Any:
    """Create the optional MCP v2 server without importing MCP at package import time."""

    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            'MCP support is not installed. Install with: pip install "cortex-relay[mcp]"'
        ) from exc

    runtime = registry or default_registry()
    server = MCPServer(
        "CortexRelay",
        instructions=(
            "Delegate bounded coding-agent work to registered external providers. "
            "The calling agent remains responsible for planning and final synthesis."
        ),
    )

    @server.tool()
    def providers() -> list[dict[str, object]]:
        """List registered runtime providers and their current capabilities."""
        return runtime.capabilities()

    @server.tool()
    def delegate(
        objective: str,
        role: str = "reviewer",
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

    return server


def run_mcp(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    registry: ProviderRegistry | None = None,
) -> None:
    server = create_server(registry)
    if transport == "stdio":
        server.run("stdio")
    elif transport == "streamable-http":
        server.run("streamable-http", host=host, port=port)
    else:
        raise ValueError(f"unsupported MCP transport: {transport}")


def _task_from_mapping(data: dict[str, Any]) -> TaskSpec:
    return _task_from_values(
        objective=str(data.get("objective", "")),
        role=str(data.get("role", "reviewer")),
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
        provider=provider,
        workspace=Path(workspace),
        access=access,  # type: ignore[arg-type]
        reasoning=reasoning,  # type: ignore[arg-type]
        model=model,
        acceptance_criteria=tuple(acceptance_criteria),
        timeout_seconds=timeout_seconds,
        isolate_write=access == "workspace_write" and isolate_write,
    )
