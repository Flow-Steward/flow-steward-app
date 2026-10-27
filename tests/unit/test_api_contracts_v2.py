from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if "psycopg_runtime" not in sys.modules:
    psycopg_runtime_mod = types.ModuleType("psycopg_runtime")
    extras_mod = types.ModuleType("psycopg_runtime.extras")
    extras_mod.RealDictCursor = object
    psycopg_runtime_mod.extras = extras_mod
    sys.modules["psycopg_runtime"] = psycopg_runtime_mod
    sys.modules["psycopg_runtime.extras"] = extras_mod

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import request_auth_user_payload
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.reports.ui_projection import user_role_from_auth
from core.entrypoints.api.contracts_v2 import create_api_contracts_v2_router

JOB_ID = "00000000-0000-0000-0000-000000000501"
PROJECT_ID = "00000000-0000-0000-0000-000000000401"


class _Payload:
    def __init__(self, payload):
        self._payload = payload

    def dump(self):
        return dict(self._payload)


class _JobQueryService:
    def __init__(self):
        self.last_list_kwargs = {}

    def list_jobs(self, **kwargs):
        self.last_list_kwargs = dict(kwargs)
        return _Payload(
            {
                "jobs": [{"id": 101, "status": "pending"}],
                "page": int(kwargs.get("page") or 1),
                "per_page": int(kwargs.get("per_page") or 25),
                "total": 1,
                "total_pages": 1,
            }
        )

    async def get_job_status(self, job_id, *, scope, include_workflow_result=False):
        assert isinstance(scope, ScopeContext)
        return _Payload(
            {
                "id": str(job_id),
                "status": "failed",
                "failure_summary": {
                    "present": True,
                    "kind": "provider_rate_limit",
                    "diagnostic_ids": {"request_id": "req-safe-42"},
                },
            }
        )

    def list_project_jobs(self, project_id, **kwargs):
        return _Payload(
            {
                "jobs": [
                    {"id": "00000000-0000-0000-0000-000000000502", "project_id": str(project_id)}
                ],
                "page": int(kwargs.get("page") or 1),
                "per_page": int(kwargs.get("per_page") or 25),
                "total": 1,
                "total_pages": 1,
            }
        )


class _ProjectQueryService:
    def list_projects(self, **kwargs):
        return _Payload(
            {
                "projects": [{"id": 11, "name": "Demo"}],
                "page": int(kwargs.get("page") or 1),
                "per_page": int(kwargs.get("per_page") or 25),
                "total": 1,
                "total_pages": 1,
            }
        )


class _JobCommandService:
    def bulk_delete_project_jobs(self, input_payload, *, scope):
        assert isinstance(scope, ScopeContext)
        return SimpleNamespace(
            status="ok",
            requested=len(input_payload.ids),
            deleted=[1],
            deleted_count=1,
            skipped=[],
        )


def _build_client(
    *,
    monkeypatch,
    jobs_query_service=None,
    project_query_service=None,
    require_authenticated_user=None,
    local_auth_enabled=False,
    auth_user=None,
    auth_principal=None,
):
    import core.entrypoints.api.contracts_v2 as contracts_v2

    async def _fake_dashboard_summary(request, range="7d"):
        _ = request
        return {"range": range, "jobs_running": 2}

    monkeypatch.setattr(
        contracts_v2.dashboard_metrics_api,
        "api_dashboard_summary",
        _fake_dashboard_summary,
    )

    app = FastAPI()

    @app.middleware("http")
    async def set_auth_context(request, call_next):
        request.state.auth_user = auth_user
        request.state.auth_principal = auth_principal
        return await call_next(request)

    app.include_router(
        create_api_contracts_v2_router(
            resolve_user_role=lambda request: user_role_from_auth(
                request_auth_user_payload(request, resolved_principal_type=ResolvedPrincipal)
            ),
            request_scope=lambda _request, **kwargs: ScopeContext(
                account_id="acc_default",
                project_id=kwargs.get("project_id"),
                workspace_id=kwargs.get("workspace_id"),
            ),
            job_query_service=lambda: jobs_query_service or _JobQueryService(),
            job_command_service=lambda: _JobCommandService(),
            project_query_service=lambda: project_query_service or _ProjectQueryService(),
            require_authenticated_user=require_authenticated_user
            or (
                lambda _request, required_roles=None: {
                    "username": "admin",
                    "roles": required_roles or [],
                }
            ),
            local_auth_enabled=lambda: bool(local_auth_enabled),
            external_auth_enabled=False,
            auth_admin_roles={"admin"},
            err_admin_role_required="Admin role required",
        )
    )
    return TestClient(app)


def test_v2_contracts_success_envelopes(monkeypatch):
    client = _build_client(monkeypatch=monkeypatch)

    jobs = client.get("/api/v2/jobs?page=2&per_page=10")
    assert jobs.status_code == 200
    jobs_body = jobs.json()
    assert jobs_body["ok"] is True
    assert jobs_body["kind"] == "list"
    assert jobs_body["meta"]["resource"] == "jobs"
    assert jobs_body["meta"]["pagination"]["page"] == 2
    assert jobs_body["data"]["jobs"][0]["id"] == 101

    job_detail = client.get(f"/api/v2/jobs/{JOB_ID}")
    assert job_detail.status_code == 200
    detail_body = job_detail.json()
    assert detail_body["ok"] is True
    assert detail_body["kind"] == "detail"
    assert detail_body["data"]["id"] == JOB_ID
    assert detail_body["data"]["job"]["id"] == JOB_ID
    assert detail_body["data"]["failure_summary"]["kind"] == "provider_rate_limit"
    assert detail_body["data"]["failure_summary"]["diagnostic_ids"]["request_id"] == "req-safe-42"
    assert detail_body["data"]["lifecycle"]["status"]["code"] == "failed"

    projects = client.get("/api/v2/projects")
    assert projects.status_code == 200
    assert projects.json()["data"]["projects"][0]["name"] == "Demo"

    project_jobs = client.get(f"/api/v2/projects/{PROJECT_ID}/jobs")
    assert project_jobs.status_code == 200
    assert project_jobs.json()["data"]["jobs"][0]["project_id"] == PROJECT_ID

    bulk_delete = client.post(
        f"/api/v2/projects/{PROJECT_ID}/jobs/bulk-delete",
        json={"ids": ["00000000-0000-0000-0000-000000000503"], "confirm": "DELETE"},
    )
    assert bulk_delete.status_code == 200
    mutation = bulk_delete.json()
    assert mutation["ok"] is True
    assert mutation["kind"] == "mutation"
    assert mutation["data"]["deleted_count"] == 1

    dashboard = client.get("/api/v2/dashboard/summary?range=24h")
    assert dashboard.status_code == 200
    dashboard_body = dashboard.json()
    assert dashboard_body["ok"] is True
    assert dashboard_body["meta"]["resource"] == "dashboard-summary"
    assert dashboard_body["data"]["range"] == "24h"


def test_v2_jobs_forwards_workflow_and_search_filters(monkeypatch):
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)

    response = client.get(
        "/api/v2/jobs?page=2&per_page=25&workflow_id=inventory-import&search=supplier+feed"
    )

    assert response.status_code == 200
    assert service.last_list_kwargs["workflow_id"] == "inventory-import"
    assert service.last_list_kwargs["search"] == "supplier feed"


def test_v2_jobs_forwards_every_status_of_a_multi_status_filter(monkeypatch):
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)

    response = client.get("/api/v2/jobs?status=failed&status=stuck")

    assert response.status_code == 200
    assert service.last_list_kwargs["status"] == ["failed", "stuck"]


def test_job_status_filter_values_accepts_repeats_and_comma_lists() -> None:
    from core.application.management.query_services_job_core_mixin import (
        job_status_filter_values,
    )

    assert job_status_filter_values(["Failed", "stuck,failed", " ", "acknowledged"]) == (
        "failed",
        "stuck",
        "acknowledged",
    )
    assert job_status_filter_values("failed, stuck") == ("failed", "stuck")
    assert job_status_filter_values("") == ()
    assert job_status_filter_values(None) == ()


def test_v2_compact_job_detail_and_execution_step_page(monkeypatch):
    import core.entrypoints.api.contracts_v2 as contracts_v2

    captured_detail: dict = {}
    captured_page: dict = {}

    async def _detail(**kwargs):
        captured_detail.update(kwargs)
        return {"id": kwargs["job_id"], "status": "completed"}

    async def _page(**kwargs):
        captured_page.update(kwargs)
        return {
            "job_id": kwargs["job_id"],
            "steps": [{"id": "step-1"}],
            "page": {"has_more": False, "next_cursor": ""},
        }

    monkeypatch.setattr(contracts_v2, "build_job_detail_contract", _detail)
    monkeypatch.setattr(contracts_v2, "build_job_execution_steps_page", _page)
    client = _build_client(monkeypatch=monkeypatch)

    detail = client.get(f"/api/v2/jobs/{JOB_ID}?compact=true&step_limit=40")
    page = client.get(f"/api/v2/jobs/{JOB_ID}/execution-steps?limit=40&cursor=80%3Amessage-80")

    assert detail.status_code == 200
    assert captured_detail["execution_step_limit"] == 40
    assert captured_detail["include_workflow_run"] is False
    assert page.status_code == 200
    assert page.json()["data"]["steps"][0]["id"] == "step-1"
    assert captured_page["limit"] == 40
    assert captured_page["cursor"] == "80:message-80"


@pytest.mark.parametrize(
    ("auth_user", "expected_role"),
    [
        ({"roles": ["viewer"]}, "viewer"),
        ({"roles": ["operator"]}, "operator"),
        ({"roles": ["admin"]}, "admin"),
        ({"roles": ["viewer", "operator"]}, "operator"),
        ({"roles": ["viewer", "operator", "admin"]}, "admin"),
        (None, "viewer"),
        (
            {
                "roles": ["operator"],
                "auth_source": "machine",
                "principal_type": "service_account",
                "scopes": ["jobs.read"],
            },
            "operator",
        ),
    ],
)
def test_v2_job_detail_projects_authenticated_role(monkeypatch, auth_user, expected_role):
    import core.entrypoints.api.contracts_v2 as contracts_v2

    captured = {}

    async def detail(**kwargs):
        captured.update(kwargs)
        return {
            "id": kwargs["job_id"],
            "status": "completed",
            "user_role": kwargs["user_role"],
        }

    monkeypatch.setattr(contracts_v2, "build_job_detail_contract", detail)
    client = _build_client(monkeypatch=monkeypatch, auth_user=auth_user)

    response = client.get(f"/api/v2/jobs/{JOB_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["user_role"] == expected_role
    assert isinstance(captured["scope"], ScopeContext)
    assert captured["scope"].account_id == "acc_default"


@pytest.mark.parametrize(
    ("auth_user", "principal", "expected_role"),
    [
        (None, ResolvedPrincipal(roles=["viewer"]), "viewer"),
        (None, ResolvedPrincipal(roles=["admin"]), "admin"),
        (
            None,
            ResolvedPrincipal(
                roles=["operator"],
                auth_source="machine",
                principal_type="service_account",
                scopes=["jobs.read"],
            ),
            "operator",
        ),
        ({"roles": ["viewer"]}, ResolvedPrincipal(roles=["admin"]), "viewer"),
    ],
)
def test_v2_job_detail_projects_typed_principal(monkeypatch, auth_user, principal, expected_role):
    import core.entrypoints.api.contracts_v2 as contracts_v2

    async def detail(**kwargs):
        assert kwargs["scope"].account_id == "acc_default"
        return {"id": kwargs["job_id"], "status": "completed", "user_role": kwargs["user_role"]}

    monkeypatch.setattr(contracts_v2, "build_job_detail_contract", detail)
    client = _build_client(monkeypatch=monkeypatch, auth_user=auth_user, auth_principal=principal)
    response = client.get(f"/api/v2/jobs/{JOB_ID}")
    assert response.status_code == 200
    assert response.json()["data"]["user_role"] == expected_role


def test_v2_projects_passes_workspace_filter_into_scope(monkeypatch):
    class _ProjectQuery(_ProjectQueryService):
        def __init__(self):
            self.workspace_id = None

        def list_projects(self, **kwargs):
            self.workspace_id = kwargs["scope"].workspace_id
            return super().list_projects(**kwargs)

    query_service = _ProjectQuery()
    client = _build_client(monkeypatch=monkeypatch, project_query_service=query_service)

    response = client.get("/api/v2/projects?workspace_id=00000000-0000-0000-0000-000000000099")

    assert response.status_code == 200
    assert query_service.workspace_id == "00000000-0000-0000-0000-000000000099"


def test_v2_projects_forwards_filter_and_sort_parameters(monkeypatch):
    class _ProjectQuery(_ProjectQueryService):
        def __init__(self):
            self.received: dict = {}

        def list_projects(self, **kwargs):
            self.received = dict(kwargs)
            return super().list_projects(**kwargs)

    query_service = _ProjectQuery()
    client = _build_client(monkeypatch=monkeypatch, project_query_service=query_service)

    response = client.get("/api/v2/projects?has_jobs=yes&sort=job_count&direction=desc")

    assert response.status_code == 200
    assert query_service.received["has_jobs"] == "yes"
    assert query_service.received["sort"] == "job_count"
    assert query_service.received["direction"] == "desc"


def test_v2_projects_omits_filter_and_sort_parameters_by_default(monkeypatch):
    class _ProjectQuery(_ProjectQueryService):
        def __init__(self):
            self.received: dict = {}

        def list_projects(self, **kwargs):
            self.received = dict(kwargs)
            return super().list_projects(**kwargs)

    query_service = _ProjectQuery()
    client = _build_client(monkeypatch=monkeypatch, project_query_service=query_service)

    assert client.get("/api/v2/projects").status_code == 200
    assert query_service.received["has_jobs"] == ""
    assert query_service.received["sort"] == ""
    assert query_service.received["direction"] == ""


def test_v2_contracts_service_error_envelope(monkeypatch):
    class _FailingJobQuery(_JobQueryService):
        def list_jobs(self, **kwargs):
            _ = kwargs
            raise ServiceForbiddenError("Forbidden")

    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=_FailingJobQuery())
    response = client.get("/api/v2/jobs")
    assert response.status_code == 403
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["status"] == 403


def test_v2_bulk_delete_requires_admin_role_when_auth_enabled(monkeypatch):
    def _deny(_request, required_roles=None):
        _ = required_roles
        raise PermissionError("denied")

    client = _build_client(
        monkeypatch=monkeypatch,
        require_authenticated_user=_deny,
        local_auth_enabled=True,
    )
    response = client.post(
        f"/api/v2/projects/{PROJECT_ID}/jobs/bulk-delete",
        json={"ids": ["00000000-0000-0000-0000-000000000503"], "confirm": "DELETE"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["ok"] is False
    assert body["kind"] == "mutation"
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["message"] == "Admin role required"


def test_v2_jobs_forwards_exact_workflow_test_run_source_with_existing_scope(monkeypatch):
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)
    response = client.get(
        "/api/v2/jobs?page=1&per_page=8&task_source=workflow_test_run"
        f"&project_id={PROJECT_ID}&workflow_id=inventory-import"
    )
    assert response.status_code == 200
    assert service.last_list_kwargs["task_source"] == "workflow_test_run"
    assert service.last_list_kwargs["workflow_id"] == "inventory-import"
    assert service.last_list_kwargs["project_id"] == PROJECT_ID
    assert service.last_list_kwargs["per_page"] == 8
    assert isinstance(service.last_list_kwargs["scope"], ScopeContext)
    assert str(service.last_list_kwargs["scope"].project_id) == PROJECT_ID


@pytest.mark.parametrize(
    "source", ["step_test_run", "manual", "Workflow_test_run", "workflow_test_run,manual"]
)
def test_v2_jobs_rejects_unsupported_exact_test_run_source(monkeypatch, source):
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)
    response = client.get("/api/v2/jobs", params={"task_source": source})
    assert response.status_code == 422
    assert service.last_list_kwargs == {}


def test_v2_jobs_omitting_test_run_source_keeps_unfiltered_list(monkeypatch):
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)
    assert client.get("/api/v2/jobs").status_code == 200
    assert service.last_list_kwargs.get("task_source") is None
