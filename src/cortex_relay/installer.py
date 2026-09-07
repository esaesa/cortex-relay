from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .configurator import ConfigValues, render_agent_file, upsert_codex_config, upsert_managed_markdown, write_text
from .gemini import GeminiConfigValues, render_gemini_agent_file, upsert_gemini_settings
from .templates import AGENTS, ORCHESTRATION_BLOCK


@dataclass(frozen=True)
class InstallResult:
    provider: str
    config_path: Path
    instructions_path: Path
    agent_paths: tuple[Path, ...]
    backups: tuple[Path, ...]


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.cortex-relay.bak.{timestamp}")
    shutil.copy2(path, backup)
    return backup


def _paths(provider: str, scope: str, project_dir: Path) -> tuple[Path, Path, Path]:
    project_dir = project_dir.resolve()
    if provider == "codex":
        config_dir = project_dir / ".codex" if scope == "project" else Path.home() / ".codex"
        config_path = config_dir / "config.toml"
        instructions_path = project_dir / "AGENTS.md" if scope == "project" else config_dir / "AGENTS.md"
        return config_dir, config_path, instructions_path

    if provider == "gemini":
        config_dir = project_dir / ".gemini" if scope == "project" else Path.home() / ".gemini"
        config_path = config_dir / "settings.json"
        instructions_path = project_dir / "GEMINI.md" if scope == "project" else config_dir / "GEMINI.md"
        return config_dir, config_path, instructions_path

    raise ValueError("provider must be 'codex' or 'gemini'")


def expected_paths(*, provider: str, scope: str, project_dir: Path) -> tuple[Path, ...]:
    if scope not in {"project", "user"}:
        raise ValueError("scope must be 'project' or 'user'")
    config_dir, config_path, instructions_path = _paths(provider, scope, project_dir)
    extension = "toml" if provider == "codex" else "md"
    return (
        config_path,
        instructions_path,
        *(config_dir / "agents" / f"{agent.name}.{extension}" for agent in AGENTS),
    )


def install(
    *,
    scope: str,
    project_dir: Path,
    values: ConfigValues | GeminiConfigValues,
    dry_run: bool = False,
    provider: str = "codex",
) -> InstallResult:
    if scope not in {"project", "user"}:
        raise ValueError("scope must be 'project' or 'user'")

    config_dir, config_path, instructions_path = _paths(provider, scope, project_dir)
    existing_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    if provider == "codex":
        if not isinstance(values, ConfigValues):
            raise TypeError("Codex installation requires ConfigValues")
        new_config = upsert_codex_config(existing_config, values)
    elif provider == "gemini":
        if not isinstance(values, GeminiConfigValues):
            raise TypeError("Gemini installation requires GeminiConfigValues")
        new_config = upsert_gemini_settings(existing_config, values)
    else:
        raise ValueError("provider must be 'codex' or 'gemini'")

    existing_instructions = instructions_path.read_text(encoding="utf-8") if instructions_path.exists() else ""
    new_instructions = upsert_managed_markdown(existing_instructions, ORCHESTRATION_BLOCK)

    agent_paths: list[Path] = []
    agent_contents: list[tuple[Path, str]] = []
    for agent in AGENTS:
        if provider == "codex":
            assert isinstance(values, ConfigValues)
            path = config_dir / "agents" / f"{agent.name}.toml"
            content = render_agent_file(
                agent.name,
                agent.description,
                agent.sandbox_mode,
                agent.instructions,
                model=values.worker_model,
                effort=values.worker_effort,
            )
        else:
            assert isinstance(values, GeminiConfigValues)
            path = config_dir / "agents" / f"{agent.name}.md"
            content = render_gemini_agent_file(agent, model=values.worker_model)
        agent_paths.append(path)
        agent_contents.append((path, content))

    backups: list[Path] = []
    if not dry_run:
        for candidate in (config_path, instructions_path, *(path for path, _ in agent_contents)):
            backup = _backup(candidate)
            if backup:
                backups.append(backup)

        write_text(config_path, new_config)
        write_text(instructions_path, new_instructions)
        for path, content in agent_contents:
            write_text(path, content)

    return InstallResult(
        provider=provider,
        config_path=config_path,
        instructions_path=instructions_path,
        agent_paths=tuple(agent_paths),
        backups=tuple(backups),
    )
