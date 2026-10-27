from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management import ServiceForbiddenError, ServiceNotFoundError
from core.application.management.query_models import (
    ProjectWorkflowCatalogView,
    ProjectWorkflowEntrypointsView,
    WorkflowContractResultView,
    WorkflowSuggestionsResultView,
    WorkflowValidationResultView,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.workflow_operator import create_workflow_operator_router


class _FakeWorkflowOperatorService:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def list_project_workflows(
        self,
        project_id,
        *,
        enabled_only=True,
        include_validation=False,
        scope=None,
    ):
        self.calls.append(
            (
                "list_project_workflows",
                {
                    "project_id": project_id,
                    "enabled_only": enabled_only,
                    "include_validation": include_validation,
                    "scope": scope,
                },
            )
        )
        return ProjectWorkflowCatalogView(
            project_id=str(project_id),
            workflows=[
                {
                    "workflow_id": "wf.weekly_digest",
                    "display_name": "Quick Fix",
                    "enabled": True,
                    "availability_status": "available",
                }
            ],
        )

    def list_project_entrypoints(self, project_id, *, enabled_only=True, scope=None):
        self.calls.append(
            (
                "list_project_entrypoints",
                {"project_id": project_id, "enabled_only": enabled_only, "scope": scope},
            )
        )
        return ProjectWorkflowEntrypointsView(
            project_id=str(project_id),
            entrypoints=[
                {
                    "entry_key": "quick-fix",
                    "workflow_id": "wf.weekly_digest",
                    "enabled": True,
                    "allowed_methods": ["POST"],
                }
            ],
        )

    def project_workflow_validate(self, project_id: str, workflow_id: str, *, scope=None):
        self.calls.append(
            (
                "project_workflow_validate",
                {"project_id": project_id, "workflow_id": workflow_id, "scope": scope},
            )
        )
        return WorkflowValidationResultView(
            workflow_id=workflow_id,
            validation={"valid": True},
            issues=[],
        )

    def project_workflow_contract(self, project_id: str, workflow_id: str, *, scope=None):
        self.calls.append(
            (
                "project_workflow_contract",
                {"project_id": project_id, "workflow_id": workflow_id, "scope": scope},
            )
        )
        return WorkflowContractResultView(
            workflow_id=workflow_id,
            interface={"expected_inputs": []},
        )

    def project_workflow_suggestions(self, project_id: str, workflow_id: str, *, scope=None):
        self.calls.append(
            (
                "project_workflow_suggestions",
                {"project_id": project_id, "workflow_id": workflow_id, "scope": scope},
            )
        )
        return WorkflowSuggestionsResultView(workflow_id=workflow_id, suggestions=[])

    def workflow_validate(self, workflow_id: str):
        self.calls.append(("workflow_validate", {"workflow_id": workflow_id}))
        return WorkflowValidationResultView(
            workflow_id=workflow_id, validation={"valid": True}, issues=[]
        )

    def workflow_contract(self, workflow_id: str):
        self.calls.append(("workflow_contract", {"workflow_id": workflow_id}))
        return WorkflowContractResultView(workflow_id=workflow_id, interface={})

    def workflow_suggestions(self, workflow_id: str):
        self.calls.append(("workflow_suggestions", {"workflow_id": workflow_id}))
        return WorkflowSuggestionsResultView(workflow_id=workflow_id, suggestions=[])


def _request_scope(_request, **_kwargs):
    return ScopeContext(account_id="acc_default")


def _service_error_response(exc):
    return JSONResponse(
        {"error": str(exc)},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def test_api_workflow_operator_routes_use_shared_service():
    service = _FakeWorkflowOperatorService()

    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=lambda: service,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )

    client = TestClient(app)

    assert client.get("/api/projects/proj-1/workflow-catalog").status_code == 200
    assert client.get("/api/projects/proj-1/workflow-entrypoints").status_code == 200
    assert client.get("/api/projects/proj-1/workflows/wf.weekly_digest/validate").status_code == 200
    assert client.get("/api/projects/proj-1/workflows/wf.weekly_digest/contract").status_code == 200
    assert (
        client.get("/api/projects/proj-1/workflows/wf.weekly_digest/suggestions").status_code == 200
    )

    assert [name for name, _payload in service.calls] == [
        "list_project_workflows",
        "list_project_entrypoints",
        "project_workflow_validate",
        "project_workflow_contract",
        "project_workflow_suggestions",
    ]
    catalog_call = service.calls[0][1]
    assert catalog_call["include_validation"] is False


def test_api_workflow_operator_returns_404_for_unlinked_project_workflow():
    class _FailingWorkflowOperatorService(_FakeWorkflowOperatorService):
        def project_workflow_validate(self, project_id: str, workflow_id: str, *, scope=None):
            raise ServiceNotFoundError("Workflow not linked to project")

    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=_FailingWorkflowOperatorService,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )

    client = TestClient(app)
    response = client.get("/api/projects/proj-1/workflows/wf.other/validate")
    assert response.status_code == 404
    assert "Workflow not linked to project" in response.json()["error"]


def test_api_workflow_operator_returns_404_for_stale_registry_workflow():
    class _FailingWorkflowOperatorService(_FakeWorkflowOperatorService):
        def project_workflow_validate(self, project_id: str, workflow_id: str, *, scope=None):
            raise ServiceNotFoundError(f"Workflow '{workflow_id}' not found")

    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=_FailingWorkflowOperatorService,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )

    client = TestClient(app)
    response = client.get("/api/projects/proj-1/workflows/wf.weekly_digest/validate")
    assert response.status_code == 404
    assert "Workflow 'wf.weekly_digest' not found" in response.json()["error"]


def test_api_workflow_operator_returns_403_for_forbidden_project_scope():
    class _ForbiddenWorkflowOperatorService(_FakeWorkflowOperatorService):
        def project_workflow_contract(self, project_id: str, workflow_id: str, *, scope=None):
            raise ServiceForbiddenError("Access denied")

    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=_ForbiddenWorkflowOperatorService,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )

    client = TestClient(app)
    response = client.get("/api/projects/proj-1/workflows/wf.weekly_digest/contract")
    assert response.status_code == 403
    assert "Access denied" in response.json()["error"]


def test_api_workflow_operator_global_routes_apply_shared_workflow_scope() -> None:
    service = _FakeWorkflowOperatorService()
    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=lambda: service,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )
    client = TestClient(app)

    assert client.get("/api/workflows/wf.weekly_digest/validate").status_code == 200
    assert client.get("/api/workflows/wf.weekly_digest/suggestions").status_code == 200
    assert [name for name, _ in service.calls] == [
        "workflow_validate",
        "workflow_suggestions",
    ]
    # `/api/workflows/{id}/contract` is not in this list because this router
    # does not own it. It used to register a second handler for that path, which
    # the admin router already owned and Starlette therefore never reached: the
    # assertion passed here while nothing in production could call it.
    assert client.get("/api/workflows/wf.weekly_digest/contract").status_code == 404


def test_hook_routes_validate_with_shared_dtos_and_call_the_shared_service():
    calls: list[tuple[str, dict]] = []

    class _HookService:
        def __getattr__(self, name):
            def _method(**kwargs):
                calls.append((name, kwargs))
                if name == "hook_preview":
                    return {"valid": True, "issues": []}
                if name in {"hook_bind", "hook_unbind", "hook_create_and_bind"}:
                    return {
                        "hook_id": kwargs["hook_id"],
                        "binding_revision": 1,
                        "revision_id": "rev-1",
                        "target_workflow_id": kwargs.get("target_workflow_id"),
                    }
                if name == "hook_get":
                    return {"hook_id": kwargs["hook_id"], "binding_revision": 0}
                if name == "where_used":
                    return {"workflow_id": kwargs["workflow_id"], "consumers": []}
                return {"project_id": kwargs["project_id"], "workflow_id": "wf", "hooks": []}

            return _method

    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=_HookService,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )
    client = TestClient(app)
    base = "/api/projects/proj-1/workflows/wf"
    body = {
        "expected_revision": 0,
        "target_workflow_id": "child",
        "input_mapping": {"invoice_id": "invoice_id"},
        "output_mapping": {"company_name": "company"},
    }
    assert client.get(f"{base}/hooks").status_code == 200
    assert client.get(f"{base}/hooks/before_invoice").status_code == 200
    assert client.post(f"{base}/hooks/before_invoice/preview", json=body).status_code == 200
    assert client.post(f"{base}/hooks/before_invoice/bind", json=body).status_code == 200
    assert (
        client.post(
            f"{base}/hooks/before_invoice/unbind", json={"expected_revision": 1}
        ).status_code
        == 200
    )
    created = client.post(
        f"{base}/hooks/before_invoice/create-and-bind",
        json={"expected_revision": 0, "child_workflow_id": "actions"},
    )
    assert created.status_code == 200
    assert client.get(f"{base}/where-used").status_code == 200
    assert [name for name, _ in calls] == [
        "hooks_list",
        "hook_get",
        "hook_preview",
        "hook_bind",
        "hook_unbind",
        "hook_create_and_bind",
        "where_used",
    ]
    bind = calls[3][1]
    assert (bind["project_id"], bind["workflow_id"], bind["hook_id"]) == (
        "proj-1",
        "wf",
        "before_invoice",
    )
    assert bind["output_mapping"] == {"company_name": "company"}
    assert calls[2][1]["sample"] is None
    # The shared DTO refuses a malformed mapping before the service is called.
    bad = client.post(
        f"{base}/hooks/before_invoice/bind",
        json={**body, "unknown": 1},
    )
    assert bad.status_code == 422
    # A sample is a preview input only: a bind refuses it, a preview passes it on.
    assert (
        client.post(f"{base}/hooks/before_invoice/bind", json={**body, "sample": {}}).status_code
        == 422
    )
    sample = {"inputs": {"invoice_id": 7}, "child_outputs": {"company_name": "Acme"}}
    sampled = client.post(f"{base}/hooks/before_invoice/preview", json={**body, "sample": sample})
    assert sampled.status_code == 200
    assert calls[-1][0] == "hook_preview"
    assert calls[-1][1]["sample"] == sample
    assert (
        client.post(
            f"{base}/hooks/before_invoice/preview", json={**body, "sample": {"outputs": {}}}
        ).status_code
        == 422
    )
    assert len(calls) == 8


def test_portability_routes_call_the_operator_methods_with_the_cli_arguments(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from core.entrypoints.cli.platform.service_bindings import workflow_authoring as cli

    class _Recorder:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        def __getattr__(self, name):
            def _method(**kwargs):
                self.calls.append((name, kwargs))
                if name == "export_workflow":
                    return {
                        "workflow_id": kwargs["workflow_id"],
                        "filename": f"{kwargs['workflow_id']}.workflow.yaml",
                        "include_children": kwargs["include_children"],
                        "bundle_yaml": "kind: bundle\n",
                    }
                if name == "workflow_stats":
                    return {"workflow_id": kwargs["workflow_id"], "total_runs": 3}
                return {"imported": ["wf"], "workflow_id": "wf"}

            return _method

    http = _Recorder()
    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=lambda *_: http,
            request_scope=_request_scope,
            service_error_response=_service_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )
    client = TestClient(app)
    base = "/api/projects/proj-1/workflows/wf"
    bundle = "schema_version: 1\nworkflows: []\n"
    resolutions = [{"dependency_key": "agent:writer", "action": "map_existing"}]

    exported = client.get(f"{base}/export", params={"include_children": "true"})
    assert exported.status_code == 200, exported.text
    assert exported.json()["bundle_yaml"] == "kind: bundle\n"
    stats = client.get(f"{base}/stats")
    assert stats.status_code == 200, stats.text
    assert stats.json() == {"workflow_id": "wf", "total_runs": 3}
    imported = client.post(
        "/api/projects/proj-1/workflow-imports",
        json={"bundle_yaml": bundle, "target_workflow_id": "wf_copy", "resolutions": resolutions},
    )
    assert imported.status_code == 200, imported.text
    plain = client.post("/api/projects/proj-1/workflow-imports", json={"bundle_yaml": bundle})
    assert plain.status_code == 200, plain.text

    # The shared request models refuse a malformed request before the service is called.
    for bad in (
        {"bundle_yaml": bundle, "unknown": 1},
        {"target_workflow_id": "wf_copy"},
        {"bundle_yaml": bundle, "resolutions": {}},
        {"bundle_yaml": bundle, "target_workflow_id": "bad id!"},
    ):
        response = client.post("/api/projects/proj-1/workflow-imports", json=bad)
        assert response.status_code == 422, (bad, response.text)
    assert client.get(f"{base}/export", params={"include_children": "maybe"}).status_code == 422
    assert [name for name, _ in http.calls] == [
        "export_workflow",
        "workflow_stats",
        "import_workflow",
        "import_workflow",
    ]

    bundle_file = tmp_path / "bundle.workflow.yaml"
    bundle_file.write_text(bundle, encoding="utf-8")
    cli_operator = _Recorder()
    runtime = SimpleNamespace(workflow_operator_service=cli_operator)
    target = {"project_id": "proj-1", "workflow_id": "wf"}
    asyncio.run(cli.workflows_export(runtime, {**target, "include_children": True}))
    asyncio.run(cli.workflows_stats(runtime, target))
    asyncio.run(
        cli.workflows_import(
            runtime,
            {
                "project_id": "proj-1",
                "bundle_file": str(bundle_file),
                "target_workflow_id": "wf_copy",
                "resolutions": resolutions,
            },
        )
    )
    asyncio.run(
        cli.workflows_import(runtime, {"project_id": "proj-1", "bundle_file": str(bundle_file)})
    )

    def without_scope(calls):
        return [(name, {k: v for k, v in kwargs.items() if k != "scope"}) for name, kwargs in calls]

    assert without_scope(http.calls) == without_scope(cli_operator.calls)
    assert all(isinstance(kwargs["scope"], ScopeContext) for _name, kwargs in http.calls)
