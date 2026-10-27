"""Plans operations for the composed trash lifecycle service."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from core.application.management.command_services_common import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

_MESSAGE_MCP_CONNECTOR_NOT_FOUND = "mcp_connector not found"
_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE = "MCP connector lifecycle plan is stale"


class TrashLifecycleServicePlansMixin:
    # Dependencies provided by the owning composed lifecycle service.
    _build_project_plan_root: Callable[..., Any]
    _build_registry_workflow_plan_root: Callable[..., Any]
    _build_workspace_plan_root: Callable[..., Any]
    _collect_direct_dependency_conflicts: Callable[..., Any]
    _collect_selectable_nodes: Callable[..., Any]
    _collect_selected_node_ids: Callable[..., Any]
    _lifecycle_plan_version: Callable[..., Any]
    _summarize_tree: Callable[..., Any]

    def _build_lifecycle_plan(
        self,
        bl: Any,
        *,
        entity_type: str,
        entity_id: str,
        action_type: str,
        scope: ScopeContext,
    ) -> dict[str, Any]:
        if entity_type == "mcp_connector":
            return self._build_mcp_connector_lifecycle_plan(
                bl,
                connector_id=entity_id,
                action_type=action_type,
                scope=scope,
            )
        if entity_type not in {"workspace", "project", "registry_workflow"}:
            raise ServiceValidationError(
                "Lifecycle planning is currently available for workspaces, projects, and registry workflows."
            )
        if entity_type == "registry_workflow":
            root = self._build_registry_workflow_plan_root(
                bl,
                workflow_id=entity_id,
                account_id=str(scope.account_id or ""),
            )
        else:
            root = (
                self._build_workspace_plan_root(
                    bl,
                    workspace_id=entity_id,
                    action_type=action_type,
                    scope=scope,
                )
                if entity_type == "workspace"
                else self._build_project_plan_root(
                    bl, project_id=entity_id, action_type=action_type
                )
            )
        warnings: list[str] = []
        validation_errors: list[str] = []
        root_status = str(root.get("status") or "").strip().lower()
        if action_type in {"restore", "hard_delete"} and root_status != "trashed":
            validation_errors.append(
                f"{entity_type} must be trashed before {action_type.replace('_', ' ')}."
            )
        if bool((root.get("metadata") or {}).get("scope_filtered")):
            warnings.append(
                "This workspace contains projects outside your project scope. Workspace-level selection is disabled; only visible project subtrees can be planned."
            )
        if (
            entity_type == "project"
            and action_type == "restore"
            and bool((root.get("metadata") or {}).get("parent_workspace_trashed"))
        ):
            workspace_label = str(
                (root.get("metadata") or {}).get("parent_workspace_label") or "its workspace"
            ).strip()
            warnings.append(
                f"Restoring this project will also restore workspace '{workspace_label}' so the project is visible again."
            )
        selectable_nodes = self._collect_selectable_nodes(root)
        summary = self._summarize_tree(root)
        cleanup_groups = (
            [
                {"group_key": "memory_scope", "label": "Memory scope"},
                {"group_key": "vector_scope", "label": "Vector scope"},
                {"group_key": "jobs_runtime", "label": "Jobs runtime data"},
                {"group_key": "logs_and_events", "label": "Logs / events / session leftovers"},
            ]
            if action_type == "hard_delete"
            else []
        )
        default_selected = self._collect_selected_node_ids(root)
        direct_dependency_conflicts = self._collect_direct_dependency_conflicts(
            bl,
            root,
            action_type=action_type,
            selected_node_ids=default_selected,
        )
        return {
            "action_type": action_type,
            "plan_version": self._lifecycle_plan_version(
                bl,
                root,
                action_type=action_type,
            ),
            "root_entity": {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "label": root.get("label") or entity_id,
            },
            "tree": root,
            "impact_summary": summary,
            "auto_managed_cleanup_groups": cleanup_groups,
            "selectable_product_nodes": selectable_nodes,
            "warnings": warnings,
            "direct_dependency_conflicts": direct_dependency_conflicts,
            "validation_errors": validation_errors,
        }

    def _build_mcp_connector_lifecycle_plan(
        self,
        bl: Any,
        *,
        connector_id: str,
        action_type: str,
        scope: ScopeContext,
    ) -> dict[str, Any]:
        project_id = str(scope.project_id or "").strip()
        tenant_id = str(scope.tenant_id or "").strip()
        if not project_id or not tenant_id:
            raise ServiceNotFoundError(_MESSAGE_MCP_CONNECTOR_NOT_FOUND)
        if scope.allowed_project_ids and project_id not in scope.allowed_project_ids:
            raise ServiceNotFoundError(_MESSAGE_MCP_CONNECTOR_NOT_FOUND)
        with bl.conn.cursor() as cur:
            cur.execute(
                """SELECT connector_id, account_id, project_id, tenant_id,
                          display_name, enabled, auth_mode, auth_status,
                          deleted_at, deleted_by, lifecycle_child_tool_ids_jsonb, updated_at
                     FROM mcp_connectors
                    WHERE connector_id = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s""",
                (connector_id, str(scope.account_id), project_id, tenant_id),
            )
            raw = cur.fetchone()
            if not raw:
                raise ServiceNotFoundError(_MESSAGE_MCP_CONNECTOR_NOT_FOUND)
            row = (
                dict(raw)
                if isinstance(raw, Mapping)
                else {str(cur.description[index][0]): value for index, value in enumerate(raw)}
            )
            expected_ids = sorted(
                str(value).strip()
                for value in (row.get("lifecycle_child_tool_ids_jsonb") or [])
                if str(value).strip()
            )
            children: list[dict[str, Any]] = []
            for tool_id in expected_ids:
                cur.execute(
                    """SELECT tool_id, display_name, deleted_at, enabled,
                              implementation
                         FROM registry_tools
                        WHERE tool_id = %s AND account_id = %s AND project_id = %s""",
                    (tool_id, str(scope.account_id), project_id),
                )
                child_raw = cur.fetchone()
                if not child_raw:
                    children.append({"tool_id": tool_id, "conflict": "missing"})
                    continue
                child = (
                    dict(child_raw)
                    if isinstance(child_raw, Mapping)
                    else {
                        str(cur.description[index][0]): value
                        for index, value in enumerate(child_raw)
                    }
                )
                implementation = child.get("implementation") or {}
                if isinstance(implementation, str):
                    try:
                        implementation = json.loads(implementation)
                    except Exception:
                        implementation = {}
                owner = implementation or {}
                if (
                    str(owner.get("connector_id") or "").strip() != connector_id
                    or str(owner.get("owner_account_id") or "").strip() != str(scope.account_id)
                    or str(owner.get("owner_project_id") or "").strip() != project_id
                    or str(owner.get("owner_tenant_id") or "").strip() != tenant_id
                ):
                    child["conflict"] = "ownership_changed"
                children.append(child)
        validation_errors: list[str] = []
        if row.get("deleted_at") is None:
            validation_errors.append(
                f"mcp_connector must be trashed before {action_type.replace('_', ' ')}."
            )
        if any(child.get("conflict") for child in children):
            validation_errors.append(
                "MCP connector child ownership changed. Refresh the lifecycle plan."
            )
        tree_children = [
            {
                "node_id": f"registry_tool:{child.get('tool_id')}",
                "entity_type": "registry_tool",
                "entity_id": str(child.get("tool_id") or ""),
                "label": str(child.get("display_name") or child.get("tool_id") or ""),
                "selectable": True,
                "selected": True,
                "mandatory": True,
                "status": "trashed" if child.get("deleted_at") else "conflict",
                "children": [],
            }
            for child in children
        ]
        tree = {
            "node_id": f"mcp_connector:{connector_id}",
            "entity_type": "mcp_connector",
            "entity_id": connector_id,
            "label": str(row.get("display_name") or connector_id),
            "selectable": True,
            "selected": True,
            "mandatory": True,
            "status": "trashed" if row.get("deleted_at") else "live",
            "metadata": {"root_kind": "mcp_connector"},
            "children": tree_children,
        }
        version_payload = {
            "action_type": action_type,
            "connector_id": connector_id,
            "updated_at": str(row.get("updated_at") or ""),
            "deleted_at": str(row.get("deleted_at") or ""),
            "children": [
                {
                    "tool_id": str(child.get("tool_id") or ""),
                    "deleted_at": str(child.get("deleted_at") or ""),
                    "conflict": str(child.get("conflict") or ""),
                }
                for child in children
            ],
        }
        if action_type == "hard_delete":
            warnings = [
                "Hard delete permanently removes this MCP connector and its projected tools. This action cannot be undone."
            ]
        elif str(row.get("auth_mode") or "").strip().lower() == "none":
            warnings = [
                "The connector will remain disabled after restore. Run Health and Sync before enabling tools."
            ]
        else:
            warnings = [
                "Credentials were cleared when the connector moved to Trash and are not restored. Reconnect and Sync before enabling tools."
            ]
        return {
            "action_type": action_type,
            "plan_version": hashlib.sha256(
                json.dumps(version_payload, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "root_entity": {
                "entity_type": "mcp_connector",
                "entity_id": connector_id,
                "label": tree["label"],
            },
            "tree": tree,
            "impact_summary": {"mcp_connectors": 1, "registry_tools": len(children)},
            "auto_managed_cleanup_groups": [],
            "selectable_product_nodes": [tree, *tree_children],
            "warnings": warnings,
            "direct_dependency_conflicts": [],
            "validation_errors": validation_errors,
        }

    def _execute_mcp_connector_lifecycle(
        self,
        bl: Any,
        *,
        connector_id: str,
        action_type: str,
        tree: Mapping[str, Any],
        scope: ScopeContext,
    ) -> dict[str, Any]:
        account_id = str(scope.account_id)
        project_id = str(scope.project_id or "").strip()
        tenant_id = str(scope.tenant_id or "").strip()
        if not project_id or not tenant_id:
            raise ServiceConflictError(_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE)
        tool_ids = [
            str(child.get("entity_id") or "").strip()
            for child in (tree.get("children") or [])
            if isinstance(child, Mapping) and str(child.get("entity_id") or "").strip()
        ]
        with bl.conn.cursor() as cur:
            cur.execute(
                """SELECT lifecycle_child_tool_ids_jsonb FROM mcp_connectors
                    WHERE connector_id = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s
                      AND deleted_at IS NOT NULL FOR UPDATE""",
                (connector_id, account_id, project_id, tenant_id),
            )
            locked_connector = cur.fetchone()
            if not locked_connector:
                raise ServiceConflictError(_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE)
            locked_child_ids = sorted(
                str(value).strip()
                for value in (
                    (
                        locked_connector.get("lifecycle_child_tool_ids_jsonb")
                        if isinstance(locked_connector, Mapping)
                        else locked_connector[0]
                    )
                    or []
                )
                if str(value).strip()
            )
            if locked_child_ids != sorted(tool_ids):
                raise ServiceConflictError("MCP connector child ownership changed")
            for tool_id in tool_ids:
                cur.execute(
                    """SELECT implementation FROM registry_tools
                        WHERE tool_id = %s AND account_id = %s AND project_id = %s
                          AND deleted_at IS NOT NULL FOR UPDATE""",
                    (tool_id, account_id, project_id),
                )
                child = cur.fetchone()
                if not child:
                    raise ServiceConflictError(_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE)
                implementation = (
                    child.get("implementation") if isinstance(child, Mapping) else child[0]
                ) or {}
                if isinstance(implementation, str):
                    implementation = json.loads(implementation)
                if (
                    str(implementation.get("connector_id") or "").strip() != connector_id
                    or str(implementation.get("owner_account_id") or "").strip() != account_id
                    or str(implementation.get("owner_project_id") or "").strip() != project_id
                    or str(implementation.get("owner_tenant_id") or "").strip() != tenant_id
                ):
                    raise ServiceConflictError("MCP connector child ownership changed")
            if action_type == "restore":
                cur.execute(
                    """UPDATE mcp_connectors
                          SET deleted_at = NULL, deleted_by = NULL, enabled = false,
                              status = 'offline',
                              auth_status = CASE
                                  WHEN auth_mode = 'none' THEN 'not_configured'
                                  ELSE 'auth_required'
                              END,
                              last_synced_at = NULL, last_sync_status = 'never',
                              last_sync_error = CASE
                                  WHEN auth_mode = 'none'
                                      THEN 'Health check and sync required after restore'
                                  ELSE 'Reconnect and sync required after restore'
                              END,
                              updated_at = now()
                        WHERE connector_id = %s AND account_id = %s
                          AND project_id = %s AND tenant_id = %s
                          AND deleted_at IS NOT NULL""",
                    (connector_id, account_id, project_id, tenant_id),
                )
                if not cur.rowcount:
                    raise ServiceConflictError(_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE)
                if tool_ids:
                    cur.execute(
                        """UPDATE registry_tools
                              SET deleted_at = NULL, enabled = false,
                                  availability_status = 'unavailable',
                                  availability_reason_code = 'source_connector_removed',
                                  availability_reason_text = %s
                            WHERE tool_id = ANY(%s) AND account_id = %s AND project_id = %s
                              AND deleted_at IS NOT NULL""",
                        (
                            f"MCP connector '{connector_id}' must be reconnected and synced.",
                            tool_ids,
                            account_id,
                            project_id,
                        ),
                    )
                    if int(cur.rowcount or 0) != len(tool_ids):
                        raise ServiceConflictError(_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE)
            else:
                if tool_ids:
                    cur.execute(
                        """DELETE FROM registry_tools
                            WHERE tool_id = ANY(%s) AND account_id = %s AND project_id = %s
                              AND deleted_at IS NOT NULL""",
                        (tool_ids, account_id, project_id),
                    )
                    if int(cur.rowcount or 0) != len(tool_ids):
                        raise ServiceConflictError(_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE)
                cur.execute(
                    """DELETE FROM mcp_connectors
                        WHERE connector_id = %s AND account_id = %s
                          AND project_id = %s AND tenant_id = %s
                          AND deleted_at IS NOT NULL""",
                    (connector_id, account_id, project_id, tenant_id),
                )
                if not cur.rowcount:
                    raise ServiceConflictError(_MESSAGE_MCP_CONNECTOR_LIFECYCLE_PLAN_IS_STALE)
        return {"root_applied": True}
