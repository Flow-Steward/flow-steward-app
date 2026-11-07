"""Reusable report generation and persisted report retrieval."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from core.application.auth.principal import coerce_optional_project_id
from core.application.management.errors import (
    ACCESS_DENIED_MESSAGE,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.guards import require_project_access
from core.application.management.scope import ScopeContext, normalize_scope
from core.application.reports.analytics_service import AnalyticsService
from core.application.reports.dashboard_metric_values import (
    _dashboard_format_duration,
    _dashboard_format_percent,
)
from core.application.reports.reporting_helpers import (
    _entity_id,
    _json_obj,
    _safe_float,
    _safe_int,
    _series_has_values,
    _tenant_id,
)
from core.application.reports.ui_projection_helpers import (
    _build_usage_breakdown,
    _humanize_identifier,
)
from core.application.reports.ui_projection_usage import _usage_row_total
from core.infrastructure.db import report_run_store

JOB_ID_IS_REQUIRED_MESSAGE = "job_id is required"
REPORT_NOT_FOUND_MESSAGE = "Report not found"


SUPPORTED_REPORT_TYPES = frozenset(
    {
        "job_summary",
        "job_usage_breakdown",
        "activity_daily",
        "core_health_summary",
        "project_summary",
        "project_stats",
    }
)


_RETIRED_ACCOUNTING_KEYS = frozenset(
    {
        "cost",
        "costs",
        "cost_breakdown",
        "cost_breakdown_raw",
        "cost_usd",
        "known_cost_usd",
        "total_cost_usd",
        "cost_knownness",
        "cost_display",
        "total_cost_display",
        "unknown_cli_tokens",
        "unknown_cost_calls",
        "spend",
        "spend_usd",
        "total_spend_usd",
        "spend_by_agent",
        "spend_breakdown",
        "spend_availability",
        "spend_known_count",
        "spend_unknown_count",
        "top_cost_center",
        "top_spenders",
    }
)

# Only these platform-owned report/chart containers carried accounting fields.
# Diagnostic objects (for example result.root_cause) may contain business costs.
_ACCOUNTING_REPORT_CONTAINERS = frozenset(
    {"kpis", "highlights", "daily", "dataset", "formatted", "breakdown", "rows", "series", "data"}
)
_LEGACY_ACCOUNTING_REPORT_TYPES = frozenset(
    {"job_summary", "activity_daily", "project_summary", "project_stats"}
)


def _contains_retired_accounting_fields(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in _RETIRED_ACCOUNTING_KEYS
            or (key in _ACCOUNTING_REPORT_CONTAINERS and _contains_retired_accounting_fields(item))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_retired_accounting_fields(item) for item in value)
    return False


class ReportingService:
    def __init__(
        self,
        *,
        get_logger_only: Callable[[], Any],
        job_query_service: Callable[[], Any],
        analytics_service: AnalyticsService | None = None,
    ):
        self._get_logger_only = get_logger_only
        self._job_query_service = job_query_service
        self._analytics = analytics_service or AnalyticsService(
            get_logger_only=get_logger_only,
            job_query_service=job_query_service,
        )

    def _logger_or_raise(self) -> Any:
        bl = self._get_logger_only()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        return bl

    @staticmethod
    def _scope(scope: ScopeContext | dict[str, Any] | None) -> ScopeContext:
        return normalize_scope(scope)

    def _ensure_project_access(
        self,
        scope: ScopeContext,
        *,
        project_id: Any = None,
    ) -> str | None:
        return self._analytics.resolve_project_filter(
            scope, bl=self._logger_or_raise(), project_id=project_id
        )

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
        chart_payload = ReportingService._chart_directive(
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

    def _project_activity_snapshot(
        self,
        *,
        scope: ScopeContext,
        params: dict[str, Any],
        report_type: str,
    ) -> dict[str, Any]:
        project_id = self._ensure_project_access(
            scope, project_id=params.get("project_id") or scope.project_id
        )
        activity = self._analytics.analytics_query(
            "dashboard_activity_daily",
            params,
            scope=scope,
        )
        rows = list(activity.get("rows") or [])
        summary = _json_obj(activity.get("summary"))
        window = _json_obj(activity.get("window"))

        project_name = None
        if project_id is not None:
            bl = self._logger_or_raise()
            if hasattr(bl, "get_project"):
                project = bl.get_project(project_id)
                if isinstance(project, dict):
                    project_name = str(project.get("name") or "").strip() or None

        workflow_id = activity.get("workflow_id")
        scope_label = project_name or (
            f"project {project_id}" if project_id is not None else "this account"
        )
        if workflow_id:
            scope_label = f"{scope_label} · workflow {workflow_id}"
        latest_day = self._latest_active_day(rows)
        active_days = sum(1 for row in rows if _safe_int(row.get("total_jobs")) > 0)

        payload = {
            "workflow_id": workflow_id,
            "scope": {
                "project_ids": [project_id] if project_id else sorted(scope.allowed_project_ids),
                "workflow_ids": [workflow_id]
                if workflow_id
                else sorted(scope.allowed_workflow_ids),
                "workspace_ids": [scope.workspace_id]
                if scope.workspace_id
                else sorted(scope.allowed_workspace_ids),
            },
            "project_id": project_id,
            "project_name": project_name,
            "window": window,
            "kpis": summary,
            "daily": rows,
            "highlights": {
                "scope_label": scope_label,
                "window_label": self._window_label(window),
                "latest_active_day": latest_day,
                "active_days": active_days,
            },
            "deep_links": {
                "project": f"/projects/{project_id}" if project_id else "/projects",
                "jobs": "/jobs",
            },
        }
        chart_payload = self._activity_chart_payload(
            scope_label=scope_label,
            rows=rows,
            window=window,
            project_id=project_id,
            report_type=report_type,
        )
        if chart_payload:
            chart_payload["meta"]["workflow_id"] = workflow_id
        return {
            "project_id": project_id,
            "project_name": project_name,
            "scope_label": scope_label,
            "window": window,
            "summary": summary,
            "rows": rows,
            "latest_day": latest_day,
            "active_days": active_days,
            "payload": payload,
            "chart_payload": chart_payload,
        }

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

    def _normalize_params(self, report_type: str, params: dict[str, Any]) -> dict[str, Any]:
        safe_params = dict(params or {})
        normalized_type = str(report_type or "").strip().lower()
        if normalized_type not in SUPPORTED_REPORT_TYPES:
            raise ServiceValidationError(f"Unsupported report_type '{report_type}'")

        if normalized_type in {"job_summary", "job_usage_breakdown"}:
            job_id = _entity_id(safe_params.get("job_id"))
            if job_id is None:
                raise ServiceValidationError(JOB_ID_IS_REQUIRED_MESSAGE)
            return {"job_id": job_id}

        if normalized_type in {"activity_daily", "project_summary", "project_stats"}:
            normalized: dict[str, Any] = {}
            project_id = coerce_optional_project_id(safe_params.get("project_id"))
            if project_id is not None:
                normalized["project_id"] = project_id
            workflow_id = _entity_id(safe_params.get("workflow_id"))
            if workflow_id is not None:
                normalized["workflow_id"] = workflow_id
            if safe_params.get("start_date") and safe_params.get("end_date"):
                normalized["start_date"] = str(safe_params.get("start_date"))
                normalized["end_date"] = str(safe_params.get("end_date"))
            else:
                normalized["range"] = str(safe_params.get("range") or "7d").strip().lower() or "7d"
            return normalized

        return {
            "window_hours": max(1, min(_safe_int(safe_params.get("window_hours"), default=24), 720))
        }

    def _report_job_summary(
        self,
        *,
        scope: ScopeContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = _entity_id(params.get("job_id"))
        if job_id is None:
            raise ServiceValidationError(JOB_ID_IS_REQUIRED_MESSAGE)
        logs_page = self._job_query_service().get_logs_page(job_id, scope=scope).dump()
        incident = self._job_query_service().get_incident_drilldown(job_id, scope=scope).dump()
        usage = self._job_query_service().get_usage_breakdown(job_id, scope=scope).dump()

        job = _json_obj(logs_page.get("job"))
        status = str(job.get("status") or "").strip().lower()
        project_name = str(logs_page.get("project_name") or "").strip() or "this project"
        task_summary = str(job.get("task_summary") or "").strip() or "this workflow"
        duration_ms = job.get("duration_ms")
        duration_seconds = (_safe_float(duration_ms) / 1000.0) if duration_ms is not None else None
        failure_reason = (
            str(logs_page.get("job_failure_reason") or "").strip()
            or str(_json_obj(incident.get("root_cause")).get("message") or "").strip()
        )
        summary_lines = [
            f"Job {job_id} summary",
            f"Status: {self._status_display(status)}",
            f"Workflow: {task_summary}",
            f"Project: {project_name}",
            f"Duration: {self._duration_display(duration_seconds)}",
            f"Events: {len(logs_page.get('events') or [])}",
            f"Agent messages: {len(logs_page.get('messages') or [])}",
            f"Next step: {self._job_next_step(status)}",
        ]
        if failure_reason:
            summary_lines.append(f"Root cause: {failure_reason}")
        elif status not in {
            "completed",
            "pending",
            "resume_pending",
            "in_progress",
            "awaiting_approval",
            "awaiting_timer",
            "awaiting_child",
        }:
            summary_lines.append("Root cause: No explicit failure reason was recorded.")
        summary_text = "\n".join(summary_lines)

        payload = {
            "job": {
                "id": job_id,
                "project_id": coerce_optional_project_id(job.get("project_id")),
                "project_name": project_name,
                "task_summary": task_summary,
                "status": status,
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "duration_ms": duration_ms,
            },
            "usage": _build_usage_breakdown(usage),
            "result": {
                "failure_reason": failure_reason or None,
                "root_cause": _json_obj(incident.get("root_cause")),
            },
            "counts": {
                "log_entries": len(logs_page.get("logs") or []),
                "events": len(logs_page.get("events") or []),
                "messages": len(logs_page.get("messages") or []),
            },
            "deep_links": {
                "job": f"/jobs/{job_id}",
                "logs": f"/jobs/{job_id}",
                "incident": f"/jobs/{job_id}",
            },
        }
        return {
            "summary_text": summary_text,
            "payload": payload,
            "chart_payload": {},
            "project_id": payload["job"]["project_id"],
        }

    def _report_job_usage_breakdown(
        self,
        *,
        scope: ScopeContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = _entity_id(params.get("job_id"))
        if job_id is None:
            raise ServiceValidationError(JOB_ID_IS_REQUIRED_MESSAGE)
        dataset = self._analytics.analytics_query(
            "job_usage_breakdown",
            {"job_id": job_id},
            scope=scope,
        )
        formatted = _json_obj(dataset.get("formatted"))
        logs_page = (
            self._job_query_service().get_logs_page(job_id, initial_limit=1, scope=scope).dump()
        )
        job = _json_obj(logs_page.get("job"))
        project_id = coerce_optional_project_id(job.get("project_id"))

        raw_rows = list(_json_obj(dataset.get("payload")).get("breakdown") or [])
        agent_order: list[str] = []
        agent_model_usage: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        model_totals: dict[str, float] = defaultdict(float)
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            agent = str(row.get("agent") or "unknown").strip() or "unknown"
            model = str(row.get("model_used") or "Unspecified").strip() or "Unspecified"
            if agent not in agent_order:
                agent_order.append(agent)
            tokens = _usage_row_total(row)
            agent_model_usage[agent][model] += tokens
            model_totals[model] += tokens

        ordered_models = [
            model
            for model, _ in sorted(model_totals.items(), key=lambda item: item[1], reverse=True)
        ]
        if len(ordered_models) <= 1:
            chart_series = [
                {
                    "name": "Tokens",
                    "data": [
                        round(sum(agent_model_usage[agent].values()), 6) for agent in agent_order
                    ],
                }
            ]
            chart_type = "bar"
        else:
            chart_series = [
                {
                    "name": model,
                    "data": [
                        round(agent_model_usage[agent].get(model, 0.0), 6) for agent in agent_order
                    ],
                }
                for model in ordered_models
            ]
            chart_type = "stacked_bar"

        total_calls = sum(_safe_int(row.get("calls")) for row in (formatted.get("rows") or []))
        summary_lines = [
            f"Job {job_id} usage stats",
            f"Model calls: {total_calls}",
            f"Tokens: {formatted.get('total_tokens_display') or '0'}",
        ]
        summary_text = "\n".join(summary_lines)

        chart_payload = self._chart_directive(
            chart_type=chart_type,
            title=f"Job {job_id} Usage Breakdown",
            dataset={
                "x": [_humanize_identifier(agent) or agent for agent in agent_order],
                "series": chart_series,
            },
            meta={
                "report_type": "job_usage_breakdown",
                "unit": "tokens",
                "job_id": job_id,
            },
            empty_state={
                "title": "No model activity",
                "message": "This job has no recorded token usage yet.",
            },
        )
        if not chart_series or not any(_series_has_values(series) for series in chart_series):
            chart_payload = {}

        payload = {
            "job_id": job_id,
            "project_id": project_id,
            "project_name": str(logs_page.get("project_name") or "").strip() or None,
            "table": formatted,
        }
        return {
            "summary_text": summary_text,
            "payload": payload,
            "chart_payload": chart_payload,
            "project_id": project_id,
        }

    def _report_activity_daily(
        self,
        *,
        scope: ScopeContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = self._project_activity_snapshot(
            scope=scope,
            params=params,
            report_type="activity_daily",
        )
        summary = snapshot["summary"]
        latest_day = snapshot["latest_day"] or {}
        latest_day_text = ""
        if latest_day:
            latest_day_text = (
                f"Latest active day: {latest_day.get('date')} "
                f"({int(latest_day.get('completed_count') or 0)} completed, "
                f"{int(latest_day.get('failed_count') or 0)} failed)."
            )
        summary_lines = [
            f"Activity report for {snapshot['scope_label']}",
            f"Window: {self._window_label(snapshot['window'])}",
            (
                f"Finished jobs: {int(summary.get('total_jobs') or 0)} "
                f"({int(summary.get('completed_count') or 0)} completed, "
                f"{int(summary.get('failed_count') or 0)} failed)"
            ),
            f"Success rate: {self._percent_display(summary.get('overall_success_rate_pct'))}",
            (
                "Typical duration: "
                f"p50 {self._duration_display(summary.get('median_p50_seconds'))} · "
                f"p95 {self._duration_display(summary.get('median_p95_seconds'))}"
            ),
        ]
        if latest_day_text:
            summary_lines.append(latest_day_text)
        summary_text = "\n".join(summary_lines)
        return {
            "summary_text": summary_text,
            "payload": snapshot["payload"],
            "chart_payload": snapshot["chart_payload"],
            "project_id": snapshot["project_id"],
        }

    def _report_project_summary(
        self,
        *,
        scope: ScopeContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = self._project_activity_snapshot(
            scope=scope,
            params=params,
            report_type="project_summary",
        )
        summary = snapshot["summary"]
        latest_day = snapshot["latest_day"] or {}
        summary_lines = [
            f"{snapshot['scope_label']} summary",
            f"Window: {self._window_label(snapshot['window'])}",
            (
                f"Finished jobs: {int(summary.get('total_jobs') or 0)} "
                f"({int(summary.get('completed_count') or 0)} completed, "
                f"{int(summary.get('failed_count') or 0)} failed)"
            ),
            f"Success rate: {self._percent_display(summary.get('overall_success_rate_pct'))}",
            (
                "Typical duration: "
                f"p50 {self._duration_display(summary.get('median_p50_seconds'))} · "
                f"p95 {self._duration_display(summary.get('median_p95_seconds'))}"
            ),
        ]
        if latest_day:
            summary_lines.append(
                f"Latest active day: {latest_day.get('date')} "
                f"({int(latest_day.get('completed_count') or 0)} completed, "
                f"{int(latest_day.get('failed_count') or 0)} failed)"
            )
        summary_lines.append(
            f"Next step: {self._project_next_step(summary, snapshot['latest_day'])}"
        )
        return {
            "summary_text": "\n".join(summary_lines),
            "payload": snapshot["payload"],
            "chart_payload": snapshot["chart_payload"],
            "project_id": snapshot["project_id"],
        }

    def _report_project_stats(
        self,
        *,
        scope: ScopeContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = self._project_activity_snapshot(
            scope=scope,
            params=params,
            report_type="project_stats",
        )
        summary = snapshot["summary"]
        summary_lines = [
            f"{snapshot['scope_label']} stats",
            f"Window: {self._window_label(snapshot['window'])}",
            f"Total jobs: {int(summary.get('total_jobs') or 0)}",
            f"Completed: {int(summary.get('completed_count') or 0)}",
            f"Failed: {int(summary.get('failed_count') or 0)}",
            f"Success rate: {self._percent_display(summary.get('overall_success_rate_pct'))}",
            f"Median p50 duration: {self._duration_display(summary.get('median_p50_seconds'))}",
            f"Median p95 duration: {self._duration_display(summary.get('median_p95_seconds'))}",
            f"Active days: {snapshot['active_days']}",
        ]
        return {
            "summary_text": "\n".join(summary_lines),
            "payload": snapshot["payload"],
            "chart_payload": snapshot["chart_payload"],
            "project_id": snapshot["project_id"],
        }

    def _report_core_health_summary(
        self,
        *,
        scope: ScopeContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        dataset = self._analytics.analytics_query(
            "core_slo_summary",
            params,
            scope=scope,
        )
        metrics = _json_obj(dataset.get("metrics"))
        window_hours = _safe_int(dataset.get("window_hours"), default=24)
        retry_rate_pct = _safe_float(metrics.get("retry_rate")) * 100.0
        dead_letter_rate_pct = _safe_float(metrics.get("dead_letter_rate")) * 100.0
        per_agent = list(metrics.get("per_agent_failure_rate") or [])

        summary_text = (
            f"Core health over the last {window_hours}h shows p95 latency at "
            f"{self._duration_display(metrics.get('p95_latency_seconds'))}, "
            f"retry rate at {self._percent_display(retry_rate_pct)}, "
            f"dead-letter rate at {self._percent_display(dead_letter_rate_pct)}, "
            f"and {int(metrics.get('stuck_jobs') or 0)} stuck jobs."
        )

        chart_payload = self._chart_directive(
            chart_type="bar",
            title=f"Per-Agent Failure Rate - Last {window_hours}h",
            dataset={
                "x": [
                    _humanize_identifier(row.get("agent")) or str(row.get("agent") or "Unknown")
                    for row in per_agent
                    if isinstance(row, dict)
                ],
                "series": [
                    {
                        "name": "Failure %",
                        "data": [
                            round(_safe_float(row.get("failure_rate")) * 100.0, 2)
                            for row in per_agent
                            if isinstance(row, dict)
                        ],
                    }
                ],
            },
            meta={
                "report_type": "core_health_summary",
                "unit": "percent",
                "window_hours": window_hours,
            },
            empty_state={
                "title": "No failure rate samples",
                "message": "Per-agent failure data is not available for this window.",
            },
        )
        if not per_agent:
            chart_payload = {}

        payload = {
            "window_hours": window_hours,
            "metrics": metrics,
        }
        return {
            "summary_text": summary_text,
            "payload": payload,
            "chart_payload": chart_payload,
            "project_id": None,
        }

    def _build_report(
        self,
        report_type: str,
        params: dict[str, Any],
        *,
        scope: ScopeContext,
    ) -> dict[str, Any]:
        normalized_type = str(report_type or "").strip().lower()
        if normalized_type in {"job_summary", "job_usage_breakdown"}:
            self._job_query_service().validate_job_diagnostic_report_access(
                str(params.get("job_id") or ""), scope=scope
            )
        if normalized_type == "job_summary":
            return self._report_job_summary(scope=scope, params=params)
        if normalized_type == "job_usage_breakdown":
            return self._report_job_usage_breakdown(scope=scope, params=params)
        if normalized_type == "activity_daily":
            return self._report_activity_daily(scope=scope, params=params)
        if normalized_type == "project_summary":
            return self._report_project_summary(scope=scope, params=params)
        if normalized_type == "project_stats":
            return self._report_project_stats(scope=scope, params=params)
        if normalized_type == "core_health_summary":
            return self._report_core_health_summary(scope=scope, params=params)
        raise ServiceValidationError(f"Unsupported report_type '{report_type}'")

    def _persist_report(
        self,
        *,
        scope: ScopeContext,
        report_type: str,
        params: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        bl = self._logger_or_raise()
        row = report_run_store.create_report_run(
            conn=bl.conn,
            account_id=scope.account_id,
            tenant_id=_tenant_id(getattr(scope, "tenant_id", None)),
            project_id=coerce_optional_project_id(result.get("project_id")),
            report_type=report_type,
            params=params,
            summary_text=str(result.get("summary_text") or ""),
            payload=_json_obj(result.get("payload")),
            chart_payload=_json_obj(result.get("chart_payload")),
            status="generated",
            generated_by=scope.actor_name,
        )
        bl.conn.commit()
        return row

    def report_generate(
        self,
        report_type: str,
        params: dict[str, Any],
        *,
        scope: ScopeContext | dict[str, Any] | None,
        persist: bool = True,
    ) -> dict[str, Any]:
        resolved_scope = self._scope(scope)
        normalized_params = self._normalize_params(report_type, params)
        result = self._build_report(report_type, normalized_params, scope=resolved_scope)
        created_at = datetime.now(UTC).isoformat()
        report_id = None
        status = "generated"

        if persist:
            row = self._persist_report(
                scope=resolved_scope,
                report_type=report_type,
                params=normalized_params,
                result=result,
            )
            report_id = row.get("id")
            created_at_value = row.get("created_at")
            created_at_formatter = getattr(created_at_value, "isoformat", None)
            if callable(created_at_formatter):
                created_at = str(created_at_formatter())
            else:
                created_at = str(created_at_value or created_at)
            status = str(row.get("status") or "generated")

        chart_payload = _json_obj(result.get("chart_payload"))
        return {
            "id": str(report_id) if report_id else None,
            "report_type": str(report_type or "").strip().lower(),
            "status": status,
            "params": normalized_params,
            "summary_text": str(result.get("summary_text") or ""),
            "payload": _json_obj(result.get("payload")),
            "chart_payload": chart_payload,
            "directives": [chart_payload] if chart_payload else [],
            "created_at": created_at,
        }

    def _enforce_report_scope(self, scope: ScopeContext, row: dict[str, Any]) -> None:
        if str(row.get("account_id") or "").strip() != scope.account_id:
            raise ServiceNotFoundError(REPORT_NOT_FOUND_MESSAGE)
        row_tenant_id = _tenant_id(row.get("tenant_id"))
        scope_tenant_id = _tenant_id(getattr(scope, "tenant_id", None))
        if row_tenant_id != scope_tenant_id:
            raise ServiceNotFoundError(REPORT_NOT_FOUND_MESSAGE)
        params = _json_obj(row.get("params_jsonb"))
        report_type = str(row.get("report_type") or "").strip().lower()
        if report_type not in SUPPORTED_REPORT_TYPES:
            raise ServiceNotFoundError(REPORT_NOT_FOUND_MESSAGE)
        project_id = coerce_optional_project_id(row.get("project_id"))
        if report_type in {"job_summary", "job_usage_breakdown"}:
            self._job_query_service().validate_job_diagnostic_report_access(
                str(params.get("job_id") or ""), scope=scope
            )
            return
        if project_id is not None:
            require_project_access(scope, {"id": project_id, "account_id": scope.account_id})
        if report_type not in {"activity_daily", "project_summary", "project_stats"}:
            return
        workspace_id = None
        if project_id is not None and (scope.allowed_workspace_ids or scope.workspace_id):
            bl = self._logger_or_raise()
            project = bl.get_project(project_id)
            if not isinstance(project, dict):
                raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
            require_project_access(scope, project)
            workspace_id = str(project.get("workspace_id") or "").strip() or None
        saved_scope = _json_obj(_json_obj(row.get("payload_jsonb")).get("scope"))
        for allowed, exact, selected, key in (
            (scope.allowed_project_ids, scope.project_id, project_id, "project_ids"),
            (
                scope.allowed_workflow_ids,
                scope.workflow_id,
                params.get("workflow_id") or _json_obj(row.get("payload_jsonb")).get("workflow_id"),
                "workflow_ids",
            ),
            (scope.allowed_workspace_ids, scope.workspace_id, workspace_id, "workspace_ids"),
        ):
            saved = {str(selected)} if selected else set(saved_scope.get(key) or [])
            if exact and (not saved or not saved.issubset({str(exact)})):
                raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
            if allowed and (not saved or not saved.issubset(allowed)):
                raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)

    def report_get(
        self,
        report_id: str | UUID,
        *,
        scope: ScopeContext | dict[str, Any] | None,
    ) -> dict[str, Any]:
        resolved_scope = self._scope(scope)
        bl = self._logger_or_raise()
        row = report_run_store.get_report_run(
            conn=bl.conn,
            report_id=report_id,
            account_id=resolved_scope.account_id,
            tenant_id=_tenant_id(resolved_scope.tenant_id),
        )
        if not isinstance(row, dict):
            raise ServiceNotFoundError(REPORT_NOT_FOUND_MESSAGE)
        self._enforce_report_scope(resolved_scope, row)
        report_type = str(row.get("report_type") or "").strip().lower()
        if report_type in _LEGACY_ACCOUNTING_REPORT_TYPES and not _json_obj(
            row.get("payload_jsonb")
        ):
            # Without a snapshot we cannot establish that stored text/charts are
            # usage-only. Keep the row intact; the operator can generate a new report.
            raise ServiceNotFoundError(REPORT_NOT_FOUND_MESSAGE)
        if any(
            _contains_retired_accounting_fields(_json_obj(row.get(field)))
            for field in ("payload_jsonb", "chart_payload_jsonb")
        ):
            raise ServiceNotFoundError(REPORT_NOT_FOUND_MESSAGE)

        persisted_params = _json_obj(row.get("params_jsonb"))
        payload: dict[str, Any] = {}
        has_payload_snapshot = "payload_jsonb" in row and row.get("payload_jsonb") is not None
        if has_payload_snapshot:
            payload = _json_obj(row.get("payload_jsonb"))
        else:
            try:
                regenerated = self._build_report(
                    report_type, persisted_params, scope=resolved_scope
                )
                payload = _json_obj(regenerated.get("payload"))
            except ServiceError:
                payload = {}

        chart_payload = _json_obj(row.get("chart_payload_jsonb"))
        created_at_value = row.get("created_at")
        created_at_formatter = getattr(created_at_value, "isoformat", None)
        if callable(created_at_formatter):
            created_at = str(created_at_formatter())
        else:
            created_at = str(created_at_value or "")
        return {
            "id": str(row.get("id") or report_id),
            "report_type": report_type,
            "status": str(row.get("status") or "generated"),
            "params": persisted_params,
            "summary_text": str(row.get("summary_text") or ""),
            "payload": payload,
            "chart_payload": chart_payload,
            "directives": [chart_payload] if chart_payload else [],
            "created_at": created_at,
            "generated_by": str(row.get("generated_by") or "") or None,
        }
