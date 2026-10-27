# ruff: noqa: B904
# mypy: ignore-errors
"""
Admin API routes — Registry changelog (full-snapshot audit trail).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.enforce import require_roles
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import (
    require_account_access,
    require_project_access,
    require_workflow_access,
)
from core.application.management.scope import normalize_scope
from core.application.registry.changelog_projection import (
    redact_changelog_diff,
    redact_changelog_payload,
)
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_agent_readiness,
    validate_guardrail_readiness,
    validate_skill_readiness,
    validate_tool_readiness,
    validate_workflow_readiness,
)
from core.application.registry.db_store import (
    SUPPORTED_CHANGELOG_ENTITY_TYPES,
    RestoreValidationError,
)
from core.application.registry.db_store_revisions_mixin import WorkflowHistoryComparisonError
from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/changelog",
    tags=["changelog"],
    responses={403: {"description": "Administrator access required"}},
)

_ACCOUNT_CONTEXT_REGISTRY_ENTITY_TYPES = {
    "agent",
    "tool",
    "skill",
    "guardrail",
    "published_block",
}


def _get_db():
    from core.application.registry.registry_manager import RegistryManager

    rm = RegistryManager.instance()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_workflow_portability_service(db=None) -> WorkflowPortabilityService:
    return WorkflowPortabilityService(db or _get_db())


def _raise_portability_http_error(exc: WorkflowPortabilityError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


def _redact_changelog_entries(db, rows: list[dict]) -> list[dict]:
    service = _get_workflow_portability_service(db)
    redacted: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("entity_type") or "").strip() == "workflow":
            row = service.redact_workflow_history_entry(row)
        redacted.append(redact_changelog_payload(row))
    return redacted


class RestoreRevisionRequest(BaseModel):
    entry_id: str | int
    expected_revision: int | None = None


def _request_auth_user(request: Request) -> dict | None:
    user = getattr(request.state, "auth_user", None)
    return user if isinstance(user, dict) else None


def _request_scope(request: Request):
    state_scope = getattr(request.state, "scope_context", None)
    if state_scope is not None:
        return normalize_scope(state_scope)
    user = _request_auth_user(request)
    if isinstance(user, dict):
        return normalize_scope(
            user,
            account_id=str(user.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
            or DEFAULT_ACCOUNT_ID,
            actor_id=str(user.get("username") or user.get("id") or "").strip() or None,
            principal_type=str(user.get("principal_type") or "").strip() or None,
        )
    return normalize_scope()


def _project_scope_record(db, payload: dict, project_id: str) -> dict:
    get_project = getattr(db, "get_project", None)
    if callable(get_project):
        project = get_project(project_id)
        if isinstance(project, dict):
            return dict(project)
    return {
        "id": project_id,
        "account_id": payload.get("account_id"),
        "workspace_id": payload.get("workspace_id"),
    }


def _account_scope_record(entity_type: str, payload: dict) -> dict:
    if (
        payload
        and entity_type in _ACCOUNT_CONTEXT_REGISTRY_ENTITY_TYPES
        and not str(payload.get("account_id") or "").strip()
        and str(payload.get("scope_kind") or "system").strip() == "system"
        and not str(payload.get("project_id") or "").strip()
        and not str(payload.get("source_install_id") or "").strip()
    ):
        scoped_payload = dict(payload)
        scoped_payload["account_id"] = DEFAULT_ACCOUNT_ID
        return scoped_payload
    return payload


def _require_admin_request(request: Request) -> None:
    user = _request_auth_user(request)
    if user is None:
        return
    try:
        require_roles(user, {"admin"})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Admin role required") from exc


def _request_is_admin(request: Request) -> bool:
    user = _request_auth_user(request)
    if user is None:
        return False
    try:
        require_roles(user, {"admin"})
    except PermissionError:
        return False
    return True


def _history_snapshot(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    snapshot = entry.get("snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _lookup_entity_record(
    db,
    entity_type: str,
    entity_id: str,
    *,
    account_id: str | None = None,
) -> dict | None:
    method_names = {
        "agent": ("get_agent",),
        "tool": ("get_tool",),
        "skill": ("get_skill",),
        "workflow": ("get_workflow",),
        "guardrail": ("get_guardrail",),
        "data_contract": ("get_data_contract",),
        "dataset": ("get_dataset",),
        "field": ("get_field",),
        "published_block": ("get_published_block_contract", "get_published_block"),
    }.get(entity_type, ())
    for method_name in method_names:
        method = getattr(db, method_name, None)
        if not callable(method):
            continue
        if entity_type == "guardrail":
            row = method(entity_id, account_id=account_id)
        else:
            try:
                row = method(entity_id)
            except TypeError:
                row = method(entity_id=entity_id)
        if isinstance(row, dict):
            return row
    return None


def _lookup_entity_history_snapshot(
    db,
    *,
    entity_type: str,
    entity_id: str,
    entry_id: str | None = None,
    revision: int | None = None,
    account_id: str | None = None,
) -> dict | None:
    entry = None
    if entry_id and hasattr(db, "get_changelog_entry"):
        entry = db.get_changelog_entry(
            entry_id,
            entity_type=entity_type,
            entity_id=entity_id,
            **({"account_id": account_id} if entity_type == "guardrail" else {}),
        )
    elif revision is not None and hasattr(db, "get_entity_revision"):
        entry = db.get_entity_revision(
            entity_type=entity_type,
            entity_id=entity_id,
            revision=revision,
            **({"account_id": account_id} if entity_type == "guardrail" else {}),
        )
    elif hasattr(db, "get_entity_history"):
        rows = db.get_entity_history(
            entity_type=entity_type,
            entity_id=entity_id,
            limit=1,
            **({"account_id": account_id} if entity_type == "guardrail" else {}),
        )
        if isinstance(rows, list) and rows:
            entry = rows[0]
    return _history_snapshot(entry)


def _assert_entity_scope(
    request: Request,
    db,
    *,
    entity_type: str,
    entity_id: str,
    record: dict | None = None,
) -> None:
    scope = _request_scope(request)
    if entity_type == "guardrail":
        # Historical content and lifecycle markers are not authority for the
        # entity's current account/project access boundary.
        payload = dict(
            _lookup_entity_record(
                db,
                entity_type,
                entity_id,
                account_id=scope.account_id,
            )
            or {}
        )
        if not payload:
            raise HTTPException(status_code=404, detail="Guardrail not found")
        try:
            project_id = str(payload.get("project_id") or "").strip()
            if project_id:
                require_project_access(scope, _project_scope_record(db, payload, project_id))
            else:
                require_account_access(scope, _account_scope_record(entity_type, payload))
        except ServiceForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return
    if _request_auth_user(request) is None:
        return
    if _request_is_admin(request):
        return
    payload = dict(record or _lookup_entity_record(db, entity_type, entity_id) or {})
    if not payload:
        payload = (
            _lookup_entity_history_snapshot(
                db,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            or {}
        )
    try:
        if entity_type == "workflow":
            require_workflow_access(scope, entity_id)
        project_id = str(payload.get("project_id") or "").strip()
        if project_id:
            require_project_access(scope, _project_scope_record(db, payload, project_id))
        else:
            require_account_access(scope, _account_scope_record(entity_type, payload))
    except ServiceForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _assert_entity_type_supported(entity_type: str) -> str:
    normalized = str(entity_type or "").strip().lower()
    if normalized not in SUPPORTED_CHANGELOG_ENTITY_TYPES:
        supported = ", ".join(sorted(SUPPORTED_CHANGELOG_ENTITY_TYPES))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported entity type '{entity_type}'. Supported: {supported}",
        )
    return normalized


def _validate_restored_entity(
    db,
    entity_type: str,
    entity_id: str,
    *,
    account_id: str | None = None,
) -> dict | None:
    if entity_type == "agent":
        return validate_agent_readiness(db, entity_id)
    if entity_type == "tool":
        return validate_tool_readiness(db, entity_id)
    if entity_type == "skill":
        return validate_skill_readiness(db, entity_id)
    if entity_type == "workflow":
        return validate_workflow_readiness(db, entity_id)
    if entity_type == "guardrail":
        return validate_guardrail_readiness(
            db,
            entity_id,
            account_id=str(account_id or DEFAULT_ACCOUNT_ID),
        )
    return {
        "valid": False,
        "errors": [
            {
                "code": "restore.entity_type_unsupported",
                "message": f"Validation for entity type '{entity_type}' is not implemented",
                "path": "restore.entity_type",
                "severity": "error",
            }
        ],
        "warnings": [],
    }


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Administrator access required"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def get_changelog(
    request: Request,
    entity_type: Annotated[
        str | None, Query(description="Filter by entity type (agent, tool, skill, workflow)")
    ] = None,
    entity_id: Annotated[str | None, Query(description="Filter by entity ID")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Get changelog entries with optional filters."""
    db = _get_db()
    if not entity_type or not entity_id:
        _require_admin_request(request)
    else:
        normalized_type = _assert_entity_type_supported(entity_type)
        _assert_entity_scope(request, db, entity_type=normalized_type, entity_id=entity_id)
    scope = _request_scope(request)
    rows = db.get_changelog(
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        **({"account_id": scope.account_id} if entity_type == "guardrail" else {}),
    )
    return _redact_changelog_entries(db, rows)


@router.get(
    "/stats",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Administrator access required"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def get_registry_stats(request: Request):
    """Get registry statistics (entity counts, enabled counts)."""
    _require_admin_request(request)
    db = _get_db()
    return db.get_stats()


@router.get(
    "/validate",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Administrator access required"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def validate_registry(request: Request):
    """Run cross-registry integrity validation."""
    _require_admin_request(request)
    from core.application.registry.registry_manager import RegistryManager

    rm = RegistryManager.instance()
    errors, warnings = rm.validate()
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


@router.get(
    "/search",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Administrator access required"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def search_entities(
    request: Request,
    q: Annotated[str, Query(min_length=1, description="Search query")],
    entity_type: Annotated[str | None, Query(description="Filter by entity type")] = None,
):
    """Search across all registry entities."""
    _require_admin_request(request)
    db = _get_db()
    return db.search_entities(q, entity_type=entity_type)


@router.get(
    "/{entity_type}/{entity_id}/history",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Access denied"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def get_entity_history(
    request: Request,
    entity_type: str,
    entity_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Fetch revision history for a single registry entity."""
    normalized_type = _assert_entity_type_supported(entity_type)
    db = _get_db()
    scope = _request_scope(request)
    rows = db.get_entity_history(
        entity_type=normalized_type,
        entity_id=entity_id,
        limit=limit,
        **({"account_id": scope.account_id} if normalized_type == "guardrail" else {}),
    )
    record = _history_snapshot(rows[0]) if rows else None
    _assert_entity_scope(
        request,
        db,
        entity_type=normalized_type,
        entity_id=entity_id,
        record=record,
    )
    if normalized_type == "workflow":
        rows = _get_workflow_portability_service(db).redact_workflow_history(
            rows,
            workflow_id=entity_id,
        )
    return redact_changelog_payload(rows)


@router.get(
    "/{entity_type}/{entity_id}/history/{revision}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Access denied"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def get_entity_revision(request: Request, entity_type: str, entity_id: str, revision: int):
    """Fetch one revision by 1-based index (1 = latest)."""
    normalized_type = _assert_entity_type_supported(entity_type)
    db = _get_db()
    scope = _request_scope(request)
    entry = db.get_entity_revision(
        entity_type=normalized_type,
        entity_id=entity_id,
        revision=revision,
        **({"account_id": scope.account_id} if normalized_type == "guardrail" else {}),
    )
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"Revision {revision} not found for {normalized_type}:{entity_id}",
        )
    _assert_entity_scope(
        request,
        db,
        entity_type=normalized_type,
        entity_id=entity_id,
        record=_history_snapshot(entry),
    )
    if normalized_type == "workflow":
        entry = _get_workflow_portability_service(db).redact_workflow_history_entry(
            entry,
            workflow_id=entity_id,
        )
    return redact_changelog_payload(entry)


@router.get(
    "/{entity_type}/{entity_id}/diff",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Access denied"},
        404: {"description": "Not found"},
        503: {"description": "Service unavailable"},
    },
)
def get_entity_diff(
    request: Request,
    entity_type: str,
    entity_id: str,
    from_entry_id: Annotated[str | None, Query()] = None,
    to_entry_id: Annotated[str | None, Query()] = None,
    format: Annotated[str, Query(description="Response format override: json")] = "",
):
    """Diff two revisions (defaults to latest vs previous)."""
    normalized_type = _assert_entity_type_supported(entity_type)
    db = _get_db()
    scope = _request_scope(request)
    _assert_entity_scope(request, db, entity_type=normalized_type, entity_id=entity_id)
    try:
        if normalized_type == "workflow":
            _get_workflow_portability_service(db).assert_history_diff_allowed(
                entity_id,
                from_entry_id=from_entry_id,
                to_entry_id=to_entry_id,
            )
        diff_payload = db.diff_entity_history(
            entity_type=normalized_type,
            entity_id=entity_id,
            from_entry_id=from_entry_id,
            to_entry_id=to_entry_id,
            **({"account_id": scope.account_id} if normalized_type == "guardrail" else {}),
        )
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    except WorkflowHistoryComparisonError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if normalized_type == "workflow":
        diff_payload = _get_workflow_portability_service(db).redact_workflow_history_payload(
            diff_payload
        )
    return redact_changelog_diff(diff_payload)


@router.post(
    "/{entity_type}/{entity_id}/restore",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Administrator access required"},
        404: {"description": "Not found"},
        409: {"description": "Workflow revision conflict"},
        503: {"description": "Service unavailable"},
    },
)
def restore_entity_revision(
    request: Request,
    entity_type: str,
    entity_id: str,
    body: RestoreRevisionRequest,
):
    """Restore a revision as a new write (append-only restore-as-new-revision)."""
    _require_admin_request(request)
    normalized_type = _assert_entity_type_supported(entity_type)
    db = _get_db()
    scope = _request_scope(request)
    snapshot = _lookup_entity_history_snapshot(
        db,
        entity_type=normalized_type,
        entity_id=entity_id,
        entry_id=str(body.entry_id),
        account_id=scope.account_id if normalized_type == "guardrail" else None,
    )
    _assert_entity_scope(
        request,
        db,
        entity_type=normalized_type,
        entity_id=entity_id,
        record=snapshot,
    )
    if normalized_type == "workflow":
        # One restore for workflows, judged by their definition service.
        from core.entrypoints.admin.workflows_routes_history import (
            restore_workflow_history_entry,
        )

        restored = restore_workflow_history_entry(
            request,
            entity_id,
            entry_id=str(body.entry_id),
            expected_revision=body.expected_revision,
            db=db,
            registry=_get_registry(),
        )
        return {
            **restored,
            "entity_type": normalized_type,
            "entity_id": entity_id,
            "new_snapshot": restored.get("snapshot"),
        }

    try:
        outcome = db.restore_entity_revision(
            entity_type=normalized_type,
            entity_id=entity_id,
            entry_id=str(body.entry_id),
            author="admin_restore",
            **(
                {
                    "account_id": scope.account_id,
                    "actor_id": scope.actor_id or scope.actor_name,
                    "actor_type": scope.principal_type or "unknown",
                }
                if normalized_type == "guardrail"
                else {}
            ),
            validate_restored=lambda _entity_type, _entity_id, _restored: _validate_restored_entity(
                db,
                _entity_type,
                _entity_id,
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
                entity_type=normalized_type,
                entity_id=entity_id,
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
        validation = _validate_restored_entity(
            db,
            normalized_type,
            entity_id,
            account_id=scope.account_id,
        )

    _get_registry().reload_all()
    return {
        "status": "restored",
        "entity_type": normalized_type,
        "entity_id": entity_id,
        "restored_from_entry_id": str(body.entry_id),
        "new_snapshot": redact_changelog_payload(outcome.get("restored")),
        "validation": validation,
    }
