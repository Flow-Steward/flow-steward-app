from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.management.scope import ScopeContext
from core.entrypoints.api.tabular_feed import create_api_tabular_feed_router


class _FakeLogger:
    def get_project(self, project_id: str) -> dict[str, Any]:
        return {"id": project_id, "account_id": "acc_api"}


def _api_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


def _service_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": str(exc)},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def test_builder_inspect_route_uses_core_service_and_bounds_preview(monkeypatch) -> None:
    plane = ArtifactDatasetPlane()
    artifact, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc_api", "project_id": "project_api"},
        run_id="run_api",
        step_id="fetch",
        filename="feed.csv",
        content_type="text/csv",
        body=(
            b"sku,qty\n"
            b"00001,1\n"
            b"00002,2\n"
            b"00003,3\n"
            b"00004,4\n"
            b"00005,5\n"
            b"00006,6\n"
            b"00007,7\n"
            b"00008,8\n"
            b"00009,9\n"
            b"00010,10\n"
            b"00011,11\n"
        ),
    )

    class _Portability:
        def __init__(self, _backend: Any) -> None:
            pass

        def assert_structure_change_allowed(self, workflow_id: str, *, action: str) -> None:
            assert workflow_id == "workflow_api"
            assert action == "tabular_feed_inspect"

    monkeypatch.setattr(
        "core.entrypoints.api.tabular_feed.WorkflowPortabilityService",
        _Portability,
    )
    monkeypatch.setattr(
        "core.entrypoints.api.tabular_feed.build_tabular_artifact_storage",
        lambda: plane,
    )

    def _request_scope(_request: Any, **kwargs: Any) -> ScopeContext:
        assert kwargs == {"project_id": "project_api", "workflow_id": "workflow_api"}
        return ScopeContext(
            account_id="acc_api",
            project_id="project_api",
            workflow_id="workflow_api",
            actor_id="builder-user",
        )

    app = FastAPI()
    app.include_router(
        create_api_tabular_feed_router(
            get_logger_only=lambda: _FakeLogger(),
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/projects/project_api/workflows/workflow_api/tabular-feed/inspect",
        json={
            "artifact_handle": f"artifact:{artifact.artifact_id}",
            "options": {"format": "csv"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["format"] == "csv"
    assert body["result"]["row_count_estimate"] == 11
    assert [column["name"] for column in body["result"]["columns"]] == ["sku", "qty"]
    assert len(body["result"]["preview_rows"]) == 10
    assert body["result"]["preview_rows"][0]["sku"] == "00001"
