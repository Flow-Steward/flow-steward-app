from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from core.application.reports.job_diagnostic_report import build_job_diagnostic_report
from core.entrypoints.api.jobs import create_api_jobs_router

_JOB_ID = "00000000-0000-4000-8000-000000000101"
_DOWNLOAD_TOKEN = "a" * 32


def _router(payload):
    class QueryService:
        def validate_job_diagnostic_report_access(self, job_id, *, scope=None):
            assert job_id == _JOB_ID

        async def get_job_diagnostic_report(self, job_id, *, scope=None):
            assert job_id == _JOB_ID
            return payload

    return create_api_jobs_router(
        job_query_service=lambda: QueryService(),
        job_command_service=lambda: SimpleNamespace(),
        approval_query_service=lambda: SimpleNamespace(),
        request_scope=lambda _request, **_kwargs: None,
        resolve_user_role=lambda _request: "operator",
        resolve_actor_username=lambda *_args, **_kwargs: "operator",
        optional_actor_username=lambda *_args, **_kwargs: "operator",
        service_error_response=lambda exc: JSONResponse(
            {"error": "Service failed"}, status_code=500
        ),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": "Internal server error"}, status_code=500
        ),
        get_logger_only=lambda: None,
        watchdog_scan=lambda *_args, **_kwargs: {},
        parse_worker_pid=lambda *_args, **_kwargs: None,
        terminate_worker_pid=lambda *_args, **_kwargs: {},
        append_runtime_journal=lambda *_args, **_kwargs: {},
        build_rerun_workflow_selection=lambda *_args, **_kwargs: ({}, {}),
        load_replay_snapshot=lambda *_args, **_kwargs: {},
        enqueue_task=lambda *_args, **_kwargs: 1,
        retryable_interrupted_step_status="retryable_interrupted_step",
        err_database_not_configured="Database unavailable",
    )


def _download(payload, token=None):
    app = FastAPI()
    app.include_router(_router(payload))
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get(
            f"/api/jobs/{_JOB_ID}/report",
            params={"download_token": token} if token else {},
        )


@pytest.mark.parametrize("token", [None, _DOWNLOAD_TOKEN])
def test_job_report_native_scalars_have_the_existing_wire_representation(token):
    report = {
        "job": {"id": UUID(_JOB_ID), "finished_at": datetime(2026, 10, 5, 12, 0, tzinfo=UTC)},
        "cost": Decimal("0.01230"),
        "rows": [{"message": "Unicode: café", "value": 42, "flag": True, "optional": None}],
    }

    response = _download(report, token)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"].endswith('-report.json"')
    assert response.json() == {
        "job": {"id": _JOB_ID, "finished_at": "2026-10-05 12:00:00+00:00"},
        "cost": "0.01230",
        "rows": report["rows"],
    }


def test_job_report_default_rendering_failure_is_mapped_without_attachment():
    class Unrenderable:
        def __str__(self):
            raise ValueError("Private rendering detail")

    response = _download({"value": Unrenderable()})

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error"}
    assert "content-disposition" not in response.headers
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("token", [None, _DOWNLOAD_TOKEN])
def test_job_report_redaction_fallback_retains_scalar_values_and_removes_sensitive_values(
    token, monkeypatch
):
    monkeypatch.setattr(
        "core.application.reports.job_diagnostic_report.redact_text_for_persistence",
        lambda *_args, **_kwargs: "replacement is not JSON",
    )
    report = build_job_diagnostic_report(
        job_id=_JOB_ID,
        project_id="project-1",
        job={"id": UUID(_JOB_ID), "password": "synthetic-private-value"},
        detail_contract={},
        messages=[],
        logs=[],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={"total": Decimal("0.01230")},
    )
    assert report["job"]["id"] == _JOB_ID
    assert report["cost_breakdown"]["total"] == "0.01230"

    response = _download(report, token)

    assert response.status_code == 200
    assert response.json()["job"]["id"] == _JOB_ID
    assert response.json()["cost_breakdown"]["total"] == "0.01230"
    assert "synthetic-private-value" not in response.text


def test_job_report_large_tokenized_download_preserves_bounded_chunks():
    payload = {"job_id": UUID(_JOB_ID), "message": "é" * (1024 * 1024 + 1)}
    router = _router(payload)
    endpoint = next(
        route.endpoint for route in router.routes if route.path == "/api/jobs/{job_id}/report"
    )

    async def read_download():
        response = await endpoint(
            SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http")),
            _JOB_ID,
            _DOWNLOAD_TOKEN,
        )
        assert isinstance(response, StreamingResponse)
        chunks = [chunk async for chunk in response.body_iterator]
        assert chunks[0] == b" "
        assert len(chunks) >= 4
        assert all(len(chunk) <= 1024 * 1024 for chunk in chunks)
        return json.loads(b"".join(chunks))

    assert asyncio.run(read_download()) == {"job_id": _JOB_ID, "message": payload["message"]}
