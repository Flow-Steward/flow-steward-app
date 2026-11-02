"""Installation-level metrics, collected only by the control-plane process.

The values are the ones ``/metrics`` reports (``collect_installation_snapshot``);
the control plane is the single existing owner of installation SQL, so no
leader election is involved. Each collection takes one snapshot through a
dedicated store connection, used only by the metric reader's thread.

Kept faithful to the source:

* ``fs.installation.jobs`` stays a gauge per status, not a counter;
* an unavailable ready queue (``-1``) and an unknown SLO value are reported
  as no observation, never as zero;
* readiness and the per-agent SLO are not exported: readiness stays in health
  endpoints and ``/metrics``, and agent names are not a bounded attribute.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from core.infrastructure.observability import otel_instruments as instruments
from core.infrastructure.observability.observability_metrics import (
    InstallationSnapshot,
    collect_installation_snapshot,
)

PROJECTS = "fs.installation.projects"
JOBS = "fs.installation.jobs"
PROJECT_LEASES = "fs.installation.project_leases.active"
READY_QUEUE_DEPTH = "fs.installation.ready_queue.depth"
SLO_P95_LATENCY = "fs.slo.workflow.p95_latency"
SLO_RETRY_RATE = "fs.slo.event_dispatch.retry_rate"
SLO_DEAD_LETTER_RATE = "fs.slo.event_dispatch.dead_letter_rate"
SLO_STUCK_JOBS = "fs.slo.jobs.stuck"

# Job lifecycle statuses written to ``jobs.status``; the column has no check
# constraint, so anything else is counted as "other" to keep the series bounded.
JOB_STATUSES = frozenset(
    {
        "awaiting_approval",
        "cancelled",
        "completed",
        "failed",
        "in_progress",
        "max_iterations",
        "pending",
        "preparing",
        "resume_pending",
        "stuck",
        "triage_needed",
        "unknown",
    }
)
# One snapshot serves every instrument of a single collection.
_SNAPSHOT_REUSE_SECONDS = 5.0


class InstallationMetrics:
    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        get_ready_queue_depth: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store_factory = store_factory
        self._get_ready_queue_depth = get_ready_queue_depth
        self._clock = clock
        self._lock = threading.Lock()
        self._store: Any = None
        self._snapshot: InstallationSnapshot | None = None
        self._taken_at = 0.0

    def snapshot(self) -> InstallationSnapshot:
        with self._lock:
            now = self._clock()
            if self._snapshot is None or now - self._taken_at >= _SNAPSHOT_REUSE_SECONDS:
                if self._store is None:
                    self._store = self._store_factory()
                self._snapshot = collect_installation_snapshot(
                    self._store, get_ready_queue_depth=self._get_ready_queue_depth
                )
                self._taken_at = now
            return self._snapshot

    def projects(self) -> Iterator[tuple[float, dict[str, str]]]:
        yield self.snapshot().project_count, {}

    def jobs(self) -> Iterator[tuple[float, dict[str, str]]]:
        totals: dict[str, int] = {}
        for status, count in self.snapshot().job_status_counts.items():
            bounded = status if status in JOB_STATUSES else "other"
            totals[bounded] = totals.get(bounded, 0) + count
        for status, count in sorted(totals.items()):
            yield count, {"fs.job.status": status}

    def leases(self) -> Iterator[tuple[float, dict[str, str]]]:
        yield self.snapshot().active_leases, {}

    def ready_queue_depth(self) -> Iterator[tuple[float, dict[str, str]]]:
        depth = self.snapshot().ready_queue_depth
        if depth >= 0:
            yield depth, {}

    def _slo(self, key: str) -> Iterator[tuple[float, dict[str, str]]]:
        value = (self.snapshot().slo or {}).get(key)
        if value is not None:
            yield value, {}


def register_installation_metrics(
    *,
    store_factory: Callable[[], Any],
    get_ready_queue_depth: Callable[[], int],
) -> InstallationMetrics | None:
    """Register the installation instruments; call once, from the control plane."""
    if not instruments.metrics_enabled():
        return None
    metrics = InstallationMetrics(
        store_factory=store_factory, get_ready_queue_depth=get_ready_queue_depth
    )
    definitions = (
        (PROJECTS, "{project}", "Configured projects.", metrics.projects),
        (JOBS, "{job}", "Jobs by status.", metrics.jobs),
        (PROJECT_LEASES, "{lease}", "Active project execution leases.", metrics.leases),
        (READY_QUEUE_DEPTH, "{message}", "Ready queue depth.", metrics.ready_queue_depth),
        (
            SLO_P95_LATENCY,
            "s",
            "Core workflow p95 latency over 24h.",
            lambda: metrics._slo("p95_latency_seconds"),
        ),
        (
            SLO_RETRY_RATE,
            "1",
            "Event dispatch retry rate over 24h.",
            lambda: metrics._slo("retry_rate"),
        ),
        (
            SLO_DEAD_LETTER_RATE,
            "1",
            "Event dead-letter rate over 24h.",
            lambda: metrics._slo("dead_letter_rate"),
        ),
        (SLO_STUCK_JOBS, "{job}", "Current stuck jobs.", lambda: metrics._slo("stuck_jobs")),
    )
    for name, unit, description, observe in definitions:
        instruments.register_observable(
            name, kind="gauge", unit=unit, description=description, observe=observe
        )
    return metrics


__all__ = [
    "JOBS",
    "JOB_STATUSES",
    "PROJECTS",
    "PROJECT_LEASES",
    "READY_QUEUE_DEPTH",
    "SLO_DEAD_LETTER_RATE",
    "SLO_P95_LATENCY",
    "SLO_RETRY_RATE",
    "SLO_STUCK_JOBS",
    "InstallationMetrics",
    "register_installation_metrics",
]
