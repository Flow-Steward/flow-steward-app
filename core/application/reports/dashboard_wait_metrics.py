"""Current status-group counters, independent of the bounded triage preview."""

from __future__ import annotations

from typing import Any

from core.application.management.errors import ServiceUnavailableError
from core.application.management.scope import ScopeContext
from core.application.observability_vocabulary import JOB_STATUS_GROUPS, job_status_group_statuses

#: Groups whose dashboard card needs an exact count rather than the triage preview.
_COUNTED_GROUPS = ("in_progress", "waiting")


def dashboard_wait_kpis(query_service: Any, *, scope: ScopeContext) -> dict[str, dict[str, Any]]:
    """Count every stored status of a group, through the scoped job query.

    The job list query is reused so the counters obey the same visibility as the
    job list a card opens, filtered by the same statuses.
    """
    labels = {key: label for key, label, _statuses in JOB_STATUS_GROUPS}
    metrics: dict[str, dict[str, Any]] = {}
    for group in _COUNTED_GROUPS:
        page = query_service.list_jobs(
            status=list(job_status_group_statuses(group)), per_page=1, scope=scope
        )
        payload = page.dump() if hasattr(page, "dump") else page
        total = payload.get("total") if isinstance(payload, dict) else None
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ServiceUnavailableError("Dashboard status counts are unavailable")
        metrics[group] = {"label": labels[group], "value": total}
    return metrics
