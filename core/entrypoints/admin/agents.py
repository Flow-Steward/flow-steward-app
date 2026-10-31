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
from core.application.management.errors import ServiceError
from core.application.management.query_models import (
    AgentRecordView,
    AgentValidationView,
)
from core.application.registry.contract_validation import validate_agent_skill_compatibility
from core.entrypoints.admin._registry_scope import assert_row_in_account_scope, request_scope
from core.entrypoints.admin._service_errors import raise_service_http_error, service_db_getter
from core.entrypoints.admin.agents_helpers import _get_agent_validation, _primary_skill_id
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
    # Late-bound lambdas keep module attributes (``_get_db``, ``_get_registry``,
    # ``_get_agent_validation``) as the single substitution points.
    return AgentCommandService(
        get_db=service_db_getter(lambda: _get_db()),
        reload_registry=lambda: _get_registry().reload_all(),
        get_agent_validation=lambda db, agent_id, **kwargs: _get_agent_validation(
            db, agent_id, **kwargs
        ),
        validate_skill_compatibility=lambda db, agent_id, skill_id: (
            validate_agent_skill_compatibility(db, agent_id, skill_id)
        ),
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
    try:
        return _agent_command_service().list_agents(
            scope=request_scope(request),
            enabled_only=enabled_only,
            search=search,
            usage=usage,
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


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
    try:
        return _agent_command_service().get_agent(agent_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


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
    try:
        return _agent_command_service().validate_agent(agent_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


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
    try:
        return _agent_command_service().create_agent(
            body.model_dump(), scope=request_scope(request)
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


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
    try:
        return _agent_command_service().update_agent(
            agent_id, body.model_dump(), scope=request_scope(request)
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


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
    try:
        return _agent_command_service().patch_agent(
            agent_id, body.model_dump(exclude_none=True), scope=request_scope(request)
        )
    except ServiceError as exc:
        raise_service_http_error(exc)


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
    try:
        return _agent_command_service().toggle_agent(agent_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


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
    try:
        return _agent_command_service().delete_agent(agent_id, scope=request_scope(request))
    except ServiceError as exc:
        raise_service_http_error(exc)


register_agent_history_routes(
    router,
    get_db=lambda: _get_db(),
    get_registry=lambda: _get_registry(),
    get_agent_validation=lambda db, agent_id: _get_agent_validation(db, agent_id),
    get_scope=request_scope,
    assert_in_scope=assert_row_in_account_scope,
    clone_agent_command=lambda **kwargs: _agent_command_service().clone_agent(**kwargs),
    restore_agent_command=lambda **kwargs: _agent_command_service().restore_agent_revision(
        **kwargs
    ),
)
register_agent_relationship_routes(
    router,
    get_db=lambda: _get_db(),
    get_agent_commands=lambda: _agent_command_service(),
)
register_agent_io_routes(
    router,
    get_db=lambda: _get_db(),
    get_registry=lambda: _get_registry(),
    primary_skill_id=lambda agent: _primary_skill_id(agent),
    get_agent_commands=lambda: _agent_command_service(),
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
    "_get_agent_validation",
    "_primary_skill_id",
    "list_agents",
    "router",
    "validate_agent_skill_compatibility",
]
