# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_invocation import _declared_operation_row
from .extension_runtime_validation import _as_dict, _as_list


def _connection_partition_errors(
    extension: dict,
    *,
    connection_type: str,
    partition: str,
    value: Any,
    runtime_context: dict | None = None,
) -> list[str]:
    contract = _as_dict(runtime_context).get("platform_connection_contract")
    if not callable(contract):
        return ["Platform connection contract is not configured."]
    errors = contract(
        "partition_errors",
        extension=extension,
        connection_type=connection_type,
        partition=partition,
        value=value,
    )
    if not isinstance(errors, list):
        return ["Platform connection contract returned an invalid validation result."]
    return [str(error) for error in errors]


def _connection_partition_schema(
    extension: dict,
    *,
    connection_type: str,
    partition: str,
    runtime_context: dict | None = None,
) -> dict[str, Any]:
    contract = _as_dict(runtime_context).get("platform_connection_contract")
    if not callable(contract):
        raise ValueError("Platform connection contract is not configured.")
    schema = contract(
        "partition_schema",
        extension=extension,
        connection_type=connection_type,
        partition=partition,
    )
    if not isinstance(schema, dict):
        raise ValueError("Platform connection contract returned an invalid schema.")
    return schema


def _invalid_connection_payload(
    extension: dict,
    errors: list[str],
    *,
    timeout_seconds: float | None,
) -> dict:
    bounded = [str(message or "").strip()[:512] for message in errors[:8] if str(message).strip()]
    return _platform_invocation_result(
        extension=extension,
        status="failed",
        detail="invalid_payload",
        response={
            "ok": False,
            "error": "invalid_payload",
            "errors": [{"code": "invalid_payload", "message": message} for message in bounded],
        },
        timeout_seconds=timeout_seconds,
    )


def _runtime_logger(runtime_context: dict | None = None):
    context = _as_dict(runtime_context)
    return context.get("logger")


def _string_or(value: Any, fallback: str = "") -> str:
    return str(value or fallback).strip()


def _coerce_entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _deep_merge_dict(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge_dict(target[key], value)
        else:
            target[key] = value
    return target


def _expand_dotted_object(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in payload.items():
        key = str(raw_key or "").strip()
        if not key or key == "_meta":
            continue
        if isinstance(raw_value, dict):
            value = _expand_dotted_object(raw_value)
        elif isinstance(raw_value, list):
            value = [
                _expand_dotted_object(item) if isinstance(item, dict) else item
                for item in raw_value
            ]
        else:
            value = raw_value
        if "." not in key:
            existing = normalized.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                normalized[key] = _deep_merge_dict(existing, value)
            else:
                normalized[key] = value
            continue
        cursor = normalized
        parts = [segment for segment in key.split(".") if segment]
        for part in parts[:-1]:
            next_value = cursor.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                cursor[part] = next_value
            cursor = next_value
        leaf = parts[-1]
        existing = cursor.get(leaf)
        if isinstance(existing, dict) and isinstance(value, dict):
            cursor[leaf] = _deep_merge_dict(existing, value)
        else:
            cursor[leaf] = value
    return normalized


def _nested_set(payload: dict[str, Any], path: str, value: Any) -> None:
    parts = [segment for segment in str(path or "").split(".") if segment]
    if not parts:
        return
    cursor = payload
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def _nested_get(payload: dict[str, Any], path: str, default: Any = None) -> Any:
    parts = [segment for segment in str(path or "").split(".") if segment]
    if not parts:
        return default
    cursor: Any = payload
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor.get(part)
    return cursor


def _project_connection_row_for_extension(
    bl: Any, *, project_id: str, extension_id: str, extension_install_id: str = ""
) -> dict[str, Any]:
    if bl is None:
        return {}
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(
                project_id=str(project_id),
                extension_id=str(extension_id),
                extension_install_id=str(extension_install_id),
                enabled_only=False,
            )
        except Exception:
            row = None
        if isinstance(row, dict):
            return dict(row)
    lister = getattr(bl, "list_project_connections", None)
    if callable(lister):
        try:
            rows = lister(str(project_id), enabled_only=False) or []
        except Exception:
            rows = []
        for row in rows:
            payload = _as_dict(row)
            if _string_or(payload.get("extension_id")).lower() != str(extension_id).lower():
                continue
            if extension_install_id and _string_or(payload.get("extension_install_id")) != str(
                extension_install_id
            ):
                continue
            if payload:
                return dict(payload)
    return {}


def _save_project_connection_config(
    bl: Any,
    *,
    project_id: str,
    extension_id: str,
    connection_config: dict[str, Any],
    extension_install_id: str = "",
    connection_type: str = "",
    account_id: str = "",
    runtime_context: dict | None = None,
) -> bool:
    operation_runner = _as_dict(runtime_context).get("platform_connection_operation")
    if bl is None or not callable(operation_runner):
        return False
    result = operation_runner(
        "config_save",
        store=bl,
        project_id=str(project_id),
        extension_id=str(extension_id),
        extension_install_id=str(extension_install_id),
        account_id=str(account_id),
        connection_type=str(connection_type),
        connection_config=dict(connection_config),
    )
    return bool(_as_dict(result).get("ok"))


def _coerce_positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = 0
    return normalized if normalized > 0 else None


def _coerce_grid_pagination(params: dict | None = None) -> tuple[int, int]:
    payload = _as_dict(params)
    page = _coerce_positive_int(payload.get("page")) or 1
    page_size = _coerce_positive_int(payload.get("page_size")) or 25
    page_size = max(1, min(page_size, 500))
    return page, page_size


def _safe_iso_datetime(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return str(value.isoformat())
        except Exception:
            return str(value)
    return str(value)


def _as_text_set(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item or "").strip().lower() for item in value if str(item or "").strip()}
    if isinstance(value, tuple):
        return {str(item or "").strip().lower() for item in value if str(item or "").strip()}
    if value in (None, "", {}):
        return set()
    return {str(value).strip().lower()}


def _matches_grid_filter(item: dict, filter_key: str, expected: Any) -> bool:
    expected_set = _as_text_set(expected)
    if not expected_set:
        return True
    actual = str(item.get(filter_key) or "").strip().lower()
    return actual in expected_set


def _apply_grid_params(
    items: list[dict], params: dict | None = None
) -> tuple[list[dict], int, int, int]:
    payload = _as_dict(params)
    page, page_size = _coerce_grid_pagination(payload)
    search = str(payload.get("search") or "").strip().lower()
    filters = _as_dict(payload.get("filters"))
    sort = _as_list(payload.get("sort"))

    filtered: list[dict] = []
    for row in items:
        item = _as_dict(row)
        if not item:
            continue
        if search:
            haystack = " ".join(
                str(item.get(key) or "")
                for key in (
                    "delivery_id",
                    "route_key",
                    "event_type",
                    "severity",
                    "status",
                    "provider_message_id",
                    "error_text",
                    "workflow_id",
                    "summary",
                )
            ).lower()
            if search not in haystack:
                continue
        include = True
        for filter_key, expected in filters.items():
            if not _matches_grid_filter(item, str(filter_key or "").strip(), expected):
                include = False
                break
        if include:
            filtered.append(item)

    if sort:
        sort_rule = _as_dict(sort[0])
        field = str(sort_rule.get("field") or "").strip()
        direction = str(sort_rule.get("direction") or "asc").strip().lower()
        reverse = direction == "desc"
        if field:
            filtered.sort(
                key=lambda row: str(_as_dict(row).get(field) or "").lower(),
                reverse=reverse,
            )

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    return filtered[start:end], page, page_size, total


def _platform_invocation_result(
    *,
    extension: dict,
    status: str,
    detail: str = "",
    response: dict | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    timeout = 30
    if timeout_seconds is not None:
        try:
            timeout = max(1, int(timeout_seconds))
        except (TypeError, ValueError):
            timeout = 30
    normalized_status = (
        "succeeded" if str(status or "").strip().lower() == "succeeded" else "failed"
    )
    return {
        "status": normalized_status,
        "detail": str(detail or "").strip() if normalized_status == "failed" else "",
        "command": [],
        "bundle_root": str(extension.get("bundle_root") or "").strip(),
        "timeout_seconds": timeout,
        "exit_code": 0 if normalized_status == "succeeded" else 2,
        "response": _as_dict(response),
        "stdout": "",
        "stderr": "",
        "handler_mode": "platform",
    }


def _resolve_platform_project_id(
    *,
    runtime_context: dict | None,
    context: dict | None = None,
    params: dict | None = None,
    target: dict | None = None,
    input_payload: dict | None = None,
) -> str | None:
    context_payload = _as_dict(context)
    params_payload = _as_dict(params)
    target_payload = _as_dict(target)
    input_data = _as_dict(input_payload)
    candidate = (
        _coerce_entity_id(context_payload.get("project_id"))
        or _coerce_entity_id(params_payload.get("project_id"))
        or _coerce_entity_id(target_payload.get("project_id"))
        or _coerce_entity_id(input_data.get("project_id"))
    )
    if candidate:
        return candidate
    logger = _runtime_logger(runtime_context)
    if logger and hasattr(logger, "list_projects"):
        try:
            rows = logger.list_projects() or []
        except Exception:
            rows = []
        if len(rows) == 1:
            return _coerce_entity_id(_as_dict(rows[0]).get("id"))
    return None


def _run_platform_query(
    extension: dict,
    *,
    query_id: str,
    page_id: str,
    component_id: str,
    context: dict | None = None,
    params: dict | None = None,
    timeout_seconds: int | None = None,
    runtime_context: dict | None = None,
) -> dict:
    _ = (query_id, page_id, component_id, params, runtime_context)
    return _platform_invocation_result(
        extension=extension,
        status="failed",
        detail="Unsupported platform query handler",
        timeout_seconds=timeout_seconds,
    )


def _run_platform_action(
    extension: dict,
    *,
    action_id: str,
    page_id: str,
    component_id: str,
    context: dict | None = None,
    target: dict | None = None,
    input_payload: dict | None = None,
    timeout_seconds: float | None = None,
    runtime_context: dict | None = None,
) -> dict:
    _ = page_id
    row = _declared_operation_row(
        extension,
        operation_type="action",
        operation_id=action_id,
    )
    handler = _as_dict(_as_dict(row).get("handler"))
    operation = _string_or(handler.get("operation"))
    config = _as_dict(handler.get("config"))
    bl = _runtime_logger(runtime_context)
    project_id = _coerce_entity_id(_as_dict(context).get("project_id"))
    extension_id = _string_or(
        extension.get("extension_key") or extension.get("extension_id"),
        "extension",
    )

    if operation == "project_connection_config_patch":
        if not project_id:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_context_required",
                response={"ok": False, "error": "project_context_required"},
                timeout_seconds=timeout_seconds,
            )
        if bl is None:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        connection = _project_connection_row_for_extension(
            bl,
            project_id=project_id,
            extension_id=extension_id,
            extension_install_id=_string_or(_as_dict(runtime_context).get("extension_install_id")),
        )
        merged_config = _as_dict(connection.get("connection_config"))
        fields = [
            str(item or "").strip()
            for item in _as_list(config.get("fields"))
            if str(item or "").strip()
        ]
        source = _as_dict(input_payload)
        for field in fields or source.keys():
            if field not in source:
                continue
            _nested_set(merged_config, field, source.get(field))
        connection_type = _string_or(
            config.get("connection_type_id") or connection.get("connection_type")
        )
        schema_errors = _connection_partition_errors(
            extension,
            connection_type=connection_type,
            partition="config",
            value=merged_config,
            runtime_context=runtime_context,
        )
        if schema_errors:
            return _invalid_connection_payload(
                extension, schema_errors, timeout_seconds=timeout_seconds
            )
        if not _save_project_connection_config(
            bl,
            project_id=project_id,
            extension_id=extension_id,
            connection_config=merged_config,
            extension_install_id=_string_or(_as_dict(runtime_context).get("extension_install_id")),
            connection_type=connection_type,
            account_id=_string_or(_as_dict(runtime_context).get("account_id")),
            runtime_context=runtime_context,
        ):
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        values = {}
        for field in fields or source.keys():
            values[field] = _nested_get(merged_config, field)
        return _platform_invocation_result(
            extension=extension,
            status="succeeded",
            response={
                "ok": True,
                "message": "Configuration saved",
                "reload": [component_id or "config_form", "health_panel"],
                "values": values,
            },
            timeout_seconds=timeout_seconds,
        )

    if operation == "project_connection_ingress_patch":
        if not project_id:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_context_required",
                response={"ok": False, "error": "project_context_required"},
                timeout_seconds=timeout_seconds,
            )
        if bl is None or not all(
            callable(getattr(bl, name, None))
            for name in ("get_project_connection", "upsert_project_connection")
        ):
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        operation_runner = _as_dict(runtime_context).get("platform_connection_operation")
        if not callable(operation_runner):
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        try:
            ingress_schema = _connection_partition_schema(
                extension,
                connection_type=_string_or(config.get("connection_type_id")),
                partition="ingress",
                runtime_context=runtime_context,
            )
        except ValueError as exc:
            return _invalid_connection_payload(
                extension, [str(exc)], timeout_seconds=timeout_seconds
            )
        operation_result = _as_dict(
            operation_runner(
                "ingress_patch",
                store=bl,
                project_id=str(project_id),
                extension_id=extension_id,
                config=config,
                input_payload=_as_dict(input_payload),
                extension_install_id=_string_or(
                    _as_dict(runtime_context).get("extension_install_id")
                ),
                connection_type=_string_or(config.get("connection_type_id")),
                account_id=_string_or(_as_dict(runtime_context).get("account_id")),
                ingress_schema=ingress_schema,
            )
        )
        if not operation_result.get("ok"):
            error = _string_or(
                operation_result.get("error"), "project_connection_storage_unavailable"
            )
            if error == "invalid_payload":
                return _invalid_connection_payload(
                    extension,
                    [str(item) for item in _as_list(operation_result.get("errors"))],
                    timeout_seconds=timeout_seconds,
                )
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail=error,
                response={"ok": False, "error": error},
                timeout_seconds=timeout_seconds,
            )
        outcome = _as_dict(operation_result.get("outcome"))
        return _platform_invocation_result(
            extension=extension,
            status="succeeded",
            response={
                "ok": True,
                "message": "Inbound settings saved",
                "values": outcome["values"],
                "connection_id": outcome["connection_id"],
                "reload": [component_id or "ingress_form", "health_panel"],
            },
            timeout_seconds=timeout_seconds,
        )

    if operation == "project_connection_secrets_save":
        if not project_id:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_context_required",
                response={"ok": False, "error": "project_context_required"},
                timeout_seconds=timeout_seconds,
            )
        if bl is None or not all(
            callable(getattr(bl, name, None))
            for name in (
                "provider_configuration_transaction",
                "get_project_connection",
                "save_provider_secret",
                "upsert_project_connection",
            )
        ):
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        operation_runner = _as_dict(runtime_context).get("platform_connection_operation")
        if not callable(operation_runner):
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        contract = _as_dict(runtime_context).get("platform_connection_contract")
        mutation_grant = (
            contract(
                "trusted_mutation_grant",
                extension=extension,
                operation_id="project_connection_secrets_save",
            )
            if callable(contract)
            else None
        )
        if not isinstance(mutation_grant, dict):
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="host_mutation_not_granted",
                response={"ok": False, "error": "host_mutation_not_granted"},
                timeout_seconds=timeout_seconds,
            )
        try:
            secret_schema = _connection_partition_schema(
                extension,
                connection_type=_string_or(config.get("connection_type_id")),
                partition="secret",
                runtime_context=runtime_context,
            )
        except ValueError:
            return _invalid_connection_payload(
                extension,
                ["Secret payload failed the declared connection schema."],
                timeout_seconds=timeout_seconds,
            )
        trusted_grant = {
            **mutation_grant,
            "project_id": str(project_id),
            "extension_id": extension_id,
            "extension_install_id": _string_or(
                _as_dict(runtime_context).get("extension_install_id")
            ),
        }
        operation_result = _as_dict(
            operation_runner(
                "secrets_save",
                store=bl,
                project_id=str(project_id),
                extension_id=extension_id,
                extension_install_id=_string_or(
                    _as_dict(runtime_context).get("extension_install_id")
                ),
                account_id=_string_or(_as_dict(runtime_context).get("account_id")),
                trusted_grant=trusted_grant,
                config=config,
                input_payload=_as_dict(input_payload),
                secret_schema=secret_schema,
            )
        )
        if not operation_result.get("ok"):
            error = _string_or(
                operation_result.get("error"), "project_connection_storage_unavailable"
            )
            if error == "invalid_payload":
                return _invalid_connection_payload(
                    extension,
                    ["Secret payload failed the declared connection schema."],
                    timeout_seconds=timeout_seconds,
                )
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail=error,
                response={"ok": False, "error": error},
                timeout_seconds=timeout_seconds,
            )
        outcome = _as_dict(operation_result.get("outcome"))
        return _platform_invocation_result(
            extension=extension,
            status="succeeded",
            response={
                "ok": True,
                "message": "Secrets saved",
                "saved_keys": outcome["saved_keys"],
                "connection_id": outcome["connection_id"],
                "reload": [component_id or "secrets_form", "health_panel"],
            },
            timeout_seconds=timeout_seconds,
        )

    if operation in {
        "project_connection_list_replace",
        "project_connection_list_toggle",
        "project_connection_list_delete",
    }:
        if not project_id:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_context_required",
                response={"ok": False, "error": "project_context_required"},
                timeout_seconds=timeout_seconds,
            )
        if bl is None:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        list_key = _string_or(config.get("list_key"))
        item_key = _string_or(config.get("item_key"))
        if not list_key or not item_key:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="invalid_platform_handler_config",
                timeout_seconds=timeout_seconds,
            )
        duplicate_error = _string_or(config.get("duplicate_error"), "duplicate_item_key")
        immutable_error = _string_or(config.get("immutable_error"), "immutable_item_key")
        not_found_error = _string_or(config.get("not_found_error"), "item_not_found")
        connection = _project_connection_row_for_extension(
            bl,
            project_id=project_id,
            extension_id=extension_id,
            extension_install_id=_string_or(_as_dict(runtime_context).get("extension_install_id")),
        )
        merged_config = _as_dict(connection.get("connection_config"))
        current_items = [_as_dict(item) for item in _as_list(merged_config.get(list_key))]

        if operation == "project_connection_list_replace":
            requested_items = _as_list(_as_dict(input_payload).get(list_key))
            if not requested_items:
                return _platform_invocation_result(
                    extension=extension,
                    status="failed",
                    detail="invalid_payload",
                    response={"ok": False, "error": "invalid_payload"},
                    timeout_seconds=timeout_seconds,
                )
            normalized_items: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for raw_item in requested_items:
                source_item = _as_dict(raw_item)
                item_id = _string_or(source_item.get(item_key))
                if not item_id:
                    return _platform_invocation_result(
                        extension=extension,
                        status="failed",
                        detail="invalid_payload",
                        response={"ok": False, "error": "invalid_payload"},
                        timeout_seconds=timeout_seconds,
                    )
                if item_id in seen_ids:
                    return _platform_invocation_result(
                        extension=extension,
                        status="failed",
                        detail=duplicate_error,
                        response={"ok": False, "error": duplicate_error},
                        timeout_seconds=timeout_seconds,
                    )
                original_item_id = _string_or(
                    _as_dict(source_item.get("_meta")).get(f"original_{item_key}")
                )
                if original_item_id and original_item_id != item_id:
                    return _platform_invocation_result(
                        extension=extension,
                        status="failed",
                        detail=immutable_error,
                        response={"ok": False, "error": immutable_error},
                        timeout_seconds=timeout_seconds,
                    )
                seen_ids.add(item_id)
                normalized_items.append(_expand_dotted_object(source_item))
            merged_config[list_key] = normalized_items
            connection_type = _string_or(
                config.get("connection_type_id") or connection.get("connection_type")
            )
            schema_errors = _connection_partition_errors(
                extension,
                connection_type=connection_type,
                partition="config",
                value=merged_config,
                runtime_context=runtime_context,
            )
            if schema_errors:
                return _invalid_connection_payload(
                    extension, schema_errors, timeout_seconds=timeout_seconds
                )
            if not _save_project_connection_config(
                bl,
                project_id=project_id,
                extension_id=extension_id,
                connection_config=merged_config,
                extension_install_id=_string_or(
                    _as_dict(runtime_context).get("extension_install_id")
                ),
                connection_type=connection_type,
                account_id=_string_or(_as_dict(runtime_context).get("account_id")),
                runtime_context=runtime_context,
            ):
                return _platform_invocation_result(
                    extension=extension,
                    status="failed",
                    detail="project_connection_storage_unavailable",
                    response={"ok": False, "error": "project_connection_storage_unavailable"},
                    timeout_seconds=timeout_seconds,
                )
            return _platform_invocation_result(
                extension=extension,
                status="succeeded",
                response={
                    "ok": True,
                    "message": "Channels saved",
                    "reload": [component_id or "channels_form", "health_panel"],
                    "values": {list_key: normalized_items},
                },
                timeout_seconds=timeout_seconds,
            )

        selected_id = _coerce_entity_id(
            _as_dict(target).get(item_key) or _as_dict(input_payload).get(item_key)
        )
        if not selected_id:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="invalid_payload",
                response={"ok": False, "error": "invalid_payload"},
                timeout_seconds=timeout_seconds,
            )
        updated_items: list[dict[str, Any]] = []
        matched = False
        for item in current_items:
            row = dict(item)
            if _string_or(row.get(item_key)) == selected_id:
                matched = True
                if operation == "project_connection_list_toggle":
                    toggle_field = _string_or(config.get("toggle_field"), "enabled")
                    _nested_set(row, toggle_field, not bool(_nested_get(row, toggle_field, True)))
            if (
                operation != "project_connection_list_delete"
                or _string_or(row.get(item_key)) != selected_id
            ):
                updated_items.append(row)
        if not matched:
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail=not_found_error,
                response={"ok": False, "error": not_found_error},
                timeout_seconds=timeout_seconds,
            )
        merged_config[list_key] = updated_items
        connection_type = _string_or(
            config.get("connection_type_id") or connection.get("connection_type")
        )
        schema_errors = _connection_partition_errors(
            extension,
            connection_type=connection_type,
            partition="config",
            value=merged_config,
            runtime_context=runtime_context,
        )
        if schema_errors:
            return _invalid_connection_payload(
                extension, schema_errors, timeout_seconds=timeout_seconds
            )
        if not _save_project_connection_config(
            bl,
            project_id=project_id,
            extension_id=extension_id,
            connection_config=merged_config,
            extension_install_id=_string_or(_as_dict(runtime_context).get("extension_install_id")),
            connection_type=connection_type,
            account_id=_string_or(_as_dict(runtime_context).get("account_id")),
            runtime_context=runtime_context,
        ):
            return _platform_invocation_result(
                extension=extension,
                status="failed",
                detail="project_connection_storage_unavailable",
                response={"ok": False, "error": "project_connection_storage_unavailable"},
                timeout_seconds=timeout_seconds,
            )
        return _platform_invocation_result(
            extension=extension,
            status="succeeded",
            response={
                "ok": True,
                "message": "Channel updated"
                if operation == "project_connection_list_toggle"
                else "Channel deleted",
                "reload": [component_id or "channels_form", "health_panel"],
                "values": {list_key: updated_items},
            },
            timeout_seconds=timeout_seconds,
        )

    return _platform_invocation_result(
        extension=extension,
        status="failed",
        detail="Unsupported platform action handler",
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "_runtime_logger",
    "_string_or",
    "_coerce_entity_id",
    "_deep_merge_dict",
    "_expand_dotted_object",
    "_nested_set",
    "_nested_get",
    "_project_connection_row_for_extension",
    "_save_project_connection_config",
    "_coerce_positive_int",
    "_coerce_grid_pagination",
    "_safe_iso_datetime",
    "_as_text_set",
    "_matches_grid_filter",
    "_apply_grid_params",
    "_platform_invocation_result",
    "_resolve_platform_project_id",
    "_run_platform_query",
    "_run_platform_action",
]
