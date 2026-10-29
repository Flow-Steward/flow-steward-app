# ruff: noqa: F403, F405, I001, RUF012
"""Read-only workflow template catalog routes.

Four endpoints, all authenticated, none of which can change the catalog: list
the cached index, report its availability, preflight one entry, and create a
workflow from one entry. Creation is delegated to the existing workflow
portability apply path, so a catalog workflow is an ordinary disabled draft.

These are registered before the ``/{workflow_id}`` reads so ``/catalog`` is not
swallowed by the workflow-id parameter.
"""

from __future__ import annotations

from core.application.extensions.service_support import get_default_extension_install_store
from core.application.workflows.workflow_template_catalog import (
    WorkflowTemplateCatalogError,
    load_workflow_template_catalog,
    workflow_template_catalog_availability,
)
from core.application.workflows.workflow_template_catalog_create import (
    InstallAwareWorkflowDb,
    WorkflowTemplateCatalogCreateService,
)
from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR

_CATALOG_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Bad request"},
    403: {"description": "Forbidden"},
    404: {"description": "Not found"},
    409: {"description": "Conflict"},
    422: {"description": "Unprocessable entity"},
    500: {"description": "Internal server error"},
    503: {"description": "Catalog unavailable"},
}


class WorkflowCatalogCreateRequest(BaseModel):
    """Everything the catalog create path accepts beyond the selected item."""

    target_workflow_id: str | None = None
    project_id: str | None = None
    resolutions: list[dict] = []


def _raise_catalog_http_error(exc: WorkflowTemplateCatalogError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


def _catalog_create_service(scope, db=None) -> WorkflowTemplateCatalogCreateService:
    resolved_db = db or _get_db()

    def _install_store():
        return get_default_extension_install_store(
            get_logger=lambda: _platform_logger_for_admin_catalog(resolved_db)
        )

    # The portability service reads installs through its workflow store, which
    # cannot answer that question on its own. Only the catalog path gets the
    # wrapper, so manual YAML import is untouched.
    install_aware_db = InstallAwareWorkflowDb(
        resolved_db,
        get_install_store=_install_store,
        account_id=str(getattr(scope, "account_id", "") or ""),
    )
    # The account is what lets `_install_exists` answer at all: with an install store
    # wired in it refuses to look without one, and the manual import already passes it.
    return WorkflowTemplateCatalogCreateService(
        portability=_get_workflow_portability_service(
            install_aware_db, account_id=str(getattr(scope, "account_id", "") or "")
        ),
        get_install_store=_install_store,
    )


@router.get("/catalog", responses=_CATALOG_RESPONSES)
def list_workflow_template_catalog(request: Request):
    """Return the whole current index so the UI can search and filter locally."""
    _request_scope(request)
    try:
        index = load_workflow_template_catalog()
    except WorkflowTemplateCatalogError as exc:
        _raise_catalog_http_error(exc)
    return {
        "kind": "workflow_template_catalog",
        "schema_version": index.schema_version,
        "catalog_revision": index.catalog_revision,
        "published_at": index.published_at,
        "availability": workflow_template_catalog_availability(index),
        "items": [item.as_dict() for item in index.items],
        "integration_labels": dict(index.integration_labels),
        "category_labels": dict(index.category_labels),
    }


@router.get("/catalog/availability", responses=_CATALOG_RESPONSES)
def workflow_template_catalog_status(request: Request):
    """Say whether the catalog can be offered, without failing when it cannot."""
    _request_scope(request)
    return workflow_template_catalog_availability()


@router.post("/catalog/{item_id}/preflight", responses=_CATALOG_RESPONSES)
def preflight_workflow_template_catalog_item(
    item_id: str, body: WorkflowCatalogCreateRequest, request: Request
):
    """Download and verify one entry's YAML, then run the existing preflight."""
    scope = _request_scope(request)
    if body.target_workflow_id:
        _enforce_workflow_scope(scope, body.target_workflow_id)
    _enforce_import_resolution_scope(_get_db(), scope, body.resolutions)
    # The project decides a template's project guardrails, as on the manual import.
    project_id = (
        _enforce_project_scope(_get_db(), scope, body.project_id) if body.project_id else ""
    )
    try:
        return _catalog_create_service(scope).preflight(
            item_id,
            target_workflow_id=body.target_workflow_id or "",
            resolutions=body.resolutions,
            scope=scope,
            project_id=project_id,
        )
    except WorkflowTemplateCatalogError as exc:
        _raise_catalog_http_error(exc)
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)


@router.post("/catalog/{item_id}/create", responses=_CATALOG_RESPONSES)
def create_workflow_from_template_catalog(
    item_id: str, body: WorkflowCatalogCreateRequest, request: Request
):
    """Create one disabled draft from a catalog entry via portability apply."""
    scope = _request_scope(request)
    require_default_registry_account(scope)
    if body.target_workflow_id:
        _enforce_workflow_scope(scope, body.target_workflow_id)
    _enforce_import_resolution_scope(_get_db(), scope, body.resolutions)
    owner_account_id = str(getattr(scope, "account_id", "") or "").strip()
    owner_project_id = (
        _enforce_project_scope(_get_db(), scope, body.project_id)
        or str(getattr(scope, "project_id", "") or "").strip()
    )
    try:
        result = _catalog_create_service(scope).create(
            item_id,
            target_workflow_id=body.target_workflow_id or "",
            resolutions=body.resolutions,
            author=scope.actor_name,
            owner_account_id=owner_account_id,
            owner_project_id=owner_project_id,
            scope=scope,
        )
    except WorkflowTemplateCatalogError as exc:
        _raise_catalog_http_error(exc)
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    _refresh_workflow_cache_after_commit()
    return result


__all__ = [
    "WorkflowCatalogCreateRequest",
    "create_workflow_from_template_catalog",
    "list_workflow_template_catalog",
    "preflight_workflow_template_catalog_item",
    "workflow_template_catalog_status",
]
