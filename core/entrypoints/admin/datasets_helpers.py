# mypy: ignore-errors
"""Helper functions for dataset admin routes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.datasets.catalog_scope import _scope_matches_row
from core.application.datasets.definition_values import (
    _apply_schema_defaults,
    _non_empty_secret_values,
    _normalize_project_ids,
    _normalize_tags,
)
from core.application.datasets.identifier_repository import (
    _generate_connection_id,
)
from core.application.datasets.saved_query_context import resolve_saved_query_action_connection
from core.application.management.errors import ServiceError
from core.application.management.scope import ScopeContext
from core.application.registry.data_connection_binding import (
    resolve_connection_dataset_connector,
)
from core.entrypoints.admin.datasets_models import (
    DataConnectionSave,
    DatasetQueryTemplateAction,
)

DATASET_NOT_FOUND_MESSAGE = "Dataset not found"
DATA_CONNECTION_STORAGE_IS_NOT_AVAILABLE_VALUE = "Data connection storage is not available"
SAVED_QUERY_NOT_FOUND_MESSAGE = "Saved query not found"
SAVED_QUERY_STORAGE_IS_NOT_AVAILABLE_VALUE = "Saved query storage is not available"

_DATASET_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ALLOWED_CONNECTOR_KINDS = {"builtin", "extension", "manual", "mcp_connector"}
_ALLOWED_RUNTIME_KINDS = {"builtin", "local_extension", "manual", "mcp_bridge", "remote_extension"}
_ALLOWED_DATASET_AVAILABILITY_MODES = {
    "all_workspace_projects",
    "specific_projects_only",
}

_SERVER_OWNED_DATASET_METADATA_KEYS = {
    "account_id",
    "availability_mode",
    "connection_id",
    "connection_resolution",
    "dataset_connector",
    "projection_bridge",
    "source_install_id",
    "source_release_id",
    "tenant_id",
    "workspace_id",
}


def _validate_json_object(value: Any, *, field_name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must be an object")
    return value


def _existing_secret_refs(secret_refs: Any) -> dict[str, str]:
    payload = _validate_json_object(secret_refs, field_name="secret_refs_jsonb")
    raw_refs = _validate_json_object(payload.get("refs"), field_name="secret_refs_jsonb.refs")
    return {
        str(name).strip(): str(reference).strip()
        for name, reference in raw_refs.items()
        if str(name).strip() and str(reference).strip()
    }


def _validate_json_schema_value(*, schema: dict, value: Any, field_name: str) -> None:
    if not schema:
        return
    from core.application.management.command_services_common import _json_schema_validate_value

    errors: list[str] = []
    _json_schema_validate_value(schema=schema, value=value, path=field_name, errors=errors)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))


def _validate_connection_config(config: dict, *, schema: dict) -> dict:
    normalized = _apply_schema_defaults(schema, config)
    _validate_json_schema_value(
        schema=schema,
        value=normalized,
        field_name="connection_config_jsonb",
    )
    return normalized


def _validate_connection_secrets(
    *,
    schema: dict,
    secret_values: dict[str, str],
    existing_refs: dict[str, str],
) -> None:
    if not schema:
        return
    properties = schema.get("properties")
    properties_map = properties if isinstance(properties, dict) else {}
    supplied_names = set(secret_values) | set(existing_refs)
    required = {
        str(item or "").strip() for item in schema.get("required") or [] if str(item or "").strip()
    }
    missing = sorted(required - supplied_names)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"secret_refs_jsonb.{missing[0]}: required property is missing",
        )
    if schema.get("additionalProperties", True) is False:
        unknown = sorted(supplied_names - set(properties_map))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"secret_refs_jsonb.{unknown[0]}: additional property is not allowed",
            )
    _validate_json_schema_value(
        schema={**schema, "required": []},
        value=secret_values,
        field_name="secret_refs_jsonb",
    )


def _validate_json_object_list(value: Any, *, field_name: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a list")
    rows: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"{field_name} items must be objects")
        rows.append(item)
    return rows


def _normalize_connector_row(row: dict) -> dict | None:
    connector_id = str(row.get("connector_id") or row.get("id") or "").strip()
    if not connector_id:
        return None
    connector_kind = str(row.get("connector_kind") or "extension").strip().lower()
    runtime_kind = str(row.get("runtime_kind") or "local_extension").strip().lower()
    return {
        "connector_id": connector_id,
        "local_connector_id": str(row.get("local_connector_id") or connector_id).strip(),
        "display_name": str(row.get("display_name") or row.get("name") or connector_id).strip(),
        "description": str(row.get("description") or "").strip(),
        "connector_kind": connector_kind
        if connector_kind in _ALLOWED_CONNECTOR_KINDS
        else "extension",
        "runtime_kind": runtime_kind
        if runtime_kind in _ALLOWED_RUNTIME_KINDS
        else "local_extension",
        "query_language": str(row.get("query_language") or "sql").strip().lower() or "sql",
        "connection_schema": _validate_json_object(
            row.get("connection_schema") or row.get("connection_schema_jsonb"),
            field_name="connection_schema",
        ),
        "secret_schema": _validate_json_object(
            row.get("secret_schema"), field_name="secret_schema"
        ),
        "capabilities": _validate_json_object(row.get("capabilities"), field_name="capabilities"),
        "safety_profile": _validate_json_object(
            row.get("safety_profile"), field_name="safety_profile"
        ),
        "preview_support": bool(row.get("preview_support", False)),
        "schema_refresh_support": bool(row.get("schema_refresh_support", False)),
        "runtime_adapter_ref": str(row.get("runtime_adapter_ref") or "").strip(),
        "source_install_id": str(row.get("source_install_id") or "").strip(),
        "source_release_id": str(row.get("source_release_id") or "").strip(),
        "extension_id": str(row.get("extension_id") or "").strip(),
        "account_id": str(row.get("account_id") or "").strip(),
        "tenant_id": str(row.get("tenant_id") or "").strip(),
        "workspace_id": str(row.get("workspace_id") or "").strip(),
        "connector_catalog": _validate_json_object_list(
            row.get("connector_catalog"), field_name="connector_catalog"
        ),
    }


def _list_dataset_connectors(db, *, scope: Any | None = None) -> list[dict]:
    list_connectors = getattr(db, "list_dataset_connectors", None)
    if not callable(list_connectors):
        return []
    rows = list_connectors() or []
    connectors: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_connector_row(row)
        if not normalized or normalized["connector_id"] in seen:
            continue
        if scope is not None and not _scope_matches_row(normalized, scope):
            continue
        connectors.append(normalized)
        seen.add(normalized["connector_id"])
    connectors.sort(key=lambda item: (item["display_name"].lower(), item["connector_id"]))
    return connectors


def _connector_catalog_by_id(db, *, scope: Any | None = None) -> dict[str, dict]:
    return {row["connector_id"]: row for row in _list_dataset_connectors(db, scope=scope)}


def _dataset_connector_for_connection(
    connection: dict,
    connector_catalog: dict[str, dict],
) -> dict:
    connector, issue = resolve_connection_dataset_connector(
        connection,
        connector_catalog,
    )
    if issue:
        raise HTTPException(status_code=400, detail=issue)
    return dict(connector or {})


def _data_connection_query_capability(
    connection: dict,
    connector_catalog: dict[str, dict],
) -> dict[str, Any]:
    connector, issue = resolve_connection_dataset_connector(connection, connector_catalog)
    if issue:
        return {
            "query_capable": False,
            "query_capability_issue": issue,
        }
    return {
        "query_capable": True,
        "query_capability_issue": "",
        "dataset_connector": {
            "connector_id": connector["connector_id"],
            "display_name": connector.get("display_name", ""),
            "source_install_id": connector.get("source_install_id", ""),
            "extension_id": connector.get("extension_id", ""),
        },
    }


def _connector_catalog_variant(connector: dict, local_connector_id: str) -> dict:
    requested_id = str(local_connector_id or "").strip()
    if not requested_id:
        return {}
    for item in connector.get("connector_catalog") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("connector_type_id") or "").strip()
        if candidate_id == requested_id:
            return dict(item)
    return {}


def _connection_extension(db, connection: dict):
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        resolve_extension_runtime_record,
        resolve_extension_runtime_record_by_install_id,
    )

    ref = _validate_json_object(
        connection.get("connector_ref_jsonb") or connection.get("connector_ref"),
        field_name="connector_ref_jsonb",
    )
    dsn = str(getattr(db, "dsn", "") or "").strip()
    account_id = str(connection.get("account_id") or "").strip()
    get_logger_only = None
    if dsn:
        from core.infrastructure.runtime.entrypoint_db_adapters import create_identity_store

        def get_logger_only():
            return create_identity_store(dsn)

    install_id = str(ref.get("install_id") or "").strip()
    if install_id:
        extension, _logger = resolve_extension_runtime_record_by_install_id(
            install_id,
            get_logger_only=get_logger_only,
            account_id=account_id or None,
        )
        if extension:
            return extension
        raise HTTPException(
            status_code=503, detail="Dataset connector extension runtime is unavailable"
        )

    for key in ("extension_key", "extension_id", "source_extension_id"):
        candidate = str(ref.get(key) or "").strip()
        if not candidate:
            continue
        extension, _logger = resolve_extension_runtime_record(
            candidate,
            get_logger_only=get_logger_only,
            account_id=account_id or None,
        )
        if extension:
            return extension
    raise HTTPException(
        status_code=503, detail="Dataset connector extension runtime is unavailable"
    )


def _extension_runtime_id(extension: dict[str, Any] | None) -> str:
    payload = _validate_json_object(extension, field_name="extension")
    compiled = _validate_json_object(
        payload.get("compiled_manifest"), field_name="compiled_manifest"
    )
    return (
        str(payload.get("extension_key") or "").strip()
        or str(payload.get("extension_id") or "").strip()
        or str(compiled.get("extension_id") or "").strip()
    )


def _project_id_for_dataset_action(dataset: dict[str, Any], connection: dict[str, Any]) -> str:
    project_ids = [str(item or "").strip() for item in dataset.get("project_ids") or [] if item]
    if len(project_ids) == 1:
        return project_ids[0]
    return str(connection.get("project_id") or "").strip()


def _process_connection_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any],
    db: Any,
    connection: dict[str, Any],
    project_id: str,
) -> None:
    if invocation.get("status") != "succeeded":
        return
    try:
        from core.application.extensions.response_envelope import process_project_state_updates

        process_project_state_updates(
            invocation,
            extension=extension,
            extension_id=_extension_runtime_id(extension),
            db=db,
            account_id=str(connection.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
            or DEFAULT_ACCOUNT_ID,
            project_id=str(project_id or "").strip(),
            connection_id=str(connection.get("connection_id") or "").strip() or None,
            actor_id=None,
        )
    except Exception as exc:
        invocation["status"] = "failed"
        invocation["detail"] = "project_state_update_failed"
        invocation["errors"] = [
            *list(invocation.get("errors") or []),
            {"code": "project_state_update_failed", "message": str(exc)},
        ]


def _response_from_invocation(invocation: dict[str, Any]) -> dict:
    response = _response_envelope(invocation)
    if invocation.get("status") == "succeeded":
        return response
    errors = list(response.get("errors") or []) if isinstance(response.get("errors"), list) else []
    errors.extend(list(invocation.get("errors") or []))
    detail = str(invocation.get("detail") or "").strip()
    if detail and not errors:
        errors.append({"code": detail, "message": detail})
    return {
        **response,
        "ok": False,
        "errors": errors,
    }


def _invoke_connection_action(db, connection: dict, action_id: str) -> dict:
    from core.application.datasets.connection_actions import execute_connection_action

    return execute_connection_action(
        connection,
        action_id,
        db=db,
        source="registry_data_connections",
    )


def _response_envelope(invocation: dict[str, Any]) -> dict:
    response = invocation.get("response") if isinstance(invocation, dict) else None
    if isinstance(response, dict):
        return response
    return {
        "ok": bool(isinstance(invocation, dict) and invocation.get("status") == "succeeded"),
        "result": {},
        "commands": [],
        "events": [],
        "reload": False,
        "errors": [],
    }


def _response_error_message(response: dict[str, Any]) -> str:
    def parse_error_value(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("error", "detail", "message", "code"):
                message = parse_error_value(value.get(key))
                if message:
                    return message
            return ""
        if isinstance(value, list):
            for item in value:
                message = parse_error_value(item)
                if message:
                    return message
            return ""
        message = str(value or "").strip()
        if not message:
            return ""
        if message[0] in "{[":
            try:
                parsed = json.loads(message)
            except Exception:
                return message
            parsed_message = parse_error_value(parsed)
            return parsed_message or message
        return message

    errors = response.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if not isinstance(item, dict):
                continue
            message = parse_error_value(item.get("message")) or parse_error_value(item.get("code"))
            if message:
                return message
    return ""


def _persist_connection_operational_state(
    db,
    connection: dict[str, Any],
    *,
    response: dict[str, Any],
    schema_snapshot: dict[str, Any] | None = None,
    author: str = "system",
) -> None:
    save_connection = getattr(db, "save_workspace_data_connection", None)
    if not callable(save_connection):
        return
    metadata = _validate_json_object(
        connection.get("metadata_jsonb") or connection.get("metadata"),
        field_name="metadata_jsonb",
    )
    if schema_snapshot:
        metadata = {**metadata, "last_schema_snapshot": schema_snapshot}
    now = datetime.now(UTC)
    ok = bool(response.get("ok"))
    save_connection(
        {
            **connection,
            "health_status": "healthy" if ok else "unhealthy",
            "last_checked_at": now,
            "last_schema_refresh_at": now
            if ok and schema_snapshot
            else connection.get("last_schema_refresh_at"),
            "last_error": "" if ok else _response_error_message(response),
            "metadata_jsonb": metadata,
        },
        author=author,
    )


def _invoke_saved_query_action(
    db,
    template: dict,
    action_id: str,
    body: DatasetQueryTemplateAction,
    *,
    scope: ScopeContext | None = None,
) -> dict:
    from core.application.orchestration.dataset_query_step_runtime import (
        _connector_action_input,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    dataset = db.get_dataset(str(template.get("dataset_id") or ""))
    if not dataset:
        raise HTTPException(status_code=404, detail=DATASET_NOT_FOUND_MESSAGE)
    from core.application.registry.saved_query_contract import (
        SavedQueryContractError,
        validate_saved_query_contract,
    )

    try:
        validate_saved_query_contract(template, dataset=dataset)
    except SavedQueryContractError as exc:
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
    try:
        connection, project_id = resolve_saved_query_action_connection(
            db, dataset, scope=scope, project_id=body.project_id
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.payload) from exc
    if not connection:
        raise HTTPException(status_code=404, detail="Dataset data connection not found")
    row_limit = max(1, min(int(body.row_limit or 100), 1000))
    extension = _connection_extension(db, connection)
    invocation = run_extension_action(
        extension,
        action_id=action_id,
        context={
            "workspace_id": str(connection.get("workspace_id") or ""),
            "connection_id": str(connection.get("connection_id") or ""),
            "dataset_id": str(dataset.get("dataset_id") or ""),
            **({"project_id": project_id} if project_id else {}),
        },
        input_payload=_connector_action_input(
            db=db,
            query_template_id=str(template.get("query_template_id") or ""),
            template=template,
            dataset=dataset,
            connection=connection,
            params=_validate_json_object(body.params, field_name="params"),
            row_limit=row_limit,
            timeout_seconds=30,
        ),
        runtime_context={"source": "registry_saved_queries"},
        timeout_seconds=30,
    )
    if isinstance(invocation, dict):
        _process_connection_project_state_updates(
            invocation,
            extension=extension,
            db=db,
            connection=connection,
            project_id=project_id or _project_id_for_dataset_action(dataset, connection),
        )
        return _response_from_invocation(invocation)
    return _response_envelope({})


def _validate_dataset_payload(
    payload: dict,
    *,
    db=None,
    connector_catalog: dict[str, dict] | None = None,
) -> dict:
    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not dataset_id:
        raise HTTPException(status_code=400, detail="dataset_id is required")
    if not _DATASET_ID_PATTERN.match(dataset_id):
        raise HTTPException(
            status_code=400,
            detail="dataset_id must match [a-z][a-z0-9_.-]*",
        )
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")
    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required")
    availability_mode = str(payload.get("availability_mode") or "specific_projects_only").strip()
    if availability_mode not in _ALLOWED_DATASET_AVAILABILITY_MODES:
        allowed = ", ".join(sorted(_ALLOWED_DATASET_AVAILABILITY_MODES))
        raise HTTPException(status_code=400, detail=f"availability_mode must be one of: {allowed}")

    connection_id = str(payload.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(status_code=400, detail="connection_id is required")
    get_connection = getattr(db, "get_workspace_data_connection", None)
    connection = get_connection(connection_id) if callable(get_connection) else None
    if not isinstance(connection, dict):
        raise HTTPException(status_code=400, detail="Selected data connection was not found")
    connection_workspace_id = str(connection.get("workspace_id") or "").strip()
    if connection_workspace_id != workspace_id:
        raise HTTPException(
            status_code=400,
            detail="Selected data connection must belong to the same workspace",
        )
    connector = _dataset_connector_for_connection(connection, connector_catalog or {})
    connector_id = str(connector["connector_id"]).strip()
    connector_kind = str(connector.get("connector_kind") or "extension").strip().lower()
    if connector_kind not in _ALLOWED_CONNECTOR_KINDS:
        allowed = ", ".join(sorted(_ALLOWED_CONNECTOR_KINDS))
        raise HTTPException(status_code=400, detail=f"connector_kind must be one of: {allowed}")

    runtime_kind = str(connector.get("runtime_kind") or "local_extension").strip().lower()
    if runtime_kind not in _ALLOWED_RUNTIME_KINDS:
        allowed = ", ".join(sorted(_ALLOWED_RUNTIME_KINDS))
        raise HTTPException(status_code=400, detail=f"runtime_kind must be one of: {allowed}")

    query_language = str(connector.get("query_language") or "").strip().lower()
    if not query_language:
        raise HTTPException(status_code=400, detail="query_language is required")

    requested_metadata = _validate_json_object(
        payload.get("metadata_jsonb") or payload.get("metadata"),
        field_name="metadata_jsonb",
    )
    resource_scope_jsonb = _normalize_resource_scope(
        payload.get("resource_scope_jsonb")
        or payload.get("resource_scope")
        or requested_metadata.get("resource_scope_jsonb")
        or requested_metadata.get("resource_scope")
    )
    metadata_jsonb = {
        **{
            key: value
            for key, value in requested_metadata.items()
            if key not in _SERVER_OWNED_DATASET_METADATA_KEYS
        },
        "resource_scope_jsonb": resource_scope_jsonb,
        "workspace_id": workspace_id,
        "availability_mode": availability_mode,
        "connection_id": connection_id,
        "dataset_connector": {
            "connector_id": connector["connector_id"],
            "local_connector_id": connector.get("local_connector_id", ""),
            "display_name": connector.get("display_name", ""),
            "source_install_id": connector.get("source_install_id", ""),
            "source_release_id": connector.get("source_release_id", ""),
            "extension_id": connector.get("extension_id", ""),
        },
    }

    schema_snapshot_jsonb = _validate_json_object(
        payload.get("schema_snapshot_jsonb") or payload.get("schema_snapshot"),
        field_name="schema_snapshot_jsonb",
    )
    if resource_scope_jsonb.get("resources"):
        schema_snapshot_jsonb = _derive_dataset_schema_snapshot(
            db,
            connection_id=connection_id,
            resource_scope_jsonb=resource_scope_jsonb,
        )
    project_ids = _normalize_project_ids(payload.get("project_ids"))
    if availability_mode == "all_workspace_projects":
        project_ids = []
    if db is not None and workspace_id and project_ids:
        get_project_scope = getattr(db, "_get_project_scope", None)
        incompatible_project_ids: list[str] = []
        missing_project_ids: list[str] = []
        for project_id in project_ids:
            project_scope = get_project_scope(project_id) if callable(get_project_scope) else None
            if not project_scope:
                missing_project_ids.append(project_id)
                continue
            project_workspace_id = str(project_scope.get("workspace_id") or "").strip()
            if project_workspace_id != workspace_id:
                incompatible_project_ids.append(project_id)
        if missing_project_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown project(s): {', '.join(sorted(set(missing_project_ids)))}",
            )
        if incompatible_project_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Projects must belong to the same workspace as the dataset: "
                    f"{', '.join(sorted(set(incompatible_project_ids)))}"
                ),
            )

    return {
        **payload,
        "dataset_id": dataset_id,
        "display_name": display_name,
        "workspace_id": workspace_id,
        "availability_mode": availability_mode,
        "description": str(payload.get("description") or "").strip(),
        "connector_kind": connector_kind,
        "connector_id": connector_id,
        "runtime_kind": runtime_kind,
        "query_language": query_language,
        "read_only": bool(payload.get("read_only", True)),
        "enabled": bool(payload.get("enabled", True)),
        "schema_snapshot_jsonb": schema_snapshot_jsonb,
        "connection_schema_jsonb": _validate_json_object(
            connector.get("connection_schema"),
            field_name="connection_schema_jsonb",
        ),
        "metadata_jsonb": metadata_jsonb,
        "project_ids": project_ids,
        "tags": [
            str(tag or "").strip() for tag in (payload.get("tags") or []) if str(tag or "").strip()
        ],
    }


def _normalize_resource_scope(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"version": 1, "resources": []}
    scope = _validate_json_object(raw, field_name="resource_scope_jsonb")
    resources = []
    for entry in scope.get("resources") or []:
        if not isinstance(entry, dict):
            continue
        resource_id = str(entry.get("resource_id") or "").strip()
        if not resource_id:
            continue
        raw_columns = entry.get("columns")
        columns = None
        if isinstance(raw_columns, list):
            normalized = [
                str(column or "").strip() for column in raw_columns if str(column or "").strip()
            ]
            columns = normalized
        resources.append({"resource_id": resource_id, "columns": columns})
    return {"version": int(scope.get("version") or 1), "resources": resources}


def _connection_schema_snapshot(connection: dict) -> dict[str, Any]:
    metadata = _validate_json_object(
        connection.get("metadata_jsonb") or connection.get("metadata"),
        field_name="metadata_jsonb",
    )
    return _validate_json_object(
        metadata.get("last_schema_snapshot") or metadata.get("schema_snapshot"),
        field_name="schema_snapshot_jsonb",
    )


def _filter_schema_snapshot(
    schema_snapshot: dict[str, Any], resource_scope_jsonb: dict[str, Any]
) -> dict[str, Any]:
    allowed_resources = {
        str(resource.get("resource_id") or "").strip(): resource.get("columns")
        for resource in resource_scope_jsonb.get("resources") or []
        if isinstance(resource, dict) and str(resource.get("resource_id") or "").strip()
    }
    filtered_resources = []
    for resource in schema_snapshot.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        resource_id = str(resource.get("resource_id") or resource.get("name") or "").strip()
        if not resource_id or resource_id not in allowed_resources:
            continue
        allowed_columns = allowed_resources[resource_id]
        filtered_resource = dict(resource)
        if isinstance(allowed_columns, list):
            allowed = {
                str(column or "").strip() for column in allowed_columns if str(column or "").strip()
            }
            filtered_resource["columns"] = [
                dict(column)
                for column in resource.get("columns") or []
                if isinstance(column, dict) and str(column.get("name") or "").strip() in allowed
            ]
        filtered_resources.append(filtered_resource)
    return {
        **schema_snapshot,
        "resources": filtered_resources,
        "metadata": {
            **_validate_json_object(schema_snapshot.get("metadata"), field_name="metadata"),
            "selected_resource_count": len(filtered_resources),
        },
    }


def _derive_dataset_schema_snapshot(
    db, *, connection_id: str, resource_scope_jsonb: dict[str, Any]
) -> dict[str, Any]:
    get_connection = getattr(db, "get_workspace_data_connection", None)
    connection = get_connection(connection_id) if callable(get_connection) else None
    if not connection:
        raise HTTPException(status_code=400, detail="Selected data connection was not found")
    schema_snapshot = _connection_schema_snapshot(connection)
    if not schema_snapshot.get("resources"):
        raise HTTPException(
            status_code=400,
            detail="Selected data connection must be introspected before creating a dataset",
        )
    filtered = _filter_schema_snapshot(schema_snapshot, resource_scope_jsonb)
    if not filtered.get("resources"):
        raise HTTPException(
            status_code=400,
            detail="Select at least one resource from the latest connection schema snapshot",
        )
    return filtered


def _workspace_row_for_connection(db, workspace_id: str, scope: Any) -> dict:
    get_workspace = getattr(db, "get_workspace", None)
    if not callable(get_workspace):
        return {
            "id": workspace_id,
            "account_id": str(getattr(scope, "account_id", "") or DEFAULT_ACCOUNT_ID).strip(),
            "tenant_id": str(getattr(scope, "tenant_id", "") or DEFAULT_TENANT_ID).strip(),
        }
    try:
        workspace = get_workspace(
            workspace_id,
            account_id=str(getattr(scope, "account_id", "") or DEFAULT_ACCOUNT_ID).strip(),
            tenant_id=str(getattr(scope, "tenant_id", "") or DEFAULT_TENANT_ID).strip(),
            include_archived=False,
        )
    except TypeError:
        workspace = get_workspace(workspace_id)
    if not isinstance(workspace, dict):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _data_connection_payload(db, body: DataConnectionSave, *, scope: Any) -> dict:
    raw = body.model_dump()
    workspace_id = str(raw.get("workspace_id") or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required")
    workspace = _workspace_row_for_connection(db, workspace_id, scope)

    display_name = str(raw.get("display_name") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")

    connector_id = str(raw.get("connector_id") or "").strip()
    if not connector_id:
        raise HTTPException(status_code=400, detail="connector_id is required")
    connector = _connector_catalog_by_id(db, scope=scope).get(connector_id)
    if not connector:
        raise HTTPException(status_code=400, detail=f"Unknown dataset connector '{connector_id}'")

    connection_id = str(raw.get("connection_id") or "").strip() or _generate_connection_id(
        db, display_name
    )
    metadata = _validate_json_object(raw.get("metadata_jsonb"), field_name="metadata_jsonb")
    requested_dataset_connector = _validate_json_object(
        metadata.get("dataset_connector"), field_name="metadata_jsonb.dataset_connector"
    )
    requested_local_connector_id = str(
        requested_dataset_connector.get("local_connector_id") or ""
    ).strip()
    catalog_variant = _connector_catalog_variant(connector, requested_local_connector_id)
    if requested_local_connector_id and not catalog_variant and connector.get("connector_catalog"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown connector variant '{requested_local_connector_id}' for "
                f"'{connector['connector_id']}'"
            ),
        )
    local_connector_id = str(
        catalog_variant.get("connector_type_id")
        or connector.get("local_connector_id")
        or connector["connector_id"]
    ).strip()
    variant_preset_config = _validate_json_object(
        catalog_variant.get("preset_config"), field_name="connector_catalog.preset_config"
    )
    incoming_connection_config = _validate_json_object(
        raw.get("connection_config_jsonb"), field_name="connection_config_jsonb"
    )
    conflicting_preset_keys = sorted(
        key
        for key, value in variant_preset_config.items()
        if key in incoming_connection_config and incoming_connection_config[key] != value
    )
    if conflicting_preset_keys:
        raise HTTPException(
            status_code=400,
            detail=(
                f"connection_config_jsonb.{conflicting_preset_keys[0]} must match "
                "the selected connector variant"
            ),
        )
    connection_schema = _validate_json_object(
        catalog_variant.get("connection_schema") or connector.get("connection_schema"),
        field_name="connection_schema",
    )
    secret_schema = _validate_json_object(
        catalog_variant.get("secret_schema") or connector.get("secret_schema"),
        field_name="secret_schema",
    )
    metadata["dataset_connector"] = {
        "connector_id": connector["connector_id"],
        "local_connector_id": local_connector_id,
        "display_name": connector.get("display_name", ""),
        "source_install_id": connector.get("source_install_id", ""),
        "source_release_id": connector.get("source_release_id", ""),
        "extension_id": connector.get("extension_id", ""),
    }
    get_connection = getattr(db, "get_workspace_data_connection", None)
    existing = (
        get_connection(connection_id, include_trashed=True)
        if callable(get_connection) and str(raw.get("connection_id") or "").strip()
        else None
    )
    if isinstance(existing, dict) and not _scope_matches_row(existing, scope):
        raise HTTPException(status_code=404, detail="Data connection not found")
    connection_config_jsonb = _validate_connection_config(
        {**variant_preset_config, **incoming_connection_config},
        schema=connection_schema,
    )
    tenant_id = str(workspace.get("tenant_id") or getattr(scope, "tenant_id", "")).strip()
    account_id = str(workspace.get("account_id") or getattr(scope, "account_id", "")).strip()
    incoming_secret_refs = _validate_json_object(
        raw.get("secret_refs_jsonb"), field_name="secret_refs_jsonb"
    )
    secret_values = _non_empty_secret_values(incoming_secret_refs)
    existing_secret_refs = _existing_secret_refs(
        (existing or {}).get("secret_refs_jsonb") or (existing or {}).get("secret_refs")
    )
    _validate_connection_secrets(
        schema=secret_schema,
        secret_values=secret_values,
        existing_refs=existing_secret_refs,
    )
    return {
        "connection_id": connection_id,
        "tenant_id": tenant_id or DEFAULT_TENANT_ID,
        "account_id": account_id or DEFAULT_ACCOUNT_ID,
        "workspace_id": workspace_id,
        "display_name": display_name,
        "description": str(raw.get("description") or "").strip(),
        "connector_ref_jsonb": {
            "connector_id": connector["connector_id"],
            "extension_id": connector.get("extension_id", ""),
            "local_connector_id": local_connector_id,
            "install_id": connector.get("source_install_id", ""),
            "release_id": connector.get("source_release_id", ""),
            "runtime_adapter_ref": connector.get("runtime_adapter_ref", ""),
        },
        "source_kind": connector.get("connector_kind") or "extension",
        "environment": str(raw.get("environment") or "dev").strip() or "dev",
        "tags": _normalize_tags(raw.get("tags")),
        "access_mode": "read_only",
        "enabled": bool(raw.get("enabled", True)),
        "health_status": "unknown",
        "connection_config_jsonb": connection_config_jsonb,
        "secret_refs_jsonb": {"refs": existing_secret_refs} if existing_secret_refs else {},
        "metadata_jsonb": metadata,
        "_pending_secret_values": secret_values,
    }


def _set_project_datasets_via_dataset_updates(
    db, project_id: str, dataset_ids: list[str], *, author: str = "system"
) -> list[dict]:
    normalized_project_id = str(project_id or "").strip()
    project_scope = getattr(db, "_get_project_scope", lambda _project_id: None)(
        normalized_project_id
    )
    if not project_scope:
        raise HTTPException(status_code=404, detail=f"Unknown project '{normalized_project_id}'")
    project_workspace_id = str(project_scope.get("workspace_id") or "").strip()
    available = db.list_datasets(enabled_only=False)
    by_id = {
        str(row.get("dataset_id") or "").strip(): row
        for row in available
        if str(row.get("dataset_id") or "").strip()
    }
    missing = [dataset_id for dataset_id in dataset_ids if dataset_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown dataset(s): {', '.join(missing)}")
    incompatible = [
        dataset_id
        for dataset_id in dataset_ids
        if str(by_id[dataset_id].get("workspace_id") or "").strip() != project_workspace_id
    ]
    if incompatible:
        raise HTTPException(
            status_code=400,
            detail=f"Dataset(s) belong to a different workspace: {', '.join(incompatible)}",
        )
    wanted = set(dataset_ids)
    for dataset_id, row in by_id.items():
        project_ids = _normalize_project_ids(row.get("project_ids"))
        has_project = normalized_project_id in project_ids
        should_have_project = dataset_id in wanted
        if str(row.get("availability_mode") or "") == "all_workspace_projects":
            continue
        if has_project == should_have_project:
            continue
        next_project_ids = [item for item in project_ids if item != normalized_project_id]
        if should_have_project:
            next_project_ids.append(normalized_project_id)
        db.save_dataset(
            {
                **row,
                "dataset_id": dataset_id,
                "project_ids": sorted(set(next_project_ids)),
            },
            author=author,
        )
    return db.list_datasets(enabled_only=False, project_id=project_id)
