"""
Admin API routes — Field catalog CRUD for workflow-visible public fields.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.entrypoints.admin._registry_scope import (
    request_scope,
    require_default_registry_account,
)

router = APIRouter(prefix="/api/fields", tags=["fields"])
_FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ALLOWED_VALUE_TYPES = {"text", "json", "list", "number", "boolean"}


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db():
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


def _validate_field_payload(payload: dict) -> None:
    field_key = str(payload.get("field_key") or "").strip()
    if not field_key:
        raise HTTPException(status_code=400, detail="field_key is required")
    if not _FIELD_KEY_PATTERN.match(field_key):
        raise HTTPException(
            status_code=400,
            detail="field_key must match [a-z][a-z0-9_.-]*",
        )
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")

    value_type = str(payload.get("value_type") or "text").strip().lower()
    if value_type not in _ALLOWED_VALUE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"value_type must be one of: {', '.join(sorted(_ALLOWED_VALUE_TYPES))}",
        )

    source_hints = payload.get("source_hints_json")
    if source_hints is None:
        source_hints = payload.get("source_hints")
    if source_hints is None:
        source_hints = {}
    if not isinstance(source_hints, dict):
        raise HTTPException(status_code=400, detail="source_hints_json must be an object")

    contract_id = str(payload.get("data_contract_id") or "").strip()
    if value_type in {"json", "list"} and not contract_id:
        raise HTTPException(
            status_code=400,
            detail="data_contract_id is required for structured value_type (json/list)",
        )


class FieldSave(BaseModel):
    field_key: str
    display_name: str
    description: str = ""
    value_type: str = "text"
    data_contract_id: str | None = None
    created_from_kind: str = "manual"
    created_from_ref: str = ""
    source_hints_json: dict = {}
    manual_allowed: bool = True
    enabled: bool = True


class FieldPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    value_type: str | None = None
    data_contract_id: str | None = None
    created_from_kind: str | None = None
    created_from_ref: str | None = None
    source_hints_json: dict | None = None
    manual_allowed: bool | None = None
    enabled: bool | None = None


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_fields(
    request: Request,
    enabled_only: Annotated[bool, Query()] = False,
    value_type: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    return db.list_fields(
        enabled_only=enabled_only,
        value_type=value_type,
        search=search,
    )


@router.get(
    "/{field_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def get_field(field_key: str, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    row = db.get_field(field_key)
    if not row:
        raise HTTPException(status_code=404, detail=f"Field '{field_key}' not found")
    return row


@router.post(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def create_field(body: FieldSave, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    if db.get_field(body.field_key):
        raise HTTPException(status_code=409, detail=f"Field '{body.field_key}' already exists")
    payload = body.model_dump()
    _validate_field_payload(payload)
    try:
        db.save_field(payload, author=request_scope(request).actor_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "created", "field_key": body.field_key}


@router.put(
    "/{field_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def update_field(field_key: str, body: FieldSave, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    payload = body.model_dump()
    payload["field_key"] = field_key
    _validate_field_payload(payload)
    try:
        db.save_field(payload, author=request_scope(request).actor_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "updated", "field_key": field_key}


@router.patch(
    "/{field_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def patch_field(field_key: str, body: FieldPatch, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    existing = db.get_field(field_key)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Field '{field_key}' not found")
    updates = body.model_dump(exclude_none=True)
    merged = {**existing, **updates, "field_key": field_key}
    _validate_field_payload(merged)
    try:
        db.save_field(merged, author=request_scope(request).actor_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "updated", "field_key": field_key}


@router.patch(
    "/{field_key}/toggle",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_field(field_key: str, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    try:
        enabled = db.toggle_field(field_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "updated", "field_key": field_key, "enabled": enabled}


@router.delete(
    "/{field_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def delete_field(field_key: str, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    try:
        db.delete_field(field_key, author=request_scope(request).actor_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "field_key": field_key}
