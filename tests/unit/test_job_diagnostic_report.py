from __future__ import annotations

import asyncio
import json
from threading import get_ident
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from core.application.management.errors import ServiceForbiddenError
from core.application.management.query_services_job_core_mixin import JobQueryServiceCoreMixin
from core.application.management.scope import ScopeContext
from core.application.reports.job_diagnostic_report import build_job_diagnostic_report
from core.entrypoints.api.jobs import create_api_jobs_router


def test_job_diagnostic_report_redacts_nested_secrets_and_lists_artifacts() -> None:
    report = build_job_diagnostic_report(
        job_id="job-1",
        project_id="project-1",
        job={
            "id": "job-1",
            "project_id": "project-1",
            "invocation": {"Authorization": "Bearer secret-token-123456789"},
        },
        detail_contract={
            "execution_steps": [
                {
                    "id": "step-1",
                    "duration_ms": None,
                    "input_artifacts": [
                        {
                            "artifact_id": "art-input",
                            "role": "input",
                            "filename": "stock.csv",
                            "available": True,
                        }
                    ],
                    "output_artifacts": [
                        {
                            "artifact_id": "art-output",
                            "role": "output",
                            "filename": "result.csv",
                            "available": True,
                        }
                    ],
                }
            ]
        },
        messages=[{"id": "message-1", "payload": {"token": "secret-token-123456789"}}],
        logs=[{"id": "log-1", "detail": "Authorization: Bearer secret-token-123456789"}],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    encoded = json.dumps(report)
    assert "secret-token-123456789" not in encoded
    assert report["job"]["id"] == "job-1"
    assert report["steps"][0]["duration_ms"] is None
    assert [row["artifact_id"] for row in report["artifact_manifest"]] == [
        "art-input",
        "art-output",
    ]


def test_job_diagnostic_report_redacts_json_sensitive_text_without_corrupting_json() -> None:
    report = build_job_diagnostic_report(
        job_id="job-1",
        project_id="project-1",
        job={"id": "job-1", "project_id": "project-1"},
        detail_contract={},
        messages=[],
        logs=[{"detail": 'provider returned token: "secret-token-123456789"'}],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    assert "secret-token-123456789" not in json.dumps(report)
    assert "[REDACTED]" in report["logs"][0]["detail"]


def test_job_diagnostic_report_scans_the_serialized_bundle_once(monkeypatch) -> None:
    scanned_payloads: list[str] = []

    def _scan_outbound(text, *_args, **_kwargs):
        scanned_payloads.append(text)
        return SimpleNamespace(action="allow", redacted_text=text)

    monkeypatch.setattr(
        "core.infrastructure.security.integration.scan_outbound",
        _scan_outbound,
    )

    report = build_job_diagnostic_report(
        job_id="large-job",
        project_id="project-1",
        job={"id": "large-job", "project_id": "project-1"},
        detail_contract={},
        messages=[
            {"id": f"message-{index}", "summary": f"persisted row {index}"} for index in range(500)
        ],
        logs=[],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    assert len(report["messages"]) == 500
    assert len(scanned_payloads) == 1


def test_job_diagnostic_report_splits_oversized_security_scans(monkeypatch) -> None:
    scanned_payloads: list[str] = []

    def _scan_outbound(text, *_args, **_kwargs):
        scanned_payloads.append(text)
        return SimpleNamespace(action="allow", redacted_text=text)

    monkeypatch.setattr(
        "core.infrastructure.security.integration.scan_outbound",
        _scan_outbound,
    )

    report = build_job_diagnostic_report(
        job_id="large-job",
        project_id="project-1",
        job={"id": "large-job", "project_id": "project-1"},
        detail_contract={},
        messages=[{"id": f"message-{index}", "payload": "x" * 400_000} for index in range(4)],
        logs=[],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    assert len(report["messages"]) == 4
    assert len(scanned_payloads) > 1
    assert max(map(len, scanned_payloads)) < 1_000_000


def test_job_diagnostic_report_supports_old_jobs_without_new_step_fields() -> None:
    report = build_job_diagnostic_report(
        job_id="old-job",
        project_id="project-1",
        job={"id": "old-job", "project_id": "project-1"},
        detail_contract={"execution_steps": [{"id": "old-step"}]},
        messages=[],
        logs=[],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    assert report["steps"] == [{"id": "old-step"}]
    assert report["artifact_manifest"] == []


def test_job_diagnostic_report_route_returns_json_attachment() -> None:
    class _QueryService:
        async def get_job_diagnostic_report(self, job_id: str, *, scope=None):
            _ = scope
            return {"schema_version": "flow-steward.job-diagnostic-report.v1", "job_id": job_id}

    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "operator",
        job_query_service=lambda: _QueryService(),
        job_command_service=lambda: SimpleNamespace(),
        approval_query_service=lambda: SimpleNamespace(),
        request_scope=lambda _request, **_kwargs: None,
        resolve_actor_username=lambda *_args, **_kwargs: "operator",
        optional_actor_username=lambda *_args, **_kwargs: "operator",
        service_error_response=lambda exc: JSONResponse(
            {"error": str(exc)}, status_code=getattr(exc, "status_code", 500)
        ),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
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
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/jobs/{job_id}/report"
    )

    response = asyncio.run(endpoint(SimpleNamespace(), "job-42"))

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="flow-steward-job-job-42-report.json"'
    )
    assert json.loads(response.body)["job_id"] == "job-42"


def test_job_diagnostic_report_marks_when_browser_download_can_start() -> None:
    class _QueryService:
        def validate_job_diagnostic_report_access(self, job_id: str, *, scope=None) -> None:
            assert job_id == "job-42"

        async def get_job_diagnostic_report(self, job_id: str, *, scope=None):
            _ = scope
            return {"schema_version": "flow-steward.job-diagnostic-report.v1", "job_id": job_id}

    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "operator",
        job_query_service=lambda: _QueryService(),
        job_command_service=lambda: SimpleNamespace(),
        approval_query_service=lambda: SimpleNamespace(),
        request_scope=lambda _request, **_kwargs: None,
        resolve_actor_username=lambda *_args, **_kwargs: "operator",
        optional_actor_username=lambda *_args, **_kwargs: "operator",
        service_error_response=lambda exc: JSONResponse(
            {"error": str(exc)}, status_code=getattr(exc, "status_code", 500)
        ),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
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
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/jobs/{job_id}/report"
    )
    token = "a" * 32
    request = SimpleNamespace(
        headers={"x-forwarded-proto": "https"},
        url=SimpleNamespace(scheme="http"),
    )

    response = asyncio.run(endpoint(request, "job-42", token))

    assert isinstance(response, StreamingResponse)
    marker = response.headers["set-cookie"]
    assert f"fs_job_report_download_{token}=1" in marker
    assert "Max-Age=120" in marker
    assert "SameSite=lax" in marker
    assert "Secure" in marker


def test_job_diagnostic_report_renders_tokenized_download_off_the_request_loop() -> None:
    calls: list[str] = []
    preflight_threads: list[int] = []
    render_threads: list[int] = []
    factory_calls: list[int] = []
    scope_calls: list[int] = []
    selected_scope = ScopeContext(account_id="00000000-0000-4000-8000-000000000001")

    class _QueryService:
        def validate_job_diagnostic_report_access(self, job_id: str, *, scope=None) -> None:
            assert job_id == "job-42"
            assert scope is selected_scope
            preflight_threads.append(get_ident())

        async def get_job_diagnostic_report(self, job_id: str, *, scope=None):
            assert scope is selected_scope
            calls.append(job_id)
            render_threads.append(get_ident())
            return {
                "schema_version": "flow-steward.job-diagnostic-report.v1",
                "job_id": job_id,
            }

    service = _QueryService()

    def _query_service():
        factory_calls.append(get_ident())
        return service

    def _request_scope(_request, **_kwargs):
        scope_calls.append(get_ident())
        return selected_scope

    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "operator",
        job_query_service=_query_service,
        job_command_service=lambda: SimpleNamespace(),
        approval_query_service=lambda: SimpleNamespace(),
        request_scope=_request_scope,
        resolve_actor_username=lambda *_args, **_kwargs: "operator",
        optional_actor_username=lambda *_args, **_kwargs: "operator",
        service_error_response=lambda exc: JSONResponse(
            {"error": str(exc)}, status_code=getattr(exc, "status_code", 500)
        ),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
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
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/jobs/{job_id}/report"
    )
    request = SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http"))

    async def _download() -> bytes:
        request_thread = get_ident()
        response = await endpoint(request, "job-42", "a" * 32)

        assert isinstance(response, StreamingResponse)
        # Access is checked before the attachment/cookie is returned, but full
        # rendering remains deferred until after the first whitespace chunk.
        assert len(preflight_threads) == 1
        assert preflight_threads[0] != request_thread
        assert calls == []
        first_chunk = await anext(response.body_iterator)
        assert first_chunk == b" "
        assert calls == []
        body = first_chunk + b"".join([chunk async for chunk in response.body_iterator])
        assert len(render_threads) == 1
        assert render_threads[0] != request_thread
        assert factory_calls == [request_thread]
        assert scope_calls == [request_thread]
        return body

    body = asyncio.run(_download())
    assert calls == ["job-42"]
    assert json.loads(body) == {
        "schema_version": "flow-steward.job-diagnostic-report.v1",
        "job_id": "job-42",
    }


@pytest.fixture
def report_access_service():
    account_id = "00000000-0000-4000-8000-000000000001"
    project_id = "00000000-0000-4000-8000-000000000201"
    project = {"id": project_id, "account_id": account_id}
    reads: list[str] = []

    class _JobStore:
        def get_job(self, job_id):
            assert job_id == "job-42"
            reads.append("job")
            return {
                "id": job_id,
                "project_id": project_id,
                "account_id": account_id,
                "tenant_id": "00000000-0000-0000-0000-000000000001",
            }

        def __getattr__(self, name):
            raise AssertionError(f"Unexpected aggregate or rendering read: {name}")

    def _get_project(selected_project_id):
        assert selected_project_id == project_id
        reads.append("project")
        return dict(project)

    def _forbid_runtime(*_args, **_kwargs):
        raise AssertionError("Access preflight must not inspect runtime state")

    async def _forbid_async_runtime(*_args, **_kwargs):
        _forbid_runtime()

    store = _JobStore()
    service = JobQueryServiceCoreMixin(
        get_logger=lambda: store,
        get_project_by_id=_get_project,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda *_args: "",
        build_runtime_payload_for_api=_forbid_async_runtime,
        build_runtime_payload=_forbid_runtime,
        get_thread_state_snapshot=_forbid_runtime,
    )
    return service, ScopeContext(account_id=account_id), project, reads


def test_report_access_preflight_uses_project_port_without_rendering(
    report_access_service, monkeypatch
) -> None:
    def _forbid_render(**_kwargs):
        raise AssertionError("Access preflight must not build the report or detail")

    monkeypatch.setattr(
        "core.application.reports.job_detail_contract.build_job_detail_contract",
        _forbid_render,
    )
    monkeypatch.setattr(
        "core.application.reports.job_diagnostic_report.build_job_diagnostic_report",
        _forbid_render,
    )
    service, scope, _project, reads = report_access_service

    assert service.validate_job_diagnostic_report_access("job-42", scope=scope) is None
    assert reads == ["job", "project"]


def test_report_rechecks_revoked_access_before_rendering(
    report_access_service, monkeypatch
) -> None:
    async def _forbid_detail(**_kwargs):
        raise AssertionError("Revoked access must fail before detail rendering")

    monkeypatch.setattr(
        "core.application.reports.job_detail_contract.build_job_detail_contract",
        _forbid_detail,
    )
    service, scope, project, reads = report_access_service
    service.validate_job_diagnostic_report_access("job-42", scope=scope)
    project["account_id"] = "00000000-0000-4000-8000-000000000002"

    report = service.get_job_diagnostic_report("job-42", scope=scope)
    with pytest.raises(ServiceForbiddenError, match="Access denied"):
        asyncio.run(report)

    # This is a service-level authorization recheck, not a promise to remap an
    # exception into a new HTTP status after streaming headers were sent.
    assert reads == ["job", "project", "job", "project"]


def test_query_service_report_collects_persisted_streams_without_artifact_bodies(
    monkeypatch,
) -> None:
    class _Logger:
        def get_agent_messages(self, job_id, **kwargs):
            assert job_id == "job-1"
            assert kwargs["newest"] is False
            return [{"id": "message-1", "payload": {"artifact": "artifact:art-1"}}]

        def get_logs(self, job_id, **kwargs):
            assert job_id == "job-1"
            assert kwargs["newest"] is False
            return [{"id": "log-1", "detail": "completed"}]

        def get_events(self, job_id, **kwargs):
            assert job_id == "job-1"
            return [{"id": "event-1", "event_type": "step.completed"}]

    class _Service(JobQueryServiceCoreMixin):
        _resolve_artifact = None
        _serialize_agent_message_row = staticmethod(dict)
        _serialize_event_row = staticmethod(dict)

        def __init__(self) -> None:
            pass

        @staticmethod
        def _serialize_log_row(row, _job_id):
            return dict(row)

        def _job_context(self, job_id, *, scope=None):
            assert job_id == "job-1"
            return _Logger(), {"id": job_id, "project_id": "project-1"}, scope

        def list_errors(self, *_args, **_kwargs):
            return SimpleNamespace(dump=lambda: {"errors": [{"id": "error-1"}]})

        def list_decision_overrides(self, *_args, **_kwargs):
            return SimpleNamespace(dump=lambda: {"overrides": [{"id": "decision-1"}]})

        def get_runtime_journal(self, *_args, **_kwargs):
            return SimpleNamespace(dump=lambda: {"runtime_journal": [{"id": "journal-1"}]})

        def get_causal_chain(self, *_args, **_kwargs):
            return SimpleNamespace(dump=lambda: {"nodes": [{"id": "node-1"}]})

        def get_cost_breakdown(self, *_args, **_kwargs):
            return SimpleNamespace(dump=lambda: {"total_cost_usd": 0.01})

    async def _detail_contract(**_kwargs):
        return {
            "execution_steps": [
                {
                    "id": "step-1",
                    "output_artifacts": [
                        {
                            "artifact_id": "art-1",
                            "filename": "result.csv",
                            "download_url": "/api/jobs/job-1/artifacts/art-1",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(
        "core.application.reports.job_detail_contract.build_job_detail_contract",
        _detail_contract,
    )

    report = asyncio.run(_Service().get_job_diagnostic_report("job-1"))

    assert [row["id"] for row in report["messages"]] == ["message-1"]
    assert [row["id"] for row in report["logs"]] == ["log-1"]
    assert [row["id"] for row in report["events"]] == ["event-1"]
    assert report["errors"] == [{"id": "error-1"}]
    assert report["decisions"] == [{"id": "decision-1"}]
    assert report["runtime_journal"] == [{"id": "journal-1"}]
    assert report["artifact_manifest"] == [
        {
            "artifact_id": "art-1",
            "filename": "result.csv",
            "download_url": "/api/jobs/job-1/artifacts/art-1",
        }
    ]
    assert "body" not in json.dumps(report).lower()
