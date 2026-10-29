# ruff: noqa: B904
# mypy: ignore-errors
"""
Admin API routes — Agents CRUD with DB backend + relationship management.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

if TYPE_CHECKING:
    from core.application.registry.db_store import RegistryDBStore


from fastapi import APIRouter, Body, HTTPException, Query, Request

from core.application.auth.enforce import require_roles
from core.application.management import AgentCommandService
from core.application.management.command_models import AgentMutationResultView
from core.application.management.query_models import (
    AgentRecordView,
    AgentValidationView,
)
from core.application.registry.active_workflow_dependencies import (
    enabled_workflow_dependencies,
)
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_agent_skill_compatibility,
)
from core.application.registry.identifier_keys import unique_text_key
from core.application.registry.projected_artifact_rows import (
    guardrail_assignable_catalog_rows,
    projected_catalog_row_matches,
    projection_aware_catalog_row,
    projection_aware_catalog_rows,
)
from core.entrypoints.admin._readiness import enforce_enabled_save_validation
from core.entrypoints.admin._registry_scope import (
    assert_create_account_scope,
    assert_row_in_account_scope,
    request_scope,
    row_in_account_scope,
)
from core.entrypoints.admin.agents_helpers import (
    _agent_tool_id,
    _compose_agent_skills,
    _get_agent_validation,
    _normalize_agent_payload,
    _primary_skill_id,
    _reject_standard_tool_actions_for_agent,
    _validate_agent_context_template,
    _validate_agent_provider_model,
)
from core.entrypoints.admin.agents_history_routes import register_agent_history_routes
from core.entrypoints.admin.agents_io_routes import register_agent_io_routes
from core.entrypoints.admin.agents_models import (
    AgentCloneRequest,
    AgentInputItem,
    AgentOutputItem,
    AgentPatch,
    AgentRelationshipsReplace,
    AgentSave,
    PromptUpdate,
    ReorderRequest,
    RestoreRevisionRequest,
    SkillAdd,
    ToolAdd,
)
from core.entrypoints.admin.agents_relationship_routes import (
    register_agent_relationship_routes,
)

VIOLATES_FOREIGN_KEY_VALUE = "violates foreign key"


router = APIRouter(prefix="/api/agents", tags=["agents"])

_AGENT_SAVE_REQUEST_EXAMPLE = {
    "agent_id": "release_writer",
    "display_name": "Release Writer",
    "description": "Drafts release notes from validated version inputs.",
    "category": "marketing",
    "enabled": False,
    "prompts": {
        "system": "Write concise release notes for the provided version.",
        "context_template": "Version: {{ inputs.version }}",
    },
    "config": {
        "provider": "openai",
        "model": "gpt-4.1-mini",
    },
    "inputs": {
        "required": [{"name": "version", "type": "text", "description": "Release version."}],
        "optional": [],
    },
    "outputs": ["draft_notes"],
    "skills": [{"skill_id": "draft_release_notes", "skill_prompt": "Use the changelog summary."}],
    "tools": ["github_issue_lookup"],
    "tags": ["release", "writing"],
}
_AGENT_PATCH_REQUEST_EXAMPLE = {
    "description": "Drafts concise release notes for a release manager review.",
    "enabled": True,
    "config": {"model": "gpt-4.1"},
    "tags": ["release", "editorial"],
}
_AGENT_CREATE_RESPONSE_EXAMPLE = {
    "status": "created",
    "agent_id": "release_writer",
    "validation": {"valid": True, "errors": [], "warnings": []},
}
_AGENT_UPDATE_RESPONSE_EXAMPLE = {
    "status": "updated",
    "agent_id": "release_writer",
    "validation": {"valid": True, "errors": [], "warnings": []},
}
_AGENT_TOGGLE_RESPONSE_EXAMPLE = {
    "status": "toggled",
    "agent_id": "release_writer",
    "enabled": True,
}
_AGENT_DELETE_RESPONSE_EXAMPLE = {
    "status": "deleted",
    "agent_id": "release_writer",
}


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db() -> RegistryDBStore:
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


def _agent_command_service() -> AgentCommandService:
    return AgentCommandService(
        get_db=_get_db,
        reload_registry=lambda: _get_registry().reload_all(),
    )


# --------------------------------------------------------------------------- #
# Core CRUD                                                                   #
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=list[AgentRecordView],
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def list_agents(
    request: Request,
    enabled_only: Annotated[bool, Query()] = False,
    search: Annotated[str | None, Query()] = None,
    usage: Annotated[Literal["guardrail"] | None, Query()] = None,
):
    """List all agents — admin role required when auth is enforced."""
    auth_user = getattr(request.state, "auth_user", None)
    if auth_user is not None:
        # Auth middleware ran and authenticated a user — enforce admin-only access.
        try:
            require_roles(auth_user, {"admin"})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    db = _get_db()
    scope = request_scope(request)
    rows = projection_aware_catalog_rows(
        db,
        "agent",
        "agent_id",
        db.list_agents(enabled_only=enabled_only) or [],
        account_id=scope.account_id,
        enabled_only=enabled_only,
    )
    if search:
        canonical_by_id = {
            str(row.get("agent_id") or "").strip(): row
            for row in rows
            if isinstance(row, dict) and str(row.get("agent_id") or "").strip()
        }
        rows = [
            canonical_by_id[agent_id]
            for hit in (db.search_entities(search, entity_type="agent") or [])
            if isinstance(hit, dict)
            and (agent_id := str(hit.get("agent_id") or hit.get("entity_id") or "").strip())
            in canonical_by_id
        ]
        rows.extend(
            row
            for agent_id, row in canonical_by_id.items()
            if agent_id not in {str(item.get("agent_id") or "").strip() for item in rows}
            and projected_catalog_row_matches(
                row,
                search,
                fields=("agent_id", "display_name", "description", "category"),
            )
        )
    rows = [row for row in (rows or []) if row_in_account_scope(row, scope)]
    if usage == "guardrail":
        return guardrail_assignable_catalog_rows(db, rows, account_id=scope.account_id)
    return rows


@router.get(
    "/{agent_id}",
    response_model=AgentRecordView,
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_agent(agent_id: str, request: Request):
    """Get a single agent with skills and tools."""
    db = _get_db()
    scope = request_scope(request)
    agent = projection_aware_catalog_row(
        db,
        "agent",
        "agent_id",
        agent_id,
        db.get_agent(agent_id),
        account_id=scope.account_id,
    )
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    assert_row_in_account_scope(agent, scope, detail=f"Agent '{agent_id}' not found")
    return agent


@router.get(
    "/{agent_id}/contract-validation",
    response_model=AgentValidationView,
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_agent_contract_validation(agent_id: str, request: Request):
    """Validate agent readiness for enable/test/invoke operations."""
    db = _get_db()
    scope = request_scope(request)
    agent = projection_aware_catalog_row(
        db,
        "agent",
        "agent_id",
        agent_id,
        db.get_agent(agent_id),
        account_id=scope.account_id,
    )
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    assert_row_in_account_scope(agent, scope, detail=f"Agent '{agent_id}' not found")
    validation = _get_agent_validation(db, agent_id, account_id=scope.account_id)
    return {"agent_id": agent_id, "validation": validation}


@router.post(
    "",
    response_model=AgentMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _AGENT_CREATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def create_agent(
    request: Request,
    body: Annotated[
        AgentSave,
        Body(
            openapi_examples={
                "createAgent": {
                    "summary": "Create a release-note drafting agent.",
                    "value": _AGENT_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    """Create a new agent."""
    db = _get_db()
    scope = request_scope(request)
    agent_id = unique_text_key(body.agent_id, lambda candidate: db.get_agent(candidate) is not None)

    data = body.model_dump()
    data["agent_id"] = agent_id
    assert_create_account_scope(data, scope)
    # Ownership is transport context, not client-controlled request data.
    data["account_id"] = scope.account_id
    _reject_standard_tool_actions_for_agent(data.get("tools"))
    data = _normalize_agent_payload(data)
    _validate_agent_context_template(data.get("prompts"))
    _validate_agent_provider_model(data, account_id=scope.account_id, conn=db.conn)
    try:
        with db._transaction():
            db.save_agent(data, author=scope.actor_name)
            validation = _get_agent_validation(db, agent_id, account_id=scope.account_id)
            enforce_enabled_save_validation(
                entity_type="agent",
                entity_id=agent_id,
                action="save",
                validation=validation,
                final_enabled=bool(data.get("enabled", False)),
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _get_registry().reload_all()
    return {"status": "created", "agent_id": agent_id, "validation": validation}


@router.put(
    "/{agent_id}",
    response_model=AgentMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _AGENT_UPDATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def update_agent(
    agent_id: str,
    request: Request,
    body: Annotated[
        AgentSave,
        Body(
            openapi_examples={
                "replaceAgent": {
                    "summary": "Replace the full agent definition.",
                    "value": _AGENT_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    """Full update of an agent (replaces all fields)."""
    db = _get_db()
    scope = request_scope(request)
    existing = db.get_agent(agent_id)
    if existing:
        assert_row_in_account_scope(existing, scope, detail=f"Agent '{agent_id}' not found")
    data = body.model_dump()
    assert_create_account_scope(data, scope)
    # A full replacement must not silently move an account-owned agent to the
    # registry store's default account when AgentSave omits ownership fields.
    data["account_id"] = existing.get("account_id") if existing is not None else scope.account_id
    _reject_standard_tool_actions_for_agent(data.get("tools"))
    data = _normalize_agent_payload(data)
    data["agent_id"] = agent_id
    _validate_agent_context_template(data.get("prompts"))
    _validate_agent_provider_model(data, account_id=scope.account_id, conn=db.conn)
    try:
        with db._transaction():
            db.save_agent(data, author=scope.actor_name)
            validation = _get_agent_validation(db, agent_id, account_id=scope.account_id)
            enforce_enabled_save_validation(
                entity_type="agent",
                entity_id=agent_id,
                action="save",
                validation=validation,
                final_enabled=bool(data.get("enabled", False)),
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _get_registry().reload_all()
    return {"status": "updated", "agent_id": agent_id, "validation": validation}


@router.patch(
    "/{agent_id}",
    response_model=AgentMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _AGENT_UPDATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def patch_agent(
    agent_id: str,
    request: Request,
    body: Annotated[
        AgentPatch,
        Body(
            openapi_examples={
                "patchAgent": {
                    "summary": "Update only selected agent fields.",
                    "value": _AGENT_PATCH_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    """Partial update — merge provided fields into existing agent."""
    db = _get_db()
    scope = request_scope(request)
    existing = db.get_agent(agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    assert_row_in_account_scope(existing, scope, detail=f"Agent '{agent_id}' not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"status": "no_change", "agent_id": agent_id}

    if bool(existing.get("enabled", False)) and updates.get("enabled") is False:
        dependencies = enabled_workflow_dependencies(db, agent_ids=[agent_id])
        if dependencies:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Active workflow dependency",
                    "message": (
                        f"Agent '{agent_id}' cannot be disabled while enabled workflows "
                        f"depend on it: {', '.join(dependencies)}. Disable or update those "
                        "workflows first."
                    ),
                    "workflow_ids": dependencies,
                },
            )

    merged = {**existing, **updates}
    merged["agent_id"] = agent_id

    # Normalize skills/tools: get_agent returns list-of-dicts but
    # save_agent expects list-of-IDs.
    if merged.get("skills") and isinstance(merged["skills"][0], dict):
        merged["skills"] = [c["skill_id"] for c in merged["skills"] if isinstance(c, dict)]
    if merged.get("tools") and isinstance(merged["tools"][0], dict):
        merged["tools"] = [t["tool_id"] for t in merged["tools"] if isinstance(t, dict)]

    # Merge config dict (shallow) instead of replacing — preserves fields like
    # action_tools, hard_cap_multiplier that the Config tab doesn't send.
    if "config" in updates and "config" in existing:
        merged["config"] = {**(existing.get("config") or {}), **updates["config"]}
        # Explicit empty route fields mean "inherit the project route". Only
        # remove fields present in this patch so unrelated config is preserved.
        for route_field in ("provider", "model"):
            if (
                route_field in updates["config"]
                and not str(updates["config"].get(route_field) or "").strip()
            ):
                merged["config"].pop(route_field, None)

    # Merge prompts dict (shallow) instead of replacing — preserves examples,
    # context sections, and skill prompts on partial updates.
    if "prompts" in updates and "prompts" in existing:
        merged["prompts"] = {**(existing.get("prompts") or {}), **updates["prompts"]}
    if "local_instructions" in updates:
        merged_prompts = dict(merged.get("prompts") or {})
        merged_prompts["local_instructions"] = list(updates.get("local_instructions") or [])
        merged["prompts"] = merged_prompts
    merged = _normalize_agent_payload(merged)
    _validate_agent_context_template(merged.get("prompts"))
    _validate_agent_provider_model(merged, account_id=scope.account_id, conn=db.conn)

    try:
        with db._transaction():
            db.save_agent(merged, author=scope.actor_name, sync_relationships=False)
            validation = _get_agent_validation(db, agent_id, account_id=scope.account_id)
            enforce_enabled_save_validation(
                entity_type="agent",
                entity_id=agent_id,
                action="save",
                validation=validation,
                final_enabled=bool(merged.get("enabled", False)),
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _get_registry().reload_all()
    return {"status": "updated", "agent_id": agent_id, "validation": validation}


@router.patch(
    "/{agent_id}/toggle",
    response_model=AgentMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _AGENT_TOGGLE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_agent(agent_id: str, request: Request):
    """Toggle an agent's enabled state."""
    db = _get_db()
    current = db.get_agent(agent_id)
    if not current:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    scope = request_scope(request)
    assert_row_in_account_scope(current, scope, detail=f"Agent '{agent_id}' not found")
    if bool(current.get("enabled", False)):
        dependencies = enabled_workflow_dependencies(db, agent_ids=[agent_id])
        if dependencies:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Active workflow dependency",
                    "message": (
                        f"Agent '{agent_id}' cannot be disabled while enabled workflows "
                        f"depend on it: {', '.join(dependencies)}. Disable or update those "
                        "workflows first."
                    ),
                    "workflow_ids": dependencies,
                },
            )
    if not bool(current.get("enabled", False)):
        validation = _get_agent_validation(db, agent_id, account_id=scope.account_id)
        if not validation.get("valid", False):
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="agent",
                    entity_id=agent_id,
                    action="enable",
                    validation=validation,
                ),
            )
    try:
        new_state = db.toggle_agent(agent_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    _get_registry().reload_all()
    return {"status": "toggled", "agent_id": agent_id, "enabled": new_state}


@router.delete(
    "/{agent_id}",
    response_model=AgentMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _AGENT_DELETE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def delete_agent(agent_id: str, request: Request):
    """Delete an agent. Fails if used in workflow steps (FK RESTRICT)."""
    db = _get_db()
    scope = request_scope(request)
    existing = db.get_agent(agent_id)
    if existing:
        assert_row_in_account_scope(existing, scope, detail=f"Agent '{agent_id}' not found")
    try:
        db.delete_agent(agent_id, author=scope.actor_name)
    except Exception as e:
        if isinstance(e, KeyError):
            detail = str(e.args[0] if e.args else e)
            raise HTTPException(status_code=404, detail=detail)
        if VIOLATES_FOREIGN_KEY_VALUE in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"Agent '{agent_id}' is referenced by workflow steps. Remove from workflows first.",
            )
        raise HTTPException(status_code=500, detail=str(e))
    _get_registry().reload_all()
    return {"status": "deleted", "agent_id": agent_id}


register_agent_history_routes(
    router,
    get_db=lambda: _get_db(),
    get_registry=lambda: _get_registry(),
    get_agent_validation=lambda db, agent_id: _get_agent_validation(db, agent_id),
    get_scope=request_scope,
    assert_in_scope=assert_row_in_account_scope,
    clone_agent_command=lambda **kwargs: _agent_command_service().clone_agent(**kwargs),
)
register_agent_relationship_routes(
    router,
    get_db=lambda: _get_db(),
    get_registry=lambda: _get_registry(),
    get_agent_validation=lambda db, agent_id: _get_agent_validation(db, agent_id),
    validate_skill_compatibility=lambda db, agent_id, skill_id: validate_agent_skill_compatibility(
        db, agent_id, skill_id
    ),
    reject_standard_tool_actions=lambda tools: _reject_standard_tool_actions_for_agent(tools),
)
register_agent_io_routes(
    router,
    get_db=lambda: _get_db(),
    get_registry=lambda: _get_registry(),
    primary_skill_id=lambda agent: _primary_skill_id(agent),
)

__all__ = [
    "AgentCloneRequest",
    "AgentInputItem",
    "AgentOutputItem",
    "AgentPatch",
    "AgentRelationshipsReplace",
    "AgentSave",
    "PromptUpdate",
    "ReorderRequest",
    "RestoreRevisionRequest",
    "SkillAdd",
    "ToolAdd",
    "_agent_tool_id",
    "_compose_agent_skills",
    "_get_agent_validation",
    "_normalize_agent_payload",
    "_primary_skill_id",
    "_reject_standard_tool_actions_for_agent",
    "_validate_agent_context_template",
    "_validate_agent_provider_model",
    "list_agents",
    "router",
    "validate_agent_skill_compatibility",
]
