from __future__ import annotations

from collections.abc import Callable
from typing import Any


def require_tool_identifier(value: str) -> str:
    """Require an addressable key without restricting opaque owner naming schemes."""
    if (
        not value
        or value in {".", ".."}
        or any(char.isspace() or not char.isprintable() or char in "/\\" for char in value)
    ):
        raise ValueError("tool identifier must be nonempty and URL-path addressable")
    return value


def normalize_tool_identifier(value: object) -> str:
    return require_tool_identifier(str(value or "").strip())


def unique_text_key(
    requested_key: str | None,
    exists: Callable[[str], Any],
    *,
    max_attempts: int = 1000,
) -> str:
    """Return requested_key or a suffixed variant that does not exist yet."""
    base_key = str(requested_key or "").strip()
    if not base_key:
        raise ValueError("identifier is required")

    candidate = base_key
    for suffix in range(max_attempts + 1):
        if not exists(candidate):
            return candidate
        candidate = f"{base_key}_{suffix + 1}"

    raise ValueError(f"No available identifier for '{base_key}'")
