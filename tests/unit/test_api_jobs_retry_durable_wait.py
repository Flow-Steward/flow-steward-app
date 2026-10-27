"""Watchdog retry refuses a run paused on a durable wait, like an approval.

A run waiting on an approval, a Wait timer or a child workflow is still
active: re-enqueueing it without ``force`` would start a second copy while
the first resumes by itself.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from core.application.management.requests import RecoveryActionRequest
from core.entrypoints.api.jobs import create_api_jobs_router


def _recover_endpoint(job: dict, enqueued: list[dict]):
    async def _get_job_status(*_args, **_kwargs):
        return {}

    router = create_api_jobs_router(
        resolve_user_role=lambda _request: "admin",
        job_query_service=lambda: SimpleNamespace(get_job_status=_get_job_status),
        job_command_service=lambda: SimpleNamespace(),
        approval_query_service=lambda: SimpleNamespace(),
        request_scope=lambda _request, **_kwargs: None,
        resolve_actor_username=lambda *_args, **_kwargs: "admin",
        optional_actor_username=lambda *_args, **_kwargs: "admin",
        service_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
        get_logger_only=lambda: SimpleNamespace(get_job=lambda _job_id: job),
        watchdog_scan=lambda *_args, **_kwargs: {},
        parse_worker_pid=lambda *_args, **_kwargs: None,
        terminate_worker_pid=lambda *_args, **_kwargs: {},
        append_runtime_journal=lambda *_args, **_kwargs: {},
        build_rerun_workflow_selection=lambda *_args, **_kwargs: ({}, {}),
        load_replay_snapshot=lambda *_args, **_kwargs: {},
        enqueue_task=lambda **kwargs: enqueued.append(kwargs) or 1,
        retryable_interrupted_step_status="retryable_interrupted_step",
        err_database_not_configured="Database unavailable",
    )
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/watchdog/jobs/{job_id}/recover"
    )


@pytest.mark.parametrize("status", ["awaiting_approval", "awaiting_timer", "awaiting_child"])
def test_retry_of_a_run_paused_on_a_durable_wait_requires_force(status: str) -> None:
    enqueued: list[dict] = []
    job = {"id": "job-1", "status": status, "project_id": "project-1", "task_id": "t-1"}
    endpoint = _recover_endpoint(job, enqueued)
    request = SimpleNamespace(state=SimpleNamespace(auth_user={"id": 1, "roles": ["admin"]}))

    response = asyncio.run(
        endpoint("job-1", RecoveryActionRequest(action="retry", reason="retry test"), request)
    )

    assert response.status_code == 409
    assert b"force=true" in response.body
    assert enqueued == []
