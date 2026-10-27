"""Registry contracts and views shared by MCP connector services."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.application.management.scope import ScopeContext, normalize_scope

from .errors import (
    ServiceForbiddenError,
    ServiceValidationError,
)
from .mcp_connector_http import (
    _mcp_header_annotations,
)
from .mcp_connector_observability import (
    logger,
)
from .mcp_connector_protocol import (
    McpProtocolError,
)

_MESSAGE_MCP_TOOLS_LIST_RETURNED_A_MALFORMED_TOOLS_PAGE = (
    "MCP tools/list returned a malformed tools page"
)


DATABASE_NOT_CONFIGURED_MESSAGE = "Database not configured"


_MCP_CONNECTOR_MUTATION_PRINCIPAL_TYPES = {
    "admin",
    "operator",
    "service_account",
    "system_actor",
}


_MCP_CONNECTOR_SYNC_STATUS_VALUES = {"never", "success", "failed"}


_MCP_PROJECTED_TOOL_CATEGORY = "mcp_tool_action"


_MCP_PROJECTED_TOOL_TAGS = ["mcp", "derived"]


MCP_OUTPUT_PROJECTION_CONFIG_KEY = "mcp_output_projection"


MCP_OUTPUT_PROJECTION_WHOLE_OBJECT_RESULT = "whole_object_result"


_MCP_STABLE_OBJECT_CONTRACT_KEYS = frozenset(
    {
        "$ref",
        "allOf",
        "anyOf",
        "oneOf",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "propertyNames",
        "unevaluatedProperties",
    }
)


def _protobuf_struct_open_object_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ProtoJSON shape for an exact protobuf ``Struct`` descriptor.

    Some protobuf-backed MCP servers publish the message descriptor (a ``fields``
    map) as JSON Schema even though ProtoJSON serializes the well-known ``Struct``
    type as the map itself. Require both the canonical descriptions and the exact
    structural signature so a provider output that genuinely owns a ``fields``
    property is never rewritten.
    """
    if str(schema.get("type") or "").strip().lower() != "object":
        return None
    if _has_stable_object_contract(schema):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != {"fields"}:
        return None
    fields_schema = properties.get("fields")
    if not isinstance(fields_schema, dict):
        return None
    if str(fields_schema.get("type") or "").strip().lower() != "object":
        return None
    additional = fields_schema.get("additionalProperties")
    if additional is not True and not isinstance(additional, dict):
        return None
    description = str(schema.get("description") or "").strip().lower()
    fields_description = str(fields_schema.get("description") or "").strip().lower()
    if not all(
        marker in description
        for marker in (
            "represents a json object",
            "unordered key-value map",
            "protojson format",
        )
    ):
        return None
    if "unordered map of dynamically typed values" not in fields_description:
        return None

    normalized = copy.deepcopy(schema)
    normalized.pop("properties", None)
    normalized.pop("required", None)
    normalized["additionalProperties"] = copy.deepcopy(additional)
    return normalized


def _explicit_dynamic_object_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return an explicitly open object only when it has no stable named outputs."""
    if str(schema.get("type") or "").strip().lower() != "object":
        return None
    if _has_stable_object_contract(schema):
        return None
    properties = schema.get("properties")
    if properties is not None and (not isinstance(properties, dict) or properties):
        return None
    if "additionalProperties" not in schema:
        return None
    additional = schema.get("additionalProperties")
    if additional is not True and not isinstance(additional, dict):
        return None
    return copy.deepcopy(schema)


def _has_stable_object_contract(schema: dict[str, Any]) -> bool:
    required = schema.get("required")
    if required not in (None, []):
        return True
    return not _MCP_STABLE_OBJECT_CONTRACT_KEYS.isdisjoint(schema)


def _project_mcp_output_contract(
    raw_schema: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Project dynamic MCP objects behind one stable, runtime-backed output."""
    schema = copy.deepcopy(raw_schema) if isinstance(raw_schema, dict) else {}
    dynamic_schema = _protobuf_struct_open_object_schema(schema)
    if dynamic_schema is None:
        dynamic_schema = _explicit_dynamic_object_schema(schema)
    if dynamic_schema is None:
        return schema, ""
    return (
        {
            "type": "object",
            "properties": {"result": dynamic_schema},
            "required": ["result"],
        },
        MCP_OUTPUT_PROJECTION_WHOLE_OBJECT_RESULT,
    )


def _service_scope(scope: ScopeContext | None) -> ScopeContext:
    return normalize_scope(scope)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _account_id_for_scope(scope: ScopeContext) -> str:
    return str(scope.account_id or "acc_default").strip() or "acc_default"


def _project_lineage_for_scope(scope: ScopeContext) -> tuple[str, str, str]:
    """Return authenticated connector lineage before repository access."""
    account_id = _account_id_for_scope(scope)
    project_id = str(scope.project_id or "").strip()
    tenant_id = str(scope.tenant_id or "").strip()
    if not project_id or not tenant_id:
        raise ServiceValidationError("MCP connector project scope is required")
    if scope.allowed_project_ids and project_id not in scope.allowed_project_ids:
        raise ServiceForbiddenError("Project access denied")
    return account_id, project_id, tenant_id


def _require_connector_mutation_access(scope: ScopeContext) -> None:
    principal_type = str(scope.principal_type or "").strip().lower()
    if not principal_type:
        return
    if principal_type not in _MCP_CONNECTOR_MUTATION_PRINCIPAL_TYPES:
        raise ServiceForbiddenError("Operator role required for MCP connector mutations")


def _normalize_auth_mode(value: str | None) -> str:
    normalized = str(value or "none").strip().lower() or "none"
    aliases = {
        "header": "api_key",
        "custom_header": "api_key",
        "apikey": "api_key",  # pragma: allowlist secret
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"none", "bearer", "api_key", "oauth2"}:
        raise ServiceValidationError("auth_mode must be one of: none, bearer, api_key, oauth2")
    return normalized


def _normalize_namespace_prefix(value: Any) -> str:
    return str(value or "").strip().strip(".")


def _owner_account_id(connector_row: dict[str, Any]) -> str:
    return str(connector_row.get("account_id") or "").strip()


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [item.strip() for item in value.split(",")]
    else:
        candidates = [str(item or "").strip() for item in (value)]
    normalized: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _normalize_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): value[key] for key in value}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return {str(key): parsed[key] for key in parsed}
    return {}


def _normalize_sync_status(value: Any) -> str:
    normalized = str(value or "never").strip().lower() or "never"
    return normalized if normalized in _MCP_CONNECTOR_SYNC_STATUS_VALUES else "never"


def _projected_tool_id_for_values(
    *,
    connector_id: str,
    namespace_prefix: str,
    remote_tool_name: str,
) -> str:
    prefix = _normalize_namespace_prefix(namespace_prefix)
    remote_name = str(remote_tool_name or "")
    if not remote_name.strip():
        return ""
    reserved_alias_prefix = "remote_unsafe_"
    is_safe_name = re.fullmatch(r"[A-Za-z0-9_.-]+", remote_name) is not None
    if is_safe_name and not remote_name.startswith(reserved_alias_prefix):
        local_name = remote_name
    else:
        domain = b"reserved-safe\0" if is_safe_name else b"unsafe\0"
        discriminator = "r" if is_safe_name else "u"
        digest = hashlib.sha256(domain + remote_name.encode("utf-8")).hexdigest()
        local_name = f"{reserved_alias_prefix}{discriminator}_{digest}"
    if prefix:
        return f"{prefix}.{local_name}"
    return f"mcp.{connector_id}.{local_name}"


def _projected_tool_id(connector_row: dict[str, Any], remote_tool_name: str) -> str:
    return _projected_tool_id_for_values(
        connector_id=str(connector_row.get("connector_id") or "").strip(),
        namespace_prefix=str(connector_row.get("namespace_prefix") or "").strip(),
        remote_tool_name=remote_tool_name,
    )


def _tool_parameter_type_from_schema(schema: dict[str, Any]) -> str:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next(
            (
                str(item).strip().lower()
                for item in schema_type
                if str(item).strip().lower() not in {"", "null"}
            ),
            "",
        )
    normalized = str(schema_type or "").strip().lower()
    if normalized in {"string", "integer", "number", "boolean", "array", "object"}:
        return normalized
    if schema.get("properties"):
        return "object"
    if schema.get("items"):
        return "array"
    return "json"


def _tool_parameters_from_schema(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = set(_normalize_string_list(input_schema.get("required")))
    parameters: list[dict[str, Any]] = []
    for name, raw_schema in properties.items():
        if not str(name or "").strip():
            continue
        schema = raw_schema if isinstance(raw_schema, dict) else {}
        parameters.append(
            {
                "name": str(name).strip(),
                "type": _tool_parameter_type_from_schema(schema),
                "required": str(name).strip() in required,
                "description": str(schema.get("description") or "").strip(),
                "default": schema.get("default"),
            }
        )
    return parameters


def _extract_tools_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ServiceValidationError("MCP tools/list returned a malformed result payload")
    raw_tools = result.get("tools")
    if not isinstance(raw_tools, list):
        raise ServiceValidationError(_MESSAGE_MCP_TOOLS_LIST_RETURNED_A_MALFORMED_TOOLS_PAGE)

    tools: list[dict[str, Any]] = []
    for item in raw_tools:
        if not isinstance(item, dict):
            raise ServiceValidationError(_MESSAGE_MCP_TOOLS_LIST_RETURNED_A_MALFORMED_TOOLS_PAGE)
        remote_tool_name = item.get("name")
        if not isinstance(remote_tool_name, str) or not remote_tool_name.strip():
            raise ServiceValidationError(_MESSAGE_MCP_TOOLS_LIST_RETURNED_A_MALFORMED_TOOLS_PAGE)
        raw_input_schema = item.get("inputSchema")
        if isinstance(raw_input_schema, dict):
            try:
                _mcp_header_annotations(raw_input_schema)
            except McpProtocolError as exc:
                logger.warning(
                    "Excluded MCP tool with invalid transport header annotations",
                    extra={
                        "mcp_tool_name": remote_tool_name,
                        "mcp_exclusion_reason": str(exc),
                    },
                )
                continue
        tools.append(copy.deepcopy(item))

    raw_next_cursor = result.get("nextCursor")
    if raw_next_cursor is None:
        return tools, None
    if not isinstance(raw_next_cursor, str):
        raise ServiceValidationError("MCP tools/list returned a malformed next cursor")
    return tools, raw_next_cursor


def _normalize_discovered_tools(
    connector_row: dict[str, Any],
    discovered_tools: Any,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(discovered_tools, list):
        return normalized
    seen_names: set[str] = set()
    for item in discovered_tools:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            remote_tool_name = raw_name
            input_schema = item.get("inputSchema")
            output_schema = item.get("outputSchema")
            display_name = str(item.get("title") or remote_tool_name).strip() or remote_tool_name
        else:
            remote_tool_name = str(item.get("remote_tool_name") or "")
            input_schema = item.get("input_schema")
            output_schema = item.get("output_schema")
            display_name = (
                str(item.get("display_name") or item.get("title") or remote_tool_name).strip()
                or remote_tool_name
            )
        if not remote_tool_name.strip() or remote_tool_name in seen_names:
            continue
        seen_names.add(remote_tool_name)
        annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
        normalized_item = copy.deepcopy(item)
        normalized_item.update(
            {
                "remote_tool_name": remote_tool_name,
                "projected_tool_id": _projected_tool_id(connector_row, remote_tool_name),
                "display_name": display_name,
                "input_schema": copy.deepcopy(input_schema)
                if isinstance(input_schema, dict)
                else {},
                "output_schema": copy.deepcopy(output_schema)
                if isinstance(output_schema, dict)
                else {},
                "annotations": copy.deepcopy(annotations),
            }
        )
        normalized_item.setdefault("description", "")
        normalized.append(normalized_item)
    return normalized


def _remap_allowlisted_tools_for_namespace(
    connector_row: dict[str, Any],
    *,
    namespace_prefix: str,
    discovered_tools: list[dict[str, Any]],
) -> list[str]:
    existing_allowlist = set(_normalize_string_list(connector_row.get("allowlisted_tools")))
    if not existing_allowlist:
        return []
    remapped: list[str] = []
    old_prefix = str(connector_row.get("namespace_prefix") or "").strip()
    connector_id = str(connector_row.get("connector_id") or "").strip()
    for tool in discovered_tools:
        remote_tool_name = str(tool.get("remote_tool_name") or "")
        if not remote_tool_name.strip():
            continue
        old_projected_tool_id = _projected_tool_id_for_values(
            connector_id=connector_id,
            namespace_prefix=old_prefix,
            remote_tool_name=remote_tool_name,
        )
        if old_projected_tool_id not in existing_allowlist:
            continue
        remapped_id = _projected_tool_id_for_values(
            connector_id=connector_id,
            namespace_prefix=namespace_prefix,
            remote_tool_name=remote_tool_name,
        )
        if remapped_id not in remapped:
            remapped.append(remapped_id)
    return remapped


def _connector_headers(row: dict[str, Any]) -> dict[str, str]:
    normalized = _normalize_json_object(row.get("headers"))
    return {
        str(key).strip(): str(value).strip()
        for key, value in normalized.items()
        if str(key).strip()
    }


def _maybe_registry_manager():
    try:
        from core.application.registry.registry_manager import RegistryManager

        return RegistryManager.instance()
    except Exception:
        return None


def _registry_tool_implementation(tool: dict[str, Any]) -> dict[str, Any]:
    implementation = tool.get("implementation") or {}
    if isinstance(implementation, str):
        try:
            implementation = json.loads(implementation)
        except Exception:
            implementation = {}
    return implementation if isinstance(implementation, dict) else {}


def _projected_tools_for_connector(connector_id: str) -> list[dict[str, Any]]:
    registry_manager = _maybe_registry_manager()
    db_store = getattr(registry_manager, "db_store", None) if registry_manager is not None else None
    if db_store is None or not hasattr(db_store, "list_tools"):
        return []

    tools = db_store.list_tools(enabled_only=False, include_trashed=False)
    projected: list[dict[str, Any]] = []
    for tool in tools:
        implementation = _registry_tool_implementation(tool)
        if (
            str(implementation.get("type") or implementation.get("kind") or "").strip() != "mcp"
            or str(implementation.get("connector_id") or "").strip() != connector_id
        ):
            continue
        projected.append(
            {
                "tool_id": str(tool.get("tool_id") or "").strip(),
                "display_name": str(tool.get("display_name") or tool.get("tool_id") or "").strip(),
                "enabled": bool(tool.get("enabled", True)),
                "remote_tool_name": str(
                    implementation.get("remote_tool_name")
                    or (
                        (tool.get("config") or {}).get("remote_tool_name")
                        if isinstance(tool.get("config"), dict)
                        else ""
                    )
                    or ""
                ).strip(),
            }
        )
    projected.sort(
        key=lambda row: (str(row.get("tool_id") or ""), str(row.get("display_name") or ""))
    )
    return projected


def _annotate_discovered_tool_lifecycle(
    connector_row: dict[str, Any],
    discovered_tools: list[dict[str, Any]],
    *,
    include_deleted_actor: bool = False,
) -> list[dict[str, Any]]:
    """Derive publication state without hiding a remote action from discovery."""
    registry_manager = _maybe_registry_manager()
    db_store = getattr(registry_manager, "db_store", None) if registry_manager is not None else None
    all_tools = (
        db_store.list_tools(enabled_only=False, include_trashed=True)
        if db_store is not None and hasattr(db_store, "list_tools")
        else []
    )
    rows_by_id = {
        str(tool.get("tool_id") or "").strip(): tool
        for tool in all_tools
        if isinstance(tool, dict) and str(tool.get("tool_id") or "").strip()
    }
    connector_id = str(connector_row.get("connector_id") or "").strip()
    account_id = str(connector_row.get("account_id") or "").strip()
    result: list[dict[str, Any]] = []
    for discovered in discovered_tools:
        payload = dict(discovered)
        projected_tool_id = str(payload.get("projected_tool_id") or "").strip()
        row = rows_by_id.get(projected_tool_id)
        unavailable = bool(payload.get("availability_reason_code")) and not bool(
            payload.get("can_publish", True)
        )
        state = (
            str(payload.get("lifecycle_state") or "unavailable")
            if unavailable
            else "available_to_add"
        )
        can_publish = not unavailable
        if row is not None:
            row_account_id = str(row.get("account_id") or account_id).strip()
            implementation = _registry_tool_implementation(row)
            same_owner = (
                row_account_id == account_id
                and str(implementation.get("connector_id") or "").strip() == connector_id
                and str(implementation.get("type") or implementation.get("kind") or "").strip()
                == "mcp"
            )
            if row.get("deleted_at") and same_owner:
                state = "in_trash"
                can_publish = False
                payload.update(
                    {
                        "availability_reason_code": "projected_tool_in_trash",
                        "availability_reason_text": (
                            "This action is in Trash and will not be republished by Sync. "
                            "Restore it from Trash or purge it there before adding it again."
                        ),
                        "recovery_action": "open_trash",
                        "trash_key": f"registry_tool:{projected_tool_id}",
                        "trash_entity_type": "registry_tool",
                        "deleted_at": str(row.get("deleted_at") or ""),
                    }
                )
                if include_deleted_actor and str(row.get("deleted_by") or "").strip():
                    payload["deleted_by"] = str(row.get("deleted_by") or "").strip()
            elif row.get("deleted_at") or not same_owner:
                state = "id_conflict"
                can_publish = False
                payload.update(
                    {
                        "availability_reason_code": "projected_tool_id_conflict",
                        "availability_reason_text": "This projected tool ID is already reserved.",
                    }
                )
            elif can_publish:
                state = "published"
        payload["lifecycle_state"] = state
        payload["can_publish"] = can_publish
        result.append(payload)
    return result


_MCP_CONNECTOR_VIEW_FIELDS = {
    "connector_id",
    "project_id",
    "display_name",
    "description",
    "transport",
    "base_url",
    "auth_mode",
    "namespace_prefix",
    "allowlisted_tools",
    "discovered_tools_jsonb",
    "projected_tools",
    "status",
    "auth_status",
    "oauth_status",
    "last_synced_at",
    "last_sync_status",
    "last_sync_error",
    "enabled",
    "created_at",
    "updated_at",
    "has_client_id",
    "has_client_secret",
    "oauth_credential_revision",
    "oauth_authorization_parameters",
    "oauth_requested_scopes",
    "oauth_refresh_token_required",
    "catalog_item_id",
    "catalog_remote_id",
    "catalog_item_revision",
}


def _sanitize_connector_row_for_view(
    row: dict[str, Any] | None,
    *,
    include_deleted_actor: bool = False,
) -> dict[str, Any]:
    payload = dict(row or {})
    payload["account_id"] = str(payload.get("account_id") or "")
    payload["project_id"] = str(payload.get("project_id") or "")
    payload["tenant_id"] = str(payload.get("tenant_id") or "")
    payload["allowlisted_tools"] = _normalize_string_list(payload.get("allowlisted_tools"))
    payload["last_sync_status"] = _normalize_sync_status(payload.get("last_sync_status"))
    payload["last_sync_error"] = str(payload.get("last_sync_error") or "").strip()
    payload["discovered_tools_jsonb"] = _annotate_discovered_tool_lifecycle(
        payload,
        _normalize_discovered_tools(
            payload,
            payload.get("discovered_tools_jsonb"),
        ),
        include_deleted_actor=include_deleted_actor,
    )
    payload["projected_tools"] = _projected_tools_for_connector(
        str(payload.get("connector_id") or "").strip()
    )
    oauth_metadata = _normalize_json_object(
        payload.get("oauth_metadata_jsonb") or payload.get("oauth_metadata")
    )
    discovery = _normalize_json_object(oauth_metadata.get("discovery"))
    registration = _normalize_json_object(oauth_metadata.get("registration"))
    token = _normalize_json_object(oauth_metadata.get("token"))
    payload["auth_status"] = str(payload.get("auth_status") or "not_configured").strip()
    secret_refs = _normalize_json_object(payload.get("oauth_secret_refs_jsonb")).get("refs")
    secret_refs = secret_refs if isinstance(secret_refs, dict) else {}
    payload["has_client_id"] = any(
        str(name or "").strip() in {"client_id", "_oauth_client_id"} for name in secret_refs
    )
    payload["has_client_secret"] = any(
        str(name or "").strip() in {"client_secret", "_oauth_client_secret"} for name in secret_refs
    )
    try:
        payload["oauth_credential_revision"] = max(
            1, int(payload.get("oauth_credential_revision") or 1)
        )
    except (TypeError, ValueError):
        payload["oauth_credential_revision"] = 1
    raw_authorization_parameters = oauth_metadata.get("authorization_parameters")
    authorization_parameters: dict[str, Any] = (
        raw_authorization_parameters if isinstance(raw_authorization_parameters, dict) else {}
    )
    payload["oauth_authorization_parameters"] = {
        str(key): str(value) for key, value in authorization_parameters.items()
    }
    payload["oauth_requested_scopes"] = _normalize_string_list(
        oauth_metadata.get("requested_scopes")
    )
    payload["oauth_refresh_token_required"] = bool(
        oauth_metadata.get("refresh_token_required", False)
    )
    payload["oauth_status"] = {
        "status": payload["auth_status"],
        "issuer": str(discovery.get("issuer") or "").strip(),
        "registration_mode": str(registration.get("mode") or "").strip(),
        "resource": str(discovery.get("resource") or "").strip(),
        "expires_at": token.get("expires_at"),
        "granted_scopes": _normalize_string_list(token.get("granted_scopes")),
        "connected_identity": str(payload.get("oauth_connected_identity") or "").strip(),
        "last_error": str(payload.get("oauth_last_error") or "").strip(),
    }
    return {key: payload.get(key) for key in _MCP_CONNECTOR_VIEW_FIELDS if key in payload}


def accessible_project_ids(list_projects: Callable[..., Any], *, scope: Any) -> list[str]:
    """Every project the caller may see, read page by page from the project list.

    The MCP page lists connectors across these when no single project is chosen;
    each project is then read with its own scope, so per-project access checks
    stay exactly those of a one-project request.
    """
    ids: list[str] = []
    page = 1
    while True:
        view = list_projects(page=page, per_page=200, scope=scope)
        ids.extend(str(project.id) for project in view.projects if str(project.id or "").strip())
        if page >= max(1, int(view.total_pages or 1)):
            return list(dict.fromkeys(ids))
        page += 1
