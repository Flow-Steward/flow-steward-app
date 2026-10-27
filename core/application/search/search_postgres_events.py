from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.search.search_postgres_common import (
    _EVENT_DETAIL_TEXT_SQL,
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


def _event_rows(
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
            COALESCE(e.title, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(e.event_type, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(e.severity, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(e.phase, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(e.agent, '') ILIKE %s ESCAPE '\\'
            OR {_EVENT_DETAIL_TEXT_SQL} ILIKE %s ESCAPE '\\'
        )
        """,
        P_ACCOUNT_ID_S_SQL,
    ]
    params: list[Any] = [contains, contains, contains, contains, contains, contains, account_id]
    conditions[0] = conditions[0].format(_EVENT_DETAIL_TEXT_SQL=_EVENT_DETAIL_TEXT_SQL)
    _append_soft_delete_conditions(conditions=conditions, filters=filters)
    _append_tenant_conditions(conditions=conditions, params=params, filters=filters)

    project_sql, project_params = _project_scope_sql(J_PROJECT_ID_VALUE, project_ids)

    job_id = filters.get("job_id")
    if job_id is not None:
        conditions.append("e.job_id = %s")
        params.append(str(job_id))

    event_type = str(filters.get("event_type") or "").strip().lower()
    if event_type:
        conditions.append("LOWER(COALESCE(e.event_type, '')) = %s")
        params.append(event_type)

    severity = str(filters.get("severity") or "").strip().lower()
    if severity:
        conditions.append("LOWER(COALESCE(e.severity, '')) = %s")
        params.append(severity)

    date_from = filters.get("date_from_ts")
    if date_from is not None:
        conditions.append("e.timestamp >= %s")
        params.append(date_from)

    date_to = filters.get("date_to_ts")
    if date_to is not None:
        conditions.append("e.timestamp <= %s")
        params.append(date_to)

    total_count = None
    if include_totals:
        count_sql = psycopg_sql.SQL(
            """
            SELECT COUNT(*) AS total
            FROM platform_events e
            JOIN jobs j ON j.id = e.job_id
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
            e.id,
            e.job_id,
            e.timestamp,
            e.phase,
            e.agent,
            e.event_type,
            e.severity,
            e.title,
            {detail_sql} AS detail_text,
            j.project_id,
            p.name AS project_name,
            CASE
                WHEN LOWER(COALESCE(e.title, '')) = LOWER(%s) THEN 1.0
                WHEN COALESCE(e.title, '') ILIKE %s ESCAPE '\\' THEN 0.9
                WHEN COALESCE(e.event_type, '') ILIKE %s ESCAPE '\\' THEN 0.84
                WHEN {detail_sql} ILIKE %s ESCAPE '\\' THEN 0.78
                WHEN COALESCE(e.agent, '') ILIKE %s ESCAPE '\\' THEN 0.62
                WHEN COALESCE(e.phase, '') ILIKE %s ESCAPE '\\' THEN 0.58
                ELSE 0.3
            END AS relevance
        FROM platform_events e
        JOIN jobs j ON j.id = e.job_id
        JOIN projects p ON p.id = j.project_id
        WHERE {where_sql}
        {project_sql}
        ORDER BY relevance DESC, e.timestamp DESC, e.id DESC
        LIMIT %s
    """
    ).format(
        detail_sql=_sql_fragment(_EVENT_DETAIL_TEXT_SQL),
        where_sql=_sql_join(conditions, separator=VALUE_AND_SQL),
        project_sql=_sql_fragment(project_sql),
    )
    ranked_params = [query_text, contains, contains, contains, contains, contains]
    rows = _execute_rows(
        conn=bl.conn,
        sql=sql,
        params=[*ranked_params, *params, *project_params, max(1, int(limit))],
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        job_id_value = str(row.get("job_id") or "").strip() or None
        items.append(
            {
                "group": "events",
                "source": "postgres.platform_events",
                "id": str(row.get("id") or ""),
                "title": _trim(row.get("title") or row.get("event_type") or "Event", limit=160),
                "snippet": _trim(
                    row.get("detail_text") or row.get("event_type") or row.get("severity"),
                    limit=240,
                ),
                "score": float(row.get("relevance") or 0.0),
                "timestamp": _isoformat(row.get("timestamp")),
                "project_id": row.get("project_id"),
                "project_name": str(row.get("project_name") or "").strip() or None,
                "job_id": job_id_value,
                "deep_link": f"/jobs/{job_id_value}" if job_id_value else "/monitoring?tab=events",
                "meta": {
                    "event_type": str(row.get("event_type") or "").strip() or None,
                    "severity": str(row.get("severity") or "").strip() or None,
                    "phase": str(row.get("phase") or "").strip() or None,
                    "agent": str(row.get("agent") or "").strip() or None,
                },
            }
        )
    return items, total_count


def _runtime_message_rows(
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
            COALESCE(m.summary, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(m.status, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(m.step_id, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(m.from_agent, '') ILIKE %s ESCAPE '\\'
            OR COALESCE(m.skill, '') ILIKE %s ESCAPE '\\'
        )
        """,
        P_ACCOUNT_ID_S_SQL,
    ]
    params: list[Any] = [contains, contains, contains, contains, contains, account_id]
    _append_soft_delete_conditions(conditions=conditions, filters=filters)
    _append_tenant_conditions(conditions=conditions, params=params, filters=filters)

    project_sql, project_params = _project_scope_sql(J_PROJECT_ID_VALUE, project_ids)

    job_id = filters.get("job_id")
    if job_id is not None:
        conditions.append("m.job_id = %s")
        params.append(str(job_id))

    status = str(filters.get("status") or "").strip().lower()
    if status:
        conditions.append("LOWER(COALESCE(m.status, '')) = %s")
        params.append(status)

    date_from = filters.get("date_from_ts")
    if date_from is not None:
        conditions.append("m.timestamp >= %s")
        params.append(date_from)

    date_to = filters.get("date_to_ts")
    if date_to is not None:
        conditions.append("m.timestamp <= %s")
        params.append(date_to)

    total_count = None
    if include_totals:
        count_sql = psycopg_sql.SQL(
            """
            SELECT COUNT(*) AS total
            FROM platform_agent_messages m
            JOIN jobs j ON j.id = m.job_id
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
            m.id,
            m.job_id,
            m.timestamp,
            m.from_agent,
            m.step_id,
            m.group_id,
            m.skill,
            m.status,
            m.summary,
            j.project_id,
            p.name AS project_name,
            CASE
                WHEN LOWER(COALESCE(m.summary, '')) = LOWER(%s) THEN 1.0
                WHEN COALESCE(m.summary, '') ILIKE %s ESCAPE '\\' THEN 0.92
                WHEN COALESCE(m.step_id, '') ILIKE %s ESCAPE '\\' THEN 0.7
                WHEN COALESCE(m.from_agent, '') ILIKE %s ESCAPE '\\' THEN 0.62
                WHEN COALESCE(m.skill, '') ILIKE %s ESCAPE '\\' THEN 0.58
                WHEN COALESCE(m.status, '') ILIKE %s ESCAPE '\\' THEN 0.52
                ELSE 0.3
            END AS relevance
        FROM platform_agent_messages m
        JOIN jobs j ON j.id = m.job_id
        JOIN projects p ON p.id = j.project_id
        WHERE {where_sql}
        {project_sql}
        ORDER BY relevance DESC, m.timestamp DESC, m.id DESC
        LIMIT %s
    """
    ).format(
        where_sql=_sql_join(conditions, separator=VALUE_AND_SQL),
        project_sql=_sql_fragment(project_sql),
    )
    ranked_params = [query_text, contains, contains, contains, contains, contains]
    rows = _execute_rows(
        conn=bl.conn,
        sql=sql,
        params=[*ranked_params, *params, *project_params, max(1, int(limit))],
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        job_id_value = str(row.get("job_id") or "").strip() or None
        items.append(
            {
                "group": "runtime_messages",
                "source": "postgres.platform_agent_messages",
                "id": str(row.get("id") or ""),
                "title": _trim(
                    row.get("summary")
                    or row.get("step_id")
                    or row.get("from_agent")
                    or "Runtime message",
                    limit=160,
                ),
                "snippet": _trim(
                    " · ".join(
                        part
                        for part in [
                            str(row.get("from_agent") or "").strip(),
                            str(row.get("step_id") or "").strip(),
                            str(row.get("status") or "").strip(),
                        ]
                        if part
                    )
                    or row.get("summary"),
                    limit=240,
                ),
                "score": float(row.get("relevance") or 0.0),
                "timestamp": _isoformat(row.get("timestamp")),
                "project_id": row.get("project_id"),
                "project_name": str(row.get("project_name") or "").strip() or None,
                "job_id": job_id_value,
                "deep_link": f"/jobs/{job_id_value}" if job_id_value else None,
                "meta": {
                    "from_agent": str(row.get("from_agent") or "").strip() or None,
                    "step_id": str(row.get("step_id") or "").strip() or None,
                    "group_id": str(row.get("group_id") or "").strip() or None,
                    "skill": str(row.get("skill") or "").strip() or None,
                    "status": str(row.get("status") or "").strip() or None,
                },
            }
        )
    return items, total_count
