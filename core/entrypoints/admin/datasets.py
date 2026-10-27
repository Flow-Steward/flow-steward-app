"""Admin API routes for registry dataset definitions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.datasets.catalog_scope import (
    _dataset_visible_to_project_restricted_scope,
    _scope_matches_row,
    _template_declares_scope,
    _template_matches_dataset,
)
from core.application.datasets.definition_values import (
    _dataset_connection_id,
    _normalize_project_ids,
)
from core.application.datasets.identifier_repository import _generate_dataset_id, _query_template_id
from core.application.datasets.saved_query_context import saved_query_project_catalog
from core.application.management.errors import ServiceError, ServiceForbiddenError
from core.application.management.guards import require_project_access
from core.application.management.scope import normalize_scope
from core.application.registry.projected_artifact_rows import (
    projection_aware_catalog_row,
    projection_aware_catalog_rows,
)
from core.application.registry.saved_query_contract import (
    SavedQueryContractError,
    validate_saved_query_contract,
)
from core.entrypoints.admin.datasets_helpers import (
    DATA_CONNECTION_STORAGE_IS_NOT_AVAILABLE_VALUE as DATA_CONNECTION_STORAGE_IS_NOT_AVAILABLE_VALUE,
)
from core.entrypoints.admin.datasets_helpers import (
    DATASET_NOT_FOUND_MESSAGE as DATASET_NOT_FOUND_MESSAGE,
)
from core.entrypoints.admin.datasets_helpers import (
    SAVED_QUERY_NOT_FOUND_MESSAGE as SAVED_QUERY_NOT_FOUND_MESSAGE,
)
from core.entrypoints.admin.datasets_helpers import (
    SAVED_QUERY_STORAGE_IS_NOT_AVAILABLE_VALUE as SAVED_QUERY_STORAGE_IS_NOT_AVAILABLE_VALUE,
)
from core.entrypoints.admin.datasets_helpers import (
    _connector_catalog_by_id as _connector_catalog_by_id,
)
from core.entrypoints.admin.datasets_helpers import (
    _data_connection_payload as _data_connection_payload,
)
from core.entrypoints.admin.datasets_helpers import (
    _data_connection_query_capability as _data_connection_query_capability,
)
from core.entrypoints.admin.datasets_helpers import (
    _invoke_connection_action as _invoke_connection_action_for_db,
)
from core.entrypoints.admin.datasets_helpers import (
    _invoke_saved_query_action as _invoke_saved_query_action,
)
from core.entrypoints.admin.datasets_helpers import (
    _list_dataset_connectors as _list_dataset_connectors,
)
from core.entrypoints.admin.datasets_helpers import (
    _persist_connection_operational_state as _persist_connection_operational_state,
)
from core.entrypoints.admin.datasets_helpers import (
    _set_project_datasets_via_dataset_updates as _set_project_datasets_via_dataset_updates,
)
from core.entrypoints.admin.datasets_helpers import (
    _validate_dataset_payload as _validate_dataset_payload,
)
from core.entrypoints.admin.datasets_helpers import (
    _validate_json_object as _validate_json_object,
)
from core.entrypoints.admin.datasets_http_errors import dataset_mutation_errors
from core.entrypoints.admin.datasets_models import (
    DataConnectionSave,
    DatasetPatch,
    DatasetProjectAssignment,
    DatasetQueryTemplateAction,
    DatasetQueryTemplateSave,
    DatasetSave,
    ProjectDatasetAssignment,
)

_MESSAGE_WORKSPACE_NOT_FOUND = "Workspace not found"

router = APIRouter(
    prefix="/api/datasets",
    tags=["datasets"],
    responses={
        403: {"description": "Dataset scope access denied"},
        422: {"description": "Dataset request validation failed"},
    },
)


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


def _invoke_connection_action(connection: dict, action_id: str) -> dict:
    return _invoke_connection_action_for_db(_get_db(), connection, action_id)


def _assert_scoped_row(row: dict | None, scope, detail: str) -> dict:
    if not isinstance(row, dict) or not _scope_matches_row(row, scope):
        raise HTTPException(status_code=404, detail=detail)
    return row


def _assert_scoped_dataset(db, dataset_id: str, scope) -> dict:
    row = projection_aware_catalog_row(
        db,
        "dataset",
        "dataset_id",
        dataset_id,
        db.get_dataset(dataset_id),
        account_id=scope.account_id,
    )
    return _assert_scoped_row(row, scope, DATASET_NOT_FOUND_MESSAGE)


def _assert_dataset_reachable(db, dataset_id: str, scope) -> dict:
    """Account/workspace scope plus the project-restricted reachability guard.

    Saved-query template *mutations* must apply the same dataset reachability check
    as execution routes so a project-restricted admin cannot create/update a
    template pointing at a dataset outside its allowed projects.
    """
    dataset = _assert_scoped_dataset(db, dataset_id, scope)
    if not _dataset_visible_to_project_restricted_scope(dataset, scope):
        raise HTTPException(status_code=404, detail=DATASET_NOT_FOUND_MESSAGE)
    return dataset


def _validate_saved_query_write(payload: dict, *, dataset: dict) -> None:
    try:
        validate_saved_query_contract(payload, dataset=dataset)
    except SavedQueryContractError as exc:
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc


def _assert_dataset_query_executable(db, query_template_id: str, scope) -> dict:
    """Scope guard for saved-query *execution* (validate/preview/infer).

    Beyond account/workspace scope (``_assert_scoped_template``), a project-
    restricted principal may only run a saved query when its dataset is reachable
    from one of the principal's allowed projects. This mirrors the workflow
    runtime ``dataset_query`` negative control and closes FS-DEEP-CAND-047, where
    account/workspace-only scoping let a restricted admin preview unassigned
    datasets.
    """
    template = _assert_scoped_template(db, query_template_id, scope)
    dataset_id = str(template.get("dataset_id") or "").strip()
    if dataset_id:
        dataset = _assert_scoped_dataset(db, dataset_id, scope)
        if not _dataset_visible_to_project_restricted_scope(dataset, scope):
            raise HTTPException(status_code=404, detail=DATASET_NOT_FOUND_MESSAGE)
    return template


def _assert_scoped_connection(db, connection_id: str, scope) -> dict:
    get_connection = getattr(db, "get_workspace_data_connection", None)
    row = get_connection(connection_id) if callable(get_connection) else None
    return _assert_scoped_row(row, scope, "Data connection not found")


def _assert_scoped_project(db, project_id: str, scope) -> str:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    project = db.get_project(normalized_project_id) if hasattr(db, "get_project") else None
    if isinstance(project, dict):
        try:
            require_project_access(scope, project)
        except ServiceForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return normalized_project_id
    allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset()) or frozenset()
    if allowed_project_ids and normalized_project_id not in allowed_project_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    return normalized_project_id


def _assert_scoped_workspace_payload(db, payload: dict, scope) -> dict:
    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not workspace_id:
        return payload
    account_id = str(getattr(scope, "account_id", "") or "").strip()
    tenant_id = str(getattr(scope, "tenant_id", "") or "").strip()
    workspace = None
    get_workspace = getattr(db, "get_workspace", None)
    if callable(get_workspace):
        try:
            workspace = get_workspace(
                workspace_id,
                account_id=account_id,
                tenant_id=tenant_id,
            )
        except TypeError:
            workspace = get_workspace(workspace_id)
    if isinstance(workspace, dict):
        payload["account_id"] = str(workspace.get("account_id") or account_id).strip()
        payload["tenant_id"] = str(workspace.get("tenant_id") or tenant_id).strip()
        return payload
    if not _scope_matches_row(payload, scope):
        raise HTTPException(status_code=404, detail=_MESSAGE_WORKSPACE_NOT_FOUND)
    return payload


def _assert_scoped_template(db, query_template_id: str, scope) -> dict:
    fetch_saved_query = getattr(db, "get_dataset_query_template", None)
    template = fetch_saved_query(query_template_id) if callable(fetch_saved_query) else None
    template = projection_aware_catalog_row(
        db,
        "saved_query",
        "query_template_id",
        query_template_id,
        template,
        account_id=scope.account_id,
    )
    if not isinstance(template, dict):
        raise HTTPException(status_code=404, detail=SAVED_QUERY_NOT_FOUND_MESSAGE)
    dataset_id = str(template.get("dataset_id") or "").strip()
    if not dataset_id:
        raise HTTPException(status_code=404, detail=SAVED_QUERY_NOT_FOUND_MESSAGE)
    _assert_scoped_dataset(db, dataset_id, scope)
    if _template_declares_scope(template):
        _assert_scoped_row(template, scope, SAVED_QUERY_NOT_FOUND_MESSAGE)
    return template


@router.get(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_datasets(
    request: Request,
    enabled_only: Annotated[bool, Query()] = False,
    project_id: Annotated[str | None, Query()] = None,
):
    db = _get_db()
    scope = _request_scope(request)
    datasets = db.list_datasets(enabled_only=enabled_only, project_id=project_id)
    if project_id is not None and not datasets:
        normalized_project_id = str(project_id or "").strip()
        datasets = [
            row
            for row in db.list_datasets(enabled_only=enabled_only)
            if normalized_project_id in _normalize_project_ids(row.get("project_ids"))
        ]
    datasets = projection_aware_catalog_rows(
        db,
        "dataset",
        "dataset_id",
        datasets,
        account_id=scope.account_id,
        enabled_only=enabled_only,
    )
    datasets = [row for row in datasets if _scope_matches_row(row, scope)]
    return {"datasets": datasets}


@router.get(
    "/connectors",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_dataset_connectors(request: Request):
    db = _get_db()
    connectors = _list_dataset_connectors(db, scope=_request_scope(request))
    return {"connectors": connectors, "count": len(connectors)}


@router.get(
    "/data-connections",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_workspace_data_connections(
    request: Request,
    workspace_id: Annotated[str | None, Query()] = None,
):
    db = _get_db()
    scope = _request_scope(request)
    list_connections = getattr(db, "list_workspace_data_connections", None)
    rows = list_connections(workspace_id=workspace_id) if callable(list_connections) else []
    rows = [row for row in rows if _scope_matches_row(row, scope)]
    connector_catalog = _connector_catalog_by_id(db, scope=scope)
    rows = [{**row, **_data_connection_query_capability(row, connector_catalog)} for row in rows]
    return {"connections": rows, "count": len(rows)}


@router.post(
    "/data-connections",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def create_workspace_data_connection(request: Request, body: DataConnectionSave):
    db = _get_db()
    save_connection_with_secrets = getattr(db, "save_workspace_data_connection_with_secrets", None)
    if not callable(save_connection_with_secrets):
        raise HTTPException(status_code=503, detail=DATA_CONNECTION_STORAGE_IS_NOT_AVAILABLE_VALUE)
    scope = _request_scope(request)
    payload = _data_connection_payload(db, body, scope=scope)
    pending_secret_values = dict(payload.pop("_pending_secret_values", {}) or {})
    try:
        row = save_connection_with_secrets(
            payload,
            secret_values=pending_secret_values,
            author=str(getattr(scope, "actor_id", "") or "admin").strip() or "admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "created", "connection": row, "connection_id": payload["connection_id"]}


@router.patch(
    "/data-connections/{connection_id}/toggle",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_workspace_data_connection(request: Request, connection_id: str):
    db = _get_db()
    _assert_scoped_connection(db, connection_id, _request_scope(request))
    toggle_connection = getattr(db, "toggle_workspace_data_connection", None)
    if not callable(toggle_connection):
        raise HTTPException(status_code=503, detail=DATA_CONNECTION_STORAGE_IS_NOT_AVAILABLE_VALUE)
    try:
        enabled = toggle_connection(connection_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "updated", "connection_id": connection_id, "enabled": enabled}


@router.delete(
    "/data-connections/{connection_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def delete_workspace_data_connection(request: Request, connection_id: str):
    db = _get_db()
    _assert_scoped_connection(db, connection_id, _request_scope(request))
    delete_connection = getattr(db, "delete_workspace_data_connection", None)
    if not callable(delete_connection):
        raise HTTPException(status_code=503, detail=DATA_CONNECTION_STORAGE_IS_NOT_AVAILABLE_VALUE)
    scope = _request_scope(request)
    try:
        delete_connection(
            connection_id,
            author=str(getattr(scope, "actor_id", "") or "admin").strip() or "admin",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "deleted", "connection_id": connection_id}


@router.post(
    "/data-connections/{connection_id}/test",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def test_workspace_data_connection(request: Request, connection_id: str):
    db = _get_db()
    scope = _request_scope(request)
    connection = _assert_scoped_connection(db, connection_id, scope)
    response = _invoke_connection_action(connection, "test_connection")
    _persist_connection_operational_state(
        db, connection, response=response, author=scope.actor_name
    )
    return response


@router.post(
    "/data-connections/{connection_id}/introspect",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def introspect_workspace_data_connection(request: Request, connection_id: str):
    db = _get_db()
    scope = _request_scope(request)
    connection = _assert_scoped_connection(db, connection_id, scope)
    response = _invoke_connection_action(connection, "introspect_schema")
    snapshot = {}
    if bool(response.get("ok")):
        snapshot = _validate_json_object(
            _validate_json_object(response.get("result"), field_name="result").get(
                "schema_snapshot"
            ),
            field_name="schema_snapshot",
        )
    _persist_connection_operational_state(
        db,
        connection,
        response=response,
        schema_snapshot=snapshot or None,
        author=scope.actor_name,
    )
    return response


@router.get(
    "/query-templates",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_dataset_query_templates(
    request: Request,
    dataset_id: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
):
    db = _get_db()
    scope = _request_scope(request)
    normalized_dataset_id = str(dataset_id or "").strip()
    list_templates = getattr(db, "list_dataset_query_templates", None)
    rows = (
        list_templates(dataset_id=normalized_dataset_id or None) if callable(list_templates) else []
    )
    rows = projection_aware_catalog_rows(
        db,
        "saved_query",
        "query_template_id",
        rows,
        account_id=scope.account_id,
    )

    def _template_visible(row: dict) -> bool:
        resolved_dataset_id = str(row.get("dataset_id") or "")
        dataset = (
            projection_aware_catalog_row(
                db,
                "dataset",
                "dataset_id",
                resolved_dataset_id,
                db.get_dataset(resolved_dataset_id),
                account_id=scope.account_id,
            )
            or {}
        )
        return (
            (not _template_declares_scope(row) or _scope_matches_row(row, scope))
            and _scope_matches_row(dataset, scope)
            # Project-restricted principals must not see templates whose dataset is
            # not reachable from one of their allowed projects (matches execution
            # routes' _assert_dataset_query_executable guard).
            and _dataset_visible_to_project_restricted_scope(dataset, scope)
        )

    rows = [
        row
        for row in rows
        if _template_matches_dataset(row, normalized_dataset_id) and _template_visible(row)
    ]
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id:
        allowed_dataset_ids = {
            str(row.get("dataset_id") or "").strip()
            for row in db.list_datasets(enabled_only=False, project_id=normalized_project_id)
            if str(row.get("dataset_id") or "").strip()
        }
        rows = [
            row for row in rows if str(row.get("dataset_id") or "").strip() in allowed_dataset_ids
        ]
    return {"query_templates": rows, "count": len(rows)}


@router.post(
    "/query-templates",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        422: {"description": "Invalid saved-query definition"},
        503: {"description": "Service unavailable"},
    },
)
def create_dataset_query_template(request: Request, body: DatasetQueryTemplateSave):
    db = _get_db()
    scope = _request_scope(request)
    payload = body.model_dump()
    dataset = _assert_dataset_reachable(db, str(payload.get("dataset_id") or ""), scope)
    _validate_saved_query_write(payload, dataset=dataset)
    payload["query_template_id"] = (
        str(payload.get("query_template_id") or "").strip() or _query_template_id()
    )
    save_template = getattr(db, "save_dataset_query_template", None)
    if not callable(save_template):
        raise HTTPException(status_code=503, detail=SAVED_QUERY_STORAGE_IS_NOT_AVAILABLE_VALUE)
    row = save_template(payload, author=scope.actor_name)
    return {
        "status": "created",
        "query_template": row,
        "query_template_id": payload["query_template_id"],
    }


@router.put(
    "/query-templates/{query_template_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        422: {"description": "Invalid saved-query definition"},
        503: {"description": "Service unavailable"},
    },
)
def update_dataset_query_template(
    request: Request, query_template_id: str, body: DatasetQueryTemplateSave
):
    db = _get_db()
    scope = _request_scope(request)
    _assert_dataset_query_executable(db, query_template_id, scope)
    payload = body.model_dump()
    payload["query_template_id"] = query_template_id
    dataset = _assert_dataset_reachable(db, str(payload.get("dataset_id") or ""), scope)
    _validate_saved_query_write(payload, dataset=dataset)
    save_template = getattr(db, "save_dataset_query_template", None)
    if not callable(save_template):
        raise HTTPException(status_code=503, detail=SAVED_QUERY_STORAGE_IS_NOT_AVAILABLE_VALUE)
    row = save_template(payload, author=scope.actor_name)
    return {"status": "updated", "query_template": row, "query_template_id": query_template_id}


@router.patch(
    "/query-templates/{query_template_id}/toggle",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_dataset_query_template(request: Request, query_template_id: str):
    db = _get_db()
    _assert_dataset_query_executable(db, query_template_id, _request_scope(request))
    toggle_template = getattr(db, "toggle_dataset_query_template", None)
    if not callable(toggle_template):
        raise HTTPException(status_code=503, detail=SAVED_QUERY_STORAGE_IS_NOT_AVAILABLE_VALUE)
    with dataset_mutation_errors():
        enabled = toggle_template(query_template_id)
    return {"status": "updated", "query_template_id": query_template_id, "enabled": enabled}


@router.delete(
    "/query-templates/{query_template_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def delete_dataset_query_template(request: Request, query_template_id: str):
    db = _get_db()
    _assert_dataset_query_executable(db, query_template_id, _request_scope(request))
    delete_template = getattr(db, "delete_dataset_query_template", None)
    if not callable(delete_template):
        raise HTTPException(status_code=503, detail=SAVED_QUERY_STORAGE_IS_NOT_AVAILABLE_VALUE)
    with dataset_mutation_errors():
        delete_template(query_template_id)
    return {"status": "deleted", "query_template_id": query_template_id}


@router.post(
    "/query-templates/{query_template_id}/validate",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def validate_dataset_query_template(
    request: Request, query_template_id: str, body: DatasetQueryTemplateAction
):
    db = _get_db()
    template = _assert_dataset_query_executable(db, query_template_id, _request_scope(request))
    return _invoke_saved_query_action(
        db, template, "validate_query", body, scope=_request_scope(request)
    )


@router.post(
    "/query-templates/{query_template_id}/preview",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def preview_dataset_query_template(
    request: Request, query_template_id: str, body: DatasetQueryTemplateAction
):
    db = _get_db()
    template = _assert_dataset_query_executable(db, query_template_id, _request_scope(request))
    return _invoke_saved_query_action(
        db, template, "preview_query", body, scope=_request_scope(request)
    )


@router.post(
    "/query-templates/{query_template_id}/infer-output-schema",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def infer_dataset_query_template_output(
    request: Request, query_template_id: str, body: DatasetQueryTemplateAction
):
    db = _get_db()
    template = _assert_dataset_query_executable(db, query_template_id, _request_scope(request))
    return _invoke_saved_query_action(
        db, template, "infer_output_schema", body, scope=_request_scope(request)
    )


@router.get(
    "/project/{project_id}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Project access denied"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_project_dataset_assignment(request: Request, project_id: str):
    db = _get_db()
    scope = _request_scope(request)
    normalized_project_id = _assert_scoped_project(db, project_id, scope)
    list_catalog = getattr(db, "list_project_dataset_catalog", None)
    if callable(list_catalog):
        try:
            catalog = list_catalog(normalized_project_id)
            if isinstance(catalog, dict):
                catalog["available_datasets"] = [
                    row
                    for row in catalog.get("available_datasets") or []
                    if _scope_matches_row(row, scope)
                ]
            return catalog
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    available = db.list_datasets(enabled_only=False)
    linked = db.list_datasets(enabled_only=False, project_id=normalized_project_id)
    return {
        "project_id": normalized_project_id,
        "dataset_ids": [str(row.get("dataset_id") or "") for row in linked],
        "explicit_dataset_ids": [str(row.get("dataset_id") or "") for row in linked],
        "available_datasets": [row for row in available if _scope_matches_row(row, scope)],
    }


@router.put(
    "/project/{project_id}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Project access denied"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def update_project_dataset_assignment(
    request: Request, project_id: str, body: ProjectDatasetAssignment
):
    db = _get_db()
    scope = _request_scope(request)
    normalized_project_id = _assert_scoped_project(db, project_id, scope)
    dataset_ids = [str(item or "").strip() for item in body.dataset_ids if str(item or "").strip()]
    for dataset_id in dataset_ids:
        _assert_scoped_dataset(db, dataset_id, scope)
    set_project_datasets = getattr(db, "set_project_datasets", None)
    try:
        if callable(set_project_datasets):
            linked = set_project_datasets(
                normalized_project_id, dataset_ids, author=scope.actor_name
            )
        else:
            linked = _set_project_datasets_via_dataset_updates(
                db, normalized_project_id, dataset_ids, author=scope.actor_name
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    linked_dataset_ids = [str(row.get("dataset_id") or "") for row in linked]
    if not linked_dataset_ids:
        linked_dataset_ids = dataset_ids
    return {
        "status": "updated",
        "project_id": normalized_project_id,
        "dataset_ids": linked_dataset_ids,
        "datasets": linked,
    }


@router.get(
    "/{dataset_id}/projects",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def list_dataset_project_assignment(request: Request, dataset_id: str):
    db = _get_db()
    _assert_scoped_dataset(db, dataset_id, _request_scope(request))
    list_catalog = getattr(db, "list_dataset_project_catalog", None)
    if not callable(list_catalog):
        raise HTTPException(status_code=503, detail="Dataset project catalog is not available")
    try:
        return saved_query_project_catalog(
            db, db.get_dataset(dataset_id), list_catalog(dataset_id), scope=_request_scope(request)
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.payload) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/{dataset_id}/projects",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Project access denied"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def update_dataset_project_assignment(
    request: Request, dataset_id: str, body: DatasetProjectAssignment
):
    db = _get_db()
    scope = _request_scope(request)
    existing = _assert_scoped_dataset(db, dataset_id, scope)
    if str(existing.get("availability_mode") or "") == "all_workspace_projects":
        raise HTTPException(
            status_code=400,
            detail="Workspace-wide datasets do not use explicit project assignments",
        )
    payload = {
        **existing,
        "dataset_id": dataset_id,
        "project_ids": [
            str(item or "").strip() for item in body.project_ids if str(item or "").strip()
        ],
    }
    for project_id in payload["project_ids"]:
        _assert_scoped_project(db, project_id, scope)
    payload = _validate_dataset_payload(
        {**payload, "connection_id": _dataset_connection_id(payload)},
        db=db,
        connector_catalog=_connector_catalog_by_id(db, scope=scope),
    )
    db.save_dataset(payload, author=scope.actor_name)
    return {
        "status": "updated",
        "dataset_id": dataset_id,
        "project_ids": payload["project_ids"],
    }


@router.get(
    "/{dataset_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def get_dataset(request: Request, dataset_id: str):
    db = _get_db()
    return _assert_scoped_dataset(db, dataset_id, _request_scope(request))


@router.post(
    "",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def create_dataset(request: Request, body: DatasetSave):
    db = _get_db()
    scope = _request_scope(request)
    raw_payload = body.model_dump()
    dataset_id = str(raw_payload.get("dataset_id") or "").strip()
    if not dataset_id:
        dataset_id = _generate_dataset_id(db, raw_payload.get("display_name") or "dataset")
        raw_payload["dataset_id"] = dataset_id
    if db.get_dataset(dataset_id, include_trashed=True):
        raise HTTPException(status_code=409, detail=f"Dataset '{dataset_id}' already exists")
    payload = _validate_dataset_payload(
        raw_payload,
        db=db,
        connector_catalog=_connector_catalog_by_id(db, scope=scope),
    )
    payload = _assert_scoped_workspace_payload(db, payload, scope)
    if not _scope_matches_row(payload, scope):
        raise HTTPException(status_code=404, detail=_MESSAGE_WORKSPACE_NOT_FOUND)
    db.save_dataset(payload, author=scope.actor_name)
    return {"status": "created", "dataset_id": dataset_id}


@router.put(
    "/{dataset_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def update_dataset(request: Request, dataset_id: str, body: DatasetSave):
    db = _get_db()
    scope = _request_scope(request)
    existing = _assert_scoped_dataset(db, dataset_id, scope)
    payload = body.model_dump()
    if "metadata_jsonb" not in body.model_fields_set:
        payload["metadata_jsonb"] = existing.get("metadata_jsonb") or {}
    payload["dataset_id"] = dataset_id
    existing_workspace_id = str(existing.get("workspace_id") or "").strip()
    requested_workspace_id = str(payload.get("workspace_id") or existing_workspace_id).strip()
    if (
        existing_workspace_id
        and requested_workspace_id
        and requested_workspace_id != existing_workspace_id
    ):
        raise HTTPException(
            status_code=400,
            detail="workspace_id cannot be changed after dataset creation",
        )
    payload = _validate_dataset_payload(
        payload, db=db, connector_catalog=_connector_catalog_by_id(db, scope=scope)
    )
    payload = _assert_scoped_workspace_payload(db, payload, scope)
    if not _scope_matches_row(payload, scope):
        raise HTTPException(status_code=404, detail=_MESSAGE_WORKSPACE_NOT_FOUND)
    db.save_dataset(payload, author=scope.actor_name)
    return {"status": "updated", "dataset_id": dataset_id}


@router.patch(
    "/{dataset_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def patch_dataset(request: Request, dataset_id: str, body: DatasetPatch):
    db = _get_db()
    scope = _request_scope(request)
    existing = _assert_scoped_dataset(db, dataset_id, scope)
    updates = body.model_dump(exclude_none=True)
    existing_workspace_id = str(existing.get("workspace_id") or "").strip()
    requested_workspace_id = str(updates.get("workspace_id") or existing_workspace_id).strip()
    if (
        existing_workspace_id
        and requested_workspace_id
        and requested_workspace_id != existing_workspace_id
    ):
        raise HTTPException(
            status_code=400,
            detail="workspace_id cannot be changed after dataset creation",
        )
    merged = {
        **existing,
        **updates,
        "dataset_id": dataset_id,
        "connection_id": str(updates.get("connection_id") or "").strip()
        or _dataset_connection_id(existing),
    }
    payload = _validate_dataset_payload(
        merged, db=db, connector_catalog=_connector_catalog_by_id(db, scope=scope)
    )
    payload = _assert_scoped_workspace_payload(db, payload, scope)
    if not _scope_matches_row(payload, scope):
        raise HTTPException(status_code=404, detail=_MESSAGE_WORKSPACE_NOT_FOUND)
    db.save_dataset(payload, author=scope.actor_name)
    return {"status": "updated", "dataset_id": dataset_id}


@router.patch(
    "/{dataset_id}/toggle",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def toggle_dataset(request: Request, dataset_id: str):
    db = _get_db()
    _assert_scoped_dataset(db, dataset_id, _request_scope(request))
    with dataset_mutation_errors():
        enabled = db.toggle_dataset(dataset_id)
    return {"status": "updated", "dataset_id": dataset_id, "enabled": enabled}


@router.delete(
    "/{dataset_id}",
    responses={
        400: {"description": "Bad request"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        503: {"description": "Service unavailable"},
    },
)
def delete_dataset(request: Request, dataset_id: str):
    db = _get_db()
    _assert_scoped_dataset(db, dataset_id, _request_scope(request))
    with dataset_mutation_errors():
        db.delete_dataset(dataset_id, author=_request_scope(request).actor_name)
    return {"status": "deleted", "dataset_id": dataset_id}
