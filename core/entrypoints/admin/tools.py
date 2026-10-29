# mypy: ignore-errors
"""
Admin API routes — Tools CRUD with DB backend.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel

from core.application.management import ServiceError, ToolCommandService, ToolQueryService
from core.application.management.command_models import (
    ToolCloneInput,
    ToolMutationResultView,
    ToolPatchInput,
    ToolRestoreRevisionInput,
    ToolSaveInput,
)
from core.application.management.command_models_registry_tools import (
    ToolIdentifier,
    ToolInterfaceInput,
    ToolPathIdentifier,
)
from core.application.management.query_models import (
    ToolDiffView,
    ToolHistoryEntriesView,
    ToolRecordView,
    ToolsHealthView,
    ToolValidationView,
)
from core.application.management.scope import ScopeContext, scope_from_principal
from core.application.registry.catalog_categories import CatalogCategory
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_tool_readiness,
)
from core.application.registry.db_store import RestoreValidationError

DB_BACKEND_NOT_CONFIGURED_VALUE = "DB backend not configured"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools"])

_TOOL_SAVE_REQUEST_EXAMPLE = {
    "tool_id": "github_issue_lookup",
    "display_name": "GitHub Issue Lookup",
    "description": "Fetch issue details by repository and issue number.",
    "enabled": False,
    "category": "developer-tools",
    "prompt": "Look up the requested issue and summarize its current state.",
    "interface": {
        "function_name": "lookup_issue",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    "implementation": {"kind": "python", "module": "extensions.github.tools.issue_lookup"},
    "tags": ["github", "issues"],
}
_TOOL_PATCH_REQUEST_EXAMPLE = {
    "description": "Look up issues and include assignee plus label context.",
    "enabled": True,
    "config": {"timeout_seconds": 15},
}
_TOOL_CLONE_REQUEST_EXAMPLE = {
    "tool_id": "github_issue_lookup_copy",
    "display_name": "GitHub Issue Lookup (Copy)",
}
_TOOL_RESTORE_REQUEST_EXAMPLE = {"entry_id": "91"}

_TOOL_CREATE_RESPONSE_EXAMPLE = {
    "status": "created",
    "tool_id": "github_issue_lookup",
    "validation": {"valid": True, "errors": [], "warnings": []},
}
_TOOL_UPDATE_RESPONSE_EXAMPLE = {
    "status": "updated",
    "tool_id": "github_issue_lookup",
    "validation": {"valid": True, "errors": [], "warnings": []},
}
_TOOL_TOGGLE_RESPONSE_EXAMPLE = {
    "status": "toggled",
    "tool_id": "github_issue_lookup",
    "enabled": True,
}
_TOOL_DELETE_RESPONSE_EXAMPLE = {
    "status": "deleted",
    "tool_id": "github_issue_lookup",
}
_TOOL_CLONE_RESPONSE_EXAMPLE = {
    "status": "cloned",
    "tool_id": "github_issue_lookup_copy",
    "source_tool_id": "github_issue_lookup",
}
_TOOL_RESTORE_RESPONSE_EXAMPLE = {
    "status": "restored",
    "tool_id": "github_issue_lookup",
    "restored_from_entry_id": "91",
    "validation": {"valid": True, "errors": [], "warnings": []},
    "snapshot": {"tool_id": "github_issue_lookup", "display_name": "GitHub Issue Lookup"},
}


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db():
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail=DB_BACKEND_NOT_CONFIGURED_VALUE)
    return rm.db_store


def _get_tool_validation(db, tool_id: str) -> dict:
    return validate_tool_readiness(db, tool_id)


def _get_db_for_service():
    try:
        return _get_db()
    except HTTPException as exc:
        raise ServiceError(
            str(exc.detail or DB_BACKEND_NOT_CONFIGURED_VALUE),
            status_code=int(exc.status_code or 500),
            payload={
                "error": exc.detail
                if isinstance(exc.detail, str)
                else DB_BACKEND_NOT_CONFIGURED_VALUE
            },
        ) from exc


def _tool_query_service() -> ToolQueryService:
    return ToolQueryService(
        get_db=_get_db_for_service,
        get_registry=_get_registry,
        get_tool_validation=_get_tool_validation,
    )


def _tool_command_service() -> ToolCommandService:
    return ToolCommandService(
        get_db=_get_db,
        reload_registry=lambda: _get_registry().reload_all(),
        get_tool_validation=_get_tool_validation,
        format_validation_error_detail=format_validation_error_detail,
    )


def _raise_service_http_error(exc: ServiceError) -> None:
    payload = getattr(exc, "payload", None)
    detail = payload if isinstance(payload, dict) and set(payload.keys()) != {"error"} else str(exc)
    raise HTTPException(status_code=int(getattr(exc, "status_code", 500) or 500), detail=detail)


def _request_scope(request: Request | None, *, default_actor: str = "admin") -> ScopeContext:
    user = (
        getattr(getattr(request, "state", None), "auth_user", None) if request is not None else None
    )
    return scope_from_principal(
        user if isinstance(user, dict) else None,
        actor_id=default_actor,
    )


# --------------------------------------------------------------------------- #
# Pydantic models                                                             #
# --------------------------------------------------------------------------- #


class ToolSave(BaseModel):
    tool_id: ToolIdentifier
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = False
    category: CatalogCategory = ""
    prompt: str = ""
    interface: ToolInterfaceInput = {}
    implementation: dict = {}
    config: dict = {}
    builder_metadata_jsonb: dict = {}
    tags: list[str] = []


class ToolPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    category: CatalogCategory | None = None
    prompt: str | None = None
    interface: ToolInterfaceInput | None = None
    implementation: dict | None = None
    config: dict | None = None
    builder_metadata_jsonb: dict | None = None
    tags: list[str] | None = None


class RestoreRevisionRequest(BaseModel):
    entry_id: str | int


class ToolDeleteExecuteRequest(BaseModel):
    revision: str


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #


@router.get(
    "/health",
    response_model=ToolsHealthView,
    responses={503: {"description": "Service unavailable"}},
)
def tools_health(request: Request):
    """ToolResolver registration status."""
    payload = _tool_query_service().get_health(scope=_request_scope(request))
    return payload.dump()


@router.get(
    "", response_model=list[ToolRecordView], responses={503: {"description": "Service unavailable"}}
)
def list_tools(
    request: Request,
    enabled_only: Annotated[bool, Query()] = False,
    category: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
):
    """List all tools, optionally filtered."""
    try:
        payload = _tool_query_service().list_tools(
            enabled_only=enabled_only,
            category=category or "",
            search=search or "",
            scope=_request_scope(request),
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return [row.dump() for row in payload.tools]


@router.get(
    "/{tool_id}",
    response_model=ToolRecordView,
    responses={503: {"description": "Service unavailable"}},
)
def get_tool(tool_id: str, request: Request):
    """Get a single tool."""
    try:
        payload = _tool_query_service().get_tool(tool_id, scope=_request_scope(request))
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.get(
    "/{tool_id}/contract-validation",
    response_model=ToolValidationView,
    responses={503: {"description": "Service unavailable"}},
)
def get_tool_contract_validation(tool_id: str, request: Request):
    """Validate tool readiness for enable/test/invoke operations."""
    try:
        payload = _tool_query_service().get_contract_validation(
            tool_id, scope=_request_scope(request)
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.get(
    "/{tool_id}/agents",
    response_model=list[str],
    responses={503: {"description": "Service unavailable"}},
)
def get_agents_using_tool(tool_id: str, request: Request):
    """Get list of agent_ids that use this tool (directly assigned)."""
    try:
        return _tool_query_service().list_agents_using_tool(tool_id, scope=_request_scope(request))
    except ServiceError as exc:
        _raise_service_http_error(exc)


@router.post(
    "",
    response_model=ToolMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _TOOL_CREATE_RESPONSE_EXAMPLE}}},
        503: {"description": "Service unavailable"},
    },
)
def create_tool(
    body: Annotated[
        ToolSave,
        Body(
            openapi_examples={
                "createTool": {
                    "summary": "Create a GitHub issue lookup tool.",
                    "value": _TOOL_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
    request: Request,
):
    """Create a new tool."""
    scope = _request_scope(request)
    try:
        payload = _tool_command_service().create_tool(
            ToolSaveInput(**body.model_dump()),
            author=scope.actor_name,
            scope=scope,
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.put(
    "/{tool_id}",
    response_model=ToolMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _TOOL_UPDATE_RESPONSE_EXAMPLE}}},
        503: {"description": "Service unavailable"},
    },
)
def update_tool(
    tool_id: ToolPathIdentifier,
    body: Annotated[
        ToolSave,
        Body(
            openapi_examples={
                "replaceTool": {
                    "summary": "Replace the full tool definition.",
                    "value": _TOOL_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
    request: Request,
):
    """Full update of a tool."""
    scope = _request_scope(request)
    try:
        payload = _tool_command_service().update_tool(
            tool_id,
            ToolSaveInput(**body.model_dump()),
            author=scope.actor_name,
            scope=scope,
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    _get_db().materialize_agents_for_tool(tool_id)
    return payload.dump()


@router.patch(
    "/{tool_id}",
    response_model=ToolMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _TOOL_UPDATE_RESPONSE_EXAMPLE}}},
        503: {"description": "Service unavailable"},
    },
)
def patch_tool(
    tool_id: ToolPathIdentifier,
    body: Annotated[
        ToolPatch,
        Body(
            openapi_examples={
                "patchTool": {
                    "summary": "Update selected tool fields.",
                    "value": _TOOL_PATCH_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
    request: Request,
):
    """Partial update — merge provided fields."""
    scope = _request_scope(request)
    try:
        payload = _tool_command_service().patch_tool(
            tool_id,
            ToolPatchInput(**body.model_dump()),
            author=scope.actor_name,
            scope=scope,
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    _get_db().materialize_agents_for_tool(tool_id)
    return payload.dump()


@router.patch(
    "/{tool_id}/toggle",
    response_model=ToolMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _TOOL_TOGGLE_RESPONSE_EXAMPLE}}},
        503: {"description": "Service unavailable"},
    },
)
def toggle_tool(tool_id: str, request: Request):
    """Toggle a tool's enabled state."""
    try:
        payload = _tool_command_service().toggle_tool(tool_id, scope=_request_scope(request))
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.delete(
    "/{tool_id}",
    response_model=ToolMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _TOOL_DELETE_RESPONSE_EXAMPLE}}},
        503: {"description": "Service unavailable"},
    },
)
def delete_tool(tool_id: str, request: Request):
    """Delete a tool. Fails if referenced by agent or skill relationships."""
    scope = _request_scope(request)
    try:
        payload = _tool_command_service().delete_tool(tool_id, author=scope.actor_name, scope=scope)
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.get("/{tool_id}/delete-plan")
def delete_tool_plan(tool_id: str, request: Request):
    try:
        return _tool_command_service().preview_delete_tool(
            tool_id,
            scope=_request_scope(request),
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)


@router.post("/{tool_id}/delete")
def delete_tool_execute(tool_id: str, body: ToolDeleteExecuteRequest, request: Request):
    scope = _request_scope(request)
    try:
        payload = _tool_command_service().delete_tool_confirmed(
            tool_id,
            revision=body.revision,
            author=scope.actor_name,
            scope=scope,
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


class ToolCloneRequest(BaseModel):
    tool_id: ToolIdentifier | None = None
    display_name: str | None = None


@router.post(
    "/{tool_id}/clone",
    response_model=ToolMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _TOOL_CLONE_RESPONSE_EXAMPLE}}},
        503: {"description": "Service unavailable"},
    },
)
def clone_tool(
    tool_id: str,
    body: Annotated[
        ToolCloneRequest,
        Body(
            openapi_examples={
                "cloneTool": {
                    "summary": "Clone a tool under a new tool id.",
                    "value": _TOOL_CLONE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
    request: Request,
):
    """Clone an existing tool into a new tool_id."""
    scope = _request_scope(request)
    try:
        payload = _tool_command_service().clone_tool(
            tool_id,
            ToolCloneInput(tool_id=body.tool_id, display_name=body.display_name),
            author=scope.actor_name,
            scope=scope,
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.get(
    "/{tool_id}/history",
    response_model=ToolHistoryEntriesView,
    responses={503: {"description": "Service unavailable"}},
)
def get_tool_history(
    tool_id: str, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
):
    try:
        payload = _tool_query_service().list_history(
            tool_id, limit=limit, scope=_request_scope(request)
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.get(
    "/{tool_id}/history/{revision}",
    response_model=ToolRecordView,
    responses={503: {"description": "Service unavailable"}},
)
def get_tool_revision(tool_id: str, revision: int, request: Request):
    try:
        payload = _tool_query_service().get_revision(
            tool_id, revision, scope=_request_scope(request)
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.get(
    "/{tool_id}/diff",
    response_model=ToolDiffView,
    responses={503: {"description": "Service unavailable"}},
)
def get_tool_diff(
    tool_id: str,
    request: Request,
    from_entry_id: Annotated[str | None, Query()] = None,
    to_entry_id: Annotated[str | None, Query()] = None,
):
    try:
        payload = _tool_query_service().get_diff(
            tool_id,
            from_entry_id=from_entry_id,
            to_entry_id=to_entry_id,
            scope=_request_scope(request),
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()


@router.post(
    "/{tool_id}/restore",
    response_model=ToolMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _TOOL_RESTORE_RESPONSE_EXAMPLE}}},
        503: {"description": "Service unavailable"},
    },
)
def restore_tool_revision(
    tool_id: str,
    body: Annotated[
        RestoreRevisionRequest,
        Body(
            openapi_examples={
                "restoreTool": {
                    "summary": "Restore a previously recorded tool revision.",
                    "value": _TOOL_RESTORE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
    request: Request,
):
    scope = _request_scope(request)
    try:
        payload = _tool_command_service().restore_tool_revision(
            tool_id,
            ToolRestoreRevisionInput(entry_id=str(body.entry_id)),
            author=f"{scope.actor_name}_restore",
            restore_validation_error_cls=RestoreValidationError,
            scope=scope,
        )
    except ServiceError as exc:
        _raise_service_http_error(exc)
    return payload.dump()
