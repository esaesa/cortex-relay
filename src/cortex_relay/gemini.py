from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .templates import AgentTemplate


GEMINI_THINKING_LEVELS = ("low", "medium", "high")
ORCHESTRATOR_ALIAS = "cortex-relay-orchestrator"


@dataclass(frozen=True)
class GeminiConfigValues:
    orchestrator_model: str
    orchestrator_thinking: str
    worker_model: str
    explorer_thinking: str = "medium"
    architect_thinking: str = "high"
    implementer_thinking: str = "high"
    tester_thinking: str = "low"
    reviewer_thinking: str = "high"

    def thinking_for(self, agent_name: str) -> str:
        values = {
            "explorer": self.explorer_thinking,
            "architect": self.architect_thinking,
            "implementer": self.implementer_thinking,
            "tester": self.tester_thinking,
            "reviewer": self.reviewer_thinking,
        }
        try:
            return values[agent_name]
        except KeyError as exc:
            raise ValueError(f"Unknown CortexRelay Gemini agent: {agent_name}") from exc


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _thinking_config(level: str) -> dict[str, str]:
    normalized = level.lower()
    if normalized not in GEMINI_THINKING_LEVELS:
        raise ValueError(f"Unsupported Gemini thinking level: {level}")
    return {"thinkingLevel": normalized.upper()}


def upsert_gemini_settings(existing: str, values: GeminiConfigValues) -> str:
    if existing.strip():
        parsed = json.loads(existing)
        if not isinstance(parsed, dict):
            raise ValueError("Gemini settings.json must contain a JSON object")
        settings: dict[str, Any] = parsed
    else:
        settings = {}

    model = _ensure_dict(settings, "model")
    model["name"] = ORCHESTRATOR_ALIAS

    model_configs = _ensure_dict(settings, "modelConfigs")
    custom_aliases = _ensure_dict(model_configs, "customAliases")
    orchestrator_alias = _ensure_dict(custom_aliases, ORCHESTRATOR_ALIAS)
    orchestrator_alias["extends"] = "chat-base"
    orchestrator_model_config = _ensure_dict(orchestrator_alias, "modelConfig")
    orchestrator_model_config["model"] = values.orchestrator_model
    orchestrator_generate = _ensure_dict(orchestrator_model_config, "generateContentConfig")
    orchestrator_thinking = _ensure_dict(orchestrator_generate, "thinkingConfig")
    orchestrator_thinking.update(_thinking_config(values.orchestrator_thinking))

    agents = _ensure_dict(settings, "agents")
    overrides = _ensure_dict(agents, "overrides")
    for agent_name in ("explorer", "architect", "implementer", "tester", "reviewer"):
        override = _ensure_dict(overrides, agent_name)
        override["enabled"] = True
        model_config = _ensure_dict(override, "modelConfig")
        model_config["model"] = values.worker_model
        generate = _ensure_dict(model_config, "generateContentConfig")
        thinking = _ensure_dict(generate, "thinkingConfig")
        thinking.update(_thinking_config(values.thinking_for(agent_name)))

    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def render_gemini_agent_file(agent: AgentTemplate, *, model: str, max_turns: int = 30) -> str:
    description = json.dumps(agent.description, ensure_ascii=False)
    model_value = json.dumps(model, ensure_ascii=False)
    return (
        "---\n"
        f"name: {agent.name}\n"
        f"description: {description}\n"
        "kind: local\n"
        f"model: {model_value}\n"
        f"max_turns: {max_turns}\n"
        "---\n\n"
        f"{agent.instructions.rstrip()}\n"
    )
