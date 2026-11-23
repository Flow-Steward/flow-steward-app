# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_invocation import _declared_operation_row
from .extension_runtime_platform_support import *  # NOSONAR
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


def _background_interval_fields(extension: dict) -> frozenset[str]:
    """Poll interval fields: changing one does not restart a poll's sync."""
    compiled = _as_dict(extension.get("compiled_manifest"))
    rows = extension.get("action_definitions") or compiled.get("action_definitions") or []
    fields: set[str] = set()
    for row in _as_list(rows):
        background = _as_dict(_as_dict(_as_dict(row).get("contract")).get("background"))
        if background.get("kind") == "connection_poll":
            field = _string_or(background.get("interval_field"))
            if field:
                fields.add(field)
    return frozenset(fields)


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
        selected_connection_id = _string_or(
            _as_dict(target).get("connection_id")
            or _as_dict(_as_dict(target).get("connection")).get("connection_id")
            or _as_dict(runtime_context).get("connection_id")
        )
        patch_connection_type = _string_or(config.get("connection_type_id"))
        if selected_connection_id:
            # The selected connection decides its own type; a patch that lists
            # connection_type_ids applies only to connections of those types.
            selected_row = _as_dict(
                bl.get_project_connection(
                    project_id=str(project_id),
                    connection_id=selected_connection_id,
                    extension_id=extension_id,
                    enabled_only=False,
                )
            )
            selected_type = _string_or(selected_row.get("connection_type"))
            allowed_types = [
                _string_or(item) for item in _as_list(config.get("connection_type_ids"))
            ]
            if not selected_row or (allowed_types and selected_type not in allowed_types):
                return _platform_invocation_result(
                    extension=extension,
                    status="failed",
                    detail="invalid_connection",
                    response={"ok": False, "error": "invalid_connection"},
                    timeout_seconds=timeout_seconds,
                )
            patch_connection_type = patch_connection_type or selected_type
        try:
            ingress_schema = _connection_partition_schema(
                extension,
                connection_type=patch_connection_type,
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
                connection_id=selected_connection_id,
                generation_exempt_fields=_background_interval_fields(extension),
                store=bl,
                project_id=str(project_id),
                extension_id=extension_id,
                config=config,
                input_payload=_as_dict(input_payload),
                extension_install_id=_string_or(
                    _as_dict(runtime_context).get("extension_install_id")
                ),
                connection_type=patch_connection_type,
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
