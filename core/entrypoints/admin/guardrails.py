# ruff: noqa: B904
# mypy: ignore-errors
"""
Admin API routes — Guardrails CRUD with DB backend.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.management.command_models import (
    GuardrailAgentsResultView,
    GuardrailDeleteResultView,
    GuardrailMutationResultView,
    GuardrailRestoreResultView,
)
from core.application.management.scope import normalize_scope
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_guardrail_readiness,
)
from core.application.registry.db_store import RestoreValidationError
from core.application.registry.guardrail_config_validation import (
    validate_deterministic_guardrail_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/guardrails",
    tags=["guardrails"],
    responses={403: {"description": "Guardrail scope access denied"}},
)

_GUARDRAIL_SAVE_REQUEST_EXAMPLE = {
    "display_name": "Block secrets in prompts",
    "description": "Reject prompts that expose API keys or credentials.",
    "enabled": True,
    "mode": "deterministic",
    "rule_type": "prompt_secret_scan",
    "severity": "error",
    "config": {"blocked_patterns": ["sk-", "ghp_"]},
    "prompt_text": "Disallow requests that reveal secrets.",
    "agent_prompt": "",
    "project_id": "proj-1",
    "sort_order": 10,
    "agents": ["release_writer"],
}
_GUARDRAIL_PATCH_REQUEST_EXAMPLE = {
    "description": "Reject prompts that expose credentials or tokens.",
    "severity": "warning",
    "enabled": False,
}
_GUARDRAIL_AGENT_SCOPE_REQUEST_EXAMPLE = {"agent_ids": ["release_writer", "release_reviewer"]}
_GUARDRAIL_RESTORE_REQUEST_EXAMPLE = {"entry_id": "72"}

_GUARDRAIL_CREATE_RESPONSE_EXAMPLE = {"id": "guardrail_secret_scan", "status": "created"}
_GUARDRAIL_UPDATE_RESPONSE_EXAMPLE = {"id": "guardrail_secret_scan", "status": "updated"}
_GUARDRAIL_TOGGLE_RESPONSE_EXAMPLE = {"id": "guardrail_secret_scan", "enabled": True}
_GUARDRAIL_DELETE_RESPONSE_EXAMPLE = {"status": "deleted"}
_GUARDRAIL_AGENT_SCOPE_RESPONSE_EXAMPLE = {
    "id": "guardrail_secret_scan",
    "agents": ["release_writer", "release_reviewer"],
}
_GUARDRAIL_RESTORE_RESPONSE_EXAMPLE = {
    "status": "restored",
    "guardrail_id": "guardrail_secret_scan",
    "restored_from_entry_id": "72",
    "validation": {"valid": True, "errors": [], "warnings": []},
    "snapshot": {"id": "guardrail_secret_scan", "display_name": "Block secrets in prompts"},
}


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


def _guardrail_in_project_scope(row: dict | None, scope) -> bool:
    """Return whether *row* is visible to *scope* under project isolation.

    Guardrail rows carry only a ``project_id`` (no ``account_id`` column), so the
    enforceable scope dimension is project membership. A principal restricted to a
    set of projects (non-empty ``scope.allowed_project_ids``) may only see a
    guardrail that is global (``project_id`` IS NULL) or bound to one of its
    allowed projects.
    """
    if not isinstance(row, dict):
        return True
    project_id = _normalize_text(row.get("project_id"))
    if not project_id:
        return True
    allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset()) or frozenset()
    if not allowed_project_ids:
        return True
    return project_id in {str(item).strip() for item in allowed_project_ids}


def _assert_guardrail_project_scope(row: dict | None, scope) -> None:
    """Deny cross-project access to a project-scoped guardrail. Fails closed (404)."""
    if not _guardrail_in_project_scope(row, scope):
        raise HTTPException(status_code=404, detail="Guardrail not found")


def _require_guardrail_in_scope(db, guardrail_id: str, request: Request) -> dict:
    """Load a guardrail and enforce caller project scope, or fail closed (404).

    Shared by the detail/mutation/history/diff/restore/agent-assignment routes so
    none of them can read or mutate a guardrail outside the caller's allowed
    projects.
    """
    scope = _request_scope(request)
    row = _resolve_guardrail_row(db, guardrail_id, account_id=scope.account_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Guardrail {guardrail_id} not found")
    _assert_guardrail_project_scope(row, scope)
    return row


def _assert_guardrail_create_project_scope(payload: dict, scope) -> None:
    """Reject creating/binding a guardrail under a project outside the caller's scope."""
    project_id = _normalize_text((payload or {}).get("project_id"))
    if not project_id:
        return
    allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset()) or frozenset()
    if allowed_project_ids and project_id not in {
        str(item).strip() for item in allowed_project_ids
    }:
        raise HTTPException(status_code=403, detail="Access denied")


def _guardrail_actor_context(scope) -> dict[str, str]:
    return {
        "account_id": str(scope.account_id or "").strip(),
        "actor_id": str(scope.actor_id or scope.actor_name).strip(),
        "actor_type": str(scope.principal_type or "unknown").strip(),
    }


def _get_guardrail_validation(db, guardrail_id: str, *, account_id: str) -> dict:
    return validate_guardrail_readiness(db, guardrail_id, account_id=account_id)


def _normalize_text(value) -> str:
    return str(value or "").strip()


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _normalize_text(value).lower()
    if normalized in {"false", "0", "off", "no", "n", "f"}:
        return False
    if normalized in {"true", "1", "on", "yes", "y", "t"}:
        return True
    return bool(value)


def _platform_logger(db):
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if not dsn:
        return None
    try:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_identity_store

        return create_identity_store(dsn)
    except Exception as exc:
        logger.debug("guardrail Flow Steward logger init failed: %s", exc)
        return None


def _get_extension_runtime_store(db):
    try:
        from core.application.extensions.service_support import (
            get_default_extension_runtime_store,
        )

        bl = _platform_logger(db)
        return get_default_extension_runtime_store(get_logger=lambda: bl)
    except Exception as exc:
        logger.debug("guardrail extension runtime store unavailable: %s", exc)
        return None


def _extension_guardrail_rows(
    db,
    *,
    enabled_only: bool = False,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> list[dict]:
    runtime_store = _get_extension_runtime_store(db)
    if runtime_store is None or not hasattr(runtime_store, "list_projections"):
        return []
    try:
        rows = runtime_store.list_projections(
            account_id=str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID,
            artifact_kind="guardrail",
            only_active=enabled_only,
        )
    except Exception as exc:
        logger.warning("guardrail projection lookup failed: %s", exc)
        return []

    normalized: list[dict] = []
    for row in rows or []:
        if hasattr(row, "model_dump"):
            projection = row.model_dump(mode="json")
        elif isinstance(row, dict):
            projection = dict(row)
        else:
            projection = {}
        snapshot = projection.get("projection")
        snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        guardrail_id = _normalize_text(snapshot.get("id") or snapshot.get("guardrail_id"))
        if not guardrail_id:
            guardrail_id = _normalize_text(projection.get("artifact_id"))
        if not guardrail_id:
            continue
        snapshot["id"] = guardrail_id
        snapshot["guardrail_id"] = guardrail_id
        snapshot["source_install_id"] = _normalize_text(
            snapshot.get("source_install_id") or projection.get("install_id")
        )
        snapshot["source_release_id"] = _normalize_text(
            snapshot.get("source_release_id") or projection.get("release_id")
        )
        snapshot["projection_mode"] = _normalize_text(snapshot.get("projection_mode") or "derived")
        snapshot["enabled"] = _as_bool(projection.get("is_active", snapshot.get("enabled", True)))
        normalized.append(snapshot)
    return normalized


def _resolve_guardrail_row(
    db,
    guardrail_id: str,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict | None:
    # RC7-006: the account is now part of the lookup. Before this the row was
    # returned as soon as the id matched, so a foreign-account admin reached a
    # first-party guardrail; account_id was consulted only for the extension
    # fallback below. A miss returns None and the caller answers 404, which does
    # not disclose that the object exists elsewhere.
    current = db.get_guardrail(guardrail_id, account_id=account_id)
    if current:
        return dict(current)
    normalized_guardrail_id = _normalize_text(guardrail_id)
    if not normalized_guardrail_id:
        return None
    for row in _extension_guardrail_rows(db, enabled_only=False, account_id=account_id):
        if _normalize_text(row.get("id") or row.get("guardrail_id")) == normalized_guardrail_id:
            return row
    return None


# --------------------------------------------------------------------------- #
# Pydantic models                                                             #
# --------------------------------------------------------------------------- #


def _normalize_explicit_agent_scope(value: list[str], *, field_name: str) -> list[str]:
    normalized = list(dict.fromkeys(str(agent_id or "").strip() for agent_id in value))
    normalized = [agent_id for agent_id in normalized if agent_id]
    if not normalized:
        raise ValueError(f"{field_name} must contain at least one agent_id")
    return normalized


class GuardrailSave(BaseModel):
    display_name: str
    description: str = ""
    enabled: bool = True
    mode: Literal["deterministic", "semantic"]
    rule_type: str
    severity: Literal["error", "warning"] = "error"
    config: dict = {}
    prompt_text: str = ""
    agent_prompt: str = ""
    project_id: str | None = None
    sort_order: int = 0
    agents: list[str]  # explicit agent_ids for scope

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("display_name is required")
        return normalized

    @field_validator("rule_type")
    @classmethod
    def validate_rule_type(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("rule_type is required")
        return normalized

    @field_validator("agents")
    @classmethod
    def validate_agents(cls, value: list[str]) -> list[str]:
        return _normalize_explicit_agent_scope(value, field_name="agents")

    @model_validator(mode="after")
    def validate_runtime_contract(self):
        validate_deterministic_guardrail_config(
            enabled=self.enabled,
            mode=self.mode,
            rule_type=self.rule_type,
            config=self.config,
        )
        return self


class GuardrailPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    mode: Literal["deterministic", "semantic"] | None = None
    rule_type: str | None = None
    severity: Literal["error", "warning"] | None = None
    config: dict | None = None
    prompt_text: str | None = None
    agent_prompt: str | None = None
    project_id: str | None = None
    sort_order: int | None = None
    agents: list[str] | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("display_name is required")
        return normalized

    @field_validator("rule_type")
    @classmethod
    def validate_rule_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("rule_type is required")
        return normalized

    @field_validator("agents")
    @classmethod
    def validate_agents(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _normalize_explicit_agent_scope(value, field_name="agents")


class AgentScope(BaseModel):
    agent_ids: list[str]

    @field_validator("agent_ids")
    @classmethod
    def validate_agent_ids(cls, value: list[str]) -> list[str]:
        return _normalize_explicit_agent_scope(value, field_name="agent_ids")


class RestoreRevisionRequest(BaseModel):
    entry_id: str | int


def _validated_guardrail_save_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if isinstance(normalized.get("project_id"), UUID):
        normalized["project_id"] = str(normalized["project_id"])
    normalized.setdefault("agents", normalized.get("_agents", []))
    try:
        return GuardrailSave.model_validate(normalized).model_dump()
    except ValidationError as exc:
        raise RequestValidationError(
            [{**issue, "loc": ("body", *issue["loc"])} for issue in exc.errors(include_url=False)]
        ) from exc


# --------------------------------------------------------------------------- #
# CRUD endpoints                                                              #
# --------------------------------------------------------------------------- #


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def list_guardrails(
    request: Request,
    project_id: Annotated[str | None, Query()] = None,
    enabled_only: Annotated[bool, Query()] = False,
):
    """List all guardrails, optionally filtered by project."""
    db = _get_db()
    scope = _request_scope(request)
    rows = [
        dict(item)
        for item in db.list_guardrails(
            project_id=project_id,
            enabled_only=enabled_only,
            account_id=scope.account_id,
        )
        if isinstance(item, dict) and _guardrail_in_project_scope(item, scope)
    ]
    seen_ids = {
        _normalize_text(item.get("id") or item.get("guardrail_id"))
        for item in rows
        if _normalize_text(item.get("id") or item.get("guardrail_id"))
    }
    for row in _extension_guardrail_rows(
        db,
        enabled_only=enabled_only,
        account_id=scope.account_id,
    ):
        guardrail_id = _normalize_text(row.get("id") or row.get("guardrail_id"))
        if not guardrail_id or guardrail_id in seen_ids:
            continue
        if project_id is not None and _normalize_text(row.get("project_id")) != _normalize_text(
            project_id
        ):
            continue
        if not _guardrail_in_project_scope(row, scope):
            continue
        rows.append(row)
        seen_ids.add(guardrail_id)
    return rows


@router.post(
    "",
    status_code=201,
    response_model=GuardrailMutationResultView,
    responses={
        201: {"content": {"application/json": {"example": _GUARDRAIL_CREATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Project access denied"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def create_guardrail(
    request: Request,
    body: Annotated[
        GuardrailSave,
        Body(
            openapi_examples={
                "createGuardrail": {
                    "summary": "Create a guardrail that blocks secrets in prompts.",
                    "value": _GUARDRAIL_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    """Create a new guardrail rule."""
    db = _get_db()
    scope = _request_scope(request)
    data = body.model_dump()
    _assert_guardrail_create_project_scope(data, scope)
    # The changelog author defaults to "admin" in the store; pass the
    # authenticated actor so the audit trail names who actually made the change.
    try:
        new_id = db.save_guardrail(
            data,
            author=scope.actor_name,
            **_guardrail_actor_context(scope),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": new_id, "status": "created"}


@router.get(
    "/{guardrail_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def get_guardrail(guardrail_id: str, request: Request):
    """Get guardrail detail."""
    db = _get_db()
    scope = _request_scope(request)
    g = _resolve_guardrail_row(db, guardrail_id, account_id=scope.account_id)
    if not g:
        raise HTTPException(status_code=404, detail=f"Guardrail {guardrail_id} not found")
    _assert_guardrail_project_scope(g, scope)
    return g


@router.put(
    "/{guardrail_id}",
    response_model=GuardrailMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _GUARDRAIL_UPDATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Project access denied"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def update_guardrail(
    guardrail_id: str,
    request: Request,
    body: Annotated[
        GuardrailSave,
        Body(
            openapi_examples={
                "replaceGuardrail": {
                    "summary": "Replace the full guardrail definition.",
                    "value": _GUARDRAIL_SAVE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    """Full update of a guardrail rule."""
    db = _get_db()
    scope = _request_scope(request)
    _require_guardrail_in_scope(db, guardrail_id, request)
    data = body.model_dump()
    data["id"] = guardrail_id
    _assert_guardrail_create_project_scope(data, scope)
    try:
        db.save_guardrail(data, author=scope.actor_name, **_guardrail_actor_context(scope))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": guardrail_id, "status": "updated"}


@router.patch(
    "/{guardrail_id}",
    response_model=GuardrailMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _GUARDRAIL_UPDATE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        403: {"description": "Project access denied"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def patch_guardrail(
    guardrail_id: str,
    request: Request,
    body: Annotated[
        GuardrailPatch,
        Body(
            openapi_examples={
                "patchGuardrail": {
                    "summary": "Update only selected guardrail fields.",
                    "value": _GUARDRAIL_PATCH_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    """Partial update of a guardrail rule."""
    db = _get_db()
    scope = _request_scope(request)
    existing = _require_guardrail_in_scope(db, guardrail_id, request)

    # Merge: only overwrite fields that are explicitly set
    update = dict(existing)
    for field_name, value in body.model_dump(exclude_none=True).items():
        update[field_name] = value
    update["id"] = guardrail_id

    # Preserve _agents as agents if not being patched
    if body.agents is None:
        update["agents"] = existing.get("_agents", [])

    _assert_guardrail_create_project_scope(update, scope)
    validated = _validated_guardrail_save_payload(update)
    validated["id"] = guardrail_id
    try:
        db.save_guardrail(validated, author=scope.actor_name, **_guardrail_actor_context(scope))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": guardrail_id, "status": "updated"}


@router.delete(
    "/{guardrail_id}",
    response_model=GuardrailDeleteResultView,
    responses={
        200: {"content": {"application/json": {"example": _GUARDRAIL_DELETE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def delete_guardrail(guardrail_id: str, request: Request):
    """Delete a guardrail rule."""
    db = _get_db()
    scope = _request_scope(request)
    _require_guardrail_in_scope(db, guardrail_id, request)
    deleted = db.delete_guardrail(
        guardrail_id,
        author=scope.actor_name,
        **_guardrail_actor_context(scope),
    )
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Guardrail {guardrail_id} not found")
    return {"status": "deleted"}


@router.patch(
    "/{guardrail_id}/toggle",
    response_model=GuardrailMutationResultView,
    responses={
        200: {"content": {"application/json": {"example": _GUARDRAIL_TOGGLE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def toggle_guardrail(guardrail_id: str, request: Request):
    """Toggle guardrail enabled/disabled."""
    db = _get_db()
    scope = _request_scope(request)
    existing = _require_guardrail_in_scope(db, guardrail_id, request)
    if not existing.get("enabled", True):
        _validated_guardrail_save_payload({**existing, "enabled": True})
    try:
        new_state = db.toggle_guardrail(
            guardrail_id,
            **_guardrail_actor_context(scope),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if new_state is None:
        raise HTTPException(status_code=404, detail=f"Guardrail {guardrail_id} not found")
    return {"id": guardrail_id, "enabled": new_state}


@router.put(
    "/{guardrail_id}/agents",
    response_model=GuardrailAgentsResultView,
    responses={
        200: {
            "content": {"application/json": {"example": _GUARDRAIL_AGENT_SCOPE_RESPONSE_EXAMPLE}}
        },
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def set_guardrail_agents(
    guardrail_id: str,
    request: Request,
    body: Annotated[
        AgentScope,
        Body(
            openapi_examples={
                "setGuardrailAgents": {
                    "summary": "Assign the guardrail to a specific set of agents.",
                    "value": _GUARDRAIL_AGENT_SCOPE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    """Set the agent scope for a guardrail."""
    db = _get_db()
    scope = _request_scope(request)
    _require_guardrail_in_scope(db, guardrail_id, request)
    try:
        db.set_guardrail_agents(
            guardrail_id,
            body.agent_ids,
            **_guardrail_actor_context(scope),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Guardrail not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": guardrail_id, "agents": body.agent_ids}


@router.get(
    "/{guardrail_id}/history",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def get_guardrail_history(
    guardrail_id: str, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
):
    db = _get_db()
    scope = _request_scope(request)
    _require_guardrail_in_scope(db, guardrail_id, request)
    return db.get_entity_history(
        entity_type="guardrail",
        entity_id=str(guardrail_id),
        limit=limit,
        account_id=scope.account_id,
    )


@router.get(
    "/{guardrail_id}/history/{revision}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def get_guardrail_revision(guardrail_id: str, revision: int, request: Request):
    db = _get_db()
    scope = _request_scope(request)
    _require_guardrail_in_scope(db, guardrail_id, request)
    entry = db.get_entity_revision(
        entity_type="guardrail",
        entity_id=str(guardrail_id),
        revision=revision,
        account_id=scope.account_id,
    )
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Revision {revision} not found for guardrail {guardrail_id}"
        )
    return entry


@router.get(
    "/{guardrail_id}/diff",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def get_guardrail_diff(
    guardrail_id: str,
    request: Request,
    from_entry_id: Annotated[str | None, Query()] = None,
    to_entry_id: Annotated[str | None, Query()] = None,
):
    db = _get_db()
    scope = _request_scope(request)
    _require_guardrail_in_scope(db, guardrail_id, request)
    try:
        return db.diff_entity_history(
            entity_type="guardrail",
            entity_id=str(guardrail_id),
            from_entry_id=from_entry_id,
            to_entry_id=to_entry_id,
            account_id=scope.account_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/{guardrail_id}/restore",
    response_model=GuardrailRestoreResultView,
    responses={
        200: {"content": {"application/json": {"example": _GUARDRAIL_RESTORE_RESPONSE_EXAMPLE}}},
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
async def restore_guardrail_revision(
    guardrail_id: str,
    request: Request,
    body: Annotated[
        RestoreRevisionRequest,
        Body(
            openapi_examples={
                "restoreGuardrail": {
                    "summary": "Restore a previously recorded guardrail revision.",
                    "value": _GUARDRAIL_RESTORE_REQUEST_EXAMPLE,
                }
            }
        ),
    ],
):
    db = _get_db()
    scope = _request_scope(request)
    _require_guardrail_in_scope(db, guardrail_id, request)
    try:
        outcome = db.restore_entity_revision(
            entity_type="guardrail",
            entity_id=str(guardrail_id),
            entry_id=str(body.entry_id),
            author=scope.actor_name,
            account_id=scope.account_id,
            actor_id=_guardrail_actor_context(scope)["actor_id"],
            actor_type=_guardrail_actor_context(scope)["actor_type"],
            validate_restored=lambda _entity_type, _entity_id, _restored: _get_guardrail_validation(
                db,
                guardrail_id,
                account_id=scope.account_id,
            ),
        )
    except RestoreValidationError as e:
        validation = (
            e.validation
            if isinstance(e.validation, dict)
            else {
                "valid": False,
                "errors": [],
                "warnings": [],
            }
        )
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="guardrail",
                entity_id=str(guardrail_id),
                action="restore",
                validation=validation,
            ),
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    validation = outcome.get("validation")
    if not isinstance(validation, dict):
        validation = _get_guardrail_validation(
            db,
            guardrail_id,
            account_id=scope.account_id,
        )
    _get_registry().reload_all()
    return {
        "status": "restored",
        "guardrail_id": guardrail_id,
        "restored_from_entry_id": str(body.entry_id),
        "validation": validation,
        "snapshot": outcome.get("restored"),
    }
