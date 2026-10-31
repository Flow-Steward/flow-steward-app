# mypy: ignore-errors
"""Route-level substitution points shared by the admin agent routes.

The agent payload and readiness rules live in
``core.application.management.command_services_agent_rules``.
"""

from __future__ import annotations

from core.application.management.command_services_agent_rules import (
    primary_skill_id as _primary_skill_id,
)
from core.application.registry.contract_validation import validate_agent_readiness


def _get_agent_validation(db, agent_id: str, *, account_id: str | None = None) -> dict:
    return validate_agent_readiness(db, agent_id, account_id=account_id)


__all__ = ["_get_agent_validation", "_primary_skill_id"]
