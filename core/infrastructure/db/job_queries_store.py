"""Data-access helpers for job listing and operator attention views."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

SQL_AND = " AND "
SQL_PROJECT_ID_EQ = "project_id = %s"
SQL_DELETED_AT_IS_NULL = "deleted_at IS NULL"
SQL_PROJECT_DELETED_AT_IS_NULL = "p.deleted_at IS NULL"
SQL_JOB_DELETED_AT_IS_NULL = "j.deleted_at IS NULL"
SQL_PROJECT_ACCOUNT_ID_EQ = "p.account_id = %s"
SQL_JOB_PROJECT_ID_EQ = "j.project_id = %s"
SQL_FINISHED_AT_GTE = "j.finished_at >= %s"

_ACKNOWLEDGED_STATUS_FILTER = (
    "LOWER(COALESCE(j.status, '')) IN ('failed', 'max_iterations', 'triage_needed')"
)


def _status_values(status: str | Sequence[str] | None) -> tuple[str, ...]:
    raw = (status,) if isinstance(status, str) else tuple(status or ())
    return tuple(dict.fromkeys(str(item).strip().lower() for item in raw if str(item).strip()))


def _parse_dt(raw: datetime | str | None) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _build_project_jobs_where(
    *,
    column_exists: Callable[[str, str], bool],
    include_trashed: bool,
) -> str:
    conditions = [SQL_PROJECT_ID_EQ]
    if not include_trashed and column_exists("jobs", "deleted_at"):
        conditions.append(SQL_DELETED_AT_IS_NULL)
    return SQL_AND.join(conditions)


def _build_attention_where(
    *,
    column_exists: Callable[[str, str], bool],
    project_id: str | None,
    project_ids: list[str] | tuple[str, ...] | None,
    status: str | Sequence[str] | None,
    workflow_id: str | None,
    search: str | None,
    account_id: str | None,
    include_trashed: bool,
    scope_conditions: tuple[list[str], list[Any]] | None = None,
    workspace_id: str | None = None,
    finished_after: datetime | str | None = None,
    finished_before: datetime | str | None = None,
    hide_acknowledged: bool = False,
    task_source: str | None = None,
) -> tuple[str, list[Any]]:
    where, params = (
        (list(scope_conditions[0]), list(scope_conditions[1])) if scope_conditions else ([], [])
    )

    if not include_trashed and column_exists("jobs", "deleted_at"):
        where.append(SQL_JOB_DELETED_AT_IS_NULL)
    if not include_trashed and column_exists("projects", "deleted_at"):
        where.append(SQL_PROJECT_DELETED_AT_IS_NULL)

    normalized_account = str(account_id or "").strip()
    if normalized_account:
        where.append(SQL_PROJECT_ACCOUNT_ID_EQ)
        params.append(normalized_account)
    normalized_workspace = str(workspace_id or "").strip()
    if normalized_workspace:
        where.append("p.workspace_id = %s")
        params.append(normalized_workspace)

    if project_id is not None:
        where.append(SQL_JOB_PROJECT_ID_EQ)
        params.append(str(project_id))
    elif project_ids:
        normalized_project_ids = sorted(
            {str(pid).strip() for pid in project_ids if str(pid).strip()}
        )
        if normalized_project_ids:
            where.append("j.project_id = ANY(%s)")
            params.append(normalized_project_ids)

    finished_after_dt = _parse_dt(finished_after)
    if finished_after_dt is not None:
        where.append(SQL_FINISHED_AT_GTE)
        params.append(finished_after_dt)
    finished_before_dt = _parse_dt(finished_before)
    if finished_before_dt is not None:
        where.append("j.finished_at < %s")
        params.append(finished_before_dt)

    # Several statuses match ANY of them; "acknowledged" is derived, not stored.
    statuses = _status_values(status)
    if statuses:
        stored = [value for value in statuses if value != "acknowledged"]
        alternatives: list[str] = []
        if stored:
            alternatives.append("LOWER(COALESCE(j.status, '')) = ANY(%s)")
            params.append(stored)
        if "acknowledged" in statuses:
            alternatives.append(
                f"({_ACKNOWLEDGED_STATUS_FILTER} AND j.acknowledged_at IS NOT NULL)"
            )
        where.append("(" + " OR ".join(alternatives) + ")")
    if hide_acknowledged and column_exists("jobs", "acknowledged_at"):
        # A triaged run stays in its status; this hides it from the list.
        where.append("j.acknowledged_at IS NULL")

    normalized_workflow_id = str(workflow_id or "").strip()
    if normalized_workflow_id:
        where.append("j.selected_workflow = %s")
        params.append(normalized_workflow_id)
    if task_source is not None:
        where.append("j.task_source = %s")
        params.append(task_source)

    normalized_search = " ".join(str(search or "").split())
    if normalized_search:
        pattern = f"%{normalized_search}%"
        searchable_columns = (
            "j.id::text",
            "COALESCE(j.task_id, '')",
            "COALESCE(j.task_summary, '')",
            "COALESCE(j.status, '')",
            "COALESCE(j.current_step_id, '')",
            "COALESCE(j.current_group_id, '')",
            "COALESCE(j.current_agent, '')",
            "COALESCE(j.selected_workflow, '')",
            "COALESCE(rw.display_name, '')",
            "COALESCE(p.name, '')",
        )
        where.append("(" + " OR ".join(f"{column} ILIKE %s" for column in searchable_columns) + ")")
        params.extend([pattern] * len(searchable_columns))

    where_sql = f"WHERE {SQL_AND.join(where)}" if where else ""
    return where_sql, params


def get_jobs(
    conn,
    *,
    column_exists: Callable[[str, str], bool],
    project_id: str,
    limit: int = 50,
    offset: int = 0,
    include_trashed: bool = False,
) -> list[dict]:
    where_sql = _build_project_jobs_where(
        column_exists=column_exists,
        include_trashed=include_trashed,
    )
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT * FROM jobs WHERE {where_sql} ORDER BY COALESCE(started_at, queued_at) DESC NULLS LAST, id DESC LIMIT %s OFFSET %s",
            (project_id, limit, max(0, int(offset or 0))),
        )
        return [dict(row) for row in cur.fetchall()]


def count_jobs(
    conn,
    *,
    column_exists: Callable[[str, str], bool],
    project_id: str,
    include_trashed: bool = False,
) -> int:
    where_sql = _build_project_jobs_where(
        column_exists=column_exists,
        include_trashed=include_trashed,
    )
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_sql}", (project_id,))
        row = cur.fetchone()
        return int(row[0] if row else 0)


def list_jobs_for_attention(
    conn,
    *,
    column_exists: Callable[[str, str], bool],
    project_id: str | None = None,
    project_ids: list[str] | tuple[str, ...] | None = None,
    limit: int = 200,
    offset: int = 0,
    status: str | Sequence[str] | None = None,
    workflow_id: str | None = None,
    task_source: str | None = None,
    search: str | None = None,
    account_id: str | None = None,
    workspace_id: str | None = None,
    finished_after: datetime | str | None = None,
    finished_before: datetime | str | None = None,
    include_trashed: bool = False,
    scope_conditions: tuple[list[str], list[Any]] | None = None,
    hide_acknowledged: bool = False,
) -> list[dict]:
    where_sql, params = _build_attention_where(
        column_exists=column_exists,
        scope_conditions=scope_conditions,
        project_id=project_id,
        project_ids=project_ids,
        status=status,
        workflow_id=workflow_id,
        task_source=task_source,
        search=search,
        account_id=account_id,
        workspace_id=workspace_id,
        include_trashed=include_trashed,
        finished_after=finished_after,
        finished_before=finished_before,
        hide_acknowledged=hide_acknowledged,
    )
    params.append(max(1, min(int(limit or 200), 2000)))
    params.append(max(0, int(offset or 0)))

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                j.id,
                j.project_id,
                p.name AS project_name,
                j.task_id,
                j.task_source,
                j.duration_ms,
                LEFT(COALESCE(j.task_summary, ''), 512) AS task_summary,
                j.meta ->> 'task_summary_key' AS task_summary_key,
                j.meta -> 'task_summary_params' AS task_summary_params,
                j.status,
                j.queued_at,
                j.started_at,
                j.finished_at,
                j.worker_id,
                j.selected_workflow,
                rw.display_name AS selected_workflow_display_name,
                COALESCE(rw.settings ? 'hook_storage', false) AS hook_storage,
                aw.display_name AS hook_author_workflow_display_name,
                j.current_step_id,
                j.current_group_id,
                j.current_agent,
                j.heartbeat_at,
                j.last_error,
                j.acknowledged_at,
                j.acknowledged_by
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            LEFT JOIN registry_workflows rw ON rw.workflow_id = j.selected_workflow
            LEFT JOIN registry_workflows aw
                ON aw.workflow_id = rw.settings -> 'hook_storage' ->> 'author_workflow_id'
            {where_sql}
            ORDER BY COALESCE(j.started_at, j.queued_at) DESC NULLS LAST, j.id DESC
            LIMIT %s
            OFFSET %s
            """,
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def count_jobs_for_attention(
    conn,
    *,
    column_exists: Callable[[str, str], bool],
    project_id: str | None = None,
    project_ids: list[str] | tuple[str, ...] | None = None,
    status: str | Sequence[str] | None = None,
    workflow_id: str | None = None,
    task_source: str | None = None,
    search: str | None = None,
    account_id: str | None = None,
    workspace_id: str | None = None,
    include_trashed: bool = False,
    scope_conditions: tuple[list[str], list[Any]] | None = None,
    hide_acknowledged: bool = False,
) -> int:
    where_sql, params = _build_attention_where(
        column_exists=column_exists,
        scope_conditions=scope_conditions,
        project_id=project_id,
        project_ids=project_ids,
        status=status,
        workflow_id=workflow_id,
        task_source=task_source,
        search=search,
        account_id=account_id,
        workspace_id=workspace_id,
        include_trashed=include_trashed,
        hide_acknowledged=hide_acknowledged,
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            LEFT JOIN registry_workflows rw ON rw.workflow_id = j.selected_workflow
            {where_sql}
            """,
            tuple(params),
        )
        row = cur.fetchone()
        return int(row[0] if row else 0)


def get_workflow_stats(
    conn,
    *,
    column_exists: Callable[[str, str], bool],
    workflow_id: str,
    account_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate job + cost statistics for a single workflow."""
    where: list[str] = ["j.selected_workflow = %s"]
    params: list[Any] = [str(workflow_id)]
    if column_exists("jobs", "deleted_at"):
        where.append(SQL_JOB_DELETED_AT_IS_NULL)
    if column_exists("projects", "deleted_at"):
        where.append(SQL_PROJECT_DELETED_AT_IS_NULL)
    normalized_account = str(account_id or "").strip()
    if normalized_account:
        where.append(SQL_PROJECT_ACCOUNT_ID_EQ)
        params.append(normalized_account)
    normalized_workspace = str(workspace_id or "").strip()
    if normalized_workspace:
        where.append("p.workspace_id = %s")
        params.append(normalized_workspace)
    where_sql = "WHERE " + SQL_AND.join(where)

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*)                                                         AS total_runs,
                SUM(CASE WHEN LOWER(COALESCE(j.status,'')) = 'completed' THEN 1 ELSE 0 END)
                                                                                  AS completed_count,
                SUM(CASE WHEN LOWER(COALESCE(j.status,'')) IN
                    ('failed','max_iterations','triage_needed')
                    THEN 1 ELSE 0 END)                                            AS failed_count,
                SUM(CASE WHEN LOWER(COALESCE(j.status,'')) IN
                    ('in_progress','pending','blocked','retryable_interrupted_step',
                     'external_wait','awaiting_approval','awaiting_timer','awaiting_child',
                     'resume_pending')
                    THEN 1 ELSE 0 END)                                            AS active_count,
                SUM(CASE WHEN LOWER(COALESCE(j.status,'')) = 'cancelled' THEN 1 ELSE 0 END)
                                                                                  AS cancelled_count,
                MAX(COALESCE(j.finished_at, j.started_at, j.queued_at))           AS last_run_at,
                AVG(EXTRACT(EPOCH FROM (j.finished_at - j.started_at)))
                    FILTER (WHERE j.started_at IS NOT NULL AND j.finished_at IS NOT NULL)
                                                                                  AS avg_duration_seconds
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            {where_sql}
            """,
            tuple(params),
        )
        row = dict(cur.fetchone() or {})

        cost_where = list(where)
        cost_where.append("a.action_type = 'llm_call'")
        cost_where_sql = "WHERE " + SQL_AND.join(cost_where)
        cur.execute(
            f"""
            SELECT
                COALESCE(SUM(a.cost_usd), 0)::float                              AS total_cost_usd,
                COUNT(DISTINCT a.job_id)                                         AS jobs_with_cost
            FROM platform_action_log a
            JOIN jobs j ON j.id = a.job_id
            JOIN projects p ON p.id = j.project_id
            {cost_where_sql}
            """,
            tuple(params),
        )
        cost_row = dict(cur.fetchone() or {})

    total_runs = int(row.get("total_runs") or 0)
    completed = int(row.get("completed_count") or 0)
    failed = int(row.get("failed_count") or 0)
    active = int(row.get("active_count") or 0)
    cancelled = int(row.get("cancelled_count") or 0)
    finished_runs = completed + failed
    success_rate = (completed / finished_runs) if finished_runs > 0 else None
    total_cost = float(cost_row.get("total_cost_usd") or 0.0)
    jobs_with_cost = int(cost_row.get("jobs_with_cost") or 0)
    avg_cost = (total_cost / total_runs) if total_runs > 0 else 0.0
    last_run_at = row.get("last_run_at")
    if isinstance(last_run_at, datetime):
        last_run_at = last_run_at.astimezone(UTC).isoformat()
    avg_duration = row.get("avg_duration_seconds")
    avg_duration_seconds = float(avg_duration) if avg_duration is not None else None
    return {
        "workflow_id": str(workflow_id),
        "total_runs": total_runs,
        "completed_count": completed,
        "failed_count": failed,
        "active_count": active,
        "cancelled_count": cancelled,
        "success_rate": success_rate,
        "total_cost_usd": total_cost,
        "avg_cost_per_run_usd": avg_cost,
        "jobs_with_cost": jobs_with_cost,
        "last_run_at": last_run_at,
        "avg_duration_seconds": avg_duration_seconds,
    }


def acknowledge_job(conn, *, job_id: str, username: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(
                (
                    "UPDATE jobs SET acknowledged_at = NOW(), acknowledged_by = %s "
                    "WHERE id = %s AND acknowledged_at IS NULL"
                ),
                (str(username or "system"), str(job_id)),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        conn.rollback()
        return False


def unacknowledge_job(conn, *, job_id: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(
                (
                    "UPDATE jobs SET acknowledged_at = NULL, acknowledged_by = NULL "
                    "WHERE id = %s AND acknowledged_at IS NOT NULL"
                ),
                (str(job_id),),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        conn.rollback()
        return False
