"""Atomic lifecycle transitions for MCP-projected registry tools."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.application.registry.active_workflow_dependencies import (
    lock_workflow_dependency_inventory,
    workflow_dependency_impacts,
)

from .errors import ServiceConflictError, ServiceNotFoundError, ServiceValidationError


def _implementation(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("implementation") or row.get("implementation_jsonb") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    return value if isinstance(value, dict) else {}


def mcp_projected_tool_connector_id(row: dict[str, Any]) -> str:
    implementation = _implementation(row)
    kind = str(implementation.get("type") or implementation.get("kind") or "").strip()
    return str(implementation.get("connector_id") or "").strip() if kind == "mcp" else ""


def _require_projected_tool_lineage(
    row: dict[str, Any],
    *,
    account_id: str,
    project_id: str,
    tenant_id: str,
) -> None:
    implementation = _implementation(row)
    if (
        str(row.get("account_id") or implementation.get("owner_account_id") or "").strip()
        != account_id
        or str(row.get("project_id") or implementation.get("owner_project_id") or "").strip()
        != project_id
        or str(implementation.get("owner_tenant_id") or "").strip() != tenant_id
    ):
        raise ServiceNotFoundError("MCP projected tool not found")


class McpProjectedToolLifecycleService:
    """Keep connector allowlists and projected-tool tombstones coherent."""

    def __init__(self, *, db_store: Any, reload_registry: Any | None = None):
        self._db = db_store
        self._reload_registry = reload_registry

    def preview_delete(
        self,
        tool_id: str,
        *,
        account_id: str,
        project_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        row = self._db.get_tool(tool_id)
        if not row:
            raise ServiceNotFoundError(f"Tool '{tool_id}' not found")
        _require_projected_tool_lineage(
            row, account_id=account_id, project_id=project_id, tenant_id=tenant_id
        )
        connector_id = mcp_projected_tool_connector_id(row)
        if not connector_id:
            raise ServiceValidationError("Only MCP-projected tools use this delete plan")
        workflows = workflow_dependency_impacts(
            self._db,
            account_id=account_id,
            connector_id="",
            tool_agent_ids={tool_id: set(self._db.get_agents_using_tool(tool_id) or [])},
        )
        workflows = [
            workflow for workflow in workflows if tool_id in set(workflow.get("tool_ids") or [])
        ]
        revision_payload = {
            "tool_id": tool_id,
            "connector_id": connector_id,
            "updated_at": str(row.get("updated_at") or ""),
            "workflows": workflows,
        }
        return {
            "tool_id": tool_id,
            "connector_id": connector_id,
            "affected": {"workflows": workflows},
            "warnings": [
                "Workflow definitions are retained but cannot validate or start until the missing tool is restored or replaced."
            ],
            "revision": hashlib.sha256(
                json.dumps(revision_payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        }

    def delete_confirmed(
        self,
        tool_id: str,
        *,
        account_id: str,
        project_id: str,
        tenant_id: str,
        deleted_by: str,
        revision: str,
    ) -> dict[str, Any]:
        if not str(revision or "").strip():
            raise ServiceValidationError("MCP projected tool deletion requires a confirmed plan")
        with self._db._transaction() as cur:
            lock_workflow_dependency_inventory(cur)
            current = self.preview_delete(
                tool_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            if current["revision"] != str(revision).strip():
                raise ServiceConflictError(
                    "MCP projected tool deletion impact changed; refresh the plan",
                    payload={"stale_plan": True, "current_plan": current},
                )
            connector_id = str(current["connector_id"])
            cur.execute(
                """SELECT allowlisted_tools FROM mcp_connectors
                    WHERE connector_id = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s AND deleted_at IS NULL
                    FOR UPDATE""",
                (connector_id, account_id, project_id, tenant_id),
            )
            connector = cur.fetchone()
            if not connector:
                raise ServiceConflictError("Owning MCP connector is unavailable")
            allowlist = list(
                (
                    connector.get("allowlisted_tools")
                    if isinstance(connector, dict)
                    else connector[0]
                )
                or []
            )
            next_allowlist = [value for value in allowlist if str(value) != tool_id]
            cur.execute(
                """UPDATE mcp_connectors SET allowlisted_tools = %s::jsonb, updated_at = now()
                    WHERE connector_id = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s AND deleted_at IS NULL""",
                (json.dumps(next_allowlist), connector_id, account_id, project_id, tenant_id),
            )
            self._db.mark_tool_source_unavailable(
                [tool_id],
                reason_code="source_connector_removed",
                reason_text=(
                    f"MCP projected tool '{tool_id}' was moved to Trash. Restore it from Trash to publish it again."
                ),
            )
            self._db.delete_tool(tool_id, author=deleted_by)
        if callable(self._reload_registry):
            self._reload_registry()
        return {"status": "trashed", "tool_id": tool_id, "connector_id": connector_id}

    def restore_from_trash(
        self, tool_id: str, *, account_id: str, project_id: str, tenant_id: str
    ) -> None:
        with self._db._transaction() as cur:
            row = self._db.get_tool_including_trashed(tool_id)
            if not row or not row.get("deleted_at"):
                raise ServiceNotFoundError("MCP projected tool not found in Trash")
            _require_projected_tool_lineage(
                row, account_id=account_id, project_id=project_id, tenant_id=tenant_id
            )
            connector_id = mcp_projected_tool_connector_id(row)
            cur.execute(
                """SELECT allowlisted_tools, discovered_tools_jsonb, last_sync_status FROM mcp_connectors
                    WHERE connector_id = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s AND deleted_at IS NULL
                    FOR UPDATE""",
                (connector_id, account_id, project_id, tenant_id),
            )
            connector = cur.fetchone()
            if not connector:
                raise ServiceConflictError("Restore the MCP connector before restoring this tool")
            connector_payload = (
                dict(connector)
                if isinstance(connector, dict)
                else {
                    "allowlisted_tools": connector[0],
                    "discovered_tools_jsonb": connector[1],
                    "last_sync_status": connector[2],
                }
            )
            if str(connector_payload.get("last_sync_status") or "").strip() != "success":
                raise ServiceConflictError(
                    "Sync the owning MCP connector successfully before restoring this tool"
                )
            discovered_ids = {
                str(item.get("projected_tool_id") or "").strip()
                for item in (connector_payload.get("discovered_tools_jsonb") or [])
                if isinstance(item, dict)
            }
            if tool_id not in discovered_ids:
                raise ServiceConflictError(
                    "The remote MCP action is no longer discovered; sync the connector before restoring"
                )
            allowlist = [
                str(value).strip()
                for value in (connector_payload.get("allowlisted_tools") or [])
                if str(value).strip()
            ]
            if tool_id not in allowlist:
                allowlist.append(tool_id)
            cur.execute(
                """UPDATE mcp_connectors SET allowlisted_tools = %s::jsonb, updated_at = now()
                    WHERE connector_id = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s""",
                (
                    json.dumps(sorted(set(allowlist))),
                    connector_id,
                    account_id,
                    project_id,
                    tenant_id,
                ),
            )
            cur.execute(
                """UPDATE registry_tools
                      SET deleted_at = NULL, enabled = false,
                          availability_status = 'unavailable',
                          availability_reason_code = 'source_connector_removed',
                          availability_reason_text = %s
                    WHERE tool_id = %s AND account_id = %s AND project_id = %s
                      AND deleted_at IS NOT NULL""",
                (
                    f"MCP connector '{connector_id}' must be synced before this tool can run.",
                    tool_id,
                    account_id,
                    project_id,
                ),
            )
            if not cur.rowcount:
                raise ServiceConflictError("MCP projected tool restore is stale")
        if callable(self._reload_registry):
            self._reload_registry()

    def purge_from_trash(
        self, tool_id: str, *, account_id: str, project_id: str, tenant_id: str
    ) -> None:
        with self._db._transaction() as cur:
            row = self._db.get_tool_including_trashed(tool_id)
            if not row or not row.get("deleted_at"):
                raise ServiceNotFoundError("MCP projected tool not found in Trash")
            _require_projected_tool_lineage(
                row, account_id=account_id, project_id=project_id, tenant_id=tenant_id
            )
            connector_id = mcp_projected_tool_connector_id(row)
            cur.execute(
                """SELECT deleted_at, allowlisted_tools FROM mcp_connectors
                    WHERE connector_id = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s FOR UPDATE""",
                (connector_id, account_id, project_id, tenant_id),
            )
            connector = cur.fetchone()
            connector_deleted_at = None
            if isinstance(connector, dict):
                connector_deleted_at = connector.get("deleted_at")
            elif connector:
                connector_deleted_at = connector[0]
            if connector and connector_deleted_at:
                raise ServiceConflictError("Purge the MCP connector to purge its projected tools")
            if connector:
                allowlist = (
                    connector.get("allowlisted_tools")
                    if isinstance(connector, dict)
                    else connector[1]
                ) or []
                next_allowlist = [value for value in allowlist if str(value) != tool_id]
                cur.execute(
                    """UPDATE mcp_connectors SET allowlisted_tools = %s::jsonb, updated_at = now()
                        WHERE connector_id = %s AND account_id = %s
                          AND project_id = %s AND tenant_id = %s""",
                    (
                        json.dumps(next_allowlist),
                        connector_id,
                        account_id,
                        project_id,
                        tenant_id,
                    ),
                )
            cur.execute(
                """DELETE FROM registry_tools
                    WHERE tool_id = %s AND account_id = %s AND project_id = %s
                      AND deleted_at IS NOT NULL""",
                (tool_id, account_id, project_id),
            )
            if not cur.rowcount:
                raise ServiceConflictError("MCP projected tool purge is stale")
