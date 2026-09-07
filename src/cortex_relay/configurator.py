from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_KEY_RE_TEMPLATE = r"^(?P<indent>\s*){key}\s*=.*$"


@dataclass(frozen=True)
class ConfigValues:
    orchestrator_model: str
    orchestrator_effort: str
    worker_model: str
    worker_effort: str
    max_threads: int


def toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"Unsupported TOML scalar type: {type(value)!r}")


def _section_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    headers: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            headers.append((match.group(1).strip(), index))

    ranges: list[tuple[str, int, int]] = []
    for position, (name, start) in enumerate(headers):
        end = headers[position + 1][1] if position + 1 < len(headers) else len(lines)
        ranges.append((name, start, end))
    return ranges


def _upsert_in_range(lines: list[str], start: int, end: int, values: dict[str, Any]) -> list[str]:
    result = list(lines)
    insertion_index = end

    for key, value in values.items():
        pattern = re.compile(_KEY_RE_TEMPLATE.format(key=re.escape(key)))
        replaced = False
        for index in range(start, insertion_index):
            match = pattern.match(result[index])
            if match:
                result[index] = f"{match.group('indent')}{key} = {toml_literal(value)}\n"
                replaced = True
                break
        if not replaced:
            result.insert(insertion_index, f"{key} = {toml_literal(value)}\n")
            insertion_index += 1
    return result


def upsert_codex_config(existing: str, values: ConfigValues) -> str:
    lines = existing.splitlines(keepends=True)
    if existing and not existing.endswith("\n"):
        lines[-1] += "\n"

    ranges = _section_ranges(lines)
    root_end = ranges[0][1] if ranges else len(lines)
    lines = _upsert_in_range(
        lines,
        0,
        root_end,
        {
            "model": values.orchestrator_model,
            "model_reasoning_effort": values.orchestrator_effort,
        },
    )

    ranges = _section_ranges(lines)
    agents_range = next((item for item in ranges if item[0] == "agents"), None)
    agent_values = {
        "enabled": True,
        "max_concurrent_threads_per_session": values.max_threads,
        "default_subagent_model": values.worker_model,
        "default_subagent_reasoning_effort": values.worker_effort,
        "interrupt_message": True,
    }

    if agents_range:
        _, header, end = agents_range
        lines = _upsert_in_range(lines, header + 1, end, agent_values)
    else:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append("[agents]\n")
        for key, value in agent_values.items():
            lines.append(f"{key} = {toml_literal(value)}\n")

    return "".join(lines)


def render_agent_file(name: str, description: str, sandbox_mode: str, instructions: str, *, model: str, effort: str) -> str:
    escaped_description = description.replace('"', '\\"')
    return (
        f'name = "{name}"\n'
        f'description = "{escaped_description}"\n'
        f'model = "{model}"\n'
        f'model_reasoning_effort = "{effort}"\n'
        f'sandbox_mode = "{sandbox_mode}"\n'
        'developer_instructions = """\n'
        f"{instructions.rstrip()}\n"
        '"""\n'
    )


def upsert_managed_markdown(existing: str, block: str) -> str:
    start_marker = "<!-- cortex-relay:start -->"
    end_marker = "<!-- cortex-relay:end -->"
    managed = f"{start_marker}\n{block.rstrip()}\n{end_marker}\n"

    start = existing.find(start_marker)
    end = existing.find(end_marker)
    if start != -1 and end != -1 and end >= start:
        end += len(end_marker)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].lstrip("\n")
        pieces = [piece for piece in (prefix, managed.rstrip(), suffix.rstrip()) if piece]
        return "\n\n".join(pieces).rstrip() + "\n"

    if not existing.strip():
        return managed
    return existing.rstrip() + "\n\n" + managed


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
