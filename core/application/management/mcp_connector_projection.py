"""MCP connector projection operations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, cast

from core.application.management.mcp_connector_common import (
    _MCP_PROJECTED_TOOL_CATEGORY,
    _MCP_PROJECTED_TOOL_TAGS,
    MCP_OUTPUT_PROJECTION_CONFIG_KEY,
    _maybe_registry_manager,
    _normalize_discovered_tools,
    _normalize_string_list,
    _owner_account_id,
    _project_mcp_output_contract,
    _projected_tools_for_connector,
    _registry_tool_implementation,
    _tool_parameters_from_schema,
)
from core.infrastructure.db.db_pool import (
    register_transaction_after_commit,
)

from .errors import (
    ServiceConflictError,
    ServiceUnavailableError,
    ServiceValidationError,
)


class McpConnectorProjectionMixin:
    """Projection of discovered tools into the shared registry."""

    def _build_projected_tool_manifest(
        self,
        connector_row: dict[str, Any],
        tool_definition: dict[str, Any],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        remote_tool_name = str(tool_definition.get("remote_tool_name") or "")
        projected_tool_id = str(tool_definition.get("projected_tool_id") or "").strip()
        display_name = (
            str(
                tool_definition.get("display_name") or remote_tool_name or projected_tool_id
            ).strip()
            or projected_tool_id
        )
        connector_display_name = str(connector_row.get("display_name") or "").strip()
        owner_account_id = _owner_account_id(connector_row)
        owner_project_id = str(connector_row.get("project_id") or "").strip()
        owner_tenant_id = str(connector_row.get("tenant_id") or "").strip()
        raw_input_schema = tool_definition.get("input_schema")
        input_schema: dict[str, Any] = (
            cast(dict[str, Any], raw_input_schema) if isinstance(raw_input_schema, dict) else {}
        )
        raw_output_schema = tool_definition.get("output_schema")
        output_schema: dict[str, Any] = (
            cast(dict[str, Any], raw_output_schema) if isinstance(raw_output_schema, dict) else {}
        )
        projected_output_schema, output_projection = _project_mcp_output_contract(output_schema)
        description = str(tool_definition.get("description") or "").strip()
        return {
            "tool_id": projected_tool_id,
            "account_id": owner_account_id,
            "project_id": owner_project_id,
            "tenant_id": owner_tenant_id,
            "scope_kind": "project",
            "version": "1.0.0",
            "display_name": f"{connector_display_name} / {display_name}"
            if connector_display_name
            else display_name,
            "description": description
            or f"Derived MCP action '{remote_tool_name}' from {connector_display_name or 'the connector'}.",
            "enabled": enabled,
            "category": _MCP_PROJECTED_TOOL_CATEGORY,
            "prompt": "",
            "interface": {
                "functions": [
                    {
                        "name": projected_tool_id,
                        "parameters": _tool_parameters_from_schema(input_schema),
                        "returns": projected_output_schema
                        or {"name": "result", "type": "json", "required": False},
                    }
                ]
            },
            "implementation": {
                "type": "mcp",
                "connector_id": str(connector_row.get("connector_id") or "").strip(),
                "remote_tool_name": remote_tool_name,
                "owner_account_id": owner_account_id,
                "owner_project_id": owner_project_id,
                "owner_tenant_id": owner_tenant_id,
            },
            "config": {
                "owner_account_id": owner_account_id,
                "owner_project_id": owner_project_id,
                "owner_tenant_id": owner_tenant_id,
                "connector_display_name": connector_display_name,
                "remote_tool_name": remote_tool_name,
                "input_schema": input_schema,
                "output_schema": output_schema,
                **(
                    {MCP_OUTPUT_PROJECTION_CONFIG_KEY: output_projection}
                    if output_projection
                    else {}
                ),
                "annotations": tool_definition.get("annotations")
                if isinstance(tool_definition.get("annotations"), dict)
                else {},
            },
            "tags": list(_MCP_PROJECTED_TOOL_TAGS),
            "builder_metadata_jsonb": {
                "purpose": f"Call the remote MCP action '{remote_tool_name}'.",
                "use_cases": [
                    "invoke an allowlisted remote MCP action",
                    "bridge a registry tool to a remote MCP server",
                ],
                "anti_use_cases": [
                    "store connector credentials in tool manifests",
                    "bypass MCP allowlist selection",
                ],
                "composition_role": "connector_action",
                "determinism_level": "remote",
                "safe_for_auto_use": False,
            },
        }

    def _sync_projected_registry_tools(
        self,
        connector_row: dict[str, Any],
    ) -> None:
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        if db_store is None or not hasattr(db_store, "save_tool"):
            raise ServiceUnavailableError("Registry store is not available for MCP tool projection")

        connector_id = str(connector_row.get("connector_id") or "").strip()
        allowlisted_tools = set(_normalize_string_list(connector_row.get("allowlisted_tools")))
        discovered_tools = _normalize_discovered_tools(
            connector_row,
            connector_row.get("discovered_tools_jsonb"),
        )
        discovered_by_projected_id = {
            str(tool.get("projected_tool_id") or "").strip(): tool
            for tool in discovered_tools
            if str(tool.get("projected_tool_id") or "").strip()
        }
        existing_tools = {
            str(tool.get("tool_id") or "").strip(): tool
            for tool in _projected_tools_for_connector(connector_id)
            if str(tool.get("tool_id") or "").strip()
        }

        tombstones_by_id = {
            str(tool.get("tool_id") or "").strip(): tool
            for tool in (db_store.list_tools(enabled_only=False, include_trashed=True) or [])
            if isinstance(tool, dict)
            and tool.get("deleted_at")
            and str(tool.get("tool_id") or "").strip() in allowlisted_tools
        }
        for projected_tool_id, tombstone in tombstones_by_id.items():
            implementation = _registry_tool_implementation(tombstone)
            same_owner = (
                str(implementation.get("connector_id") or "").strip() == connector_id
                and str(implementation.get("type") or implementation.get("kind") or "").strip()
                == "mcp"
                and str(
                    tombstone.get("account_id") or connector_row.get("account_id") or ""
                ).strip()
                == str(connector_row.get("account_id") or "").strip()
            )
            if same_owner:
                self._reject_trashed_projected_tool_publication(
                    connector_id=connector_id,
                    account_id=str(connector_row.get("account_id") or "").strip(),
                    tool_ids=[projected_tool_id],
                )
            raise ServiceConflictError(
                "A projected MCP tool ID is reserved and cannot be published",
                payload={
                    "error": "projected_tool_id_conflict",
                    "tool_ids": [projected_tool_id],
                },
            )

        self._reject_trashed_projected_tool_publication(
            connector_id=connector_id,
            account_id=str(connector_row.get("account_id") or "").strip(),
            tool_ids=sorted(allowlisted_tools),
        )

        for projected_tool_id in sorted(allowlisted_tools):
            tool_definition = discovered_by_projected_id.get(projected_tool_id)
            if tool_definition is None:
                raise ServiceValidationError(
                    f"allowlisted tool '{projected_tool_id}' is not part of the discovered MCP catalog"
                )
            existing_registry_tool = db_store.get_tool(projected_tool_id)
            if not existing_registry_tool:
                continue
            implementation = _registry_tool_implementation(existing_registry_tool)
            existing_connector_id = str(implementation.get("connector_id") or "").strip()
            implementation_type = str(
                implementation.get("type") or implementation.get("kind") or ""
            ).strip()
            if implementation_type == "mcp" and existing_connector_id == connector_id:
                continue
            if implementation_type == "mcp" and existing_connector_id:
                if not bool(existing_registry_tool.get("enabled", True)):
                    raise ServiceValidationError(
                        f"projected tool id '{projected_tool_id}' remains reserved by inactive MCP connector '{existing_connector_id}'"
                    )
                raise ServiceValidationError(
                    f"projected tool id '{projected_tool_id}' is already owned by MCP connector '{existing_connector_id}'"
                )
            raise ServiceValidationError(
                f"projected tool id '{projected_tool_id}' is already used by another registry tool"
            )

        for projected_tool_id in sorted(allowlisted_tools):
            tool_definition = discovered_by_projected_id.get(projected_tool_id)
            if tool_definition is None:
                raise ServiceValidationError(
                    f"allowlisted tool '{projected_tool_id}' is not part of the discovered MCP catalog"
                )
            db_store.save_tool(
                self._build_projected_tool_manifest(
                    connector_row,
                    tool_definition,
                    enabled=bool(connector_row.get("enabled", True)),
                ),
                author="system",
            )

        for projected_tool_id in sorted(existing_tools):
            if projected_tool_id in allowlisted_tools:
                continue
            existing_tool = db_store.get_tool(projected_tool_id)
            if not existing_tool:
                continue
            existing_tool["enabled"] = False
            db_store.save_tool(existing_tool, author="system")

    @contextmanager
    def _projected_registry_transaction(self, persistence: Any):
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        participate = getattr(db_store, "participate_in_external_transaction", None)
        if callable(participate) and callable(getattr(persistence, "cursor", None)):
            with participate(persistence):
                yield True
            return
        yield False

    @staticmethod
    def _publish_projected_registry_after_commit(
        persistence: Any,
        *,
        shares_connector_transaction: bool,
    ) -> None:
        registry_manager = _maybe_registry_manager()
        reload_all = getattr(registry_manager, "reload_all", None)
        if not callable(reload_all):
            return
        if shares_connector_transaction:
            register_transaction_after_commit(persistence, reload_all)
            return
        reload_all()

    def _trashed_projected_tool_ids(
        self,
        *,
        connector_id: str,
        account_id: str,
        tool_ids: list[str],
    ) -> set[str]:
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        if db_store is None or not hasattr(db_store, "list_tools"):
            return set()
        wanted = set(_normalize_string_list(tool_ids))
        trashed: set[str] = set()
        for row in db_store.list_tools(enabled_only=False, include_trashed=True) or []:
            tool_id = str(row.get("tool_id") or "").strip()
            if tool_id not in wanted or not row.get("deleted_at"):
                continue
            row_account_id = str(row.get("account_id") or account_id).strip()
            implementation = _registry_tool_implementation(row)
            if (
                row_account_id == account_id
                and str(implementation.get("connector_id") or "").strip() == connector_id
                and str(implementation.get("type") or implementation.get("kind") or "").strip()
                == "mcp"
            ):
                trashed.add(tool_id)
        return trashed

    def _reject_trashed_projected_tool_publication(
        self,
        *,
        connector_id: str,
        account_id: str,
        tool_ids: list[str],
    ) -> None:
        trashed = self._trashed_projected_tool_ids(
            connector_id=connector_id,
            account_id=account_id,
            tool_ids=tool_ids,
        )
        if not trashed:
            return
        raise ServiceConflictError(
            "A projected MCP tool is in Trash and cannot be published",
            payload={
                "error": "projected_tool_in_trash",
                "tool_ids": sorted(trashed),
                "recovery_action": "open_trash",
                "recovery_guidance": "Restore the MCP tool from Trash or purge it before adding it again.",
            },
        )

    def _snapshot_projected_registry_tools(
        self,
        current_connector_row: dict[str, Any],
        next_connector_row: dict[str, Any],
    ) -> dict[str, dict[str, Any] | None]:
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        if db_store is None or not hasattr(db_store, "get_tool"):
            return {}

        tool_ids: set[str] = set()
        connector_id = str(current_connector_row.get("connector_id") or "").strip()
        tool_ids.update(
            str(tool.get("tool_id") or "").strip()
            for tool in _projected_tools_for_connector(connector_id)
            if str(tool.get("tool_id") or "").strip()
        )
        for row in (current_connector_row, next_connector_row):
            discovered = _normalize_discovered_tools(row, row.get("discovered_tools_jsonb"))
            allowlisted = set(_normalize_string_list(row.get("allowlisted_tools")))
            for tool in discovered:
                projected_tool_id = str(tool.get("projected_tool_id") or "").strip()
                if projected_tool_id and projected_tool_id in allowlisted:
                    tool_ids.add(projected_tool_id)

        return {tool_id: db_store.get_tool(tool_id) for tool_id in sorted(tool_ids)}

    def _snapshot_registry_tool_ids(
        self,
        tool_ids: list[str],
    ) -> dict[str, dict[str, Any] | None]:
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        if db_store is None or not hasattr(db_store, "get_tool"):
            return {}
        return {tool_id: db_store.get_tool(tool_id) for tool_id in sorted(set(tool_ids))}

    def _restore_projected_registry_tools(
        self,
        snapshot: dict[str, dict[str, Any] | None],
    ) -> None:
        if not snapshot:
            return
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        if (
            db_store is None
            or not hasattr(db_store, "get_tool")
            or not hasattr(db_store, "save_tool")
        ):
            return

        for tool_id, row in snapshot.items():
            if row is None:
                current = db_store.get_tool(tool_id)
                if current is None:
                    continue
                if hasattr(db_store, "delete_tool"):
                    try:
                        db_store.delete_tool(tool_id, author="system")
                        continue
                    except Exception:
                        pass
                current["enabled"] = False
                db_store.save_tool(current, author="system")
                continue
            db_store.save_tool(row, author="system")

        if registry_manager is not None and hasattr(registry_manager, "reload_all"):
            registry_manager.reload_all()

    def _trash_projected_registry_tools(
        self,
        tool_ids: list[str],
        *,
        connector_id: str,
        account_id: str,
        project_id: str,
        tenant_id: str,
        reload_registry: bool = True,
    ) -> None:
        """Disable and soft-delete every live tool owned by a deleted connector."""
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        if db_store is None or not hasattr(db_store, "get_tool"):
            return

        disabled_tool_ids: list[str] = []
        for tool_id in sorted(set(tool_ids)):
            if not tool_id:
                continue
            existing = db_store.get_tool(tool_id)
            if not existing:
                continue
            implementation = _registry_tool_implementation(existing)
            if (
                str(
                    existing.get("account_id") or implementation.get("owner_account_id") or ""
                ).strip()
                != account_id
                or str(
                    existing.get("project_id") or implementation.get("owner_project_id") or ""
                ).strip()
                != project_id
                or str(implementation.get("type") or implementation.get("kind") or "").strip()
                != "mcp"
                or str(implementation.get("connector_id") or "").strip() != connector_id
                or str(implementation.get("owner_account_id") or "").strip() != account_id
                or str(implementation.get("owner_project_id") or "").strip() != project_id
                or str(implementation.get("owner_tenant_id") or "").strip() != tenant_id
            ):
                raise ServiceConflictError(
                    "MCP projected tool ownership changed; refresh the connector delete plan"
                )
            existing["enabled"] = False
            db_store.save_tool(existing, author="system")
            disabled_tool_ids.append(tool_id)

        if disabled_tool_ids:
            mark_unavailable = getattr(db_store, "mark_tool_source_unavailable", None)
            if callable(mark_unavailable):
                mark_unavailable(
                    disabled_tool_ids,
                    reason_code="source_connector_removed",
                    reason_text=(
                        f"The MCP connector '{connector_id}' that projected this tool was "
                        "deleted. The tool was moved to Trash and cannot run unless its source "
                        "is recreated and the projection is safely rebound."
                    ),
                )
            if not hasattr(db_store, "delete_tool"):
                raise ServiceUnavailableError("Registry tool deletion is unavailable")
            for tool_id in disabled_tool_ids:
                db_store.delete_tool(tool_id, author="system")

        if (
            reload_registry
            and registry_manager is not None
            and hasattr(registry_manager, "reload_all")
        ):
            registry_manager.reload_all()
