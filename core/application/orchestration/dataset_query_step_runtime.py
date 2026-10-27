# mypy: ignore-errors
"""Runtime support for persisted dataset_query workflow steps."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from uuid import UUID

from core.application.auth.secrets import decrypt_secret
from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.management.scope import normalize_scope
from core.application.orchestration.state_paths import resolve_state_path
from core.application.orchestration.step_templating import set_state_path
from core.application.registry.data_connection_binding import (
    connection_matches_scope,
    resolve_project_scoped_data_connection,
)
from core.infrastructure.extension_runtime.extension_runtime_helpers import (
    resolve_extension_runtime_record,
    resolve_extension_runtime_record_by_install_id,
)

logger = logging.getLogger(__name__)
_MISSING = object()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _literal_value(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text.strip("'\"")


def _state_value(state: dict[str, Any], selector: str) -> Any:
    path = _text(selector)
    if path != "state" and not path.startswith("state."):
        return _MISSING
    value, found = resolve_state_path(state, path)
    return value if found else _MISSING


def _matches_json_type(value: Any, expected: str) -> bool:
    normalized = str(expected or "").strip().lower()
    if not normalized:
        return True
    if normalized == "string":
        return isinstance(value, str)
    if normalized == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized == "boolean":
        return isinstance(value, bool)
    if normalized == "object":
        return isinstance(value, Mapping)
    if normalized == "array":
        return isinstance(value, list)
    if normalized == "null":
        return value is None
    return True


def _resolve_query_params(
    *,
    raw_params: dict[str, Any],
    state: dict[str, Any],
    template: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    schema = _as_dict(template.get("params_schema_jsonb") or template.get("param_schema_jsonb"))
    properties = _as_dict(schema.get("properties"))
    required = {
        str(name).strip() for name in (schema.get("required") or []) if str(name or "").strip()
    }
    resolved: dict[str, Any] = {}
    errors: list[str] = []
    for raw_name, raw_value in raw_params.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        value = raw_value
        if isinstance(raw_value, str):
            selector = raw_value.strip()
            if selector.startswith("literal:"):
                value = _literal_value(selector.removeprefix("literal:"))
            elif selector == "state" or selector.startswith("state."):
                value = _state_value(state, selector)
                if value is _MISSING:
                    errors.append(f"parameter '{name}' source '{selector}' is unavailable")
                    continue
        definition = _as_dict(properties.get(name))
        expected_type = str(definition.get("type") or "").strip()
        if expected_type and not _matches_json_type(value, expected_type):
            errors.append(f"parameter '{name}' must be {expected_type}")
            continue
        resolved[name] = value

    for name in sorted(required):
        if (name not in resolved or resolved[name] in (None, "")) and not any(
            f"parameter '{name}'" in error for error in errors
        ):
            errors.append(f"required parameter '{name}' is not mapped")
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(resolved) - set(properties))
        errors.extend(f"parameter '{name}' is not declared by the saved query" for name in unknown)
    return resolved, "; ".join(errors)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _resolve_secret_ref_value(db: Any, *, account_id: str, ref_key: str) -> str:
    if not account_id or not ref_key or not hasattr(db, "_cursor"):
        return ""
    try:
        with db._cursor() as cur:
            cur.execute(
                """
                SELECT encrypted_value
                  FROM secret_refs
                 WHERE account_id::text = %s
                   AND ref_key = %s
                 LIMIT 1
                """,
                (str(account_id), str(ref_key)),
            )
            row = cur.fetchone()
    except Exception:
        return ""
    encrypted = ""
    if isinstance(row, dict):
        encrypted = str(row.get("encrypted_value") or "").strip()
    elif isinstance(row, (tuple, list)) and row:
        encrypted = str(row[0] or "").strip()
    if not encrypted:
        return ""
    try:
        return str(decrypt_secret(encrypted) or "")
    except Exception:
        return ""


def _resolved_secret_refs(db: Any, connection: dict[str, Any]) -> dict[str, Any]:
    secret_refs = _as_dict(connection.get("secret_refs_jsonb") or connection.get("secret_refs"))
    refs = _as_dict(secret_refs.get("refs"))
    account_id = _text(connection.get("account_id"))
    if not refs:
        return secret_refs

    resolved_values = {
        key: value
        for key, raw_ref in refs.items()
        if (value := _resolve_secret_ref_value(db, account_id=account_id, ref_key=_text(raw_ref)))
    }
    if not resolved_values:
        return secret_refs

    payload = dict(secret_refs)
    existing_values = _as_dict(payload.get("values"))
    payload["values"] = {**existing_values, **resolved_values}
    return payload


def _set_path(update: dict[str, Any], state: dict[str, Any], target: str, value: Any) -> None:
    set_state_path(update, state, target or "dataset_query_result", value)


def _failure(code: str, message: str, *, detail: str = "") -> dict[str, Any]:
    error = {"code": code, "message": message}
    if detail:
        error["detail"] = detail
    return {
        "ok": False,
        "runtime": "dataset_query",
        "detail": message,
        "result": None,
        "errors": [error],
        "state_patch": {},
    }


def _registry_store() -> Any:
    from core.application.registry.registry_manager import RegistryManager

    registry = RegistryManager.instance()
    return getattr(registry, "db_store", registry)


def _select_first(db: Any, table: str, key_column: str, key_value: str) -> dict[str, Any] | None:
    table_name = str(table or "").strip()
    key = str(key_column or "").strip()
    allowed_tables = {
        "jobs",
        "projects",
        "dataset_query_templates",
        "registry_datasets",
        "workspace_data_connections",
    }
    allowed_keys = {"id", "dataset_id", "query_template_id", "connection_id"}
    if table_name not in allowed_tables or key not in allowed_keys or not key_value:
        return None
    try:
        with db._cursor() as cur:
            cur.execute(
                f"SELECT * FROM {table_name} WHERE {key} = %s LIMIT 1",
                (str(key_value),),
            )
            row = cur.fetchone()
    except Exception:
        logger.debug("dataset_query: fallback lookup failed", exc_info=True)
        return None
    return dict(row) if row else None


def _resolve_scope(state: dict[str, Any], db: Any) -> dict[str, str]:
    project_config = _as_dict(state.get("project_config"))
    scope = {
        "project_id": _text(state.get("project_id")) or _text(project_config.get("project_id")),
        "account_id": _text(state.get("account_id")) or _text(project_config.get("account_id")),
        "tenant_id": _text(state.get("tenant_id")) or _text(project_config.get("tenant_id")),
        "workspace_id": _text(state.get("workspace_id"))
        or _text(project_config.get("workspace_id")),
    }
    job_id = _text(state.get("job_id"))
    job = None
    if job_id:
        getter = getattr(db, "get_job", None)
        try:
            job = getter(job_id) if callable(getter) else None
        except Exception:
            logger.debug("dataset_query: get_job failed", exc_info=True)
        job = job or _select_first(db, "jobs", "id", job_id)
    if isinstance(job, dict):
        for key in ("project_id", "account_id", "tenant_id", "workspace_id"):
            scope[key] = scope[key] or _text(job.get(key))

    project_id = scope["project_id"]
    project = None
    if project_id:
        getter = getattr(db, "get_project", None)
        try:
            project = getter(project_id) if callable(getter) else None
        except Exception:
            logger.debug("dataset_query: get_project failed", exc_info=True)
        project = project or _select_first(db, "projects", "id", project_id)
    if isinstance(project, dict):
        for key in ("account_id", "tenant_id", "workspace_id"):
            scope[key] = scope[key] or _text(project.get(key))

    return scope


def _get_template(db: Any, query_template_id: str) -> dict[str, Any] | None:
    getter = getattr(db, "get_dataset_query_template", None)
    if callable(getter):
        try:
            return getter(query_template_id) or None
        except TypeError:
            try:
                return getter(query_template_id=query_template_id) or None
            except Exception:
                logger.debug("dataset_query: keyword get_template failed", exc_info=True)
        except Exception:
            logger.debug("dataset_query: get_template failed", exc_info=True)
    return _select_first(db, "dataset_query_templates", "query_template_id", query_template_id)


def _get_dataset(db: Any, dataset_id: str) -> dict[str, Any] | None:
    getter = getattr(db, "get_dataset", None)
    if callable(getter):
        try:
            return getter(dataset_id) or None
        except Exception:
            logger.debug("dataset_query: get_dataset failed", exc_info=True)
    return _select_first(db, "registry_datasets", "dataset_id", dataset_id)


def _get_connection(db: Any, connection_id: str) -> dict[str, Any] | None:
    getter = getattr(db, "get_workspace_data_connection", None)
    if callable(getter):
        try:
            return getter(connection_id) or None
        except TypeError:
            try:
                return getter(connection_id=connection_id) or None
            except Exception:
                logger.debug("dataset_query: keyword get_connection failed", exc_info=True)
        except Exception:
            logger.debug("dataset_query: get_connection failed", exc_info=True)
    return _select_first(db, "workspace_data_connections", "connection_id", connection_id)


def _workflow_source_install_id(db: Any, state: dict[str, Any]) -> str:
    workflow_id = _text(state.get("workflow_id"))
    if not workflow_id:
        return ""
    getter = getattr(db, "get_workflow", None)
    try:
        workflow = getter(workflow_id) if callable(getter) else None
    except Exception:
        logger.debug("dataset_query: workflow ownership lookup failed", exc_info=True)
        return ""
    return _text((workflow or {}).get("source_install_id")) if isinstance(workflow, dict) else ""


def _get_projection(
    db: Any,
    *,
    account_id: str,
    install_id: str,
    artifact_kind: str,
    artifact_id: str,
) -> dict[str, Any] | None:
    getter = getattr(db, "get_extension_registry_projection", None)
    if callable(getter):
        try:
            row = getter(
                account_id=account_id,
                install_id=install_id,
                artifact_kind=artifact_kind,
                artifact_id=artifact_id,
            )
            return dict(row) if isinstance(row, dict) else None
        except Exception:
            # The step then reports the projection unavailable; say why here.
            logger.warning(
                "dataset_query: extension projection lookup failed install_id=%s "
                "artifact_kind=%s artifact_id=%s",
                install_id,
                artifact_kind,
                artifact_id,
                exc_info=True,
            )
    return None


def _projection_is_available(
    row: dict[str, Any] | None,
    *,
    active_extension_ids: set[str] | None = None,
    active_extension_versions: dict[str, set[str]] | None = None,
) -> bool:
    if not row or row.get("is_active") is False:
        return False
    snapshot = _as_dict(row.get("projection"))
    from core.application.extensions.projection_dependency_availability import (
        resolve_projection_dependency_availability,
    )

    dependency = resolve_projection_dependency_availability(
        snapshot,
        active_extension_ids=active_extension_ids,
        active_extension_versions=active_extension_versions,
    )
    if bool(row.get("new_runs_blocked")) or bool(dependency.get("new_runs_blocked")):
        return False
    availability_status = row.get("availability_status") or dependency.get("availability_status")
    return _text(availability_status).lower() == "available"


def _dataset_connection_id(dataset: dict[str, Any]) -> str:
    connection_id = _text(dataset.get("connection_id"))
    if connection_id:
        return connection_id
    metadata = _as_dict(dataset.get("metadata_jsonb") or dataset.get("metadata"))
    return _text(metadata.get("connection_id"))


def _dataset_resource_scope(dataset: dict[str, Any]) -> dict[str, Any]:
    scope = _as_dict(dataset.get("resource_scope_jsonb") or dataset.get("resource_scope"))
    if scope:
        return scope
    metadata = _as_dict(dataset.get("metadata_jsonb") or dataset.get("metadata"))
    return _as_dict(metadata.get("resource_scope_jsonb") or metadata.get("resource_scope"))


def _connector_action_input(
    *,
    db: Any,
    query_template_id: str,
    template: dict[str, Any],
    dataset: dict[str, Any],
    connection: dict[str, Any],
    params: dict[str, Any],
    row_limit: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Build the canonical extension action input expected by dataset connectors."""
    dataset_payload = {
        "dataset_id": dataset.get("dataset_id"),
        "display_name": dataset.get("display_name"),
        "resource_scope_jsonb": _dataset_resource_scope(dataset),
        "schema_snapshot_jsonb": _as_dict(dataset.get("schema_snapshot_jsonb")),
    }
    template_payload = {
        "query_template_id": query_template_id,
        "dataset_id": template.get("dataset_id"),
        "display_name": template.get("display_name"),
        "query_language": template.get("query_language") or "sql",
        "query_definition_jsonb": _as_dict(template.get("query_definition_jsonb")),
        "param_schema_jsonb": _as_dict(
            template.get("params_schema_jsonb") or template.get("param_schema_jsonb")
        ),
        "output_schema_jsonb": _as_dict(template.get("output_schema_jsonb")),
    }
    return _jsonable(
        {
            "connection_config": _as_dict(
                connection.get("connection_config_jsonb") or connection.get("connection_config")
            ),
            "secret_refs": _resolved_secret_refs(db, connection),
            "query_template": template_payload,
            "dataset": dataset_payload,
            "params": params,
            "limits": {
                "max_preview_rows": row_limit,
                "max_runtime_rows": row_limit,
                "timeout_seconds": timeout_seconds,
            },
        }
    )


def _extension_for_connection(
    db: Any, connection: dict[str, Any]
) -> tuple[dict[str, Any] | None, Any]:
    ref = _as_dict(connection.get("connector_ref_jsonb") or connection.get("connector_ref"))
    account_id = _text(connection.get("account_id"))
    dsn = _text(getattr(db, "dsn", ""))
    get_logger_only = None
    if dsn:
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        def get_logger_only():
            return PlatformIdentityStore(dsn)

    install_id = _text(ref.get("install_id"))
    if install_id:
        extension, logger_obj = resolve_extension_runtime_record_by_install_id(
            install_id,
            get_logger_only=get_logger_only,
            account_id=account_id or None,
        )
        if extension:
            return extension, logger_obj
        return None, logger_obj

    candidates = [
        _text(ref.get("extension_key")),
        _text(ref.get("extension_id")),
        _text(ref.get("source_extension_id")),
    ]
    for candidate in [item for item in candidates if item]:
        extension, logger_obj = resolve_extension_runtime_record(
            candidate,
            get_logger_only=get_logger_only,
            account_id=account_id or None,
        )
        if extension:
            return extension, logger_obj
    return None, None


def _extension_runtime_id(extension: dict[str, Any] | None) -> str:
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    return (
        _text(payload.get("extension_key"))
        or _text(payload.get("extension_id"))
        or _text(compiled.get("extension_id"))
    )


def _response_envelope(invocation: dict[str, Any]) -> dict[str, Any]:
    response = invocation.get("response")
    if isinstance(response, dict):
        return dict(response)
    if isinstance(invocation.get("result"), dict):
        return dict(invocation["result"])
    return {
        "ok": invocation.get("status") == "succeeded",
        "result": {},
        "errors": [],
    }


def _invocation_errors(invocation: dict[str, Any], envelope: dict[str, Any]) -> list[Any]:
    errors: list[Any] = []
    envelope_errors = envelope.get("errors")
    if isinstance(envelope_errors, list):
        errors.extend(envelope_errors)
    invocation_errors = invocation.get("errors")
    if isinstance(invocation_errors, list):
        errors.extend(invocation_errors)
    if not errors:
        detail = _text(invocation.get("detail"))
        if detail:
            errors.append({"code": detail, "message": detail})
    return errors


def _process_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any],
    db: Any,
    scope: dict[str, str],
    connection: dict[str, Any],
    state: dict[str, Any],
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
            account_id=scope.get("account_id") or "",
            project_id=scope.get("project_id") or "",
            connection_id=_text(connection.get("connection_id")) or None,
            actor_id=_text(state.get("actor_id")) or None,
        )
    except Exception as exc:
        logger.warning("dataset_query: failed to process project-state updates: %s", exc)
        invocation["status"] = "failed"
        invocation["detail"] = "project_state_update_failed"
        invocation["errors"] = [
            *list(invocation.get("errors") or []),
            {"code": "project_state_update_failed", "message": str(exc)},
        ]


def _requested_row_limit(config: dict, step: dict):
    """Take the author's row_limit as written; `or` would swallow a deliberate 0."""
    if "row_limit" in config:
        return config.get("row_limit")
    return step.get("row_limit")


def execute_dataset_query_step(*, step: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    override = _as_dict(step.get("override"))
    config = _as_dict(override.get("dataset_query") or step.get("dataset_query"))
    from core.application.orchestration.workflow_state_selector_contract import (
        RUNTIME_ERROR_CODE,
        validate_step_state_selectors,
    )

    selector_issues = validate_step_state_selectors(
        {"step_kind": "dataset_query", "override": {"dataset_query": config}}
    )
    if selector_issues:
        issue = selector_issues[0]
        return {
            "ok": False,
            "runtime": "dataset_query",
            "detail": RUNTIME_ERROR_CODE,
            "result": None,
            "errors": [
                {
                    "code": RUNTIME_ERROR_CODE,
                    "message": "dataset_query has an invalid input selector",
                    "selector": issue["selector"],
                    "path": issue["path"].removeprefix("step."),
                }
            ],
            "state_patch": {},
        }
    if config.get("sql") or step.get("sql"):
        return _failure(
            "raw_query_not_allowed",
            "dataset_query workflow steps must reference a saved query template",
        )

    query_template_id = _text(config.get("query_template_id") or step.get("query_template_id"))
    if not query_template_id:
        return _failure(
            "query_template_missing",
            "dataset_query requires override.dataset_query.query_template_id",
        )

    # Approve the requested row limit before anything is fetched: the whole result
    # set lands in workflow state, so an out-of-range request is refused rather
    # than quietly truncated.
    from core.application.registry.dataset_query_limits import (
        InvalidDatasetQueryLimitError,
        resolve_dataset_query_row_limit,
    )

    try:
        row_limit = resolve_dataset_query_row_limit(_requested_row_limit(config, step))
    except InvalidDatasetQueryLimitError as exc:
        return _failure("row_limit_exceeds_maximum", str(exc))

    db = _registry_store()
    scope = _resolve_scope(state, db)
    if not all(scope.values()):
        return _failure(
            "scope_missing",
            "dataset_query requires canonical project/account/tenant/workspace scope",
        )

    source_install_id = _workflow_source_install_id(db, state)
    active_extension_ids: set[str] | None = None
    active_extension_versions: dict[str, set[str]] | None = None
    if source_install_id:
        from core.application.extensions.projection_dependency_availability import (
            active_extension_ids_for_account,
            active_extension_versions_for_account,
        )

        active_extension_ids = active_extension_ids_for_account(
            db,
            account_id=scope["account_id"],
        )
        active_extension_versions = active_extension_versions_for_account(
            db,
            account_id=scope["account_id"],
        )
    saved_query_projection = None
    resolved_query_template_id = query_template_id
    if source_install_id:
        saved_query_projection = _get_projection(
            db,
            account_id=scope["account_id"],
            install_id=source_install_id,
            artifact_kind="saved_query",
            artifact_id=query_template_id,
        )
        if not _projection_is_available(
            saved_query_projection,
            active_extension_ids=active_extension_ids,
            active_extension_versions=active_extension_versions,
        ):
            return _failure(
                "projection_unavailable",
                "Saved query projection is unavailable for new runs",
            )
        resolved_query_template_id = projection_bridge_id(source_install_id, query_template_id)

    template = _get_template(db, resolved_query_template_id)
    if not source_install_id:
        from core.application.registry.projected_artifact_rows import projection_aware_catalog_row

        template = projection_aware_catalog_row(
            db,
            "saved_query",
            "query_template_id",
            resolved_query_template_id,
            template,
            account_id=scope["account_id"],
        )
    if not template and saved_query_projection:
        snapshot = _as_dict(saved_query_projection.get("projection"))
        template = {**snapshot, "query_template_id": query_template_id}
    if not template:
        return _failure("query_template_not_found", "Saved query template was not found")

    local_dataset_id = _text(template.get("dataset_id"))
    dataset_projection = None
    resolved_dataset_id = local_dataset_id
    if source_install_id:
        query_snapshot = _as_dict((saved_query_projection or {}).get("projection"))
        local_dataset_id = _text(query_snapshot.get("dataset_id")) or local_dataset_id
        dataset_projection = _get_projection(
            db,
            account_id=scope["account_id"],
            install_id=source_install_id,
            artifact_kind="dataset",
            artifact_id=local_dataset_id,
        )
        if not _projection_is_available(
            dataset_projection,
            active_extension_ids=active_extension_ids,
        ):
            return _failure(
                "projection_unavailable",
                "Dataset projection is unavailable for new runs",
            )
        resolved_dataset_id = projection_bridge_id(source_install_id, local_dataset_id)

    dataset = _get_dataset(db, resolved_dataset_id)
    if not source_install_id:
        dataset = projection_aware_catalog_row(
            db,
            "dataset",
            "dataset_id",
            resolved_dataset_id,
            dataset,
            account_id=scope["account_id"],
        )
    if not dataset:
        return _failure("dataset_not_found", "Dataset for saved query was not found")
    project_ids = {str(item) for item in dataset.get("project_ids") or [] if item}
    if not project_ids or scope["project_id"] not in project_ids:
        return _failure("dataset_not_assigned", "Dataset is not assigned to this project")

    if template.get("enabled") is False:
        return _failure("query_template_disabled", "Saved query template is disabled")
    from core.application.registry.saved_query_contract import (
        SavedQueryContractError,
        validate_saved_query_contract,
    )

    try:
        validate_saved_query_contract(template, dataset=dataset)
    except SavedQueryContractError as exc:
        return _failure(exc.code, exc.message, detail=exc.path)

    raw_params = _as_dict(config.get("params") or config.get("input_params")) or _as_dict(
        step.get("params")
    )
    params, params_error = _resolve_query_params(
        raw_params=raw_params,
        state=state,
        template=template,
    )
    if params_error:
        return _failure("query_parameter_mapping_invalid", params_error)

    connection_id = _dataset_connection_id(dataset)
    connection = _get_connection(db, connection_id) if connection_id else None
    connection_error = ""
    if connection is None:
        connection, connection_error = resolve_project_scoped_data_connection(db, dataset, scope)
    if not connection:
        return _failure(
            connection_error or "connection_not_found",
            "Dataset data connection was not found or was ambiguous",
        )
    if not connection_matches_scope(connection, scope):
        return _failure(
            "connection_scope_mismatch",
            "Dataset data connection is outside the workflow project scope",
        )
    extension, _logger_obj = _extension_for_connection(db, connection)
    if not extension:
        return _failure(
            "connector_runtime_unavailable",
            "Dataset connector extension runtime is unavailable",
        )
    if extension.get("enabled") is False:
        return _failure(
            "extension_disabled",
            "This extension is disabled—ask an admin to enable it.",
        )

    timeout_seconds = int(config.get("timeout_seconds") or step.get("timeout_seconds") or 30)
    context = _jsonable(
        {
            "project_id": scope["project_id"],
            "account_id": scope["account_id"],
            "tenant_id": scope["tenant_id"],
            "workspace_id": scope["workspace_id"],
            "workflow_id": state.get("workflow_id"),
            "job_id": state.get("job_id"),
        }
    )
    payload = _connector_action_input(
        db=db,
        query_template_id=query_template_id,
        template=template,
        dataset=dataset,
        connection=connection,
        params=params,
        row_limit=row_limit,
        timeout_seconds=timeout_seconds,
    )

    try:
        from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

        invocation = run_extension_action(
            extension,
            action_id="execute_template",
            context=context,
            input_payload=payload,
            runtime_context={"source": "dataset_query_step", **context},
            scope=normalize_scope(
                account_id=scope["account_id"],
                project_id=scope["project_id"],
                workflow_id=_text(state.get("workflow_id")) or None,
            ),
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        logger.exception("dataset_query: extension runtime invocation failed")
        return _failure(
            "connector_runtime_failed",
            "Dataset connector execution failed",
            detail=str(exc),
        )

    if isinstance(invocation, dict):
        _process_project_state_updates(
            invocation,
            extension=extension,
            db=db,
            scope=scope,
            connection=connection,
            state=state,
        )
    envelope = _response_envelope(dict(invocation or {}))
    invocation_ok = _text((invocation or {}).get("status")) == "succeeded"
    ok = invocation_ok and bool(envelope.get("ok"))
    result = copy.deepcopy(envelope.get("result") or {})
    state_patch: dict[str, Any] = {}
    if ok:
        target = _text(config.get("result_target") or step.get("result_target"))
        _set_path(
            state_patch,
            dict(state or {}),
            target or "dataset_query_result",
            _jsonable(result),
        )
    errors = []
    if not ok:
        errors = _invocation_errors(dict(invocation or {}), envelope)
    elif isinstance(envelope.get("errors"), list):
        errors = envelope["errors"]
    return {
        "ok": ok,
        "runtime": "dataset_query",
        "detail": "dataset_query step executed" if ok else "dataset_query step failed",
        "result": _jsonable(result),
        "errors": _jsonable(errors),
        "invocation": {
            "status": _text((invocation or {}).get("status")),
            "detail": _text((invocation or {}).get("detail")),
        },
        "state_patch": state_patch,
    }
