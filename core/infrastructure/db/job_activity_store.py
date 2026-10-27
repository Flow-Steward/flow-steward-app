"""Reusable helpers for active job monitoring queries."""

from __future__ import annotations

from core.infrastructure.db import psycopg_runtime as psycopg_runtime


def get_active_jobs(*, conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                j.id,
                j.project_id,
                p.name AS project_name,
                j.task_id,
                j.task_summary,
                j.status,
                j.started_at,
                j.queued_at,
                j.worker_id,
                j.selected_workflow,
                j.current_step_id,
                j.current_group_id,
                j.current_agent,
                j.heartbeat_at,
                j.last_error,
                j.last_response_excerpt
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            WHERE j.status IN (
                'pending', 'resume_pending', 'in_progress',
                'awaiting_approval', 'awaiting_timer', 'awaiting_child'
            )
            ORDER BY
                j.heartbeat_at DESC NULLS LAST,
                j.started_at DESC
            """
        )
        return cur.fetchall()
