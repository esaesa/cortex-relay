from __future__ import annotations

from typing import Any


RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string"},
                    "path": {"type": "string"},
                    "symbol": {"type": "string"},
                    "severity": {"type": "string"},
                },
                "required": ["finding"],
                "additionalProperties": False,
            },
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "commands": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "evidence", "changed_files", "commands", "tests", "risks"],
    "additionalProperties": False,
}
