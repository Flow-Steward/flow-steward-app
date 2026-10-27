"""Phase 2 project/workflow CLI migration regression tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.command_models import (
    ProjectNotificationDispatchResultView,
)
from core.application.management.query_models import (
    ProjectsListView,
    WorkflowValidationResultView,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.project_integrations import create_project_integrations_router
from core.entrypoints.api.projects import create_api_projects_router
from core.entrypoints.api.workflow_operator import create_workflow_operator_router
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _ProjectQuery:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def list_projects(self, **kwargs: Any) -> ProjectsListView:
        self.calls.append(("list_projects", kwargs))
        return ProjectsListView(
            projects=[{"id": "proj_1", "name": "Project One"}],
            total=1,
            page=int(kwargs.get("page") or 1),
            per_page=int(kwargs.get("per_page") or 25),
            total_pages=1,
        )

    def get_project_detail(self, project_id: str) -> dict[str, Any]:
        self.calls.append(("get_project_detail", {"project_id": project_id}))
        return {"project": {"id": project_id, "name": "Project One"}}

    def list_notification_routes(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_notification_routes", {"project_id": project_id, **kwargs}))
        return {"project_id": project_id, "notification_routes": []}

    def list_notification_deliveries(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_notification_deliveries", {"project_id": project_id, **kwargs}))
        return {"project_id": project_id, "notification_deliveries": []}


@dataclass
class _ProjectCommand:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def dispatch_project_notification(
        self,
        project_id: str,
        command: Any,
        **kwargs: Any,
    ) -> ProjectNotificationDispatchResultView:
        self.calls.append(
            (
                "dispatch_project_notification",
                {
                    "project_id": project_id,
                    "command": command.model_dump(mode="json"),
                    **kwargs,
                },
            )
        )
        return ProjectNotificationDispatchResultView(
            project_id=project_id,
            notification_deliveries=[{"id": "delivery_1"}],
        )


@dataclass
class _WorkflowOperator:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def list_project_workflows(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_project_workflows", {"project_id": project_id, **kwargs}))
        return {"project_id": project_id, "workflows": [{"workflow_id": "wf.release"}]}

    def list_project_entrypoints(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_project_entrypoints", {"project_id": project_id, **kwargs}))
        return {"project_id": project_id, "entrypoints": [{"entry_key": "release"}]}

    def project_workflow_validate(
        self, project_id: str, workflow_id: str, **kwargs: Any
    ) -> WorkflowValidationResultView:
        self.calls.append(
            (
                "project_workflow_validate",
                {"project_id": project_id, "workflow_id": workflow_id, **kwargs},
            )
        )
        return WorkflowValidationResultView(
            workflow_id=workflow_id,
            validation={"valid": True},
            issues=[],
        )

    def project_workflow_contract(self, project_id: str, workflow_id: str) -> dict[str, Any]:
        self.calls.append(
            (
                "project_workflow_contract",
                {"project_id": project_id, "workflow_id": workflow_id},
            )
        )
        return {"workflow_id": workflow_id, "interface": {"input": {}}}

    def project_workflow_suggestions(self, project_id: str, workflow_id: str) -> dict[str, Any]:
        self.calls.append(
            (
                "project_workflow_suggestions",
                {"project_id": project_id, "workflow_id": workflow_id},
            )
        )
        return {"workflow_id": workflow_id, "suggestions": []}


@dataclass
class _Runtime:
    project_query_service: _ProjectQuery
    project_command_service: _ProjectCommand
    workflow_operator_service: _WorkflowOperator
    user_query_service: object = object()
    user_command_service: object = object()
    extension_query_service: object = object()
    extension_binding_service: object = object()
    extension_scaffold_service: object = object()


def _runtime() -> _Runtime:
    return _Runtime(
        project_query_service=_ProjectQuery(),
        project_command_service=_ProjectCommand(),
        workflow_operator_service=_WorkflowOperator(),
    )


def _cli_app(runtime: _Runtime) -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(ServiceRegistry(runtime), SimpleNamespace())


def _scope(_request: Any = None, **kwargs: Any) -> ScopeContext:
    return ScopeContext(
        account_id="acc_default",
        allowed_project_ids=frozenset({str(kwargs.get("project_id") or "proj_1")}),
    )


def _assert_cli_scope(
    value: object, *, project_id: str | None = None, workflow_id: str | None = None
) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "system_actor"
    assert value.project_id == project_id
    assert value.workflow_id == workflow_id
    if project_id:
        assert value.allowed_project_ids == frozenset({project_id})
    if workflow_id:
        assert value.allowed_workflow_ids == frozenset({workflow_id})
    return value


def _service_error_response(exc: Exception, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=int(getattr(exc, "status_code", 500)))


def _api_error_response(exc: Exception, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


def test_projects_and_workflows_phase2_tree_contains_only_approved_leaves() -> None:
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    tree: dict[str, list[str]] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        if len(spec.path) == 2:
            tree.setdefault(spec.path[0], []).append(spec.path[1])

    assert tree["projects"] == [
        "list",
        "show",
        "notification-routes",
        "notification-deliveries",
        "dispatch-notification",
    ]
    assert tree["workflows"] == [
        "convert-group",
        "list",
        "entrypoints",
        "validate",
        "contract",
        "suggestions",
        "hooks-list",
        "hook-get",
        "hook-preview",
        "hook-bind",
        "hook-unbind",
        "hook-create-and-bind",
        "where-used",
        "authoring-schema",
        "draft-validate",
        "draft-save",
        "definition",
        "apply-changes",
        "export",
        "import",
        "run",
        "stats",
    ]


def test_legacy_project_workflow_aliases_are_not_public_routes() -> None:
    runtime = _runtime()
    app = _cli_app(runtime)

    for args in (
        ["projects", "index"],
        ["projects", "notifications"],
        ["workflows", "show"],
        ["tooling", "workflows", "validate"],
    ):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
    assert runtime.project_query_service.calls == []
    assert runtime.project_command_service.calls == []
    assert runtime.workflow_operator_service.calls == []


def test_cli_and_api_project_query_preserve_shared_service_and_scope_semantics() -> None:
    runtime = _runtime()
    cli = CliRunner().invoke(
        _cli_app(runtime),
        ["projects", "list", "--json", "--page", "2", "--per-page", "5", "--search", "api"],
    )
    assert cli.exit_code == 0, cli.output

    app = FastAPI()
    app.include_router(
        create_api_projects_router(
            job_query_service=lambda: object(),
            job_command_service=lambda: object(),
            project_query_service=lambda: runtime.project_query_service,
            project_command_service=lambda: runtime.project_command_service,
            request_scope=_scope,
            require_authenticated_user=lambda *_args, **_kwargs: {"roles": ["admin"]},
            local_auth_enabled=lambda: True,
            external_auth_enabled=False,
            auth_admin_roles={"admin"},
            resolve_actor_username=lambda *_args, **_kwargs: "tester",
            project_create_input_cls=dict,
            project_update_input_cls=dict,
            service_error_cls=Exception,
            service_not_found_error_cls=LookupError,
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
            err_admin_role_required="admin required",
            err_database_not_configured="database not configured",
            default_agent_models=lambda *_args, **_kwargs: {},
            available_providers=lambda *_args, **_kwargs: [],
            available_models=lambda *_args, **_kwargs: [],
        )
    )
    api = TestClient(app).get("/api/projects?page=2&per_page=5&search=api")
    assert api.status_code == 200

    assert [name for name, _payload in runtime.project_query_service.calls] == [
        "list_projects",
        "list_projects",
    ]
    cli_call, api_call = [payload for _name, payload in runtime.project_query_service.calls]
    _assert_cli_scope(cli_call["scope"])
    assert isinstance(api_call["scope"], ScopeContext)


def test_cli_and_api_notification_dispatch_preserve_shared_command_input() -> None:
    runtime = _runtime()
    cli = CliRunner().invoke(
        _cli_app(runtime),
        [
            "projects",
            "dispatch-notification",
            "--json",
            "--project-id",
            "proj_1",
            "--event-type",
            "workflow.completed",
            "--workflow-id",
            "wf.release",
            "--payload",
            '{"ok": true}',
            "--confirm",
        ],
    )
    assert cli.exit_code == 0, cli.output

    app = FastAPI()
    app.include_router(
        create_project_integrations_router(
            get_logger_only=lambda: object(),
            project_query_service=lambda: runtime.project_query_service,
            project_command_service=lambda: runtime.project_command_service,
            request_scope=_scope,
            require_authenticated_user=lambda *_args, **_kwargs: {"roles": ["admin"]},
            to_json_obj=lambda value: value,
            api_error_response=_api_error_response,
            service_error_response=_service_error_response,
            secret_storage_config_error=lambda _exc: False,
            auth_admin_roles={"admin"},
            err_database_not_configured="database not configured",
            err_project_not_found="project not found",
            err_admin_role_required="admin required",
        )
    )
    api = TestClient(app).post(
        "/api/projects/proj_1/notifications/dispatch",
        json={
            "event_type": "workflow.completed",
            "severity": "info",
            "workflow_id": "wf.release",
            "payload": {"ok": True},
        },
    )
    assert api.status_code == 200

    cli_call, api_call = runtime.project_command_service.calls
    assert cli_call[0] == api_call[0] == "dispatch_project_notification"
    assert cli_call[1]["command"] == api_call[1]["command"]
    _assert_cli_scope(cli_call[1]["scope"], project_id="proj_1", workflow_id="wf.release")
    assert isinstance(api_call[1]["scope"], ScopeContext)


def test_cli_and_api_workflow_validation_use_same_operator_method() -> None:
    runtime = _runtime()
    cli = CliRunner().invoke(
        _cli_app(runtime),
        ["workflows", "validate", "--json", "--project-id", "proj_1", "wf.release"],
    )
    assert cli.exit_code == 0, cli.output

    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=lambda: runtime.workflow_operator_service,
            request_scope=_scope,
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    api = TestClient(app).get("/api/projects/proj_1/workflows/wf.release/validate")
    assert api.status_code == 200

    cli_call, api_call = runtime.workflow_operator_service.calls
    assert cli_call[0] == api_call[0] == "project_workflow_validate"
    assert cli_call[1]["project_id"] == api_call[1]["project_id"] == "proj_1"
    assert cli_call[1]["workflow_id"] == api_call[1]["workflow_id"] == "wf.release"
    _assert_cli_scope(cli_call[1]["scope"], project_id="proj_1", workflow_id="wf.release")
    assert isinstance(api_call[1]["scope"], ScopeContext)


def test_fake_runtime_keeps_side_effect_boundaries_in_memory_only() -> None:
    runtime = _runtime()
    runner = CliRunner()
    app = _cli_app(runtime)

    for args in (
        [
            "projects",
            "dispatch-notification",
            "--json",
            "--project-id",
            "proj_1",
            "--event-type",
            "noop",
            "--confirm",
        ],
        ["workflows", "validate", "--json", "--project-id", "proj_1", "wf.release"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
        assert json.loads(result.output.splitlines()[-1])["ok"] is True

    assert [name for name, _payload in runtime.project_command_service.calls] == [
        "dispatch_project_notification",
    ]
    assert [name for name, _payload in runtime.workflow_operator_service.calls] == [
        "project_workflow_validate"
    ]
