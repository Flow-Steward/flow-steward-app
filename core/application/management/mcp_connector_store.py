"""Stable raw-row persistence helpers for MCP connectors."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from core.application.management.mcp_connector_common import (
    _normalize_string_list,
)
from core.application.management.mcp_connector_header_secrets import (
    delete_connector_header_secrets,
)
from core.infrastructure.db.psycopg_runtime import sql

from .errors import (
    ServiceConflictError,
)

logger = logging.getLogger(__name__)


class McpConnectorStore:
    """Stable raw-row persistence helpers for MCP connectors."""

    def insert_connector(self, bl: Any, row: dict[str, Any]) -> None:
        if not hasattr(bl, "cursor"):
            return
        try:
            with bl.cursor() as cur:
                cur.execute(
                    """INSERT INTO mcp_connectors
                    (connector_id, account_id, project_id, tenant_id,
                     display_name, description, transport,
                     base_url, auth_mode, header_secret_refs_jsonb, allowlisted_tools,
                     namespace_prefix, status, auth_status, oauth_metadata_jsonb,
                     oauth_secret_refs_jsonb, oauth_credential_revision,
                     oauth_connected_identity, oauth_last_error, discovered_tools_jsonb,
                     last_synced_at, last_sync_status, last_sync_error, enabled,
                     catalog_item_id, catalog_remote_id, catalog_item_revision)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                            %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s,
                            %s::jsonb, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        row["connector_id"],
                        row["account_id"],
                        row["project_id"],
                        row["tenant_id"],
                        row["display_name"],
                        row["description"],
                        row["transport"],
                        row["base_url"],
                        row["auth_mode"],
                        json.dumps(row.get("header_secret_refs_jsonb") or {}),
                        json.dumps(row.get("allowlisted_tools") or []),
                        row["namespace_prefix"],
                        row["status"],
                        row.get("auth_status") or "not_configured",
                        json.dumps(row.get("oauth_metadata_jsonb") or {}),
                        json.dumps(row.get("oauth_secret_refs_jsonb") or {}),
                        row.get("oauth_credential_revision") or 1,
                        row.get("oauth_connected_identity") or "",
                        row.get("oauth_last_error") or "",
                        json.dumps(row.get("discovered_tools_jsonb") or []),
                        row["last_synced_at"],
                        row["last_sync_status"],
                        row["last_sync_error"],
                        row["enabled"],
                        row.get("catalog_item_id") or "",
                        row.get("catalog_remote_id") or "",
                        int(row.get("catalog_item_revision") or 0),
                    ),
                )
        except Exception as exc:
            logger.warning("Failed to insert mcp_connector: %s", exc)
            raise

    def get_connector_row(
        self,
        bl: Any,
        connector_id: str,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        include_trashed: bool = False,
    ) -> dict[str, Any] | None:
        if not hasattr(bl, "cursor"):
            return None
        account_filter: sql.Composable = (
            sql.SQL("") if include_trashed else sql.SQL(" AND deleted_at IS NULL")
        )
        params: list[Any] = [connector_id]
        if str(account_id or "").strip():
            account_filter += sql.SQL(" AND account_id = %s")
            params.append(str(account_id or "").strip())
        if str(project_id or "").strip():
            account_filter += sql.SQL(" AND project_id = %s")
            params.append(str(project_id or "").strip())
        if str(tenant_id or "").strip():
            account_filter += sql.SQL(" AND tenant_id = %s")
            params.append(str(tenant_id or "").strip())
        try:
            with bl.cursor() as cur:
                cur.execute(
                    sql.SQL("""SELECT connector_id, account_id, project_id, tenant_id,
                              display_name, description,
                              transport, base_url, auth_mode,
                              header_secret_refs_jsonb, allowlisted_tools,
                              namespace_prefix, status,
                              auth_status, oauth_metadata_jsonb,
                              oauth_secret_refs_jsonb, oauth_credential_revision,
                              oauth_connected_identity, oauth_last_error,
                              discovered_tools_jsonb, last_synced_at,
                              last_sync_status, last_sync_error,
                              enabled, deleted_at, deleted_by,
                              lifecycle_child_tool_ids_jsonb,
                              catalog_item_id, catalog_remote_id, catalog_item_revision,
                              created_at, updated_at
                       FROM mcp_connectors
                       WHERE connector_id = %s""")
                    + account_filter,
                    tuple(params),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self.row_to_dict(row, cur.description)
        except Exception as exc:
            logger.warning("Failed to read mcp_connector '%s': %s", connector_id, exc)
            raise

    def list_connector_rows(
        self,
        bl: Any,
        account_id: str | None = None,
        *,
        project_id: str | None = None,
        tenant_id: str | None = None,
        include_trashed: bool = False,
    ) -> list[dict[str, Any]]:
        if not hasattr(bl, "cursor"):
            return []
        try:
            with bl.cursor() as cur:
                if str(account_id or "").strip():
                    cur.execute(
                        """SELECT connector_id, account_id, project_id, tenant_id,
                                  display_name, description,
                                  transport, base_url, auth_mode,
                                  header_secret_refs_jsonb, allowlisted_tools,
                                  namespace_prefix, status,
                                  auth_status, oauth_metadata_jsonb,
                                  oauth_secret_refs_jsonb, oauth_credential_revision,
                                  oauth_connected_identity, oauth_last_error,
                                  discovered_tools_jsonb, last_synced_at,
                                  last_sync_status, last_sync_error,
                                  enabled, deleted_at, deleted_by,
                                  lifecycle_child_tool_ids_jsonb,
                                  catalog_item_id, catalog_remote_id, catalog_item_revision,
                                  created_at, updated_at
                           FROM mcp_connectors
                           WHERE account_id = %s
                             AND project_id = %s
                             AND tenant_id = %s
                             AND (%s OR deleted_at IS NULL)
                           ORDER BY created_at""",
                        (
                            str(account_id or "").strip(),
                            str(project_id or "").strip(),
                            str(tenant_id or "").strip(),
                            include_trashed,
                        ),
                    )
                else:
                    cur.execute(
                        """SELECT connector_id, account_id, project_id, tenant_id,
                              display_name, description,
                              transport, base_url, auth_mode,
                              header_secret_refs_jsonb, allowlisted_tools,
                              namespace_prefix, status,
                              auth_status, oauth_metadata_jsonb,
                              oauth_secret_refs_jsonb, oauth_credential_revision,
                              oauth_connected_identity, oauth_last_error,
                              discovered_tools_jsonb, last_synced_at,
                              last_sync_status, last_sync_error,
                              enabled, deleted_at, deleted_by,
                              lifecycle_child_tool_ids_jsonb,
                              catalog_item_id, catalog_remote_id, catalog_item_revision,
                              created_at, updated_at
                       FROM mcp_connectors
                       WHERE (%s OR deleted_at IS NULL)
                       ORDER BY created_at""",
                        (include_trashed,),
                    )
                return [self.row_to_dict(row, cur.description) for row in cur.fetchall()]
        except Exception as exc:
            logger.warning("Failed to list mcp_connectors: %s", exc)
            raise

    def update_connector_row(
        self,
        bl: Any,
        connector_id: str,
        updates: dict[str, Any],
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        if not hasattr(bl, "cursor"):
            return
        set_clauses: list[str] = []
        values: list[Any] = []
        jsonb_fields = {
            "header_secret_refs_jsonb": "header_secret_refs_jsonb",  # pragma: allowlist secret
            "allowlisted_tools": "allowlisted_tools",
            "discovered_tools_jsonb": "discovered_tools_jsonb",
            "oauth_metadata_jsonb": "oauth_metadata_jsonb",
            "oauth_secret_refs_jsonb": "oauth_secret_refs_jsonb",  # pragma: allowlist secret
        }
        for key, value in updates.items():
            database_key = jsonb_fields.get(key, key)
            if key in jsonb_fields:
                set_clauses.append(f"{database_key} = %s::jsonb")
                values.append(json.dumps(value))
            else:
                set_clauses.append(f"{database_key} = %s")
                values.append(value)
        set_clauses.append("updated_at = now()")
        where_clauses = ["connector_id = %s"]
        where_clauses.append("deleted_at IS NULL")
        values.append(connector_id)
        if str(account_id or "").strip():
            where_clauses.append("account_id = %s")
            values.append(str(account_id or "").strip())
        if str(project_id or "").strip():
            where_clauses.append("project_id = %s")
            values.append(str(project_id or "").strip())
        if str(tenant_id or "").strip():
            where_clauses.append("tenant_id = %s")
            values.append(str(tenant_id or "").strip())
        query = "".join(
            [
                "UPDATE mcp_connectors SET ",
                ", ".join(set_clauses),
                " WHERE ",
                " AND ".join(where_clauses),
            ]
        )
        try:
            with bl.cursor() as cur:
                cur.execute(query, values)
                if getattr(cur, "rowcount", 1) == 0:
                    raise ServiceConflictError(
                        "MCP connector changed or was deleted before the update completed"
                    )
        except Exception as exc:
            logger.warning("Failed to update mcp_connector: %s", exc)
            raise

    def soft_delete_connector_row(
        self,
        bl: Any,
        connector_id: str,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        deleted_by: str = "system",
        child_tool_ids: list[str] | None = None,
        oauth_metadata: dict[str, Any] | None = None,
    ) -> None:
        if not hasattr(bl, "cursor"):
            return
        query = """UPDATE mcp_connectors
                      SET enabled = false,
                          status = 'offline',
                          auth_status = CASE
                              WHEN auth_mode = 'none' THEN 'not_configured'
                              ELSE 'auth_required'
                          END,
                          header_secret_refs_jsonb = '{}'::jsonb,
                          oauth_metadata_jsonb = %s::jsonb,
                          oauth_secret_refs_jsonb = '{}'::jsonb,
                          oauth_connected_identity = '',
                          oauth_last_error = '',
                          last_synced_at = NULL,
                          last_sync_status = 'never',
                          last_sync_error = CASE
                              WHEN auth_mode = 'none'
                                  THEN 'Health check and sync required after restore'
                              ELSE 'Reconnect and sync required after restore'
                          END,
                          oauth_credential_revision = oauth_credential_revision + 1,
                          lifecycle_child_tool_ids_jsonb = %s::jsonb,
                          deleted_at = now(), deleted_by = %s, updated_at = now()
                    WHERE connector_id = %s AND deleted_at IS NULL"""
        params: list[Any] = [
            json.dumps(oauth_metadata or {}, sort_keys=True),
            json.dumps(sorted(set(_normalize_string_list(child_tool_ids)))),
            str(deleted_by or "system").strip() or "system",
            connector_id,
        ]
        if str(account_id or "").strip():
            query += " AND account_id = %s"
            params.append(str(account_id or "").strip())
        if str(project_id or "").strip():
            query += " AND project_id = %s"
            params.append(str(project_id or "").strip())
        if str(tenant_id or "").strip():
            query += " AND tenant_id = %s"
            params.append(str(tenant_id or "").strip())
        try:
            if all(str(value or "").strip() for value in (account_id, project_id, tenant_id)):
                delete_connector_header_secrets(
                    bl,
                    connector_id=connector_id,
                    account_id=str(account_id),
                    project_id=str(project_id),
                    tenant_id=str(tenant_id),
                )
            with bl.cursor() as cur:
                cur.execute(query, tuple(params))
        except Exception as exc:
            logger.warning("Failed to delete mcp_connector: %s", exc)
            raise

    @staticmethod
    def row_to_dict(row: tuple, description: Any) -> dict[str, Any]:
        columns = [col[0] if hasattr(col, "__getitem__") else col.name for col in description]
        payload: dict[str, Any] = {}
        for index, column in enumerate(columns):
            value = row[index]
            if isinstance(value, datetime):
                value = value.isoformat()
            payload[column] = value
        return payload
