"""Scoped analytics datasets for reports and future report cards."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.auth.principal import coerce_optional_project_id
from core.application.management.errors import (
    ACCESS_DENIED_MESSAGE,
    ServiceForbiddenError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.guards import require_project_access, require_workflow_access
from core.application.management.scope import ScopeContext, normalize_scope
from core.application.reports import dashboard_metrics_helpers as dashboard_helpers
from core.application.reports.dashboard_metric_values import _dashboard_percentile
from core.application.reports.ui_projection_helpers import _build_cost_breakdown


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _utc_start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)


def _entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_activity_window(
    filters: dict[str, Any],
) -> tuple[datetime, datetime, date, date, str]:
    start_date = _parse_date(filters.get("start_date"))
    end_date = _parse_date(filters.get("end_date"))
    range_key = ""
    if start_date is None or end_date is None:
        range_key = dashboard_helpers._dashboard_normalize_range(str(filters.get("range") or "7d"))
        hours = int(
            dashboard_helpers._DASHBOARD_RANGE_HOURS.get(
                range_key,
                dashboard_helpers._DASHBOARD_RANGE_HOURS[
                    dashboard_helpers._DASHBOARD_DEFAULT_RANGE
                ],
            )
        )
        end_dt = datetime.now(UTC)
        start_dt = end_dt - timedelta(hours=max(1, hours))
        return start_dt, end_dt, start_dt.date(), end_dt.date(), range_key

    if end_date < start_date:
        raise ServiceValidationError("end_date must be on or after start_date")

    start_dt = _utc_start_of_day(start_date)
    end_dt = _utc_start_of_day(end_date + timedelta(days=1))
    return start_dt, end_dt, start_date, end_date, range_key


class AnalyticsService:
    def __init__(
        self,
        *,
        get_logger_only: Callable[[], Any],
        job_query_service: Callable[[], Any] | None = None,
    ):
        self._get_logger_only = get_logger_only
        self._job_query_service = job_query_service or (lambda: None)

    def _logger_or_raise(self) -> Any:
        bl = self._get_logger_only()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        return bl

    @staticmethod
    def _scope(scope: ScopeContext | dict[str, Any] | None) -> ScopeContext:
        return normalize_scope(scope)

    def resolve_project_filter(
        self,
        scope: ScopeContext,
        *,
        bl: Any,
        project_id: Any = None,
    ) -> str | None:
        normalized_project_id = coerce_optional_project_id(project_id)
        if normalized_project_id is None:
            return None
        if scope.project_id and normalized_project_id != str(scope.project_id):
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)

        project = {"id": normalized_project_id, "account_id": scope.account_id}
        if hasattr(bl, "get_project"):
            resolved = bl.get_project(normalized_project_id)
            if not isinstance(resolved, dict):
                raise ServiceValidationError("Project not found")
            project = resolved
        if str(project.get("tenant_id") or DEFAULT_TENANT_ID) != str(
            scope.tenant_id or DEFAULT_TENANT_ID
        ):
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
        require_project_access(scope, project)
        return normalized_project_id

    @staticmethod
    def _activity_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        completed = sum(int(row.get("completed_count") or 0) for row in rows)
        failed = sum(int(row.get("failed_count") or 0) for row in rows)
        total_jobs = completed + failed
        total_spend = sum(_safe_float(row.get("spend_usd")) for row in rows)
        p50_values = [_safe_float(row.get("p50_seconds")) for row in rows if row.get("p50_seconds")]
        p95_values = [_safe_float(row.get("p95_seconds")) for row in rows if row.get("p95_seconds")]
        return {
            "completed_count": completed,
            "failed_count": failed,
            "total_jobs": total_jobs,
            "total_spend_usd": total_spend,
            "overall_success_rate_pct": round((completed / total_jobs) * 100, 1)
            if total_jobs
            else 0.0,
            "median_p50_seconds": _dashboard_percentile(p50_values, 0.5),
            "median_p95_seconds": _dashboard_percentile(p95_values, 0.5),
        }

    @staticmethod
    def _daily_rows_from_facts(
        rows: list[dict[str, Any]],
        *,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            day_value = _parse_date(row.get("finished_date") or row.get("finished_at"))
            if day_value is None:
                continue
            bucket = grouped.setdefault(
                day_value.isoformat(),
                {
                    "date": day_value.isoformat(),
                    "completed_count": 0,
                    "failed_count": 0,
                    "total_jobs": 0,
                    "success_rate_pct": 0.0,
                    "spend_usd": 0.0,
                    "_durations": [],
                },
            )
            result = str(row.get("result") or row.get("status") or "").strip().lower()
            if result == "completed":
                bucket["completed_count"] += 1
            else:
                bucket["failed_count"] += 1
            bucket["total_jobs"] += 1
            bucket["spend_usd"] += _safe_float(row.get("cost_usd"))
            duration_value = row.get("duration_seconds")
            if duration_value is not None:
                bucket["_durations"].append(_safe_float(duration_value))

        cursor = start_date
        merged: list[dict[str, Any]] = []
        while cursor <= end_date:
            current = grouped.get(
                cursor.isoformat(),
                {
                    "date": cursor.isoformat(),
                    "completed_count": 0,
                    "failed_count": 0,
                    "total_jobs": 0,
                    "success_rate_pct": 0.0,
                    "spend_usd": 0.0,
                    "_durations": [],
                },
            )
            total_jobs = _safe_int(current.get("total_jobs"))
            durations = list(current.get("_durations") or [])
            merged.append(
                {
                    "date": cursor.isoformat(),
                    "completed_count": _safe_int(current.get("completed_count")),
                    "failed_count": _safe_int(current.get("failed_count")),
                    "total_jobs": total_jobs,
                    "success_rate_pct": round(
                        (_safe_int(current.get("completed_count")) / total_jobs) * 100, 1
                    )
                    if total_jobs
                    else 0.0,
                    "spend_usd": round(_safe_float(current.get("spend_usd")), 6),
                    "p50_seconds": _dashboard_percentile(durations, 0.5),
                    "p95_seconds": _dashboard_percentile(durations, 0.95),
                }
            )
            cursor += timedelta(days=1)
        return merged

    def _job_cost_breakdown(
        self,
        *,
        scope: ScopeContext,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = _entity_id(filters.get("job_id"))
        if job_id is None:
            raise ServiceValidationError("job_id is required")
        query_service = self._job_query_service()
        if query_service is None:
            raise ServiceUnavailableError("Query service is unavailable")
        payload = query_service.get_cost_breakdown(job_id, scope=scope).dump()
        return {
            "dataset_id": "job_cost_breakdown",
            "job_id": job_id,
            "payload": payload,
            "formatted": _build_cost_breakdown(payload),
        }

    def _activity_facts(
        self,
        *,
        bl: Any,
        scope: ScopeContext,
        filters: dict[str, Any],
        project_id: str | None,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict[str, Any]]:
        workflow_id = _entity_id(filters.get("workflow_id"))
        if workflow_id:
            require_workflow_access(scope, workflow_id)
            if scope.workflow_id and workflow_id != scope.workflow_id:
                raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
        if not hasattr(bl, "list_report_activity_facts"):
            raise ServiceUnavailableError("Dashboard activity datasets are unavailable")
        return bl.list_report_activity_facts(
            scope=scope,
            project_id=project_id,
            workflow_id=workflow_id,
            window_start=start_dt,
            window_end=end_dt,
        )

    def _dashboard_activity_daily(
        self, *, scope: ScopeContext, filters: dict[str, Any]
    ) -> dict[str, Any]:
        bl = self._logger_or_raise()
        project_id = self.resolve_project_filter(
            scope, bl=bl, project_id=filters.get("project_id") or scope.project_id
        )
        start_dt, end_dt, start_date, end_date, range_key = _normalize_activity_window(filters)
        # Account rollups lack tenant and membership dimensions; always use scoped facts.
        facts = self._activity_facts(
            bl=bl,
            scope=scope,
            filters=filters,
            project_id=project_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        rows = self._daily_rows_from_facts(facts, start_date=start_date, end_date=end_date)
        return {
            "dataset_id": "dashboard_activity_daily",
            "project_id": project_id,
            "workflow_id": _entity_id(filters.get("workflow_id")) or scope.workflow_id,
            "window": {
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "range_key": range_key,
            },
            "rows": rows,
            "summary": self._activity_summary(rows),
        }

    def _dashboard_spend_by_agent(
        self,
        *,
        scope: ScopeContext,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        bl = self._logger_or_raise()
        if not hasattr(bl, "list_report_activity_facts"):
            raise ServiceUnavailableError("Dashboard spend datasets are unavailable")

        project_id = self.resolve_project_filter(
            scope, bl=bl, project_id=filters.get("project_id") or scope.project_id
        )
        start_dt, end_dt, _, _, range_key = _normalize_activity_window(filters)
        rows = self._activity_facts(
            bl=bl,
            scope=scope,
            filters=filters,
            project_id=project_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )

        grouped: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"agent": "unknown", "cost_usd": 0.0, "job_count": 0}
        )
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (
                project_id is not None
                and coerce_optional_project_id(row.get("project_id")) != project_id
            ):
                continue
            agent = (
                str(row.get("current_agent") or row.get("agent") or "unknown").strip() or "unknown"
            )
            grouped[agent]["agent"] = agent
            grouped[agent]["cost_usd"] += _safe_float(row.get("cost_usd"))
            grouped[agent]["job_count"] += 1

        prepared = sorted(
            (
                {
                    "agent": agent,
                    "cost_usd": round(values["cost_usd"], 6),
                    "job_count": int(values["job_count"]),
                }
                for agent, values in grouped.items()
            ),
            key=lambda row: (row["cost_usd"], row["job_count"]),
            reverse=True,
        )
        return {
            "dataset_id": "dashboard_spend_by_agent",
            "project_id": project_id,
            "window": {
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "range_key": range_key,
            },
            "rows": prepared,
        }

    def _core_slo_summary(
        self,
        *,
        scope: ScopeContext,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        bl = self._logger_or_raise()
        if not hasattr(bl, "get_core_slo_metrics"):
            raise ServiceUnavailableError("Core SLO metrics are unavailable")
        window_hours = max(1, min(_safe_int(filters.get("window_hours"), default=24), 720))
        metrics = (
            bl.get_core_slo_metrics(
                account_id=scope.account_id,
                window_hours=window_hours,
            )
            or {}
        )
        return {
            "dataset_id": "core_slo_summary",
            "metrics": metrics if isinstance(metrics, dict) else {},
            "window_hours": window_hours,
        }

    def analytics_query(
        self,
        dataset_id: str,
        filters: dict[str, Any] | None,
        *,
        scope: ScopeContext | dict[str, Any] | None,
    ) -> dict[str, Any]:
        resolved_scope = self._scope(scope)
        safe_filters = dict(filters or {})
        normalized_dataset = str(dataset_id or "").strip().lower()

        if normalized_dataset == "job_cost_breakdown":
            return self._job_cost_breakdown(scope=resolved_scope, filters=safe_filters)
        if normalized_dataset == "dashboard_activity_daily":
            return self._dashboard_activity_daily(scope=resolved_scope, filters=safe_filters)
        if normalized_dataset == "dashboard_spend_by_agent":
            return self._dashboard_spend_by_agent(scope=resolved_scope, filters=safe_filters)
        if normalized_dataset == "core_slo_summary":
            return self._core_slo_summary(scope=resolved_scope, filters=safe_filters)

        raise ServiceValidationError(f"Unsupported dataset_id '{dataset_id}'")
