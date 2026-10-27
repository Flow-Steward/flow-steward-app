# ruff: noqa: F403, F405, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore
from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR


def _list_connector_catalog_rows(db, *, scope: Any) -> list[dict]:
    bl = _platform_logger_for_admin_catalog(db)
    if bl is None:
        return []
    try:
        from core.application.management.mcp_connector_services import McpConnectorQueryService

        payload = McpConnectorQueryService(get_logger=lambda: bl).list_connectors(scope=scope)
    except Exception as exc:
        logger.warning("connector catalog connectors query failed: %s", exc)
        return []

    rows: list[dict] = []
    for item in getattr(payload, "connectors", []) or []:
        if hasattr(item, "model_dump"):
            row = item.model_dump()
        elif isinstance(item, dict):
            row = dict(item)
        else:
            continue
        rows.append(row)
    return rows


def _tool_interface_functions(tool: dict[str, Any]) -> list[dict[str, Any]]:
    interface = _json_object(tool.get("interface"))
    functions = interface.get("functions")
    if isinstance(functions, list) and functions:
        return [dict(item) for item in functions if isinstance(item, dict)]
    parameters = interface.get("parameters")
    returns = interface.get("returns")
    if isinstance(parameters, list) or isinstance(returns, dict):
        return [
            {
                "name": str(tool.get("tool_id") or "invoke").strip() or "invoke",
                "description": str(tool.get("description") or "").strip(),
                "parameters": parameters if isinstance(parameters, list) else [],
                "returns": returns if isinstance(returns, dict) else {},
            }
        ]
    return []


def _connector_operations(
    connector: dict[str, Any],
    registry_operation_index: dict[str, dict[str, Any]],
    grouped_mcp_tool_ids: set[str],
) -> list[dict[str, Any]]:
    allowlisted_tools = [
        str(item or "").strip()
        for item in (connector.get("allowlisted_tools") or [])
        if str(item or "").strip()
    ]
    operations: list[dict[str, Any]] = []
    for op_id in allowlisted_tools:
        operation = dict(
            registry_operation_index.get(op_id)
            or {
                "operation_id": op_id,
                "display_name": op_id,
                "description": "",
                "parameters": [],
                "returns": {},
            }
        )
        projected_tool_id = str(operation.get("tool_id") or op_id).strip()
        operation["runtime_connector_id"] = (
            f"connector.{projected_tool_id}" if projected_tool_id else ""
        )
        if projected_tool_id:
            grouped_mcp_tool_ids.add(projected_tool_id)
        operations.append(operation)
    return operations


def _operation_display_name(tool: dict[str, Any], func: dict[str, Any], operation_id: str) -> str:
    implementation = _json_object(tool.get("implementation"))
    config = _json_object(tool.get("config"))
    is_mcp_action = (
        str(tool.get("category") or "").strip() == "mcp_tool_action"
        or str(implementation.get("type") or "").strip() == "mcp"
    )
    return str(
        func.get("display_name")
        or func.get("label")
        or (
            implementation.get("remote_tool_name") or config.get("remote_tool_name")
            if is_mcp_action
            else ""
        )
        or operation_id
    ).strip()


def _localized_text(
    messages: dict[str, str] | None, presentation: dict[str, Any], field: str, fallback: str
) -> str:
    """``presentation``'s ``field`` in the UI language, or ``fallback`` when it has none."""
    default = str(presentation.get(field) or "").strip()
    if not default:
        return fallback
    key = str(presentation.get("label_key" if field == "display_name" else f"{field}_key") or "")
    return str((messages or {}).get(key) or default).strip() or fallback


def _operation_row_from_tool_function(
    tool: dict[str, Any],
    func: dict[str, Any],
    *,
    messages: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One catalog operation; a built-in tool's name and description follow ``messages``."""
    from core.application.registry.builtin_standard_tools import standard_tool_presentation

    operation_id = str(func.get("name") or func.get("operation_id") or "").strip()
    if not operation_id:
        return {}
    parameters = func.get("parameters")
    returns = func.get("returns")
    tool_id = str(tool.get("tool_id") or "").strip()
    source = "standard_tool_action" if tool_id.startswith("std.") else "registry_tool"
    # A built-in operation is named by the code that ships it, not by its saved registry row.
    builtin = standard_tool_presentation(tool_id).get("operations", {}).get(operation_id, {})
    display_name = _localized_text(
        messages, builtin, "display_name", _operation_display_name(tool, func, operation_id)
    )
    description = _localized_text(
        messages, builtin, "description", str(func.get("description") or "").strip()
    )
    return {
        "operation_id": operation_id,
        "display_name": display_name or operation_id,
        "description": description,
        "parameters": [dict(item) for item in parameters if isinstance(item, dict)]
        if isinstance(parameters, list)
        else [],
        "returns": dict(returns) if isinstance(returns, dict) else {},
        "source": source,
        "category": "standard_tool_action"
        if source == "standard_tool_action"
        else str(tool.get("category") or "").strip(),
        "tool_id": tool_id,
        "runtime_connector_id": f"connector.{tool_id}" if tool_id else "",
        "tool_display_name": str(tool.get("display_name") or tool.get("tool_id") or "").strip(),
    }


def _enabled_registry_tools(db) -> list[dict[str, Any]]:
    list_tools = getattr(db, "list_tools", None)
    if not callable(list_tools):
        return []
    try:
        tools = list_tools(enabled_only=True) or []
    except Exception as exc:
        logger.warning("connector catalog registry tool query failed: %s", exc)
        return []
    if not isinstance(tools, list):
        return []

    return [
        tool for tool in tools if isinstance(tool, dict) and str(tool.get("tool_id") or "").strip()
    ]


def _registry_tool_operation_index(db) -> dict[str, dict[str, Any]]:
    tools = _enabled_registry_tools(db)

    index: dict[str, dict[str, Any]] = {}
    for tool in tools:
        tool_id = str(tool.get("tool_id") or "").strip()
        for func in _tool_interface_functions(tool):
            operation = _operation_row_from_tool_function(tool, func)
            operation_id = str(operation.get("operation_id") or "").strip()
            if not operation_id:
                continue
            index.setdefault(operation_id, operation)
            index.setdefault(f"{tool_id}.{operation_id}", operation)
            index.setdefault(f"connector.{tool_id}.{operation_id}", operation)
    return index


def _builtin_tool_text(
    messages: dict[str, str] | None, tool: dict[str, Any], field: str, fallback: str
) -> str:
    """A built-in tool's name or description in the UI language, unless an admin changed it."""
    from core.application.registry.builtin_standard_tools import standard_tool_presentation

    presentation = standard_tool_presentation(str(tool.get("tool_id") or ""))
    if str(presentation.get(field) or "").strip() != fallback:
        return fallback
    return _localized_text(messages, presentation, field, fallback)


def _registry_tool_connector_rows(db, *, locale: str = "") -> list[dict[str, Any]]:
    """Enabled registry tools as catalog rows; built-in names follow ``locale``."""
    from core.application.settings.ui_i18n import ui_messages_for_locale

    tools = _enabled_registry_tools(db)
    messages = ui_messages_for_locale(locale) if tools else {}

    rows: list[dict[str, Any]] = []
    for tool in tools:
        tool_id = str(tool.get("tool_id") or "").strip()
        operations = [
            operation
            for operation in (
                _operation_row_from_tool_function(tool, func, messages=messages)
                for func in _tool_interface_functions(tool)
            )
            if operation
        ]
        if not operations:
            continue
        source = "standard_tool_action" if tool_id.startswith("std.") else "registry_tool"
        display_name = str(tool.get("display_name") or tool_id).strip() or tool_id
        rows.append(
            {
                "connector_id": f"connector.{tool_id}",
                "display_name": _builtin_tool_text(messages, tool, "display_name", display_name),
                "description": _builtin_tool_text(
                    messages, tool, "description", str(tool.get("description") or "").strip()
                ),
                "namespace_prefix": tool_id,
                "enabled": bool(tool.get("enabled", True)),
                "status": source,
                "connector_install_id": "",
                "source": source,
                "category": "standard_tool_action"
                if source == "standard_tool_action"
                else str(tool.get("category") or "").strip(),
                "tool_id": tool_id,
                "tool_url": f"/registry/tools/{tool_id}",
                "operations": operations,
                "connections": [],
            }
        )
    return rows


def _active_extension_install_index_for_catalog(bl: Any, *, scope: Any) -> dict[str, Any]:
    account_id = str(getattr(scope, "account_id", "") or DEFAULT_ACCOUNT_ID).strip()
    if not account_id:
        account_id = DEFAULT_ACCOUNT_ID
    conn = getattr(bl, "conn", None)
    if conn is None:
        return {}
    try:
        from core.application.extensions.install import DBExtensionInstallStore

        records = DBExtensionInstallStore(conn).list_for_account(
            account_id,
            include_global=True,
            include_inactive_owned=False,
        )
    except Exception as exc:
        logger.warning("connector catalog extension install query failed: %s", exc)
        return {}
    result: dict[str, Any] = {}
    for record in sorted(
        records or [],
        key=lambda row: (
            str(getattr(row, "account_id", "") or "").strip() != account_id,
            str(getattr(row, "install_id", "") or ""),
        ),
    ):
        extension_id = str(getattr(record, "extension_id", "") or "").strip()
        if extension_id and extension_id not in result:
            result[extension_id] = record
    return result


def _extension_catalog_manifest_for_install(
    bl: Any,
    *,
    install_record: Any,
) -> dict[str, Any]:
    release_id = str(getattr(install_record, "release_id", "") or "").strip()
    conn = getattr(bl, "conn", None)
    if not release_id or conn is None:
        return {}
    try:
        release = DBExtensionRuntimeStore(conn).get_release(release_id)
    except Exception as exc:
        logger.warning("connector catalog extension release query failed: %s", exc)
        return {}
    if release is None:
        return {}
    descriptor = dict(getattr(release, "descriptor", {}) or {})
    release_extension_id = str(getattr(release, "extension_id", "") or "").strip()
    install_extension_id = str(getattr(install_record, "extension_id", "") or "").strip()
    if release_extension_id != install_extension_id:
        return {}
    return descriptor


def _extension_action_operations(
    extension_id: str,
    action_definitions: Any,
    operation_manifest: Any = None,
    *,
    locale: str = "",
    locale_bundles: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    from core.application.extensions.workflow_install_contract import (
        extension_action_operations,
    )

    return extension_action_operations(
        extension_id,
        action_definitions,
        operation_manifest,
        locale=locale,
        locale_bundles=locale_bundles,
    )


def _release_locale_bundles(compiled: dict[str, Any]) -> dict[str, dict[str, str]]:
    bundles = compiled.get("locale_bundles")
    return bundles if isinstance(bundles, dict) else {}


def _extension_scoped_connections(
    connections: list[dict[str, Any]],
    extension_id: str,
    install_record: Any,
) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in connection.items() if key != "_refs"}
        for connection in (
            {
                **dict(item),
                "_refs": _connection_ref_set(dict(item)),
            }
            for item in connections
            if isinstance(item, dict)
        )
        if extension_id in connection.get("_refs", set())
        and bool(connection.get("enabled", True))
        and not (
            str(connection.get("extension_install_id") or "").strip()
            and str(connection.get("extension_install_id") or "").strip()
            != str(getattr(install_record, "install_id", "") or "").strip()
        )
    ]


def _extension_action_connector_rows(
    db,
    *,
    project_id: Any,
    scope: Any,
    locale: str = "",
) -> list[dict[str, Any]]:
    """Project active extension actions into the workflow connector catalog.

    Names and descriptions follow ``locale`` through the dictionaries of the
    release each install pins, never another version's.
    """
    from core.application.extensions.display_labels import localized_extension_display_name
    from core.application.extensions.extension_locales import resolve_locale_text

    bl = _platform_logger_for_admin_catalog(db)
    if bl is None:
        return []
    active_installs = _active_extension_install_index_for_catalog(bl, scope=scope)
    if not active_installs:
        return []
    connections = _list_project_connections_for_catalog(db, project_id=project_id)
    rows: list[dict[str, Any]] = []
    for extension_id, install_record in active_installs.items():
        compiled = _extension_catalog_manifest_for_install(
            bl,
            install_record=install_record,
        )
        if not compiled:
            continue
        action_definitions = compiled.get("action_definitions")
        locale_bundles = _release_locale_bundles(compiled)
        operations = _extension_action_operations(
            extension_id,
            action_definitions,
            compiled.get("operation_manifest"),
            locale=locale,
            locale_bundles=locale_bundles,
        )
        if not operations:
            continue
        scoped_connections = _extension_scoped_connections(
            connections, extension_id, install_record
        )
        rows.append(
            {
                "connector_id": f"extension.{extension_id}",
                "display_name": localized_extension_display_name(compiled, locale=locale),
                "description": resolve_locale_text(
                    locale_bundles,
                    locale,
                    "ext.description",
                    str(compiled.get("description") or "").strip(),
                ),
                "namespace_prefix": extension_id,
                "enabled": True,
                "status": "extension",
                "connector_install_id": str(
                    getattr(install_record, "install_id", "") or ""
                ).strip(),
                "source": "extension",
                "category": "extension_action",
                "extension_id": extension_id,
                "extension_install_id": str(
                    getattr(install_record, "install_id", "") or ""
                ).strip(),
                "operations": operations,
                "connections": scoped_connections,
            }
        )
    return rows


def _list_project_connections_for_catalog(
    db,
    *,
    project_id: Any,
    enabled_only: bool = True,
) -> list[dict]:
    normalized_project_id = _normalize_optional_entity_id(project_id)
    if not normalized_project_id:
        return []
    list_connections = getattr(db, "list_project_connections", None)
    if callable(list_connections):
        try:
            rows = list_connections(normalized_project_id, enabled_only=enabled_only) or []
            return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception as exc:
            logger.warning("connector catalog project connection query failed: %s", exc)
            return []
    bl = _platform_logger_for_admin_catalog(db)
    if bl is None or not callable(getattr(bl, "list_project_connections", None)):
        return []
    try:
        rows = bl.list_project_connections(normalized_project_id, enabled_only=enabled_only) or []
    except Exception as exc:
        logger.warning("connector catalog project connection query failed: %s", exc)
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _connector_ref_set(connector_row: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    connector_id = str(connector_row.get("connector_id") or "").strip()
    namespace_prefix = str(connector_row.get("namespace_prefix") or "").strip()
    if connector_id:
        refs.add(connector_id)
    if namespace_prefix:
        refs.add(namespace_prefix)
    return refs


def _reference_values(row: dict[str, Any], keys: tuple[str, ...]) -> set[str]:
    return {str(row.get(key) or "").strip() for key in keys if str(row.get(key) or "").strip()}


def _connection_ref_set(connection_row: dict[str, Any]) -> set[str]:
    refs = _reference_values(
        connection_row, ("connection_id", "extension_id", "extension_install_id", "connection_type")
    )
    refs.update(
        _reference_values(
            _json_object(connection_row.get("connection_config")),
            (
                "connector_id",
                "namespace_prefix",
                "provider_key",
                "extension_id",
                "extension_install_id",
            ),
        )
    )
    for route in connection_row.get("connection_routes") or []:
        if isinstance(route, dict):
            refs.update(
                _reference_values(route, ("extension_id", "extension_install_id", "provider_key"))
            )
    return refs


def _connection_matches_connector(
    connector_refs: set[str],
    connection_refs: set[str],
) -> bool:
    if not connector_refs or not connection_refs:
        return False
    return bool(connector_refs.intersection(connection_refs))


__all__ = [
    "_connector_operations",
    "_list_connector_catalog_rows",
    "_tool_interface_functions",
    "_operation_row_from_tool_function",
    "_registry_tool_operation_index",
    "_registry_tool_connector_rows",
    "_active_extension_install_index_for_catalog",
    "_extension_catalog_manifest_for_install",
    "_extension_action_connector_rows",
    "_list_project_connections_for_catalog",
    "_connector_ref_set",
    "_connection_ref_set",
    "_connection_matches_connector",
]
