"""
Workload signal service — returns a recommendation based on current system metrics.

Recommendations:
    normal   — all metrics within safe bounds
    throttle — approaching capacity, slow down new submissions
    shed     — overloaded, reject non-critical work
    pause    — system at risk, stop all new submissions
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.config import CONFIG

logger = logging.getLogger(__name__)

# Thresholds (canonicalized in core.infrastructure.config.runtime_config)
_THROTTLE_ACTIVE_JOBS = int(CONFIG.get("workload_throttle_active_jobs") or 5)
_THROTTLE_QUEUE_DEPTH = int(CONFIG.get("workload_throttle_queue_depth") or 10)
_SHED_ACTIVE_JOBS = int(CONFIG.get("workload_shed_active_jobs") or 10)
_SHED_QUEUE_DEPTH = int(CONFIG.get("workload_shed_queue_depth") or 50)
_PAUSE_ACTIVE_JOBS = int(CONFIG.get("workload_pause_active_jobs") or 20)
_PAUSE_QUEUE_DEPTH = int(CONFIG.get("workload_pause_queue_depth") or 100)


def _count_active_jobs(bl, account_id: str | None = None) -> int:
    """Count jobs in active states, optionally scoped to a tenant account.

    When *account_id* is provided the count is restricted to jobs that belong
    to projects owned by that account, preventing cross-tenant metric leakage.
    """
    try:
        with bl.conn.cursor() as cur:
            if account_id:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM jobs j
                    JOIN projects p ON p.id = j.project_id
                    WHERE j.status IN ('in_progress', 'queued', 'pending')
                      AND p.account_id = %s
                    """,
                    (str(account_id),),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('in_progress', 'queued', 'pending')"
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:
        logger.debug("_count_active_jobs failed: %s", exc)
        return -1


def _get_db_connection_count(bl) -> int:
    """Get current DB connection count."""
    try:
        with bl.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            row = cur.fetchone()
            return int(row[0]) if row else -1
    except Exception as exc:
        logger.debug("_get_db_connection_count failed: %s", exc)
        return -1


def _classify(active_jobs: int, queue_depth: int) -> str:
    """Return recommendation based on metrics."""
    if active_jobs < 0 and queue_depth < 0:
        return "normal"  # No metrics available, assume ok

    # Effective values: -1 means unavailable, treat as 0 for classification
    aj = max(0, active_jobs)
    qd = max(0, queue_depth)

    if aj >= _PAUSE_ACTIVE_JOBS or qd >= _PAUSE_QUEUE_DEPTH:
        return "pause"
    if aj >= _SHED_ACTIVE_JOBS or qd >= _SHED_QUEUE_DEPTH:
        return "shed"
    if aj >= _THROTTLE_ACTIVE_JOBS or qd >= _THROTTLE_QUEUE_DEPTH:
        return "throttle"
    return "normal"


def get_workload_signal(
    bl,
    get_ready_queue_depth_fn=None,
    *,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Compute the workload signal with metrics.

    Parameters
    ----------
    bl : Flow Steward logger
        Database connection wrapper.
    get_ready_queue_depth_fn : callable, optional
        Function returning the ready queue depth from RabbitMQ.
        Falls back gracefully if unavailable.
    account_id : str, optional
        When provided, active_jobs count is scoped to this tenant account.
        The queue_depth and db_connections remain global system metrics (they
        are not tenant-attributable at the infrastructure level), but are
        marked with ``scope`` in the response so callers understand the semantics.
    """
    active_jobs = _count_active_jobs(bl, account_id=account_id)

    queue_depth = -1
    if get_ready_queue_depth_fn is not None:
        try:
            queue_depth = int(get_ready_queue_depth_fn())
        except Exception as exc:
            logger.debug("get_ready_queue_depth failed (non-fatal): %s", exc)
            queue_depth = -1

    db_connections = _get_db_connection_count(bl)
    recommendation = _classify(active_jobs, queue_depth)

    return {
        "recommendation": recommendation,
        "scope": {
            "account_id": account_id,
            "active_jobs_scoped": account_id is not None,
        },
        "metrics": {
            "active_jobs": active_jobs,
            "queue_depth": queue_depth,
            "db_connections": db_connections,
        },
        "thresholds": {
            "throttle": {
                "active_jobs": _THROTTLE_ACTIVE_JOBS,
                "queue_depth": _THROTTLE_QUEUE_DEPTH,
            },
            "shed": {"active_jobs": _SHED_ACTIVE_JOBS, "queue_depth": _SHED_QUEUE_DEPTH},
            "pause": {"active_jobs": _PAUSE_ACTIVE_JOBS, "queue_depth": _PAUSE_QUEUE_DEPTH},
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }
