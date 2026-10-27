"""MCP polling shares job scope authorization and never returns UI drilldowns."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceNotFoundError
from core.application.management.query_models import JobStatusView
from core.application.management.query_services import JobQueryService
from core.application.management.scope import scope_from_principal
from core.entrypoints.api.jobs import create_api_jobs_router


def _client(payload=None, *, error=None, service=None, principal=None):
    scope = scope_from_principal(principal) if principal is not None else object()

    async def get_status(job_id, *, scope):
        assert scope is expected_scope
        assert job_id == "job-1"
        if error:
            raise error
        return JobStatusView(**payload)

    expected_scope = scope
    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "user",
        job_query_service=lambda: service or SimpleNamespace(get_job_status=get_status),
        job_command_service=lambda: None,
        approval_query_service=lambda: None,
        request_scope=lambda _request, **_kwargs: scope,
        resolve_actor_username=lambda *_args, **_kwargs: "user",
        optional_actor_username=lambda *_args, **_kwargs: "user",
        service_error_response=lambda exc: JSONResponse(
            {"error": str(exc)}, status_code=exc.status_code
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
        enqueue_task=lambda **kwargs: None,
        retryable_interrupted_step_status="retryable_interrupted_step",
        err_database_not_configured="Database unavailable",
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize(
    "status", ["awaiting_timer", "awaiting_approval", "awaiting_child", "completed"]
)
def test_polling_status_preserves_run_state_without_loading_ui_drilldowns(status):
    payload = {
        "id": "job-1",
        "status": status,
        "project_id": "project-1",
        "thread_id": "thread-1",
        "task_summary": "Review",
        "current_step_id": "pause",
        "current_group_id": "group-1",
        "runtime": {"workflow_wait": {"kind": "timer", "due_at": "2026-10-12T12:00:00Z"}},
    }
    response = _client(payload).get("/api/jobs/job-1/status")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == status
    assert result["id"] == "job-1"
    assert result["project_id"] == "project-1"
    assert result["current_step_id"] == "pause"
    assert result["current_group_id"] == "group-1"
    assert result["workflow_wait"]["due_at"] == "2026-10-12T12:00:00Z"
    assert "sections" not in result
    assert "runtime" not in result


def test_polling_response_is_bounded_even_with_large_runtime_history_and_parallel_group():
    huge = "x" * 100000
    payload = {
        "id": "job-1",
        "status": "in_progress",
        "task_summary": huge,
        "runtime": {"retry_telemetry": [huge] * 20, "last_tool_call": {"payload": huge}},
        "invocation": {"inputs": huge},
        "trigger": {"payload": huge},
        "active_steps": [
            {
                "step_id": "step-1",
                "step_execution_id": "execution-1",
                "status": "in_progress",
                "unrelated": huge,
            }
        ]
        * 1000,
    }
    response = _client(payload).get("/api/jobs/job-1/status")
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(json.dumps(result)) < 20000
    assert result["active_steps"][0]["step_id"] == "step-1"
    assert result["active_steps_total"] == 1000
    assert result["active_steps_truncated"] is True
    assert "unrelated" not in result["active_steps"][0]
    assert "invocation" not in result
    assert "trigger" not in result


def test_polling_status_propagates_scope_denial_instead_of_exposing_job():
    response = _client(error=ServiceNotFoundError("Job not found in caller scope")).get(
        "/api/jobs/job-1/status"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "Job not found in caller scope"


@pytest.mark.parametrize(
    "workflow,expected",
    [
        ("wf-foreign", 403),
        (None, 403),
        ("wf-allowed", 200),
    ],
    ids=["foreign", "missing", "allowed"],
)
def test_polling_http_real_service_enforces_pat_workflow_boundary(workflow, expected):
    project_id = "11111111-1111-4111-8111-111111111111"
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": "job-1",
        "project_id": project_id,
        "account_id": DEFAULT_ACCOUNT_ID,
        "selected_workflow": workflow,
        "selected_workflow_display_name": "A run",
        "status": "in_progress",
        "task_summary": "Scoped run",
        "thread_id": "thread-1",
    }
    bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
    bl.get_job_cost_summary.return_value = {}
    runtime = AsyncMock(return_value={"current_step_id": "work", "private_debug": "not-public"})
    service = JobQueryService(
        get_logger=lambda: bl,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda *_args: "running",
        build_runtime_payload_for_api=runtime,
    )
    principal = {
        "id": "restricted-admin",
        "username": "restricted-admin",
        "principal_type": "user",
        "auth_source": "pat",
        "roles": ["admin"],
        "account_id": DEFAULT_ACCOUNT_ID,
        "scopes": ["jobs.read", "jobs.write"],
        "allowed_project_ids": [project_id],
        "allowed_workflow_ids": ["wf-allowed"],
    }
    response = _client(service=service, principal=principal).get("/api/jobs/job-1/status")
    assert response.status_code == expected, response.text
    if expected == 403:
        assert response.json() == {"error": "Access denied"}
        bl.get_job_cost_summary.assert_not_called()
        runtime.assert_not_awaited()
    else:
        result = response.json()
        assert result["id"] == "job-1"
        assert result["status"] == "in_progress"
        assert result["current_step_id"] == "work"
        assert "runtime" not in result
        assert "private_debug" not in result
        runtime.assert_awaited_once()
