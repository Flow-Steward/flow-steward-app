"""ADR 0076 section 8a: workspace, project and workflow grants of a limited person.

The effective role in a place is the most specific grant covering it (workflow, then
project, then workspace), capped by the global role. A workspace grant covers the
projects the workspace has when the limit is read, so a later project is covered. A
project reached only through workflow grants shows only those workflows and their
runs, and holds read access itself. Every other workspace is out of reach.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.application.auth.permissions import (
    OPERATOR_PERMISSIONS,
    SYSTEM_ROLE_PERMISSIONS,
    VIEWER_PERMISSIONS,
)
from core.application.auth.project_scope_limit import (
    NO_PROJECT_ID,
    PROJECT_OUT_OF_REACH_MESSAGE,
    PROJECT_ROLE_REFUSAL_MESSAGE,
    place_permissions,
    principal_places_payload,
    scope_limit_route_refusal,
    scoped_places,
    workflow_id_from_project_path,
)
from core.application.auth.scoped_assignments import ScopeLimit, load_scope_limit
from core.application.jobs.query_scope import job_row_in_scope, job_scope_conditions
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import (
    require_project_access,
    require_project_permission,
    require_workflow_permission,
    require_workspace_access,
    workflow_in_scope_limit,
)
from core.application.management.scope import ScopeContext, scope_from_principal
from core.application.reports.dashboard_metrics_helpers import _dashboard_rows_in_projects
from core.application.search.global_search_service import _filter_item_for_scope
from core.entrypoints.api.app_bootstrap import _user_summary
from tests.unit.test_scoped_roles_authorization import _person, run_middleware

ACCOUNT = "00000000-0000-0000-0000-000000000002"
#: Accounting holds Ledger and Payroll (and later Audit); Marketing holds Campaigns;
#: Sales holds Leads and is never granted.
ACCOUNTING = "aaaaaaaa-0000-4000-8000-00000000000a"
MARKETING = "bbbbbbbb-0000-4000-8000-00000000000b"
SALES = "cccccccc-0000-4000-8000-00000000000c"
LEDGER = "11111111-0000-4000-8000-000000000001"
PAYROLL = "22222222-0000-4000-8000-000000000002"
AUDIT = "44444444-0000-4000-8000-000000000004"
CAMPAIGNS = "33333333-0000-4000-8000-000000000003"
LEADS = "55555555-0000-4000-8000-000000000005"
INVOICE = "wf-invoice"
REPORT = "wf-report"
PAYSLIP = "wf-payslip"

WORKSPACES = {
    LEDGER: ACCOUNTING,
    PAYROLL: ACCOUNTING,
    CAMPAIGNS: MARKETING,
    LEADS: SALES,
}


def _limit(**kwargs: Any) -> ScopeLimit:
    return ScopeLimit(**kwargs)


#: Viewer in the Accounting workspace, operator in Payroll, viewer on the Invoice
#: workflow of Campaigns, operator on Payslip in Payroll (more specific than Payroll).
MIXED = _limit(
    workspace_roles={ACCOUNTING: frozenset({"viewer"})},
    project_roles={PAYROLL: frozenset({"operator"})},
    workflow_roles={
        CAMPAIGNS: {INVOICE: frozenset({"viewer"})},
        PAYROLL: {PAYSLIP: frozenset({"viewer"})},
    },
    project_workspaces={LEDGER: ACCOUNTING, PAYROLL: ACCOUNTING, CAMPAIGNS: MARKETING},
)


# --- precedence --------------------------------------------------------------------------


def test_workspace_viewer_and_project_operator_the_project_grant_wins() -> None:
    assert MIXED.project_level_roles(PAYROLL) == {"operator"}
    assert MIXED.project_level_roles(LEDGER) == {"viewer"}


def test_a_workflow_grant_is_more_specific_than_its_project() -> None:
    assert MIXED.workflow_level_roles(PAYROLL, PAYSLIP) == {"viewer"}
    assert MIXED.workflow_level_roles(PAYROLL, "wf-other") == {"operator"}
    assert MIXED.workflow_level_roles(LEDGER, "wf-any") == {"viewer"}


def test_a_workflow_only_grant_shows_the_project_and_only_that_workflow() -> None:
    assert CAMPAIGNS in MIXED.project_ids
    assert MIXED.project_level_roles(CAMPAIGNS) is None
    assert MIXED.workflow_only_projects() == {CAMPAIGNS: frozenset({INVOICE})}
    assert MIXED.workflow_level_roles(CAMPAIGNS, INVOICE) == {"viewer"}
    assert MIXED.workflow_level_roles(CAMPAIGNS, REPORT) is None


def test_a_workflow_grant_inside_a_covered_project_restricts_nothing() -> None:
    # Payroll is covered by its own grant: its other workflows stay reachable.
    assert PAYROLL not in MIXED.workflow_only_projects()


def test_a_project_created_later_in_a_granted_workspace_is_covered() -> None:
    later = _limit(
        workspace_roles={ACCOUNTING: frozenset({"operator"})},
        project_workspaces={LEDGER: ACCOUNTING, PAYROLL: ACCOUNTING, AUDIT: ACCOUNTING},
    )

    assert later.project_ids == {LEDGER, PAYROLL, AUDIT}
    assert later.project_level_roles(AUDIT) == {"operator"}


def test_another_workspace_is_invisible() -> None:
    assert SALES not in MIXED.workspace_ids
    assert LEADS not in MIXED.project_ids
    assert MIXED.workspace_ids == {ACCOUNTING, MARKETING}


# --- permissions per place ----------------------------------------------------------------


def _places(limit: ScopeLimit = MIXED, granted=OPERATOR_PERMISSIONS, **kwargs: Any):
    return scoped_places(granted, limit, SYSTEM_ROLE_PERMISSIONS, **kwargs)


def test_places_carry_the_most_specific_role_capped_by_the_global_role() -> None:
    places = _places()

    assert places.allowed_project_ids == sorted([LEDGER, PAYROLL, CAMPAIGNS])
    assert places.allowed_workspace_ids == sorted([ACCOUNTING, MARKETING])
    assert places.project_permissions == {
        LEDGER: VIEWER_PERMISSIONS,
        PAYROLL: OPERATOR_PERMISSIONS,
        # Reached only through a workflow grant: shown, not acted on.
        CAMPAIGNS: VIEWER_PERMISSIONS,
    }
    assert places.workspace_permissions == {
        ACCOUNTING: VIEWER_PERMISSIONS,
        MARKETING: VIEWER_PERMISSIONS,
    }
    assert places.workflow_permissions == {
        CAMPAIGNS: {INVOICE: VIEWER_PERMISSIONS},
        PAYROLL: {PAYSLIP: VIEWER_PERMISSIONS},
    }
    assert places.project_workflow_ids == {CAMPAIGNS: frozenset({INVOICE})}


def test_a_global_viewer_scoped_as_operator_in_a_workspace_stays_a_viewer() -> None:
    limit = _limit(
        workspace_roles={ACCOUNTING: frozenset({"operator"})},
        project_workspaces={LEDGER: ACCOUNTING},
    )

    places = _places(limit, granted=VIEWER_PERMISSIONS)

    assert places.project_permissions == {LEDGER: VIEWER_PERMISSIONS}
    assert places.workspace_permissions == {ACCOUNTING: VIEWER_PERMISSIONS}


def test_a_token_reaches_only_the_places_it_and_its_owner_both_reach() -> None:
    places = _places(token_project_ids=[LEDGER, LEADS])

    assert places.allowed_project_ids == [LEDGER]
    assert set(places.project_permissions) == {LEDGER}
    assert places.project_workflow_ids == {}

    by_workspace = _places(token_workspace_ids=[MARKETING])
    assert by_workspace.allowed_workspace_ids == [MARKETING]
    assert by_workspace.allowed_project_ids == [CAMPAIGNS]


def test_a_limit_without_grants_reaches_nothing() -> None:
    places = _places(ScopeLimit())

    assert places.allowed_project_ids == [NO_PROJECT_ID]
    assert places.allowed_workspace_ids == [NO_PROJECT_ID]
    assert places.project_permissions == {}


@pytest.mark.parametrize(
    ("project", "workflow", "expected"),
    [
        (PAYROLL, None, OPERATOR_PERMISSIONS),
        (PAYROLL, PAYSLIP, VIEWER_PERMISSIONS),
        (PAYROLL, "wf-other", OPERATOR_PERMISSIONS),
        (CAMPAIGNS, INVOICE, VIEWER_PERMISSIONS),
        (CAMPAIGNS, REPORT, None),
        (LEADS, None, None),
    ],
)
def test_place_permissions(project, workflow, expected) -> None:
    places = _places()

    assert (
        place_permissions(
            project,
            workflow,
            places.project_permissions,
            places.workflow_permissions,
            places.project_workflow_ids,
        )
        == expected
    )


# --- the middleware --------------------------------------------------------------------


def _mixed(_bl, _account_id, _user_id):
    return MIXED


def test_the_middleware_puts_every_place_on_the_request() -> None:
    status, state = run_middleware("/api/jobs", "GET", _person(), loader=_mixed)

    assert status == 204
    assert state.auth_user["allowed_workspace_ids"] == sorted([ACCOUNTING, MARKETING])
    assert state.auth_principal.allowed_workspace_ids == sorted([ACCOUNTING, MARKETING])
    assert state.auth_user["project_workflow_ids"] == {CAMPAIGNS: [INVOICE]}
    assert state.auth_user["workflow_permissions"][PAYROLL] == {PAYSLIP: sorted(VIEWER_PERMISSIONS)}
    scope = scope_from_principal(state.auth_user)
    assert scope.allowed_workspace_ids == frozenset({ACCOUNTING, MARKETING})
    assert scope.project_workflow_ids == {CAMPAIGNS: frozenset({INVOICE})}
    assert scope.workflow_permissions == {
        CAMPAIGNS: {INVOICE: VIEWER_PERMISSIONS},
        PAYROLL: {PAYSLIP: VIEWER_PERMISSIONS},
    }
    assert scope.workspace_permissions == {
        ACCOUNTING: VIEWER_PERMISSIONS,
        MARKETING: VIEWER_PERMISSIONS,
    }


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        (f"/api/projects/{LEDGER}", "GET", 204),
        (f"/api/projects/{CAMPAIGNS}", "GET", 204),
        (f"/api/projects/{LEADS}", "GET", 403),
        (f"/api/projects/{PAYROLL}/workflow-preview", "POST", 204),
        (f"/api/projects/{LEDGER}/workflow-preview", "POST", 403),
        (f"/api/projects/{CAMPAIGNS}/workflow-preview", "POST", 403),
        (f"/api/projects/{CAMPAIGNS}/workflows/{INVOICE}/contract", "GET", 204),
        (f"/api/projects/{CAMPAIGNS}/workflows/{REPORT}/contract", "GET", 403),
    ],
)
def test_the_middleware_checks_each_place(path, method, expected) -> None:
    status, _state = run_middleware(path, method, _person(), loader=_mixed)

    assert status == expected


def test_a_workflow_path_is_checked_against_its_workflow() -> None:
    places = _places()
    args = (places.project_permissions, places.workflow_permissions, places.project_workflow_ids)

    assert workflow_id_from_project_path(f"/api/projects/{CAMPAIGNS}/workflows/{REPORT}/x") == (
        REPORT
    )
    assert (
        scope_limit_route_refusal(
            "GET", f"/api/projects/{CAMPAIGNS}/workflows/{REPORT}/contract", None, *args
        )
        == PROJECT_OUT_OF_REACH_MESSAGE
    )
    assert (
        scope_limit_route_refusal(
            "POST", f"/api/projects/{CAMPAIGNS}/workflows/{INVOICE}/x", "workflows.run", *args
        )
        == PROJECT_ROLE_REFUSAL_MESSAGE
    )


# --- guards -------------------------------------------------------------------------------


def _scope() -> ScopeContext:
    places = _places()
    return scope_from_principal(
        {
            "id": "6a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "account_id": ACCOUNT,
            "roles": ["operator"],
            "auth_source": "local",
            "permissions": sorted(OPERATOR_PERMISSIONS),
            **principal_places_payload(places),
        }
    )


def _project(project_id: str) -> dict[str, str]:
    return {"id": project_id, "account_id": ACCOUNT, "workspace_id": WORKSPACES[project_id]}


def test_require_project_permission_inherits_from_the_workspace() -> None:
    scope = _scope()

    require_project_permission(scope, _project(LEDGER), "jobs.read")
    with pytest.raises(ServiceForbiddenError, match="role in this project"):
        require_project_permission(scope, _project(LEDGER), "workflows.run")
    require_project_permission(scope, _project(PAYROLL), "workflows.run")
    with pytest.raises(ServiceForbiddenError, match="Access denied"):
        require_project_permission(scope, _project(LEADS), "jobs.read")


@pytest.mark.parametrize(
    ("project", "workflow", "permission", "allowed"),
    [
        (PAYROLL, "wf-other", "workflows.run", True),
        (PAYROLL, PAYSLIP, "workflows.run", False),
        (PAYROLL, PAYSLIP, "jobs.read", True),
        (CAMPAIGNS, INVOICE, "jobs.read", True),
        (CAMPAIGNS, INVOICE, "workflows.run", False),
        (CAMPAIGNS, REPORT, "jobs.read", False),
        (LEDGER, INVOICE, "workflows.run", False),
    ],
)
def test_require_workflow_permission_workflow_then_project_then_workspace(
    project, workflow, permission, allowed
) -> None:
    scope = _scope()

    if allowed:
        require_workflow_permission(scope, _project(project), workflow, permission)
    else:
        with pytest.raises(ServiceForbiddenError):
            require_workflow_permission(scope, _project(project), workflow, permission)


def test_require_workspace_access() -> None:
    scope = _scope()

    require_workspace_access(scope, ACCOUNTING, "projects.read")
    require_workspace_access(scope, MARKETING)
    with pytest.raises(ServiceForbiddenError):
        require_workspace_access(scope, SALES)
    with pytest.raises(ServiceForbiddenError):
        require_workspace_access(scope, MARKETING, "workflows.run")


def test_a_project_of_an_invisible_workspace_is_refused_even_by_id() -> None:
    scope = _scope()

    with pytest.raises(ServiceForbiddenError):
        require_project_access(scope, _project(LEADS))


def test_an_unlimited_person_is_not_restricted_by_the_new_guards() -> None:
    scope = scope_from_principal({"id": "u", "account_id": ACCOUNT, "roles": ["operator"]})

    assert workflow_in_scope_limit(scope, CAMPAIGNS, REPORT) is True
    require_workspace_access(scope, SALES, "projects.write")
    require_workflow_permission(scope, _project(LEADS), REPORT, "workflows.run")


def test_workflow_in_scope_limit() -> None:
    scope = _scope()

    assert workflow_in_scope_limit(scope, CAMPAIGNS, INVOICE) is True
    assert workflow_in_scope_limit(scope, CAMPAIGNS, REPORT) is False
    assert workflow_in_scope_limit(scope, CAMPAIGNS, None) is False
    assert workflow_in_scope_limit(scope, PAYROLL, "wf-anything") is True


# --- lists: runs, dashboard, search -------------------------------------------------------


def _job(project: str, workflow: str) -> dict[str, str]:
    return {
        "account_id": ACCOUNT,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "project_id": project,
        "workspace_id": WORKSPACES[project],
        "selected_workflow": workflow,
    }


@pytest.mark.parametrize(
    ("project", "workflow", "visible"),
    [
        (LEDGER, REPORT, True),
        (PAYROLL, PAYSLIP, True),
        (CAMPAIGNS, INVOICE, True),
        (CAMPAIGNS, REPORT, False),
        (LEADS, INVOICE, False),
    ],
)
def test_run_rows_follow_the_places(project, workflow, visible) -> None:
    assert job_row_in_scope(_scope(), _job(project, workflow)) is visible


def test_run_queries_carry_the_workflow_only_predicate() -> None:
    conditions, params = job_scope_conditions(_scope())

    predicate = next(item for item in conditions if "selected_workflow_key" in item)
    assert predicate.startswith("(NOT (j.project_id::text = ANY(")
    assert [CAMPAIGNS] in params
    assert [f"{CAMPAIGNS}|{INVOICE}"] in params
    assert "p.workspace_id = ANY(%s)" in conditions


def test_an_unlimited_scope_adds_no_workflow_predicate() -> None:
    scope = scope_from_principal({"id": "u", "account_id": ACCOUNT, "roles": ["operator"]})

    conditions, _params = job_scope_conditions(scope)

    assert not any("selected_workflow_key" in item for item in conditions)


def test_the_dashboard_keeps_only_reachable_runs() -> None:
    scope = _scope()
    rows = [_job(p, w) for p, w in ((LEDGER, REPORT), (CAMPAIGNS, INVOICE), (CAMPAIGNS, REPORT))]

    kept = _dashboard_rows_in_projects(rows, scope.allowed_project_ids, scope.project_workflow_ids)

    assert [(row["project_id"], row["selected_workflow"]) for row in kept] == [
        (LEDGER, REPORT),
        (CAMPAIGNS, INVOICE),
    ]


@pytest.mark.parametrize(
    ("item", "visible"),
    [
        ({"group": "projects", "project_id": CAMPAIGNS}, True),
        ({"group": "jobs", "project_id": CAMPAIGNS, "meta": {"selected_workflow": INVOICE}}, True),
        ({"group": "jobs", "project_id": CAMPAIGNS, "meta": {"selected_workflow": REPORT}}, False),
        ({"group": "workflows", "project_id": CAMPAIGNS, "workflow_id": REPORT}, False),
        ({"group": "workflows", "project_id": LEDGER, "workflow_id": REPORT}, True),
        ({"group": "jobs", "project_id": LEADS, "meta": {"selected_workflow": INVOICE}}, False),
    ],
)
def test_search_results_follow_the_places(item, visible) -> None:
    scope = _scope()

    assert (
        _filter_item_for_scope(
            item, scope=scope, project_id_set=scope.allowed_project_ids, selected_project_id=None
        )
        is visible
    )


# --- bootstrap ----------------------------------------------------------------------------


def test_the_bootstrap_sends_every_place_permission() -> None:
    places = _places()
    summary = _user_summary(
        {"id": "u-1", "roles": ["operator"], **principal_places_payload(places)}
    )

    assert summary["workspace_permissions"][ACCOUNTING] == sorted(VIEWER_PERMISSIONS)
    assert summary["workflow_permissions"] == {
        CAMPAIGNS: {INVOICE: sorted(VIEWER_PERMISSIONS)},
        PAYROLL: {PAYSLIP: sorted(VIEWER_PERMISSIONS)},
    }
    assert summary["project_workflow_ids"] == {CAMPAIGNS: [INVOICE]}
    assert summary["project_permissions"][PAYROLL] == sorted(OPERATOR_PERMISSIONS)


def test_the_bootstrap_sends_no_place_maps_without_a_limit() -> None:
    summary = _user_summary({"id": "u-1", "roles": ["operator"]})

    assert summary["workspace_permissions"] is None
    assert summary["workflow_permissions"] is None
    assert summary["project_workflow_ids"] is None


# --- the loader ---------------------------------------------------------------------------


class _LoaderConn:
    """The limit row, one grant per scope, and the granted workspace's projects."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> Any:
        conn = self

        class _Cursor:
            description = None

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def execute(self, sql: str, params: Any = None) -> None:
                text = " ".join(sql.split())
                conn.statements.append((text, params))
                if "FROM public.scoped_role_users" in text:
                    self._rows = [{"present": 1}]
                elif "FROM public.user_roles ur" in text:
                    self._rows = [
                        {"user_id": "u", "workspace_id": ACCOUNTING, "role_key": "viewer"},
                        {
                            "user_id": "u",
                            "project_id": PAYROLL,
                            "project_workspace_id": ACCOUNTING,
                            "role_key": "operator",
                        },
                        {
                            "user_id": "u",
                            "project_id": CAMPAIGNS,
                            "workflow_id": INVOICE,
                            "project_workspace_id": MARKETING,
                            "role_key": "viewer",
                        },
                    ]
                else:
                    self._rows = [
                        {"project_id": LEDGER, "workspace_id": ACCOUNTING},
                        {"project_id": PAYROLL, "workspace_id": ACCOUNTING},
                        {"project_id": AUDIT, "workspace_id": ACCOUNTING},
                    ]

            def fetchall(self) -> list[dict[str, Any]]:
                return list(self._rows)

        return _Cursor()


def test_the_loader_reads_all_three_scopes_and_the_workspace_projects() -> None:
    conn = _LoaderConn()

    limit = load_scope_limit(conn, account_id=ACCOUNT, user_id="u")

    assert limit is not None
    assert limit.workspace_roles == {ACCOUNTING: {"viewer"}}
    assert limit.project_roles == {PAYROLL: {"operator"}}
    assert limit.workflow_roles == {CAMPAIGNS: {INVOICE: {"viewer"}}}
    assert limit.project_ids == {LEDGER, PAYROLL, AUDIT, CAMPAIGNS}
    # Every query is bound to the account it was set in.
    assert all(ACCOUNT in str(params) for _sql, params in conn.statements)


# --- list services ------------------------------------------------------------------------


def _job_service(bl: Any) -> Any:
    from unittest.mock import AsyncMock

    from core.application.management.query_services import JobQueryService

    return JobQueryService(
        get_logger=lambda: bl,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: "running",
        build_runtime_payload_for_api=AsyncMock(return_value={}),
        build_display_summary=lambda value: value,
    )


def _project_store(project_id: str) -> Any:
    from unittest.mock import MagicMock

    bl = MagicMock()
    bl.get_project.return_value = _project(project_id)
    bl.get_jobs.return_value = []
    bl.count_jobs.return_value = 0
    bl.get_project_workflows.return_value = [
        {"workflow_id": INVOICE, "enabled": True, "is_default": False},
        {"workflow_id": REPORT, "enabled": True, "is_default": True},
    ]
    return bl


def test_a_workflow_only_projects_run_list_asks_only_for_its_workflows() -> None:
    bl = _project_store(CAMPAIGNS)

    _job_service(bl).list_project_jobs(CAMPAIGNS, scope=_scope())

    assert bl.get_jobs.call_args.kwargs["workflow_ids"] == [INVOICE]
    assert bl.count_jobs.call_args.kwargs["workflow_ids"] == [INVOICE]


def test_a_covered_projects_run_list_is_not_cut_to_workflows() -> None:
    bl = _project_store(PAYROLL)

    _job_service(bl).list_project_jobs(PAYROLL, scope=_scope())

    assert "workflow_ids" not in bl.get_jobs.call_args.kwargs


def test_a_run_list_of_an_invisible_workspaces_project_is_refused() -> None:
    bl = _project_store(LEADS)

    with pytest.raises(ServiceForbiddenError):
        _job_service(bl).list_project_jobs(LEADS, scope=_scope())


def test_a_workflow_only_project_lists_only_its_granted_workflows() -> None:
    from core.application.management.query_services import ProjectQueryService

    bl = _project_store(CAMPAIGNS)
    service = ProjectQueryService(
        get_logger=lambda: bl,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda _project_id: 0,
        get_ready_queue_depth=lambda: 0,
    )

    payload = service.get_workflow_bindings(CAMPAIGNS, scope=_scope()).dump()

    assert [row["workflow_id"] for row in payload["project_workflows"]] == [INVOICE]
    assert payload["workflow_ids"] == [INVOICE]
    assert payload["operator_default_workflow_id"] == ""
