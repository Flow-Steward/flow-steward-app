"""Reusable helpers for command audit persistence."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def log_command_execution(
    *,
    conn,
    table_exists,
    column_exists,
    default_account_id,
    as_dict,
    command_id: str,
    command_type: str,
    account_id: str,
    actor_type: str,
    actor_id: str,
    target: dict | None = None,
    payload: dict | None = None,
    status: str = "accepted",
    result: dict | None = None,
    error_text: str | None = None,
    event_id: str | None = None,
    dispatch_id: str | None = None,
) -> dict | None:
    if not table_exists("command_audit_log"):
        return None
    columns = [
        "command_id",
        "command_type",
        "account_id",
        "actor_type",
        "actor_id",
        "target_jsonb",
        "payload_jsonb",
        "status",
        "result_jsonb",
        "error_text",
    ]
    values: list[Any] = [
        str(command_id or "").strip(),
        str(command_type or "").strip(),
        str(account_id or "").strip() or default_account_id(),
        str(actor_type or "system_actor").strip() or "system_actor",
        str(actor_id or "").strip() or "system",
        json.dumps(as_dict(target)),
        json.dumps(as_dict(payload)),
        str(status or "accepted").strip() or "accepted",
        json.dumps(as_dict(result)),
        str(error_text or "").strip() or None,
    ]
    returning_columns = [
        "id",
        "command_id",
        "command_type",
        "account_id",
        "actor_type",
        "actor_id",
        "target_jsonb",
        "payload_jsonb",
        "status",
        "result_jsonb",
        "error_text",
        "created_at",
        "updated_at",
    ]
    has_event_id = column_exists("command_audit_log", "event_id")
    has_dispatch_id = column_exists("command_audit_log", "dispatch_id")
    if has_event_id:
        columns.append("event_id")
        values.append(str(event_id or "").strip() or None)
        returning_columns.append("event_id")
    if has_dispatch_id:
        columns.append("dispatch_id")
        values.append(str(dispatch_id or "").strip() or None)
        returning_columns.append("dispatch_id")
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            INSERT INTO command_audit_log (
                {", ".join(columns)}
            ) VALUES ({placeholders})
            RETURNING
                {", ".join(returning_columns)}
            """,
            tuple(values),
        )
        row = cur.fetchone()
    if not row:
        return None
    payload_row = dict(row)
    payload_row["target"] = as_dict(payload_row.get("target_jsonb"))
    payload_row["payload"] = as_dict(payload_row.get("payload_jsonb"))
    payload_row["result"] = as_dict(payload_row.get("result_jsonb"))
    payload_row.pop("target_jsonb", None)
    payload_row.pop("payload_jsonb", None)
    payload_row.pop("result_jsonb", None)
    return payload_row


def update_command_execution(
    *,
    conn,
    as_dict,
    command_id: str,
    status: str,
    result: dict | None = None,
    error_text: str | None = None,
) -> dict | None:
    """Finalize one accepted command without manufacturing a second audit row."""
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE command_audit_log
               SET status = %s,
                   result_jsonb = %s::jsonb,
                   error_text = %s,
                   updated_at = now()
             WHERE command_id = %s
         RETURNING id, command_id, command_type, account_id, actor_type, actor_id,
                   target_jsonb, payload_jsonb, status, result_jsonb, error_text,
                   created_at, updated_at, event_id, dispatch_id
            """,
            (
                str(status or "failed").strip() or "failed",
                json.dumps(as_dict(result)),
                str(error_text or "").strip() or None,
                str(command_id or "").strip(),
            ),
        )
        row = cur.fetchone()
    if not row:
        return None
    payload_row = dict(row)
    payload_row["target"] = as_dict(payload_row.pop("target_jsonb", None))
    payload_row["payload"] = as_dict(payload_row.pop("payload_jsonb", None))
    payload_row["result"] = as_dict(payload_row.pop("result_jsonb", None))
    return payload_row
