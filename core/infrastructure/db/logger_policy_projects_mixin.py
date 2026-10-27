from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_policy_projects_common import (
    _as_dict,
    _coerce_float,
    _coerce_int,
    _merge_string_fields,
    _normalize_policy_enum,
)

DEFAULT_PROJECT_POLICY: dict = {
    "autonomy_mode": "supervised",
    "autonomy_confidence_threshold": 0.80,
    "retry_strategy": "bounded",
    "retry_max_attempts": 2,
    "retry_backoff_initial_seconds": 2,
    "retry_backoff_max_seconds": 120,
    "concurrency_cap": 1,
    "tool_policy_mode": "registry_only",
}

PROJECT_POLICY_AUTONOMY_MODES = {"manual", "supervised", "auto"}
PROJECT_POLICY_RETRY_STRATEGIES = {"bounded", "flat", "failure_class"}
PROJECT_POLICY_TOOL_MODES = {"registry_only", "restricted", "permissive"}
#: Saving a project's workflow selection keeps the links of hook storage
#: workflows (``settings.hook_storage``), which no selection ever lists.
KEEP_HOOK_STORAGE_LINKS_SQL = (
    " AND NOT EXISTS (SELECT 1 FROM registry_workflows hook_rw"
    " WHERE hook_rw.workflow_id = project_workflows.workflow_id"
    " AND hook_rw.settings ? 'hook_storage')"
)


class PlatformLoggerPolicyProjectsMixin:
    conn: Any
    _table_exists: Any
    _column_exists: Any
    get_project: Any
    list_project_connections: Any
    get_project_provider_bindings: Any
    _normalize_project_db_dsn: Any

    @staticmethod
    def _normalize_project_policy(policy: dict | None = None, *, strict: bool = False) -> dict:
        raw = policy if isinstance(policy, dict) else {}
        normalized = dict(DEFAULT_PROJECT_POLICY)

        normalized["autonomy_mode"] = _normalize_policy_enum(
            raw.get("autonomy_mode", normalized["autonomy_mode"]),
            default=normalized["autonomy_mode"],
            allowed=PROJECT_POLICY_AUTONOMY_MODES,
            strict=strict,
            aliases={
                "assist": "supervised",
                "assisted": "supervised",
                "human": "manual",
                "human_only": "manual",
            },
            field_name="autonomy_mode",
        )
        normalized["autonomy_confidence_threshold"] = max(
            0.0,
            min(
                1.0,
                _coerce_float(
                    raw.get(
                        "autonomy_confidence_threshold", normalized["autonomy_confidence_threshold"]
                    ),
                    normalized["autonomy_confidence_threshold"],
                ),
            ),
        )
        normalized["retry_strategy"] = _normalize_policy_enum(
            raw.get("retry_strategy", normalized["retry_strategy"]),
            default=normalized["retry_strategy"],
            allowed=PROJECT_POLICY_RETRY_STRATEGIES,
            strict=strict,
            field_name="retry_strategy",
        )
        normalized["retry_max_attempts"] = max(
            0,
            _coerce_int(
                raw.get("retry_max_attempts", normalized["retry_max_attempts"]),
                normalized["retry_max_attempts"],
            ),
        )
        normalized["retry_backoff_initial_seconds"] = max(
            0,
            _coerce_int(
                raw.get(
                    "retry_backoff_initial_seconds", normalized["retry_backoff_initial_seconds"]
                ),
                normalized["retry_backoff_initial_seconds"],
            ),
        )
        normalized["retry_backoff_max_seconds"] = max(
            0,
            _coerce_int(
                raw.get("retry_backoff_max_seconds", normalized["retry_backoff_max_seconds"]),
                normalized["retry_backoff_max_seconds"],
            ),
        )
        normalized["concurrency_cap"] = max(
            1,
            _coerce_int(
                raw.get("concurrency_cap", normalized["concurrency_cap"]),
                normalized["concurrency_cap"],
            ),
        )
        normalized["tool_policy_mode"] = _normalize_policy_enum(
            raw.get("tool_policy_mode", normalized["tool_policy_mode"]),
            default=normalized["tool_policy_mode"],
            allowed=PROJECT_POLICY_TOOL_MODES,
            strict=strict,
            aliases={"allow_all": "permissive"},
            field_name="tool_policy_mode",
        )

        return normalized

    @staticmethod
    def _derive_project_policy_from_row(row: dict | None) -> dict:
        policy = dict(DEFAULT_PROJECT_POLICY)
        if not isinstance(row, dict):
            return policy

        agents_config = _as_dict(row.get("agents_config"))
        config_blob = _as_dict(row.get("config"))
        orchestrator_cfg = _merge_string_fields(
            _as_dict(config_blob.get("orchestrator")),
            _as_dict(agents_config.get("orchestrator")),
        )
        autonomy_cfg = _merge_string_fields(
            _as_dict(orchestrator_cfg.get("autonomy")),
            _as_dict(agents_config.get("autonomy")),
        )

        if autonomy_cfg:
            policy["autonomy_mode"] = (
                str(
                    autonomy_cfg.get("default_mode", policy["autonomy_mode"])
                    or policy["autonomy_mode"]
                ).strip()
                or policy["autonomy_mode"]
            )
            policy["autonomy_confidence_threshold"] = max(
                0.0,
                min(
                    1.0,
                    _coerce_float(
                        autonomy_cfg.get(
                            "min_confidence",
                            autonomy_cfg.get(
                                "assisted_min_confidence", policy["autonomy_confidence_threshold"]
                            ),
                        ),
                        policy["autonomy_confidence_threshold"],
                    ),
                ),
            )

        if orchestrator_cfg:
            policy["concurrency_cap"] = max(
                1,
                _coerce_int(
                    orchestrator_cfg.get(
                        "concurrency_cap",
                        orchestrator_cfg.get("max_parallel", policy["concurrency_cap"]),
                    ),
                    policy["concurrency_cap"],
                ),
            )

        return policy

    def get_project_policy(self, project_id: str, project_row: dict | None = None) -> dict:
        if not self._table_exists("project_policies"):
            return self._normalize_project_policy(
                self._derive_project_policy_from_row(project_row or self.get_project(project_id))
            )

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM project_policies WHERE project_id = %s", (project_id,))
            row = cur.fetchone()
        if not row:
            return self._normalize_project_policy(
                self._derive_project_policy_from_row(project_row or self.get_project(project_id))
            )
        return self._normalize_project_policy(dict(row))

    @staticmethod
    def _save_project_policy_cur(cur, *, project_id: str, policy: dict | None = None) -> None:
        normalized = PlatformLoggerPolicyProjectsMixin._normalize_project_policy(
            policy, strict=True
        )
        cur.execute(
            """
            INSERT INTO project_policies (
                project_id,
                autonomy_mode,
                autonomy_confidence_threshold,
                retry_strategy,
                retry_max_attempts,
                retry_backoff_initial_seconds,
                retry_backoff_max_seconds,
                concurrency_cap,
                tool_policy_mode
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (project_id) DO UPDATE SET
                autonomy_mode = EXCLUDED.autonomy_mode,
                autonomy_confidence_threshold = EXCLUDED.autonomy_confidence_threshold,
                retry_strategy = EXCLUDED.retry_strategy,
                retry_max_attempts = EXCLUDED.retry_max_attempts,
                retry_backoff_initial_seconds = EXCLUDED.retry_backoff_initial_seconds,
                retry_backoff_max_seconds = EXCLUDED.retry_backoff_max_seconds,
                concurrency_cap = EXCLUDED.concurrency_cap,
                tool_policy_mode = EXCLUDED.tool_policy_mode
            """,
            (
                project_id,
                normalized["autonomy_mode"],
                normalized["autonomy_confidence_threshold"],
                normalized["retry_strategy"],
                normalized["retry_max_attempts"],
                normalized["retry_backoff_initial_seconds"],
                normalized["retry_backoff_max_seconds"],
                normalized["concurrency_cap"],
                normalized["tool_policy_mode"],
            ),
        )

    def save_project_policy(self, project_id: str, policy: dict | None = None) -> None:
        if not self._table_exists("project_policies"):
            return
        with self.conn.cursor() as cur:
            self._save_project_policy_cur(cur, project_id=project_id, policy=policy)

    def save_project_workflows(
        self,
        project_id: str,
        workflow_ids: list[str],
        operator_default_workflow_id: str | None = None,
    ) -> None:
        if not self._table_exists("project_workflows"):
            return
        with self.conn.cursor() as cur:
            self._sync_project_workflows_cur(
                cur,
                project_id=project_id,
                project_workflow_ids=list(workflow_ids or []),
                operator_default_workflow_id=str(operator_default_workflow_id or "").strip(),
            )

    def add_project_workflow_binding(self, project_id: str, workflow_id: str) -> None:
        """Add one enabled binding without rewriting any sibling binding."""
        if not self._table_exists("project_workflows"):
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO project_workflows
                   (project_id, workflow_id, is_default, priority, enabled, deleted_at)
                   VALUES (%s, %s, false, 0, true, NULL)
                   ON CONFLICT (project_id, workflow_id) DO UPDATE SET
                       enabled = true,
                       deleted_at = NULL""",
                (project_id, workflow_id),
            )

    def get_project_run_config(self, project_id: Any) -> dict:
        """Return the project config dict shaped for graph state."""
        row = self.get_project(project_id)
        if not row:
            return {}
        agents_config = _as_dict(row.get("agents_config"))
        config_blob = _as_dict(row.get("config"))

        project_workflows = self.get_project_workflows(project_id, enabled_only=True)
        project_policy = self.get_project_policy(project_id, project_row=row)
        project_connections = self.list_project_connections(project_id, enabled_only=True)
        project_data_connections = self.list_project_data_connections(str(project_id))
        project_providers = self.get_project_provider_bindings(
            project_id, project_row=row, enabled_only=True
        )
        active_default_workflow = next(
            (
                str(link.get("workflow_id") or "").strip()
                for link in project_workflows
                if bool(link.get("is_default"))
            ),
            "",
        )
        # Normalized source of truth: project_workflows.is_default.
        operator_default_workflow_id = active_default_workflow or ""
        return {
            "project_name": row["name"],
            "project_id": row["id"],
            "account_id": str(row.get("account_id") or "").strip(),
            "tenant_id": str(row.get("tenant_id") or "").strip(),
            "workspace_id": str(row.get("workspace_id") or "").strip(),
            "agents_config": agents_config,
            "config": config_blob,
            # Per-project agent instructions — injected into every agent's context block
            "project_rules": row.get("project_rules") or "",
            "operator_default_workflow_id": operator_default_workflow_id,
            "project_workflows": project_workflows,
            "project_policy": project_policy,
            "project_connections": project_connections,
            "assigned_data_connections": project_data_connections,
            "data_connection_ids": [
                str(item.get("connection_id") or "").strip()
                for item in project_data_connections
                if str(item.get("connection_id") or "").strip()
            ],
            "project_providers": project_providers,
        }

    @staticmethod
    def _normalize_project_data_connection_row(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row or {})
        connection_id = str(
            payload.get("connection_id") or payload.get("assigned_connection_id") or ""
        ).strip()
        display_name = str(payload.get("display_name") or connection_id or "").strip()
        workspace_id = str(payload.get("workspace_id") or "").strip()
        workspace_display_name = str(payload.get("workspace_display_name") or "").strip()
        enabled = bool(payload.get("enabled", False))
        deleted = bool(payload.get("deleted_at"))
        health_status = str(payload.get("health_status") or "unknown").strip() or "unknown"
        source_kind = str(payload.get("source_kind") or "").strip()
        connector_ref = _as_dict(payload.get("connector_ref_jsonb") or payload.get("connector_ref"))
        metadata = _as_dict(payload.get("metadata_jsonb") or payload.get("metadata"))
        schema_snapshot = _as_dict(
            payload.get("schema_snapshot")
            or metadata.get("last_schema_snapshot")
            or metadata.get("schema_snapshot")
        )
        engine_hint = str(
            payload.get("engine_hint")
            or connector_ref.get("local_connector_id")
            or connector_ref.get("display_name")
            or source_kind
            or ""
        ).strip()
        last_error = str(payload.get("last_error") or "").strip()
        status = "healthy"
        if deleted:
            status = "deleted"
        elif not enabled:
            status = "disabled"
        elif health_status not in {"", "healthy", "ok", "success"}:
            status = health_status
        return {
            "connection_id": connection_id,
            "display_name": display_name or connection_id,
            "workspace_id": workspace_id,
            "workspace_display_name": workspace_display_name,
            "source_kind": source_kind,
            "engine_hint": engine_hint,
            "enabled": enabled and not deleted,
            "health_status": health_status,
            "status": status,
            "is_deleted": deleted,
            "is_assigned": bool(payload.get("is_assigned", True)),
            "last_error": last_error,
            "last_checked_at": payload.get("last_checked_at"),
            "last_schema_refresh_at": payload.get("last_schema_refresh_at"),
            "schema_snapshot": schema_snapshot,
        }

    def list_project_data_connections(self, project_id: str) -> list[dict[str, Any]]:
        if not self._table_exists("project_data_connections"):
            return []
        workspace_join = (
            "LEFT JOIN workspaces w ON w.id = c.workspace_id"
            if self._table_exists("workspaces")
            else ""
        )
        workspace_select = (
            "COALESCE(w.display_name, '') AS workspace_display_name"
            if self._table_exists("workspaces")
            else "'' AS workspace_display_name"
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    pdc.project_id,
                    pdc.connection_id AS assigned_connection_id,
                    pdc.connection_id,
                    pdc.created_at AS assigned_at,
                    c.workspace_id,
                    c.display_name,
                    c.connector_ref_jsonb,
                    c.source_kind,
                    c.enabled,
                    c.health_status,
                    c.last_checked_at,
                    c.last_schema_refresh_at,
                    c.last_error,
                    c.metadata_jsonb,
                    c.deleted_at,
                    {workspace_select}
                FROM project_data_connections pdc
                LEFT JOIN workspace_data_connections c
                  ON c.connection_id = pdc.connection_id
                {workspace_join}
                WHERE pdc.project_id = %s
                ORDER BY pdc.created_at ASC, pdc.connection_id ASC
                """,
                (str(project_id),),
            )
            rows = cur.fetchall() or []
        return [self._normalize_project_data_connection_row(dict(row)) for row in rows]

    def list_workspace_project_data_connection_options(
        self, workspace_id: str
    ) -> list[dict[str, Any]]:
        if not workspace_id or not self._table_exists("workspace_data_connections"):
            return []
        workspace_join = (
            "LEFT JOIN workspaces w ON w.id = c.workspace_id"
            if self._table_exists("workspaces")
            else ""
        )
        workspace_select = (
            "COALESCE(w.display_name, '') AS workspace_display_name"
            if self._table_exists("workspaces")
            else "'' AS workspace_display_name"
        )
        deleted_clause = (
            "AND c.deleted_at IS NULL"
            if self._column_exists("workspace_data_connections", "deleted_at")
            else ""
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    c.connection_id,
                    c.workspace_id,
                    c.display_name,
                    c.connector_ref_jsonb,
                    c.source_kind,
                    c.enabled,
                    c.health_status,
                    c.last_checked_at,
                    c.last_schema_refresh_at,
                    c.last_error,
                    c.metadata_jsonb,
                    c.deleted_at,
                    {workspace_select}
                FROM workspace_data_connections c
                {workspace_join}
                WHERE c.workspace_id = %s
                {deleted_clause}
                ORDER BY c.display_name ASC, c.connection_id ASC
                """,
                (str(workspace_id),),
            )
            rows = cur.fetchall() or []
        return [self._normalize_project_data_connection_row(dict(row)) for row in rows]

    def set_project_data_connections(
        self,
        project_id: str,
        connection_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        if not self._table_exists("project_data_connections"):
            return []

        normalized_project_id = str(project_id or "").strip()
        requested_ids: list[str] = []
        seen_ids: set[str] = set()
        for raw_id in connection_ids or []:
            connection_id = str(raw_id or "").strip()
            if not connection_id:
                continue
            if connection_id in seen_ids:
                raise ValueError(f"Duplicate data connection id '{connection_id}'")
            seen_ids.add(connection_id)
            requested_ids.append(connection_id)

        project_row = self.get_project(normalized_project_id, include_trashed=True)
        if not isinstance(project_row, dict):
            raise ValueError("Project not found")
        project_workspace_id = str(project_row.get("workspace_id") or "").strip()
        if not project_workspace_id:
            raise ValueError("Project workspace is not configured")

        available_rows = self.list_workspace_project_data_connection_options(project_workspace_id)
        available_by_id = {
            str(row.get("connection_id") or "").strip(): dict(row)
            for row in available_rows
            if str(row.get("connection_id") or "").strip()
        }
        missing_ids = [
            connection_id for connection_id in requested_ids if connection_id not in available_by_id
        ]
        if missing_ids:
            raise ValueError(
                "Unknown or cross-workspace data connection ids: " + ", ".join(sorted(missing_ids))
            )

        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM project_data_connections WHERE project_id = %s",
                (normalized_project_id,),
            )
            for connection_id in requested_ids:
                cur.execute(
                    """
                    INSERT INTO project_data_connections (project_id, connection_id)
                    VALUES (%s, %s)
                    ON CONFLICT (project_id, connection_id) DO NOTHING
                    """,
                    (normalized_project_id, connection_id),
                )
        return self.list_project_data_connections(normalized_project_id)

    def get_project_workflows(
        self,
        project_id: str,
        enabled_only: bool = True,
        *,
        include_deleted: bool = True,
    ) -> list[dict]:
        """Return project_workflows rows for a project (best-effort).

        include_deleted stays True by default because one caller genuinely wants
        tombstoned rows: resolving the display name of a job that ran on a
        binding since removed. The operator-facing binding list passes False, so
        a removed binding stops being listed instead of lingering forever as a
        row nothing can get rid of.
        """
        if not self._table_exists("project_workflows"):
            return []
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            current_filters: list[str] = []
            if enabled_only:
                current_filters.extend(["pw.enabled = true", "pw.deleted_at IS NULL"])
            elif not include_deleted:
                current_filters.append("pw.deleted_at IS NULL")
            if enabled_only or not include_deleted:
                current_filters.append("rw.deleted_at IS NULL")
            where_current = "".join(f" AND {condition}" for condition in current_filters)
            cur.execute(
                f"""
                SELECT
                    pw.project_id,
                    pw.workflow_id,
                    rw.display_name AS workflow_display_name,
                    pw.is_default,
                    pw.priority,
                    pw.enabled,
                    pw.deleted_at,
                    rw.deleted_at AS workflow_deleted_at,
                    COALESCE(rw.settings ? 'hook_storage', false) AS hook_storage
                FROM project_workflows pw
                JOIN registry_workflows rw ON rw.workflow_id = pw.workflow_id
                WHERE pw.project_id = %s
                {where_current}
                ORDER BY pw.is_default DESC, pw.priority DESC, pw.workflow_id
                """,
                (project_id,),
            )
            return cur.fetchall()

    @staticmethod
    def _sync_project_workflows_cur(
        cur,
        *,
        project_id: str,
        project_workflow_ids: list[str],
        operator_default_workflow_id: str,
    ) -> None:
        normalized_ids = [
            str(wf or "").strip() for wf in project_workflow_ids if str(wf or "").strip()
        ]
        deduped_ids = list(dict.fromkeys(normalized_ids))
        # Removal must tombstone, not merely disable. The insert path below
        # clears deleted_at on re-add, so the round trip works; without the
        # tombstone a removed binding stayed listed for good and the operator
        # had no way to get rid of the row.
        # A hook's storage workflow is never listed to the operator, so a saved
        # selection never names it: its link to the project stays (R2-11).
        # Both parts are module constants; only %s parameters carry values.
        remove_sql = (
            "UPDATE project_workflows"  # nosec B608
            " SET enabled = false, is_default = false, deleted_at = NOW()"
            " WHERE project_id = %s AND deleted_at IS NULL" + KEEP_HOOK_STORAGE_LINKS_SQL
        )
        if not deduped_ids:
            cur.execute(remove_sql, (project_id,))
            return

        cur.execute(remove_sql + " AND workflow_id <> ALL(%s)", (project_id, deduped_ids))
        for idx, workflow_id in enumerate(deduped_ids):
            cur.execute(
                """INSERT INTO project_workflows
                   (project_id, workflow_id, is_default, priority, enabled, deleted_at)
                   VALUES (%s, %s, false, %s, true, NULL)
                   ON CONFLICT (project_id, workflow_id) DO UPDATE SET
                       priority = EXCLUDED.priority,
                       enabled = true,
                       deleted_at = NULL""",
                (project_id, workflow_id, max(0, len(deduped_ids) - idx)),
            )
        cur.execute(
            "UPDATE project_workflows SET is_default = false WHERE project_id = %s",
            (project_id,),
        )
        if operator_default_workflow_id:
            cur.execute(
                """UPDATE project_workflows
                   SET is_default = true
                   WHERE project_id = %s
                     AND workflow_id = %s
                     AND enabled = true
                     AND deleted_at IS NULL""",
                (project_id, operator_default_workflow_id),
            )

    def update_project(
        self,
        project_id: str,
        name: str,
        description: str | None = None,
        agents_config: dict | None = None,
        project_rules: str | None = None,
        project_policy: dict | None = None,
        workspace_id: str | None = None,
        project_workflow_ids: list[str] | None = None,
        operator_default_workflow_id: str | None = None,
    ) -> None:
        """Update all project config fields."""
        normalized_description = str(description or "").strip() or None
        has_workspace_id = bool(self._column_exists("projects", "workspace_id"))
        set_clauses = [
            "name=%s",
            "description=%s",
            "agents_config=%s",
            "project_rules=%s",
        ]
        params: list[Any] = [
            name,
            normalized_description or "",
            json.dumps(agents_config or {}),
            project_rules or None,
        ]
        if has_workspace_id:
            set_clauses.append("workspace_id=%s")
            params.append(str(workspace_id or "").strip())
        params.append(project_id)
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE projects SET {', '.join(set_clauses)} WHERE id=%s",
                tuple(params),
            )
            if self._table_exists("project_policies") and project_policy is not None:
                self._save_project_policy_cur(cur, project_id=project_id, policy=project_policy)
            if project_workflow_ids is not None:
                self._sync_project_workflows_cur(
                    cur,
                    project_id=project_id,
                    project_workflow_ids=project_workflow_ids,
                    operator_default_workflow_id=str(operator_default_workflow_id or "").strip(),
                )

    def update_project_agents_config(self, project_id: str, agents_config: dict | None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE projects SET agents_config = %s::jsonb WHERE id = %s",
                (json.dumps(_as_dict(agents_config)), str(project_id)),
            )

    def delete_project(self, project_id: str) -> None:
        """Delete a project. Raises IntegrityError if jobs exist."""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
