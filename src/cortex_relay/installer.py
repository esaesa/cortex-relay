from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .configurator import ConfigValues, render_agent_file, upsert_codex_config, upsert_managed_markdown, write_text
from .templates import AGENTS, ORCHESTRATION_BLOCK


@dataclass(frozen=True)
class InstallResult:
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


def install(
    *,
    scope: str,
    project_dir: Path,
    values: ConfigValues,
    dry_run: bool = False,
) -> InstallResult:
    if scope not in {"project", "user"}:
        raise ValueError("scope must be 'project' or 'user'")

    project_dir = project_dir.resolve()
    if scope == "project":
        config_dir = project_dir / ".codex"
        instructions_path = project_dir / "AGENTS.md"
    else:
        config_dir = Path.home() / ".codex"
        instructions_path = config_dir / "AGENTS.md"

    config_path = config_dir / "config.toml"
    existing_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new_config = upsert_codex_config(existing_config, values)

    existing_instructions = instructions_path.read_text(encoding="utf-8") if instructions_path.exists() else ""
    new_instructions = upsert_managed_markdown(existing_instructions, ORCHESTRATION_BLOCK)

    agent_paths: list[Path] = []
    agent_contents: list[tuple[Path, str]] = []
    for agent in AGENTS:
        path = config_dir / "agents" / f"{agent.name}.toml"
        content = render_agent_file(
            agent.name,
            agent.description,
            agent.sandbox_mode,
            agent.instructions,
            model=values.worker_model,
            effort=values.worker_effort,
        )
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
        config_path=config_path,
        instructions_path=instructions_path,
        agent_paths=tuple(agent_paths),
        backups=tuple(backups),
    )
