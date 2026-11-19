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
    approval_query_service=None,
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
            approval_query_service=(lambda: approval_query_service)
            if approval_query_service is not None
            else None,
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


@pytest.mark.parametrize(
    "foreign_owner", [False, True], ids=["authorized-display", "foreign-artifact-no-sidecar"]
)
def test_v2_lazy_execution_steps_preserves_post_authorization_presentation_reader(
    monkeypatch, foreign_owner
):
    from copy import deepcopy
    from unittest.mock import MagicMock

    from core.application.extensions.artifact_presentation import ArtifactPresentationResolver
    from core.entrypoints.api import dependencies

    metadata = {
        "artifact_id": "art-fixture",
        "filename": "download.bin",
        "content_type": "application/octet-stream",
        "size_bytes": 21,
        "object_key": "artifacts/art-fixture/body.bin",
        "sha256": "a" * 64,
        "owner_scope": {
            "project_id": "foreign-project" if foreign_owner else PROJECT_ID,
            "job_id": JOB_ID,
        },
    }
    original = deepcopy(metadata)
    claim = {
        "artifact_id": "art-fixture",
        "binding_key": "download",
        "filename": "orders.csv",
        "content_type": "text/csv",
        "size_bytes": 21,
        "sha256": "a" * 64,
    }
    plane = SimpleNamespace(
        get_artifact=MagicMock(return_value=metadata),
        read_artifact_presentation=MagicMock(return_value=claim),
    )
    monkeypatch.setattr(dependencies, "_resolve_artifact", ArtifactPresentationResolver(plane))

    class Service(_JobQueryService):
        async def get_job_status(self, job_id, *, scope, **kwargs):
            return _Payload({"id": job_id, "project_id": PROJECT_ID, "status": "completed"})

        async def list_messages(self, job_id, **kwargs):
            return _Payload(
                {
                    "messages": [
                        {
                            "id": "m-download",
                            "step_id": "download",
                            "from_agent": "connector_step",
                            "status": "pass",
                            "artifacts": {
                                "step_kind": "connector",
                                "output_payload": {"file": "artifact:art-fixture"},
                            },
                        }
                    ],
                    "has_more": False,
                }
            )

    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=Service())
    response = client.get(f"/api/v2/jobs/{JOB_ID}/execution-steps?limit=50")
    assert response.status_code == 200
    files = response.json()["data"]["steps"][0]["output_artifacts"]
    if foreign_owner:
        assert files == []
        plane.read_artifact_presentation.assert_not_called()
    else:
        assert files == [
            {
                "artifact_id": "art-fixture",
                "role": "output",
                "filename": "orders.csv",
                "content_type": "text/csv",
                "size_bytes": 21,
                "preview_too_large": False,
                "available": True,
                "download_url": f"/api/jobs/{JOB_ID}/artifacts/art-fixture",
            }
        ]
        assert plane.read_artifact_presentation.call_count >= 1
        assert all(
            call.args[0]["owner_scope"] == metadata["owner_scope"]
            for call in plane.read_artifact_presentation.call_args_list
        )
    assert metadata == original


def test_v2_lazy_execution_steps_denied_job_never_reads_artifact_or_presentation(monkeypatch):
    from unittest.mock import MagicMock

    from core.application.extensions.artifact_presentation import ArtifactPresentationResolver
    from core.entrypoints.api import dependencies

    plane = SimpleNamespace(get_artifact=MagicMock(), read_artifact_presentation=MagicMock())
    monkeypatch.setattr(dependencies, "_resolve_artifact", ArtifactPresentationResolver(plane))

    class DeniedService(_JobQueryService):
        async def get_job_status(self, job_id, *, scope, **kwargs):
            raise ServiceForbiddenError("Job is not available in this scope")

    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=DeniedService())
    response = client.get(f"/api/v2/jobs/{JOB_ID}/execution-steps?limit=50")
    assert response.status_code == 403
    plane.get_artifact.assert_not_called()
    plane.read_artifact_presentation.assert_not_called()


def test_v2_jobs_forwards_date_range_with_existing_filters(monkeypatch):
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)
    response = client.get("/api/v2/jobs?from_date=2026-03-01&to_date=2026-03-01&workflow_id=wf")
    assert response.status_code == 200
    assert service.last_list_kwargs["from_date"] == "2026-03-01"
    assert service.last_list_kwargs["to_date"] == "2026-03-01"
    assert service.last_list_kwargs["workflow_id"] == "wf"


@pytest.mark.parametrize(
    "from_date,to_date",
    [("2026-03-10", "2026-03-05"), ("not-a-date", "2026-03-05"), ("2026-03-01", "9999-12-31")],
)
def test_v2_job_date_validation_returns_bad_request_without_querying(
    monkeypatch, from_date, to_date
):
    from unittest.mock import AsyncMock, MagicMock

    from core.application.management import JobQueryService

    store = MagicMock()
    service = JobQueryService(
        get_logger=lambda: store,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "failed",
        build_runtime_payload_for_api=AsyncMock(return_value={}),
    )
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)
    response = client.get("/api/v2/jobs", params={"from_date": from_date, "to_date": to_date})
    assert response.status_code == 400
    assert response.json()["ok"] is False
    store.list_jobs_for_attention.assert_not_called()


@pytest.mark.parametrize("source", ["manual_ui", "mcp_workflow"])
def test_v2_jobs_filters_ui_and_api_runs(monkeypatch, source):
    """ADR 0075: the history filters by how a run started."""
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)

    response = client.get("/api/v2/jobs", params={"task_source": source})

    assert response.status_code == 200
    assert service.last_list_kwargs["task_source"] == source


def test_v2_jobs_forwards_an_operator_list_request(monkeypatch):
    """ADR 0075: the operator list of one workflow in one project, with its filters."""
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)

    response = client.get(
        "/api/v2/jobs",
        params={
            "project_id": PROJECT_ID,
            "workflow_id": "wf.receipts",
            "operator_view": "1",
            "filters": '{"total": {"min": 10}}',
        },
    )

    assert response.status_code == 200
    assert service.last_list_kwargs["operator_view"] is True
    assert service.last_list_kwargs["filters"] == '{"total": {"min": 10}}'
    assert service.last_list_kwargs["workflow_id"] == "wf.receipts"
    assert service.last_list_kwargs["project_id"] == PROJECT_ID


def test_v2_jobs_returns_422_for_an_unknown_operator_filter(monkeypatch):
    from core.application.management.errors import ServiceValidationError

    class _Refusing(_JobQueryService):
        def list_jobs(self, **kwargs):
            raise ServiceValidationError(
                "'ghost' is not a filter of this list",
                status_code=422,
                payload={"error": "'ghost' is not a filter of this list", "code": "unknown_filter"},
            )

    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=_Refusing())

    response = client.get(
        "/api/v2/jobs",
        params={
            "project_id": PROJECT_ID,
            "workflow_id": "wf",
            "operator_view": "1",
            "filters": '{"ghost": 1}',
        },
    )

    assert response.status_code == 422
    assert response.json()["ok"] is False


class _SectionsQueryService:
    def __init__(self):
        self.calls: list[tuple] = []

    def list_operator_sections(self, *, scope):
        self.calls.append(("list", scope.account_id))
        return [{"project_id": "p1", "workflow_id": "wf", "title": "Contracts"}]

    def get_operator_section(self, project_id, workflow_id, *, scope):
        from core.application.management.errors import ServiceNotFoundError

        self.calls.append(("detail", project_id, workflow_id, scope.project_id))
        if workflow_id == "missing":
            raise ServiceNotFoundError("Section not found")
        return {"project_id": project_id, "workflow_id": workflow_id, "operator": {}}


def test_v2_operator_sections_menu_and_detail(monkeypatch):
    """ADR 0075: the menu of (project, workflow) pairs and one section's settings."""
    service = _SectionsQueryService()
    client = _build_client(monkeypatch=monkeypatch, project_query_service=service)

    menu = client.get("/api/v2/projects/sections")
    detail = client.get("/api/v2/projects/p1/workflows/wf/section")
    missing = client.get("/api/v2/projects/p1/workflows/missing/section")

    assert menu.status_code == 200
    assert menu.json()["data"] == {
        "sections": [{"project_id": "p1", "workflow_id": "wf", "title": "Contracts"}]
    }
    assert menu.json()["meta"]["resource"] == "operator-sections"
    assert detail.status_code == 200
    assert detail.json()["data"]["workflow_id"] == "wf"
    assert missing.status_code == 404
    assert service.calls[1] == ("detail", "p1", "wf", "p1")


def test_v2_jobs_around_passes_the_job_whose_neighbours_are_asked(monkeypatch):
    service = _JobQueryService()
    client = _build_client(monkeypatch=monkeypatch, jobs_query_service=service)

    response = client.get(
        "/api/v2/jobs",
        params={
            "project_id": PROJECT_ID,
            "workflow_id": "wf",
            "operator_view": "1",
            "around": "j7",
        },
    )

    assert response.status_code == 200
    assert service.last_list_kwargs["around"] == "j7"
    assert service.last_list_kwargs["operator_view"] is True


class _ReviewQuery:
    def __init__(self):
        self.calls: list[dict] = []

    def get_review(self, job_id, *, scope, can_decide):
        self.calls.append({"job_id": job_id, "scope": scope, "can_decide": can_decide})
        return _Payload({"job_id": job_id, "waiting": True, "access": "none", "gate": None})


@pytest.mark.parametrize(
    ("roles", "can_decide"),
    [(["operator"], True), (["admin"], True), (["viewer"], False)],
)
def test_v2_job_review_decides_only_for_operators_and_admins(monkeypatch, roles, can_decide):
    review = _ReviewQuery()
    client = _build_client(
        monkeypatch=monkeypatch,
        approval_query_service=review,
        auth_user={"username": "person", "roles": roles},
    )

    response = client.get("/api/v2/jobs/job-a/review")

    assert response.status_code == 200
    assert response.json()["data"]["access"] == "none"
    assert review.calls[0]["job_id"] == "job-a"
    assert review.calls[0]["can_decide"] is can_decide
