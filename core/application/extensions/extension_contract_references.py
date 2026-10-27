"""Validate relationships between typed extension contract sections."""

from __future__ import annotations

from typing import Any

from core.application.extensions.extension_contract_policies import (
    ALLOWED_CURSOR_MODES,
    ALLOWED_DATASET_RUNTIME_KINDS,
    ALLOWED_OPERATION_KINDS,
    ALLOWED_PARTIAL_FAILURE_MODES,
    ALLOWED_WIDGET_TYPES,
    _collect_ids,
    _reject_unexecutable_schema,
    _validate_artifact_policy_grants,
    _validate_batch_capabilities,
    _validate_connection_type_auth,
    _validate_public_io_value_types,
)
from core.application.extensions.extension_contract_v2_common import (
    _as_bool,
    _as_text,
    _error,
    _normalize_lower,
)


def _validate_ids_and_references(
    contract: dict[str, list[dict[str, Any]]],
    *,
    errors: list[dict[str, str]],
) -> None:
    oauth_provider_ids = _collect_ids(
        contract["oauth_providers"],
        id_field="provider_id",
        path_prefix="extension_contract_v2.oauth_providers",
        errors=errors,
    )
    connection_ids = _collect_ids(
        contract["connection_types"],
        id_field="connection_type_id",
        path_prefix="extension_contract_v2.connection_types",
        errors=errors,
    )
    connection_types_by_id = {
        _as_text(row.get("connection_type_id")): row
        for row in contract["connection_types"]
        if _as_text(row.get("connection_type_id"))
    }
    dataset_connector_ids = _collect_ids(
        contract["dataset_connectors"],
        id_field="connector_id",
        path_prefix="extension_contract_v2.dataset_connectors",
        errors=errors,
    )
    operation_ids = _collect_ids(
        contract["operation_manifest"],
        id_field="operation_id",
        path_prefix="extension_contract_v2.operation_manifest",
        errors=errors,
    )
    query_ids = _collect_ids(
        contract["resource_query_manifest"],
        id_field="query_id",
        path_prefix="extension_contract_v2.resource_query_manifest",
        errors=errors,
    )
    _ = dataset_connector_ids
    _collect_ids(
        contract["event_bindings"],
        id_field="trigger_id",
        path_prefix="extension_contract_v2.event_bindings",
        errors=errors,
    )

    for idx, connection_type in enumerate(contract["connection_types"]):
        if "connection_type" in connection_type:
            _error(
                errors,
                path=f"extension_contract_v2.connection_types[{idx}].connection_type",
                message="connection_type is not allowed; use connection_type_id",
                code="legacy_connection_type_field",
            )
        _validate_connection_type_auth(
            connection_type,
            oauth_provider_ids=oauth_provider_ids,
            idx=idx,
            errors=errors,
        )
        for field_name in ("config_schema", "secret_schema", "ingress_schema"):
            schema = connection_type.get(field_name)
            if schema is None:
                continue
            if not isinstance(schema, dict):
                _error(
                    errors,
                    path=f"extension_contract_v2.connection_types[{idx}].{field_name}",
                    message=f"{field_name} must be an object",
                    code="invalid_field_type",
                )
                continue
            _reject_unexecutable_schema(
                errors,
                schema=schema,
                path=f"extension_contract_v2.connection_types[{idx}].{field_name}",
            )

    for idx, connector in enumerate(contract["dataset_connectors"]):
        connector_path = f"extension_contract_v2.dataset_connectors[{idx}]"
        display_name = _as_text(connector.get("display_name") or connector.get("name"))
        if not display_name:
            _error(
                errors,
                path=f"{connector_path}.display_name",
                message="display_name is required",
                code="missing_required_field",
            )
        query_language = _as_text(connector.get("query_language"))
        if not query_language:
            _error(
                errors,
                path=f"{connector_path}.query_language",
                message="query_language is required",
                code="missing_required_field",
            )
        runtime_kind = _normalize_lower(connector.get("runtime_kind") or "local_extension")
        if runtime_kind not in ALLOWED_DATASET_RUNTIME_KINDS:
            _error(
                errors,
                path=f"{connector_path}.runtime_kind",
                message=f"Unknown runtime_kind '{runtime_kind}'",
                code="invalid_runtime_kind",
            )
        runtime_adapter_ref = _as_text(connector.get("runtime_adapter_ref"))
        if not runtime_adapter_ref:
            _error(
                errors,
                path=f"{connector_path}.runtime_adapter_ref",
                message="runtime_adapter_ref is required",
                code="missing_required_field",
            )
        for field_name in ("connection_schema", "secret_schema"):
            _reject_unexecutable_schema(
                errors,
                schema=connector.get(field_name),
                path=f"{connector_path}.{field_name}",
            )
        for field_name in (
            "connection_schema",
            "secret_schema",
            "capabilities",
            "safety_profile",
        ):
            value = connector.get(field_name)
            if not isinstance(value, dict):
                _error(
                    errors,
                    path=f"{connector_path}.{field_name}",
                    message=f"{field_name} must be an object",
                    code="invalid_field_type",
                )
        for field_name in ("preview_support", "schema_refresh_support"):
            value = connector.get(field_name)
            if not isinstance(value, bool):
                _error(
                    errors,
                    path=f"{connector_path}.{field_name}",
                    message=f"{field_name} must be a boolean",
                    code="invalid_field_type",
                )
        catalog = connector.get("connector_catalog", [])
        if not isinstance(catalog, list):
            _error(
                errors,
                path=f"{connector_path}.connector_catalog",
                message="connector_catalog must be a list",
                code="invalid_field_type",
            )
            continue
        catalog_ids: set[str] = set()
        for catalog_idx, catalog_item in enumerate(catalog):
            catalog_path = f"{connector_path}.connector_catalog[{catalog_idx}]"
            if not isinstance(catalog_item, dict):
                _error(
                    errors,
                    path=catalog_path,
                    message="connector_catalog items must be objects",
                    code="invalid_field_type",
                )
                continue
            catalog_id = _as_text(catalog_item.get("connector_type_id"))
            if not catalog_id:
                _error(
                    errors,
                    path=f"{catalog_path}.connector_type_id",
                    message="connector_type_id is required",
                    code="missing_required_field",
                )
            elif catalog_id in catalog_ids:
                _error(
                    errors,
                    path=f"{catalog_path}.connector_type_id",
                    message=f"Duplicate connector_type_id '{catalog_id}'",
                    code="duplicate_id",
                )
            else:
                catalog_ids.add(catalog_id)
            for field_name in ("connection_schema", "secret_schema"):
                if field_name in catalog_item:
                    _reject_unexecutable_schema(
                        errors,
                        schema=catalog_item.get(field_name),
                        path=f"{catalog_path}.{field_name}",
                    )
            for field_name in ("preset_config", "connection_schema", "secret_schema"):
                if field_name in catalog_item and not isinstance(catalog_item[field_name], dict):
                    _error(
                        errors,
                        path=f"{catalog_path}.{field_name}",
                        message=f"{field_name} must be an object",
                        code="invalid_field_type",
                    )

    for idx, operation in enumerate(contract["operation_manifest"]):
        operation_kind = _normalize_lower(
            operation.get("operation_kind") or operation.get("kind") or "action"
        )
        if operation_kind not in ALLOWED_OPERATION_KINDS:
            _error(
                errors,
                path=f"extension_contract_v2.operation_manifest[{idx}].operation_kind",
                message=f"Unknown operation_kind '{operation_kind}'",
                code="invalid_operation_kind",
            )

        input_mapping_rules = operation.get("input_mapping_rules")
        rules_path = f"extension_contract_v2.operation_manifest[{idx}].input_mapping_rules"
        operation_input_names = {
            _as_text(item.get("name"))
            for item in operation.get("inputs", [])
            if isinstance(item, dict) and _as_text(item.get("name"))
        }
        if input_mapping_rules is not None:
            if not isinstance(input_mapping_rules, list) or not input_mapping_rules:
                _error(
                    errors,
                    path=rules_path,
                    message="input_mapping_rules must be a non-empty list",
                    code="invalid_operation_input_mapping_rules",
                )
            else:
                for rule_index, rule in enumerate(input_mapping_rules):
                    rule_path = f"{rules_path}[{rule_index}]"
                    if (
                        not isinstance(rule, dict)
                        or not isinstance(rule.get("schema"), dict)
                        or not rule.get("schema")
                    ):
                        _error(
                            errors,
                            path=rule_path,
                            message="input mapping rule requires a schema object",
                            code="invalid_operation_input_mapping_rule",
                        )
                        continue
                    _reject_unexecutable_schema(
                        errors, schema=rule["schema"], path=f"{rule_path}.schema"
                    )
                    error = rule.get("error")
                    if not isinstance(error, dict) or not all(
                        isinstance(error.get(key), str) and _as_text(error.get(key))
                        for key in ("code", "message", "field")
                    ):
                        _error(
                            errors,
                            path=f"{rule_path}.error",
                            message="rule error requires non-empty code, message, and field",
                            code="invalid_operation_input_mapping_rule_error",
                        )
                    elif _as_text(error.get("field")) not in operation_input_names:
                        _error(
                            errors,
                            path=f"{rule_path}.error.field",
                            message="rule error field must name a declared operation input",
                            code="unknown_operation_input_mapping_rule_field",
                        )

        # The plural field is where an operation's connection policy now lives,
        # so it is checked like any other contract value. Validating only the
        # singular aliases meant a manifest could name a connection type the
        # bundle never declares and still be called clean.
        plural_path = f"extension_contract_v2.operation_manifest[{idx}].connection_type_ids"
        for singular_key in (
            "connection_type_id",
            "connection_type",
            "required_connection_type",
        ):
            if singular_key in operation:
                _error(
                    errors,
                    path=f"extension_contract_v2.operation_manifest[{idx}].{singular_key}",
                    message=(
                        f"{singular_key} is not allowed; declare connection_type_ids as a list"
                    ),
                    code="singular_operation_connection_type_forbidden",
                )
        raw_plural = operation.get("connection_type_ids")
        plural_refs: list[str] = []
        if raw_plural is not None:
            if not isinstance(raw_plural, (list, tuple)):
                _error(
                    errors,
                    path=plural_path,
                    message="connection_type_ids must be a list of declared connection type ids",
                    code="invalid_connection_type",
                )
            else:
                for entry_index, entry in enumerate(raw_plural):
                    entry_path = f"{plural_path}[{entry_index}]"
                    if not isinstance(entry, str) or not entry.strip():
                        _error(
                            errors,
                            path=entry_path,
                            message="connection_type_ids entries must be non-empty strings",
                            code="invalid_connection_type",
                        )
                        continue
                    value = entry.strip()
                    if value in plural_refs:
                        _error(
                            errors,
                            path=entry_path,
                            message=f"Duplicate connection_type_id '{value}'",
                            code="duplicate_connection_type",
                        )
                        continue
                    plural_refs.append(value)
        for connection_ref in plural_refs:
            if connection_ref and connection_ref not in connection_ids:
                _error(
                    errors,
                    path=plural_path,
                    message=f"Unknown connection_type_id '{connection_ref}'",
                    code="unknown_connection_type",
                )
        required_connection_ref = any(
            isinstance(item, dict)
            and _as_text(item.get("name")) == "connection_ref"
            and _as_bool(item.get("required"))
            for item in operation.get("inputs", [])
        )
        if (
            operation.get("workflow_visible") is not False
            and required_connection_ref
            and not plural_refs
        ):
            _error(
                errors,
                path=plural_path,
                message=(
                    "A workflow-visible operation with required connection_ref must declare "
                    "connection_type_ids"
                ),
                code="operation_connection_ref_without_connection_type",
            )

        raw_required_oauth_scopes = operation.get("required_oauth_scopes", [])
        operation_scope_path = (
            f"extension_contract_v2.operation_manifest[{idx}].required_oauth_scopes"
        )
        required_oauth_scopes: list[str] = []
        if not isinstance(raw_required_oauth_scopes, list):
            _error(
                errors,
                path=operation_scope_path,
                message="required_oauth_scopes must be a list",
                code="invalid_operation_oauth_scopes",
            )
        else:
            seen_operation_scopes: set[str] = set()
            for scope_idx, value in enumerate(raw_required_oauth_scopes):
                scope = _as_text(value) if isinstance(value, str) else ""
                if not scope:
                    _error(
                        errors,
                        path=f"{operation_scope_path}[{scope_idx}]",
                        message="Operation OAuth scopes must be non-empty strings",
                        code="invalid_operation_oauth_scopes",
                    )
                    continue
                if scope in seen_operation_scopes:
                    _error(
                        errors,
                        path=f"{operation_scope_path}[{scope_idx}]",
                        message=f"Duplicate operation OAuth scope '{scope}'",
                        code="duplicate_operation_oauth_scope",
                    )
                    continue
                seen_operation_scopes.add(scope)
                required_oauth_scopes.append(scope)
        if required_oauth_scopes:
            if not plural_refs:
                _error(
                    errors,
                    path=operation_scope_path,
                    message="Operation OAuth scopes require connection_type_ids",
                    code="operation_oauth_scope_without_connection",
                )
            for connection_ref in plural_refs:
                connection_type = connection_types_by_id.get(connection_ref, {})
                auth = connection_type.get("auth")
                auth = auth if isinstance(auth, dict) else {}
                scopes = auth.get("scopes")
                scopes = scopes if isinstance(scopes, dict) else {}
                declared_scopes: set[str] = set()
                for group in ("required", "optional"):
                    scope_values = scopes.get(group)
                    if not isinstance(scope_values, list):
                        continue
                    declared_scopes.update(
                        _as_text(value)
                        for value in scope_values
                        if isinstance(value, str) and _as_text(value)
                    )
                for scope_idx, scope in enumerate(required_oauth_scopes):
                    if scope not in declared_scopes:
                        _error(
                            errors,
                            path=f"{operation_scope_path}[{scope_idx}]",
                            message=(
                                f"Operation OAuth scope '{scope}' is not declared by connection "
                                f"type '{connection_ref}'"
                            ),
                            code="operation_oauth_scope_outside_connection",
                        )

        partial_failure_mode = _normalize_lower(operation.get("partial_failure_mode"))
        if partial_failure_mode not in ALLOWED_PARTIAL_FAILURE_MODES:
            _error(
                errors,
                path=f"extension_contract_v2.operation_manifest[{idx}].partial_failure_mode",
                message=f"Unknown partial_failure_mode '{partial_failure_mode}'",
                code="invalid_partial_failure_mode",
            )
        _validate_batch_capabilities(
            operation=operation,
            operation_index=idx,
            operation_kind=operation_kind,
            partial_failure_mode=partial_failure_mode,
            errors=errors,
        )

        _validate_public_io_value_types(
            operation=operation,
            operation_index=idx,
            errors=errors,
        )

    for idx, query in enumerate(contract["resource_query_manifest"]):
        cursor_mode = _normalize_lower(query.get("cursor_mode") or "none")
        if cursor_mode not in ALLOWED_CURSOR_MODES:
            _error(
                errors,
                path=f"extension_contract_v2.resource_query_manifest[{idx}].cursor_mode",
                message=f"Unknown cursor_mode '{cursor_mode}'",
                code="invalid_cursor_mode",
            )

    for idx, step_ui in enumerate(contract["step_ui_manifest"]):
        operation_id = _as_text(step_ui.get("operation_id"))
        if operation_id and operation_id not in operation_ids:
            _error(
                errors,
                path=f"extension_contract_v2.step_ui_manifest[{idx}].operation_id",
                message=f"Unknown operation_id '{operation_id}'",
                code="unknown_operation_id",
            )

        fields = step_ui.get("fields")
        if isinstance(fields, list):
            for field_idx, field in enumerate(fields):
                if not isinstance(field, dict):
                    continue
                widget_type = _normalize_lower(
                    field.get("widget") or field.get("widget_type") or field.get("type")
                )
                if widget_type and widget_type not in ALLOWED_WIDGET_TYPES:
                    _error(
                        errors,
                        path=(
                            "extension_contract_v2.step_ui_manifest"
                            f"[{idx}].fields[{field_idx}].widget_type"
                        ),
                        message=f"Unknown widget type '{widget_type}'",
                        code="invalid_widget_type",
                    )

                query_ref = _as_text(
                    field.get("query_id")
                    or field.get("resource_query_id")
                    or field.get("picker_query_id")
                )
                if query_ref and query_ref not in query_ids:
                    _error(
                        errors,
                        path=(
                            "extension_contract_v2.step_ui_manifest"
                            f"[{idx}].fields[{field_idx}].query_id"
                        ),
                        message=f"Unknown query_id '{query_ref}'",
                        code="unknown_query_id",
                    )

    for idx, binding in enumerate(contract["event_bindings"]):
        operation_id = _as_text(binding.get("operation_id"))
        if operation_id and operation_id not in operation_ids:
            _error(
                errors,
                path=f"extension_contract_v2.event_bindings[{idx}].operation_id",
                message=f"Unknown operation_id '{operation_id}'",
                code="unknown_operation_id",
            )

    for idx, policy in enumerate(contract["artifact_policies"]):
        operation_id = _as_text(policy.get("operation_id"))
        if operation_id and operation_id not in operation_ids:
            _error(
                errors,
                path=f"extension_contract_v2.artifact_policies[{idx}].operation_id",
                message=f"Unknown operation_id '{operation_id}'",
                code="unknown_operation_id",
            )
        _validate_artifact_policy_grants(policy=policy, policy_index=idx, errors=errors)
