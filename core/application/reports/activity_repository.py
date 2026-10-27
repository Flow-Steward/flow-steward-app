"""Scope-aware report facts; aggregates never use an account-wide capped scan."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.application.jobs.query_scope import job_scope_conditions
from core.application.management.scope import ScopeContext
from core.infrastructure.db import psycopg_runtime


def list_activity_facts(
    conn: Any,
    *,
    scope: ScopeContext,
    project_id: str | None,
    workflow_id: str | None,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Select report facts in SQL before daily and secondary aggregation."""
    conditions, params = job_scope_conditions(scope)
    conditions.extend(["f.account_id = %s", "f.finished_at >= %s", "f.finished_at < %s"])
    params.extend([scope.account_id, window_start, window_end])
    if project_id:
        conditions.append("j.project_id = %s")
        params.append(project_id)
    if workflow_id:
        conditions.append("j.selected_workflow = %s")
        params.append(workflow_id)
    where_clause = psycopg_runtime.sql.SQL(" AND ").join(
        psycopg_runtime.sql.SQL(condition) for condition in conditions
    )
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            psycopg_runtime.sql.SQL(
                """
            SELECT f.*, j.selected_workflow, p.tenant_id, p.workspace_id
            FROM dashboard_job_facts f
            JOIN jobs j ON j.id = f.job_id AND j.project_id = f.project_id
            JOIN projects p ON p.id = j.project_id
            WHERE {where_clause}
            ORDER BY f.finished_at, f.job_id
        """
            ).format(where_clause=where_clause),
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]
