"""Helpers for observability-oriented Prometheus metrics payloads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_METRIC_NAMESPACE = "platform"


def _metric_name(suffix: str) -> str:
    normalized = str(suffix or "").strip().replace("-", "_")
    return f"{_METRIC_NAMESPACE}_{normalized}"


def _prometheus_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metrics_line(name: str, value: Any, labels: dict[str, Any] | None = None) -> str:
    if labels:
        parts = [f'{key}="{_prometheus_escape(val)}"' for key, val in sorted(labels.items())]
        return f"{name}{{{','.join(parts)}}} {value}"
    return f"{name} {value}"


def metric_header_lines(*, suffix: str, help_text: str, metric_type: str) -> list[str]:
    metric_name = _metric_name(suffix)
    return [f"# HELP {metric_name} {help_text}", f"# TYPE {metric_name} {metric_type}"]


def active_lease_count(bl) -> int:
    lease_lister = getattr(bl, "list_project_execution_leases", None)
    if callable(lease_lister):
        try:
            rows = lease_lister(active_only=True)
            if isinstance(rows, list):
                return len([row for row in rows if isinstance(row, dict)])
        except Exception:
            pass
    try:
        with bl.conn.cursor() as cur:
            if bl._table_exists("project_execution_slots"):
                cur.execute("SELECT COUNT(*) FROM project_execution_slots WHERE expires_at > now()")
                return int((cur.fetchone() or [0])[0] or 0)
    except Exception:
        return 0
    return 0


def build_prometheus_metrics_payload(
    bl,
    *,
    compute_readiness: Callable[[Any], dict[str, Any]],
    to_json_obj: Callable[[Any], Any],
    get_ready_queue_depth: Callable[[], int],
) -> str:
    readiness = compute_readiness(bl)
    checks = to_json_obj(readiness.get("checks")) or {}

    lines: list[str] = []
    lines.extend(
        metric_header_lines(
            suffix="health_ready",
            help_text="Service readiness state (1 ready, 0 not ready).",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(_metric_name("health_ready"), 1 if bool(readiness.get("ready")) else 0)
    )
    lines.extend(
        metric_header_lines(
            suffix="readiness_check",
            help_text="Readiness check status by check name (1 ok, 0 failed).",
            metric_type="gauge",
        )
    )
    for check_name, payload in sorted(checks.items()):
        payload_obj = to_json_obj(payload) or {}
        lines.append(
            metrics_line(
                _metric_name("readiness_check"),
                1 if bool(payload_obj.get("ok")) else 0,
                labels={"check": check_name},
            )
        )

    project_count = 0
    active_leases = 0
    ready_queue_depth = -1
    job_status_counts: dict[str, int] = {}
    try:
        with bl.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM projects")
            project_count = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM jobs
                GROUP BY status
                """
            )
            for row in cur.fetchall() or []:
                status_value = str(row[0] or "").strip() or "unknown"
                job_status_counts[status_value] = int(row[1] or 0)
            active_leases = active_lease_count(bl)
    except Exception:
        pass
    try:
        ready_queue_depth = get_ready_queue_depth()
    except Exception:
        ready_queue_depth = -1

    lines.extend(
        metric_header_lines(
            suffix="projects_total",
            help_text="Total configured projects.",
            metric_type="gauge",
        )
    )
    lines.append(metrics_line(_metric_name("projects_total"), project_count))
    lines.extend(
        metric_header_lines(
            suffix="jobs_total",
            help_text="Job count grouped by status.",
            metric_type="gauge",
        )
    )
    for status_name, count_value in sorted(job_status_counts.items()):
        lines.append(
            metrics_line(_metric_name("jobs_total"), count_value, labels={"status": status_name})
        )
    lines.extend(
        metric_header_lines(
            suffix="project_leases_active",
            help_text="Active project execution leases.",
            metric_type="gauge",
        )
    )
    lines.append(metrics_line(_metric_name("project_leases_active"), active_leases))
    lines.extend(
        metric_header_lines(
            suffix="ready_queue_depth",
            help_text="Current ready queue depth (-1 unavailable).",
            metric_type="gauge",
        )
    )
    lines.append(metrics_line(_metric_name("ready_queue_depth"), ready_queue_depth))

    slo_payload: dict[str, Any] = {}
    if hasattr(bl, "get_core_slo_metrics"):
        try:
            slo_payload = bl.get_core_slo_metrics(window_hours=24) or {}
        except Exception:
            slo_payload = {}

    if isinstance(slo_payload, dict):
        p95_latency = slo_payload.get("p95_latency_seconds")
        retry_rate = slo_payload.get("retry_rate")
        dead_letter_rate = slo_payload.get("dead_letter_rate")
        stuck_jobs = slo_payload.get("stuck_jobs")
        lines.extend(
            metric_header_lines(
                suffix="slo_p95_latency_seconds",
                help_text="Core workflow p95 latency in seconds (24h).",
                metric_type="gauge",
            )
        )
        lines.append(
            metrics_line(
                _metric_name("slo_p95_latency_seconds"),
                p95_latency if p95_latency is not None else 0,
            )
        )
        lines.extend(
            metric_header_lines(
                suffix="slo_retry_rate",
                help_text="Event dispatch retry rate in the last 24h.",
                metric_type="gauge",
            )
        )
        lines.append(
            metrics_line(
                _metric_name("slo_retry_rate"), retry_rate if retry_rate is not None else 0
            )
        )
        lines.extend(
            metric_header_lines(
                suffix="slo_dead_letter_rate",
                help_text="Event dead-letter rate in the last 24h.",
                metric_type="gauge",
            )
        )
        lines.append(
            metrics_line(
                _metric_name("slo_dead_letter_rate"),
                dead_letter_rate if dead_letter_rate is not None else 0,
            )
        )
        lines.extend(
            metric_header_lines(
                suffix="slo_stuck_jobs",
                help_text="Current number of stuck jobs.",
                metric_type="gauge",
            )
        )
        lines.append(
            metrics_line(
                _metric_name("slo_stuck_jobs"), stuck_jobs if stuck_jobs is not None else 0
            )
        )
        lines.extend(
            metric_header_lines(
                suffix="slo_agent_failure_rate",
                help_text="Per-agent job failure rate in the last 24h.",
                metric_type="gauge",
            )
        )
        for row in slo_payload.get("per_agent_failure_rate") or []:
            if not isinstance(row, dict):
                continue
            lines.append(
                metrics_line(
                    _metric_name("slo_agent_failure_rate"),
                    row.get("failure_rate") if row.get("failure_rate") is not None else 0,
                    labels={"agent": str(row.get("agent") or "unknown")},
                )
            )

    return "\n".join(lines) + "\n"


__all__ = [
    "active_lease_count",
    "build_prometheus_metrics_payload",
    "metric_header_lines",
    "metrics_line",
]
