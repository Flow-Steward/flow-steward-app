"""Errors and the readiness comparison of agent relationship writes."""

from __future__ import annotations

from typing import Any


class AgentRelationshipConflictError(RuntimeError):
    """The relationship snapshot changed after the editor loaded it."""


class AgentRelationshipValidationError(ValueError):
    """A proposed atomic relationship snapshot violates the agent contract."""

    def __init__(self, validation: dict):
        super().__init__("Agent relationship contract validation failed")
        self.validation = validation


class AgentRelationshipReadinessError(AgentRelationshipValidationError):
    """An in-scope relationship was rejected before its transaction committed."""


def _error_codes(validation: Any) -> set[str]:
    errors = validation.get("errors") if isinstance(validation, dict) else None
    return {
        str(item.get("code") or item.get("message") or "")
        for item in errors or []
        if isinstance(item, dict)
    }


def new_readiness_errors(before: Any, after: Any) -> bool:
    """True when *after* is not valid and has an error *before* did not have."""
    if not isinstance(after, dict) or after.get("valid", False):
        return False
    return bool(_error_codes(after) - _error_codes(before))


__all__ = [
    "AgentRelationshipConflictError",
    "AgentRelationshipReadinessError",
    "AgentRelationshipValidationError",
    "new_readiness_errors",
]
