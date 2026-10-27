"""Worker bootstrap helpers that do not own queue execution."""

from __future__ import annotations

import logging
import os
import socket
import uuid
from collections.abc import Callable

logger = logging.getLogger("platform.worker")

# A container restart commonly reuses both hostname and PID 1.  Persisting only
# those two values made the new process indistinguishable from the crashed lease
# owner, so it renewed the stale lease and hot-requeued the interrupted job.
# Keep one random identity for this Python process lifetime.
_WORKER_INCARNATION_ID = uuid.uuid4().hex[:12]


def _repair_canonical_guardrails_for_worker(
    *,
    ensure_guardrails_seeded: Callable[..., dict] | None = None,
) -> None:
    if ensure_guardrails_seeded is None:
        return
    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.db.platform_logger import resolve_observability_dsn

        dsn = resolve_observability_dsn(
            str(CONFIG.get("fs_observability_dsn") or CONFIG.get("fs_db_dsn") or "")
        )
        if not dsn:
            return
        summary = ensure_guardrails_seeded(dsn=dsn, author="system")
    except Exception as exc:
        logger.warning("Worker canonical guardrail check failed (non-fatal): %s", exc)
        return

    restored = int(summary.get("restored") or 0)
    scope_repaired = int(summary.get("scope_repaired") or 0)
    if restored or scope_repaired:
        logger.warning(
            "Worker restored canonical guardrails (restored=%d scope_repaired=%d expected=%d)",
            restored,
            scope_repaired,
            int(summary.get("expected") or 0),
        )


def _worker_process_metrics_lines(*, project_id: str | None) -> list[str]:
    from core.infrastructure.observability.process_metrics_runtime import (
        build_worker_process_metrics_lines,
    )

    return build_worker_process_metrics_lines(project_id=project_id)


def _worker_id() -> str:
    """Return a process-incarnation worker identifier."""
    return f"{socket.gethostname()}-{_WORKER_INCARNATION_ID}-{os.getpid()}"


def _get_all_project_ids() -> list:
    """Return all project IDs from the database."""
    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.db.platform_stores import PlatformProjectStore

        dsn = CONFIG.get("fs_observability_dsn", "") or CONFIG.get("fs_db_dsn", "")
        if not dsn:
            return []
        bl = PlatformProjectStore(dsn)
        projects = bl.get_projects()
        return [p["id"] for p in projects]
    except Exception as e:
        logger.warning("Could not load project IDs: %s", e)
        return []
