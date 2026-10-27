"""Deterministic guardrail validation without workflow execution dependencies."""

from __future__ import annotations

import re


def validate_deterministic_guardrail_config(
    *, enabled: bool, mode: str, rule_type: str, config: dict
) -> None:
    """Share the existing save contract with registry activation writes."""
    if mode != "deterministic":
        return
    config_keys = {"forbidden_patterns": "patterns", "required_sections": "sections"}
    config_key = config_keys.get(rule_type)
    if config_key is None:
        supported = ", ".join(sorted(config_keys))
        raise ValueError(
            f"Unsupported deterministic rule type '{rule_type}'. Supported types: {supported}"
        )
    values = config.get(config_key, [])
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError(f"config.{config_key} must be a list of strings")
    if any(not item.strip() for item in values):
        raise ValueError(f"config.{config_key} entries must be non-blank strings")
    if enabled and not values:
        singular = "pattern" if config_key == "patterns" else "section"
        raise ValueError(f"Enabled {rule_type} requires at least one {singular}")
    if config_key == "patterns":
        for pattern in values:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(
                    f"config.patterns contains an invalid regular expression: {exc}"
                ) from exc
