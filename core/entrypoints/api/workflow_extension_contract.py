"""Public API route for release-bound workflow extension contracts."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access
from core.application.management.scope import normalize_scope

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/workflows",
    tags=["workflows"],
    responses={
        403: {"description": "Workflow extension access denied"},
        404: {"description": "Workflow extension resource not found"},
        503: {"description": "Workflow extension storage is unavailable"},
    },
)


class WorkflowExtensionInstallContractResponse(BaseModel):
    """Public release-bound workflow contract, including install availability."""

    install_id: str
    extension_id: str
    install_state: str
    release_id: str = ""
    descriptor_sha256: str = ""
    operations: list[dict[str, Any]]
    connections: list[dict[str, Any]]


def _get_db() -> Any:
    from core.application.registry.registry_manager import RegistryManager

    db = RegistryManager.instance().db_store
    if db is None:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return db


@lru_cache(maxsize=4)
def _connection_store_for_dsn(dsn: str) -> Any:
    from core.infrastructure.runtime.entrypoint_db_adapters import create_project_store

    return create_project_store(dsn)


def _get_connection_store(db: Any) -> Any:
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if not dsn:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return _connection_store_for_dsn(dsn)


def _request_scope(request: Request) -> Any:
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


def _resolve_extension_contract(install_id: str, *, account_id: str = "") -> dict | None:
    db = _get_db()
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if not dsn:
        return None
    try:
        from core.application.extensions.workflow_install_contract import (
            resolve_workflow_extension_install_contract,
        )
        from core.infrastructure.runtime.entrypoint_db_adapters import create_connection_store

        return resolve_workflow_extension_install_contract(
            create_connection_store(dsn).conn,
            install_id=install_id,
            account_id=account_id,
            default_account_id=DEFAULT_ACCOUNT_ID,
        )
    except Exception as exc:
        logger.warning("extension contract lookup failed for install_id=%s: %s", install_id, exc)
        return None


def _enforce_extension_contract_scope(scope: Any, contract: dict[str, Any]) -> None:
    contract_account_id = str(contract.get("account_id") or "").strip()
    scope_account_id = str(getattr(scope, "account_id", "") or "").strip()
    if contract_account_id and scope_account_id and contract_account_id != scope_account_id:
        raise HTTPException(status_code=403, detail="Access denied")


def _enforce_project_scope(db: Any, scope: Any, project_id: str) -> str:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return ""
    project = db.get_project(normalized_project_id) if hasattr(db, "get_project") else None
    if isinstance(project, dict):
        try:
            require_project_access(scope, project)
        except ServiceForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    elif (
        getattr(scope, "allowed_project_ids", None)
        and normalized_project_id not in scope.allowed_project_ids
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    return normalized_project_id


@router.get(
    "/meta/extension-install-contract",
    response_model=WorkflowExtensionInstallContractResponse,
    responses={
        403: {"description": "Project or extension access denied"},
        404: {"description": "Extension contract not found"},
        503: {"description": "Database not configured"},
    },
)
def get_extension_install_contract(
    request: Request,
    install_id: Annotated[str, Query(description="Extension install ID")],
    project_id: Annotated[str, Query(description="Project ID for connection bindings")] = "",
    locale: Annotated[str, Query(description="UI language for readable text")] = "",
):
    """Return workflow surfaces materialized from one exact extension install."""
    from core.application.settings.ui_i18n import resolve_ui_locale

    # resolve_ui_locale reads ?locale= first, then the user's profile language.
    _ = locale
    ui_locale = resolve_ui_locale(request)

    from core.application.extensions.workflow_install_contract import (
        extension_action_operations,
        workflow_install_connections,
    )

    scope = _request_scope(request)
    contract = _resolve_extension_contract(
        install_id,
        account_id=str(getattr(scope, "account_id", "") or ""),
    )
    if contract is None:
        raise HTTPException(
            status_code=404,
            detail=f"Extension contract not found for install '{install_id}'.",
        )
    _enforce_extension_contract_scope(scope, contract)
    extension_id = str(contract.get("extension_id") or "").strip()
    if str(contract.get("install_id") or "").strip() != install_id or not extension_id:
        raise HTTPException(status_code=404, detail="Extension install identity is unavailable.")

    connections: list[dict[str, Any]] = []
    if project_id:
        db = _get_db()
        connections = workflow_install_connections(
            _get_connection_store(db),
            project_id=_enforce_project_scope(db, scope, project_id),
            install_id=install_id,
        )
    return {
        "install_id": install_id,
        "extension_id": extension_id,
        "install_state": str(contract.get("install_state") or "active").strip().lower(),
        "release_id": str(contract.get("release_id") or "").strip(),
        "descriptor_sha256": str(contract.get("descriptor_sha256") or "").strip().lower(),
        "operations": extension_action_operations(
            extension_id,
            contract.get("action_definitions"),
            contract.get("operation_manifest"),
            locale=ui_locale,
            locale_bundles=contract.get("locale_bundles"),
        ),
        "connections": connections,
    }


__all__ = ["WorkflowExtensionInstallContractResponse", "router"]
