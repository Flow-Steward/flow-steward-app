"""Infrastructure storage adapter for process monitoring."""

from __future__ import annotations

import contextlib
from typing import Any, Literal


def monitoring_observability_dsn() -> str:
    from core.infrastructure.config.runtime_config import CONFIG
    from core.infrastructure.db.platform_logger import resolve_observability_dsn

    return resolve_observability_dsn(
        CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or ""
    )


def load_active_job_rows() -> list[dict[str, Any]]:
    from core.infrastructure.db.platform_stores import PlatformJobStore

    dsn = monitoring_observability_dsn()
    if not dsn:
        return []
    bl = PlatformJobStore(dsn)
    try:
        try:
            rows = bl.get_active_jobs()
        except Exception:
            with bl.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.id, j.task_id, j.task_summary, j.status,
                           j.started_at, j.worker_id, j.meta,
                           p.name AS project_name
                    FROM jobs j
                    JOIN projects p ON p.id = j.project_id
                    WHERE j.status IN (
                        'pending', 'resume_pending', 'in_progress',
                        'awaiting_approval', 'awaiting_timer', 'awaiting_child'
                    )
                    ORDER BY j.started_at DESC
                    """
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        return [dict(row) for row in rows if isinstance(row, dict)]
    finally:
        close_suppressing_errors(bl)


def load_latest_agent_message(job_id: str) -> dict[str, Any] | None:
    from core.infrastructure.db.platform_stores import PlatformJobStore

    dsn = monitoring_observability_dsn()
    if not dsn:
        return None
    bl = PlatformJobStore(dsn)
    try:
        row = bl.get_latest_agent_message(job_id)
        return dict(row) if isinstance(row, dict) else None
    finally:
        close_suppressing_errors(bl)


def load_active_project_lease_rows() -> list[dict[str, Any]]:
    from core.infrastructure.db.platform_stores import PlatformJobStore

    dsn = monitoring_observability_dsn()
    if not dsn:
        return []
    bl = PlatformJobStore(dsn)
    try:
        if hasattr(bl, "list_project_execution_leases"):
            rows = bl.list_project_execution_leases(active_only=True) or []
        else:
            with bl.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        project_id,
                        worker_instance_id,
                        lease_token,
                        heartbeat_at,
                        expires_at,
                        acquired_at,
                        active_job_id,
                        drain_status
                    FROM project_execution_slots
                    WHERE expires_at > now()
                    ORDER BY expires_at DESC
                    """
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        return [dict(row) for row in rows if isinstance(row, dict)]
    finally:
        close_suppressing_errors(bl)


def reap_stale_worker_reports(stale_by_job: dict[str, set[str]]) -> int:
    from core.infrastructure.db.platform_stores import PlatformJobStore
    from core.infrastructure.runtime.subprocess_lifecycle import reap_stale_worker_subprocesses

    dsn = monitoring_observability_dsn()
    if not dsn:
        return 0
    bl = PlatformJobStore(dsn)
    try:
        count = 0
        for job_id, stale_worker_ids in stale_by_job.items():
            count += reap_stale_worker_subprocesses(
                bl,
                job_id=job_id,
                stale_worker_ids=stale_worker_ids,
                grace_seconds=120,
            )
        return count
    finally:
        close_suppressing_errors(bl)


def load_reported_worker_subprocess_rows(job_ids: list[str]) -> list[dict[str, Any]]:
    from core.infrastructure.db.platform_stores import PlatformJobStore

    if not job_ids:
        return []
    dsn = monitoring_observability_dsn()
    if not dsn:
        return []
    bl = PlatformJobStore(dsn)
    try:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (
                           job_id,
                           COALESCE(NULLIF(payload->>'worker_id', ''), 'unknown-worker'),
                           COALESCE(NULLIF(payload->>'pid', ''), 'unknown-pid'),
                           COALESCE(NULLIF(payload->>'kind', ''), NULLIF(payload->>'source', ''), 'worker_reported')
                    )
                           job_id,
                           agent,
                           status,
                           timestamp,
                           payload
                      FROM platform_action_log
                     WHERE job_id = ANY(%s)
                       AND action_type = 'worker_subprocess'
                       AND NULLIF(payload->>'pid', '') IS NOT NULL
                     ORDER BY job_id,
                              COALESCE(NULLIF(payload->>'worker_id', ''), 'unknown-worker'),
                              COALESCE(NULLIF(payload->>'pid', ''), 'unknown-pid'),
                              COALESCE(NULLIF(payload->>'kind', ''), NULLIF(payload->>'source', ''), 'worker_reported'),
                              timestamp DESC
                )
                SELECT job_id, agent, status, timestamp, payload
                  FROM latest
                 WHERE status = 'running'
                 ORDER BY timestamp DESC
                """,
                (job_ids,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        close_suppressing_errors(bl)


def close_suppressing_errors(bl: Any) -> None:
    with contextlib.suppress(Exception):
        bl.conn.close()


class suppress_close:
    def __init__(self, bl: Any) -> None:
        self.bl = bl

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> Literal[False]:
        close_suppressing_errors(self.bl)
        return False
