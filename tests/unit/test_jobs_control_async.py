from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.scope import ScopeContext
from core.entrypoints.api import jobs_control


def _error_response(exc) -> JSONResponse:
    return JSONResponse(exc.payload, status_code=exc.status_code)


def _request_scope(*_args, **_kwargs) -> ScopeContext:
    return ScopeContext(
        account_id="account-42",
        actor_id="user-42",
        principal_type="user",
    )


def test_workflow_run_api_delegates_explicit_identity_and_inputs():
    requests = []

    class _WorkflowRunService:
        def start_mcp_workflow_run(self, command, *, idempotency_key, scope):
            requests.append((command, idempotency_key, scope))
            return SimpleNamespace(
                dump=lambda: {
                    "project_id": "00000000-0000-0000-0000-000000000042",
                    "job_id": "job-42",
                    "status": "pending",
                    "task_id": "server-correlation-42",
                    "file_count": 0,
                    "project_created": False,
                }
            )

    app = FastAPI()
    app.include_router(
        jobs_control.create_jobs_control_router(
            project_command_service=lambda: _WorkflowRunService(),
            approval_command_service=lambda: SimpleNamespace(),
            job_command_service=lambda: SimpleNamespace(),
            request_scope=_request_scope,
            service_error_response=_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
            logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
        )
    )
    response = TestClient(app).post(
        "/api/workflow-runs",
        headers={"Idempotency-Key": "stock-import-42"},
        json={
            "project_id": "00000000-0000-0000-0000-000000000042",
            "workflow_id": "stock-import",
            "inputs": {"supplier": "acme"},
            "client_context": {"session": "voice-42"},
        },
    )
    assert response.status_code == 200
    assert response.json()["job_id"] == "job-42"
    assert not {"task_id", "file_count", "project_created"} & response.json().keys()
    command, key, scope = requests[0]
    assert command.project_id == "00000000-0000-0000-0000-000000000042"
    assert command.workflow_id == "stock-import"
    assert command.inputs == {"supplier": "acme"}
    assert command.client_context == {"session": "voice-42"}
    assert key == "stock-import-42"
    assert scope.account_id == "account-42"


def test_resume_task_offloads_application_command(monkeypatch) -> None:
    calls: list[str] = []

    async def _run_in_threadpool(func, *args, **kwargs):
        calls.append(func.__name__)
        return func(*args, **kwargs)

    async def _decision(*_args, **_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(jobs_control, "run_in_threadpool", _run_in_threadpool, raising=False)
    monkeypatch.setattr(jobs_control, "approval_decision_from_request", _decision)

    class _ApprovalService:
        def resume_task(self, **_kwargs):
            return SimpleNamespace(dump=lambda: {"status": "accepted"})

    router = jobs_control.create_jobs_control_router(
        project_command_service=lambda: SimpleNamespace(),
        approval_command_service=lambda: _ApprovalService(),
        job_command_service=lambda: SimpleNamespace(),
        request_scope=_request_scope,
        service_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).post("/api/approvals/thread-42")

    assert response.status_code == 202
    assert calls == ["resume_task"]


def test_approval_api_rejects_coercive_boolean() -> None:
    router = jobs_control.create_jobs_control_router(
        project_command_service=lambda: SimpleNamespace(),
        approval_command_service=lambda: SimpleNamespace(),
        job_command_service=lambda: SimpleNamespace(),
        request_scope=_request_scope,
        service_error_response=_error_response,
        api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).post(
        "/api/approvals/thread-42",
        headers={"Idempotency-Key": "approval-42"},
        json={"approval_id": "gate-42", "approved": "false"},
    )

    assert response.status_code == 400
