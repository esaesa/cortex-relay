from __future__ import annotations

import json


def gemini_remote_agent_markdown(
    *,
    name: str,
    agent_card_url: str,
    description: str = "Delegate cross-provider coding tasks through CortexRelay.",
) -> str:
    """Render Gemini CLI remote-subagent configuration for a future A2A endpoint.

    CortexRelay does not yet ship an A2A HTTP server. This helper keeps the
    integration format provider-neutral while the runtime stabilizes.
    """

    if not name or any(char.isspace() for char in name):
        raise ValueError("name must be a non-empty slug")
    if not agent_card_url.startswith(("http://", "https://")):
        raise ValueError("agent_card_url must be an HTTP(S) URL")

    return (
        "---\n"
        "kind: remote\n"
        f"name: {name}\n"
        f"agent_card_url: {agent_card_url}\n"
        "---\n\n"
        f"{description.strip()}\n"
    )


def inline_agent_card_json(card: dict[str, object]) -> str:
    return json.dumps(card, separators=(",", ":"), ensure_ascii=False)
