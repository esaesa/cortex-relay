from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .core.registry import ProviderRegistry
from .installer import expected_paths


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def configuration_checks(*, provider: str, scope: str, project_dir: Path) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    for path in expected_paths(provider=provider, scope=scope, project_dir=project_dir):
        checks.append(
            DiagnosticCheck(
                name=f"config:{path}",
                ok=path.exists(),
                detail="present" if path.exists() else "missing",
            )
        )
    return checks


def task_store_checks(store: Any) -> list[DiagnosticCheck]:
    """Verify task-state lock/write paths and surface observed write failures.

    Healthy state roots emit nothing, so existing doctor output is unchanged.
    """
    try:
        store.verify_state_paths()
    except (OSError, RuntimeError, ValueError) as exc:
        return [
            DiagnosticCheck(
                name="state:task-store",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        ]
    report = store.state_failure_report()
    if not report.get("total"):
        return []
    failures = report.get("failures") or {}
    summary = ",".join(f"{name}={count}" for name, count in failures.items())
    return [
        DiagnosticCheck(
            name="state:task-store",
            ok=False,
            detail=f"write_failures={report['total']} ops={summary}",
        )
    ]


def runtime_checks(registry: ProviderRegistry) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    try:
        health = registry.agent_store.health()
        integrity = health.get("integrity") or []
        checks.append(
            DiagnosticCheck(
                name="state:agent-store",
                ok=bool(health.get("ok")),
                detail=(
                    f"schema={health.get('schema_version')}/"
                    f"{health.get('expected_schema_version')} "
                    f"integrity={','.join(str(item) for item in integrity)} "
                    f"path={health.get('db_path')}"
                ),
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        checks.append(
            DiagnosticCheck(
                name="state:agent-store",
                ok=False,
                detail=str(exc),
            )
        )

    for item in registry.capabilities():
        checks.append(
            DiagnosticCheck(
                name=f"runtime:{item['name']}",
                ok=bool(item["available"]),
                detail=str(item["detail"]),
            )
        )
    run_store = getattr(registry, "run_store", None)
    if run_store is not None:
        checks.extend(task_store_checks(run_store))
    return checks
