"""Persistence helpers for workflow provider trigger instances."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _row_to_dict(cursor, row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, Mapping):
        return dict(row.items())
    if isinstance(row, (list, tuple)):
        columns = [str(item[0]) for item in (cursor.description or [])]
        if columns and len(columns) == len(row):
            return {columns[idx]: row[idx] for idx in range(len(columns))}
    return {}


def list_workflow_provider_triggers(
    bl,
    *,
    workflow_id: str,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    with bl.conn.cursor() as cur:
        query = """
            SELECT
                trigger_id,
                workflow_id,
                project_id,
                provider_key,
                trigger_type_id,
                display_name,
                enabled,
                event_name,
                trigger_config_jsonb,
                readiness_status,
                validation_errors_jsonb,
                account_id,
                created_at,
                updated_at
            FROM workflow_provider_triggers
            WHERE workflow_id = %s
        """
        params: list[Any] = [str(workflow_id or "").strip()]
        if project_id:
            query += " AND project_id = %s"
            params.append(str(project_id or "").strip())
        query += " ORDER BY created_at ASC, trigger_id ASC"
        cur.execute(query, tuple(params))
        return [_row_to_dict(cur, row) for row in (cur.fetchall() or [])]


def list_enabled_provider_triggers_for_provider(
    bl,
    *,
    provider_key: str,
) -> list[dict[str, Any]]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                trigger_id,
                workflow_id,
                project_id,
                provider_key,
                trigger_type_id,
                display_name,
                enabled,
                event_name,
                trigger_config_jsonb,
                readiness_status,
                validation_errors_jsonb,
                account_id,
                created_at,
                updated_at
            FROM workflow_provider_triggers
            WHERE provider_key = %s
              AND enabled = true
              AND EXISTS (SELECT 1 FROM registry_workflows w
                  WHERE w.workflow_id = workflow_provider_triggers.workflow_id AND w.deleted_at IS NULL)
            ORDER BY created_at ASC, trigger_id ASC
            """,
            (str(provider_key or "").strip().lower(),),
        )
        return [_row_to_dict(cur, row) for row in (cur.fetchall() or [])]


def has_retained_provider_triggers(bl, *, provider_key: str, project_id: str) -> bool:
    """Retained configuration keeps this project in the workflow ingress channel.

    Disabled triggers and deleted owners remain configuration until explicit
    cleanup removes them. Their absence, rather than their enabled state, is
    what permits the independent generic provider flow.
    """
    with bl.conn.cursor() as cursor:
        cursor.execute(
            """SELECT 1 FROM workflow_provider_triggers
               WHERE provider_key = %s AND project_id = %s LIMIT 1""",
            (str(provider_key).strip().lower(), str(project_id).strip()),
        )
        return cursor.fetchone() is not None


def get_workflow_provider_trigger(
    bl,
    *,
    trigger_id: str,
) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                trigger_id,
                workflow_id,
                project_id,
                provider_key,
                trigger_type_id,
                display_name,
                enabled,
                event_name,
                trigger_config_jsonb,
                readiness_status,
                validation_errors_jsonb,
                account_id,
                created_at,
                updated_at
            FROM workflow_provider_triggers
            WHERE trigger_id = %s
            LIMIT 1
            """,
            (str(trigger_id or "").strip(),),
        )
        return _row_to_dict(cur, cur.fetchone())


def create_workflow_provider_trigger(
    bl,
    *,
    trigger_id: str,
    workflow_id: str,
    project_id: str,
    provider_key: str,
    trigger_type_id: str,
    display_name: str,
    enabled: bool,
    event_name: str,
    trigger_config_jsonb: dict[str, Any],
    readiness_status: str,
    validation_errors_jsonb: list[dict[str, Any]],
    account_id: str | None,
) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_provider_triggers (
                trigger_id,
                workflow_id,
                project_id,
                provider_key,
                trigger_type_id,
                display_name,
                enabled,
                event_name,
                trigger_config_jsonb,
                readiness_status,
                validation_errors_jsonb,
                account_id
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb,
                COALESCE(%s::uuid, '00000000-0000-0000-0000-000000000002'::uuid)
            )
            RETURNING
                trigger_id,
                workflow_id,
                project_id,
                provider_key,
                trigger_type_id,
                display_name,
                enabled,
                event_name,
                trigger_config_jsonb,
                readiness_status,
                validation_errors_jsonb,
                account_id,
                created_at,
                updated_at
            """,
            (
                str(trigger_id or "").strip(),
                str(workflow_id or "").strip(),
                str(project_id or "").strip(),
                str(provider_key or "").strip().lower(),
                str(trigger_type_id or "").strip(),
                str(display_name or "").strip(),
                bool(enabled),
                str(event_name or "").strip(),
                json.dumps(dict(trigger_config_jsonb or {})),
                str(readiness_status or "draft").strip() or "draft",
                json.dumps(list(validation_errors_jsonb or [])),
                str(account_id or "").strip() or None,
            ),
        )
        return _row_to_dict(cur, cur.fetchone())


def update_workflow_provider_trigger(
    bl,
    *,
    trigger_id: str,
    project_id: str,
    provider_key: str,
    trigger_type_id: str,
    display_name: str,
    enabled: bool,
    event_name: str,
    trigger_config_jsonb: dict[str, Any],
    readiness_status: str,
    validation_errors_jsonb: list[dict[str, Any]],
    account_id: str,
) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_provider_triggers
               SET provider_key = %s,
                   trigger_type_id = %s,
                   display_name = %s,
                   enabled = %s,
                   event_name = %s,
                   project_id = %s,
                   account_id = %s::uuid,
                   trigger_config_jsonb = %s::jsonb,
                   readiness_status = %s,
                   validation_errors_jsonb = %s::jsonb,
                   updated_at = now()
             WHERE trigger_id = %s
         RETURNING
                trigger_id,
                workflow_id,
                project_id,
                provider_key,
                trigger_type_id,
                display_name,
                enabled,
                event_name,
                trigger_config_jsonb,
                readiness_status,
                validation_errors_jsonb,
                account_id,
                created_at,
                updated_at
            """,
            (
                str(provider_key or "").strip().lower(),
                str(trigger_type_id or "").strip(),
                str(display_name or "").strip(),
                bool(enabled),
                str(event_name or "").strip(),
                str(project_id or "").strip(),
                str(account_id or "").strip(),
                json.dumps(dict(trigger_config_jsonb or {})),
                str(readiness_status or "draft").strip() or "draft",
                json.dumps(list(validation_errors_jsonb or [])),
                str(trigger_id or "").strip(),
            ),
        )
        return _row_to_dict(cur, cur.fetchone())


def delete_workflow_provider_trigger(bl, *, trigger_id: str) -> bool:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM workflow_provider_triggers
            WHERE trigger_id = %s
            """,
            (str(trigger_id or "").strip(),),
        )
        return bool(cur.rowcount)


_SCHEDULE_KIND_SQL = """
    SELECT 1 FROM scheduled_jobs
    WHERE workflow_id = %s AND enabled AND (%s = '' OR account_id::text = %s)
    LIMIT 1
"""
_ENTRYPOINT_KINDS_SQL = """
    SELECT DISTINCT entry_kind FROM workflow_entrypoints
    WHERE workflow_id = %s AND enabled AND (%s = '' OR account_id::text = %s)
    ORDER BY entry_kind
"""
_PROVIDER_TRIGGER_KINDS_SQL = """
    SELECT DISTINCT provider_key, trigger_type_id FROM workflow_provider_triggers
    WHERE workflow_id = %s AND enabled AND (%s = '' OR account_id::text = %s)
    ORDER BY provider_key, trigger_type_id
"""


def list_workflow_trigger_kinds(
    bl,
    *,
    workflow_id: str,
    account_id: str = "",
) -> list[dict[str, Any]]:
    """How an enabled workflow can be started, without any of its configuration.

    Schedules, webhook entrypoints and provider triggers each live in their own
    project-scoped table, so a workflow row alone cannot say how it runs. This
    answers with kinds only: a cron expression, an entry key or a project id belongs
    to one installation and must not travel in an export. An empty account id means
    no account filter.
    """
    normalized_workflow_id = str(workflow_id or "").strip()
    if not normalized_workflow_id:
        return []
    normalized_account_id = str(account_id or "").strip()
    params = (normalized_workflow_id, normalized_account_id, normalized_account_id)
    kinds: list[dict[str, Any]] = []
    with bl.conn.cursor() as cur:
        cur.execute(_SCHEDULE_KIND_SQL, params)
        if cur.fetchone():
            kinds.append({"kind": "schedule"})
        cur.execute(_ENTRYPOINT_KINDS_SQL, params)
        for row in cur.fetchall() or []:
            entry_kind = str(_row_to_dict(cur, row).get("entry_kind") or "").strip()
            if entry_kind:
                kinds.append({"kind": entry_kind})
        cur.execute(_PROVIDER_TRIGGER_KINDS_SQL, params)
        for row in cur.fetchall() or []:
            data = _row_to_dict(cur, row)
            kinds.append(
                {
                    "kind": "provider",
                    "provider_key": str(data.get("provider_key") or "").strip(),
                    "trigger_type_id": str(data.get("trigger_type_id") or "").strip(),
                }
            )
    return kinds
