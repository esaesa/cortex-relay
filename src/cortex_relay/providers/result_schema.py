from __future__ import annotations

from typing import Any


# Structured-output strict mode (Codex/OpenAI) requires every property key to
# appear in "required" and optional fields to be explicitly nullable.
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "final_text": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string"},
                    "path": {"type": ["string", "null"]},
                    "symbol": {"type": ["string", "null"]},
                    "severity": {"type": ["string", "null"]},
                },
                "required": ["finding", "path", "symbol", "severity"],
                "additionalProperties": False,
            },
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "commands": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "final_text", "evidence", "changed_files", "commands", "tests", "risks"],
    "additionalProperties": False,
}
