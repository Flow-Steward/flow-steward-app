"""Release-safe dependency guard for destructive workflow lifecycle operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.management.errors import ServiceConflictError
from core.infrastructure.db.psycopg_runtime.extras import RealDictCursor

_TERMINAL_JOB_STATUSES = (
    "cancelled",
    "completed",
    "failed",
    "max_iterations",
    "triage_needed",
)
_BLOCKER_LIMIT = 20
_BLOCKING_JOBS_SQL = """
    SELECT id::text AS job_id, status
      FROM jobs
     WHERE (
           COALESCE(selected_workflow_key, '') = %s
        OR selected_workflow_id::text = %s
        OR COALESCE(selected_workflow, '') = %s
        OR COALESCE(meta->>'selected_workflow_id', '') = %s
        OR COALESCE(meta->'invocation'->>'selected_workflow_id', '') = %s
     )
       AND LOWER(COALESCE(status, '')) <> ALL(%s)
       AND deleted_at IS NULL
       AND (%s = '' OR account_id::text = %s)
       AND (%s = '' OR project_id::text = %s)
     ORDER BY started_at, id
     LIMIT %s
"""


def assert_workflow_has_no_nonterminal_jobs(
    store: Any,
    *,
    workflow_id: str,
    account_id: str | None = None,
    project_id: str | None = None,
    cursor: Any | None = None,
) -> None:
    """Raise a typed 409 while any live job still depends on ``workflow_id``."""
    normalized_workflow_id = str(workflow_id or "").strip()
    normalized_account_id = str(account_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    conn = getattr(store, "conn", None)
    if not normalized_workflow_id or conn is None:
        raise ServiceConflictError(
            "Workflow dependency state is unavailable",
            payload={
                "code": "workflow_dependency_check_unavailable",
                "workflow_id": normalized_workflow_id,
                "blocking_jobs": [],
            },
        )

    params: list[Any] = [normalized_workflow_id] * 5
    params.append(list(_TERMINAL_JOB_STATUSES))
    params.extend((normalized_account_id, normalized_account_id))
    params.extend((normalized_project_id, normalized_project_id))
    params.append(_BLOCKER_LIMIT + 1)

    try:
        if cursor is not None:
            cursor.execute(_BLOCKING_JOBS_SQL, tuple(params))
            columns = [str(column[0]) for column in cursor.description or []]
            rows = [
                dict(row) if isinstance(row, Mapping) else dict(zip(columns, row, strict=True))
                for row in cursor.fetchall()
            ]
        else:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(_BLOCKING_JOBS_SQL, tuple(params))
                rows = [dict(row) for row in cur.fetchall()]
    except ServiceConflictError:
        raise
    except Exception as exc:
        raise ServiceConflictError(
            "Workflow dependency state is unavailable",
            payload={
                "code": "workflow_dependency_check_unavailable",
                "workflow_id": normalized_workflow_id,
                "blocking_jobs": [],
            },
        ) from exc

    if not rows:
        return
    blocking_jobs = [
        {
            "job_id": str(row.get("job_id") or row.get("id") or "").strip(),
            "status": str(row.get("status") or "unknown").strip() or "unknown",
        }
        for row in rows[:_BLOCKER_LIMIT]
    ]
    raise ServiceConflictError(
        "Workflow has non-terminal dependent jobs",
        payload={
            "code": "workflow_has_nonterminal_jobs",
            "workflow_id": normalized_workflow_id,
            "blocking_jobs": blocking_jobs,
            "truncated": len(rows) > _BLOCKER_LIMIT,
        },
    )


__all__ = ["assert_workflow_has_no_nonterminal_jobs"]
