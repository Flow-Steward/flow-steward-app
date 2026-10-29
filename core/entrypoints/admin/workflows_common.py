# ruff: noqa: RUF022
# mypy: ignore-errors
"""
Admin API routes — Workflows CRUD with DB backend + phase/step management.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, field_validator

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access, require_workflow_access
from core.application.management.scope import normalize_scope
from core.application.management.workflow_operator_service import WorkflowOperatorService
from core.application.registry.contract_validation import (
    format_validation_error_detail,
    validate_agent_skill_compatibility,
    validate_workflow_readiness,
)
from core.application.registry.db_store import RestoreValidationError
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.workflows.canvas_graph_service import build_workflow_canvas_graph
from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.application.workflows.workflow_sample_file_service import WorkflowSampleFileService
from core.application.workflows.workflow_secret_default_service import (
    WorkflowSecretDefaultService,
)
from core.application.workflows.workflow_step_config_contracts import (
    validate_workflow_step_config_keys,
)
from core.entrypoints.admin._readiness import enforce_enabled_save_validation
from core.entrypoints.admin._registry_scope import require_default_registry_account
from core.entrypoints.admin.workflows_definition import create_workflow_definition_router
from core.infrastructure.runtime.entrypoint_db_adapters import (
    DATABASE_OPERATION_ERRORS,
    create_integration_store,
    create_job_store,
    create_project_store,
    workflow_import_transaction,
)
from core.infrastructure.runtime.workflow_db_adapters import (
    create_workflow_secret_default_repository,
)
from core.infrastructure.security.prompt_safety import validate_reference_path

ACCESS_DENIED_VALUE = "Access denied"


logger = logging.getLogger(__name__)


def _require_default_workflow_registry_request(request: Request) -> None:
    """Reject dormant secondary accounts before any workflow route touches storage."""
    state_scope = getattr(request.state, "scope_context", None)
    auth_user = getattr(request.state, "auth_user", None)
    if state_scope is not None:
        scope = normalize_scope(state_scope)
    elif isinstance(auth_user, dict):
        scope = normalize_scope(
            auth_user,
            account_id=str(auth_user.get("account_id") or DEFAULT_ACCOUNT_ID).strip(),
            tenant_id=str(auth_user.get("tenant_id") or DEFAULT_TENANT_ID).strip(),
            actor_id=str(auth_user.get("username") or auth_user.get("id") or "").strip() or None,
            principal_type=str(auth_user.get("principal_type") or "").strip() or None,
        )
    else:
        scope = normalize_scope()
    require_default_registry_account(scope)


router = APIRouter(
    prefix="/api/workflows",
    tags=["workflows"],
    dependencies=[Depends(_require_default_workflow_registry_request)],
)
FORBIDDEN_TRIGGER_KEYS = {"task_types", "complexity"}


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


def _get_db():
    rm = _get_registry()
    if not rm.db_store:
        raise HTTPException(status_code=503, detail="DB backend not configured")
    return rm.db_store


def _workflow_operator_service(*, db=None) -> WorkflowOperatorService:
    return WorkflowOperatorService(
        get_logger=lambda: logger,
        project_query_service=None,
        registry_manager_provider=_get_registry,
        registry_db_provider=(lambda: db) if db is not None else _get_db,
    )


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            import json

            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _normalize_optional_entity_id(value: Any) -> str:
    return str(value or "").strip()


def _platform_logger_for_admin_catalog(db) -> Any | None:
    """Store used by the connector catalog to read project connections.

    It must be the integration store: list_project_connections lives there, and
    the identity store this used to build does not have it. Combined with the
    admin db handle (RegistryDBStore) also lacking the method, the catalog's
    two lookup paths both missed and it returned an empty connection list on
    every deployment — so no connector step could ever be bound to a
    connection, and the authoring UI showed none rather than an error.
    """
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if not dsn:
        return None
    try:
        return create_integration_store(dsn)
    except Exception as exc:
        logger.debug("connector catalog logger init failed: %s", exc)
        return None


def _project_store_for_admin_import(db) -> Any | None:
    """Project store the import reads the project's model from; None without a DSN."""
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if not dsn:
        return None
    try:
        return create_project_store(dsn)
    except DATABASE_OPERATION_ERRORS:
        return None


def _platform_job_store_for_workflow_stats(db) -> Any | None:
    """Return the job-owned store used by workflow run statistics."""
    dsn = str(getattr(db, "dsn", "") or "").strip()
    if not dsn:
        return None
    try:
        return create_job_store(dsn)
    except Exception as exc:
        logger.debug("workflow stats store init failed: %s", exc)
        return None


def _workflow_secret_default_service(db) -> WorkflowSecretDefaultService:
    return WorkflowSecretDefaultService(create_workflow_secret_default_repository(db))


def _workflow_sample_file_service(db) -> WorkflowSampleFileService:
    from core.entrypoints.admin.deps import get_artifact_plane

    return WorkflowSampleFileService(db, get_artifact_plane())


__all__ = [
    "json",
    "logging",
    "os",
    "Annotated",
    "Any",
    "APIRouter",
    "HTTPException",
    "Query",
    "Request",
    "Response",
    "BaseModel",
    "field_validator",
    "DEFAULT_ACCOUNT_ID",
    "DEFAULT_TENANT_ID",
    "ServiceForbiddenError",
    "require_project_access",
    "require_workflow_access",
    "normalize_scope",
    "WorkflowOperatorService",
    "format_validation_error_detail",
    "validate_agent_skill_compatibility",
    "validate_workflow_readiness",
    "RestoreValidationError",
    "WorkflowInterfaceService",
    "build_workflow_canvas_graph",
    "WorkflowPortabilityError",
    "WorkflowPortabilityService",
    "WorkflowSecretDefaultService",
    "validate_workflow_step_config_keys",
    "enforce_enabled_save_validation",
    "require_default_registry_account",
    "create_workflow_definition_router",
    "DATABASE_OPERATION_ERRORS",
    "workflow_import_transaction",
    "validate_reference_path",
    "ACCESS_DENIED_VALUE",
    "logger",
    "router",
    "FORBIDDEN_TRIGGER_KEYS",
    "_get_registry",
    "_get_db",
    "_workflow_operator_service",
    "_normalize_text",
    "_json_object",
    "_normalize_optional_entity_id",
    "_platform_logger_for_admin_catalog",
    "_project_store_for_admin_import",
    "_platform_job_store_for_workflow_stats",
    "_workflow_secret_default_service",
    "_workflow_sample_file_service",
]
