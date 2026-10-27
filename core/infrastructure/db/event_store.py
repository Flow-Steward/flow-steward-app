"""Reusable helpers for event persistence, retry, and stream queries."""

from __future__ import annotations

import json
import logging
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.psycopg_runtime import errorcodes

_MESSAGE_E_JOB_ID_S = "e.job_id = %s"
_MESSAGE_E_EVENT_TYPE_S = "e.event_type = %s"
_TOKEN_WORKFLOW_RUN_STATE = "workflow_run.state"
_MESSAGE_J_PROJECT_ID_S = "j.project_id = %s"

_MISSING_JOB_EVENT_QUEUE_REASON = "Dropped queued event for missing/deleted job"
_PUBLIC_STATE_MARKER_REVISION_SQL = "COALESCE((e.detail ->> 'revision')::bigint, 0)"
_PUBLIC_STATE_MARKER_AFTER_CURSOR_SQL = """
COALESCE((e.detail ->> 'revision')::bigint, 0) > (
    SELECT COALESCE((marker.detail ->> 'revision')::bigint, 0)
    FROM platform_events marker
    JOIN jobs marker_job ON marker.job_id = marker_job.id
    JOIN projects marker_project ON marker_job.project_id = marker_project.id
    WHERE marker.id::text = %s AND marker.job_id = %s
      AND marker.event_type = %s AND marker_project.account_id = %s
)
"""


def _cursor_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return "" if normalized in {"", "0"} else normalized


def _is_missing_job_reference_error(exc: Exception) -> bool:
    diag = getattr(exc, "diag", None)
    code = (
        str(getattr(exc, "pgcode", "") or "").strip().upper()
        or str(getattr(exc, "sqlstate", "") or "").strip().upper()
        or str(getattr(diag, "sqlstate", "") or "").strip().upper()
    )
    constraint_name = str(getattr(diag, "constraint_name", "") or "").strip()
    if constraint_name == "platform_events_job_id_fkey":
        return True
    message = str(exc)
    if "job_id" in message and 'not present in table "jobs"' in message:
        return True
    if "invalid input syntax for type uuid" in message:
        return True
    return code == errorcodes.FOREIGN_KEY_VIOLATION and "platform_events_job_id_fkey" in message


def insert_event_row(
    *,
    conn,
    job_id: str,
    phase: str,
    agent: str,
    event_type: str,
    title: str,
    detail: dict | None = None,
    severity: str = "info",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO platform_events (job_id, phase, agent, event_type, severity, title, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                job_id,
                phase,
                agent,
                event_type,
                severity,
                title,
                json.dumps(detail) if detail else None,
            ),
        )


def log_event(
    *,
    insert_event_row_func,
    logger: logging.Logger,
    job_id: str,
    phase: str,
    agent: str,
    event_type: str,
    title: str,
    detail: dict | None = None,
    severity: str = "info",
) -> bool:
    try:
        insert_event_row_func(
            job_id=job_id,
            phase=phase,
            agent=agent,
            event_type=event_type,
            title=title,
            detail=detail,
            severity=severity,
        )
        return True
    except Exception as exc:
        if _is_missing_job_reference_error(exc):
            logger.debug(
                "log_event dropped for missing/deleted job_id=%s event_type=%s: %s",
                job_id,
                event_type,
                exc,
            )
            return True
        logger.debug("log_event failed (non-fatal): %s", exc)
        return False


def queue_event_for_retry(
    *,
    conn,
    logger: logging.Logger,
    payload: dict,
    failure_reason: str = "",
) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_event_retry_queue (payload, failure_reason)
                VALUES (%s, %s)
                """,
                (
                    json.dumps(payload),
                    (failure_reason or "")[:2000] or None,
                ),
            )
        return True
    except Exception as exc:
        logger.debug("queue_event_for_retry failed (non-fatal): %s", exc)
        return False


def replay_queued_events(
    *,
    conn,
    insert_event_row_func,
    logger: logging.Logger,
    max_events: int = 200,
) -> int:
    if max_events <= 0:
        return 0
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, payload
                FROM platform_event_retry_queue
                WHERE sent_at IS NULL
                ORDER BY enqueued_at ASC, id ASC
                LIMIT %s
                """,
                (max_events,),
            )
            queued_rows = cur.fetchall()
    except Exception as exc:
        logger.debug("replay_queued_events select failed (non-fatal): %s", exc)
        return 0

    replayed = 0
    for row in queued_rows:
        row_id = row.get("id")
        payload = row.get("payload") if isinstance(row, dict) else None
        try:
            if not isinstance(payload, dict):
                raise ValueError("queued payload is not an object")
            insert_event_row_func(
                job_id=str(payload["job_id"]),
                phase=str(payload["phase"]),
                agent=str(payload["agent"]),
                event_type=str(payload["event_type"]),
                title=str(payload["title"]),
                detail=payload.get("detail"),
                severity=str(payload.get("severity", "info") or "info"),
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE platform_event_retry_queue
                    SET sent_at = NOW(), last_retry_at = NOW()
                    WHERE id = %s
                    """,
                    (row_id,),
                )
            replayed += 1
        except Exception as replay_err:
            try:
                with conn.cursor() as cur:
                    if _is_missing_job_reference_error(replay_err):
                        cur.execute(
                            """
                            UPDATE platform_event_retry_queue
                            SET sent_at = NOW(),
                                last_retry_at = NOW(),
                                failure_reason = %s
                            WHERE id = %s
                            """,
                            (
                                f"{_MISSING_JOB_EVENT_QUEUE_REASON}: {str(replay_err)[:1900]}",
                                row_id,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE platform_event_retry_queue
                            SET retry_count = retry_count + 1,
                                last_retry_at = NOW(),
                                failure_reason = %s
                            WHERE id = %s
                            """,
                            (str(replay_err)[:2000], row_id),
                        )
            except Exception as update_err:
                logger.debug("replay_queued_events update failed (non-fatal): %s", update_err)
    return replayed


def get_events(
    *,
    conn,
    logger: logging.Logger,
    job_id: str,
    since_id: Any = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            params: list[Any] = [job_id]
            cursor_id = _cursor_id(since_id)
            if cursor_id:
                query = """
                SELECT id, timestamp, phase, agent, event_type, severity, title, detail
                  FROM platform_events
                 WHERE job_id = %s
                   AND (timestamp, id::text) > (
                        (SELECT timestamp FROM platform_events WHERE id::text = %s),
                        %s
                   )
                 ORDER BY timestamp ASC, id ASC
                 LIMIT %s
                """
                params.extend([cursor_id, cursor_id])
            else:
                query = """
                SELECT id, timestamp, phase, agent, event_type, severity, title, detail
                  FROM platform_events
                 WHERE job_id = %s
                 ORDER BY timestamp ASC, id ASC
                 LIMIT %s
                """
            params.append(limit)
            cur.execute(query, tuple(params))
            return cur.fetchall()
    except Exception as exc:
        logger.debug("get_events failed (non-fatal): %s", exc)
        return []


def get_latest_event(
    *,
    conn,
    logger: logging.Logger,
    sql_job_id_eq: str,
    sql_and: str,
    job_id: str,
    event_types: list[str] | None = None,
) -> dict[str, Any] | None:
    conditions = [sql_job_id_eq]
    params: list[Any] = [job_id]
    if event_types:
        placeholders = ",".join(["%s"] * len(event_types))
        conditions.append(f"event_type IN ({placeholders})")
        params.extend(event_types)
    where_sql = sql_and.join(conditions)
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT id, timestamp, phase, agent, event_type, severity, title, detail
                FROM platform_events
                WHERE {where_sql}
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                tuple(params),
            )
            return cur.fetchone()
    except Exception as exc:
        logger.debug("get_latest_event failed (non-fatal): %s", exc)
        return None


def list_events_since(
    cursor_id: Any,
    account_id: str,
    *,
    conn,
    logger: logging.Logger,
    sql_project_account_id_eq: str,
    sql_and: str,
    project_id: str | None = None,
    event_type: str | None = None,
    allowed_project_ids: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conditions = [sql_project_account_id_eq]
    params: list[Any] = [account_id]
    normalized_cursor_id = _cursor_id(cursor_id)
    if normalized_cursor_id:
        conditions.append(
            """
            (e.timestamp, e.id::text) > (
                (SELECT timestamp FROM platform_events WHERE id::text = %s),
                %s
            )
            """
        )
        params.extend([normalized_cursor_id, normalized_cursor_id])
    if project_id is not None:
        conditions.append("p.id = %s")
        params.append(project_id)
    elif allowed_project_ids:
        conditions.append("p.id = ANY(%s)")
        params.append(allowed_project_ids)
    if event_type:
        conditions.append(_MESSAGE_E_EVENT_TYPE_S)
        params.append(event_type)
    where_sql = sql_and.join(conditions)
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT e.id, e.job_id, e.timestamp, e.phase, e.agent,
                       e.event_type, e.severity, e.title, e.detail,
                       j.project_id
                  FROM platform_events e
                  JOIN jobs j ON e.job_id = j.id
                  JOIN projects p ON j.project_id = p.id
                 WHERE {where_sql}
                 ORDER BY e.timestamp ASC, e.id ASC
                 LIMIT %s
                """,
                (*params, limit),
            )
            return cur.fetchall()
    except Exception as exc:
        logger.debug("list_events_since failed (non-fatal): %s", exc)
        return []


def get_public_state_marker(
    cursor_id: Any,
    job_id: str,
    account_id: str,
    *,
    conn,
    logger: logging.Logger,
    sql_project_account_id_eq: str,
    sql_and: str,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Return one public-state marker, scoped to the already-authorized job.

    Unlike the global UI query above this path is deliberately strict: database
    failures are allowed to propagate so a public stream cannot silently turn a
    broken query into an empty (and potentially misleading) stream.
    """
    del logger
    normalized_cursor = _cursor_id(cursor_id)
    if not normalized_cursor:
        return None
    conditions = [
        "e.id::text = %s",
        _MESSAGE_E_JOB_ID_S,
        sql_project_account_id_eq,
        _MESSAGE_E_EVENT_TYPE_S,
    ]
    params: list[Any] = [normalized_cursor, job_id, account_id, _TOKEN_WORKFLOW_RUN_STATE]
    if project_id is not None:
        conditions.append(_MESSAGE_J_PROJECT_ID_S)
        params.append(project_id)
    where_sql = sql_and.join(conditions)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT e.id, e.job_id, e.timestamp, e.detail, j.project_id
              FROM platform_events e
              JOIN jobs j ON e.job_id = j.id
              JOIN projects p ON j.project_id = p.id
             WHERE {where_sql}
             LIMIT 1
            """,
            tuple(params),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_latest_public_state_marker(
    job_id: str,
    account_id: str,
    *,
    conn,
    logger: logging.Logger,
    sql_project_account_id_eq: str,
    sql_and: str,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest durable marker for one authorized job."""
    del logger
    conditions = [
        _MESSAGE_E_JOB_ID_S,
        sql_project_account_id_eq,
        _MESSAGE_E_EVENT_TYPE_S,
    ]
    params: list[Any] = [job_id, account_id, _TOKEN_WORKFLOW_RUN_STATE]
    if project_id is not None:
        conditions.append(_MESSAGE_J_PROJECT_ID_S)
        params.append(project_id)
    where_sql = sql_and.join(conditions)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT e.id, e.job_id, e.timestamp, e.detail, j.project_id
              FROM platform_events e
              JOIN jobs j ON e.job_id = j.id
              JOIN projects p ON j.project_id = p.id
             WHERE {where_sql}
             ORDER BY {_PUBLIC_STATE_MARKER_REVISION_SQL} DESC, e.timestamp DESC
             LIMIT 1
            """,
            tuple(params),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def list_public_state_markers_since(
    cursor_id: Any,
    job_id: str,
    account_id: str,
    *,
    conn,
    logger: logging.Logger,
    sql_project_account_id_eq: str,
    sql_and: str,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Strictly list durable public-state markers for one authorized job."""
    del logger
    conditions = [
        _MESSAGE_E_JOB_ID_S,
        sql_project_account_id_eq,
        _MESSAGE_E_EVENT_TYPE_S,
    ]
    params: list[Any] = [job_id, account_id, _TOKEN_WORKFLOW_RUN_STATE]
    normalized_cursor = _cursor_id(cursor_id)
    if normalized_cursor:
        conditions.append(_PUBLIC_STATE_MARKER_AFTER_CURSOR_SQL)
        params.extend([normalized_cursor, job_id, _TOKEN_WORKFLOW_RUN_STATE, account_id])
    if project_id is not None:
        conditions.append(_MESSAGE_J_PROJECT_ID_S)
        params.append(project_id)
    where_sql = sql_and.join(conditions)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT e.id, e.job_id, e.timestamp, e.detail, j.project_id
              FROM platform_events e
              JOIN jobs j ON e.job_id = j.id
              JOIN projects p ON j.project_id = p.id
             WHERE {where_sql}
             ORDER BY {_PUBLIC_STATE_MARKER_REVISION_SQL} ASC, e.timestamp ASC
             LIMIT %s
            """,
            (*params, max(1, min(int(limit or 50), 1000))),
        )
        return [dict(row) for row in cur.fetchall()]
