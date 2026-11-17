"""Scope-aware report facts; aggregates never use an account-wide capped scan.

Finished runs come from two places. The dashboard rollup (``dashboard_job_facts``)
holds completed and failed runs up to its last refresh, which the control plane
runs every few minutes. Runs that finished after that refresh, and every
cancelled run (the rollup keeps none), are read live from ``jobs`` with the same
scope predicates. A report therefore counts every run that finished before the
moment it was generated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.application.jobs.query_scope import job_scope_conditions
from core.application.management.scope import ScopeContext
from core.infrastructure.db import psycopg_runtime

#: Facts refreshed up to T may miss a run whose terminal write committed just
#: after the refresh read; runs that finished this close to T are read live.
_ROLLUP_SAFETY_MARGIN = "5 minutes"

#: The rollup's own result mapping (``refresh_dashboard_job_facts_window``).
_LIVE_RESULT_SQL = """
    CASE
        WHEN LOWER(COALESCE(j.status, '')) = 'completed' THEN 'completed'
        WHEN LOWER(COALESCE(j.status, '')) IN ('failed', 'max_iterations', 'triage_needed')
            THEN 'failed'
        WHEN LOWER(COALESCE(j.status, '')) = 'cancelled' THEN 'cancelled'
    END
"""


def list_activity_facts(
    conn: Any,
    *,
    scope: ScopeContext,
    project_id: str | None,
    workflow_id: str | None,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Select report facts in SQL before daily and secondary aggregation.

    Each row has ``result`` (completed, failed or cancelled) and ``source``
    (``rollup`` or ``live``).
    """
    conditions, params = job_scope_conditions(scope)
    if project_id:
        conditions.append("j.project_id = %s")
        params.append(project_id)
    if workflow_id:
        conditions.append("j.selected_workflow = %s")
        params.append(workflow_id)
    scope_sql = psycopg_runtime.sql.SQL(" AND ").join(
        psycopg_runtime.sql.SQL(condition) for condition in conditions
    )
    query = psycopg_runtime.sql.SQL(
        """
        WITH rollup_cutoff AS (
            SELECT GREATEST(
                %s::timestamptz,
                LEAST(
                    %s::timestamptz,
                    COALESCE(
                        (
                            SELECT s.facts_covered_until - INTERVAL '{margin}'
                            FROM dashboard_rollup_state s
                            WHERE s.account_id::text = %s
                        ),
                        %s::timestamptz
                    )
                )
            ) AS covered_until
        )
        SELECT f.job_id, f.project_id, j.selected_workflow, p.tenant_id, p.workspace_id,
               f.result, f.started_at, f.finished_at, f.duration_seconds,
               'rollup' AS source
        FROM dashboard_job_facts f
        JOIN jobs j ON j.id = f.job_id AND j.project_id = f.project_id
        JOIN projects p ON p.id = j.project_id
        CROSS JOIN rollup_cutoff c
        WHERE {scope_sql}
          AND f.account_id = %s
          AND f.finished_at >= %s
          AND f.finished_at < c.covered_until
        UNION ALL
        SELECT j.id AS job_id, j.project_id, j.selected_workflow, p.tenant_id, p.workspace_id,
               {live_result} AS result, j.started_at, j.finished_at,
               CASE
                   WHEN j.started_at IS NOT NULL AND j.finished_at >= j.started_at
                       THEN EXTRACT(EPOCH FROM (j.finished_at - j.started_at))::double precision
               END AS duration_seconds,
               'live' AS source
        FROM jobs j
        JOIN projects p ON p.id = j.project_id
        CROSS JOIN rollup_cutoff c
        WHERE {scope_sql}
          AND j.deleted_at IS NULL
          AND p.deleted_at IS NULL
          AND j.finished_at >= %s
          AND j.finished_at < %s
          AND (
              LOWER(COALESCE(j.status, '')) = 'cancelled'
              OR (
                  LOWER(COALESCE(j.status, ''))
                      IN ('completed', 'failed', 'max_iterations', 'triage_needed')
                  AND j.finished_at >= c.covered_until
              )
          )
        ORDER BY finished_at, job_id
        """
    ).format(
        margin=psycopg_runtime.sql.SQL(_ROLLUP_SAFETY_MARGIN),
        scope_sql=scope_sql,
        live_result=psycopg_runtime.sql.SQL(_LIVE_RESULT_SQL),
    )
    query_params = (
        # rollup_cutoff: inside the window; without a refresh state, all of it is live.
        window_start,
        window_end,
        str(scope.account_id),
        window_start,
        # the rollup part
        *params,
        scope.account_id,
        window_start,
        # the live part
        *params,
        window_start,
        window_end,
    )
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(query, query_params)
        return [dict(row) for row in cur.fetchall()]
