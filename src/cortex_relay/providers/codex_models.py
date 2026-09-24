from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodexModelProfile:
    model: str
    reasoning_efforts: tuple[str, ...]
    description: str


CURRENT_CODEX_MODELS: tuple[CodexModelProfile, ...] = (
    CodexModelProfile(
        model="gpt-6-astra",
        reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
        description="Flagship model for the hardest end-to-end reasoning and coding work.",
    ),
    CodexModelProfile(
        model="gpt-6-sol",
        reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        description="Balanced model for complex coding and agentic workflows.",
    ),
    CodexModelProfile(
        model="gpt-6-luna",
        reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        description="Efficient model for focused, high-volume delegated work.",
    ),
)

_PROFILE_BY_MODEL = {profile.model: profile for profile in CURRENT_CODEX_MODELS}


def known_model_ids() -> tuple[str, ...]:
    return tuple(profile.model for profile in CURRENT_CODEX_MODELS)


def compatibility_error(model: str | None, reasoning: str | None) -> str | None:
    """Validate only model/effort pairs CortexRelay knows about.

    Unknown model IDs and future reasoning names are deliberately passed through
    to Codex CLI so CortexRelay does not become a stale model allowlist.
    """

    if not model or not reasoning:
        return None

    profile = _PROFILE_BY_MODEL.get(model)
    if profile is None:
        return None

    if reasoning not in profile.reasoning_efforts:
        supported = ", ".join(profile.reasoning_efforts)
        return (
            f"{model} does not support reasoning effort {reasoning!r}. "
            f"Known supported efforts: {supported}"
        )
    return None
