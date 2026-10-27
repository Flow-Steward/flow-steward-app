from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.scope import ScopeContext
from core.entrypoints.api.jobs_control import create_jobs_control_router


def test_submit_task_rest_route_is_not_callable_or_in_openapi():
    submitted = []

    def enqueue_manual_task(command, **kwargs):
        submitted.append(command)
        return SimpleNamespace(
            dump=lambda: {
                "project_id": "project-42",
                "task_id": "task-42",
                "job_id": "job-42",
                "queue": "platform.project.project-42",
            }
        )

    app = FastAPI()
    app.include_router(
        create_jobs_control_router(
            project_command_service=lambda: SimpleNamespace(
                enqueue_manual_task=enqueue_manual_task
            ),
            approval_command_service=lambda: SimpleNamespace(),
            job_command_service=lambda: SimpleNamespace(),
            request_scope=lambda *args, **kwargs: ScopeContext(account_id="account-42"),
            service_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
            logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
        )
    )
    response = TestClient(app).post(
        "/api/tasks",
        json={
            "project_id": "project-42",
            "task_id": "task-42",
            "task_summary": "import stock",
        },
    )
    assert response.status_code == 404
    assert submitted == []
    assert "/api/tasks" not in app.openapi()["paths"]


def test_submit_task_mcp_tool_is_not_exposed_to_agents():
    from core.entrypoints.mcp.server import mcp

    assert "submit_task" not in mcp._tool_manager._tools
    assert "workflow_trigger" in mcp._tool_manager._tools


def test_submit_task_cli_command_is_not_discoverable():
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    assert not any(spec.command_id == "tasks.submit" for spec in PUBLIC_COMMAND_SPECS)
