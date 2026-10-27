"""Admin API routes for first-class workspaces."""

from __future__ import annotations

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.management.scope import normalize_scope
from core.application.workspaces import WorkspaceConflictError, WorkspaceService

_MESSAGE_WORKSPACE_KEY_MUST_MATCH_A_Z_A_Z0_9 = "workspace_key must match ^[a-z][a-z0-9_.-]*$"

WORKSPACE_NOT_FOUND_MESSAGE = "Workspace not found"


router = APIRouter(
    prefix="/api/workspaces",
    tags=["workspaces"],
    responses={
        400: {"description": "Invalid workspace request"},
        404: {"description": "Workspace not found"},
        409: {"description": "Workspace key conflict"},
        503: {"description": "Workspace storage is unavailable"},
    },
)

_WORKSPACE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_WORKSPACE_KEY_SAFE_CHARS_PATTERN = re.compile(r"[^a-z0-9_.-]+")


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db():
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


def _request_scope(request: Request):
    state_scope = getattr(request.state, "scope_context", None)
    auth_user = getattr(request.state, "auth_user", None)
    if state_scope is not None:
        return normalize_scope(state_scope)
    if isinstance(auth_user, dict):
        return normalize_scope(
            auth_user,
            account_id=str(auth_user.get("account_id") or DEFAULT_ACCOUNT_ID).strip(),
            tenant_id=str(auth_user.get("tenant_id") or DEFAULT_TENANT_ID).strip(),
            actor_id=str(auth_user.get("username") or auth_user.get("id") or "").strip() or None,
            principal_type=str(auth_user.get("principal_type") or "").strip() or None,
        )
    return normalize_scope()


def _service() -> WorkspaceService:
    return WorkspaceService(_get_db())


def _workspace_key_from_name(display_name: str) -> str:
    normalized = str(display_name or "").strip().lower().replace(" ", "-")
    normalized = _WORKSPACE_KEY_SAFE_CHARS_PATTERN.sub("-", normalized).strip(".-")
    return normalized or "workspace"


class WorkspaceSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    workspace_key: str = ""
    display_name: str
    description: str = ""
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)


class WorkspacePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_key: str | None = None
    display_name: str | None = None
    description: str | None = None
    metadata_jsonb: dict[str, Any] | None = None


class WorkspaceKeyChangePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_key: str


class WorkspaceKeyChange(WorkspaceKeyChangePreview):
    confirm_workspace_id: str


def _payload(body: WorkspaceSave | WorkspacePatch, *, workspace_id: str | None = None) -> dict:
    raw = body.model_dump(exclude_unset=True)
    display_name = str(raw.get("display_name") or "").strip()
    workspace_key = str(raw.get("workspace_key") or "").strip()
    if not workspace_id and not workspace_key and display_name:
        workspace_key = _workspace_key_from_name(display_name)
    if workspace_key and not _WORKSPACE_KEY_PATTERN.match(workspace_key):
        raise HTTPException(status_code=400, detail=_MESSAGE_WORKSPACE_KEY_MUST_MATCH_A_Z_A_Z0_9)
    payload = dict(raw)
    if workspace_id:
        payload["id"] = workspace_id
    elif raw.get("id"):
        payload["id"] = str(raw.get("id") or "").strip()
    if workspace_key or not workspace_id:
        payload["workspace_key"] = workspace_key
    if display_name:
        payload["display_name"] = display_name
    payload["status"] = "active"
    return payload


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def list_workspaces(
    request: Request,
    search: str = "",
    status: str = "",
    include_archived: Annotated[bool, Query()] = False,
):
    rows = _service().list_workspaces(
        scope=_request_scope(request),
        search=search,
        status=status,
        include_archived=include_archived,
    )
    return {"workspaces": rows}


@router.post(
    "",
    responses={
        400: {"description": "Bad request"},
        409: {"description": "Conflict"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def create_workspace(request: Request, body: WorkspaceSave):
    payload = _payload(body)
    if not str(payload.get("display_name") or "").strip():
        raise HTTPException(status_code=400, detail="display_name is required")
    try:
        return _service().create_workspace(payload, scope=_request_scope(request))
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/{workspace_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def get_workspace(request: Request, workspace_id: UUID):
    try:
        return _service().get_workspace(str(workspace_id), scope=_request_scope(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=WORKSPACE_NOT_FOUND_MESSAGE) from exc


@router.put(
    "/{workspace_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def update_workspace(request: Request, workspace_id: UUID, body: WorkspacePatch):
    payload = _payload(body, workspace_id=str(workspace_id))
    try:
        return _service().save_workspace(payload, scope=_request_scope(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=WORKSPACE_NOT_FOUND_MESSAGE) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{workspace_id}/key-change/preview",
    responses={
        400: {"description": "Invalid workspace key"},
        404: {"description": "Workspace not found"},
    },
)
def preview_workspace_key_change(
    request: Request,
    workspace_id: UUID,
    body: WorkspaceKeyChangePreview,
):
    workspace_key = str(body.workspace_key or "").strip()
    if not _WORKSPACE_KEY_PATTERN.match(workspace_key):
        raise HTTPException(status_code=400, detail=_MESSAGE_WORKSPACE_KEY_MUST_MATCH_A_Z_A_Z0_9)
    try:
        return _service().preview_workspace_key_change(
            str(workspace_id),
            workspace_key,
            scope=_request_scope(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=WORKSPACE_NOT_FOUND_MESSAGE) from exc


@router.post(
    "/{workspace_id}/key-change",
    responses={
        400: {"description": "Invalid workspace request"},
        404: {"description": "Workspace not found"},
        409: {"description": "Workspace key conflict"},
    },
)
def change_workspace_key(
    request: Request,
    workspace_id: UUID,
    body: WorkspaceKeyChange,
):
    workspace_key = str(body.workspace_key or "").strip()
    if not _WORKSPACE_KEY_PATTERN.match(workspace_key):
        raise HTTPException(status_code=400, detail=_MESSAGE_WORKSPACE_KEY_MUST_MATCH_A_Z_A_Z0_9)
    try:
        return _service().change_workspace_key(
            str(workspace_id),
            workspace_key,
            confirm_workspace_id=body.confirm_workspace_id,
            scope=_request_scope(request),
        )
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=WORKSPACE_NOT_FOUND_MESSAGE) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch(
    "/{workspace_id}/restore",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def restore_workspace(request: Request, workspace_id: UUID):
    try:
        return _service().restore_workspace(str(workspace_id), scope=_request_scope(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=WORKSPACE_NOT_FOUND_MESSAGE) from exc


@router.get(
    "/{workspace_id}/projects",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def list_workspace_projects(request: Request, workspace_id: UUID):
    normalized_workspace_id = str(workspace_id)
    try:
        _service().get_workspace(normalized_workspace_id, scope=_request_scope(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=WORKSPACE_NOT_FOUND_MESSAGE) from exc
    return {
        "projects": _service().list_projects(normalized_workspace_id, scope=_request_scope(request))
    }
