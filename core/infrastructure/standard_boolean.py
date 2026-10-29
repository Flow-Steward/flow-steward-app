"""Strict scalar boolean coercion shared by standard tool adapters."""

from __future__ import annotations


def coerce(value: object) -> bool:
    """Keep transform's explicit text tokens and numeric nonzero semantics."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return value != 0
    raise ValueError(f"{type(value).__name__} is not a boolean")
