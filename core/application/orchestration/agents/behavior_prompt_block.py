"""Shared prompt block helpers for behavior-aware agent prompts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def _default_trim(text: str, limit: int = 2400) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def build_behavior_prompt_block(
    *,
    project_rules_context: Mapping[str, Any] | None = None,
    trim_text: Callable[[str, int], str] | None = None,
    project_rules_limit: int = 720,
) -> str:
    trim = trim_text or _default_trim
    lines: list[str] = []

    rules_payload = dict(project_rules_context or {})
    rules_excerpt = str(
        rules_payload.get("rules_excerpt") or rules_payload.get("rules_text") or ""
    ).strip()
    if rules_excerpt:
        lines.append("")
        lines.append("Project rules to preserve:")
        lines.append(trim(rules_excerpt, int(project_rules_limit)))

    if not lines:
        return ""
    return "\n\nExecution policy context:\n" + "\n".join(lines)


__all__ = ["build_behavior_prompt_block"]
