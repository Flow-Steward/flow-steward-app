"""MCP connector deletion operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from core.application.management.mcp_connector_common import (
    DATABASE_NOT_CONFIGURED_MESSAGE,
    _maybe_registry_manager,
    _normalize_discovered_tools,
    _normalize_string_list,
    _project_lineage_for_scope,
    _registry_tool_implementation,
    _require_connector_mutation_access,
    _service_scope,
)
from core.application.management.mcp_connector_header_secrets import (
    delete_connector_header_secrets,
)
from core.application.management.scope import ScopeContext
from core.application.registry.active_workflow_dependencies import (
    lock_workflow_dependency_inventory,
    workflow_dependency_impacts,
)

from .errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)

_MESSAGE_MCP_CONNECTOR_DELETION_IMPACT_CHANGED_REFRESH_THE_PLAN_BEFORE_CONFI = (
    "MCP connector deletion impact changed; refresh the plan before confirming"
)


class McpConnectorDeletionMixin:
    """Confirmed connector deletion and dependency-impact validation."""

    _get_connector_row: Callable[..., Any]
    _get_logger: Callable[..., Any]
    _restore_projected_registry_tools: Callable[..., Any]
    _snapshot_registry_tool_ids: Callable[..., Any]
    _soft_delete_connector_row: Callable[..., Any]
    _trash_projected_registry_tools: Callable[..., Any]

    def preview_delete_connector(
        self,
        connector_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        scope = _service_scope(scope)
        _require_connector_mutation_access(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        existing = self._get_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if not existing:
            raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
        return self._build_delete_plan(existing, account_id=account_id)

    def delete_connector(
        self,
        connector_id: str,
        *,
        plan_revision: str = "",
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        scope = _service_scope(scope)
        _require_connector_mutation_access(scope)
        account_id, project_id, tenant_id = _project_lineage_for_scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        existing = self._get_connector_row(
            bl,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        if not existing:
            raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
        normalized_revision = str(plan_revision or "").strip()
        if not normalized_revision:
            raise ServiceValidationError(
                "MCP connector deletion requires a confirmed impact plan revision"
            )

        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        if db_store is not None and hasattr(db_store, "_transaction"):
            return self._delete_connector_atomic(
                bl,
                existing=existing,
                connector_id=connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
                normalized_revision=normalized_revision,
                deleted_by=scope.actor_name,
                db_store=db_store,
            )

        revoke_after_commit: dict[str, Any] | None = None
        with self._delete_plan_inventory_lock(bl):
            locked_existing = self._get_connector_row(
                bl,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            if not locked_existing:
                raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
            current_plan = self._build_delete_plan(locked_existing, account_id=account_id)
            if str(current_plan.get("revision") or "") != normalized_revision:
                raise ServiceConflictError(
                    _MESSAGE_MCP_CONNECTOR_DELETION_IMPACT_CHANGED_REFRESH_THE_PLAN_BEFORE_CONFI,
                    payload={"stale_plan": True, "current_plan": current_plan},
                )

            planned_tool_ids = [
                str(tool.get("tool_id") or "").strip()
                for tool in (current_plan.get("remove") or {}).get("tools", [])
                if isinstance(tool, dict) and str(tool.get("tool_id") or "").strip()
            ]
            snapshot = self._snapshot_registry_tool_ids(planned_tool_ids)
            try:
                self._trash_projected_registry_tools(
                    planned_tool_ids,
                    connector_id=connector_id,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                )
                retained_oauth_metadata: dict[str, Any] = {}
                if str(locked_existing.get("auth_mode") or "").strip().lower() == "oauth2":
                    from core.application.auth.mcp_connector_oauth import (
                        prepare_mcp_connector_oauth_delete_locked,
                    )

                    revoke_after_commit, retained_oauth_metadata = (
                        prepare_mcp_connector_oauth_delete_locked(
                            bl,
                            connector_row=dict(locked_existing),
                        )
                    )
                self._soft_delete_connector_row(
                    bl,
                    connector_id,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    deleted_by=scope.actor_name,
                    child_tool_ids=planned_tool_ids,
                    oauth_metadata=retained_oauth_metadata,
                )
            except Exception:
                self._restore_projected_registry_tools(snapshot)
                raise
        if revoke_after_commit is not None:
            from core.application.auth.mcp_connector_oauth import (
                execute_prepared_mcp_connector_revocation,
            )

            execute_prepared_mcp_connector_revocation(revoke_after_commit)
        return {"status": "trashed", "connector_id": connector_id}

    def _delete_connector_atomic(
        self,
        bl: Any,
        *,
        existing: dict[str, Any],
        connector_id: str,
        account_id: str,
        project_id: str,
        tenant_id: str,
        normalized_revision: str,
        deleted_by: str,
        db_store: Any,
    ) -> dict[str, Any]:
        del bl
        expected_plan = self._build_delete_plan(existing, account_id=account_id)
        if str(expected_plan.get("revision") or "") != normalized_revision:
            raise ServiceConflictError(
                _MESSAGE_MCP_CONNECTOR_DELETION_IMPACT_CHANGED_REFRESH_THE_PLAN_BEFORE_CONFI,
                payload={"stale_plan": True, "current_plan": expected_plan},
            )
        expected_allowlist = _normalize_string_list(existing.get("allowlisted_tools"))
        expected_discovered_ids = sorted(
            str(tool.get("projected_tool_id") or "").strip()
            for tool in _normalize_discovered_tools(
                existing,
                existing.get("discovered_tools_jsonb"),
            )
            if str(tool.get("projected_tool_id") or "").strip()
        )
        revoke_after_commit: dict[str, Any] | None = None
        with db_store._transaction() as cur:
            lock_workflow_dependency_inventory(cur)
            cur.execute(
                "SELECT * FROM mcp_connectors WHERE connector_id = %s AND account_id = %s AND project_id = %s AND tenant_id = %s AND deleted_at IS NULL FOR UPDATE",
                (connector_id, account_id, project_id, tenant_id),
            )
            locked_existing = cur.fetchone()
            if not locked_existing:
                raise ServiceNotFoundError(f"MCP connector '{connector_id}' not found")
            locked_row = dict(locked_existing)
            current_plan = self._build_delete_plan(locked_row, account_id=account_id)
            current_discovered_ids = sorted(
                str(tool.get("projected_tool_id") or "").strip()
                for tool in _normalize_discovered_tools(
                    locked_row,
                    locked_row.get("discovered_tools_jsonb"),
                )
                if str(tool.get("projected_tool_id") or "").strip()
            )
            if (
                (current_plan.get("remove") or {}) != (expected_plan.get("remove") or {})
                or (current_plan.get("affected") or {}) != (expected_plan.get("affected") or {})
                or _normalize_string_list(locked_row.get("allowlisted_tools")) != expected_allowlist
                or current_discovered_ids != expected_discovered_ids
            ):
                raise ServiceConflictError(
                    _MESSAGE_MCP_CONNECTOR_DELETION_IMPACT_CHANGED_REFRESH_THE_PLAN_BEFORE_CONFI,
                    payload={"stale_plan": True, "current_plan": current_plan},
                )
            planned_tool_ids = [
                str(tool.get("tool_id") or "").strip()
                for tool in (current_plan.get("remove") or {}).get("tools", [])
                if isinstance(tool, dict) and str(tool.get("tool_id") or "").strip()
            ]
            self._trash_projected_registry_tools(
                planned_tool_ids,
                connector_id=connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
                reload_registry=False,
            )
            retained_oauth_metadata: dict[str, Any] = {}
            if str(locked_row.get("auth_mode") or "").strip().lower() == "oauth2":
                from core.application.auth.mcp_connector_oauth import (
                    prepare_mcp_connector_oauth_delete_locked,
                )

                revoke_after_commit, retained_oauth_metadata = (
                    prepare_mcp_connector_oauth_delete_locked(
                        cur.connection,
                        connector_row=locked_row,
                    )
                )
            else:
                cur.execute(
                    "DELETE FROM mcp_connector_oauth_states WHERE connector_id = %s AND account_id = %s AND project_id = %s AND tenant_id = %s",
                    (connector_id, account_id, project_id, tenant_id),
                )
            delete_connector_header_secrets(
                cur.connection,
                connector_id=connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            cur.execute(
                """UPDATE mcp_connectors
                      SET enabled = false, status = 'offline',
                          auth_status = CASE
                              WHEN auth_mode = 'none' THEN 'not_configured'
                              ELSE 'auth_required'
                          END,
                          header_secret_refs_jsonb = '{}'::jsonb, oauth_metadata_jsonb = %s::jsonb,
                          oauth_secret_refs_jsonb = '{}'::jsonb,
                          oauth_connected_identity = '', oauth_last_error = '',
                          last_synced_at = NULL, last_sync_status = 'never',
                          last_sync_error = CASE
                              WHEN auth_mode = 'none'
                                  THEN 'Health check and sync required after restore'
                              ELSE 'Reconnect and sync required after restore'
                          END,
                          oauth_credential_revision = oauth_credential_revision + 1,
                          lifecycle_child_tool_ids_jsonb = %s::jsonb,
                          deleted_at = now(), deleted_by = %s, updated_at = now()
                    WHERE connector_id = %s AND account_id = %s AND project_id = %s AND tenant_id = %s AND deleted_at IS NULL""",
                (
                    json.dumps(retained_oauth_metadata, sort_keys=True),
                    json.dumps(planned_tool_ids),
                    deleted_by,
                    connector_id,
                    account_id,
                    project_id,
                    tenant_id,
                ),
            )
            if not cur.rowcount:
                raise ServiceConflictError("MCP connector deletion plan is stale")
        if revoke_after_commit is not None:
            from core.application.auth.mcp_connector_oauth import (
                execute_prepared_mcp_connector_revocation,
            )

            execute_prepared_mcp_connector_revocation(revoke_after_commit)
        registry_manager = _maybe_registry_manager()
        if registry_manager is not None and hasattr(registry_manager, "reload_all"):
            registry_manager.reload_all()
        return {"status": "trashed", "connector_id": connector_id}

    @contextmanager
    def _delete_plan_inventory_lock(self, bl: Any):
        conn = getattr(bl, "conn", None)
        transaction = getattr(conn, "transaction", None)
        if not callable(transaction) or not hasattr(bl, "cursor"):
            yield
            return
        with transaction():
            with bl.cursor() as cur:
                cur.execute(
                    "LOCK TABLE mcp_connectors, registry_tools, registry_agent_tools, "
                    "registry_workflows, registry_workflow_groups, registry_workflow_steps "
                    "IN SHARE ROW EXCLUSIVE MODE"
                )
            yield

    def _build_delete_plan(
        self,
        connector_row: dict[str, Any],
        *,
        account_id: str,
    ) -> dict[str, Any]:
        connector_id = str(connector_row.get("connector_id") or "").strip()
        project_id = str(connector_row.get("project_id") or "").strip()
        tenant_id = str(connector_row.get("tenant_id") or "").strip()
        registry_manager = _maybe_registry_manager()
        db_store = (
            getattr(registry_manager, "db_store", None) if registry_manager is not None else None
        )
        required_inventory_methods = ("list_tools", "list_workflows", "get_agents_using_tool")
        if db_store is None or any(
            not hasattr(db_store, method_name) for method_name in required_inventory_methods
        ):
            raise ServiceUnavailableError("MCP connector delete impact inventory is unavailable")
        tool_rows: list[dict[str, Any]] = []
        for tool in db_store.list_tools(enabled_only=False, include_trashed=True) or []:
            if not isinstance(tool, dict):
                continue
            implementation = _registry_tool_implementation(tool)
            if (
                str(implementation.get("type") or implementation.get("kind") or "").strip() != "mcp"
                or str(implementation.get("connector_id") or "").strip() != connector_id
            ):
                continue
            tool_account_id = str(
                tool.get("account_id") or implementation.get("owner_account_id") or ""
            ).strip()
            tool_project_id = str(
                tool.get("project_id") or implementation.get("owner_project_id") or ""
            ).strip()
            if (
                tool_account_id != account_id
                or tool_project_id != project_id
                or str(implementation.get("owner_account_id") or "").strip() != account_id
                or str(implementation.get("owner_project_id") or "").strip() != project_id
                or str(implementation.get("owner_tenant_id") or "").strip() != tenant_id
            ):
                continue
            tool_id = str(tool.get("tool_id") or "").strip()
            if tool_id:
                tool_rows.append(tool)
        tool_rows.sort(key=lambda row: str(row.get("tool_id") or ""))
        tool_agent_ids = {
            str(tool.get("tool_id") or "").strip(): set(
                db_store.get_agents_using_tool(str(tool.get("tool_id") or "").strip()) or []
            )
            for tool in tool_rows
        }
        workflow_rows = workflow_dependency_impacts(
            db_store,
            account_id=account_id,
            connector_id=connector_id,
            tool_agent_ids=tool_agent_ids,
        )
        workflow_ids_by_tool: dict[str, list[str]] = {
            tool_id: sorted(
                str(workflow.get("workflow_id") or "")
                for workflow in workflow_rows
                if tool_id in set(workflow.get("tool_ids") or [])
            )
            for tool_id in sorted(tool_agent_ids)
        }
        tools = [
            {
                "tool_id": str(tool.get("tool_id") or "").strip(),
                "display_name": str(tool.get("display_name") or tool.get("tool_id") or "").strip(),
                "affected_workflow_ids": workflow_ids_by_tool.get(
                    str(tool.get("tool_id") or "").strip(), []
                ),
            }
            for tool in tool_rows
        ]
        revision_payload = {
            "connector": {
                key: connector_row.get(key)
                for key in (
                    "connector_id",
                    "account_id",
                    "project_id",
                    "tenant_id",
                    "display_name",
                    "enabled",
                    "status",
                    "auth_status",
                    "updated_at",
                )
            },
            "allowlisted_tools": sorted(
                str(tool_id).strip()
                for tool_id in (connector_row.get("allowlisted_tools") or [])
                if str(tool_id).strip()
            ),
            "discovered_projected_tool_ids": sorted(
                str(tool.get("projected_tool_id") or "").strip()
                for tool in _normalize_discovered_tools(
                    connector_row,
                    connector_row.get("discovered_tools_jsonb"),
                )
                if str(tool.get("projected_tool_id") or "").strip()
            ),
            "tool_ids": [tool["tool_id"] for tool in tools],
            "workflows": workflow_rows,
        }
        revision = hashlib.sha256(
            json.dumps(
                revision_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "connector_id": connector_id,
            "connector": {
                "display_name": str(connector_row.get("display_name") or connector_id).strip(),
                "enabled": bool(connector_row.get("enabled", True)),
                "status": str(connector_row.get("status") or "unchecked").strip(),
            },
            "remove": {"tools": tools},
            "affected": {"workflows": workflow_rows},
            "warnings": [
                "Affected workflow definitions are retained but cannot validate or start until each missing tool is restored or replaced.",
                "Running workflows are not cancelled automatically; a later tool call may fail.",
            ],
            "revision": revision,
        }
