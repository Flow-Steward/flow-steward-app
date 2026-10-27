from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.search.search_postgres_common import (
    J_PROJECT_ID_VALUE,
    P_ACCOUNT_ID_S_SQL,
    VALUE_AND_SQL,
    _append_soft_delete_conditions,
    _append_tenant_conditions,
    _execute_rows,
    _execute_total,
    _isoformat,
    _like_pattern,
    _project_scope_sql,
    _sql_fragment,
    _sql_join,
    _trim,
)
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql


def _job_rows(
    *,
    bl: Any,
    query: str,
    account_id: str,
    project_ids: Sequence[str] | None,
    filters: Mapping[str, Any],
    limit: int,
    include_totals: bool = False,
) -> tuple[list[dict[str, Any]], int | None]:
    contains = _like_pattern(query)
    query_text = str(query or "").strip()
    conditions = [
        """
        (
            CAST(j.id AS TEXT) = %s
            OR COALESCE(j.task_id, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(j.task_summary, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(j.selected_workflow, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(j.status, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(j.last_error, '') ILIKE %s ESCAPE '\\'
        )
        """,
        P_ACCOUNT_ID_S_SQL,
    ]
    params: list[Any] = [query_text, contains, contains, contains, contains, contains, account_id]
    _append_soft_delete_conditions(conditions=conditions, filters=filters)
    _append_tenant_conditions(conditions=conditions, params=params, filters=filters)

    project_sql, project_params = _project_scope_sql(J_PROJECT_ID_VALUE, project_ids)

    job_id = filters.get("job_id")
    if job_id is not None:
        conditions.append("j.id = %s")
        params.append(str(job_id))

    status = str(filters.get("status") or "").strip().lower()
    if status:
        conditions.append("LOWER(COALESCE(j.status, '')) = %s")
        params.append(status)

    date_from = filters.get("date_from_ts")
    if date_from is not None:
        conditions.append("COALESCE(j.finished_at, j.started_at, j.queued_at) >= %s")
        params.append(date_from)

    date_to = filters.get("date_to_ts")
    if date_to is not None:
        conditions.append("COALESCE(j.finished_at, j.started_at, j.queued_at) <= %s")
        params.append(date_to)

    total_count = None
    if include_totals:
        count_sql = psycopg_sql.SQL(
            """
            SELECT COUNT(*) AS total
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            WHERE {where_sql}
            {project_sql}
        """
        ).format(
            where_sql=_sql_join(conditions, separator=VALUE_AND_SQL),
            project_sql=_sql_fragment(project_sql),
        )
        total_count = _execute_total(
            conn=bl.conn,
            sql=count_sql,
            params=[*params, *project_params],
        )

    sql = psycopg_sql.SQL(
        """
        SELECT
            j.id,
            j.project_id,
            p.name AS project_name,
            j.task_id,
            j.task_summary,
            j.status,
            j.selected_workflow,
            j.last_error,
            COALESCE(j.finished_at, j.started_at, j.queued_at) AS search_timestamp,
            CASE
                WHEN CAST(j.id AS TEXT) = %s THEN 1.0
                WHEN LOWER(COALESCE(j.task_id, '')) = LOWER(%s) THEN 0.98
                WHEN COALESCE(j.task_id, '') ILIKE %s ESCAPE '\\' THEN 0.92
                WHEN COALESCE(j.task_summary, '') ILIKE %s ESCAPE '\\' THEN 0.84
                WHEN COALESCE(j.selected_workflow, '') ILIKE %s ESCAPE '\\' THEN 0.72
                WHEN COALESCE(j.status, '') ILIKE %s ESCAPE '\\' THEN 0.58
                WHEN COALESCE(j.last_error, '') ILIKE %s ESCAPE '\\' THEN 0.5
                ELSE 0.25
            END AS relevance
        FROM jobs j
        JOIN projects p ON p.id = j.project_id
        WHERE {where_sql}
        {project_sql}
        ORDER BY relevance DESC, search_timestamp DESC NULLS LAST, j.id DESC
        LIMIT %s
    """
    ).format(
        where_sql=_sql_join(conditions, separator=VALUE_AND_SQL),
        project_sql=_sql_fragment(project_sql),
    )
    ranked_params = [
        query_text,
        query_text,
        f"{query_text}%",
        contains,
        contains,
        contains,
        contains,
    ]
    rows = _execute_rows(
        conn=bl.conn,
        sql=sql,
        params=[*ranked_params, *params, *project_params, max(1, int(limit))],
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        title = _trim(
            row.get("task_summary") or row.get("task_id") or f"Job #{row.get('id')}", limit=160
        )
        snippet = _trim(
            row.get("last_error")
            or row.get("task_summary")
            or row.get("selected_workflow")
            or row.get("status"),
            limit=240,
        )
        job_id_value = str(row.get("id") or "").strip()
        items.append(
            {
                "group": "jobs",
                "source": "postgres.jobs",
                "id": str(job_id_value),
                "title": title or f"Job #{job_id_value}",
                "snippet": snippet,
                "score": float(row.get("relevance") or 0.0),
                "timestamp": _isoformat(row.get("search_timestamp")),
                "project_id": row.get("project_id"),
                "project_name": str(row.get("project_name") or "").strip() or None,
                "job_id": job_id_value or None,
                "deep_link": f"/jobs/{job_id_value}" if job_id_value else None,
                "meta": {
                    "task_id": str(row.get("task_id") or "").strip() or None,
                    "status": str(row.get("status") or "").strip() or None,
                    "selected_workflow": str(row.get("selected_workflow") or "").strip() or None,
                },
            }
        )
    return items, total_count
