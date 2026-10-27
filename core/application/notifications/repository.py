"""NotificationRepository persistence operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from core.application.workflows.reaction_scopes import (
    merge_scoped_reaction_rules,
    serialize_scoped_reaction_rows,
)
from core.application.workflows.workflow_reaction_scope_repository import (
    require_current_reaction_scopes,
)
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.db.platform_logger_common import (
    ERR_EVENT_TYPE_REQUIRED,
    ERR_PROVIDER_KEY_REQUIRED,
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    SQL_ENABLED_TRUE,
    SQL_WORKFLOW_ID_EQ,
    _as_dict,
    _coerce_bool,
    _coerce_int,
    _default_account_id,
)
from core.infrastructure.db.psycopg_runtime import sql


class NotificationRouteConflictError(ValueError):
    """Two routes claim one key, which is a conflict rather than a bad value.

    Carries `is_conflict` so the service layer can answer 409 without the
    infrastructure layer importing the application's error types.
    """

    is_conflict = True


class NotificationRepositoryMixin:
    conn: Any
    _table_exists: Callable[[str], bool]
    _column_exists: Callable[[str, str], bool]

    @staticmethod
    def _normalize_notification_route_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["route_id"] = (
            str(payload.get("route_id") or payload.get("id") or "").strip() or None
        )
        payload["project_id"] = str(payload.get("project_id") or "").strip() or None
        payload["provider_id"] = str(payload.get("provider_id") or "").strip() or None
        payload["account_id"] = str(payload.get("account_id") or "").strip() or None
        payload["workflow_id"] = str(payload.get("workflow_id") or "").strip() or None
        payload["route_key"] = str(payload.get("route_key") or "").strip()
        payload["display_name"] = str(payload.get("display_name") or "").strip()
        payload["event_type"] = str(payload.get("event_type") or "").strip()
        payload["severity"] = str(payload.get("severity") or "any").strip().lower() or "any"
        payload["route_config"] = _as_dict(
            payload.get("route_config") or payload.get("route_config_jsonb")
        )
        payload["enabled"] = _coerce_bool(payload.get("enabled"), True)
        payload.pop("route_config_jsonb", None)
        payload.pop("id", None)
        return payload

    @staticmethod
    def _normalize_notification_delivery_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["delivery_id"] = (
            str(payload.get("delivery_id") or payload.get("id") or "").strip() or None
        )
        payload["route_id"] = str(payload.get("route_id") or "").strip() or None
        payload["project_id"] = str(payload.get("project_id") or "").strip() or None
        payload["job_id"] = str(payload.get("job_id") or "").strip() or None
        payload["provider_id"] = str(payload.get("provider_id") or "").strip() or None
        payload["workflow_id"] = str(payload.get("workflow_id") or "").strip() or None
        payload["provider_key"] = str(payload.get("provider_key") or "").strip()
        payload["event_type"] = str(payload.get("event_type") or "").strip()
        payload["severity"] = str(payload.get("severity") or "info").strip().lower() or "info"
        payload["status"] = str(payload.get("status") or "pending").strip().lower() or "pending"
        payload["provider_message_id"] = (
            str(payload.get("provider_message_id") or "").strip() or None
        )
        payload["external_effect_id"] = str(payload.get("external_effect_id") or "").strip() or None
        payload["error_text"] = str(payload.get("error_text") or "").strip() or None
        payload["request"] = _as_dict(payload.get("request") or payload.get("request_jsonb"))
        payload["response"] = _as_dict(payload.get("response") or payload.get("response_jsonb"))
        payload.pop("request_jsonb", None)
        payload.pop("response_jsonb", None)
        payload.pop("id", None)
        return payload

    def _resolve_notification_provider_id_cur(self, cur, provider_key: str) -> str:
        normalized_key = str(provider_key or "").strip()
        if not normalized_key:
            raise ValueError(ERR_PROVIDER_KEY_REQUIRED)
        cur.execute(
            """
            SELECT p.id, p.enabled
            FROM providers p
            JOIN provider_types pt ON pt.id = p.provider_type_id
            WHERE p.provider_key = %s
              AND pt.key = 'notification_provider'
            LIMIT 1
            """,
            (normalized_key,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Unknown notification provider_key '{normalized_key}'")
        if isinstance(row, dict):
            provider_id = str(row.get("id") or "").strip()
            enabled = row.get("enabled")
        else:
            provider_id = str(row[0] or "").strip()
            enabled = row[1]
        if enabled is False:
            # A route on a disabled provider is accepted and then never delivers.
            # The operator finds out when a notification does not arrive, which is
            # the worst possible time to be told.
            raise ValueError(
                f"Notification provider '{normalized_key}' is disabled, so a route "
                "cannot deliver through it. Enable its extension, or choose an "
                "available provider."
            )
        return provider_id

    def list_notification_routes(
        self,
        project_id: Any,
        *,
        enabled_only: bool = True,
    ) -> list[dict]:
        if not (self._table_exists("notification_routes") and self._table_exists("providers")):
            return []
        has_account_column = self._column_exists("notification_routes", "account_id")

        conditions = ["nr.project_id = %s"]
        params: list = [project_id]
        if enabled_only:
            conditions.extend(["nr.enabled = true", "p.enabled = true", "pt.enabled = true"])
        where_sql = sql.SQL(SQL_AND).join(sql.SQL(condition) for condition in conditions)
        account_select = sql.SQL(
            "nr.account_id" if has_account_column else "NULL::text AS account_id"
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                sql.SQL("""
                SELECT
                    nr.id AS route_id,
                    nr.project_id,
                    {account_select},
                    nr.route_key,
                    nr.display_name,
                    nr.workflow_id,
                    nr.provider_id,
                    p.provider_key,
                    pt.key AS provider_type_key,
                    p.display_name AS provider_display_name,
                    p.implementation_kind,
                    p.integration_key,
                    p.config_jsonb AS provider_config,
                    nr.event_type,
                    nr.severity,
                    nr.route_config_jsonb,
                    nr.enabled,
                    nr.created_at,
                    nr.updated_at
                FROM notification_routes nr
                JOIN providers p ON p.id = nr.provider_id
                JOIN provider_types pt ON pt.id = p.provider_type_id
                WHERE {where_sql}
                ORDER BY nr.route_key
                """).format(account_select=account_select, where_sql=where_sql),
                tuple(params),
            )
            rows = cur.fetchall()
        normalized: list[dict] = []
        for row in rows:
            item = self._normalize_notification_route_row(dict(row))
            item["provider_key"] = str((row or {}).get("provider_key") or "").strip()
            item["provider_type_key"] = str((row or {}).get("provider_type_key") or "").strip()
            item["provider_display_name"] = str(
                (row or {}).get("provider_display_name") or ""
            ).strip()
            item["implementation_kind"] = (
                str((row or {}).get("implementation_kind") or "").strip() or "builtin"
            )
            item["integration_key"] = str((row or {}).get("integration_key") or "").strip()
            item["provider_config"] = _as_dict((row or {}).get("provider_config"))
            normalized.append(item)
        return normalized

    _ROUTE_WORKFLOW_INDEX = 2

    def _reject_unknown_route_workflows(self, cur: Any, prepared_rows: list[tuple]) -> None:
        """Refuse a route naming a workflow that does not exist, by name.

        `notification_routes.workflow_id` references `registry_workflows`, so an
        unknown one used to surface as an integrity error, which the service
        layer could only report as "Failed to update notification routes" with
        HTTP 500 — an internal failure for something the operator typed. Checking
        it here makes it what it is: a rejected value, named, before any write.
        """
        requested = sorted(
            {
                str(row[self._ROUTE_WORKFLOW_INDEX])
                for row in prepared_rows
                if row[self._ROUTE_WORKFLOW_INDEX]
            }
        )
        if not requested or not self._table_exists("registry_workflows"):
            return
        cur.execute(
            "SELECT workflow_id FROM registry_workflows WHERE workflow_id = ANY(%s)",
            (requested,),
        )
        known = set()
        for row in cur.fetchall() or []:
            known.add(str(row.get("workflow_id") if isinstance(row, dict) else row[0]))
        missing = [value for value in requested if value not in known]
        if missing:
            raise ValueError(
                "Notification route workflow_id is not a known workflow: " + ", ".join(missing)
            )

    def save_notification_routes(self, project_id: Any, routes: list[dict] | None = None) -> None:
        if not self._table_exists("notification_routes"):
            return
        has_account_column = self._column_exists("notification_routes", "account_id")
        has_project_account_column = self._column_exists("projects", "account_id")
        normalized_routes = [dict(item) for item in (routes or []) if isinstance(item, dict)]
        prepared_rows: list[tuple] = []
        seen_keys: set[str] = set()
        allowed_severities = {"any", "debug", "info", "warning", "error", "critical"}
        for idx, item in enumerate(normalized_routes):
            explicit_key = str(item.get("route_key") or "").strip()
            # Only an absent key is generated. Renaming one the operator typed
            # produces a route they did not ask for, under a name they will not
            # find again — and silently, so nothing tells them it happened.
            route_key = explicit_key or f"route_{idx + 1}"
            if route_key in seen_keys:
                if explicit_key:
                    raise NotificationRouteConflictError(
                        f"Notification route_key '{route_key}' is used more than once. "
                        "Route keys identify a route, so each must be unique."
                    )
                route_key = f"{route_key}_{idx + 1}"
            seen_keys.add(route_key)

            provider_key = str(item.get("provider_key") or "").strip()
            if not provider_key:
                continue
            event_type = str(item.get("event_type") or "").strip()
            if not event_type:
                continue
            severity = str(item.get("severity") or "any").strip().lower() or "any"
            if severity not in allowed_severities:
                severity = "any"
            prepared_rows.append(
                (
                    route_key,
                    str(item.get("display_name") or "").strip(),
                    str(item.get("workflow_id") or "").strip() or None,
                    provider_key,
                    event_type,
                    severity,
                    json.dumps(_as_dict(item.get("route_config"))),
                    _coerce_bool(item.get("enabled"), True),
                )
            )

        with self.conn.cursor() as cur:
            self._reject_unknown_route_workflows(cur, prepared_rows)
            project_account_id = _default_account_id()
            if has_account_column and has_project_account_column:
                cur.execute("SELECT account_id FROM projects WHERE id = %s", (project_id,))
                project_row = cur.fetchone()
                if project_row:
                    if isinstance(project_row, dict):
                        project_account_id = (
                            str(project_row.get("account_id") or _default_account_id()).strip()
                            or _default_account_id()
                        )
                    else:
                        project_account_id = (
                            str(project_row[0] or _default_account_id()).strip()
                            or _default_account_id()
                        )
            for row in prepared_rows:
                provider_id = self._resolve_notification_provider_id_cur(cur, row[3])
                if has_account_column:
                    cur.execute(
                        """
                        INSERT INTO notification_routes (
                            project_id,
                            account_id,
                            route_key,
                            display_name,
                            workflow_id,
                            provider_id,
                            event_type,
                            severity,
                            route_config_jsonb,
                            enabled
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (project_id, route_key) DO UPDATE SET
                            account_id = EXCLUDED.account_id,
                            display_name = EXCLUDED.display_name,
                            workflow_id = EXCLUDED.workflow_id,
                            provider_id = EXCLUDED.provider_id,
                            event_type = EXCLUDED.event_type,
                            severity = EXCLUDED.severity,
                            route_config_jsonb = EXCLUDED.route_config_jsonb,
                            enabled = EXCLUDED.enabled
                        """,
                        (
                            project_id,
                            project_account_id,
                            row[0],
                            row[1],
                            row[2],
                            provider_id,
                            row[4],
                            row[5],
                            row[6],
                            row[7],
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO notification_routes (
                            project_id,
                            route_key,
                            display_name,
                            workflow_id,
                            provider_id,
                            event_type,
                            severity,
                            route_config_jsonb,
                            enabled
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (project_id, route_key) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            workflow_id = EXCLUDED.workflow_id,
                            provider_id = EXCLUDED.provider_id,
                            event_type = EXCLUDED.event_type,
                            severity = EXCLUDED.severity,
                            route_config_jsonb = EXCLUDED.route_config_jsonb,
                            enabled = EXCLUDED.enabled
                        """,
                        (
                            project_id,
                            row[0],
                            row[1],
                            row[2],
                            provider_id,
                            row[4],
                            row[5],
                            row[6],
                            row[7],
                        ),
                    )
            if prepared_rows:
                keep_keys = [row[0] for row in prepared_rows]
                cur.execute(
                    """
                    DELETE FROM notification_routes
                    WHERE project_id = %s
                      AND route_key <> ALL(%s)
                    """,
                    (project_id, keep_keys),
                )
            else:
                cur.execute("DELETE FROM notification_routes WHERE project_id = %s", (project_id,))

    def log_notification_delivery(
        self,
        *,
        project_id: Any,
        event_type: str,
        severity: str = "info",
        status: str = "pending",
        route_id: str | None = None,
        workflow_id: str | None = None,
        job_id: Any | None = None,
        provider_key: str | None = None,
        provider_message_id: str | None = None,
        request: dict | None = None,
        response: dict | None = None,
        error_text: str | None = None,
        external_effect_id: str | None = None,
    ) -> dict | None:
        if not self._table_exists("notification_delivery_logs"):
            return None
        normalized_event_type = str(event_type or "").strip()
        if not normalized_event_type:
            raise ValueError(ERR_EVENT_TYPE_REQUIRED)
        normalized_status = str(status or "pending").strip().lower() or "pending"
        if normalized_status not in {"pending", "delivered", "failed", "skipped"}:
            normalized_status = "failed"
        normalized_severity = str(severity or "info").strip().lower() or "info"
        if normalized_severity not in {"debug", "info", "warning", "error", "critical"}:
            normalized_severity = "info"

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            provider_id = None
            normalized_provider_key = str(provider_key or "").strip()
            if normalized_provider_key and self._table_exists("providers"):
                cur.execute(
                    "SELECT id FROM providers WHERE provider_key = %s LIMIT 1",
                    (normalized_provider_key,),
                )
                row = cur.fetchone()
                if isinstance(row, dict):
                    provider_id = str(row.get("id") or "").strip() or None
                elif row:
                    provider_id = str(row[0] or "").strip() or None

            columns = [
                "route_id",
                "project_id",
                "workflow_id",
                "job_id",
                "provider_id",
                "event_type",
                "severity",
                "status",
                "request_jsonb",
                "response_jsonb",
                "provider_message_id",
                "error_text",
            ]
            values: list[Any] = [
                str(route_id).strip() if route_id is not None else None,
                project_id,
                str(workflow_id or "").strip() or None,
                str(job_id) if job_id is not None else None,
                provider_id,
                normalized_event_type,
                normalized_severity,
                normalized_status,
                json.dumps(_as_dict(request), default=str),
                json.dumps(_as_dict(response), default=str),
                str(provider_message_id or "").strip() or None,
                str(error_text or "").strip() or None,
            ]
            returning_columns = [
                "id AS delivery_id",
                "route_id",
                "project_id",
                "workflow_id",
                "job_id",
                "provider_id",
                "event_type",
                "severity",
                "status",
                "request_jsonb",
                "response_jsonb",
                "provider_message_id",
                "error_text",
            ]
            if self._column_exists("notification_delivery_logs", "external_effect_id"):
                columns.append("external_effect_id")
                values.append(str(external_effect_id or "").strip() or None)
                returning_columns.append("external_effect_id")
            returning_columns.append("created_at")
            cur.execute(
                sql.SQL("""
                INSERT INTO notification_delivery_logs (
                    {columns}
                ) VALUES ({values})
                RETURNING
                    {returning_columns}
                """).format(
                    columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                    values=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                    returning_columns=sql.SQL(", ").join(
                        [
                            sql.SQL("id AS delivery_id"),
                            *(sql.Identifier(column) for column in returning_columns[1:]),
                        ]
                    ),
                ),
                tuple(values),
            )
            saved = cur.fetchone()
        row = self._normalize_notification_delivery_row(dict(saved or {}))
        if normalized_provider_key and not row.get("provider_key"):
            row["provider_key"] = normalized_provider_key
        return row

    def list_notification_delivery_logs(
        self,
        *,
        project_id: Any | None = None,
        job_id: Any | None = None,
        limit: int = 200,
    ) -> list[dict]:
        if not self._table_exists("notification_delivery_logs"):
            return []
        conditions: list[str] = []
        params: list = []
        if project_id is not None:
            conditions.append("ndl.project_id = %s")
            params.append(project_id)
        if job_id is not None:
            conditions.append("ndl.job_id = %s")
            params.append(str(job_id))
        where_sql = (
            sql.SQL("WHERE ")
            + sql.SQL(SQL_AND).join(sql.SQL(condition) for condition in conditions)
            if conditions
            else sql.SQL("")
        )
        external_effect_select = sql.SQL(
            "ndl.external_effect_id,"
            if self._column_exists("notification_delivery_logs", "external_effect_id")
            else "NULL AS external_effect_id,"
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                sql.SQL("""
                SELECT
                    ndl.id AS delivery_id,
                    ndl.route_id,
                    ndl.project_id,
                    ndl.workflow_id,
                    ndl.job_id,
                    ndl.provider_id,
                    p.provider_key,
                    ndl.event_type,
                    ndl.severity,
                    ndl.status,
                    ndl.request_jsonb,
                    ndl.response_jsonb,
                    ndl.provider_message_id,
                    ndl.error_text,
                    {external_effect_select}
                    ndl.created_at
                FROM notification_delivery_logs ndl
                LEFT JOIN providers p ON p.id = ndl.provider_id
                {where_sql}
                ORDER BY ndl.created_at DESC, ndl.id DESC
                LIMIT %s
                """).format(external_effect_select=external_effect_select, where_sql=where_sql),
                (*params, max(1, int(limit or 1))),
            )
            rows = cur.fetchall()
        return [self._normalize_notification_delivery_row(dict(row)) for row in rows]

    @staticmethod
    def _normalize_workflow_reaction_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["conditions"] = _as_dict(
            payload.get("conditions") or payload.get("conditions_jsonb")
        )
        payload["action"] = _as_dict(payload.get("action") or payload.get("action_jsonb"))
        payload["enabled"] = _coerce_bool(payload.get("enabled"), True)
        payload["debounce_seconds"] = max(0, _coerce_int(payload.get("debounce_seconds"), 0))
        payload["reaction_key"] = str(payload.get("reaction_key") or "").strip()
        payload["display_name"] = str(payload.get("display_name") or "").strip()
        payload["event_type"] = str(payload.get("event_type") or "").strip()
        payload["idempotency_key_template"] = (
            str(payload.get("idempotency_key_template") or "").strip() or None
        )
        payload.pop("conditions_jsonb", None)
        payload.pop("action_jsonb", None)
        return payload

    def list_workflow_reactions(
        self,
        workflow_id: str,
        enabled_only: bool = False,
        account_id: str | None = None,
    ) -> list[dict]:
        if not self._table_exists("workflow_reactions"):
            return []
        has_account_column = self._column_exists("workflow_reactions", "account_id")
        conditions = [SQL_WORKFLOW_ID_EQ]
        params: list = [workflow_id]
        if has_account_column:
            conditions.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id or _default_account_id()).strip() or _default_account_id())
        if enabled_only:
            conditions.append(SQL_ENABLED_TRUE)
        where_sql = sql.SQL("WHERE ") + sql.SQL(SQL_AND).join(
            sql.SQL(condition) for condition in conditions
        )
        account_select = (
            sql.SQL("account_id")
            if has_account_column
            else sql.SQL("{}::text AS account_id").format(sql.Literal(_default_account_id()))
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                sql.SQL("""
                SELECT
                    id,
                    {account_select},
                    workflow_id,
                    reaction_key,
                    display_name,
                    event_type,
                    conditions_jsonb,
                    action_jsonb,
                    debounce_seconds,
                    idempotency_key_template,
                    enabled,
                    created_at,
                    updated_at
                FROM workflow_reactions
                {where_sql}
                ORDER BY enabled DESC, event_type, reaction_key
                """).format(account_select=account_select, where_sql=where_sql),
                tuple(params),
            )
            rows = cur.fetchall()
        return [self._normalize_workflow_reaction_row(dict(row)) for row in rows]

    def list_workflow_reaction_scope_names(self, workflow_id: str) -> dict[str, dict[str, str]]:
        """Display names of the groups and steps a workflow's rules can be scoped to.

        Read from the same tables that validate a rule's scope, so a name exists
        for every scope a saved rule can carry.
        """
        names: dict[str, dict[str, str]] = {"group": {}, "step": {}}
        queries = (
            (
                "group",
                "registry_workflow_groups",
                "SELECT group_id, display_name FROM registry_workflow_groups WHERE workflow_id = %s",
            ),
            (
                "step",
                "registry_workflow_steps",
                "SELECT step_id, display_name FROM registry_workflow_steps "
                "WHERE owner_workflow_id = %s",
            ),
        )
        with self.conn.cursor() as cur:
            for kind, table, query in queries:
                if not self._table_exists(table):
                    continue
                cur.execute(query, (workflow_id,))
                for scope_id, display_name in cur.fetchall():
                    key = str(scope_id or "").strip()
                    if key:
                        names[kind][key] = str(display_name or "").strip() or key
        return names

    def replace_scoped_workflow_reactions(
        self,
        workflow_id: str,
        reactions: list[dict],
        *,
        scope_kind: str = "workflow",
        scope_id: str = "",
        account_id: str | None = None,
    ) -> tuple[str, str, list[dict]]:
        """Replace one scope against current persisted rules under the workflow lock."""
        if not self._table_exists("workflow_reactions"):
            raise RuntimeError("Workflow reaction storage is not available")
        with transaction_connection(self.conn) as conn:
            # Lock the workflow even when it has no rules yet. Definition writes
            # use this same lock, before reading or validating reaction scopes.
            with conn.cursor() as cur:
                require_current_reaction_scopes(cur, workflow_id, [])
            existing = self.list_workflow_reactions(
                workflow_id, enabled_only=False, account_id=account_id
            )
            target_kind, target_scope_id, merged = merge_scoped_reaction_rules(
                existing, reactions, scope_kind=scope_kind, scope_id=scope_id
            )
            # Reuse full replacement's normalization, scope checks and SQL;
            # transaction_connection pins these nested calls to this connection.
            self.save_workflow_reactions(workflow_id, merged, account_id=account_id)
            return serialize_scoped_reaction_rows(
                self.list_workflow_reactions(
                    workflow_id, enabled_only=False, account_id=account_id
                ),
                scope_kind=target_kind,
                scope_id=target_scope_id,
            )

    def save_workflow_reactions(
        self,
        workflow_id: str,
        reactions: list[dict] | None = None,
        account_id: str | None = None,
    ) -> None:
        if not self._table_exists("workflow_reactions"):
            return
        has_account_column = self._column_exists("workflow_reactions", "account_id")
        normalized_account_id = (
            str(account_id or _default_account_id()).strip() or _default_account_id()
        )
        normalized_reactions = [dict(item) for item in (reactions or []) if isinstance(item, dict)]
        prepared_rows: list[tuple] = []
        seen_keys: set[str] = set()
        for idx, item in enumerate(normalized_reactions):
            raw_key = str(item.get("reaction_key") or "").strip() or f"reaction_{idx + 1}"
            reaction_key = raw_key
            if reaction_key in seen_keys:
                reaction_key = f"{raw_key}_{idx + 1}"
            seen_keys.add(reaction_key)
            display_name = str(item.get("display_name") or "").strip()
            event_type = str(item.get("event_type") or "").strip()
            if not event_type:
                continue
            if has_account_column:
                prepared_rows.append(
                    (
                        normalized_account_id,
                        workflow_id,
                        reaction_key,
                        display_name,
                        event_type,
                        json.dumps(_as_dict(item.get("conditions"))),
                        json.dumps(_as_dict(item.get("action"))),
                        max(0, _coerce_int(item.get("debounce_seconds"), 0)),
                        str(item.get("idempotency_key_template") or "").strip() or None,
                        _coerce_bool(item.get("enabled"), True),
                    )
                )
            else:
                prepared_rows.append(
                    (
                        workflow_id,
                        reaction_key,
                        display_name,
                        event_type,
                        json.dumps(_as_dict(item.get("conditions"))),
                        json.dumps(_as_dict(item.get("action"))),
                        max(0, _coerce_int(item.get("debounce_seconds"), 0)),
                        str(item.get("idempotency_key_template") or "").strip() or None,
                        _coerce_bool(item.get("enabled"), True),
                    )
                )

        with transaction_connection(self.conn) as conn, conn.cursor() as cur:
            require_current_reaction_scopes(cur, workflow_id, normalized_reactions)
            for row in prepared_rows:
                if has_account_column:
                    cur.execute(
                        """
                        INSERT INTO workflow_reactions (
                            account_id,
                            workflow_id,
                            reaction_key,
                            display_name,
                            event_type,
                            conditions_jsonb,
                            action_jsonb,
                            debounce_seconds,
                            idempotency_key_template,
                            enabled
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (account_id, workflow_id, reaction_key) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            event_type = EXCLUDED.event_type,
                            conditions_jsonb = EXCLUDED.conditions_jsonb,
                            action_jsonb = EXCLUDED.action_jsonb,
                            debounce_seconds = EXCLUDED.debounce_seconds,
                            idempotency_key_template = EXCLUDED.idempotency_key_template,
                            enabled = EXCLUDED.enabled
                        """,
                        row,
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO workflow_reactions (
                            workflow_id,
                            reaction_key,
                            display_name,
                            event_type,
                            conditions_jsonb,
                            action_jsonb,
                            debounce_seconds,
                            idempotency_key_template,
                            enabled
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (workflow_id, reaction_key) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            event_type = EXCLUDED.event_type,
                            conditions_jsonb = EXCLUDED.conditions_jsonb,
                            action_jsonb = EXCLUDED.action_jsonb,
                            debounce_seconds = EXCLUDED.debounce_seconds,
                            idempotency_key_template = EXCLUDED.idempotency_key_template,
                            enabled = EXCLUDED.enabled
                        """,
                        row,
                    )
            if prepared_rows:
                keep_keys = (
                    [row[2] for row in prepared_rows]
                    if has_account_column
                    else [row[1] for row in prepared_rows]
                )
                if has_account_column:
                    cur.execute(
                        """
                        DELETE FROM workflow_reactions
                        WHERE account_id = %s
                          AND workflow_id = %s
                          AND reaction_key <> ALL(%s)
                        """,
                        (normalized_account_id, workflow_id, keep_keys),
                    )
                else:
                    cur.execute(
                        """
                        DELETE FROM workflow_reactions
                        WHERE workflow_id = %s
                          AND reaction_key <> ALL(%s)
                        """,
                        (workflow_id, keep_keys),
                    )
            else:
                if has_account_column:
                    cur.execute(
                        "DELETE FROM workflow_reactions WHERE account_id = %s AND workflow_id = %s",
                        (normalized_account_id, workflow_id),
                    )
                else:
                    cur.execute(
                        "DELETE FROM workflow_reactions WHERE workflow_id = %s", (workflow_id,)
                    )
