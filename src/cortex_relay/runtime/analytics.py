"""Read-only routing analytics derived from persisted CortexRelay task history."""

from __future__ import annotations

import statistics

from pathlib import Path
from typing import Any

from cortex_relay.observability import RunStore, TERMINAL_STATUSES


def analyze_routing(
    store: RunStore,
    workspace: Path,
    *,
    min_samples: int = 1,
) -> dict[str, Any]:
    records = store.list_tasks(workspace, limit=100000, completed_only=True)
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("status") not in TERMINAL_STATUSES:
            continue
        key = (
            str(record.get("role") or ""),
            str(record.get("profile") or ""),
            str(record.get("provider") or ""),
            str(record.get("model") or ""),
        )
        buckets.setdefault(key, []).append(record)

    rows: list[dict[str, Any]] = []
    for (role, profile, provider, model), items in buckets.items():
        if len(items) < min_samples:
            continue
        successes = sum(item.get("status") == "success" for item in items)
        fallback_count = sum(len(item.get("attempts") or []) > 1 for item in items)
        durations = [
            float(item["duration_seconds"])
            for item in items
            if isinstance(item.get("duration_seconds"), (int, float))
        ]
        tokens = [
            int((item.get("usage_summary") or {}).get("total_tokens") or 0)
            for item in items
            if isinstance(item.get("usage_summary"), dict)
            and (item.get("usage_summary") or {}).get("total_tokens") is not None
        ]
        costs = [
            float((item.get("usage_summary") or {}).get("cost"))
            for item in items
            if isinstance((item.get("usage_summary") or {}).get("cost"), (int, float))
        ]
        gate_failures = sum(item.get("status") == "failed_gate" for item in items)
        budget_exceeded = sum(
            item.get("status") == "budget_exceeded" for item in items
        )
        rows.append(
            {
                "role": role,
                "profile": profile,
                "provider": provider,
                "model": model or None,
                "samples": len(items),
                "successes": successes,
                "success_rate": successes / len(items),
                "fallback_rate": fallback_count / len(items),
                "gate_failure_rate": gate_failures / len(items),
                "budget_exceeded_rate": budget_exceeded / len(items),
                "median_duration_seconds": (
                    statistics.median(durations) if durations else None
                ),
                "median_total_tokens": statistics.median(tokens) if tokens else None,
                "median_cost": statistics.median(costs) if costs else None,
            }
        )

    rows.sort(
        key=lambda row: (
            row["role"],
            row["profile"],
            row["provider"],
            row["model"] or "",
        )
    )
    return {
        "workspace": str(Path(workspace).expanduser().resolve()),
        "samples": sum(row["samples"] for row in rows),
        "routes": rows,
        "note": (
            "Descriptive historical metrics only. CortexRelay does not automatically "
            "change routing from this report."
        ),
    }


def render_routing_analysis(report: dict[str, Any]) -> str:
    lines = [
        "CortexRelay routing analysis",
        "============================",
        f"Workspace: {report.get('workspace', '')}",
        f"Samples: {report.get('samples', 0)}",
        "",
    ]
    routes = report.get("routes") or []
    if not routes:
        lines.append("No completed routing samples are available.")
        return "\n".join(lines)

    for row in routes:
        label = (
            f"{row.get('role') or '?'} → {row.get('profile') or '?'} → "
            f"{row.get('provider') or '?'}"
        )
        if row.get("model"):
            label += f"/{row['model']}"
        lines.append(label)
        lines.append(
            f"  samples {row['samples']} | success {row['success_rate']:.1%} | "
            f"fallback {row['fallback_rate']:.1%}"
        )
        if row.get("median_duration_seconds") is not None:
            lines.append(
                f"  median duration {float(row['median_duration_seconds']):.1f}s"
            )
        if row.get("median_total_tokens") is not None:
            lines.append(f"  median tokens {int(row['median_total_tokens'])}")
        if row.get("median_cost") is not None:
            lines.append("  median cost $" + f"{float(row['median_cost']):.4f}")
        if row.get("gate_failure_rate"):
            lines.append(
                f"  quality-gate failures {row['gate_failure_rate']:.1%}"
            )
        if row.get("budget_exceeded_rate"):
            lines.append(
                f"  budget exceeded {row['budget_exceeded_rate']:.1%}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()
