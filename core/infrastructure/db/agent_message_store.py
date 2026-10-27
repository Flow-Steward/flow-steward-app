"""Reusable helpers for canonical agent message persistence and queries."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def _cursor_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return "" if normalized in {"", "0"} else normalized


def _json_default(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


def log_agent_message(
    *,
    conn,
    logger: logging.Logger,
    validate_agent_message_payload,
    job_id: str,
    envelope: dict,
) -> bool:
    try:
        model, errors = validate_agent_message_payload(envelope)
        if errors or model is None:
            logger.debug("log_agent_message validation failed: %s", "; ".join(errors))
            return False
        msg = model.model_dump()
        artifacts = msg.get("artifacts") if isinstance(msg.get("artifacts"), dict) else {}
        errors_list = msg.get("errors") if isinstance(msg.get("errors"), list) else []
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_agent_messages
                    (job_id, correlation_id, schema_version, from_agent, to_agent, step_id,
                     step_execution_id, attempt, group_id, for_each_id, skill, status, summary,
                     artifacts, errors, envelope)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    job_id,
                    msg.get("correlation_id"),
                    msg.get("schema_version", "agent_message_v1"),
                    msg.get("from_agent"),
                    msg.get("to_agent"),
                    msg.get("step_id"),
                    msg.get("step_execution_id"),
                    msg.get("attempt"),
                    msg.get("group_id"),
                    msg.get("for_each_id"),
                    msg.get("skill"),
                    msg.get("status"),
                    msg.get("summary", ""),
                    _json_dumps(artifacts),
                    _json_dumps(errors_list),
                    _json_dumps(msg),
                ),
            )
        return True
    except Exception as exc:
        logger.warning("log_agent_message failed (non-fatal): %s", exc)
        return False


def get_agent_messages(
    *,
    conn,
    logger: logging.Logger,
    sql_job_id_eq: str,
    sql_status_eq: str,
    sql_and: str,
    job_id: str,
    since_id: Any = 0,
    limit: int = 500,
    before_id: Any | None = None,
    newest: bool = False,
    step_id: str | None = None,
    from_agent: str | None = None,
    skill: str | None = None,
    status: str | None = None,
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
                    (SELECT timestamp FROM platform_agent_messages WHERE id::text = %s),
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
                        (SELECT timestamp FROM platform_agent_messages WHERE id::text = %s),
                        %s
                    )
                    """
                )
                params.extend([since_cursor, since_cursor])
        if step_id:
            where.append("step_id = %s")
            params.append(step_id)
        if from_agent:
            where.append("from_agent = %s")
            params.append(from_agent)
        if skill:
            where.append("skill = %s")
            params.append(skill)
        if status:
            where.append(sql_status_eq)
            params.append(status)
        params.append(limit)
        order_sql = (
            "ORDER BY timestamp DESC, id DESC"
            if newest or before_cursor
            else "ORDER BY timestamp ASC, id ASC"
        )
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT id, timestamp, job_id, correlation_id, schema_version, from_agent, to_agent,
                       step_id, step_execution_id, attempt, group_id, for_each_id, skill, status, summary, artifacts, errors, envelope
                FROM platform_agent_messages
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
        logger.debug("get_agent_messages failed (table may not exist yet): %s", exc)
        return []


def get_agent_message_by_id(
    *,
    conn,
    logger: logging.Logger,
    message_id: Any,
) -> dict[str, Any] | None:
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, timestamp, job_id, correlation_id, schema_version, from_agent, to_agent,
                       step_id, step_execution_id, attempt, group_id, for_each_id, skill, status, summary, artifacts, errors, envelope
                FROM platform_agent_messages
                WHERE id::text = %s
                LIMIT 1
                """,
                (message_id,),
            )
            return cur.fetchone()
    except Exception as exc:
        logger.debug("get_agent_message_by_id failed (table may not exist yet): %s", exc)
        return None


def get_latest_agent_message(
    *,
    conn,
    logger: logging.Logger,
    sql_job_id_eq: str,
    sql_and: str,
    job_id: str,
    from_agent: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any] | None:
    where = [sql_job_id_eq]
    params: list[Any] = [job_id]
    if from_agent:
        where.append("from_agent = %s")
        params.append(from_agent)
    if step_id:
        where.append("step_id = %s")
        params.append(step_id)
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT id, timestamp, job_id, correlation_id, schema_version, from_agent, to_agent,
                       step_id, step_execution_id, attempt, group_id, for_each_id, skill, status, summary, artifacts, errors, envelope
                FROM platform_agent_messages
                WHERE {sql_and.join(where)}
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                tuple(params),
            )
            return cur.fetchone()
    except Exception as exc:
        logger.debug("get_latest_agent_message failed (non-fatal): %s", exc)
        return None
