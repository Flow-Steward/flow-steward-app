"""The workflow authoring CLI commands are adapters, not a second authoring API.

For each of the nine commands this proves three things against the HTTP route it
mirrors: the command requires the route's owner/project/workflow/revision inputs
(the CLI always names the project it acts in), the CLI binding reaches the same
application service method the route calls, and every write runs in the
deployment-admin scope. ``workflows.apply-changes`` is also run end to end
against one in-memory store through both the route and the CLI, and the stored
topologies are compared.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import WorkflowRunInput
from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS
from core.application.management.errors import ServiceError, ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.management.workflow_operator_service import WorkflowOperatorService
from core.application.workflows.authoring.service import WorkflowAuthoringService
from core.application.workflows.workflow_authoring_requests import (
    WorkflowChangeSet,
    WorkflowDraftInput,
)
from core.application.workflows.workflow_portability import WorkflowPortabilityService
from core.entrypoints.api.workflow_operator_portability import WorkflowImportBody
from core.entrypoints.cli.platform.service_bindings import workflow_authoring as bindings
from core.entrypoints.cli.platform.service_bindings.common import (
    cli_admin_scope,
    cli_operator_scope,
)
from core.entrypoints.cli.platform.service_registry import SERVICE_FACTORIES
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES
from tests._workflow_definitions import linear_workflow, node

PROJECT_ID = "6b0f8a8e-4c1e-4a43-9c1e-2b8a3f0d1c11"
WORKFLOW_ID = "wf_parity"

#: command id -> (HTTP method, route path, body model the route validates, MCP tool or None)
_ROUTES: dict[str, tuple[str, str, type | None, str | None]] = {
    "workflows.authoring-schema": (
        "GET",
        "/api/workflow-authoring/schema",
        None,
        "workflow_authoring_schema",
    ),
    "workflows.draft-validate": (
        "POST",
        "/api/projects/{project_id}/workflow-authoring/validate",
        WorkflowDraftInput,
        "workflow_draft_validate",
    ),
    "workflows.draft-save": (
        "POST",
        "/api/projects/{project_id}/workflow-authoring/drafts",
        WorkflowDraftInput,
        "workflow_draft_save",
    ),
    "workflows.definition": ("GET", "/api/workflows/{workflow_id}", None, "workflow_definition"),
    "workflows.apply-changes": (
        "POST",
        "/api/workflows/{workflow_id}/changes",
        WorkflowChangeSet,
        "workflow_apply_changes",
    ),
    "workflows.export": (
        "GET",
        "/api/projects/{project_id}/workflows/{workflow_id}/export",
        None,
        "workflow_export",
    ),
    "workflows.import": (
        "POST",
        "/api/projects/{project_id}/workflow-imports",
        WorkflowImportBody,
        "workflow_import",
    ),
    "workflows.run": ("POST", "/api/workflow-runs", WorkflowRunInput, "workflow_trigger"),
    "workflows.stats": (
        "GET",
        "/api/projects/{project_id}/workflows/{workflow_id}/stats",
        None,
        "workflow_stats",
    ),
}

#: A YAML bundle reaches the CLI as a file; the route takes its text.
_TRANSPORT_RENAMES = {"bundle_file": "bundle_yaml"}

#: Deployment-wide, as its route is: no project, no workflow.
_UNSCOPED_COMMANDS = {"workflows.authoring-schema"}

_WRITES = {
    "workflows.draft-save",
    "workflows.apply-changes",
    "workflows.import",
    "workflows.run",
}


def _specs() -> dict[str, Any]:
    return {spec.command_id: spec for spec in PUBLIC_COMMAND_SPECS if spec.command_id in _ROUTES}


def _http_required(path: str, body: type | None) -> set[str]:
    required = set(re.findall(r"{(\w+)}", path))
    if body is not None:
        required |= {name for name, field in body.model_fields.items() if field.is_required()}
    return required


def test_every_authoring_command_exists_with_a_binding_and_mirrors_a_real_route() -> None:
    from core.entrypoints.admin.workflows import router as admin_router
    from core.entrypoints.api.workflow_operator import create_workflow_operator_router

    specs = _specs()
    assert set(specs) == set(_ROUTES)
    admin_routes = {
        (method, route.path): route
        for route in admin_router.routes
        for method in getattr(route, "methods", ())
    }
    operator_routes = {
        (method, route.path): route
        for route in create_workflow_operator_router(
            workflow_operator_service=MagicMock,
            request_scope=MagicMock(),
            service_error_response=MagicMock(),
            api_error_response=MagicMock(),
        ).routes
        for method in getattr(route, "methods", ())
    }
    for command_id, (method, path, body, mcp_tool) in _ROUTES.items():
        assert command_id in SERVICE_FACTORIES
        if mcp_tool is not None:
            assert (MCP_TOOL_ROUTES[mcp_tool].method, MCP_TOOL_ROUTES[mcp_tool].path) == (
                method,
                path,
            )
        route = (admin_routes if path.startswith("/api/workflows/") else operator_routes).get(
            (method, path)
        )
        if route is not None:
            annotation = route.body_field.field_info.annotation if route.body_field else None
            assert annotation is body, command_id
        else:
            # The authoring router's own routes are checked by the facade test below.
            assert "/workflow-authoring" in path or path == "/api/workflow-runs", command_id


def test_required_cli_fields_are_the_routes_owner_project_workflow_and_revision_inputs() -> None:
    for command_id, spec in _specs().items():
        _method, path, body, _tool = _ROUTES[command_id]
        cli_required = {
            _TRANSPORT_RENAMES.get(field.key, field.key) for field in spec.fields if field.required
        }
        expected = _http_required(path, body)
        if command_id not in _UNSCOPED_COMMANDS:
            # The CLI always acts in a named project: reads check the workflow is
            # linked to it, a draft or an import is associated with it.
            expected |= {"project_id"}
        assert cli_required == expected, command_id


def test_reads_are_immediate_and_writes_always_review() -> None:
    for command_id, spec in _specs().items():
        if command_id in _WRITES:
            assert spec.operation_type.value == "change", command_id
            assert spec.interactive_policy.value == "always_review", command_id
        else:
            assert spec.operation_type.value == "read", command_id
            assert spec.interactive_policy.value == "immediate", command_id


# -- the CLI binding reaches the shared service, in the right scope -----------------


class _RecordingOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def record(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            return {"workflow_id": WORKFLOW_ID}

        return record


class _RecordingProjectCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def start_mcp_workflow_run(self, command: Any, **kwargs: Any) -> Any:
        self.calls.append((command, kwargs))
        return SimpleNamespace(dump=lambda: {"project_id": PROJECT_ID, "job_id": "1"})


@pytest.fixture
def bundle_file(tmp_path):
    path = tmp_path / "draft.workflow.yaml"
    path.write_text("schema_version: 1\nworkflows: []\n", encoding="utf-8")
    return str(path)


def _binding_cases(bundle: str) -> list[tuple[str, dict[str, Any], str]]:
    target = {"project_id": PROJECT_ID, "workflow_id": WORKFLOW_ID}
    return [
        ("workflows.authoring-schema", {"step_kind": "agent"}, "authoring_schema"),
        (
            "workflows.draft-validate",
            {"project_id": PROJECT_ID, "bundle_file": bundle},
            "draft_validate",
        ),
        ("workflows.draft-save", {"project_id": PROJECT_ID, "bundle_file": bundle}, "draft_save"),
        ("workflows.definition", target, "workflow_definition"),
        (
            "workflows.apply-changes",
            {**target, "expected_revision": 2, "operations": '[{"op": "remove_step"}]'},
            "apply_changes",
        ),
        ("workflows.export", target, "export_workflow"),
        ("workflows.import", {"project_id": PROJECT_ID, "bundle_file": bundle}, "import_workflow"),
        ("workflows.stats", target, "workflow_stats"),
    ]


def test_each_binding_calls_its_operator_method_with_the_matching_scope(bundle_file) -> None:
    for command_id, value, method in _binding_cases(bundle_file):
        operator = _RecordingOperator()
        runtime = SimpleNamespace(workflow_operator_service=operator)
        asyncio.run(SERVICE_FACTORIES[command_id](runtime, value))
        assert [name for name, _ in operator.calls] == [method], command_id
        scope = operator.calls[0][1]["scope"]
        assert isinstance(scope, ScopeContext)
        if command_id in _WRITES:
            assert scope.principal_type == "admin", command_id
        else:
            assert scope.principal_type == "system_actor", command_id
            if command_id not in _UNSCOPED_COMMANDS:
                assert scope.allowed_project_ids == frozenset({PROJECT_ID}), command_id


def test_bundle_bodies_reach_the_service_as_the_route_passes_them(bundle_file) -> None:
    operator = _RecordingOperator()
    runtime = SimpleNamespace(workflow_operator_service=operator)
    asyncio.run(
        bindings.workflows_draft_validate(
            runtime,
            {"project_id": PROJECT_ID, "bundle_file": bundle_file, "resolutions": "[]"},
        )
    )
    _name, kwargs = operator.calls[0]
    assert kwargs["bundle_yaml"].startswith("schema_version: 1")
    assert kwargs["resolutions"] is None  # As WorkflowDraftInput leaves an absent list.
    asyncio.run(
        bindings.workflows_apply_changes(
            runtime,
            {
                "project_id": PROJECT_ID,
                "workflow_id": WORKFLOW_ID,
                "expected_revision": 4,
                "operations": [{"op": "remove_step", "step_id": "x"}],
                "dry_run": True,
            },
        )
    )
    _name, kwargs = operator.calls[-1]
    assert kwargs["expected_revision"] == 4
    assert kwargs["operations"] == [{"op": "remove_step", "step_id": "x"}]
    assert kwargs["dry_run"] is True


def test_run_starts_through_the_same_project_command_as_post_workflow_runs() -> None:
    from core.entrypoints.api.jobs_control import create_jobs_control_router

    http_service = _RecordingProjectCommands()
    app = FastAPI()
    app.include_router(
        create_jobs_control_router(
            project_command_service=lambda: http_service,
            approval_command_service=MagicMock,
            job_command_service=MagicMock,
            request_scope=lambda _request, **kwargs: cli_admin_scope(),
            service_error_response=MagicMock(),
            api_error_response=MagicMock(),
            logger=MagicMock(),
        )
    )
    body = {
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "inputs": {"invoice_id": "INV-42"},
        "test_mode": True,
    }
    response = TestClient(app).post(
        "/api/workflow-runs", json=body, headers={"Idempotency-Key": "retry-1"}
    )
    assert response.status_code == 200, response.text

    cli_service = _RecordingProjectCommands()
    result = asyncio.run(
        bindings.workflows_run(
            SimpleNamespace(project_command_service=cli_service),
            {
                **body,
                "inputs": '{"invoice_id": "INV-42"}',
                "idempotency_key": "retry-1",
            },
        )
    )
    assert result == {"project_id": PROJECT_ID, "job_id": "1"}
    (http_command, http_kwargs), (cli_command, cli_kwargs) = (
        http_service.calls[0],
        cli_service.calls[0],
    )
    assert cli_command == http_command
    assert cli_kwargs["idempotency_key"] == http_kwargs["idempotency_key"] == "retry-1"
    assert cli_kwargs["scope"].principal_type == "admin"


# -- the operator adapters call the route's own application services ----------------


class _Store:
    """An in-memory registry store: one workflow, revisions, a locking cursor."""

    def __init__(self, workflow: dict[str, Any]) -> None:
        self.rows = {workflow["workflow_id"]: deepcopy(workflow)}
        self.saves: list[dict[str, Any]] = []

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        row = self.rows.get(workflow_id)
        return deepcopy(row) if row is not None else None

    def _transaction(self, *_args: Any, **_kwargs: Any):
        store = self

        class _Cursor:
            def execute(self, _sql: str, params: tuple[Any, ...] = ()) -> None:
                self.params = params

            def fetchone(self) -> dict[str, Any] | None:
                row = store.rows.get(str(self.params[0])) if self.params else None
                return {"revision": row["revision"]} if row else None

            def fetchall(self) -> list[Any]:
                return []

        return nullcontext(_Cursor())

    def save_workflow(self, payload: dict[str, Any], **kwargs: Any) -> int:
        current = self.rows[payload["workflow_id"]]
        revision = int(current["revision"]) + 1
        self.rows[payload["workflow_id"]] = {**current, **deepcopy(payload), "revision": revision}
        self.saves.append({"payload": deepcopy(payload), **kwargs})
        return revision


def _workflow() -> dict[str, Any]:
    return linear_workflow(
        {"step_id": "sync", "step_kind": "set_variable", "display_name": "Sync"},
        workflow_id=WORKFLOW_ID,
        enabled=False,
        revision=1,
        account_id=DEFAULT_ACCOUNT_ID,
    )


def _operator(store: _Store, **stores: Any) -> WorkflowOperatorService:
    binding = {"workflow_id": WORKFLOW_ID, "enabled": True, "availability_status": "available"}
    project_query = SimpleNamespace(
        get_workflow_bindings=lambda _project_id, scope=None: SimpleNamespace(
            project_workflows=[binding]
        )
    )
    return WorkflowOperatorService(
        get_logger=MagicMock,
        project_query_service=lambda: project_query,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=store, reload_all=lambda: None, refresh_workflow_cache=lambda: None
        ),
        registry_db_provider=lambda: store,
        **stores,
    )


_INSERT = {
    "op": "insert_step_between",
    "source": node("sync"),
    "outcome": {"name": "success"},
    "step": {"step_id": "audit", "step_kind": "set_variable", "display_name": "Audit"},
}


def _topology(store: _Store) -> dict[str, Any]:
    row = store.rows[WORKFLOW_ID]
    return {
        "revision": row["revision"],
        "steps": [(step["step_id"], step.get("step_kind")) for step in row["steps"]],
        "graph": row["settings"]["execution_graph"],
    }


def test_operator_refuses_licensed_installed_source_changes_without_store_write(
    monkeypatch,
) -> None:
    from core.application.registry import workflow_lookup

    workflow = _workflow()
    workflow.update(source_install_id="trusted-install", projection_mode="derived")
    workflow["settings"]["portability"] = {"allow_full_export": True}
    store = _Store(workflow)
    monkeypatch.setattr(
        workflow_lookup,
        "get_workflow_row",
        lambda _store, _workflow_id, account_id: deepcopy(workflow),
    )
    service = _operator(store)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="licensed-source-owner",
        principal_type="admin",
    )

    with pytest.raises(ServiceError) as raised:
        service.apply_changes(
            project_id=PROJECT_ID,
            workflow_id=WORKFLOW_ID,
            expected_revision=1,
            operations=[_INSERT],
            scope=scope,
        )

    assert raised.value.status_code == 403
    assert store.saves == []
    assert store.rows[WORKFLOW_ID] == workflow


def test_apply_changes_stores_the_same_topology_through_the_route_and_the_cli(
    monkeypatch,
) -> None:
    from core.application.registry import contract_validation
    from core.entrypoints.admin.workflows_definition import create_workflow_definition_router

    valid = {"valid": True, "errors": [], "warnings": []}
    monkeypatch.setattr(contract_validation, "validate_workflow_readiness", lambda *_a: dict(valid))

    http_store = _Store(_workflow())
    router = create_workflow_definition_router(
        get_db=lambda: http_store,
        get_registry=MagicMock,
        get_workflow_validation=lambda _db, _id: dict(valid),
        get_step_test_run_validation=lambda _db, _id, _step: dict(valid),
        get_readiness_validation=lambda _store, _id: dict(valid),
        resolve_workflow_row=lambda store, workflow_id: store.get_workflow(workflow_id),
        assert_workflow_structure_change_allowed=lambda _db, _id, _action: None,
        assert_workflow_step_test_run_allowed=lambda _db, _id, _action: None,
        request_scope=lambda _request: SimpleNamespace(
            account_id=DEFAULT_ACCOUNT_ID, actor_name="operator"
        ),
        enforce_workflow_row_scope=lambda _db, _scope, row: row,
        assert_workflow_mutation_allowed=lambda _row: None,
        validate_context_inject_sources=lambda _value: None,
        change_reference_access=lambda _db, _scope, _row: SimpleNamespace(
            agent=lambda _id: True,
            workflow=lambda _id: True,
            connection=lambda _id: True,
            install=lambda _id: True,
        ),
        durable_test_run_job_state=lambda _job_id: {},
        enqueue_test_run=lambda **_kwargs: 1,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/workflows")
    response = TestClient(app).post(
        f"/api/workflows/{WORKFLOW_ID}/changes",
        json={"expected_revision": 1, "operations": [_INSERT]},
    )
    assert response.status_code == 200, response.text

    cli_store = _Store(_workflow())
    runtime = SimpleNamespace(workflow_operator_service=_operator(cli_store))
    result = asyncio.run(
        bindings.workflows_apply_changes(
            runtime,
            {
                "project_id": PROJECT_ID,
                "workflow_id": WORKFLOW_ID,
                "expected_revision": 1,
                "operations": [_INSERT],
            },
        )
    )

    assert result["applied"] is True
    assert result["revision"] == 2
    assert response.json()["revision"] == result["revision"]
    assert _topology(cli_store) == _topology(http_store)
    assert ("audit", "set_variable") in _topology(cli_store)["steps"]
    assert cli_store.saves[0]["expected_revision"] == http_store.saves[0]["expected_revision"] == 1


def test_apply_changes_dry_run_and_a_stale_revision_save_nothing(monkeypatch) -> None:
    from core.application.management.errors import ServiceConflictError
    from core.application.registry import contract_validation

    monkeypatch.setattr(
        contract_validation,
        "validate_workflow_readiness",
        lambda *_a: {"valid": True, "errors": [], "warnings": []},
    )
    store = _Store(_workflow())
    service = _operator(store)
    preview = service.apply_changes(
        project_id=PROJECT_ID,
        workflow_id=WORKFLOW_ID,
        expected_revision=1,
        operations=[_INSERT],
        dry_run=True,
        scope=cli_admin_scope(),
    )
    assert preview["applied"] is False
    assert store.saves == []
    admin_scope = cli_admin_scope()
    with pytest.raises(ServiceConflictError):
        service.apply_changes(
            project_id=PROJECT_ID,
            workflow_id=WORKFLOW_ID,
            expected_revision=7,
            operations=[_INSERT],
            scope=admin_scope,
        )
    assert store.saves == []


def test_writes_refuse_the_read_only_operator_scope() -> None:
    service = _operator(_Store(_workflow()))
    scope = cli_operator_scope(project_id=PROJECT_ID, workflow_id=WORKFLOW_ID)
    for write, kwargs in (
        ("draft_save", {"project_id": PROJECT_ID, "bundle_yaml": "x"}),
        (
            "apply_changes",
            {
                "project_id": PROJECT_ID,
                "workflow_id": WORKFLOW_ID,
                "expected_revision": 1,
                "operations": [_INSERT],
            },
        ),
        ("import_workflow", {"project_id": PROJECT_ID, "bundle_yaml": "x"}),
    ):
        write_method = getattr(service, write)
        with pytest.raises(ServiceForbiddenError):
            write_method(scope=scope, **kwargs)


def test_schema_and_drafts_use_the_authoring_facade_methods_the_routes_dispatch(
    monkeypatch,
) -> None:
    from core.entrypoints.api.workflow_authoring import create_workflow_authoring_router

    http_calls: list[tuple[str, dict[str, Any]]] = []
    cli_calls: list[tuple[str, dict[str, Any]]] = []

    class _HttpFacade:
        def __getattr__(self, name: str):
            def record(**kwargs: Any) -> dict[str, Any]:
                http_calls.append((name, kwargs))
                return {}

            return record

    app = FastAPI()
    app.include_router(
        create_workflow_authoring_router(
            workflow_authoring_service=lambda _request: _HttpFacade(),
            request_scope=lambda _request, **kwargs: cli_admin_scope(),
            service_error_response=MagicMock(),
            api_error_response=MagicMock(),
        )
    )
    client = TestClient(app, raise_server_exceptions=False)  # Calls are what matter.
    body = {"bundle_yaml": "schema_version: 1\n", "resolutions": [{"action": "skip"}]}
    client.get("/api/workflow-authoring/schema", params={"step_kind": "agent"})
    client.post(f"/api/projects/{PROJECT_ID}/workflow-authoring/validate", json=body)
    client.post(f"/api/projects/{PROJECT_ID}/workflow-authoring/drafts", json=body)

    for name in ("workflow_authoring_schema", "workflow_draft_validate", "workflow_draft_save"):

        def record(self, *, _name=name, **kwargs: Any) -> dict[str, Any]:
            cli_calls.append((_name, kwargs))
            return {}

        monkeypatch.setattr(WorkflowAuthoringService, name, record)
    service = _operator(
        _Store(_workflow()),
        get_project_store=lambda: SimpleNamespace(),
        get_integration_store=lambda: SimpleNamespace(),
    )
    service.authoring_schema(step_kind="agent", scope=cli_operator_scope())
    service.draft_validate(
        project_id=PROJECT_ID,
        bundle_yaml=body["bundle_yaml"],
        resolutions=body["resolutions"],
        scope=cli_operator_scope(project_id=PROJECT_ID),
    )
    service.draft_save(
        project_id=PROJECT_ID,
        bundle_yaml=body["bundle_yaml"],
        resolutions=body["resolutions"],
        scope=cli_admin_scope(),
    )

    def without_scope(calls):
        return [(name, {k: v for k, v in kwargs.items() if k != "scope"}) for name, kwargs in calls]

    assert without_scope(cli_calls) == without_scope(http_calls)


def _route_source(path: str, method: str) -> str:
    from core.entrypoints.admin.workflows import router as admin_router

    for route in admin_router.routes:
        if route.path == path and method in route.methods:
            return inspect.getsource(inspect.getmodule(route.endpoint))
    raise AssertionError(f"{method} {path} is not routed")


def test_definition_export_import_and_stats_call_the_routes_services(monkeypatch) -> None:
    from core.application.extensions import service_support
    from core.application.workflows import workflow_template_catalog_create

    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        WorkflowPortabilityService,
        "export_yaml",
        lambda self, workflow_id, *, include_children=False, project_id="", reference_only=False: (
            calls.append(("export_yaml", (workflow_id, include_children, project_id)))
            or "kind: bundle\n"
        ),
    )
    monkeypatch.setattr(
        WorkflowPortabilityService,
        "apply_import",
        lambda self, bundle_yaml, **kwargs: (
            calls.append(("apply_import", kwargs)) or {"imported": [WORKFLOW_ID]}
        ),
    )
    monkeypatch.setattr(
        WorkflowPortabilityService,
        "redact_protected_workflow",
        lambda self, workflow: calls.append(("redact_protected_workflow", None)) or workflow,
    )
    monkeypatch.setattr(service_support, "get_default_extension_install_store", lambda **_k: None)
    monkeypatch.setattr(
        workflow_template_catalog_create,
        "resolve_portable_extensions",
        lambda _service, _bundle, *, resolutions, **_k: (
            calls.append(("resolve_portable_extensions", None)) or (resolutions, [], {})
        ),
    )
    job_store = SimpleNamespace(
        get_workflow_stats=lambda **kwargs: (
            calls.append(("get_workflow_stats", kwargs)) or {"total_runs": 3}
        )
    )
    service = _operator(_Store(_workflow()), get_job_store=lambda: job_store)
    read = cli_operator_scope(project_id=PROJECT_ID, workflow_id=WORKFLOW_ID)

    definition = service.workflow_definition(
        project_id=PROJECT_ID, workflow_id=WORKFLOW_ID, scope=read
    )
    exported = service.export_workflow(
        project_id=PROJECT_ID, workflow_id=WORKFLOW_ID, include_children=True, scope=read
    )
    imported = service.import_workflow(
        project_id=PROJECT_ID, bundle_yaml=exported["bundle_yaml"], scope=cli_admin_scope()
    )
    stats = service.workflow_stats(project_id=PROJECT_ID, workflow_id=WORKFLOW_ID, scope=read)

    assert definition["workflow_id"] == WORKFLOW_ID
    assert definition["revision"] == 1
    assert exported["bundle_yaml"] == "kind: bundle\n"
    assert imported == {"imported": [WORKFLOW_ID]}
    assert stats == {"workflow_id": WORKFLOW_ID, "total_runs": 3}
    by_name = dict(calls)
    # Exported from the project, so the project's hook bindings travel with it.
    assert by_name["export_yaml"] == (WORKFLOW_ID, True, PROJECT_ID)
    assert by_name["apply_import"]["owner_project_id"] == PROJECT_ID
    assert by_name["apply_import"]["owner_account_id"] == DEFAULT_ACCOUNT_ID
    assert by_name["get_workflow_stats"] == {
        "workflow_id": WORKFLOW_ID,
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    # The routes call the very same service methods.
    assert "redact_protected_workflow(" in _route_source("/api/workflows/{workflow_id}", "GET")
    assert ".export_yaml(" in _route_source("/api/workflows/{workflow_id}/export.yaml", "GET")
    import_source = _route_source("/api/workflows/import/apply", "POST")
    assert ".apply_import(" in import_source
    assert "resolve_portable_extensions(" in import_source
    assert ".get_workflow_stats(" in _route_source("/api/workflows/{workflow_id}/stats", "GET")


def test_reads_refuse_a_workflow_not_linked_to_the_project() -> None:
    from core.application.management.errors import ServiceNotFoundError

    service = _operator(_Store(_workflow()))
    scope = cli_operator_scope(project_id=PROJECT_ID, workflow_id="wf_other")
    with pytest.raises(ServiceNotFoundError):
        service.workflow_definition(project_id=PROJECT_ID, workflow_id="wf_other", scope=scope)


@pytest.mark.parametrize("mode", ["production", "development"])
def test_route_and_cli_share_hook_creation_mode_policy(monkeypatch, mode):
    from core.application.management.errors import ServiceValidationError
    from core.application.registry import contract_validation
    from core.entrypoints.admin.workflows_definition import create_workflow_definition_router
    from tests.unit.test_workflow_hook_contract import declaration

    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    valid = {"valid": True, "errors": [], "warnings": []}
    monkeypatch.setattr(contract_validation, "validate_workflow_readiness", lambda *_a: dict(valid))
    hook = declaration(
        input_schema={"type": "object", "properties": {}},
        input_mapping={},
        mutable_fields=[],
        additional_outputs={},
    )
    operation = {
        "op": "insert_step_between",
        "source": {"kind": "step", "id": "sync"},
        "outcome": {"name": "success"},
        "step": {
            "step_id": "before",
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"hook": hook}},
        },
    }
    http_store = _Store(_workflow())
    router = create_workflow_definition_router(
        get_db=lambda: http_store,
        get_registry=MagicMock,
        get_workflow_validation=lambda _db, _id: dict(valid),
        get_step_test_run_validation=lambda _db, _id, _step: dict(valid),
        get_readiness_validation=lambda _store, _id: dict(valid),
        resolve_workflow_row=lambda store, workflow_id: store.get_workflow(workflow_id),
        assert_workflow_structure_change_allowed=lambda db, workflow_id, action: (
            WorkflowPortabilityService(db).assert_structure_change_allowed(
                workflow_id, action=action
            )
        ),
        assert_workflow_step_test_run_allowed=lambda _db, _id, _action: None,
        request_scope=lambda _request: SimpleNamespace(
            account_id=DEFAULT_ACCOUNT_ID, actor_name="operator"
        ),
        enforce_workflow_row_scope=lambda _db, _scope, row: row,
        assert_workflow_mutation_allowed=lambda _row: None,
        validate_context_inject_sources=lambda _value: None,
        change_reference_access=lambda _db, _scope, _row: SimpleNamespace(
            agent=lambda _id: True,
            workflow=lambda _id: True,
            connection=lambda _id: True,
            install=lambda _id: True,
        ),
        durable_test_run_job_state=lambda _job_id: {},
        enqueue_test_run=lambda **_kwargs: 1,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/workflows")
    response = TestClient(app).post(
        f"/api/workflows/{WORKFLOW_ID}/changes?installation_mode=development",
        headers={"X-Installation-Mode": "development"},
        json={"expected_revision": 1, "operations": [operation]},
    )
    cli_store = _Store(_workflow())
    runtime = SimpleNamespace(workflow_operator_service=_operator(cli_store))
    arguments = {
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "expected_revision": 1,
        "operations": [operation],
    }
    if mode == "production":
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "Contract validation failed"
        assert (detail["entity_type"], detail["entity_id"], detail["action"]) == (
            "workflow",
            WORKFLOW_ID,
            "apply_changes",
        )
        assert detail["validation"]["valid"] is False
        assert any(
            issue.get("code") == "workflow.hook.declaration_requires_development"
            for issue in detail["validation"]["errors"]
        )
        apply_changes = bindings.workflows_apply_changes(runtime, arguments)
        with pytest.raises(ServiceValidationError) as raised:
            asyncio.run(apply_changes)
        assert raised.value.status_code == 400
        assert set(raised.value.payload) == {"message", "issues"}
        assert any(
            issue.get("code") == "workflow.hook.declaration_requires_development"
            for issue in raised.value.payload["issues"]
        )
        assert http_store.saves == []
        assert cli_store.saves == []
        assert (
            http_store.rows[WORKFLOW_ID]["revision"] == cli_store.rows[WORKFLOW_ID]["revision"] == 1
        )
    else:
        assert response.status_code == 200, response.text
        result = asyncio.run(bindings.workflows_apply_changes(runtime, arguments))
        assert result["applied"] is True
        assert response.json()["revision"] == result["revision"] == 2
        assert _topology(cli_store) == _topology(http_store)
        assert len(cli_store.saves) == len(http_store.saves) == 1


def test_operator_import_reads_the_project_model_from_the_project_store() -> None:
    """CLI, API and MCP imports check the model where the admin import does."""
    project_store = SimpleNamespace()
    service = _operator(
        _Store(_workflow()),
        get_project_store=lambda: project_store,
        get_integration_store=lambda: SimpleNamespace(),
    )

    assert service._portability().project_store is project_store
