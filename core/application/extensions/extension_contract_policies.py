"""Validate extension connection, schema and artifact policy values."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from core.application.extensions.extension_contract_v2_common import (
    _as_bool,
    _as_text,
    _error,
    _normalize_lower,
    _positive_int,
)
from core.application.extensions.json_schema_subset import unsupported_schema_keywords

ALLOWED_WIDGET_TYPES: set[str] = {
    "text",
    "textarea",
    "number",
    "integer",
    "boolean",
    "select",
    "multi_select",
    "radio_group",
    "checkbox_group",
    "date",
    "datetime",
    "json",
    "key_value",
    "repeater",
    "resource_picker",
    "artifact_picker",
    "dataset_picker",
    "schema_mapper",
    "mapping_table",
    "batch_policy",
}


ALLOWED_OPERATION_KINDS: set[str] = {
    "source",
    "action",
    "bulk_action",
    "report",
    "human_channel",
}


ALLOWED_VALUE_TYPES: set[str] = {
    "text",
    "number",
    "integer",
    "boolean",
    "json",
    "array",
    "object",
    "artifact_handle",
    "dataset_handle",
    "connection_ref",
}


VALUE_TYPE_JSON_SCHEMA_TYPES: dict[str, str] = {
    "text": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


ALLOWED_CURSOR_MODES: set[str] = {
    "",
    "none",
    "offset",
    "cursor",
    "page",
}


ALLOWED_PARTIAL_FAILURE_MODES: set[str] = {
    "",
    "fail_fast",
    "report_failed_items",
    "best_effort",
}


ALLOWED_DATASET_RUNTIME_KINDS: set[str] = {
    "builtin",
    "local_extension",
    "remote_extension",
    "mcp_bridge",
}


ALLOWED_CONNECTION_AUTH_TYPES: set[str] = {
    "none",
    "api_key",
    "oauth2",
    "static_secret",
}


ALLOWED_ARTIFACT_POLICY_KINDS: set[str] = {"artifact", "dataset"}


ALLOWED_ARTIFACT_POLICY_RETENTION_CLASSES: set[str] = {
    "ephemeral",
    "run_window",
    "audit",
    "persistent",
}


ALLOWED_ARTIFACT_POLICY_VALUE_TYPES: set[str] = {"artifact_handle", "dataset_handle"}


RESERVED_ARTIFACT_POLICY_BINDING_KEYS: set[str] = {
    "action_id",
    "artifact_handle",
    "dataset_handle",
    "detail",
    "errors",
    "external_effect",
    "extension_id",
    "grant_outputs",
    "handled",
    "invocation",
    "ok",
    "produced_artifacts",
    "produced_datasets",
    "runtime",
}


ARTIFACT_POLICY_GRANT_GROUPS: tuple[str, ...] = (
    "inputs",
    "read_grants",
    "reads",
    "outputs",
    "write_grants",
    "writes",
)


def _reject_unexecutable_schema(
    errors: list[dict[str, Any]],
    *,
    schema: Any,
    path: str,
) -> None:
    """Refuse a declared schema the platform would silently fail to enforce.

    An author writing `allOf` / `if` / `then` has every reason to believe the
    requirement is enforced. Before this check it was not: unknown keywords were
    skipped, so the declaration was decoration and a connection missing every
    conditionally-required field installed cleanly. Saying so at install time is
    the only honest option — the alternative is a contract that looks enforced
    and is not.
    """
    if not isinstance(schema, dict):
        return
    for finding in unsupported_schema_keywords(schema, path=path):
        _error(
            errors,
            path=path,
            message=finding,
            code="unsupported_schema_keyword",
        )


def _validate_batch_capabilities(
    *,
    operation: dict[str, Any],
    operation_index: int,
    operation_kind: str,
    partial_failure_mode: str,
    errors: list[dict[str, str]],
) -> None:
    supports_batch = _as_bool(operation.get("supports_batch"))
    if operation_kind == "bulk_action" and not supports_batch:
        _error(
            errors,
            path=f"extension_contract_v2.operation_manifest[{operation_index}].supports_batch",
            message="bulk_action operations must declare supports_batch=true",
            code="missing_batch_capabilities",
        )
        return
    if not supports_batch:
        return

    preferred_batch_size = _positive_int(operation.get("preferred_batch_size"))
    if preferred_batch_size is None:
        _error(
            errors,
            path=f"extension_contract_v2.operation_manifest[{operation_index}].preferred_batch_size",
            message="preferred_batch_size must be a positive integer when supports_batch=true",
            code="invalid_batch_capability",
        )

    max_batch_size = _positive_int(operation.get("max_batch_size"))
    if max_batch_size is None:
        _error(
            errors,
            path=f"extension_contract_v2.operation_manifest[{operation_index}].max_batch_size",
            message="max_batch_size must be a positive integer when supports_batch=true",
            code="invalid_batch_capability",
        )
    elif preferred_batch_size is not None and max_batch_size < preferred_batch_size:
        _error(
            errors,
            path=f"extension_contract_v2.operation_manifest[{operation_index}].max_batch_size",
            message="max_batch_size must be >= preferred_batch_size",
            code="invalid_batch_capability",
        )

    batch_input_field = _as_text(operation.get("batch_input_field"))
    if not batch_input_field:
        _error(
            errors,
            path=f"extension_contract_v2.operation_manifest[{operation_index}].batch_input_field",
            message="batch_input_field is required when supports_batch=true",
            code="invalid_batch_capability",
        )

    if not partial_failure_mode:
        _error(
            errors,
            path=f"extension_contract_v2.operation_manifest[{operation_index}].partial_failure_mode",
            message="partial_failure_mode is required when supports_batch=true",
            code="invalid_batch_capability",
        )

    supports_async_job = operation.get("supports_async_job")
    if not isinstance(supports_async_job, bool):
        _error(
            errors,
            path=f"extension_contract_v2.operation_manifest[{operation_index}].supports_async_job",
            message="supports_async_job must be a boolean when supports_batch=true",
            code="invalid_batch_capability",
        )


def _collect_ids(
    rows: list[dict[str, Any]],
    *,
    id_field: str,
    path_prefix: str,
    errors: list[dict[str, str]],
) -> set[str]:
    seen: set[str] = set()
    for idx, row in enumerate(rows):
        row_id = _as_text(row.get(id_field))
        if not row_id:
            _error(
                errors,
                path=f"{path_prefix}[{idx}].{id_field}",
                message=f"{id_field} is required",
                code="missing_id",
            )
            continue
        if row_id in seen:
            _error(
                errors,
                path=f"{path_prefix}[{idx}].{id_field}",
                message=f"Duplicate {id_field} '{row_id}'",
                code="duplicate_id",
            )
            continue
        seen.add(row_id)
    return seen


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validate_oauth_string_list(
    value: Any,
    *,
    path: str,
    errors: list[dict[str, str]],
) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        _error(
            errors,
            path=path,
            message="OAuth scope list must be an array of strings",
            code="invalid_connection_auth_oauth_scopes",
        )
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            _error(
                errors,
                path=f"{path}[{idx}]",
                message="OAuth scope values must be strings",
                code="invalid_connection_auth_oauth_scopes",
            )
            continue
        text = _as_text(item)
        if not text:
            _error(
                errors,
                path=f"{path}[{idx}]",
                message="OAuth scope values must be non-empty strings",
                code="invalid_connection_auth_oauth_scopes",
            )
            continue
        if text in seen:
            _error(
                errors,
                path=f"{path}[{idx}]",
                message=f"Duplicate OAuth scope '{text}'",
                code="duplicate_connection_auth_oauth_scope",
            )
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _validate_oauth_scopes(
    value: Any,
    *,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(value, dict):
        _error(
            errors,
            path=path,
            message="OAuth2 connection auth requires scopes object",
            code="invalid_connection_auth_oauth_scopes",
        )
        return
    required_scopes = _validate_oauth_string_list(
        value.get("required"),
        path=f"{path}.required",
        errors=errors,
    )
    optional_scopes = _validate_oauth_string_list(
        value.get("optional"),
        path=f"{path}.optional",
        errors=errors,
    )
    if not required_scopes:
        _error(
            errors,
            path=f"{path}.required",
            message="OAuth2 connection auth requires at least one required scope",
            code="missing_connection_auth_oauth_required_scope",
        )
    for scope in sorted(set(required_scopes) & set(optional_scopes)):
        _error(
            errors,
            path=path,
            message=f"OAuth scope '{scope}' cannot be both required and optional",
            code="overlapping_connection_auth_oauth_scope",
        )


def _validate_platform_secret_credential(
    credential: Any,
    *,
    path: str,
    required: bool,
    errors: list[dict[str, str]],
) -> None:
    if credential in (None, ""):
        if required:
            _error(
                errors,
                path=path,
                message="OAuth platform credential declaration is required",
                code="missing_connection_auth_oauth_client_credentials",
            )
        return
    if not isinstance(credential, dict):
        _error(
            errors,
            path=path,
            message="OAuth platform credential declaration must be an object",
            code="invalid_connection_auth_oauth_client_credentials",
        )
        return
    provider_key = _normalize_lower(credential.get("provider_key"))
    if provider_key:
        _error(
            errors,
            path=f"{path}.provider_key",
            message="Legacy provider-owned OAuth credentials are unsupported",
            code="invalid_connection_auth_oauth_client_credentials",
        )
        return
    source = _normalize_lower(credential.get("source"))
    if source != "platform_secret":
        _error(
            errors,
            path=f"{path}.source",
            message="OAuth client credentials must use source=platform_secret",
            code="invalid_connection_auth_oauth_client_credentials",
        )


def _validate_connection_type_auth(
    connection_type: dict[str, Any],
    *,
    oauth_provider_ids: set[str],
    idx: int,
    errors: list[dict[str, str]],
) -> None:
    auth_value = connection_type.get("auth")
    if auth_value in (None, ""):
        return
    path_prefix = f"extension_contract_v2.connection_types[{idx}].auth"
    if not isinstance(auth_value, dict):
        _error(
            errors,
            path=path_prefix,
            message="connection type auth must be an object",
            code="invalid_connection_auth",
        )
        return
    auth = dict(auth_value)
    auth_type = _normalize_lower(auth.get("type") or "none") or "none"
    if auth_type not in ALLOWED_CONNECTION_AUTH_TYPES:
        _error(
            errors,
            path=f"{path_prefix}.type",
            message=f"Unsupported connection auth.type '{auth_type}'",
            code="unsupported_connection_auth_type",
        )
        return
    if auth_type != "oauth2":
        return

    provider_id = _as_text(auth.get("provider_id"))
    if provider_id:
        if provider_id not in oauth_provider_ids:
            _error(
                errors,
                path=f"{path_prefix}.provider_id",
                message=f"OAuth provider '{provider_id}' is not declared by this extension",
                code="unknown_connection_oauth_provider",
            )
        _validate_oauth_scopes(
            auth.get("scopes"),
            path=f"{path_prefix}.scopes",
            errors=errors,
        )
        return

    if _normalize_lower(auth.get("grant")) != "authorization_code":
        _error(
            errors,
            path=f"{path_prefix}.grant",
            message="OAuth2 connection auth requires grant=authorization_code",
            code="invalid_connection_auth_oauth_grant",
        )
    if _normalize_lower(auth.get("pkce")) != "required":
        _error(
            errors,
            path=f"{path_prefix}.pkce",
            message="OAuth2 connection auth requires pkce=required",
            code="invalid_connection_auth_oauth_pkce",
        )
    for endpoint_key, required in (
        ("authorization_endpoint", True),
        ("token_endpoint", True),
        ("revocation_endpoint", False),
    ):
        endpoint = _as_text(auth.get(endpoint_key))
        if not endpoint:
            if required:
                _error(
                    errors,
                    path=f"{path_prefix}.{endpoint_key}",
                    message=f"OAuth2 connection auth requires {endpoint_key}",
                    code="missing_connection_auth_oauth_endpoint",
                )
            continue
        if not _valid_http_url(endpoint):
            _error(
                errors,
                path=f"{path_prefix}.{endpoint_key}",
                message=f"OAuth2 {endpoint_key} must be an http(s) URL",
                code="invalid_connection_auth_oauth_endpoint",
            )

    _validate_oauth_scopes(
        auth.get("scopes"),
        path=f"{path_prefix}.scopes",
        errors=errors,
    )

    credentials = auth.get("client_credentials")
    if not isinstance(credentials, dict):
        _error(
            errors,
            path=f"{path_prefix}.client_credentials",
            message="OAuth2 connection auth requires client_credentials object",
            code="missing_connection_auth_oauth_client_credentials",
        )
    else:
        _validate_platform_secret_credential(
            credentials.get("client_id"),
            path=f"{path_prefix}.client_credentials.client_id",
            required=True,
            errors=errors,
        )
        client_secret = credentials.get("client_secret")
        secret_optional = isinstance(client_secret, dict) and bool(client_secret.get("optional"))
        _validate_platform_secret_credential(
            client_secret,
            path=f"{path_prefix}.client_credentials.client_secret",
            required=not secret_optional,
            errors=errors,
        )

    if _normalize_lower(auth.get("token_placement") or "secrets") != "secrets":
        _error(
            errors,
            path=f"{path_prefix}.token_placement",
            message="OAuth2 connection auth requires token_placement=secrets",
            code="invalid_connection_auth_oauth_token_placement",
        )
    secret_names = auth.get("secret_names")
    if not isinstance(secret_names, dict) or not _as_text(secret_names.get("access_token")):
        _error(
            errors,
            path=f"{path_prefix}.secret_names.access_token",
            message="OAuth2 connection auth requires secret_names.access_token",
            code="missing_connection_auth_oauth_secret_name",
        )


def _validate_public_io_value_types(
    *,
    operation: dict[str, Any],
    operation_index: int,
    errors: list[dict[str, str]],
) -> None:
    for group in ("public_inputs", "inputs", "public_outputs", "outputs"):
        items = operation.get(group)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            value_type = _normalize_lower(item.get("value_type") or item.get("type"))
            if value_type and value_type not in ALLOWED_VALUE_TYPES:
                _error(
                    errors,
                    path=f"extension_contract_v2.operation_manifest[{operation_index}].{group}[{idx}].value_type",
                    message=f"Unknown value_type '{value_type}'",
                    code="invalid_value_type",
                )
            if "schema" not in item:
                continue
            schema = item.get("schema")
            schema_path = (
                f"extension_contract_v2.operation_manifest[{operation_index}].{group}[{idx}].schema"
            )
            if not isinstance(schema, dict):
                _error(
                    errors,
                    path=schema_path,
                    message="schema must be an object",
                    code="invalid_field_type",
                )
                continue
            _reject_unexecutable_schema(errors, schema=schema, path=schema_path)
            expected_schema_type = VALUE_TYPE_JSON_SCHEMA_TYPES.get(value_type)
            raw_schema_type = schema.get("type")
            declared_schema_types: list[str] = []
            if isinstance(raw_schema_type, str):
                declared_schema_type = _normalize_lower(raw_schema_type)
                if declared_schema_type:
                    declared_schema_types = [declared_schema_type]
            elif isinstance(raw_schema_type, list):
                if (
                    not raw_schema_type
                    or any(
                        not isinstance(item, str) or not item.strip() for item in raw_schema_type
                    )
                    or len({_normalize_lower(item) for item in raw_schema_type})
                    != len(raw_schema_type)
                ):
                    _error(
                        errors,
                        path=f"{schema_path}.type",
                        message="schema type list must contain unique non-empty strings",
                        code="invalid_io_schema_type",
                    )
                else:
                    declared_schema_types = [_normalize_lower(item) for item in raw_schema_type]
            elif raw_schema_type is not None:
                _error(
                    errors,
                    path=f"{schema_path}.type",
                    message="schema type must be a string or a non-empty list of unique strings",
                    code="invalid_io_schema_type",
                )
            if (
                expected_schema_type
                and declared_schema_types
                and expected_schema_type not in declared_schema_types
            ):
                _error(
                    errors,
                    path=f"{schema_path}.type",
                    message=(
                        f"schema types {declared_schema_types!r} do not include "
                        f"value_type '{value_type}'"
                    ),
                    code="io_schema_type_mismatch",
                )


def _validate_artifact_policy_grants(
    *,
    policy: dict[str, Any],
    policy_index: int,
    errors: list[dict[str, str]],
) -> None:
    for group in ARTIFACT_POLICY_GRANT_GROUPS:
        raw_items = policy.get(group)
        if raw_items is None:
            continue
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            _error(
                errors,
                path=f"extension_contract_v2.artifact_policies[{policy_index}].{group}",
                message=f"artifact_policies[].{group} must be a list",
                code="invalid_artifact_policy_grants",
            )
            continue
        for item_index, raw_item in enumerate(raw_items):
            path = f"extension_contract_v2.artifact_policies[{policy_index}].{group}[{item_index}]"
            if not isinstance(raw_item, dict):
                _error(
                    errors,
                    path=path,
                    message=f"artifact_policies[].{group} entries must be objects",
                    code="invalid_artifact_policy_grant",
                )
                continue
            kind = _normalize_lower(raw_item.get("kind") or raw_item.get("grant_kind"))
            value_type = _normalize_lower(raw_item.get("value_type") or raw_item.get("type"))
            binding_key = _as_text(raw_item.get("binding_key") or raw_item.get("name"))
            retention_class = _normalize_lower(
                raw_item.get("retention_class") or raw_item.get("retention")
            )
            if binding_key in RESERVED_ARTIFACT_POLICY_BINDING_KEYS:
                _error(
                    errors,
                    path=f"{path}.binding_key",
                    message=f"artifact policy binding_key '{binding_key}' is reserved",
                    code="reserved_artifact_policy_binding_key",
                )
            if kind and kind not in ALLOWED_ARTIFACT_POLICY_KINDS:
                _error(
                    errors,
                    path=f"{path}.kind",
                    message=f"Unknown artifact policy kind '{kind}'",
                    code="invalid_artifact_policy_kind",
                )
            if value_type and value_type not in ALLOWED_ARTIFACT_POLICY_VALUE_TYPES:
                _error(
                    errors,
                    path=f"{path}.value_type",
                    message=f"Unknown artifact policy value_type '{value_type}'",
                    code="invalid_artifact_policy_value_type",
                )
            if kind == "artifact" and value_type == "dataset_handle":
                _error(
                    errors,
                    path=f"{path}.value_type",
                    message="artifact policy kind 'artifact' requires value_type 'artifact_handle'",
                    code="artifact_policy_kind_value_type_mismatch",
                )
            if kind == "dataset" and value_type == "artifact_handle":
                _error(
                    errors,
                    path=f"{path}.value_type",
                    message="artifact policy kind 'dataset' requires value_type 'dataset_handle'",
                    code="artifact_policy_kind_value_type_mismatch",
                )
            if (
                "max_size_bytes" in raw_item
                and _positive_int(raw_item.get("max_size_bytes")) is None
            ):
                _error(
                    errors,
                    path=f"{path}.max_size_bytes",
                    message="artifact policy max_size_bytes must be a positive integer",
                    code="invalid_artifact_policy_max_size",
                )
            max_size_env = _as_text(raw_item.get("max_size_env"))
            if max_size_env and (
                max_size_env != "FS_EXTENSION_ARTIFACT_MAX_BYTES"
                or _positive_int(raw_item.get("max_size_bytes")) != 200 * 1024 * 1024
            ):
                _error(
                    errors,
                    path=f"{path}.max_size_env",
                    message=(
                        "artifact policy max_size_env requires the canonical "
                        "FS_EXTENSION_ARTIFACT_MAX_BYTES default of 209715200 bytes"
                    ),
                    code="invalid_artifact_policy_max_size_env",
                )
            if retention_class and retention_class not in ALLOWED_ARTIFACT_POLICY_RETENTION_CLASSES:
                _error(
                    errors,
                    path=f"{path}.retention_class",
                    message=f"Unknown artifact policy retention_class '{retention_class}'",
                    code="invalid_artifact_policy_retention_class",
                )
            state_ref = raw_item.get("from_project_state")
            if state_ref is None:
                continue
            if not isinstance(state_ref, dict):
                _error(
                    errors,
                    path=f"{path}.from_project_state",
                    message="from_project_state must be an object",
                    code="invalid_artifact_policy_project_state_ref",
                )
                continue
            if not _as_text(state_ref.get("namespace")):
                _error(
                    errors,
                    path=f"{path}.from_project_state.namespace",
                    message="from_project_state.namespace is required",
                    code="missing_artifact_policy_project_state_namespace",
                )
            if not _as_text(state_ref.get("state_key") or state_ref.get("key")):
                _error(
                    errors,
                    path=f"{path}.from_project_state.state_key",
                    message="from_project_state.state_key is required",
                    code="missing_artifact_policy_project_state_key",
                )
