"""
Admin API routes — Published block contracts CRUD.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.entrypoints.admin._registry_scope import (
    assert_create_account_scope,
    request_scope,
    row_in_account_scope,
)

router = APIRouter(prefix="/api/published-blocks", tags=["published-blocks"])
_ALLOWED_SOURCE_KINDS = {"agent", "connector", "workflow"}


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db():
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


class PublishedBlockInputSave(BaseModel):
    field_id: str | None = None
    field_key: str = ""
    required: bool = False
    multiple_allowed: bool = False
    manual_allowed_override: bool | None = None
    default_source_hint: str = ""
    notes: str = ""
    sort_order: int = 0


class PublishedBlockOutputSave(BaseModel):
    field_id: str | None = None
    field_key: str = ""
    required_by_default: bool = False
    exposed_by_default: bool = True
    notes: str = ""
    sort_order: int = 0


class PublishedBlockContractSave(BaseModel):
    block_key: str
    display_name: str
    block_type: str = "agent"
    source_kind: str
    source_ref: str
    description: str = ""
    family: str = ""
    enabled: bool = True
    inputs: list[PublishedBlockInputSave] = []
    outputs: list[PublishedBlockOutputSave] = []


class PublishedBlockContractPatch(BaseModel):
    display_name: str | None = None
    block_type: str | None = None
    source_kind: str | None = None
    source_ref: str | None = None
    description: str | None = None
    family: str | None = None
    enabled: bool | None = None
    inputs: list[PublishedBlockInputSave] | None = None
    outputs: list[PublishedBlockOutputSave] | None = None


def _validate_payload(payload: dict, db=None) -> None:
    block_key = str(payload.get("block_key") or "").strip()
    if not block_key:
        raise HTTPException(status_code=400, detail="block_key is required")
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")
    source_kind = str(payload.get("source_kind") or "").strip().lower()
    if source_kind not in _ALLOWED_SOURCE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"source_kind must be one of: {', '.join(sorted(_ALLOWED_SOURCE_KINDS))}",
        )
    source_ref = str(payload.get("source_ref") or "").strip()
    if not source_ref:
        raise HTTPException(status_code=400, detail="source_ref is required")
    if db is not None:
        if source_kind == "agent":
            if "::" in source_ref:
                src_agent, src_skill = source_ref.split("::", 1)
                src_agent = str(src_agent or "").strip()
                src_skill = str(src_skill or "").strip()
                if not src_agent or not src_skill:
                    raise HTTPException(
                        status_code=400,
                        detail="agent source_ref must be 'agent_id::skill_id'",
                    )
                if not db.get_agent(src_agent):
                    raise HTTPException(
                        status_code=400, detail=f"source_ref references unknown agent '{src_agent}'"
                    )
                if not db.get_skill(src_skill):
                    raise HTTPException(
                        status_code=400, detail=f"source_ref references unknown skill '{src_skill}'"
                    )
                cap_ids = {
                    str(item.get("skill_id") or "").strip()
                    for item in (db.get_agent_skills(src_agent) or [])
                    if str(item.get("skill_id") or "").strip()
                }
                if src_skill not in cap_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"source_ref '{source_ref}' is invalid: agent '{src_agent}' is not mapped to skill '{src_skill}'",
                    )
            else:
                if not db.get_agent(source_ref) and not db.get_skill(source_ref):
                    raise HTTPException(
                        status_code=400,
                        detail=f"source_ref '{source_ref}' does not reference an existing agent_id or skill_id",
                    )
        elif source_kind == "workflow":
            if not db.get_workflow(source_ref):
                raise HTTPException(
                    status_code=400, detail=f"source_ref references unknown workflow '{source_ref}'"
                )
        elif source_kind == "connector":
            get_tool = getattr(db, "get_tool", None)
            if not callable(get_tool) or not get_tool(source_ref):
                raise HTTPException(
                    status_code=400,
                    detail=f"source_ref references unknown connector/tool '{source_ref}'",
                )

    def _validate_field_ref(item: dict, *, path: str) -> None:
        field_id = item.get("field_id")
        field_key = str(item.get("field_key") or "").strip()
        has_field_id = field_id is not None and str(field_id).strip() != ""
        if not has_field_id and not field_key:
            raise HTTPException(
                status_code=400,
                detail=f"{path} must provide either field_id or field_key",
            )

    for idx, item in enumerate(payload.get("inputs") or []):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"inputs[{idx}] must be an object")
        _validate_field_ref(item, path=f"inputs[{idx}]")
    for idx, item in enumerate(payload.get("outputs") or []):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"outputs[{idx}] must be an object")
        _validate_field_ref(item, path=f"outputs[{idx}]")


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_published_blocks(
    request: Request,
    enabled_only: Annotated[bool, Query()] = False,
    block_type: Annotated[str | None, Query()] = None,
    source_kind: Annotated[str | None, Query()] = None,
):
    db = _get_db()
    scope = request_scope(request)
    rows = db.list_published_block_contracts(
        enabled_only=enabled_only,
        block_type=block_type,
        source_kind=source_kind,
    )
    # Account-owned blocks (non-empty account_id) must stay confined to their owner;
    # global blocks (NULL account_id) remain visible to every scoped admin.
    return [row for row in rows if row_in_account_scope(row, scope)]


@router.get(
    "/{block_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def get_published_block(block_key: str, request: Request):
    db = _get_db()
    scope = request_scope(request)
    row = db.get_published_block_contract(block_key, account_id=scope.account_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Published block '{block_key}' not found")
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
def create_published_block(body: PublishedBlockContractSave, request: Request):
    db = _get_db()
    scope = request_scope(request)
    if db.get_published_block_contract(body.block_key):
        raise HTTPException(
            status_code=409, detail=f"Published block '{body.block_key}' already exists"
        )
    payload = body.model_dump()
    assert_create_account_scope(payload, scope)
    _validate_payload(payload, db=db)
    try:
        db.save_published_block_contract(payload, author=scope.actor_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "created", "block_key": body.block_key}


@router.put(
    "/{block_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def update_published_block(block_key: str, body: PublishedBlockContractSave, request: Request):
    db = _get_db()
    scope = request_scope(request)
    existing = db.get_published_block_contract(block_key, account_id=scope.account_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Published block '{block_key}' not found")
    payload = body.model_dump()
    payload["block_key"] = block_key
    assert_create_account_scope(payload, scope)
    _validate_payload(payload, db=db)
    try:
        db.save_published_block_contract(payload, author=scope.actor_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "updated", "block_key": block_key}


@router.patch(
    "/{block_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def patch_published_block(block_key: str, body: PublishedBlockContractPatch, request: Request):
    db = _get_db()
    existing = db.get_published_block_contract(
        block_key, account_id=request_scope(request).account_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail=f"Published block '{block_key}' not found")
    updates = body.model_dump(exclude_none=True)
    merged = {**existing, **updates, "block_key": block_key}
    _validate_payload(merged, db=db)
    try:
        db.save_published_block_contract(merged, author=request_scope(request).actor_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "updated", "block_key": block_key}


@router.patch(
    "/{block_key}/toggle",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_published_block(block_key: str, request: Request):
    db = _get_db()
    if not db.get_published_block_contract(block_key, account_id=request_scope(request).account_id):
        raise HTTPException(status_code=404, detail=f"Published block '{block_key}' not found")
    try:
        enabled = db.toggle_published_block_contract(block_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "updated", "block_key": block_key, "enabled": enabled}


@router.delete(
    "/{block_key}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def delete_published_block(block_key: str, request: Request):
    db = _get_db()
    if not db.get_published_block_contract(block_key, account_id=request_scope(request).account_id):
        raise HTTPException(status_code=404, detail=f"Published block '{block_key}' not found")
    try:
        db.delete_published_block_contract(block_key, author=request_scope(request).actor_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "block_key": block_key}
