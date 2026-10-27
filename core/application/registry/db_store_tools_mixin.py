"""Registry persistence for tools and their public contracts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from core.application.registry.db_store_common import (
    DELETED_AT_IS_NULL_VALUE,
    DESCRIPTION_EXCLUDED_DESCRIPTION_SQL,
    DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL,
    ENABLED_EXCLUDED_ENABLED_SQL,
    SQL_AND,
    SQL_DELETED_AT_IS_NULL,
    SQL_ENABLED_TRUE,
    TAGS_EXCLUDED_TAGS_SQL,
    VERSION_EXCLUDED_VERSION_SQL,
    _nested_text_value,
    _nullable_scope_sql,
    _system_registry_account_id,
    json,
    logger,
    normalize_builder_metadata,
    psycopg_runtime,
)


class _ProjectScopeLoader(Protocol):
    def __call__(
        self,
        project_id: str,
        *,
        include_trashed: bool = False,
        account_id: str | None = None,
    ) -> dict[str, str] | None: ...


class RegistryDBStoreToolsMixin:
    # Connection and revision operations are supplied by RegistryDBStore's
    # existing sibling components; these declarations add no runtime fallback.
    _column_exists: Callable[[str, str], bool]
    _cursor: Callable[[], AbstractContextManager[Any]]
    _transaction: Callable[[], AbstractContextManager[Any]]
    _log_change_cur: Callable[[Any, str, str, str, dict, str], None]
    _trigger_public_contract_projection_sync: Callable[[], None]
    _get_project_scope: _ProjectScopeLoader

    def mark_tool_source_unavailable(
        self,
        tool_ids: list[str],
        *,
        reason_code: str,
        reason_text: str,
    ) -> int:
        """Record that a tool's projecting source is gone.

        save_tool cannot carry this: it writes a fixed column set built from
        ToolManifest, which models the portable tool contract and deliberately
        drops availability. A retained projection whose source was deleted would
        otherwise keep reading availability_status='available' — disabled, but
        claiming a source that no longer exists.
        """
        wanted = [str(item or "").strip() for item in (tool_ids or [])]
        wanted = [item for item in wanted if item]
        if not wanted:
            return 0
        if not self._column_exists("registry_tools", "availability_status"):
            return 0
        assignments = ["availability_status = %s"]
        params: list[Any] = ["unavailable"]
        if self._column_exists("registry_tools", "availability_reason_code"):
            assignments.append("availability_reason_code = %s")
            params.append(str(reason_code or "").strip() or "source_unavailable")
        if self._column_exists("registry_tools", "availability_reason_text"):
            assignments.append("availability_reason_text = %s")
            params.append(str(reason_text or "").strip())
        params.append(wanted)
        with self._cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL(
                    "UPDATE registry_tools SET {} WHERE tool_id = ANY(%s)"
                ).format(
                    psycopg_runtime.sql.SQL(", ").join(
                        psycopg_runtime.sql.SQL(assignment) for assignment in assignments
                    )
                ),
                tuple(params),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def get_tool(self, tool_id: str) -> dict | None:
        with self._cursor() as cur:
            conditions = ["tool_id = %s"]
            params: list[Any] = [tool_id]
            if self._column_exists("registry_tools", "deleted_at"):
                conditions.append(SQL_DELETED_AT_IS_NULL)
            where_sql = psycopg_runtime.sql.SQL(SQL_AND).join(
                psycopg_runtime.sql.SQL(condition) for condition in conditions
            )
            cur.execute(
                psycopg_runtime.sql.SQL("SELECT * FROM registry_tools WHERE {}").format(where_sql),
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                return None
            tool = dict(row)
            cur.execute(
                "SELECT DISTINCT agent_id FROM registry_agent_tools WHERE tool_id = %s ORDER BY agent_id",
                (tool_id,),
            )
            using_agents = [str(r.get("agent_id") or "").strip() for r in cur.fetchall()]
            using_agents = [agent_id for agent_id in using_agents if agent_id]
            tool["using_agents"] = using_agents
            tool["_agents"] = using_agents
            tool["agents"] = using_agents
            return tool

    def get_tool_including_trashed(self, tool_id: str) -> dict | None:
        """Return an exact registry tool tombstone without making it live."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM registry_tools WHERE tool_id = %s", (tool_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_tools(self, enabled_only: bool = False, include_trashed: bool = False) -> list[dict]:
        with self._cursor() as cur:
            conditions: list[str] = []
            if not include_trashed and self._column_exists("registry_tools", "deleted_at"):
                conditions.append(SQL_DELETED_AT_IS_NULL)
            if enabled_only:
                conditions.append(SQL_ENABLED_TRUE)
            where_sql = (
                psycopg_runtime.sql.SQL("WHERE {}").format(
                    psycopg_runtime.sql.SQL(SQL_AND).join(
                        psycopg_runtime.sql.SQL(condition) for condition in conditions
                    )
                )
                if conditions
                else psycopg_runtime.sql.SQL("")
            )
            cur.execute(
                psycopg_runtime.sql.SQL("SELECT * FROM registry_tools {} ORDER BY tool_id").format(
                    where_sql
                )
            )
            return [dict(r) for r in cur.fetchall()]

    def list_tools_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        with self._cursor() as cur:
            has_deleted_at = self._column_exists("registry_tools", "deleted_at")
            has_builder_metadata = self._column_exists("registry_tools", "builder_metadata_jsonb")
            has_account_id = self._column_exists("registry_tools", "account_id")
            has_project_id = self._column_exists("registry_tools", "project_id")
            has_scope_kind = self._column_exists("registry_tools", "scope_kind")
            has_source_install_id = self._column_exists("registry_tools", "source_install_id")
            has_availability_status = self._column_exists("registry_tools", "availability_status")
            has_new_runs_blocked = self._column_exists("registry_tools", "new_runs_blocked")
            has_visible_in_catalog = self._column_exists("registry_tools", "visible_in_catalog")
            normalized_account_id = str(account_id or "").strip()
            normalized_project_id = str(project_id or "").strip()
            scoped_conditions: list[str] = []
            if has_account_id and normalized_account_id:
                scoped_conditions.append(
                    f"({_nullable_scope_sql('account_id')} OR account_id = %(account_id)s)"
                )
            if has_project_id:
                if normalized_project_id:
                    scoped_conditions.append(
                        f"({_nullable_scope_sql('project_id')} OR project_id = %(project_id)s)"
                    )
                else:
                    scoped_conditions.append(_nullable_scope_sql("project_id"))
            active_clause = " AND deleted_at IS NULL" if has_deleted_at else ""
            if enabled_only:
                cur.execute(
                    psycopg_runtime.sql.SQL(
                        """SELECT
                           tool_id,
                           display_name,
                           LEFT(COALESCE(description, ''), 120) AS description_preview,
                           enabled,
                           category,
                           COALESCE(implementation->>'kind', implementation->>'type', '') AS impl_type,
                           COALESCE(config->>'cost_tier', '') AS cost_tier
                           {account_id_column}
                           {project_id_column}
                           {scope_kind_column}
                           {source_install_id_column}
                           {availability_status_column}
                           {new_runs_blocked_column}
                           {visible_in_catalog_column}
                           {builder_metadata_column}
                       FROM registry_tools
                       WHERE enabled = true{active_clause}{scope_clause}
                       ORDER BY tool_id"""
                    ).format(
                        account_id_column=psycopg_runtime.sql.SQL(
                            ", account_id" if has_account_id else ""
                        ),
                        project_id_column=psycopg_runtime.sql.SQL(
                            ", project_id" if has_project_id else ""
                        ),
                        scope_kind_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(scope_kind, 'system') AS scope_kind"
                            if has_scope_kind
                            else ""
                        ),
                        source_install_id_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(source_install_id, '') AS source_install_id"
                            if has_source_install_id
                            else ""
                        ),
                        availability_status_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(availability_status, 'available') AS availability_status"
                            if has_availability_status
                            else ""
                        ),
                        new_runs_blocked_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(new_runs_blocked, false) AS new_runs_blocked"
                            if has_new_runs_blocked
                            else ""
                        ),
                        visible_in_catalog_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(visible_in_catalog, true) AS visible_in_catalog"
                            if has_visible_in_catalog
                            else ""
                        ),
                        builder_metadata_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(builder_metadata_jsonb, '{}'::jsonb) AS builder_metadata_jsonb"
                            if has_builder_metadata
                            else ""
                        ),
                        active_clause=psycopg_runtime.sql.SQL(active_clause),
                        scope_clause=(
                            psycopg_runtime.sql.SQL(" AND {}").format(
                                psycopg_runtime.sql.SQL(SQL_AND).join(
                                    psycopg_runtime.sql.SQL(condition)
                                    for condition in scoped_conditions
                                )
                            )
                            if scoped_conditions
                            else psycopg_runtime.sql.SQL("")
                        ),
                    ),
                    {
                        "account_id": normalized_account_id or None,
                        "project_id": normalized_project_id or None,
                    },
                )
            else:
                conditions: list[str] = []
                if has_deleted_at:
                    conditions.append(DELETED_AT_IS_NULL_VALUE)
                conditions.extend(scoped_conditions)
                where_sql = (
                    psycopg_runtime.sql.SQL("WHERE {}").format(
                        psycopg_runtime.sql.SQL(SQL_AND).join(
                            psycopg_runtime.sql.SQL(condition) for condition in conditions
                        )
                    )
                    if conditions
                    else psycopg_runtime.sql.SQL("")
                )
                cur.execute(
                    psycopg_runtime.sql.SQL(
                        """SELECT
                           tool_id,
                           display_name,
                           LEFT(COALESCE(description, ''), 120) AS description_preview,
                           enabled,
                           category,
                           COALESCE(implementation->>'kind', implementation->>'type', '') AS impl_type,
                           COALESCE(config->>'cost_tier', '') AS cost_tier
                           {account_id_column}
                           {project_id_column}
                           {scope_kind_column}
                           {source_install_id_column}
                           {availability_status_column}
                           {new_runs_blocked_column}
                           {visible_in_catalog_column}
                           {builder_metadata_column}
                           FROM registry_tools
                           {where_sql}
                           ORDER BY tool_id"""
                    ).format(
                        account_id_column=psycopg_runtime.sql.SQL(
                            ", account_id" if has_account_id else ""
                        ),
                        project_id_column=psycopg_runtime.sql.SQL(
                            ", project_id" if has_project_id else ""
                        ),
                        scope_kind_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(scope_kind, 'system') AS scope_kind"
                            if has_scope_kind
                            else ""
                        ),
                        source_install_id_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(source_install_id, '') AS source_install_id"
                            if has_source_install_id
                            else ""
                        ),
                        availability_status_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(availability_status, 'available') AS availability_status"
                            if has_availability_status
                            else ""
                        ),
                        new_runs_blocked_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(new_runs_blocked, false) AS new_runs_blocked"
                            if has_new_runs_blocked
                            else ""
                        ),
                        visible_in_catalog_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(visible_in_catalog, true) AS visible_in_catalog"
                            if has_visible_in_catalog
                            else ""
                        ),
                        builder_metadata_column=psycopg_runtime.sql.SQL(
                            ", COALESCE(builder_metadata_jsonb, '{}'::jsonb) AS builder_metadata_jsonb"
                            if has_builder_metadata
                            else ""
                        ),
                        where_sql=where_sql,
                    ),
                    {
                        "account_id": normalized_account_id or None,
                        "project_id": normalized_project_id or None,
                    },
                )
            return [dict(r) for r in cur.fetchall()]

    def save_tool(self, data: dict, author: str = "admin") -> None:
        from core.application.registry.identifier_keys import normalize_tool_identifier
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.model_validate(
            {**data, "tool_id": normalize_tool_identifier(data.get("tool_id"))}
        )
        payload = manifest.to_registry_payload()
        snapshot = manifest.to_dict()
        tool_id = manifest.tool_id
        has_deleted_at = self._column_exists("registry_tools", "deleted_at")
        has_deleted_by = self._column_exists("registry_tools", "deleted_by")
        owner_account_id = (
            # ToolManifest intentionally models the portable tool contract and
            # therefore drops transport ownership. Preserve the authenticated
            # account supplied by the command layer before consulting legacy
            # owner hints embedded in implementation/config payloads.
            str(data.get("account_id") or "").strip()
            or str(payload.get("account_id") or "").strip()
            or _nested_text_value(payload.get("implementation"), "owner_account_id")
            or _nested_text_value(payload.get("config"), "owner_account_id")
        )
        owner_project_id = (
            str(data.get("project_id") or "").strip()
            or _nested_text_value(payload.get("implementation"), "owner_project_id")
            or _nested_text_value(payload.get("config"), "owner_project_id")
        )
        owner_tenant_id = (
            str(data.get("tenant_id") or "").strip()
            or _nested_text_value(payload.get("implementation"), "owner_tenant_id")
            or _nested_text_value(payload.get("config"), "owner_tenant_id")
        )
        scope_kind = str(data.get("scope_kind") or "").strip() or (
            "project" if owner_project_id else "system"
        )
        implementation_kind = str(
            (payload.get("implementation") or {}).get("type")
            or (payload.get("implementation") or {}).get("kind")
            or ""
        ).strip()
        if owner_project_id or scope_kind == "project":
            if scope_kind != "project" or not owner_account_id or not owner_project_id:
                raise ValueError("project-scoped tool ownership is incomplete")
            project_scope = self._get_project_scope(
                owner_project_id,
                account_id=owner_account_id,
            )
            if not project_scope:
                raise ValueError("project-scoped tool ownership does not resolve")
            authoritative_tenant_id = str(project_scope.get("tenant_id") or "").strip()
            if owner_tenant_id and owner_tenant_id != authoritative_tenant_id:
                raise ValueError("project-scoped tool tenant ownership does not match project")
            if implementation_kind == "mcp":
                if not owner_tenant_id:
                    raise ValueError("project-scoped tool tenant ownership does not match project")
                for owner_payload in (
                    payload.get("implementation") or {},
                    payload.get("config") or {},
                ):
                    if (
                        _nested_text_value(owner_payload, "owner_account_id") != owner_account_id
                        or _nested_text_value(owner_payload, "owner_project_id") != owner_project_id
                        or _nested_text_value(owner_payload, "owner_tenant_id") != owner_tenant_id
                    ):
                        raise ValueError("MCP tool ownership metadata is inconsistent")
        persisted_account_id = _system_registry_account_id(
            data,
            account_id=owner_account_id,
            project_id=owner_project_id,
            source_install_id=(
                data.get("source_install_id")
                or _nested_text_value(payload.get("config"), "source_install_id")
            ),
        )
        source_install_id = str(
            data.get("source_install_id") or payload.get("source_install_id") or ""
        ).strip()
        if not source_install_id:
            source_install_id = _nested_text_value(payload.get("config"), "source_install_id")
        clear_deleted_assignments = psycopg_runtime.sql.SQL("")
        if has_deleted_at:
            clear_deleted_assignments += psycopg_runtime.sql.SQL(", deleted_at = NULL")
        if has_deleted_by:
            clear_deleted_assignments += psycopg_runtime.sql.SQL(", deleted_by = NULL")
        row_payload = {
            "tool_id": tool_id,
            "version": payload.get("version", "1.0.0"),
            "display_name": payload.get("display_name", tool_id),
            "description": payload.get("description", ""),
            "enabled": payload.get("enabled", True),
            "category": payload.get("category", "general"),
            "prompt": payload.get("prompt", ""),
            "interface": json.dumps(payload.get("interface", {})),
            "implementation": json.dumps(payload.get("implementation", {})),
            "config": json.dumps(payload.get("config", {})),
            "tags": payload.get("tags", []),
            "builder_metadata_jsonb": json.dumps(
                normalize_builder_metadata(payload.get("builder_metadata_jsonb"))
            ),
        }
        columns = [
            "tool_id",
            "version",
            "display_name",
            "description",
            "enabled",
            "category",
            "prompt",
            "interface",
            "implementation",
            "config",
            "tags",
        ]
        updates = [
            VERSION_EXCLUDED_VERSION_SQL,
            DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL,
            DESCRIPTION_EXCLUDED_DESCRIPTION_SQL,
            ENABLED_EXCLUDED_ENABLED_SQL,
            "category = EXCLUDED.category",
            "prompt = EXCLUDED.prompt",
            "interface = EXCLUDED.interface",
            "implementation = EXCLUDED.implementation",
            "config = EXCLUDED.config",
            TAGS_EXCLUDED_TAGS_SQL,
        ]
        update_assignments: list[psycopg_runtime.sql.Composable] = [
            psycopg_runtime.sql.SQL(fragment) for fragment in updates
        ]
        optional_columns = [
            ("builder_metadata_jsonb", row_payload.get("builder_metadata_jsonb")),
            (
                "account_id",
                persisted_account_id,
            ),
            ("project_id", owner_project_id or None),
            ("scope_kind", scope_kind),
            ("source_install_id", source_install_id or None),
            (
                "availability_status",
                str(data.get("availability_status") or "available").strip() or "available",
            ),
            (
                "availability_reason_code",
                str(data.get("availability_reason_code") or "none").strip() or "none",
            ),
            (
                "availability_reason_text",
                str(data.get("availability_reason_text") or "").strip(),
            ),
            ("new_runs_blocked", bool(data.get("new_runs_blocked", False))),
            ("visible_in_catalog", bool(data.get("visible_in_catalog", True))),
        ]
        for column_name, column_value in optional_columns:
            if not self._column_exists("registry_tools", column_name):
                continue
            columns.append(column_name)
            update_assignments.append(
                psycopg_runtime.sql.SQL("{} = EXCLUDED.{}").format(
                    psycopg_runtime.sql.Identifier(column_name),
                    psycopg_runtime.sql.Identifier(column_name),
                )
            )
            row_payload[column_name] = column_value
        with self._transaction() as cur:
            # A row lock cannot serialize two first inserts because no row exists
            # yet. The transaction-scoped identity lock closes that gap before
            # either publisher observes or creates the globally unique tool_id.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"registry_tool:{tool_id}",),
            )
            cur.execute(
                """SELECT account_id, project_id, scope_kind
                     FROM registry_tools
                    WHERE tool_id = %s
                    FOR UPDATE""",
                (tool_id,),
            )
            existing_raw = cur.fetchone()
            existing_ownership: tuple[str, str, str] | None = None
            if isinstance(existing_raw, dict):
                existing_ownership = (
                    str(existing_raw.get("account_id") or "").strip(),
                    str(existing_raw.get("project_id") or "").strip(),
                    str(existing_raw.get("scope_kind") or "system").strip() or "system",
                )
            elif isinstance(existing_raw, (tuple, list)) and len(existing_raw) >= 3:
                existing_ownership = (
                    str(existing_raw[0] or "").strip(),
                    str(existing_raw[1] or "").strip(),
                    str(existing_raw[2] or "system").strip() or "system",
                )
            requested_ownership = (
                str(persisted_account_id or "").strip(),
                owner_project_id,
                scope_kind,
            )
            if existing_ownership is not None and existing_ownership != requested_ownership:
                raise ValueError("registry tool ownership cannot be changed by upsert")
            is_new = existing_ownership is None
            cur.execute(
                psycopg_runtime.sql.SQL("""INSERT INTO registry_tools ({columns})
                    VALUES ({values})
                    ON CONFLICT (tool_id) DO UPDATE SET
                        {updates}{clear_deleted_assignments}""").format(
                    columns=psycopg_runtime.sql.SQL(", ").join(
                        psycopg_runtime.sql.Identifier(column) for column in columns
                    ),
                    values=psycopg_runtime.sql.SQL(", ").join(
                        psycopg_runtime.sql.Placeholder(column) for column in columns
                    ),
                    updates=psycopg_runtime.sql.SQL(", ").join(update_assignments),
                    clear_deleted_assignments=clear_deleted_assignments,
                ),
                row_payload,
            )
            self._log_change_cur(
                cur, "tool", tool_id, "create" if is_new else "update", snapshot, author
            )
        self._trigger_public_contract_projection_sync()

    def get_tool_catalog_entry(self, tool_id: str) -> dict | None:
        from core.application.registry.tool_manifest import ToolManifest

        row = self.get_tool(tool_id)
        if not row:
            return None
        return ToolManifest.from_db_row(row).catalog_entry()

    def list_tool_catalog(self, enabled_only: bool = False) -> list[dict]:
        from core.application.registry.tool_manifest import ToolManifest

        catalog: list[dict] = []
        for row in self.list_tools(enabled_only=enabled_only):
            try:
                catalog.append(ToolManifest.from_db_row(row).catalog_entry())
            except Exception as exc:
                logger.warning("Skipping invalid tool catalog row %s: %s", row.get("tool_id"), exc)
        return catalog

    def get_effective_tool_catalog(self, agent_id: str) -> list[dict]:
        from core.application.registry.tool_manifest import ToolManifest

        with self._cursor() as cur:
            cur.execute(
                """SELECT DISTINCT t.*,
                          'direct' AS source
                   FROM registry_tools t
                   JOIN registry_agent_tools at ON at.tool_id = t.tool_id
                   WHERE at.agent_id = %s
                   ORDER BY t.tool_id""",
                (agent_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]

        catalog: list[dict] = []
        for row in rows:
            try:
                catalog.append(
                    ToolManifest.from_db_row(row).catalog_entry(
                        source=str(row.get("source") or "").strip() or None
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Skipping invalid effective tool catalog row %s: %s", row.get("tool_id"), exc
                )
        return catalog

    def delete_tool(self, tool_id: str, author: str = "admin") -> None:
        snapshot = self.get_tool(tool_id)
        if not snapshot:
            raise KeyError(f"Tool '{tool_id}' not found")
        has_deleted_at = self._column_exists("registry_tools", "deleted_at")
        has_deleted_by = self._column_exists("registry_tools", "deleted_by")
        with self._transaction() as cur:
            if has_deleted_at and has_deleted_by:
                cur.execute(
                    "UPDATE registry_tools SET deleted_at = NOW(), deleted_by = %s WHERE tool_id = %s AND deleted_at IS NULL",
                    (author, tool_id),
                )
            elif has_deleted_at:
                cur.execute(
                    "UPDATE registry_tools SET deleted_at = NOW() WHERE tool_id = %s AND deleted_at IS NULL",
                    (tool_id,),
                )
            else:
                cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
            self._log_change_cur(cur, "tool", tool_id, "delete", snapshot, author)
        self._trigger_public_contract_projection_sync()

    def toggle_tool(self, tool_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE registry_tools SET enabled = NOT enabled WHERE tool_id = %s RETURNING enabled",
                (tool_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Tool '{tool_id}' not found")
            enabled = row["enabled"]
        self._trigger_public_contract_projection_sync()
        return enabled

    def get_agents_using_tool(self, tool_id: str) -> list[str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT agent_id FROM registry_agent_tools WHERE tool_id = %s ORDER BY agent_id",
                (tool_id,),
            )
            return [r["agent_id"] for r in cur.fetchall()]
