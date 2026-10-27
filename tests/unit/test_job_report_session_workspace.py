"""Report access honors normalized workspace claims without implicit selection."""

import pytest

from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.resolve import normalize_principal
from core.application.jobs.query_scope import job_row_in_scope
from core.application.management.management_runtime import create_job_query_service
from core.application.management.scope import ScopeContext, scope_from_principal
from tests.unit.test_job_report_download_access import (
    _ACCOUNT_ID,
    _JOB_ID,
    _PROJECT_ID,
    _AccessStore,
    _job,
    _project,
    _router_client,
)

CUSTOM_WORKSPACE = "00000000-0000-4000-8000-000000000303"
FOREIGN_WORKSPACE = "00000000-0000-4000-8000-000000000404"


@pytest.mark.parametrize("marker", [None, "a" * 32])
def test_normal_account_admin_can_download_own_custom_workspace_report(monkeypatch, marker):
    # Real local session lookup supplies no selected workspace. Do not invent an
    # explicit workspace on the caller, skip normalization, or clear scope later.
    principal = normalize_principal(
        {
            "id": "00000000-0000-4000-8000-000000000909",
            "username": "synthetic@example.invalid",
            "account_id": _ACCOUNT_ID,
            "account_ids": [_ACCOUNT_ID],
            "is_active": True,
            "roles": ["admin"],
            "auth_source": "local",
        }
    )
    scope = scope_from_principal(principal.model_dump())
    job = {**_job(), "workspace_id": CUSTOM_WORKSPACE}
    project = {**_project(), "workspace_id": CUSTOM_WORKSPACE}
    assert job["project_id"] == project["id"] == _PROJECT_ID

    async def minimal_detail(**_kwargs):
        # Only expensive rendering is replaced; authorization/normalization/
        # service and canonical route are the real source implementations.
        return {"execution_steps": []}

    monkeypatch.setattr(
        "core.application.reports.job_detail_contract.build_job_detail_contract", minimal_detail
    )
    service = create_job_query_service(
        get_logger=lambda: _AccessStore(job=job, project=project, allow_render=True)
    )
    client = _router_client(service, scope)
    response = client.get(
        "/api/jobs/" + _JOB_ID + "/report", params={"download_token": marker} if marker else {}
    )
    assert response.status_code == 200
    assert response.json()["job"]["id"] == _JOB_ID
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"].startswith("attachment;")


def projected_job():
    return {
        **_job(),
        "workspace_id": CUSTOM_WORKSPACE,
        "selected_workflow": "owned_workflow",
    }


def normal_scope(**claims):
    principal = normalize_principal(
        {
            "id": "00000000-0000-4000-8000-000000000909",
            "username": "synthetic@example.invalid",
            "account_id": _ACCOUNT_ID,
            "roles": ["admin"],
            "auth_source": "local",
            **claims,
        }
    )
    return scope_from_principal(principal.model_dump())


def test_missing_selected_workspace_does_not_fabricate_default_binding():
    assert job_row_in_scope(normal_scope(), projected_job()) is True


def test_typed_principal_without_selection_does_not_restrict_workspace():
    principal = ResolvedPrincipal(account_id=_ACCOUNT_ID, roles=["admin"], auth_source="local")
    assert (
        job_row_in_scope(scope_from_principal(principal.to_scope_dict()), projected_job()) is True
    )


def test_explicit_raw_workspace_selection_survives_and_denies_foreign_row():
    scope = normal_scope(workspace_id=CUSTOM_WORKSPACE)
    assert scope.workspace_id == CUSTOM_WORKSPACE
    assert job_row_in_scope(scope, projected_job()) is True
    assert job_row_in_scope(scope, {**projected_job(), "workspace_id": FOREIGN_WORKSPACE}) is False


def test_explicit_raw_allowed_workspaces_survive_and_deny_foreign_row():
    scope = normal_scope(allowed_workspace_ids=[CUSTOM_WORKSPACE])
    assert scope.allowed_workspace_ids == frozenset({CUSTOM_WORKSPACE})
    assert job_row_in_scope(scope, projected_job()) is True
    assert job_row_in_scope(scope, {**projected_job(), "workspace_id": FOREIGN_WORKSPACE}) is False


@pytest.mark.parametrize(
    "selection",
    [
        {"project_id": "00000000-0000-4000-8000-000000000405"},
        {"workspace_id": FOREIGN_WORKSPACE},
        {"workflow_id": "foreign_workflow"},
        {"allowed_project_ids": frozenset({"00000000-0000-4000-8000-000000000405"})},
        {"allowed_workspace_ids": frozenset({FOREIGN_WORKSPACE})},
        {"allowed_workflow_ids": frozenset({"foreign_workflow"})},
    ],
)
def test_explicit_selected_scope_and_allowlists_still_deny_foreign_id(selection):
    scope = ScopeContext(account_id=_ACCOUNT_ID, **selection)
    assert job_row_in_scope(scope, projected_job()) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "00000000-0000-4000-8000-000000000406"),
        ("tenant_id", "00000000-0000-4000-8000-000000000407"),
        ("account_id", None),
        ("tenant_id", None),
    ],
)
def test_foreign_or_missing_row_account_and_tenant_fail_closed(field, value):
    scope = ScopeContext(account_id=_ACCOUNT_ID)
    assert job_row_in_scope(scope, {**projected_job(), field: value}) is False


@pytest.mark.parametrize(
    "field,restriction",
    [
        ("project_id", {"allowed_project_ids": frozenset({_PROJECT_ID})}),
        ("workspace_id", {"allowed_workspace_ids": frozenset({CUSTOM_WORKSPACE})}),
        ("selected_workflow", {"allowed_workflow_ids": frozenset({"owned_workflow"})}),
    ],
)
@pytest.mark.parametrize("missing", [True, False])
def test_missing_restricted_row_identity_fails_closed(field, restriction, missing):
    row = projected_job()
    if missing:
        row.pop(field)
    else:
        row[field] = None
    assert job_row_in_scope(ScopeContext(account_id=_ACCOUNT_ID, **restriction), row) is False


def test_explicit_workspace_claims_are_normalized_without_default_substitution():
    scope = normal_scope(
        workspace_id="  " + CUSTOM_WORKSPACE + "  ",
        allowed_workspace_ids=["  " + CUSTOM_WORKSPACE + "  ", "", None],
    )
    assert scope.workspace_id == CUSTOM_WORKSPACE
    assert scope.allowed_workspace_ids == frozenset({CUSTOM_WORKSPACE})
    assert job_row_in_scope(scope, projected_job()) is True
