"""Admin-oriented job lookup and deletion helpers."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.job_scope_contract import JOB_PROJECT_IDENTITY_CONDITIONS

#: Durable waits (approval, timer, child) are active: the run is not finished.
_ACTIVE_JOB_STATUSES = {
    "pending",
    "resume_pending",
    "in_progress",
    "awaiting_approval",
    "awaiting_timer",
    "awaiting_child",
}
_JOB_CHILD_TABLES = (
    "platform_events",
    "platform_action_log",
    "platform_agent_messages",
    "job_decision_overrides",
    "job_errors",
    "job_step_replays",
    "job_attachments",
)
_LANGGRAPH_CHECKPOINT_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
)


def _normalize_job_id(job_id: object) -> str | None:
    value = str(job_id or "").strip()
    if not value:
        return None
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None


def get_job(conn, *, job_id: str) -> dict | None:
    normalized_job_id = _normalize_job_id(job_id)
    if normalized_job_id is None:
        return None

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                j.*,
                p.name AS project_name,
                rw.display_name AS selected_workflow_display_name,
                COALESCE(rw.settings ? 'hook_storage', false) AS hook_storage,
                aw.display_name AS hook_author_workflow_display_name
            FROM jobs j
            LEFT JOIN projects p ON p.id = j.project_id
            LEFT JOIN registry_workflows rw ON rw.workflow_id = j.selected_workflow
            LEFT JOIN registry_workflows aw
                ON aw.workflow_id = rw.settings -> 'hook_storage' ->> 'author_workflow_id'
            WHERE j.id = %s
            """,
            (normalized_job_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def delete_job(
    conn,
    *,
    table_exists: Callable[[str], bool],
    get_job_func: Callable[[str], dict | None],
    job_id: str,
    allow_active: bool = False,
) -> bool:
    job = get_job_func(job_id)
    if not job:
        return False

    status = str(job.get("status") or "")
    if not allow_active and status in _ACTIVE_JOB_STATUSES:
        raise ValueError(f"Cannot delete active job #{job_id} (status={status})")

    with conn.cursor() as cur:
        thread_id = str(job.get("thread_id") or "").strip()
        if thread_id:
            for table in _LANGGRAPH_CHECKPOINT_TABLES:
                if table_exists(table):
                    cur.execute(
                        f"DELETE FROM {table} WHERE thread_id = %s",
                        (thread_id,),
                    )
        for table in _JOB_CHILD_TABLES:
            if table_exists(table):
                cur.execute(f"DELETE FROM {table} WHERE job_id = %s", (job_id,))
        cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        return bool(cur.rowcount)


def get_awaiting_approval_jobs(
    conn,
    *,
    account_id: str | None = None,
    sql_project_account_id_eq: str,
    sql_and: str,
) -> list[dict]:
    conditions = ["j.status = 'awaiting_approval'", *JOB_PROJECT_IDENTITY_CONDITIONS]
    params: list[str] = []

    normalized_account = str(account_id or "").strip()
    if normalized_account:
        conditions.append(sql_project_account_id_eq)
        params.append(normalized_account)

    where_sql = sql_and.join(conditions)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                j.id,
                LEFT(COALESCE(j.task_summary, ''), 512) AS task_summary,
                j.project_id,
                j.tenant_id,
                j.workspace_id,
                j.account_id,
                p.name AS project_name,
                j.selected_workflow,
                j.started_at,
                j.thread_id
              FROM jobs j
              JOIN projects p ON p.id = j.project_id
             WHERE {where_sql}
             ORDER BY COALESCE(j.started_at, j.queued_at) DESC NULLS LAST, j.id DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def get_pending_jobs(conn, *, project_id: str) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_id, task_summary, task_source, queued_at, queue_name, status, meta
              FROM jobs
             WHERE project_id = %s AND status IN ('pending', 'resume_pending')
             ORDER BY queued_at ASC NULLS LAST, id ASC
            """,
            (project_id,),
        )
        return [dict(row) for row in cur.fetchall()]
