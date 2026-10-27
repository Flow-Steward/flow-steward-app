from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
)
from core.application.management.management_runtime import create_job_query_service
from core.application.management.scope import ScopeContext
from core.entrypoints.api.jobs import create_api_jobs_router
from core.infrastructure.runtime import serve_request_runtime

_ACCOUNT_ID = "00000000-0000-4000-8000-000000000001"
_FOREIGN_ACCOUNT_ID = "00000000-0000-4000-8000-000000000002"
_JOB_ID = "00000000-0000-4000-8000-000000000101"
_PROJECT_ID = "00000000-0000-4000-8000-000000000201"
_OTHER_PROJECT_ID = "00000000-0000-4000-8000-000000000202"
_DOWNLOAD_TOKEN = "a" * 32
_CORRELATION_ID = "job-report-access-test-correlation"


class _AccessStore:
    def __init__(
        self,
        *,
        job: dict[str, Any] | None,
        project: dict[str, Any] | None = None,
        project_error: Exception | None = None,
        allow_render: bool = False,
    ) -> None:
        self._job = job
        self._project = project
        self._project_error = project_error
        self._allow_render = allow_render

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        assert job_id == _JOB_ID
        return dict(self._job) if self._job is not None else None

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        assert project_id == _PROJECT_ID
        if self._project_error is not None:
            raise self._project_error
        return dict(self._project) if self._project is not None else None

    def _render_rows(self, owner: str) -> list[dict[str, Any]]:
        assert self._allow_render, f"{owner} must not run before report access is authorized"
        return []

    def get_agent_messages(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return self._render_rows("agent-message rendering")

    def get_logs(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return self._render_rows("log rendering")

    def get_events(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return self._render_rows("event rendering")

    def list_job_errors(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return self._render_rows("error rendering")

    def list_job_decision_overrides(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return self._render_rows("decision rendering")

    def get_job_cost_breakdown(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return self._render_rows("cost rendering")


@dataclass(frozen=True)
class _DeniedCase:
    job: dict[str, Any] | None
    project: dict[str, Any] | None
    scope: ScopeContext
    error_type: type[Exception]
    status_code: int
    error_code: str
    error_message: str
    project_error: Exception | None = None


def _job(*, account_id: str | None = _ACCOUNT_ID) -> dict[str, Any]:
    return {
        "id": _JOB_ID,
        "account_id": account_id,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "project_id": _PROJECT_ID,
        "status": "completed",
        "task_summary": "Owned diagnostic report access fixture",
    }


def _project(*, account_id: str = _ACCOUNT_ID) -> dict[str, Any]:
    return {"id": _PROJECT_ID, "account_id": account_id, "workspace_id": None}


class _NoopLogger:
    def error(self, *_args, **_kwargs) -> None:
        pass


def _canonical_api_error_response(exc: Exception, **kwargs) -> JSONResponse:
    error_payload = serve_request_runtime.build_error_payload_factory(
        err_request_failed="Request failed",
        default_correlation_id_fn=lambda: _CORRELATION_ID,
    )
    return serve_request_runtime.api_error_response(
        exc,
        request=kwargs.get("request"),
        logger=_NoopLogger(),
        error_payload=error_payload,
        request_correlation_id=lambda _request: _CORRELATION_ID,
    )


def _router_client(
    service,
    scope: ScopeContext,
    *,
    job_query_service: Callable[[], Any] | None = None,
    request_scope: Callable[..., ScopeContext] | None = None,
    api_error_response: Callable[..., JSONResponse] | None = None,
) -> TestClient:
    error_payload = serve_request_runtime.build_error_payload_factory(
        err_request_failed="Request failed",
        default_correlation_id_fn=lambda: _CORRELATION_ID,
    )

    def service_error_response(exc):
        return serve_request_runtime.service_error_response(
            exc,
            error_payload=error_payload,
            request_correlation_id=lambda _request, fallback="": fallback or _CORRELATION_ID,
            error_code_for_status=serve_request_runtime.error_code_for_status,
            err_request_failed="Request failed",
        )

    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "operator",
        job_query_service=job_query_service or (lambda: service),
        job_command_service=lambda: object(),
        approval_query_service=lambda: object(),
        request_scope=request_scope or (lambda _request, **_kwargs: scope),
        resolve_actor_username=lambda *_args, **_kwargs: "operator",
        optional_actor_username=lambda *_args, **_kwargs: "operator",
        service_error_response=service_error_response,
        api_error_response=api_error_response or _canonical_api_error_response,
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
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("failure_stage", "expected_calls"),
    [
        pytest.param("factory", ["factory", "api_error"], id="service-factory"),
        pytest.param(
            "scope",
            ["factory", "scope", "api_error"],
            id="request-scope",
        ),
        pytest.param(
            "preflight",
            ["factory", "scope", "preflight", "api_error"],
            id="access-preflight",
        ),
    ],
)
def test_tokenized_download_maps_unexpected_preflight_failures_before_response(
    failure_stage: str,
    expected_calls: list[str],
) -> None:
    calls: list[str] = []
    scope = ScopeContext(account_id=_ACCOUNT_ID)

    class _Service:
        def validate_job_diagnostic_report_access(self, job_id: str, *, scope=None) -> None:
            assert job_id == _JOB_ID
            assert scope is not None
            calls.append("preflight")
            if failure_stage == "preflight":
                raise RuntimeError("access preflight unavailable")

        async def get_job_diagnostic_report(self, *_args, **_kwargs):
            calls.append("render")
            raise AssertionError("report rendering must not start after a preflight failure")

    service = _Service()

    def _service_factory():
        calls.append("factory")
        if failure_stage == "factory":
            raise RuntimeError("query service unavailable")
        return service

    def _request_scope(_request, **_kwargs):
        calls.append("scope")
        if failure_stage == "scope":
            raise RuntimeError("request scope unavailable")
        return scope

    def _api_error_response(exc: Exception, **kwargs) -> JSONResponse:
        calls.append("api_error")
        return _canonical_api_error_response(exc, **kwargs)

    client = _router_client(
        service,
        scope,
        job_query_service=_service_factory,
        request_scope=_request_scope,
        api_error_response=_api_error_response,
    )

    response = client.get(
        f"/api/jobs/{_JOB_ID}/report",
        params={"download_token": _DOWNLOAD_TOKEN},
    )

    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "code": "internal_error",
        "message": "Internal server error",
        "details": {},
        "correlation_id": _CORRELATION_ID,
        "error": "Internal server error",
    }
    assert response.headers.get("set-cookie", "") == ""
    assert calls == expected_calls


_DENIED_CASES = [
    pytest.param(
        _DeniedCase(
            job=None,
            project=None,
            scope=ScopeContext(account_id=_ACCOUNT_ID),
            error_type=ServiceNotFoundError,
            status_code=404,
            error_code="not_found",
            error_message="Job not found",
        ),
        id="missing-job",
    ),
    pytest.param(
        _DeniedCase(
            job=_job(),
            project=_project(account_id=_FOREIGN_ACCOUNT_ID),
            scope=ScopeContext(account_id=_ACCOUNT_ID),
            error_type=ServiceForbiddenError,
            status_code=403,
            error_code="forbidden",
            error_message="Access denied",
        ),
        id="foreign-project-account",
    ),
    pytest.param(
        _DeniedCase(
            job=_job(),
            project=_project(),
            scope=ScopeContext(
                account_id=_ACCOUNT_ID,
                allowed_project_ids=frozenset({_OTHER_PROJECT_ID}),
            ),
            error_type=ServiceForbiddenError,
            status_code=403,
            error_code="forbidden",
            error_message="Access denied",
        ),
        id="excluded-project",
    ),
    pytest.param(
        _DeniedCase(
            job=_job(account_id=_FOREIGN_ACCOUNT_ID),
            project=None,
            scope=ScopeContext(account_id=_ACCOUNT_ID),
            error_type=ServiceForbiddenError,
            status_code=403,
            error_code="forbidden",
            error_message="Access denied",
        ),
        id="foreign-orphan",
    ),
    pytest.param(
        _DeniedCase(
            job=_job(account_id=None),
            project=None,
            scope=ScopeContext(account_id=_ACCOUNT_ID),
            error_type=ServiceForbiddenError,
            status_code=403,
            error_code="forbidden",
            error_message="Access denied",
        ),
        id="unowned-orphan",
    ),
    pytest.param(
        _DeniedCase(
            job=_job(),
            project=None,
            scope=ScopeContext(account_id=_ACCOUNT_ID),
            error_type=ServiceUnavailableError,
            status_code=503,
            error_code="service_unavailable",
            error_message="Project lookup unavailable",
            project_error=RuntimeError("project store unavailable"),
        ),
        id="project-lookup-outage",
    ),
]


@pytest.mark.parametrize("case", _DENIED_CASES)
def test_job_report_download_denials_are_decided_before_streaming(
    case: _DeniedCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_expensive_detail_render(**_kwargs):
        raise AssertionError("detail rendering must not run before report access is authorized")

    monkeypatch.setattr(
        "core.application.reports.job_detail_contract.build_job_detail_contract",
        reject_expensive_detail_render,
    )
    store = _AccessStore(
        job=case.job,
        project=case.project,
        project_error=case.project_error,
    )
    service = create_job_query_service(get_logger=lambda: store)

    report = service.get_job_diagnostic_report(_JOB_ID, scope=case.scope)
    with pytest.raises(case.error_type):
        asyncio.run(report)

    client = _router_client(service, case.scope)
    for params in ({}, {"download_token": _DOWNLOAD_TOKEN}):
        response = client.get(f"/api/jobs/{_JOB_ID}/report", params=params)

        assert response.status_code == case.status_code
        assert response.json() == {
            "status": "error",
            "code": case.error_code,
            "message": case.error_message,
            "details": {},
            "correlation_id": _CORRELATION_ID,
            "error": case.error_message,
        }
        assert f"fs_job_report_download_{_DOWNLOAD_TOKEN}" not in response.headers.get(
            "set-cookie", ""
        )


@pytest.mark.parametrize("download_token", [None, _DOWNLOAD_TOKEN])
def test_owned_orphan_job_report_remains_downloadable(
    download_token: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def minimal_detail_contract(**_kwargs):
        return {"execution_steps": []}

    monkeypatch.setattr(
        "core.application.reports.job_detail_contract.build_job_detail_contract",
        minimal_detail_contract,
    )
    store = _AccessStore(job=_job(), project=None, allow_render=True)
    scope = ScopeContext(account_id=_ACCOUNT_ID)
    service = create_job_query_service(get_logger=lambda: store)
    client = _router_client(service, scope)

    response = client.get(
        f"/api/jobs/{_JOB_ID}/report",
        params={"download_token": download_token} if download_token else {},
    )

    assert response.status_code == 200
    assert response.json()["job"]["id"] == _JOB_ID
    marker = response.headers.get("set-cookie", "")
    if download_token:
        assert f"fs_job_report_download_{download_token}=1" in marker
    else:
        assert marker == ""
