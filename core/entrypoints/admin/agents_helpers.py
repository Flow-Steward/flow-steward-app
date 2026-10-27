# mypy: ignore-errors
"""Shared helpers for admin agent routes."""

from __future__ import annotations

from fastapi import HTTPException

from core.application.registry.contract_validation import validate_agent_readiness
from core.infrastructure.security.prompt_safety import find_forbidden_template_paths

_STANDARD_TOOL_ACTION_AGENT_DETAIL = (
    "Standard Tool Actions are available only through connector workflow steps"
)


def _primary_skill_id(agent: dict | None) -> str | None:
    skills = (agent or {}).get("skills") or []
    skill_ids = []
    for item in skills:
        if isinstance(item, dict):
            skill_id = str(item.get("skill_id") or "").strip()
        else:
            skill_id = str(item or "").strip()
        if skill_id:
            skill_ids.append(skill_id)
    if len(skill_ids) == 1:
        return skill_ids[0]
    return None


def _get_agent_validation(db, agent_id: str) -> dict:
    return validate_agent_readiness(db, agent_id)


def _validate_agent_context_template(prompts: dict | None) -> None:
    template = str((prompts or {}).get("context_template") or "")
    if not template:
        return
    blocked = find_forbidden_template_paths(template)
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=(
                "context_template contains forbidden placeholder path(s): " + ", ".join(blocked)
            ),
        )
    if "{{" in template or "{%" in template:
        # The prompt builder renders it in the Jinja sandbox; refuse at save
        # what the sandbox would refuse at run time.
        from core.infrastructure.security.jinja_sandbox import check_context_template

        problem = check_context_template(template)
        if problem is not None:
            raise HTTPException(status_code=400, detail=f"context_template: {problem}")


def _validate_agent_provider_model(data: dict, *, account_id: str, conn) -> None:
    """Validate an agent route against built-in and account-scoped models."""
    config = data.get("config")
    if not isinstance(config, dict):
        return
    provider = str(config.get("provider") or "").strip()
    model = str(config.get("model") or "").strip()
    if not provider and not model:
        return
    try:
        from core.application.settings.model_catalog import (
            validate_provider_model_pair,
        )

        valid, err = validate_provider_model_pair(
            provider,
            model,
            account_id=account_id,
            conn=conn,
        )
        if not valid:
            raise HTTPException(status_code=400, detail=f"Agent model config: {err}")
    except ImportError:
        pass


def _agent_tool_id(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("tool_id") or value.get("id") or value.get("name") or "").strip()
    return str(value or "").strip()


def _reject_standard_tool_actions_for_agent(tools: object) -> None:
    if not isinstance(tools, list):
        return
    standard_tool_ids = [
        tool_id
        for tool_id in (_agent_tool_id(item) for item in tools)
        if tool_id.startswith("std.")
    ]
    if standard_tool_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{_STANDARD_TOOL_ACTION_AGENT_DETAIL}: "
                + ", ".join(dict.fromkeys(standard_tool_ids))
            ),
        )


# --------------------------------------------------------------------------- #
# Pydantic models                                                             #
# --------------------------------------------------------------------------- #


def _compose_agent_skills(payload: dict) -> list[str]:
    explicit = []
    for item in payload.get("skills") or []:
        if isinstance(item, dict):
            skill_id = str(item.get("skill_id") or "").strip()
        else:
            skill_id = str(item or "").strip()
        if skill_id:
            explicit.append(skill_id)
    if explicit:
        return list(dict.fromkeys(explicit))

    base_skill = str(payload.get("base_skill") or "").strip()
    secondary = [str(item or "").strip() for item in (payload.get("secondary_skills") or [])]
    combined = [skill_id for skill_id in [base_skill, *secondary] if skill_id]
    return list(dict.fromkeys(combined))


def _normalize_agent_payload(payload: dict) -> dict:
    normalized = dict(payload or {})
    role_type = str(normalized.get("role_type") or "").strip()
    if role_type:
        normalized["agent_type_key"] = role_type
        if not str(normalized.get("agent_type_id") or "").strip():
            normalized["agent_type_id"] = role_type

    skills = _compose_agent_skills(normalized)
    if skills:
        normalized["skills"] = skills
    if "base_skill" not in normalized:
        normalized["base_skill"] = skills[0] if skills else ""
    if "secondary_skills" not in normalized:
        normalized["secondary_skills"] = skills[1:] if len(skills) > 1 else []

    local_instructions = normalized.get("local_instructions")
    if local_instructions is not None:
        prompts = dict(normalized.get("prompts") or {})
        prompts["local_instructions"] = list(local_instructions or [])
        normalized["prompts"] = prompts
    return normalized
