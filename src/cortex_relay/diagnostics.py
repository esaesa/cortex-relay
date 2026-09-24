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


def runtime_checks(registry: ProviderRegistry) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    for item in registry.capabilities():
        checks.append(
            DiagnosticCheck(
                name=f"runtime:{item['name']}",
                ok=bool(item["available"]),
                detail=str(item["detail"]),
            )
        )
    return checks
