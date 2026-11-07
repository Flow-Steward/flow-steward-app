from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.reports.dashboard_metrics_helpers import _dashboard_fetch_usage_by_job
from core.entrypoints.api.dashboard_metrics import create_dashboard_metrics_router


class _JobMetricsStore:
    def __init__(self) -> None:
        self.calls = 0
        self.usage_calls = 0

    def list_jobs_for_attention(self, **_kwargs):
        self.calls += 1
        now = datetime.now(UTC)
        return [
            {
                "job_id": "job-1",
                "project_name": "Project One",
                "status": "completed",
                "selected_workflow": "wf-main",
                "started_at": (now - timedelta(minutes=2)).isoformat(),
                "finished_at": now.isoformat(),
            }
        ]

    def get_jobs_usage_summary(self, job_ids):
        self.usage_calls += 1
        return {
            str(job_id): {
                "tokens_prompt": 100,
                "tokens_completion": 25,
                "tokens_total": 125,
                "dominant_agent": "writer",
            }
            for job_id in job_ids
        }


class _SloRollupStore:
    def list_dashboard_job_facts(self, **_kwargs):
        return []

    def list_dashboard_daily_rollups(self, **_kwargs):
        return []

    def get_dashboard_rollup_state(self, **_kwargs):
        return {}

    def get_core_slo_metrics(self, **kwargs):
        return {"window_hours": kwargs["window_hours"], "samples": {}}


class _JobQueryService:
    def list_jobs(self, **_kwargs):
        if _kwargs.get("status"):
            return {"jobs": [], "total": 0}
        return {
            "groups": {
                "needs_attention": [],
                "acknowledged": [],
                "running": [],
                "blocked": [],
                "done": [],
            }
        }


def test_dashboard_uses_job_store_for_population_and_usage_and_observability_for_rollups() -> None:
    job_store = _JobMetricsStore()
    slo_store = _SloRollupStore()
    app = FastAPI()
    app.include_router(
        create_dashboard_metrics_router(
            request_scope=lambda _request, **_kwargs: SimpleNamespace(account_id="account-1"),
            get_logger_only=lambda: object(),
            get_job_metrics_store=lambda: job_store,
            get_slo_store=lambda: slo_store,
            job_query_service=lambda: _JobQueryService(),
            service_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=503
            ),
            api_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            dashboard_default_range="7d",
            dashboard_range_hours={"7d": 168},
            dashboard_metrics_page_size=100,
            dashboard_metrics_max_rows=0,
            dashboard_triage_limit=25,
            dashboard_rollup_read_enabled=True,
        )
    )

    response = TestClient(app).get("/api/dashboard/summary?range=7d")

    assert response.status_code == 200
    assert response.json()["hero_kpis"]["completed"]["value"] == 1
    assert "spend" not in response.json()["hero_kpis"]
    assert "spend_breakdown" not in response.json()["trends"]
    assert job_store.usage_calls == 2
    assert job_store.calls == 2


def test_dashboard_reports_unavailable_instead_of_false_zero_when_job_projection_is_missing() -> (
    None
):
    app = FastAPI()
    app.include_router(
        create_dashboard_metrics_router(
            request_scope=lambda _request, **_kwargs: SimpleNamespace(account_id="account-1"),
            get_logger_only=lambda: object(),
            get_job_metrics_store=lambda: object(),
            get_slo_store=lambda: _SloRollupStore(),
            job_query_service=lambda: _JobQueryService(),
            service_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=503
            ),
            api_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            dashboard_default_range="7d",
            dashboard_range_hours={"7d": 168},
            dashboard_metrics_page_size=100,
            dashboard_metrics_max_rows=0,
            dashboard_triage_limit=25,
            dashboard_rollup_read_enabled=True,
        )
    )

    response = TestClient(app).get("/api/dashboard/summary?range=7d")

    assert response.status_code == 503
    assert "list_jobs_for_attention" in response.json()["error"]
    assert "get_jobs_usage_summary" in response.json()["error"]


def test_dashboard_store_retains_token_totals_and_dominant_agent() -> None:
    store = _JobMetricsStore()

    usage = _dashboard_fetch_usage_by_job(bl=store, rows=[{"job_id": "job-1"}])

    assert usage == {
        "job-1": {
            "tokens_prompt": 100,
            "tokens_completion": 25,
            "tokens_total": 125,
            "dominant_agent": "writer",
        }
    }
    assert store.usage_calls == 1


def test_dashboard_group_counts_are_not_limited_to_triage_preview() -> None:
    class WaitQueryService(_JobQueryService):
        def list_jobs(self, **kwargs):
            if kwargs.get("status"):
                # The page has no rows, but its scoped count is authoritative.
                return {
                    "jobs": [],
                    "total": {
                        ("in_progress", "pending", "resume_pending"): 5,
                        ("awaiting_timer", "awaiting_child", "external_wait"): 18,
                    }[tuple(kwargs["status"])],
                }
            return super().list_jobs(**kwargs)

    app = FastAPI()
    app.include_router(
        create_dashboard_metrics_router(
            request_scope=lambda _request, **_kwargs: SimpleNamespace(account_id="account-1"),
            get_logger_only=lambda: object(),
            get_job_metrics_store=lambda: _JobMetricsStore(),
            get_slo_store=lambda: _SloRollupStore(),
            job_query_service=lambda: WaitQueryService(),
            service_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=503
            ),
            api_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            dashboard_default_range="7d",
            dashboard_range_hours={"7d": 168},
            dashboard_metrics_page_size=100,
            dashboard_metrics_max_rows=0,
            dashboard_triage_limit=1,
            dashboard_rollup_read_enabled=True,
        )
    )

    response = TestClient(app).get("/api/dashboard/summary?range=7d")

    assert response.status_code == 200
    kpis = response.json()["hero_kpis"]
    # Each card counts every stored status of its group, not one status.
    assert kpis["in_progress"] == {"label": "In progress", "value": 5}
    assert kpis["waiting"] == {"label": "Waiting", "value": 18}
    assert "awaiting_timer" not in kpis
    assert kpis["needs_attention"]["value"] == 0
