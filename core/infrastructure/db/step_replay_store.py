"""Reusable helpers for job step replay persistence."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

ALLOWED_REPLAY_MODES = {"prompt_reinvoke", "step_reexecute"}
ALLOWED_REPLAY_STATUSES = {"queued", "running", "succeeded", "failed"}


def _normalize_replay_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower() or "step_reexecute"
    if normalized not in ALLOWED_REPLAY_MODES:
        raise ValueError(f"Unsupported replay_mode '{value}'")
    return normalized


def _normalize_replay_status(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in ALLOWED_REPLAY_STATUSES:
        raise ValueError(f"Unsupported replay status '{value}'")
    return normalized


def create_job_step_replay(
    job_id: str,
    *,
    conn,
    table_exists,
    truncate_text,
    message_id: str | None = None,
    correlation_id: str | None = None,
    requested_by: str = "operator",
    replay_mode: str = "prompt_reinvoke",
    status: str = "running",
    prompt_version: str | None = None,
    policy_snapshot: dict | None = None,
    captured_input: dict | None = None,
    original_outcome: dict | None = None,
) -> dict[str, Any] | None:
    if not table_exists("job_step_replays"):
        return None

    clean_requested_by = truncate_text(requested_by, 120) or "operator"
    clean_replay_mode = _normalize_replay_mode(replay_mode)
    clean_status = _normalize_replay_status(status)
    clean_message_id = str(message_id or "").strip() or None
    clean_correlation_id = truncate_text(correlation_id, 255)
    clean_prompt_version = truncate_text(prompt_version, 120)
    clean_policy_snapshot = policy_snapshot if isinstance(policy_snapshot, dict) else {}
    clean_captured_input = captured_input if isinstance(captured_input, dict) else {}
    clean_original_outcome = original_outcome if isinstance(original_outcome, dict) else {}

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO job_step_replays (
                job_id,
                message_id,
                correlation_id,
                requested_by,
                replay_mode,
                status,
                prompt_version,
                policy_snapshot,
                captured_input,
                original_outcome,
                started_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      CASE WHEN %s = 'running' THEN NOW() ELSE NULL END)
            RETURNING *
            """,
            (
                job_id,
                clean_message_id,
                clean_correlation_id,
                clean_requested_by,
                clean_replay_mode,
                clean_status,
                clean_prompt_version,
                json.dumps(clean_policy_snapshot),
                json.dumps(clean_captured_input),
                json.dumps(clean_original_outcome),
                clean_status,
            ),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def update_job_step_replay(
    replay_id: str,
    *,
    conn,
    table_exists,
    truncate_text,
    status: str,
    replay_outcome: dict | None = None,
    original_outcome: dict | None = None,
    diff_excerpt: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    if not table_exists("job_step_replays"):
        return None

    clean_status = _normalize_replay_status(status)
    clean_replay_outcome = replay_outcome if isinstance(replay_outcome, dict) else {}
    clean_original_outcome = original_outcome if isinstance(original_outcome, dict) else {}
    clean_diff_excerpt = truncate_text(diff_excerpt, 16000)
    clean_error = truncate_text(error_message, 4000)

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE job_step_replays
               SET status = %s,
                   replay_outcome = %s,
                   original_outcome = %s,
                   diff_excerpt = %s,
                   error_message = %s,
                   started_at = CASE
                       WHEN started_at IS NULL AND %s = 'running' THEN NOW()
                       ELSE started_at
                   END,
                   finished_at = CASE
                       WHEN %s IN ('succeeded', 'failed') THEN NOW()
                       ELSE finished_at
                   END
             WHERE id::text = %s
            RETURNING *
            """,
            (
                clean_status,
                json.dumps(clean_replay_outcome),
                json.dumps(clean_original_outcome),
                clean_diff_excerpt,
                clean_error,
                clean_status,
                clean_status,
                str(replay_id),
            ),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def list_job_step_replays(
    job_id: str,
    *,
    conn,
    table_exists,
    sql_job_id_eq: str,
    sql_and: str,
    message_id: str | None = None,
    correlation_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not table_exists("job_step_replays"):
        return []

    where = [sql_job_id_eq]
    params: list[Any] = [job_id]
    if message_id:
        where.append("message_id = %s")
        params.append(str(message_id))
    clean_correlation_id = str(correlation_id or "").strip()
    if clean_correlation_id:
        where.append("correlation_id = %s")
        params.append(clean_correlation_id)
    params.append(max(1, min(int(limit or 50), 500)))

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT *
            FROM job_step_replays
            WHERE {sql_and.join(where)}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]
