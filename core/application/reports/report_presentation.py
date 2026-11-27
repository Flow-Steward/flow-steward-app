"""Presentation helpers shared by the report builders of ``ReportingService``.

These helpers only shape already-loaded report data into display strings and
chart directives; they read nothing and enforce no access rules.
"""

from __future__ import annotations

from typing import Any

from core.application.reports.dashboard_metric_values import (
    _dashboard_format_duration,
    _dashboard_format_percent,
)
from core.application.reports.reporting_helpers import _safe_int


class ReportPresentationMixin:
    """Display labels, next-step hints and chart directives for reports."""

    @staticmethod
    def _chart_directive(
        *,
        chart_type: str,
        title: str,
        dataset: dict[str, Any],
        subtitle: str = "",
        meta: dict[str, Any] | None = None,
        empty_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "type": "chart",
            "chart_library": "echarts",
            "chart_type": chart_type,
            "title": title,
            "dataset": dataset,
        }
        if subtitle:
            payload["subtitle"] = subtitle
        if meta:
            payload["meta"] = meta
        if empty_state:
            payload["empty_state"] = empty_state
        return payload

    @staticmethod
    def _duration_display(duration_seconds: float | None) -> str:
        return _dashboard_format_duration(duration_seconds)

    @staticmethod
    def _percent_display(value: float | None) -> str:
        return _dashboard_format_percent(value)

    @staticmethod
    def _status_display(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return "Unknown"
        return normalized.replace("_", " ")

    @staticmethod
    def _job_next_step(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized == "awaiting_approval":
            return "Review the pending approval so the workflow can continue."
        if normalized == "awaiting_timer":
            return "No action is required: the run resumes by itself when its Wait step is due."
        if normalized == "awaiting_child":
            return "No action is required: the run resumes when its child workflow finishes."
        if normalized == "awaiting_event":
            return (
                "No action is required: the run resumes when its event arrives or fails "
                "when the Wait step times out."
            )
        if normalized in {"pending", "resume_pending", "in_progress"}:
            return "Wait for the run to finish or open the live logs for more detail."
        if normalized == "completed":
            return "No action is required unless you want to inspect logs or outputs."
        if normalized in {
            "failed",
            "cancelled",
            "max_iterations",
            "retryable_interrupted_step",
        }:
            return "Open diagnostics or logs to inspect the blocking step before retrying."
        return "Open the job detail page for the latest workflow state."

    @staticmethod
    def _window_label(window: dict[str, Any]) -> str:
        range_key = str(window.get("range_key") or "").strip().lower()
        if range_key == "24h":
            return "last 24h"
        if range_key == "7d":
            return "last 7d"
        if range_key == "30d":
            return "last 30d"
        start = str(window.get("start") or "").strip()
        end = str(window.get("end") or "").strip()
        if start and end:
            return f"{start[:10]} to {end[:10]}"
        return "selected window"

    @staticmethod
    def _latest_active_day(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        for row in reversed(rows):
            if _safe_int(row.get("total_jobs")) > 0:
                return dict(row)
        return None

    @staticmethod
    def _activity_chart_payload(
        *,
        scope_label: str,
        rows: list[dict[str, Any]],
        window: dict[str, Any],
        project_id: str | None,
        report_type: str,
    ) -> dict[str, Any]:
        chart_payload = ReportPresentationMixin._chart_directive(
            chart_type="stacked_bar",
            title="Success vs Failure Trend",
            subtitle=scope_label,
            dataset={
                "x": [str(row.get("date") or "") for row in rows],
                "series": [
                    {
                        "name": "Completed",
                        "data": [_safe_int(row.get("completed_count")) for row in rows],
                    },
                    {
                        "name": "Failed",
                        "data": [_safe_int(row.get("failed_count")) for row in rows],
                    },
                ],
            },
            meta={
                "report_type": report_type,
                "unit": "jobs",
                "window": window,
                "project_id": project_id,
            },
            empty_state={
                "title": "No finished jobs",
                "message": "No completed or failed jobs were found for the selected window.",
            },
        )
        if not rows or not any(
            _safe_int(row.get("completed_count")) or _safe_int(row.get("failed_count"))
            for row in rows
        ):
            return {}
        return chart_payload

    @staticmethod
    def _project_next_step(summary: dict[str, Any], latest_day: dict[str, Any] | None) -> str:
        failed_count = _safe_int(summary.get("failed_count"))
        total_jobs = _safe_int(summary.get("total_jobs"))
        if total_jobs <= 0:
            return "No finished jobs in this window. Run a job or widen the report range."
        if failed_count > 0:
            return "Open recent failed jobs and approvals to inspect what is blocking this project."
        if latest_day and _safe_int(latest_day.get("total_jobs")) > 0:
            return (
                "Project looks stable. Review the latest jobs if you want a run-by-run breakdown."
            )
        return "Project looks stable. Open the project job list for more detail."
