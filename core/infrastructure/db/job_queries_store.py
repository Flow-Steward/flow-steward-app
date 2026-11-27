"""Data-access helpers for job listing and operator attention views."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.job_review_chain_store import (
    AWAITING_REVIEW_STATUS,
    DEFAULT_REVIEW_CHAIN_DEPTH,
    awaiting_review_predicate,
)

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
    project_id: str,
    include_trashed: bool,
    status: Sequence[str] | None = None,
    workflow_id: str | None = None,
    workflow_ids: Sequence[str] | None = None,
) -> tuple[str, list[Any]]:
    conditions = [SQL_PROJECT_ID_EQ]
    params: list[Any] = [project_id]
    if not include_trashed and column_exists("jobs", "deleted_at"):
        conditions.append(SQL_DELETED_AT_IS_NULL)
    statuses = _status_values(status)
    if statuses:
        conditions.append("LOWER(COALESCE(status, '')) = ANY(%s)")
        params.append(list(statuses))
    if workflow_id:
        conditions.append("selected_workflow = %s")
        params.append(str(workflow_id))
    if workflow_ids is not None:
        # A workflow restriction fails closed: a run with no workflow is outside it.
        conditions.append("selected_workflow = ANY(%s)")
        params.append(sorted({str(value) for value in workflow_ids}))
    return SQL_AND.join(conditions), params


_VALUE_FILTER_BASES = {
    # Full input values written at run acceptance (ADR 0075), never the bounded
    # display projection in meta.invocation.
    "input": "(j.meta -> 'filter_values')",
    "output": "(j.meta -> 'workflow_outputs' -> 'outputs')",
}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _status_filter_sql(
    statuses: Sequence[str], *, review_chain_depth: Any = None
) -> tuple[str, list[Any]]:
    """Stored statuses (any match), with "awaiting review" read down the Invoke chain.

    The derived "acknowledged" is an alternative too, as in the plain status filter.
    """
    stored = [item for item in statuses if item not in {AWAITING_REVIEW_STATUS, "acknowledged"}]
    parts: list[str] = []
    params: list[Any] = []
    if stored:
        parts.append("LOWER(COALESCE(j.status, '')) = ANY(%s)")
        params.append(stored)
    if "acknowledged" in statuses:
        parts.append(f"({_ACKNOWLEDGED_STATUS_FILTER} AND j.acknowledged_at IS NOT NULL)")
    if AWAITING_REVIEW_STATUS in statuses:
        predicate, predicate_params = awaiting_review_predicate(
            int(review_chain_depth or DEFAULT_REVIEW_CHAIN_DEPTH)
        )
        parts.append(predicate)
        params.extend(predicate_params)
    if not parts:
        return "false", []
    return "(" + " OR ".join(parts) + ")", params


def _value_filter_sql(spec: Mapping[str, Any]) -> tuple[str, list[Any]]:
    """One configured jobs-list filter as a predicate; names and values are parameters."""
    source = str(spec.get("source") or "")
    key = str(spec.get("key") or "")
    filter_type = str(spec.get("type") or "")
    value = spec.get("value")
    if source == "job":
        # The only job filter is its status (choice / choice_multi); "awaiting
        # review" also matches a parent whose Invoke chain waits for an approval.
        return _status_filter_sql(
            [str(item) for item in value or ()],
            review_chain_depth=spec.get("review_chain_depth"),
        )
    base = _VALUE_FILTER_BASES.get(source)
    if base is None or not key:
        raise ValueError(f"Unsupported jobs-list filter source: {source!r}")
    node = f"({base} -> %s)"
    if filter_type == "search":
        return (
            f"COALESCE({base} ->> %s, '') ILIKE %s ESCAPE '\\'",
            [key, f"%{_escape_like(str(value))}%"],
        )
    if filter_type == "boolean":
        return f"{node} = %s::jsonb", [key, "true" if value else "false"]
    if filter_type in {"number_range", "date_range"}:
        low, high = value
        if filter_type == "number_range":
            typed = f"(CASE WHEN jsonb_typeof({node}) = 'number' THEN ({base} ->> %s)::numeric END)"
        else:
            typed = f"(CASE WHEN jsonb_typeof({node}) = 'string' THEN LEFT({base} ->> %s, 10) END)"
        parts: list[str] = []
        params: list[Any] = []
        for bound, operator in ((low, ">="), (high, "<=")):
            if bound is not None:
                parts.append(f"{typed} {operator} %s")
                params.extend([key, key, bound])
        return "(" + SQL_AND.join(parts) + ")", params
    if filter_type in {"choice", "choice_multi"}:
        options = [str(item) for item in value or ()]
        # A select holds a string, a multi-select an array of strings: any match.
        return (
            f"(CASE jsonb_typeof({node})"
            f" WHEN 'array' THEN {node} ?| %s::text[]"
            f" WHEN 'string' THEN ({base} ->> %s) = ANY(%s::text[])"
            " ELSE false END)",
            [key, key, options, key, options],
        )
    raise ValueError(f"Unsupported jobs-list filter type: {filter_type!r}")


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
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    task_source: str | None = None,
    exclude_task_source: str | None = None,
    value_filters: Sequence[Mapping[str, Any]] | None = None,
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

    if started_after is not None:
        where.append("COALESCE(j.started_at, j.queued_at) >= %s")
        params.append(started_after)
    if started_before is not None:
        where.append("COALESCE(j.started_at, j.queued_at) < %s")
        params.append(started_before)

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
    if exclude_task_source is not None:
        # The operator list of a workflow (ADR 0075) leaves Test Runs out.
        where.append("j.task_source IS DISTINCT FROM %s")
        params.append(exclude_task_source)
    for spec in value_filters or ():
        predicate, predicate_params = _value_filter_sql(spec)
        where.append(predicate)
        params.extend(predicate_params)

    normalized_search = " ".join(str(search or "").split())
    if normalized_search:
        pattern = f"%{normalized_search}%"
        searchable_columns = (
            "j.id::text",
            "COALESCE(j.task_id, '')",
            "COALESCE(j.task_summary, '')",
            "COALESCE(j.meta ->> 'run_title', '')",
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
    status: Sequence[str] | None = None,
    workflow_id: str | None = None,
    workflow_ids: Sequence[str] | None = None,
) -> list[dict]:
    where_sql, params = _build_project_jobs_where(
        column_exists=column_exists,
        project_id=project_id,
        include_trashed=include_trashed,
        status=status,
        workflow_id=workflow_id,
        workflow_ids=workflow_ids,
    )
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT * FROM jobs WHERE {where_sql} ORDER BY COALESCE(started_at, queued_at) DESC NULLS LAST, id DESC LIMIT %s OFFSET %s",
            (*params, limit, max(0, int(offset or 0))),
        )
        return [dict(row) for row in cur.fetchall()]


def count_jobs(
    conn,
    *,
    column_exists: Callable[[str, str], bool],
    project_id: str,
    include_trashed: bool = False,
    status: Sequence[str] | None = None,
    workflow_id: str | None = None,
    workflow_ids: Sequence[str] | None = None,
) -> int:
    where_sql, params = _build_project_jobs_where(
        column_exists=column_exists,
        project_id=project_id,
        include_trashed=include_trashed,
        status=status,
        workflow_id=workflow_id,
        workflow_ids=workflow_ids,
    )
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_sql}", tuple(params))
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
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    exclude_task_source: str | None = None,
    value_filters: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict]:
    where_sql, params = _build_attention_where(
        column_exists=column_exists,
        scope_conditions=scope_conditions,
        project_id=project_id,
        project_ids=project_ids,
        status=status,
        workflow_id=workflow_id,
        task_source=task_source,
        exclude_task_source=exclude_task_source,
        value_filters=value_filters,
        search=search,
        account_id=account_id,
        workspace_id=workspace_id,
        include_trashed=include_trashed,
        finished_after=finished_after,
        finished_before=finished_before,
        hide_acknowledged=hide_acknowledged,
        started_after=started_after,
        started_before=started_before,
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
                j.meta ->> 'run_title' AS run_title,
                j.status,
                j.queued_at,
                j.started_at,
                j.finished_at,
                j.worker_id,
                j.selected_workflow,
                rw.display_name AS selected_workflow_display_name,
                COALESCE(rw.settings ? 'hook_storage', false) AS hook_storage,
                aw.display_name AS hook_author_workflow_display_name,
                j.meta -> 'composite_action' AS composite_action,
                j.current_step_id,
                j.current_group_id,
                j.current_agent,
                j.heartbeat_at,
                j.last_error,
                j.acknowledged_at,
                j.acknowledged_by
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            LEFT JOIN registry_workflows rw
                ON rw.workflow_id = j.selected_workflow AND rw.account_id = j.account_id
            LEFT JOIN registry_workflows aw
                ON aw.workflow_id = rw.settings -> 'hook_storage' ->> 'author_workflow_id'
               AND aw.account_id = rw.account_id
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
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    exclude_task_source: str | None = None,
    value_filters: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    where_sql, params = _build_attention_where(
        column_exists=column_exists,
        scope_conditions=scope_conditions,
        project_id=project_id,
        project_ids=project_ids,
        status=status,
        workflow_id=workflow_id,
        task_source=task_source,
        exclude_task_source=exclude_task_source,
        value_filters=value_filters,
        search=search,
        account_id=account_id,
        workspace_id=workspace_id,
        include_trashed=include_trashed,
        hide_acknowledged=hide_acknowledged,
        started_after=started_after,
        started_before=started_before,
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            LEFT JOIN registry_workflows rw
                ON rw.workflow_id = j.selected_workflow AND rw.account_id = j.account_id
            {where_sql}
            """,
            tuple(params),
        )
        row = cur.fetchone()
        return int(row[0] if row else 0)


#: The list's order with a missing time last, as one comparable key.
_LIST_SORT_KEY = "COALESCE(j.started_at, j.queued_at, '-infinity'::timestamptz)"
_LIST_FROM = """
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            LEFT JOIN registry_workflows rw
                ON rw.workflow_id = j.selected_workflow AND rw.account_id = j.account_id
"""


def job_list_neighbours(
    conn,
    *,
    job_id: str,
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
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    exclude_task_source: str | None = None,
    value_filters: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Position of one job in a jobs list and its neighbours there, by keyset.

    The same filters and order as :func:`list_jobs_for_attention`. The neighbours
    are found relative to the job's own sort key, not its row number, so a job
    that has just left the filter (a decided review) still has the right
    previous and next job: none is skipped or shown twice. ``None`` when the
    job is not in the caller's project, workflow and scope at all.
    """
    base_where, base_params = _build_attention_where(
        column_exists=column_exists,
        scope_conditions=scope_conditions,
        project_id=project_id,
        project_ids=project_ids,
        status=None,
        workflow_id=workflow_id,
        search=None,
        account_id=account_id,
        workspace_id=workspace_id,
        include_trashed=include_trashed,
    )
    anchor_where = f"{base_where}{SQL_AND if base_where else 'WHERE '}j.id::text = %s"
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_LIST_SORT_KEY} AS sort_at, j.id {_LIST_FROM} {anchor_where}",
            (*base_params, str(job_id)),
        )
        anchor = cur.fetchone()
        if not anchor:
            return None
        where_sql, params = _build_attention_where(
            column_exists=column_exists,
            scope_conditions=scope_conditions,
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            workflow_id=workflow_id,
            task_source=task_source,
            exclude_task_source=exclude_task_source,
            value_filters=value_filters,
            search=search,
            account_id=account_id,
            workspace_id=workspace_id,
            include_trashed=include_trashed,
            hide_acknowledged=hide_acknowledged,
            started_after=started_after,
            started_before=started_before,
        )
        key = (anchor["sort_at"], anchor["id"])
        cur.execute(
            f"""
            WITH listed AS (
                SELECT j.id, {_LIST_SORT_KEY} AS sort_at
                {_LIST_FROM}
                {where_sql}
            )
            SELECT
                (SELECT COUNT(*) FROM listed) AS total,
                (SELECT COUNT(*) FROM listed WHERE (sort_at, id) > (%s, %s)) AS before_count,
                EXISTS (SELECT 1 FROM listed WHERE id = %s) AS in_list,
                (
                    SELECT id::text FROM listed WHERE (sort_at, id) > (%s, %s)
                    ORDER BY sort_at ASC, id ASC LIMIT 1
                ) AS previous_job_id,
                (
                    SELECT id::text FROM listed WHERE (sort_at, id) < (%s, %s)
                    ORDER BY sort_at DESC, id DESC LIMIT 1
                ) AS next_job_id
            """,
            (*params, *key, key[1], *key, *key),
        )
        row = dict(cur.fetchone() or {})
    in_list = bool(row.get("in_list"))
    return {
        "job_id": str(anchor["id"]),
        "in_list": in_list,
        "position": int(row.get("before_count") or 0) + 1 if in_list else None,
        "total": int(row.get("total") or 0),
        "previous_job_id": row.get("previous_job_id"),
        "next_job_id": row.get("next_job_id"),
    }


def get_workflow_stats(
    conn,
    *,
    column_exists: Callable[[str, str], bool],
    workflow_id: str,
    account_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate job statistics for a single workflow."""
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
                     'awaiting_event','resume_pending')
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

    total_runs = int(row.get("total_runs") or 0)
    completed = int(row.get("completed_count") or 0)
    failed = int(row.get("failed_count") or 0)
    active = int(row.get("active_count") or 0)
    cancelled = int(row.get("cancelled_count") or 0)
    finished_runs = completed + failed
    success_rate = (completed / finished_runs) if finished_runs > 0 else None
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
