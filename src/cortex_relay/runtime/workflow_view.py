"""Render persisted task groups as a compact dependency graph."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cortex_relay.observability import RunStore


def group_snapshot(
    store: RunStore,
    workspace: Path,
    group_id: str,
) -> dict[str, Any]:
    tasks = store.group_tasks(workspace, group_id)
    by_id = {str(item.get("task_id")): item for item in tasks}
    roots = [
        item for item in tasks
        if not [
            dep for dep in (item.get("depends_on") or [])
            if dep in by_id
        ]
    ]
    return {
        "workspace": str(Path(workspace).expanduser().resolve()),
        "group_id": group_id,
        "tasks": tasks,
        "roots": [item.get("task_id") for item in roots],
    }


def render_group(snapshot: dict[str, Any]) -> str:
    tasks = snapshot.get("tasks") or []
    lines = [
        f"CortexRelay group — {snapshot.get('group_id', '')}",
        "=" * (20 + len(str(snapshot.get("group_id", "")))),
        f"Workspace: {snapshot.get('workspace', '')}",
        "",
    ]
    if not tasks:
        lines.append("No tasks found for this group.")
        return "\n".join(lines)

    by_id = {str(item.get("task_id")): item for item in tasks}
    children: dict[str, list[str]] = {task_id: [] for task_id in by_id}
    roots: list[str] = []
    for task_id, item in by_id.items():
        deps = [
            str(dep) for dep in (item.get("depends_on") or [])
            if str(dep) in by_id
        ]
        if not deps:
            roots.append(task_id)
        for dep in deps:
            children.setdefault(dep, []).append(task_id)

    seen: set[str] = set()

    def emit(task_id: str, prefix: str, connector: str) -> None:
        if task_id in seen:
            lines.append(f"{prefix}{connector}{task_id} ↩")
            return
        seen.add(task_id)
        item = by_id[task_id]
        status = str(item.get("status") or "unknown").upper()
        role = item.get("role") or "task"
        provider = item.get("provider") or "?"
        model = item.get("model")
        label = f"{role} [{status}] {provider}"
        if model:
            label += f"/{model}"
        artifact = item.get("artifact_id")
        inherited = item.get("inherited_artifact_id")
        queue = item.get("queued_reason")
        lines.append(f"{prefix}{connector}{label}")
        if artifact:
            lines.append(f"{prefix}   artifact {artifact}")
        if inherited:
            lines.append(f"{prefix}   inherits {inherited}")
        if queue:
            lines.append(f"{prefix}   queued: {queue}")
        child_ids = children.get(task_id, [])
        for index, child_id in enumerate(child_ids):
            last = index == len(child_ids) - 1
            emit(
                child_id,
                prefix + ("   " if connector == "└─ " else "│  "),
                "└─ " if last else "├─ ",
            )

    for index, root in enumerate(roots):
        emit(root, "", "└─ " if index == len(roots) - 1 else "├─ ")

    for task_id in by_id:
        if task_id not in seen:
            emit(task_id, "", "└─ ")

    return "\n".join(lines)
