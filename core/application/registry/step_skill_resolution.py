"""Which skill an authored agent step will actually run, whoever owns the agent.

An extension's agents and skills are advertised through the same catalog as the
host's own — `GET /api/agents` and `GET /api/skills` return both — but they live
in `extension_registry_projections`, not in `registry_agents`/`registry_skills`.
Two authoring paths each resolved them their own way and both got it wrong: the
incremental route looked only in `registry_agent_skills` and told the author the
agent "has no primary skill", while bulk creation wrote the projected id into
`registry_workflow_steps.skill_id`, whose foreign key points at `registry_skills`,
and the request died as an empty 500.

Row lookup itself already had one owner, `get_registry_artifact_row`. What was
missing is the question above, and the one below it: whether an id may enter a
column that can only hold host-owned rows.
"""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.workflow_lookup import get_registry_artifact_row

__all__ = [
    "agent_primary_skill_id",
    "catalog_agent_row",
    "catalog_skill_row",
    "skill_is_host_owned",
]


def catalog_skill_row(
    db: Any, skill_id: str, *, account_id: str = DEFAULT_ACCOUNT_ID
) -> dict[str, Any] | None:
    """Return the skill as the catalog shows it, host-owned or projected."""
    return get_registry_artifact_row(
        db,
        artifact_kind="skill",
        id_key="skill_id",
        artifact_id=skill_id,
        platform_getter="get_skill",
        account_id=account_id,
    )


def catalog_agent_row(
    db: Any, agent_id: str, *, account_id: str = DEFAULT_ACCOUNT_ID
) -> dict[str, Any] | None:
    """Return the agent as the catalog shows it, host-owned or projected."""
    return get_registry_artifact_row(
        db,
        artifact_kind="agent",
        id_key="agent_id",
        artifact_id=agent_id,
        platform_getter="get_agent",
        account_id=account_id,
    )


def _first_skill_id(values: Any) -> str:
    for value in values or []:
        if isinstance(value, dict):
            candidate = str(value.get("skill_id") or value.get("id") or "").strip()
        else:
            candidate = str(value or "").strip()
        if candidate:
            return candidate
    return ""


def agent_primary_skill_id(db: Any, agent_id: str, *, account_id: str = DEFAULT_ACCOUNT_ID) -> str:
    """Return the skill an agent step will run.

    A step never chooses a skill: the agent's base skill, or the first bound to
    it, is what runs. Answer that once, for a host-owned agent and a projected
    one alike.
    """
    normalized = str(agent_id or "").strip()
    if not normalized:
        return ""

    bound_getter = getattr(db, "get_agent_skills", None)
    if callable(bound_getter):
        host_skill = _first_skill_id(bound_getter(normalized) or [])
        if host_skill:
            return host_skill

    row = catalog_agent_row(db, normalized, account_id=account_id) or {}
    base_skill = str(row.get("base_skill") or "").strip()
    return base_skill or _first_skill_id(row.get("skills"))


def skill_is_host_owned(db: Any, skill_id: str) -> bool:
    """Whether `registry_workflow_steps.skill_id` may hold this id.

    That column has a foreign key into `registry_skills`. A projected skill is a
    real, advertised skill, but it is not a row there, and writing it is the
    foreign-key failure an author saw as an empty 500.
    """
    normalized = str(skill_id or "").strip()
    if not normalized:
        return False
    getter = getattr(db, "get_skill", None)
    if not callable(getter):
        return False
    try:
        return isinstance(getter(normalized), dict)
    except Exception:  # a store that cannot answer is not a host-owned skill
        return False
