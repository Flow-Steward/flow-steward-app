"""
Admin API routes — Data contracts CRUD for structured payload schemas.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.entrypoints.admin._registry_scope import (
    request_scope,
    require_default_registry_account,
)

router = APIRouter(prefix="/api/data-contracts", tags=["data-contracts"])


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db():
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


def _validate_data_contract_payload(payload: dict) -> None:
    contract_id = str(payload.get("contract_id") or "").strip()
    if not contract_id:
        raise HTTPException(status_code=400, detail="contract_id is required")
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")
    kind = str(payload.get("kind") or "json").strip().lower()
    if kind not in {"json", "list"}:
        raise HTTPException(status_code=400, detail="kind must be one of: json, list")
    schema = payload.get("schema") or {}
    if not isinstance(schema, dict):
        raise HTTPException(status_code=400, detail="schema must be an object")
    schema_type = str(schema.get("type") or "").strip().lower()
    if kind == "json" and schema_type and schema_type != "object":
        raise HTTPException(
            status_code=400, detail="json data contracts must use schema.type=object"
        )
    if kind == "list" and schema_type and schema_type != "array":
        raise HTTPException(
            status_code=400, detail="list data contracts must use schema.type=array"
        )


class DataContractSave(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_id: str
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    kind: str = "json"
    schema_body: dict = Field(default_factory=dict, alias="schema")
    enabled: bool = True
    tags: list[str] = []


class DataContractPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: str | None = None
    display_name: str | None = None
    description: str | None = None
    kind: str | None = None
    schema_body: dict | None = Field(default=None, alias="schema")
    enabled: bool | None = None
    tags: list[str] | None = None


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_data_contracts(request: Request, enabled_only: Annotated[bool, Query()] = False):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    return db.list_data_contracts(enabled_only=enabled_only)


@router.get(
    "/{contract_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def get_data_contract(contract_id: str, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    row = db.get_data_contract(contract_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Data contract '{contract_id}' not found")
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
def create_data_contract(body: DataContractSave, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    existing = db.get_data_contract(body.contract_id, include_trashed=True)
    if existing:
        raise HTTPException(
            status_code=409, detail=f"Data contract '{body.contract_id}' already exists"
        )
    payload = body.model_dump(by_alias=True)
    _validate_data_contract_payload(payload)
    db.save_data_contract(payload, author=request_scope(request).actor_name)
    return {"status": "created", "contract_id": body.contract_id}


@router.put(
    "/{contract_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def update_data_contract(contract_id: str, body: DataContractSave, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    payload = body.model_dump(by_alias=True)
    payload["contract_id"] = contract_id
    _validate_data_contract_payload(payload)
    db.save_data_contract(payload, author=request_scope(request).actor_name)
    return {"status": "updated", "contract_id": contract_id}


@router.patch(
    "/{contract_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def patch_data_contract(contract_id: str, body: DataContractPatch, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    existing = db.get_data_contract(contract_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Data contract '{contract_id}' not found")
    updates = body.model_dump(exclude_none=True, by_alias=True)
    existing_payload = dict(existing)
    existing_payload["schema"] = existing_payload.pop("schema_json", {})
    merged = {**existing_payload, **updates, "contract_id": contract_id}
    _validate_data_contract_payload(merged)
    db.save_data_contract(merged, author=request_scope(request).actor_name)
    return {"status": "updated", "contract_id": contract_id}


@router.patch(
    "/{contract_id}/toggle",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_data_contract(contract_id: str, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    try:
        enabled = db.toggle_data_contract(contract_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "updated", "contract_id": contract_id, "enabled": enabled}


@router.delete(
    "/{contract_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def delete_data_contract(contract_id: str, request: Request):
    require_default_registry_account(request_scope(request))
    db = _get_db()
    try:
        db.delete_data_contract(contract_id, author=request_scope(request).actor_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "contract_id": contract_id}
