from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.entrypoints.api.dashboard_metrics import create_dashboard_metrics_router


class _JobMetricsStore:
    def __init__(self, *, cost_usd: float | None = 1.25, unknown_cli_tokens: int = 0) -> None:
        self.calls = 0
        self.cost_usd = cost_usd
        self.unknown_cli_tokens = unknown_cli_tokens

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

    def get_jobs_cost_summary(self, job_ids):
        return {
            str(job_id): {
                "cost_usd": self.cost_usd,
                "unknown_cli_tokens": self.unknown_cli_tokens,
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


def test_dashboard_uses_job_store_for_population_and_cost_and_observability_for_rollups() -> None:
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
    assert response.json()["hero_kpis"]["spend"]["value_usd"] == 1.25
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
    assert "get_jobs_cost_summary" in response.json()["error"]


def test_dashboard_renders_unknown_job_cost_as_unavailable_not_zero() -> None:
    app = FastAPI()
    app.include_router(
        create_dashboard_metrics_router(
            request_scope=lambda _request, **_kwargs: SimpleNamespace(account_id="account-1"),
            get_logger_only=lambda: object(),
            get_job_metrics_store=lambda: _JobMetricsStore(cost_usd=None),
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

    assert response.status_code == 200
    spend = response.json()["hero_kpis"]["spend"]
    assert spend["value_usd"] is None
    assert spend["display"] == "Unavailable"
    assert spend["availability"] == "unavailable"
    assert spend["unknown_jobs"] == 1
    project_spend = response.json()["trends"]["spend_breakdown"]["project"]
    assert project_spend == [
        {"name": "Project One", "spend_usd": None, "availability": "unavailable"}
    ]


def test_dashboard_keeps_known_subtotal_and_marks_mixed_cost_partial() -> None:
    app = FastAPI()
    app.include_router(
        create_dashboard_metrics_router(
            request_scope=lambda _request, **_kwargs: SimpleNamespace(account_id="account-1"),
            get_logger_only=lambda: object(),
            get_job_metrics_store=lambda: _JobMetricsStore(
                cost_usd=0.116143, unknown_cli_tokens=900
            ),
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

    spend = TestClient(app).get("/api/dashboard/summary?range=7d").json()["hero_kpis"]["spend"]

    assert spend["value_usd"] == 0.116143
    assert spend["availability"] == "partial"
    assert spend["unknown_jobs"] == 1


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
