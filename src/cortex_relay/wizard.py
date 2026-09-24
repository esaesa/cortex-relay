from __future__ import annotations

import json
import shutil
import tomllib

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cortex_relay.core.profiles import runtime_config_from_mapping
from cortex_relay.core.registry import ProviderRegistry, default_registry

ROLES = ("orchestrator", "explorer", "architect", "implementer", "tester", "reviewer")
_REASONING_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass
class WizardIO:
    input_fn: Callable[[str], str] = input
    output_fn: Callable[[str], None] = print

    def ask(self, prompt: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None and default != "" else ""
        value = self.input_fn(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        return value

    def confirm(self, prompt: str, default: bool = True) -> bool:
        marker = "Y/n" if default else "y/N"
        value = self.input_fn(f"{prompt} [{marker}]: ").strip().lower()
        if not value:
            return default
        return value in {"y", "yes", "1", "true"}

    def choose(
        self,
        prompt: str,
        options: list[str],
        *,
        default: str | None = None,
    ) -> str:
        if not options:
            raise ValueError(f"no choices available for {prompt}")
        if default not in options:
            default = options[0]

        self.output_fn(prompt)
        for index, option in enumerate(options, start=1):
            marker = " *" if option == default else ""
            self.output_fn(f"  {index}. {option}{marker}")

        while True:
            raw = self.input_fn(f"Choose [default: {default}]: ").strip()
            if not raw:
                return default
            if raw in options:
                return raw
            try:
                number = int(raw)
            except ValueError:
                self.output_fn("Enter a number or one of the listed values.")
                continue
            if 1 <= number <= len(options):
                return options[number - 1]
            self.output_fn("Choice is out of range.")


@dataclass(frozen=True)
class ProviderCatalog:
    name: str
    models: dict[str, dict[str, Any]]
    reasoning_levels: tuple[str, ...]


def config_path(*, scope: str, workspace: Path) -> Path:
    if scope == "user":
        return Path.home() / ".cortex-relay" / "config.toml"
    if scope != "project":
        raise ValueError("scope must be project or user")
    return Path(workspace).expanduser().resolve() / ".cortex-relay" / "config.toml"


def load_raw_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        parsed = tomllib.load(handle)
    if not isinstance(parsed, dict):
        raise ValueError(f"invalid CortexRelay config: {path}")
    return parsed


def write_raw_config(path: Path, data: dict[str, Any]) -> Path | None:
    runtime_config_from_mapping(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(render_toml(data), encoding="utf-8")
    temp.replace(path)
    return backup


def render_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _render_table(lines, (), data, emit_header=False)
    return "\n".join(lines).rstrip() + "\n"


def _render_table(
    lines: list[str],
    path: tuple[str, ...],
    data: dict[str, Any],
    *,
    emit_header: bool,
) -> None:
    scalars = [(key, value) for key, value in data.items() if not isinstance(value, dict)]
    children = [(key, value) for key, value in data.items() if isinstance(value, dict)]

    if emit_header:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")

    for key, value in scalars:
        lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")

    for key, value in children:
        _render_table(lines, (*path, str(key)), value, emit_header=True)


def _toml_key(value: str) -> str:
    if value and all(char.isalnum() or char in "_-" for char in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if value is None:
        raise ValueError("TOML does not support null values")
    raise ValueError(f"unsupported TOML value: {type(value).__name__}")


def discover_catalogs(
    registry: ProviderRegistry | None = None,
    *,
    refresh: bool = True,
) -> dict[str, ProviderCatalog]:
    runtime = registry or default_registry()
    capabilities = {
        str(item["name"]): item
        for item in runtime.capabilities()
        if bool(item.get("available"))
    }

    catalogs: dict[str, ProviderCatalog] = {}
    for name, item in capabilities.items():
        models = {
            str(model): {}
            for model in (item.get("known_models") or ())
            if str(model).strip()
        }
        reasoning = tuple(
            str(level)
            for level in (item.get("reasoning_levels") or ())
            if str(level).strip()
        )
        catalogs[name] = ProviderCatalog(
            name=name,
            models=models,
            reasoning_levels=reasoning,
        )

    if "opencode" in capabilities:
        from cortex_relay.providers.opencode import OpenCodeAdapter

        adapter = OpenCodeAdapter()
        discovered = adapter.discover_models(refresh=refresh, verbose=True)
        if discovered:
            catalogs["opencode"] = ProviderCatalog(
                name="opencode",
                models=discovered,
                reasoning_levels=(),
            )

    if "antigravity" in capabilities:
        from cortex_relay.providers.antigravity import AntigravityAdapter

        adapter = AntigravityAdapter()
        discovered = adapter.discover_models()
        if discovered:
            catalogs["antigravity"] = ProviderCatalog(
                name="antigravity",
                models=discovered,
                reasoning_levels=("low", "medium", "high"),
            )

    return catalogs


def run_setup(
    *,
    workspace: Path,
    scope: str = "project",
    io: WizardIO | None = None,
    registry: ProviderRegistry | None = None,
) -> Path:
    io = io or WizardIO()
    target = config_path(scope=scope, workspace=workspace)
    existing = load_raw_config(target)

    io.output_fn("CortexRelay setup")
    io.output_fn(f"Configuration: {target}")
    if existing:
        io.output_fn("Existing configuration will be updated; a .bak copy will be kept.")

    catalogs = discover_catalogs(registry)
    if not catalogs:
        raise RuntimeError(
            "No runtime providers were detected. Install/login to OpenCode, Codex, "
            "or Antigravity, then run cortex-relay setup again."
        )

    providers = sorted(catalogs)
    orchestrator = _build_profile(
        io,
        catalogs,
        profile_name="orchestrator",
        label="Orchestrator",
        default_provider="opencode" if "opencode" in catalogs else providers[0],
        default_access="read_only",
    )
    worker = _build_profile(
        io,
        catalogs,
        profile_name="worker",
        label="Default worker",
        default_provider="opencode" if "opencode" in catalogs else providers[0],
        default_access="workspace_write",
    )

    profiles: dict[str, dict[str, Any]] = dict(existing.get("profiles", {}))
    profiles["orchestrator"] = orchestrator
    profiles["worker"] = worker

    roles: dict[str, str] = dict(existing.get("roles", {}))
    defaults = {
        "orchestrator": "orchestrator",
        "explorer": "worker",
        "architect": "orchestrator",
        "implementer": "worker",
        "tester": "worker",
        "reviewer": "worker",
    }

    io.output_fn("")
    io.output_fn("Assign profiles to roles")
    for role in ROLES:
        roles[role] = io.choose(
            role.capitalize(),
            sorted(profiles),
            default=roles.get(role, defaults[role]),
        )

    if io.confirm("Add a fallback profile now?", default=False):
        fallback = _build_profile(
            io,
            catalogs,
            profile_name="fallback",
            label="Fallback",
            default_provider=("codex" if "codex" in catalogs else providers[0]),
            default_access="workspace_write",
        )
        profiles["fallback"] = fallback
        worker_fallbacks = list(profiles["worker"].get("fallbacks", []))
        if "fallback" not in worker_fallbacks:
            worker_fallbacks.append("fallback")
        profiles["worker"]["fallbacks"] = worker_fallbacks

    presets = dict(existing.get("presets", {}))
    presets["default"] = {"roles": dict(roles)}

    data = dict(existing)
    data["active_preset"] = "default"
    data["profiles"] = profiles
    data["roles"] = roles
    data["presets"] = presets

    backup = write_raw_config(target, data)
    io.output_fn("")
    io.output_fn(f"Saved: {target}")
    if backup:
        io.output_fn(f"Backup: {backup}")
    io.output_fn("Run: cortex-relay launch")
    return target


def run_config_editor(
    *,
    workspace: Path,
    scope: str = "project",
    io: WizardIO | None = None,
    registry: ProviderRegistry | None = None,
) -> Path:
    io = io or WizardIO()
    target = config_path(scope=scope, workspace=workspace)
    data = load_raw_config(target)
    if not data:
        io.output_fn("No configuration exists yet; starting setup.")
        return run_setup(
            workspace=workspace,
            scope=scope,
            io=io,
            registry=registry,
        )

    catalogs = discover_catalogs(registry, refresh=False)

    while True:
        profiles = data.setdefault("profiles", {})
        roles = data.setdefault("roles", {})
        presets = data.setdefault("presets", {})

        io.output_fn("")
        io.output_fn("CortexRelay configuration")
        io.output_fn(f"  file: {target}")
        io.output_fn(f"  active preset: {data.get('active_preset', '(none)')}")
        io.output_fn("  roles:")
        for role in ROLES:
            io.output_fn(f"    {role}: {roles.get(role, '(unassigned)')}")

        action = io.choose(
            "What do you want to change?",
            [
                "assign role",
                "edit profile",
                "add profile",
                "set active preset",
                "save and exit",
                "cancel",
            ],
            default="assign role",
        )

        if action == "assign role":
            role = io.choose("Role", list(ROLES), default="reviewer")
            if not profiles:
                io.output_fn("No profiles exist. Add a profile first.")
                continue
            roles[role] = io.choose(
                f"Profile for {role}",
                sorted(profiles),
                default=roles.get(role),
            )
            continue

        if action == "edit profile":
            if not profiles:
                io.output_fn("No profiles exist. Add a profile first.")
                continue
            name = io.choose("Profile", sorted(profiles))
            profiles[name] = _edit_profile(
                io,
                catalogs,
                name=name,
                current=dict(profiles[name]),
            )
            continue

        if action == "add profile":
            name = _profile_name(io, profiles)
            profiles[name] = _build_profile(
                io,
                catalogs,
                profile_name=name,
                label=f"Profile {name}",
                default_provider=(
                    "opencode"
                    if "opencode" in catalogs
                    else (sorted(catalogs)[0] if catalogs else "opencode")
                ),
                default_access="workspace_write",
            )
            continue

        if action == "set active preset":
            if not presets:
                if io.confirm("Create a preset from the current role assignments?", True):
                    name = io.ask("Preset name", "default")
                    presets[name] = {"roles": dict(roles)}
                    data["active_preset"] = name
                continue
            choices = ["(none)", *sorted(presets)]
            chosen = io.choose(
                "Active preset",
                choices,
                default=str(data.get("active_preset") or "(none)"),
            )
            if chosen == "(none)":
                data.pop("active_preset", None)
            else:
                data["active_preset"] = chosen
            continue

        if action == "cancel":
            io.output_fn("No changes saved.")
            return target

        if action == "save and exit":
            runtime_config_from_mapping(data)
            backup = write_raw_config(target, data)
            io.output_fn(f"Saved: {target}")
            if backup:
                io.output_fn(f"Backup: {backup}")
            return target


def _build_profile(
    io: WizardIO,
    catalogs: dict[str, ProviderCatalog],
    *,
    profile_name: str,
    label: str,
    default_provider: str,
    default_access: str,
) -> dict[str, Any]:
    providers = sorted(catalogs)
    provider = io.choose(
        f"{label} provider",
        providers,
        default=default_provider if default_provider in providers else providers[0],
    )
    catalog = catalogs[provider]
    model, metadata = _select_model(io, provider, catalog.models)
    reasoning = _select_reasoning(io, provider, metadata, catalog.reasoning_levels)
    access = io.choose(
        f"{label} access",
        ["read_only", "workspace_write"],
        default=default_access,
    )

    profile: dict[str, Any] = {
        "provider": provider,
        "reasoning": reasoning,
        "access": access,
        "isolate_write": True,
    }
    if model:
        profile["model"] = model
    if provider == "opencode":
        profile["options"] = {"validate_variant": True}
    return profile


def _edit_profile(
    io: WizardIO,
    catalogs: dict[str, ProviderCatalog],
    *,
    name: str,
    current: dict[str, Any],
) -> dict[str, Any]:
    if not catalogs:
        io.output_fn("No live providers detected; keeping provider/model and editing basic fields.")
        current["reasoning"] = io.ask("Reasoning", str(current.get("reasoning", "high")))
        current["access"] = io.choose(
            "Access",
            ["read_only", "workspace_write"],
            default=str(current.get("access", "workspace_write")),
        )
        return current

    providers = sorted(catalogs)
    provider = io.choose(
        f"{name} provider",
        providers,
        default=str(current.get("provider", providers[0])),
    )
    catalog = catalogs[provider]
    model, metadata = _select_model(
        io,
        provider,
        catalog.models,
        default=str(current.get("model", "")) or None,
    )
    reasoning = _select_reasoning(
        io,
        provider,
        metadata,
        catalog.reasoning_levels,
        default=str(current.get("reasoning", "high")),
    )
    access = io.choose(
        "Access",
        ["read_only", "workspace_write"],
        default=str(current.get("access", "workspace_write")),
    )

    updated = dict(current)
    updated["provider"] = provider
    if model:
        updated["model"] = model
    else:
        updated.pop("model", None)
    updated["reasoning"] = reasoning
    updated["access"] = access
    updated.setdefault("isolate_write", True)
    if provider == "opencode":
        options = updated.get("options")
        updated["options"] = dict(options) if isinstance(options, dict) else {}
        updated["options"].setdefault("validate_variant", True)
    return updated


def _select_model(
    io: WizardIO,
    provider: str,
    models: dict[str, dict[str, Any]],
    *,
    default: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    if not models:
        manual = io.ask(
            f"{provider} model ID (blank = provider default)",
            default or "",
        )
        return (manual or None), {}

    names = sorted(models)
    if default and default not in names:
        names = [default, *names]

    while True:
        shown = names[:25]
        io.output_fn(f"{provider} models ({len(names)} available):")
        for index, name in enumerate(shown, start=1):
            marker = " *" if name == default else ""
            io.output_fn(f"  {index}. {name}{marker}")
        if len(names) > len(shown):
            io.output_fn("  ... type /text to filter, or type the full model ID")

        raw = io.ask(
            "Model number, model ID, or /search",
            default if default else (shown[0] if shown else ""),
        )
        if raw in models:
            return raw, models[raw]
        if raw in names:
            return raw, models.get(raw, {})
        if raw.startswith("/"):
            term = raw[1:].strip().lower()
            matches = [name for name in names if term in name.lower()]
            if not matches:
                io.output_fn("No models matched that search.")
                continue
            names = matches
            continue
        try:
            number = int(raw)
        except ValueError:
            io.output_fn("Enter a listed number, full model ID, or /search term.")
            continue
        if 1 <= number <= len(shown):
            selected = shown[number - 1]
            return selected, models.get(selected, {})
        io.output_fn("Choice is out of range.")


def _select_reasoning(
    io: WizardIO,
    provider: str,
    model_metadata: dict[str, Any],
    provider_levels: tuple[str, ...],
    *,
    default: str | None = None,
) -> str:
    levels = sorted(
        _variant_names(model_metadata) or set(provider_levels),
        key=_reasoning_sort_key,
    )
    if not levels:
        return io.ask(f"{provider} reasoning/variant", default or "default")

    chosen_default = default if default in levels else ("high" if "high" in levels else levels[-1])
    return io.choose("Reasoning / variant", levels, default=chosen_default)


def _variant_names(metadata: dict[str, Any]) -> set[str]:
    for key in ("variants", "reasoning_options", "reasoningOptions"):
        value = metadata.get(key)
        if isinstance(value, dict):
            return {str(name) for name in value}
        if isinstance(value, list):
            names: set[str] = set()
            for item in value:
                if isinstance(item, str):
                    names.add(item)
                elif isinstance(item, dict):
                    name = item.get("id") or item.get("name")
                    if isinstance(name, str):
                        names.add(name)
            return names
    return set()


def _reasoning_sort_key(value: str) -> tuple[int, str]:
    try:
        return (_REASONING_ORDER.index(value), value)
    except ValueError:
        return (len(_REASONING_ORDER), value)


def _profile_name(io: WizardIO, profiles: dict[str, Any]) -> str:
    while True:
        name = io.ask("New profile name").strip()
        if not name:
            io.output_fn("Profile name cannot be empty.")
            continue
        if name in profiles:
            io.output_fn("That profile already exists.")
            continue
        return name
