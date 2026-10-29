# ruff: noqa: F403, F405, I001, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from core.application.extensions.extension_catalog_install import install_option_rows
from core.application.extensions.service_support import get_default_extension_install_store
from core.application.management.errors import ServiceUnavailableError
from core.application.workflows.workflow_hook_contract import is_hook_storage
from core.application.registry.projected_artifact_rows import (
    projected_catalog_row_matches,
    projection_aware_catalog_rows,
)
from core.application.workflows.workflow_template_catalog_create import (
    resolve_portable_extensions,
)
from core.entrypoints.admin.workflows_connector_catalog import (
    _list_project_connections_for_catalog,
)
from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def list_workflows(
    request: Request,
    enabled_only: Annotated[bool, Query()] = False,
    search: Annotated[str | None, Query()] = None,
):
    """List all workflows."""
    scope = _request_scope(request)
    db = _get_db()
    portability = _get_workflow_portability_service(db)
    catalog_rows = projection_aware_catalog_rows(
        db,
        "workflow",
        "workflow_id",
        # A hook's storage workflow is a place inside another workflow (R2-11).
        [
            row
            for row in db.list_workflows(enabled_only=enabled_only) or []
            if not is_hook_storage(row)
        ],
        account_id=scope.account_id,
        enabled_only=enabled_only,
    )
    if search:
        matching_ids = {
            workflow_id
            for row in (db.search_entities(search, entity_type="workflow") or [])
            if isinstance(row, dict)
            and (workflow_id := _normalize_text(row.get("workflow_id") or row.get("entity_id")))
        }
        normalized_rows: list[dict[str, Any]] = []
        for row in catalog_rows:
            item = dict(row)
            workflow_id = _normalize_text(item.get("workflow_id"))
            if workflow_id not in matching_ids and not projected_catalog_row_matches(
                item,
                search,
                fields=("workflow_id", "display_name", "description"),
            ):
                continue
            if item.get("source_install_id"):
                item = _annotate_extension_workflow_availability(
                    db,
                    item,
                    account_id=scope.account_id,
                )
            if not _workflow_visible_to_scope(scope, item):
                continue
            item = portability.redact_protected_workflow(item)
            normalized_rows.append(_with_viewer_extension_permission(item, request))
        return normalized_rows
    normalized_rows: list[dict[str, Any]] = []
    for row in catalog_rows:
        item = dict(row)
        if item.get("source_install_id"):
            item = _annotate_extension_workflow_availability(
                db,
                item,
                account_id=scope.account_id,
            )
        if not _workflow_visible_to_scope(scope, item):
            continue
        item = portability.redact_protected_workflow(item)
        normalized_rows.append(_with_viewer_extension_permission(item, request))
    return normalized_rows


@router.post(
    "/import/preflight",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def preflight_workflow_import(body: WorkflowImportRequest, request: Request):
    """Validate a workflow YAML bundle and return dependency resolution needs."""
    scope = _request_scope(request)
    if body.target_workflow_id:
        _enforce_workflow_scope(scope, body.target_workflow_id)
    _enforce_import_resolution_scope(_get_db(), scope, body.resolutions)
    bundle_yaml = body.bundle_yaml or body.yaml or ""
    # The project the file's project guardrails would attach to, checked like apply.
    target_project_id = (
        _enforce_project_scope(_get_db(), scope, body.project_id) if body.project_id else ""
    )
    service = _get_workflow_portability_service(account_id=scope.account_id)
    try:
        merged, outcomes, discovery = _map_portable_extensions(
            service,
            bundle_yaml,
            target_workflow_id=body.target_workflow_id or "",
            resolutions=list(body.resolutions or []),
            account_id=scope.account_id,
            target_project_id=target_project_id or "",
        )
        preflight = (
            discovery
            if merged == list(body.resolutions or [])
            else service.preflight_import(
                bundle_yaml,
                target_workflow_id=body.target_workflow_id or "",
                resolutions=merged,
                target_project_id=target_project_id or "",
            )
        )
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    return {**preflight, "extension_resolutions": outcomes}


def _map_portable_extensions(
    service,
    bundle_yaml: str,
    *,
    target_workflow_id: str,
    resolutions: list,
    account_id: str,
    target_project_id: str = "",
) -> tuple[list, list, dict]:
    """Map extensions by their portable id, exactly as "From catalog" does.

    The catalog and this import share one bundle format and one resolver, so a
    workflow exported elsewhere maps to this account's install on both paths.
    Without an install store (no database connection) nothing is added and the
    operator resolves extensions by hand, as before.
    """
    integration_store = _platform_logger_for_admin_catalog(_get_db())
    try:
        install_store = get_default_extension_install_store(get_logger=lambda: integration_store)
    except ServiceUnavailableError:
        discovery = service.preflight_import(
            bundle_yaml,
            target_workflow_id=target_workflow_id,
            resolutions=resolutions,
            target_project_id=target_project_id,
        )
        return resolutions, [], discovery
    return resolve_portable_extensions(
        service,
        bundle_yaml,
        target_workflow_id=target_workflow_id,
        resolutions=resolutions,
        account_id=str(account_id or "").strip() or "acc_default",
        get_install_store=lambda: install_store,
        target_project_id=target_project_id,
    )


@router.get(
    "/import/options",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def workflow_import_dependency_options(
    request: Request,
    project_id: Annotated[str | None, Query()] = None,
):
    """Return typed registry candidates used by the workflow import wizard."""
    scope = _request_scope(request)
    db = _get_db()
    normalized_project_id = _enforce_project_scope(db, scope, project_id)
    connection_rows = _list_project_connections_for_catalog(
        db,
        project_id=normalized_project_id,
        enabled_only=False,
    )
    return _get_workflow_portability_service(db).import_dependency_options(
        project_id=normalized_project_id or None,
        account_id=scope.account_id,
        project_connection_rows=connection_rows,
        extension_install_rows=_extension_install_rows(db, scope.account_id),
    )


def _extension_install_rows(db, account_id: str) -> list[dict] | None:
    """Install candidates from the store that owns installs, or None to fall back."""
    integration_store = _platform_logger_for_admin_catalog(db)
    try:
        install_store = get_default_extension_install_store(get_logger=lambda: integration_store)
    except ServiceUnavailableError:
        return None
    list_bundles = getattr(integration_store, "list_extensions", None)
    return install_option_rows(
        install_store,
        account_id=str(account_id or "").strip() or "acc_default",
        bundle_rows=list_bundles(enabled_only=False) if callable(list_bundles) else None,
    )


@router.post(
    "/import/apply",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def apply_workflow_import(body: WorkflowImportRequest, request: Request):
    """Apply a preflighted workflow YAML bundle as disabled draft workflow(s)."""
    scope = _request_scope(request)
    require_default_registry_account(scope)
    if body.target_workflow_id:
        _enforce_workflow_scope(scope, body.target_workflow_id)
    _enforce_import_resolution_scope(_get_db(), scope, body.resolutions)
    bundle_yaml = body.bundle_yaml or body.yaml or ""
    # Bind imported rows to the caller's account/project so import cannot persist
    # an unowned, system-scoped workflow on behalf of an account-scoped caller
    # (FS-DEEP-CAND-013).
    owner_account_id = str(getattr(scope, "account_id", "") or "").strip()
    # The import page asks which project to import into. Falling back to the
    # scope's project ignored that answer entirely for an admin caller, whose
    # scope carries none, so the workflow was stored associated with nothing.
    # The chosen project is checked against the caller's scope first, so
    # accepting the field cannot widen what they can reach.
    owner_project_id = (
        _enforce_project_scope(_get_db(), scope, body.project_id)
        or str(getattr(scope, "project_id", "") or "").strip()
    )
    service = _get_workflow_portability_service(account_id=owner_account_id)
    try:
        # The same mapping the preflight showed; apply re-runs preflight itself.
        merged, _outcomes, _discovery = _map_portable_extensions(
            service,
            bundle_yaml,
            target_workflow_id=body.target_workflow_id or "",
            resolutions=list(body.resolutions or []),
            account_id=owner_account_id,
            target_project_id=owner_project_id,
        )
        result = service.apply_import(
            bundle_yaml,
            target_workflow_id=body.target_workflow_id or "",
            resolutions=merged,
            author=scope.actor_name,
            owner_account_id=owner_account_id,
            owner_project_id=owner_project_id,
        )
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    _refresh_workflow_cache_after_commit(
        reload_all=bool((result.get("definitions") or {}).get("created"))
    )
    return result


__all__ = [
    "list_workflows",
    "preflight_workflow_import",
    "workflow_import_dependency_options",
    "apply_workflow_import",
]
