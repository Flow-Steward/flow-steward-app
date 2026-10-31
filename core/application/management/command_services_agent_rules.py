"""Payload and readiness rules shared by the agent and skill command services.

These rules used to live in the admin controllers and raised ``HTTPException``.
They now raise service errors; the controllers translate them to the same HTTP
status codes and details.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.registry.active_workflow_dependencies import (
    enabled_workflow_dependencies,
)
from core.application.registry.contract_validation_workflow_readiness import (
    format_validation_error_detail,
)
from core.infrastructure.security.prompt_safety import find_forbidden_template_paths

from .errors import (
    ACCESS_DENIED_MESSAGE,
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)

_STANDARD_TOOL_ACTION_AGENT_DETAIL = (
    "Standard Tool Actions are available only through connector workflow steps"
)
ACTIVE_WORKFLOW_DEPENDENCY_ERROR = "Active workflow dependency"


def row_in_account_scope(row: Any, scope: Any) -> bool:
    """True if *row* is global (no account_id) or owned by the caller's account.

    Fails closed on anything that is not a mapping. Rows with an empty
    ``account_id`` are platform-global registry objects every scoped admin may
    read; owned rows are visible only to a caller in the same account.
    """
    if row is None or not isinstance(row, dict):
        return False
    row_account_id = str(row.get("account_id") or "").strip()
    if not row_account_id:
        return True
    scope_account_id = str(getattr(scope, "account_id", "") or "").strip()
    if not scope_account_id:
        return False
    return row_account_id == scope_account_id


def require_row_in_account_scope(row: Any, scope: Any, *, detail: str) -> dict[str, Any]:
    """Return *row* when the caller may see it; otherwise a non-enumerating 404."""
    if not row_in_account_scope(row, scope):
        raise ServiceNotFoundError(detail)
    return row


def create_account_scope_allowed(payload: Mapping[str, Any] | None, scope: Any) -> bool:
    """A caller may create a global row or one in its own account, never another's."""
    requested_account_id = str((payload or {}).get("account_id") or "").strip()
    if not requested_account_id:
        return True
    scope_account_id = str(getattr(scope, "account_id", "") or "").strip()
    return not scope_account_id or requested_account_id == scope_account_id


def require_create_account_scope(payload: Mapping[str, Any] | None, scope: Any) -> None:
    if not create_account_scope_allowed(payload, scope):
        raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)


def primary_skill_id(agent: Mapping[str, Any] | None) -> str | None:
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


def validate_agent_context_template(prompts: Mapping[str, Any] | None) -> None:
    template = str((prompts or {}).get("context_template") or "")
    if not template:
        return
    blocked = find_forbidden_template_paths(template)
    if blocked:
        raise ServiceValidationError(
            "context_template contains forbidden placeholder path(s): " + ", ".join(blocked)
        )
    if "{{" in template or "{%" in template:
        # The prompt builder renders it in the Jinja sandbox; refuse at save
        # what the sandbox would refuse at run time.
        from core.infrastructure.security.jinja_sandbox import check_context_template

        problem = check_context_template(template)
        if problem is not None:
            raise ServiceValidationError(f"context_template: {problem}")


def validate_agent_provider_model(data: Mapping[str, Any], *, account_id: str, conn: Any) -> None:
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
            raise ServiceValidationError(f"Agent model config: {err}")
    except ImportError:
        pass


def agent_tool_id(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("tool_id") or value.get("id") or value.get("name") or "").strip()
    return str(value or "").strip()


def reject_standard_tool_actions_for_agent(tools: object) -> None:
    if not isinstance(tools, list):
        return
    standard_tool_ids = [
        tool_id for tool_id in (agent_tool_id(item) for item in tools) if tool_id.startswith("std.")
    ]
    if standard_tool_ids:
        raise ServiceValidationError(
            f"{_STANDARD_TOOL_ACTION_AGENT_DETAIL}: " + ", ".join(dict.fromkeys(standard_tool_ids))
        )


def compose_agent_skills(payload: Mapping[str, Any]) -> list[str]:
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


def normalize_agent_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(payload or {})
    skills = compose_agent_skills(normalized)
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


def contract_validation_error(
    *, entity_type: str, entity_id: str, action: str, validation: Any
) -> ServiceValidationError:
    return ServiceValidationError(
        "Contract validation failed",
        payload=format_validation_error_detail(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            validation=validation,
        ),
    )


def enforce_enabled_save_validation(
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    validation: Mapping[str, Any] | None,
    final_enabled: bool,
) -> None:
    """An enabled registry row may only be saved when its readiness is valid."""
    if not final_enabled:
        return
    if bool((validation or {}).get("valid", False)):
        return
    raise contract_validation_error(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        validation=validation,
    )


def contract_items_error(
    *, entity_type: str, entity_id: str, action: str, issues: list[Any]
) -> ServiceValidationError:
    return contract_validation_error(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        validation={"valid": False, "errors": issues, "warnings": []},
    )


def require_disable_allowed(
    db: Any,
    *,
    agent_ids: list[str],
    message: Callable[[str], str],
) -> None:
    """The one rule for turning an agent or skill off: no enabled workflow may depend on it.

    Both the desired-state ``PATCH {"enabled": false}`` and the ``toggle`` route
    reach this check through their service's enabled-transition guard.
    """
    dependencies = enabled_workflow_dependencies(db, agent_ids=agent_ids)
    if dependencies:
        text = message(", ".join(dependencies))
        raise ServiceConflictError(
            text,
            payload={
                "error": ACTIVE_WORKFLOW_DEPENDENCY_ERROR,
                "message": text,
                "workflow_ids": dependencies,
            },
        )


__all__ = [
    "ACTIVE_WORKFLOW_DEPENDENCY_ERROR",
    "agent_tool_id",
    "compose_agent_skills",
    "contract_items_error",
    "contract_validation_error",
    "create_account_scope_allowed",
    "enforce_enabled_save_validation",
    "normalize_agent_payload",
    "primary_skill_id",
    "reject_standard_tool_actions_for_agent",
    "require_create_account_scope",
    "require_disable_allowed",
    "require_row_in_account_scope",
    "row_in_account_scope",
    "validate_agent_context_template",
    "validate_agent_provider_model",
]
