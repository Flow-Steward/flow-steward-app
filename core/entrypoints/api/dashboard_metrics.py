"""Dashboard summary and core SLO API routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.management.errors import ServiceError, ServiceUnavailableError
from core.application.reports import dashboard_metrics_helpers as _helpers
from core.application.reports.dashboard_wait_metrics import dashboard_wait_kpis


# Runtime-configured dependencies; set by create_dashboard_metrics_router().
def _default_request_scope(request: Request, **kwargs: Any) -> Any:
    return None


def _default_get_logger_only() -> Any:
    return None


def _default_job_query_service() -> Any:
    return None


def _default_service_error_response(exc: Exception, request: Request | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": str(getattr(exc, "message", exc))},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _default_api_error_response(exc: Exception, request: Request | None = None) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


_request_scope: Callable[..., Any] = _default_request_scope
_get_logger_only: Callable[[], Any] = _default_get_logger_only
_get_job_metrics_store: Callable[[], Any] = _default_get_logger_only
_get_slo_store: Callable[[], Any] = _default_get_logger_only
_job_query_service: Callable[[], Any] = _default_job_query_service
_service_error_response: Callable[..., JSONResponse] = _default_service_error_response
_api_error_response: Callable[..., JSONResponse] = _default_api_error_response


def _require_dashboard_ports(job_store: Any, slo_store: Any) -> None:
    missing: list[str] = []
    for name in ("list_jobs_for_attention", "get_jobs_cost_summary"):
        if not callable(getattr(job_store, name, None)):
            missing.append(name)
    if _helpers._DASHBOARD_ROLLUP_READ_ENABLED:
        for name in ("list_dashboard_job_facts", "list_dashboard_daily_rollups"):
            if not callable(getattr(slo_store, name, None)):
                missing.append(name)
    if missing:
        raise ServiceUnavailableError("Dashboard projection is unavailable: " + ", ".join(missing))


def api_dashboard_summary(
    request: Request,
    range: Annotated[str, Query()] = "7d",
):
    range_key = _helpers._dashboard_normalize_range(range)
    range_hours = int(
        _helpers._DASHBOARD_RANGE_HOURS.get(
            range_key,
            _helpers._DASHBOARD_RANGE_HOURS[_helpers._DASHBOARD_DEFAULT_RANGE],
        )
    )
    try:
        scope = _request_scope(request, default_actor="system")
        job_bl = _get_job_metrics_store()
        slo_bl = _get_slo_store()
        _require_dashboard_ports(job_bl, slo_bl)
        now_utc = datetime.now(UTC)
        range_window_start = now_utc - timedelta(hours=max(1, range_hours))
        utc_today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        historical_window_start = range_window_start
        historical_window_end = min(utc_today_start, now_utc)

        rows: list[dict[str, Any]] = []
        historical_rows, historical_truncated, historical_source = (
            _helpers._dashboard_collect_historical_rows(
                bl=slo_bl,
                metrics_bl=job_bl,
                account_id=scope.account_id,
                historical_window_start=historical_window_start,
                historical_window_end=historical_window_end,
            )
        )
        rows.extend(historical_rows)
        truncated = bool(historical_truncated)
        daily_rollups = _helpers._dashboard_collect_daily_rollups(
            bl=slo_bl,
            account_id=scope.account_id,
            historical_source=historical_source,
            historical_window_start=historical_window_start,
            historical_window_end=historical_window_end,
        )

        live_window_start = max(range_window_start, utc_today_start)
        live_rows, live_truncated = _helpers._dashboard_collect_live_rows(
            bl=job_bl,
            account_id=scope.account_id,
            live_window_start=live_window_start,
            now_utc=now_utc,
        )
        rows.extend(live_rows)
        truncated = bool(truncated or live_truncated)

        rows = _helpers._dashboard_dedupe_rows(rows)

        query_service = _job_query_service()
        triage_payload = query_service.list_jobs(
            group_by="attention",
            limit=_helpers._DASHBOARD_TRIAGE_LIMIT,
            scope=scope,
        )
        groups = _helpers._dashboard_groups_payload(triage_payload)
        summary = _helpers._dashboard_compute_summary(
            rows=rows,
            groups=groups,
            range_hours=range_hours,
            daily_rollups=daily_rollups,
        )
        summary["hero_kpis"].update(dashboard_wait_kpis(query_service, scope=scope))
        summary["range"] = range_key
        summary["range_hours"] = range_hours
        summary["metrics_truncated"] = bool(truncated)
        summary["historical_metrics_source"] = historical_source
        if _helpers._DASHBOARD_METRICS_MAX_ROWS > 0:
            summary["metrics_cap"] = int(_helpers._DASHBOARD_METRICS_MAX_ROWS)
        return summary
    except ServiceError as e:
        return _service_error_response(e)
    except Exception as e:
        return _api_error_response(e)


def api_core_slo_metrics(
    request: Request,
    window_hours: Annotated[int, Query(ge=1, le=720)] = 24,
):
    """Return core SLO metrics for the caller account over a time window."""
    try:
        scope = _request_scope(request, default_actor="system")
        bl = _get_slo_store()
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        if not hasattr(bl, "get_core_slo_metrics"):
            raise ServiceUnavailableError("Core SLO metrics are unavailable")
        payload = (
            bl.get_core_slo_metrics(
                account_id=scope.account_id,
                window_hours=int(window_hours),
            )
            or {}
        )
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("window_hours", int(window_hours))
        payload.setdefault("account_id", scope.account_id)
        payload.setdefault("generated_at", datetime.now(UTC).isoformat())
        return payload
    except ServiceError as e:
        return _service_error_response(e, request)
    except Exception as e:
        return _api_error_response(e, request=request)


def create_dashboard_metrics_router(
    *,
    request_scope: Callable[..., Any],
    get_logger_only: Callable[[], Any],
    get_job_metrics_store: Callable[[], Any] | None = None,
    get_slo_store: Callable[[], Any] | None = None,
    job_query_service: Callable[[], Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    dashboard_default_range: str,
    dashboard_range_hours: dict[str, int],
    dashboard_metrics_page_size: int,
    dashboard_metrics_max_rows: int,
    dashboard_triage_limit: int,
    dashboard_rollup_read_enabled: bool,
) -> APIRouter:
    """Configure and return dashboard/SLO routes."""

    global _request_scope
    global _get_logger_only
    global _get_job_metrics_store
    global _get_slo_store
    global _job_query_service
    global _service_error_response
    global _api_error_response

    _request_scope = request_scope
    _get_logger_only = get_logger_only
    _get_job_metrics_store = get_job_metrics_store or get_logger_only
    _get_slo_store = get_slo_store or get_logger_only
    _job_query_service = job_query_service
    _service_error_response = service_error_response
    _api_error_response = api_error_response

    _helpers.configure_dashboard_metrics(
        dashboard_default_range=dashboard_default_range,
        dashboard_range_hours=dashboard_range_hours,
        dashboard_metrics_page_size=dashboard_metrics_page_size,
        dashboard_metrics_max_rows=dashboard_metrics_max_rows,
        dashboard_triage_limit=dashboard_triage_limit,
        dashboard_rollup_read_enabled=dashboard_rollup_read_enabled,
    )

    router = APIRouter(tags=["dashboard-metrics"])
    router.add_api_route(
        "/api/dashboard/summary",
        api_dashboard_summary,
        methods=["GET"],
    )
    router.add_api_route(
        "/api/slo/core",
        api_core_slo_metrics,
        methods=["GET"],
    )
    return router


_dashboard_fetch_cost_by_job = _helpers._dashboard_fetch_cost_by_job


__all__ = [
    "_dashboard_fetch_cost_by_job",
    "create_dashboard_metrics_router",
]
