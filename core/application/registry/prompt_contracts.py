"""Helpers for canonical agent response-contract prompt rules."""

from __future__ import annotations

import re

_RETIRED_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CHECKS", re.compile(r"(?im)^\s*CHECKS:\s*")),
    ("VERDICT", re.compile(r"(?im)^\s*VERDICT:\s*")),
    ("FILES_CHANGED", re.compile(r"(?im)^\s*FILES_CHANGED:\s*")),
    ("VERIFICATION", re.compile(r"(?im)^\s*VERIFICATION:\s*")),
    ("required_output_format", re.compile(r"(?im)^\s*##\s*Required Output Format\b")),
    ("json_response_instruction", re.compile(r"(?i)\b(?:return|respond with)\b.*\bjson object\b")),
    ("schema_version", re.compile(r'(?i)"schema_version"\s*:')),
    ("retired_json_contract", re.compile(r"(?i)return (?:exactly )?one json object")),
)


def strip_agent_output_format(prompts: dict | None) -> dict:
    """Drop deprecated agent-level output_format from stored prompts."""
    data = dict(prompts or {})
    data.pop("output_format", None)
    return data


def retired_output_markers(text: str | None) -> list[str]:
    """Return retired structural output markers found in prompt text."""
    source = str(text or "")
    markers: list[str] = []
    for label, pattern in _RETIRED_MARKERS:
        if pattern.search(source):
            markers.append(label)
    return markers


def validate_skill_prompt(text: str | None) -> list[str]:
    """Reject skill prompts that try to redefine the outer response shape."""
    markers = retired_output_markers(text)
    if not markers:
        return []
    marker_list = ", ".join(markers)
    return [
        "Skill prompts must not define outer response formatting. "
        f"Remove retired markers: {marker_list}."
    ]
