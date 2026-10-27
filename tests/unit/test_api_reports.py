from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if "psycopg_runtime" not in sys.modules:
    psycopg_runtime_mod = types.ModuleType("psycopg_runtime")
    extras_mod = types.ModuleType("psycopg_runtime.extras")
    extras_mod.RealDictCursor = object
    psycopg_runtime_mod.extras = extras_mod
    sys.modules["psycopg_runtime"] = psycopg_runtime_mod
    sys.modules["psycopg_runtime.extras"] = extras_mod

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.scope import ScopeContext
from core.entrypoints.api.reports import create_api_reports_router


class _FakeReportingService:
    def __init__(self):
        self.generate_calls = []
        self.get_calls = []

    def report_generate(self, report_type, params, *, scope, persist=True):
        self.generate_calls.append(
            {
                "report_type": report_type,
                "params": params,
                "scope": scope,
                "persist": persist,
            }
        )
        return {
            "id": "report-123" if persist else None,
            "report_type": report_type,
            "status": "generated",
            "params": params,
            "summary_text": "ok",
            "payload": {"rows": []},
            "chart_payload": {},
            "directives": [],
            "created_at": "2026-04-04T10:00:00+00:00",
        }

    def report_get(self, report_id, *, scope):
        self.get_calls.append({"report_id": report_id, "scope": scope})
        return {
            "id": report_id,
            "report_type": "job_cost_breakdown",
            "status": "generated",
            "params": {"job_id": 42},
            "summary_text": "saved",
            "payload": {"rows": []},
            "chart_payload": {},
            "directives": [],
            "created_at": "2026-04-04T10:00:00+00:00",
        }


@pytest.mark.parametrize("report_type", ["activity_daily", "project_summary", "project_stats"])
@pytest.mark.parametrize("persist", [False, True])
def test_api_reports_generate_and_get(report_type, persist):
    service = _FakeReportingService()
    captured_scope_kwargs = []

    def _request_scope(_request, **kwargs):
        captured_scope_kwargs.append(kwargs)
        return ScopeContext(account_id="acc_default")

    app = FastAPI()
    app.include_router(
        create_api_reports_router(
            reporting_service=lambda: service,
            request_scope=_request_scope,
            service_error_response=lambda exc: JSONResponse(
                {"error": str(exc)},
                status_code=int(getattr(exc, "status_code", 500) or 500),
            ),
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )

    client = TestClient(app)
    response = client.post(
        "/api/reports/generate",
        json={
            "report_type": report_type,
            "params": {"project_id": 7, "workflow_id": "weekly_report", "range": "7d"},
            "persist": persist,
        },
    )
    assert response.status_code == 200
    assert response.json()["report_type"] == report_type
    assert response.json()["id"] == ("report-123" if persist else None)
    assert service.generate_calls[0]["report_type"] == report_type
    assert service.generate_calls[0]["params"] == {
        "project_id": 7,
        "workflow_id": "weekly_report",
        "range": "7d",
    }
    assert service.generate_calls[0]["persist"] is persist
    assert service.generate_calls[0]["scope"].account_id == "acc_default"
    assert captured_scope_kwargs[0]["project_id"] == 7

    saved = client.get("/api/reports/report-123")
    assert saved.status_code == 200
    assert saved.json()["id"] == "report-123"
    assert service.get_calls[0]["report_id"] == "report-123"

    unsupported = client.post("/api/reports/generate", json={"report_type": "unsupported_report"})
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"][0]["type"] == "literal_error"
    assert unsupported.json()["detail"][0]["loc"] == ["body", "report_type"]

    extra = client.post(
        "/api/reports/generate", json={"report_type": report_type, "session_id": "retired"}
    )
    assert extra.status_code == 422
    assert extra.json()["detail"][0]["type"] == "extra_forbidden"
    assert extra.json()["detail"][0]["loc"] == ["body", "session_id"]
    assert len(service.generate_calls) == 1
    assert len(captured_scope_kwargs) == 2
