"""Reusable helpers for action log persistence and queries."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.psycopg_runtime import errorcodes

logger = logging.getLogger(__name__)
_LOG_ACTION_SAVEPOINT = "platform_action_log_guard"


def _cursor_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return "" if normalized in {"", "0"} else normalized


def _is_missing_job_reference_error(exc: Exception) -> bool:
    code = str(getattr(exc, "sqlstate", "") or getattr(exc, "pgcode", "") or "").strip().upper()
    if code != errorcodes.FOREIGN_KEY_VIOLATION:
        return False
    diag = getattr(exc, "diag", None)
    constraint_name = str(getattr(diag, "constraint_name", "") or "").strip()
    if constraint_name == "platform_action_log_job_id_fkey":
        return True
    message = str(exc)
    return "job_id" in message and 'not present in table "jobs"' in message


def _connection_has_active_transaction(conn: Any) -> bool:
    transaction_status = getattr(conn, "get_transaction_status", None)
    if callable(transaction_status):
        with contextlib.suppress(Exception):
            return (
                int(transaction_status()) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS
            )
    info = getattr(conn, "info", None)
    status = getattr(info, "transaction_status", None)
    if status is None:
        return False
    with contextlib.suppress(TypeError, ValueError):
        return int(status) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS
    return False


def log_action(
    *,
    conn,
    project_id,
    task_id,
    task_summary,
    agent,
    action_type,
    status=None,
    iteration_no=None,
    cycle_no=None,
    duration=None,
    payload=None,
    result=None,
    error=None,
    meta=None,
    lifecycle_id=None,
    job_id=None,
    tokens_prompt=None,
    tokens_completion=None,
    tokens_total=None,
    cost_usd=None,
    model_used=None,
):
    clean_duration = max(0, int(duration)) if duration is not None else None
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        savepoint_active = False
        if job_id is not None and _connection_has_active_transaction(conn):
            try:
                cur.execute(f"SAVEPOINT {_LOG_ACTION_SAVEPOINT}")
                savepoint_active = True
            except Exception:
                savepoint_active = False
        try:
            cur.execute(
                """
                INSERT INTO platform_action_log
                    (project_id, job_id, task_id, task_summary, lifecycle_id, timestamp,
                     agent, action_type, status, iteration_no, cycle_no, duration,
                     payload, result, error, meta,
                     tokens_prompt, tokens_completion, tokens_total, cost_usd, model_used)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    project_id,
                    job_id,
                    task_id,
                    task_summary,
                    lifecycle_id,
                    datetime.now(UTC),
                    agent,
                    action_type,
                    status,
                    iteration_no,
                    cycle_no,
                    clean_duration,
                    json.dumps(payload) if payload else None,
                    json.dumps(result) if result else None,
                    error,
                    json.dumps(meta) if meta else None,
                    tokens_prompt,
                    tokens_completion,
                    tokens_total,
                    cost_usd,
                    model_used,
                ),
            )
        except Exception as exc:
            if job_id is not None and _is_missing_job_reference_error(exc):
                if savepoint_active:
                    with contextlib.suppress(Exception):
                        cur.execute(f"ROLLBACK TO SAVEPOINT {_LOG_ACTION_SAVEPOINT}")
                    with contextlib.suppress(Exception):
                        cur.execute(f"RELEASE SAVEPOINT {_LOG_ACTION_SAVEPOINT}")
                else:
                    rollback = getattr(conn, "rollback", None)
                    if callable(rollback):
                        with contextlib.suppress(Exception):
                            rollback()
                logger.debug(
                    "log_action dropped for missing/deleted job_id=%s action_type=%s: %s",
                    job_id,
                    action_type,
                    exc,
                )
                return None
            if savepoint_active:
                with contextlib.suppress(Exception):
                    cur.execute(f"ROLLBACK TO SAVEPOINT {_LOG_ACTION_SAVEPOINT}")
                with contextlib.suppress(Exception):
                    cur.execute(f"RELEASE SAVEPOINT {_LOG_ACTION_SAVEPOINT}")
            raise
        row = cur.fetchone()
        if savepoint_active:
            with contextlib.suppress(Exception):
                cur.execute(f"RELEASE SAVEPOINT {_LOG_ACTION_SAVEPOINT}")
        return row


def get_job_cost_breakdown(
    *,
    conn,
    logger: logging.Logger,
    job_id: str,
) -> list[dict[str, Any]]:
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT agent, model_used,
                       COUNT(*)                            AS calls,
                       COALESCE(SUM(tokens_prompt), 0)     AS tokens_prompt,
                       COALESCE(SUM(tokens_completion), 0) AS tokens_completion,
                       COALESCE(SUM(tokens_total), 0)      AS tokens_total,
                       SUM(cost_usd)                       AS cost_usd,
                       COALESCE(SUM(cost_usd), 0)          AS known_cost_usd,
                       COALESCE(SUM(
                           COALESCE(tokens_total, tokens_prompt, 0)
                           + CASE WHEN tokens_total IS NULL THEN COALESCE(tokens_completion, 0) ELSE 0 END
                       ) FILTER (
                           WHERE cost_usd IS NULL
                             AND COALESCE(tokens_total, tokens_prompt, tokens_completion, 0) > 0
                       ), 0)                               AS unknown_cli_tokens,
                       COUNT(*) FILTER (
                           WHERE cost_usd IS NULL
                             AND COALESCE(tokens_total, tokens_prompt, tokens_completion, 0) > 0
                       )                                   AS unknown_cost_calls
                  FROM platform_action_log
                 WHERE job_id = %s AND action_type = 'llm_call'
                 GROUP BY agent, model_used
                 ORDER BY known_cost_usd DESC, unknown_cli_tokens DESC
                """,
                (job_id,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        logger.debug("get_job_cost_breakdown failed (non-fatal): %s", exc)
        return []


def get_logs(
    *,
    conn,
    logger: logging.Logger,
    sql_job_id_eq: str,
    sql_and: str,
    job_id: str,
    since_id: Any = 0,
    limit: int = 200,
    before_id: Any | None = None,
    newest: bool = False,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    try:
        where = [sql_job_id_eq]
        params: list[Any] = [job_id]
        before_cursor = _cursor_id(before_id)
        since_cursor = _cursor_id(since_id)
        if before_cursor:
            where.append(
                """
                (timestamp, id::text) < (
                    (SELECT timestamp FROM platform_action_log WHERE id::text = %s),
                    %s
                )
                """
            )
            params.extend([before_cursor, before_cursor])
        else:
            if since_cursor:
                where.append(
                    """
                    (timestamp, id::text) > (
                        (SELECT timestamp FROM platform_action_log WHERE id::text = %s),
                        %s
                    )
                    """
                )
                params.extend([since_cursor, since_cursor])
        if not include_deleted:
            where.append("(meta->>'_deleted') IS DISTINCT FROM 'true'")
        params.append(limit)
        order_sql = (
            "ORDER BY timestamp DESC, id DESC"
            if newest or before_cursor
            else "ORDER BY timestamp ASC, id ASC"
        )
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    timestamp,
                    agent,
                    action_type,
                    status,
                    duration,
                    payload,
                    result,
                    error,
                    meta,
                    tokens_prompt,
                    tokens_completion,
                    tokens_total,
                    cost_usd,
                    model_used
                  FROM platform_action_log
                 WHERE {sql_and.join(where)}
                 {order_sql}
                 LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
            if newest or before_id is not None:
                rows.reverse()
            return rows
    except Exception as exc:
        logger.debug("get_logs failed (table may not exist yet): %s", exc)
        return []


def get_latest_log_entry(
    *,
    conn,
    logger: logging.Logger,
    sql_job_id_eq: str,
    sql_and: str,
    job_id: str,
    action_types: list[str] | None = None,
    require_error: bool = False,
) -> dict[str, Any] | None:
    conditions = [sql_job_id_eq, "(meta->>'_deleted') IS DISTINCT FROM 'true'"]
    params: list[Any] = [job_id]
    if action_types:
        placeholders = ",".join(["%s"] * len(action_types))
        conditions.append(f"action_type IN ({placeholders})")
        params.extend(action_types)
    if require_error:
        conditions.append("error IS NOT NULL AND error <> ''")
    where_sql = sql_and.join(conditions)
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    timestamp,
                    agent,
                    action_type,
                    status,
                    duration,
                    payload,
                    result,
                    error,
                    meta,
                    tokens_prompt,
                    tokens_completion,
                    tokens_total,
                    cost_usd,
                    model_used
                FROM platform_action_log
                WHERE {where_sql}
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                tuple(params),
            )
            return cur.fetchone()
    except Exception as exc:
        logger.debug("get_latest_log_entry failed (non-fatal): %s", exc)
        return None
