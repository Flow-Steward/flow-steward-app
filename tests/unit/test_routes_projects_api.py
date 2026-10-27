from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.management.command_models import ProjectCreateInput
from core.application.management.command_services import ProjectCommandService
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
)
from core.entrypoints.api.projects import create_api_projects_router


class _Input:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _dumping(**payload: Any) -> Any:
    return SimpleNamespace(dump=lambda: dict(payload))


def _client(*, project_query_service, project_command_service) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_api_projects_router(
            job_query_service=lambda: SimpleNamespace(),
            job_command_service=lambda: SimpleNamespace(),
            project_query_service=project_query_service,
            project_command_service=project_command_service,
            request_scope=lambda _request, **kwargs: {"scope": kwargs},
            require_authenticated_user=lambda _request, **_kwargs: {"id": 1},
            local_auth_enabled=lambda: False,
            external_auth_enabled=False,
            auth_admin_roles={"admin"},
            resolve_actor_username=lambda _request, _fallback, default="system": default,
            project_create_input_cls=_Input,
            project_update_input_cls=_Input,
            service_error_cls=ServiceError,
            service_not_found_error_cls=ServiceNotFoundError,
            service_error_response=lambda exc, request=None: JSONResponse(
                {"detail": str(getattr(exc, "message", exc)), "request_seen": request is not None},
                status_code=int(getattr(exc, "status_code", 500) or 500),
            ),
            api_error_response=lambda exc, request=None: JSONResponse(
                {"detail": str(exc), "request_seen": request is not None},
                status_code=500,
            ),
            err_admin_role_required="Admin role required",
            err_database_not_configured="Database not configured",
            default_agent_models=dict,
            available_providers=list,
            available_models=list,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_api_projects_create_rejects_legacy_project_types() -> None:
    state = {"project_type": ""}

    class _ProjectCommand:
        def create_project(self, input_payload: Any, *, scope: Any) -> Any:
            del scope
            state["project_type"] = str(getattr(input_payload, "project_type", ""))
            return _dumping(project_id=31, name="Retired Project")

    client = _client(
        project_query_service=lambda: SimpleNamespace(),
        project_command_service=lambda: _ProjectCommand(),
    )

    for project_type in ("unsupported_project_type", "__invalid_project_type__"):
        response = client.post(
            "/api/projects",
            json={
                "name": "Retired Project",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "project_type": project_type,
            },
        )

        assert response.status_code == 422
    assert state["project_type"] == ""


def test_api_projects_create_rejects_retired_or_unknown_fields() -> None:
    class _ProjectCommand:
        def create_project(self, input_payload: Any, *, scope: Any) -> Any:
            raise AssertionError("create_project must not be called for invalid payloads")

    client = _client(
        project_query_service=lambda: SimpleNamespace(),
        project_command_service=lambda: _ProjectCommand(),
    )

    for field_name in ("db_type", "db_schema", "db_dsn", "unexpected_field"):
        response = client.post(
            "/api/projects",
            json={
                "name": "Invalid Project",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                field_name: "retired",
            },
        )

        assert response.status_code == 422


def test_api_projects_create_returns_conflict_for_duplicate_name() -> None:
    class _ProjectCommand:
        def create_project(self, input_payload: Any, *, scope: Any) -> Any:
            del input_payload, scope
            raise ServiceConflictError("Project already exists in this workspace.")

    client = _client(
        project_query_service=lambda: SimpleNamespace(),
        project_command_service=lambda: _ProjectCommand(),
    )

    response = client.post(
        "/api/projects",
        json={
            "name": "Existing Project",
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Project already exists in this workspace."


def test_api_projects_create_rejects_unknown_provider_as_project_default() -> None:
    logger = SimpleNamespace(conn=object())
    service = ProjectCommandService(
        get_logger=lambda: logger,
        list_workflow_choices=lambda **_kwargs: [],
    )

    class _ProjectCommand:
        def create_project(self, input_payload: Any, *, scope: Any) -> Any:
            del scope
            return service.create_project(ProjectCreateInput(**vars(input_payload)))

    client = _client(
        project_query_service=lambda: SimpleNamespace(),
        project_command_service=lambda: _ProjectCommand(),
    )

    response = client.post(
        "/api/projects",
        json={
            "name": "Invalid Provider Project",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "default_provider": "retired_" + "executor",
            "default_model": "",
        },
    )

    assert response.status_code == 400
    assert "Unknown provider" in response.json()["detail"]


def test_api_project_update_rejects_retired_or_unknown_fields() -> None:
    class _ProjectQuery:
        def get_project_edit_form(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(project={"id": "7", "name": "Demo", "workspace_id": "ws"})

    class _ProjectCommand:
        def update_project(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("update_project must not be called for invalid payloads")

    client = _client(
        project_query_service=lambda: _ProjectQuery(),
        project_command_service=lambda: _ProjectCommand(),
    )

    for field_name in ("db_type", "db_schema", "db_dsn", "unexpected_field"):
        response = client.put(
            "/api/projects/7",
            json={
                "name": "Invalid Project",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                field_name: "retired",
            },
        )

        assert response.status_code == 422


def test_api_projects_create_rejects_removed_repository_fields() -> None:
    class _ProjectCommand:
        def create_project(self, input_payload: Any, *, scope: Any) -> Any:
            raise AssertionError("create_project must not be called for incompatible payloads")

    client = _client(
        project_query_service=lambda: SimpleNamespace(),
        project_command_service=lambda: _ProjectCommand(),
    )

    for field_name, value in (
        ("project_type", "it"),
        ("repo_url", "https://example.invalid/repo.git"),
        ("project_root", "/tmp/demo"),
        ("container_root", "/workspace/demo"),
        ("supported_extensions", ".py"),
    ):
        response = client.post(
            "/api/projects",
            json={
                "name": "General Project",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                field_name: value,
            },
        )

        assert response.status_code == 422
        assert "Unknown project field(s)" in response.text
        assert field_name in response.text


def test_api_projects_create_allows_data_connections_for_general_project() -> None:
    captured: dict[str, Any] = {}

    class _ProjectCommand:
        def create_project(self, input_payload: Any, *, scope: Any) -> Any:
            captured.update(vars(input_payload))
            captured["scope"] = scope
            return _dumping(project_id=31, name="General Data Project")

    client = _client(
        project_query_service=lambda: SimpleNamespace(),
        project_command_service=lambda: _ProjectCommand(),
    )

    response = client.post(
        "/api/projects",
        json={
            "name": "General Data Project",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "data_connection_ids": ["warehouse-primary"],
        },
    )

    assert response.status_code == 201
    assert captured["data_connection_ids"] == ["warehouse-primary"]
    assert not any(captured.get(field) for field in ("project_root", "container_root", "git_root"))


def test_api_project_detail_includes_model_selection_context() -> None:
    class _ProjectQuery:
        def get_project_edit_form(self, project_id: str, *, scope: Any) -> Any:
            assert project_id == "7"
            assert scope == {"scope": {"project_id": "7"}}
            return SimpleNamespace(project={"id": "7", "name": "Demo"})

    app = FastAPI()
    app.include_router(
        create_api_projects_router(
            job_query_service=lambda: SimpleNamespace(),
            job_command_service=lambda: SimpleNamespace(),
            project_query_service=lambda: _ProjectQuery(),
            project_command_service=lambda: SimpleNamespace(),
            request_scope=lambda _request, **kwargs: {"scope": kwargs},
            require_authenticated_user=lambda _request, **_kwargs: {"id": 1},
            local_auth_enabled=lambda: False,
            external_auth_enabled=False,
            auth_admin_roles={"admin"},
            resolve_actor_username=lambda _request, _fallback, default="system": default,
            project_create_input_cls=_Input,
            project_update_input_cls=_Input,
            service_error_cls=ServiceError,
            service_not_found_error_cls=ServiceNotFoundError,
            service_error_response=lambda exc, request=None: JSONResponse({"detail": str(exc)}),
            api_error_response=lambda exc, request=None: JSONResponse({"detail": str(exc)}),
            err_admin_role_required="Admin role required",
            err_database_not_configured="Database not configured",
            default_agent_models=lambda: {"scout": "gpt-4.1-mini"},
            available_providers=lambda: [("openai", "OpenAI")],
            available_models=lambda: ["gpt-4.1-mini"],
            providers_with_models=lambda: {"openai": ["gpt-4.1-mini"]},
        )
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/projects/7")

    assert response.status_code == 200
    assert response.json()["default_agent_models"] == {"scout": "gpt-4.1-mini"}
    assert response.json()["available_models"] == ["gpt-4.1-mini"]


def test_api_project_detail_returns_503_when_model_catalog_is_unavailable() -> None:
    from core.application.settings.model_catalog import ModelCatalogUnavailableError

    class _ProjectQuery:
        def get_project_edit_form(self, project_id: str, *, scope: Any) -> Any:
            return SimpleNamespace(project={"id": project_id, "name": "Demo"})

    app = FastAPI()
    app.include_router(
        create_api_projects_router(
            job_query_service=lambda: SimpleNamespace(),
            job_command_service=lambda: SimpleNamespace(),
            project_query_service=lambda: _ProjectQuery(),
            project_command_service=lambda: SimpleNamespace(),
            request_scope=lambda _request, **kwargs: {"scope": kwargs},
            require_authenticated_user=lambda _request, **_kwargs: {"id": 1},
            local_auth_enabled=lambda: False,
            external_auth_enabled=False,
            auth_admin_roles={"admin"},
            resolve_actor_username=lambda _request, _fallback, default="system": default,
            project_create_input_cls=_Input,
            project_update_input_cls=_Input,
            service_error_cls=ServiceError,
            service_not_found_error_cls=ServiceNotFoundError,
            service_error_response=lambda exc, request=None: JSONResponse({"detail": str(exc)}),
            api_error_response=lambda exc, request=None: JSONResponse({"detail": str(exc)}),
            err_admin_role_required="Admin role required",
            err_database_not_configured="Database not configured",
            default_agent_models=dict,
            available_providers=list,
            available_models=list,
            providers_with_models=dict,
            resolve_model_selection_context=lambda **_kwargs: (_ for _ in ()).throw(
                ModelCatalogUnavailableError("database unavailable")
            ),
        )
    )

    response = TestClient(app, raise_server_exceptions=False).get("/api/projects/7")

    assert response.status_code == 503
    assert response.json()["detail"] == "Model catalog is unavailable."


def test_api_project_update_and_delete_hide_server_error_details() -> None:
    class _ProjectQuery:
        def get_project_edit_form(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(project={"id": "7", "name": "Demo", "workspace_id": "ws"})

    class _ProjectCommand:
        def update_project(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ServiceError("raw-update-secret", status_code=500)

        def delete_project(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ServiceError("raw-delete-secret", status_code=500)

    client = _client(
        project_query_service=lambda: _ProjectQuery(),
        project_command_service=lambda: _ProjectCommand(),
    )

    update = client.put("/api/projects/7", json={"name": "Demo", "workspace_id": "ws"})
    delete = client.delete("/api/projects/7")

    assert update.status_code == 500
    assert update.json()["detail"] == "Project update failed."
    assert "raw-update-secret" not in update.text
    assert delete.status_code == 500
    assert delete.json()["detail"] == "Project deletion failed."
    assert "raw-delete-secret" not in delete.text


def test_api_projects_list_preserves_operator_default_workflow_id() -> None:
    class _ProjectQuery:
        def list_projects(self, **_kwargs: Any) -> Any:
            return _dumping(
                projects=[
                    {
                        "id": "7",
                        "name": "Demo",
                        "workspace_id": "ws",
                        "workspace_name": "Product Workspace",
                        "operator_default_workflow_id": "wf_default",
                        "updated_at": "2026-05-16T09:00:00Z",
                    }
                ],
                total=1,
                page=1,
                per_page=25,
                total_pages=1,
            )

    client = _client(
        project_query_service=lambda: _ProjectQuery(),
        project_command_service=lambda: SimpleNamespace(),
    )

    response = client.get("/api/projects")

    assert response.status_code == 200
    project = response.json()["projects"][0]
    assert project["workspace_name"] == "Product Workspace"
    assert project["operator_default_workflow_id"] == "wf_default"
    assert project["updated_at"] == "2026-05-16T09:00:00Z"


def test_api_projects_list_omission_clears_principal_workspace_but_explicit_filter_is_kept() -> (
    None
):
    observed_scopes: list[Any] = []

    class _ProjectQuery:
        def list_projects(self, **kwargs: Any) -> Any:
            observed_scopes.append(kwargs["scope"])
            return _dumping(
                projects=[],
                total=0,
                page=1,
                per_page=25,
                total_pages=1,
            )

    client = _client(
        project_query_service=lambda: _ProjectQuery(),
        project_command_service=lambda: SimpleNamespace(),
    )

    unfiltered = client.get("/api/projects")
    filtered = client.get("/api/projects?workspace_id=workspace-two")

    assert unfiltered.status_code == 200
    assert filtered.status_code == 200
    assert observed_scopes == [
        {"scope": {"workspace_id": ""}},
        {"scope": {"workspace_id": "workspace-two"}},
    ]


def test_api_project_update_allows_clearing_operator_default_workflow_id() -> None:
    captured: dict[str, Any] = {}

    class _ProjectQuery:
        def get_project_edit_form(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                project={
                    "id": "7",
                    "name": "Demo",
                    "workspace_id": "ws",
                    "operator_default_workflow_id": "wf_existing",
                },
                project_workflow_ids=["wf_existing", "wf_alt"],
            )

    class _ProjectCommand:
        def update_project(self, project_id: str, input_payload: Any, *, scope: Any) -> Any:
            captured["project_id"] = project_id
            captured["input"] = input_payload
            captured["scope"] = scope
            return _dumping(project_id=project_id, operator_default_workflow_id="")

    client = _client(
        project_query_service=lambda: _ProjectQuery(),
        project_command_service=lambda: _ProjectCommand(),
    )

    response = client.put(
        "/api/projects/7",
        json={
            "name": "Demo",
            "workspace_id": "ws",
            "workflow_ids": ["wf_existing", "wf_alt"],
            "operator_default_workflow_id": "",
        },
    )

    assert response.status_code == 200
    assert response.json()["operator_default_workflow_id"] == ""
    assert captured["input"].operator_default_workflow_id == ""


def test_api_project_update_preserves_an_explicitly_empty_workflow_binding_set() -> None:
    captured: dict[str, Any] = {}

    class _ProjectQuery:
        def get_project_edit_form(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                project={
                    "id": "7",
                    "name": "Demo",
                    "workspace_id": "ws",
                    "operator_default_workflow_id": "wf_existing",
                },
                project_workflow_ids=["wf_existing", "wf_alt"],
            )

    class _ProjectCommand:
        def update_project(self, project_id: str, input_payload: Any, *, scope: Any) -> Any:
            captured["input"] = input_payload
            return _dumping(project_id=project_id, workflow_ids=[])

    client = _client(
        project_query_service=lambda: _ProjectQuery(),
        project_command_service=lambda: _ProjectCommand(),
    )

    response = client.put(
        "/api/projects/7",
        json={
            "name": "Demo",
            "workspace_id": "ws",
            "workflow_ids": [],
            "operator_default_workflow_id": "",
        },
    )

    assert response.status_code == 200
    assert captured["input"].workflow_ids == []


def test_api_project_create_rejects_extra_tools_before_service_write() -> None:
    writes: list[Any] = []

    class _ProjectCommand:
        def create_project(self, input_payload: Any, *, scope: Any) -> Any:
            writes.append(input_payload)
            return _dumping(project_id="7", name="Demo")

    client = _client(
        project_query_service=lambda: SimpleNamespace(),
        project_command_service=lambda: _ProjectCommand(),
    )
    response = client.post(
        "/api/projects",
        json={
            "name": "Demo",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "workflow_ids": [],
            "extra_tools_json": "[]",
        },
    )

    assert (response.status_code, len(writes)) == (422, 0)
    assert response.json()["detail"] == "Unknown project field(s): extra_tools_json"


def test_api_project_update_rejects_extra_tools_before_service_write() -> None:
    writes: list[Any] = []

    class _ProjectQuery:
        def get_project_edit_form(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                project={
                    "id": "7",
                    "name": "Demo",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                }
            )

    class _ProjectCommand:
        def update_project(self, project_id: Any, input_payload: Any, *, scope: Any) -> Any:
            writes.append((project_id, input_payload))
            return _dumping(status="updated", project_id="7", name="Demo")

    client = _client(
        project_query_service=lambda: _ProjectQuery(),
        project_command_service=lambda: _ProjectCommand(),
    )
    response = client.put(
        "/api/projects/7",
        json={
            "name": "Renamed",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "extra_tools_json": "[]",
        },
    )

    assert (response.status_code, len(writes)) == (422, 0)
    assert response.json()["detail"] == "Unknown project field(s): extra_tools_json"
