from __future__ import annotations

import tomllib

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cortex_relay.core.models import TaskAccess, TaskSpec


@dataclass(frozen=True)
class ExecutionProfile:
    """One named executable provider/model configuration."""

    name: str
    provider: str
    model: str | None = None
    reasoning: str = "high"
    access: TaskAccess = "workspace_write"
    isolate_write: bool = True
    fallbacks: tuple[str, ...] = ()
    fallback_on: tuple[str, ...] = ("unavailable",)
    billing_class: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("profile name must not be empty")
        if not self.provider.strip():
            raise ValueError(f"profile {self.name!r} provider must not be empty")
        if not self.reasoning.strip():
            raise ValueError(f"profile {self.name!r} reasoning must not be empty")
        if self.access not in {"read_only", "workspace_write"}:
            raise ValueError(
                f"profile {self.name!r} access must be read_only or workspace_write"
            )
        object.__setattr__(self, "reasoning", self.reasoning.strip().lower())
        object.__setattr__(
            self,
            "fallbacks",
            tuple(item.strip() for item in self.fallbacks if item.strip()),
        )
        object.__setattr__(
            self,
            "fallback_on",
            tuple(item.strip().lower() for item in self.fallback_on if item.strip()),
        )


@dataclass(frozen=True)
class SchedulerConfig:
    max_workers: int = 4
    provider_limits: dict[str, int] = field(default_factory=dict)
    profile_limits: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("scheduler.max_workers must be positive")
        for label, values in (
            ("scheduler.providers", self.provider_limits),
            ("scheduler.profiles", self.profile_limits),
        ):
            if any(value < 1 for value in values.values()):
                raise ValueError(f"{label} limits must be positive")


@dataclass(frozen=True)
class BudgetConfig:
    max_session_tokens: int | None = None
    max_group_tokens: int | None = None
    max_premium_tasks: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_session_tokens", self.max_session_tokens),
            ("max_group_tokens", self.max_group_tokens),
            ("max_premium_tasks", self.max_premium_tasks),
        ):
            if value is not None and value < 1:
                raise ValueError(f"budgets.{name} must be positive")


@dataclass(frozen=True)
class StateConfig:
    retention_days: int = 30
    max_completed_tasks: int = 1000
    max_event_log_mb: int = 10
    cleanup_on_start: bool = True

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            raise ValueError("state.retention_days must be positive")
        if self.max_completed_tasks < 1:
            raise ValueError("state.max_completed_tasks must be positive")
        if self.max_event_log_mb < 1:
            raise ValueError("state.max_event_log_mb must be positive")


@dataclass(frozen=True)
class RuntimePreset:
    name: str
    roles: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeProfileConfig:
    profiles: dict[str, ExecutionProfile] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    presets: dict[str, RuntimePreset] = field(default_factory=dict)
    active_preset: str | None = None
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    state: StateConfig = field(default_factory=StateConfig)
    sources: tuple[Path, ...] = ()

    def effective_roles(self, preset: str | None = None) -> dict[str, str]:
        selected = preset or self.active_preset
        roles = dict(self.roles)
        if selected:
            try:
                roles.update(self.presets[selected].roles)
            except KeyError as exc:
                raise ValueError(f"unknown CortexRelay preset: {selected}") from exc
        return roles

    def profile_for_task(self, task: TaskSpec) -> ExecutionProfile | None:
        profile_name = task.profile
        if profile_name is None and task.provider == "auto" and task.model is None:
            roles = self.effective_roles(task.preset)
            profile_name = roles.get(task.role)
            if profile_name is None and roles:
                raise ValueError(f"unknown CortexRelay role: {task.role}")
        if profile_name is None:
            return None
        try:
            return self.profiles[profile_name]
        except KeyError as exc:
            raise ValueError(f"unknown CortexRelay profile: {profile_name}") from exc

    def fallback_chain(self, profile: ExecutionProfile) -> tuple[ExecutionProfile, ...]:
        resolved: list[ExecutionProfile] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                cycle = " -> ".join([*seen, name])
                raise ValueError(f"profile fallback cycle detected: {cycle}")
            try:
                current = self.profiles[name]
            except KeyError as exc:
                raise ValueError(
                    f"profile {profile.name!r} references unknown fallback {name!r}"
                ) from exc

            seen.add(name)
            resolved.append(current)
            for fallback in current.fallbacks:
                visit(fallback)
            seen.remove(name)

        visit(profile.name)
        return tuple(resolved)

    def to_dict(self, *, preset: str | None = None) -> dict[str, Any]:
        effective = self.effective_roles(preset)
        return {
            "active_preset": preset or self.active_preset,
            "sources": [str(path) for path in self.sources],
            "roles": effective,
            "profiles": {
                name: {
                    "provider": item.provider,
                    "model": item.model,
                    "reasoning": item.reasoning,
                    "access": item.access,
                    "isolate_write": item.isolate_write,
                    "fallbacks": list(item.fallbacks),
                    "fallback_on": list(item.fallback_on),
                    "billing_class": item.billing_class,
                    "options": item.options,
                }
                for name, item in sorted(self.profiles.items())
            },
            "presets": {
                name: {"roles": dict(sorted(item.roles.items()))}
                for name, item in sorted(self.presets.items())
            },
            "scheduler": {
                "max_workers": self.scheduler.max_workers,
                "providers": dict(sorted(self.scheduler.provider_limits.items())),
                "profiles": dict(sorted(self.scheduler.profile_limits.items())),
            },
            "budgets": {
                "max_session_tokens": self.budgets.max_session_tokens,
                "max_group_tokens": self.budgets.max_group_tokens,
                "max_premium_tasks": self.budgets.max_premium_tasks,
            },
            "state": {
                "retention_days": self.state.retention_days,
                "max_completed_tasks": self.state.max_completed_tasks,
                "max_event_log_mb": self.state.max_event_log_mb,
                "cleanup_on_start": self.state.cleanup_on_start,
            },
        }


class ProfileResolver:
    """Loads user/project profile configuration and resolves a task."""

    def load(self, workspace: Path) -> RuntimeProfileConfig:
        paths: list[Path] = []
        user_path = Path.home() / ".cortex-relay" / "config.toml"
        if user_path.is_file():
            paths.append(user_path)

        project_path = find_project_config(workspace)
        if project_path is not None and project_path != user_path:
            paths.append(project_path)

        merged: dict[str, Any] = {}
        for path in paths:
            with path.open("rb") as handle:
                parsed = tomllib.load(handle)
            if not isinstance(parsed, dict):
                raise ValueError(f"invalid CortexRelay config: {path}")
            merged = _merge_config(merged, parsed)

        return runtime_config_from_mapping(merged, sources=tuple(paths))


def find_project_config(workspace: Path) -> Path | None:
    current = Path(workspace).expanduser().resolve()
    if current.is_file():
        current = current.parent

    for directory in (current, *current.parents):
        candidate = directory / ".cortex-relay" / "config.toml"
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            return None
    return None


def runtime_config_from_mapping(
    data: dict[str, Any],
    *,
    sources: tuple[Path, ...] = (),
) -> RuntimeProfileConfig:
    profiles: dict[str, ExecutionProfile] = {}
    raw_profiles = data.get("profiles", {})
    if raw_profiles is not None and not isinstance(raw_profiles, dict):
        raise ValueError("[profiles] must be a table")

    for name, raw in (raw_profiles or {}).items():
        if not isinstance(raw, dict):
            raise ValueError(f"profile {name!r} must be a table")
        provider = str(raw.get("provider", "")).strip()
        model_value = raw.get("model")
        model = str(model_value).strip() if model_value is not None else None
        access = str(raw.get("access", "workspace_write"))
        fallbacks = _string_tuple(raw.get("fallbacks", ()))
        fallback_on = _string_tuple(raw.get("fallback_on", ("unavailable",)))
        options = raw.get("options", {})
        if not isinstance(options, dict):
            raise ValueError(f"profile {name!r} options must be a table")
        billing_value = raw.get("billing_class")
        billing_class = (
            str(billing_value).strip() if billing_value is not None else None
        )
        profiles[str(name)] = ExecutionProfile(
            name=str(name),
            provider=provider,
            model=model or None,
            reasoning=str(raw.get("reasoning", "high")),
            access=access,  # type: ignore[arg-type]
            isolate_write=bool(raw.get("isolate_write", True)),
            fallbacks=fallbacks,
            fallback_on=fallback_on,
            billing_class=billing_class or None,
            options=dict(options),
        )

    roles = _string_mapping(data.get("roles", {}), "[roles]")

    presets: dict[str, RuntimePreset] = {}
    raw_presets = data.get("presets", {})
    if raw_presets is not None and not isinstance(raw_presets, dict):
        raise ValueError("[presets] must be a table")
    for name, raw in (raw_presets or {}).items():
        if not isinstance(raw, dict):
            raise ValueError(f"preset {name!r} must be a table")
        preset_roles = _string_mapping(raw.get("roles", {}), f"[presets.{name}.roles]")
        orchestrator = raw.get("orchestrator")
        if orchestrator is not None:
            preset_roles["orchestrator"] = str(orchestrator).strip()
        presets[str(name)] = RuntimePreset(name=str(name), roles=preset_roles)

    active_value = data.get("active_preset")
    active_preset = str(active_value).strip() if active_value is not None else None

    raw_scheduler = data.get("scheduler", {})
    if raw_scheduler is None:
        raw_scheduler = {}
    if not isinstance(raw_scheduler, dict):
        raise ValueError("[scheduler] must be a table")
    scheduler = SchedulerConfig(
        max_workers=int(raw_scheduler.get("max_workers", 4)),
        provider_limits=_positive_int_mapping(
            raw_scheduler.get("providers", {}), "[scheduler.providers]"
        ),
        profile_limits=_positive_int_mapping(
            raw_scheduler.get("profiles", {}), "[scheduler.profiles]"
        ),
    )

    raw_budgets = data.get("budgets", {})
    if raw_budgets is None:
        raw_budgets = {}
    if not isinstance(raw_budgets, dict):
        raise ValueError("[budgets] must be a table")
    budgets = BudgetConfig(
        max_session_tokens=_optional_positive_int(raw_budgets.get("max_session_tokens")),
        max_group_tokens=_optional_positive_int(raw_budgets.get("max_group_tokens")),
        max_premium_tasks=_optional_positive_int(raw_budgets.get("max_premium_tasks")),
    )

    raw_state = data.get("state", {})
    if raw_state is None:
        raw_state = {}
    if not isinstance(raw_state, dict):
        raise ValueError("[state] must be a table")
    state = StateConfig(
        retention_days=int(raw_state.get("retention_days", 30)),
        max_completed_tasks=int(raw_state.get("max_completed_tasks", 1000)),
        max_event_log_mb=int(raw_state.get("max_event_log_mb", 10)),
        cleanup_on_start=bool(raw_state.get("cleanup_on_start", True)),
    )

    config = RuntimeProfileConfig(
        profiles=profiles,
        roles=roles,
        presets=presets,
        active_preset=active_preset or None,
        scheduler=scheduler,
        budgets=budgets,
        state=state,
        sources=sources,
    )
    _validate_references(config)
    return config


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_config(existing, value)
        else:
            merged[key] = value
    return merged


def _validate_references(config: RuntimeProfileConfig) -> None:
    references: list[tuple[str, str]] = []
    references.extend((f"role {role}", profile) for role, profile in config.roles.items())
    for preset_name, preset in config.presets.items():
        references.extend(
            (f"preset {preset_name} role {role}", profile)
            for role, profile in preset.roles.items()
        )
    for label, profile_name in references:
        if profile_name not in config.profiles:
            raise ValueError(f"{label} references unknown profile {profile_name!r}")

    for profile in config.profiles.values():
        for fallback in profile.fallbacks:
            if fallback not in config.profiles:
                raise ValueError(
                    f"profile {profile.name!r} references unknown fallback {fallback!r}"
                )

    if config.active_preset and config.active_preset not in config.presets:
        raise ValueError(f"unknown active_preset: {config.active_preset}")


def _string_mapping(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    return {
        str(key): str(item).strip()
        for key, item in value.items()
        if str(item).strip()
    }


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _positive_int_mapping(value: Any, label: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    result: dict[str, int] = {}
    for key, item in value.items():
        parsed = int(item)
        if parsed < 1:
            raise ValueError(f"{label}.{key} must be positive")
        result[str(key)] = parsed
    return result


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed < 1:
        raise ValueError("budget values must be positive")
    return parsed
