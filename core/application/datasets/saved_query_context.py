"""Authorized explicit project context for registry saved-query actions."""

from __future__ import annotations

from typing import Any

from core.application.datasets.catalog_scope import _scope_matches_row
from core.application.datasets.definition_values import _dataset_connection_id
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.guards import require_project_access
from core.application.management.scope import ScopeContext
from core.application.registry.data_connection_binding import resolve_project_scoped_data_connection

_PROJECT_ACCESS_DENIED = "Project access denied"


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_project_scoped_dataset(dataset: dict[str, Any]) -> bool:
    metadata = dataset.get("metadata_jsonb") or dataset.get("metadata")
    return isinstance(metadata, dict) and metadata.get("connection_resolution") == "project_scope"


def _error(error_type: type[ServiceError], code: str, message: str) -> ServiceError:
    return error_type(message, payload={"code": code, "message": message})


def _project_store(db: Any) -> Any:
    if callable(getattr(db, "get_project", None)) and callable(
        getattr(db, "list_project_data_connections", None)
    ):
        return db
    dsn = _text(getattr(db, "dsn", ""))
    if not dsn:
        raise _error(
            ServiceUnavailableError, "project_context_unavailable", "Project context is unavailable"
        )
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    return PlatformProjectStore.get_shared_instance(dsn)


def _authorized_project(db: Any, project_id: str, scope: ScopeContext | None) -> dict[str, Any]:
    if scope is None:
        raise _error(ServiceForbiddenError, "project_forbidden", _PROJECT_ACCESS_DENIED)
    get_project = getattr(db, "get_project", None)
    project = get_project(project_id) if callable(get_project) else None
    if not isinstance(project, dict) or project.get("deleted_at"):
        raise _error(ServiceNotFoundError, "project_not_found", "Project not found")
    project = dict(project)
    project["id"] = _text(project.get("id") or project.get("project_id"))
    try:
        require_project_access(scope, project)
    except ServiceForbiddenError as exc:
        raise _error(ServiceForbiddenError, "project_forbidden", _PROJECT_ACCESS_DENIED) from exc
    if project["id"] != project_id or not _scope_matches_row(project, scope):
        raise _error(ServiceForbiddenError, "project_forbidden", _PROJECT_ACCESS_DENIED)
    return project


def resolve_saved_query_action_connection(
    db: Any,
    dataset: dict[str, Any],
    *,
    scope: ScopeContext | None,
    project_id: str | None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve a fixed connection or the chosen project's selected connections.

    Workflow runtime retains its existing workspace candidate policy. Registry
    actions require an explicit project choice and restrict candidates to that
    project's connections, including an empty selection without fallback.
    """
    if not is_project_scoped_dataset(dataset):
        get_connection = getattr(db, "get_workspace_data_connection", None)
        connection = (
            get_connection(_dataset_connection_id(dataset)) if callable(get_connection) else None
        )
        return connection, ""
    if scope is None or not _scope_matches_row(dataset, scope):
        raise _error(ServiceForbiddenError, "project_forbidden", _PROJECT_ACCESS_DENIED)
    selected_project_id = _text(project_id)
    if not selected_project_id:
        raise _error(
            ServiceValidationError,
            "project_required",
            "Choose a project to run this saved query",
        )
    project_store = _project_store(db)
    project = _authorized_project(project_store, selected_project_id, scope)
    workspace_id = _text(project.get("workspace_id"))
    dataset_workspace = _text(dataset.get("workspace_id"))
    assigned = selected_project_id in {_text(item) for item in dataset.get("project_ids") or []}
    workspace_available = (
        dataset.get("availability_mode") == "all_workspace_projects"
        and bool(dataset_workspace)
        and dataset_workspace == workspace_id
    )
    if (dataset_workspace and dataset_workspace != workspace_id) or not (
        assigned or workspace_available
    ):
        raise _error(
            ServiceForbiddenError,
            "dataset_project_unassigned",
            "Dataset is not assigned to this project",
        )
    list_connections = getattr(project_store, "list_project_data_connections", None)
    selected_connections = (
        list_connections(selected_project_id) if callable(list_connections) else []
    )
    get_connection = getattr(db, "get_workspace_data_connection", None)
    # Project assignments are summaries; execution needs the canonical full row.
    candidates = []
    if callable(get_connection):
        for selected in selected_connections or []:
            connection_id = (
                _text(selected.get("connection_id")) if isinstance(selected, dict) else ""
            )
            connection = get_connection(connection_id) if connection_id else None
            if isinstance(connection, dict):
                candidates.append(connection)
    connection, issue = resolve_project_scoped_data_connection(
        db,
        dataset,
        {
            "account_id": scope.account_id,
            "tenant_id": _text(project.get("tenant_id") or scope.tenant_id),
            "workspace_id": workspace_id,
        },
        connection_candidates=candidates or [],
    )
    if issue:
        raise _error(
            ServiceConflictError,
            issue,
            {
                "connection_rebind_required": "Dataset projection is unavailable or must be rebound",
                "connection_not_found": "No compatible data connection is selected for this project",
                "connection_ambiguous": "Select exactly one compatible data connection for this project",
            }.get(issue, "Dataset data connection is unavailable"),
        )
    return connection, selected_project_id


def saved_query_project_catalog(
    db: Any,
    dataset: dict[str, Any],
    catalog: dict[str, Any],
    *,
    scope: ScopeContext,
) -> dict[str, Any]:
    """Expose only assigned authorized projects for a project-scoped dataset."""
    if not is_project_scoped_dataset(dataset):
        return catalog
    projects = []
    if _scope_matches_row(dataset, scope):
        project_store = _project_store(db)
        for project_id in dict.fromkeys(_text(item) for item in dataset.get("project_ids") or []):
            if not project_id:
                continue
            try:
                projects.append(_authorized_project(project_store, project_id, scope))
            except (ServiceForbiddenError, ServiceNotFoundError):
                continue
    authorized_ids = [project["id"] for project in projects]
    return {
        **catalog,
        "available_projects": projects,
        "project_ids": authorized_ids,
        "explicit_project_ids": authorized_ids,
    }
