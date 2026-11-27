"""Follow-ups of the ADR 0076 section 8b review that the first fix left open.

Each test fails on the code before its fix:

1. a schedule's change, pause, deletion and "run now" are checked in the schedule's
   own workflow (workflow > project > workspace), not only in its project;
2. in a project reached only through workflow grants, project-level listings keep
   only what a granted workflow needs: connections (list, detail, secret approval
   status, provider inventory), extension project state; direct external effects
   (which belong to no workflow) are out of reach.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.auth.permissions import OPERATOR_PERMISSIONS, VIEWER_PERMISSIONS
from core.application.management.command_models import (
    ProjectScheduledJobToggleInput,
    ProjectScheduledJobUpdateInput,
)
from core.application.management.command_services import (
    ProjectCommandService,
    ProjectConnectionService,
)
from core.application.management.command_services_project_run_requests_mixin import (
    require_schedule_workflow_permission,
)
from core.application.management.errors import ServiceForbiddenError, ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.application.projects.workflow_only_connections import (
    connections_for_scope,
    require_install_needed,
    workflow_connection_needs,
)
from tests.unit.test_multilevel_scoped_roles import (
    ACCOUNT,
    ACCOUNTING,
    CAMPAIGNS,
    INVOICE,
    MARKETING,
    PAYROLL,
    PAYSLIP,
    REPORT,
    WORKSPACES,
)

#: A custom role that may change project configuration, on top of the operator's work.
_CONFIGURE = OPERATOR_PERMISSIONS | {"projects.write"}


def _limited_scope() -> ScopeContext:
    """Payroll: may configure, but only views Payslip. Campaigns: only Invoice, configure it."""
    return ScopeContext(
        account_id=ACCOUNT,
        actor_id="person",
        principal_type="operator",
        allowed_project_ids=frozenset({PAYROLL, CAMPAIGNS}),
        allowed_workspace_ids=frozenset({ACCOUNTING, MARKETING}),
        project_permissions={PAYROLL: _CONFIGURE, CAMPAIGNS: VIEWER_PERMISSIONS},
        workspace_permissions={ACCOUNTING: VIEWER_PERMISSIONS, MARKETING: VIEWER_PERMISSIONS},
        workflow_permissions={
            PAYROLL: {PAYSLIP: VIEWER_PERMISSIONS},
            CAMPAIGNS: {INVOICE: _CONFIGURE},
        },
        project_workflow_ids={CAMPAIGNS: frozenset({INVOICE})},
    )


def _project(project_id: str) -> dict[str, Any]:
    return {
        "id": project_id,
        "account_id": ACCOUNT,
        "workspace_id": WORKSPACES[project_id],
        "name": "demo",
    }


# --- 1. schedules are checked in their own workflow ------------------------------------


@pytest.mark.parametrize(
    ("project", "workflow", "outcome"),
    [
        (PAYROLL, "wf-other", None),
        (PAYROLL, PAYSLIP, ServiceForbiddenError),
        (CAMPAIGNS, INVOICE, None),
        (CAMPAIGNS, REPORT, ServiceNotFoundError),
        (CAMPAIGNS, "", ServiceNotFoundError),
    ],
)
def test_a_schedule_is_checked_in_its_workflow(project, workflow, outcome) -> None:
    schedule = {"workflow_id": workflow}
    if outcome is None:
        require_schedule_workflow_permission(_limited_scope(), _project(project), schedule)
    else:
        with pytest.raises(outcome):
            require_schedule_workflow_permission(_limited_scope(), _project(project), schedule)


def test_an_unlimited_person_keeps_every_schedule() -> None:
    require_schedule_workflow_permission(
        ScopeContext(account_id=ACCOUNT), _project(PAYROLL), {"workflow_id": PAYSLIP}
    )


def _schedule_store(project: str, workflow: str) -> MagicMock:
    bl = MagicMock()
    bl.get_project.return_value = _project(project)
    bl.get_project_run_config.return_value = {"project_name": "demo"}
    bl.get_scheduled_job.return_value = {
        "id": 9,
        "account_id": ACCOUNT,
        "project_id": project,
        "workflow_id": workflow,
        "enabled": True,
        "cron_expr": "0 9 * * *",
        "inputs": {},
    }
    return bl


def _schedule_service(bl: MagicMock, run_once: MagicMock | None = None) -> ProjectCommandService:
    return ProjectCommandService(
        get_logger=lambda: bl,
        resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        validate_explicit_workflow_override=MagicMock(),
        cron_next_run_at=lambda expr, ref, **kwargs: datetime(2026, 11, 28, 9, 0, tzinfo=UTC),
        form_flag=lambda value: str(value or "").strip().lower() in {"1", "true", "on"},
        run_scheduled_job_once=run_once,
    )


def test_pausing_a_schedule_needs_the_permission_in_its_workflow() -> None:
    # The project lets the person configure; the Payslip grant (viewer) is more specific.
    bl = _schedule_store(PAYROLL, PAYSLIP)

    with pytest.raises(ServiceForbiddenError):
        _schedule_service(bl).toggle_scheduled_job(
            PAYROLL, "9", ProjectScheduledJobToggleInput(enabled="0"), scope=_limited_scope()
        )
    bl.update_scheduled_job.assert_not_called()


def test_pausing_a_schedule_of_another_workflow_of_the_project_is_allowed() -> None:
    bl = _schedule_store(PAYROLL, "wf-other")

    _schedule_service(bl).toggle_scheduled_job(
        PAYROLL, "9", ProjectScheduledJobToggleInput(enabled="0"), scope=_limited_scope()
    )
    bl.update_scheduled_job.assert_called_once()


def test_run_now_of_a_schedule_of_an_ungranted_workflow_is_not_revealed() -> None:
    bl = _schedule_store(CAMPAIGNS, REPORT)
    run_once = MagicMock()

    with pytest.raises(ServiceNotFoundError):
        _schedule_service(bl, run_once).run_scheduled_job_now(
            CAMPAIGNS, "9", scope=_limited_scope()
        )
    run_once.assert_not_called()
    bl.update_scheduled_job.assert_not_called()


def test_editing_a_schedule_needs_the_permission_in_its_workflow() -> None:
    bl = _schedule_store(PAYROLL, PAYSLIP)

    with pytest.raises(ServiceForbiddenError):
        _schedule_service(bl).update_scheduled_job(
            PAYROLL,
            "9",
            ProjectScheduledJobUpdateInput(workflow_id=PAYSLIP, cron_expr="0 10 * * *"),
            scope=_limited_scope(),
        )
    bl.update_scheduled_job.assert_not_called()


def test_deleting_a_schedule_needs_the_permission_in_its_workflow() -> None:
    bl = _schedule_store(PAYROLL, PAYSLIP)

    with pytest.raises(ServiceForbiddenError):
        _schedule_service(bl).delete_scheduled_job(PAYROLL, "9", scope=_limited_scope())
    bl.delete_scheduled_job.assert_not_called()


# --- 2. project configuration in a workflow-only project --------------------------------

_INVOICE_INSTALL = "inst-mail"
_DEPENDENCIES = [
    {"dependency_kind": "project_connection", "dependency_ref": "conn-bound", "install_id": None},
    {
        "dependency_kind": "extension_action",
        "dependency_ref": "send",
        "install_id": _INVOICE_INSTALL,
    },
]


def _loader(rows: list[dict[str, Any]] = _DEPENDENCIES):
    calls: list[tuple[str, list[str]]] = []

    def load(account_id: str, workflow_ids: list[str]) -> list[dict[str, Any]]:
        calls.append((account_id, workflow_ids))
        return rows

    load.calls = calls  # type: ignore[attr-defined]
    return load


_CONNECTIONS = [
    {"connection_id": "conn-bound", "extension_install_id": "inst-other"},
    {"connection_id": "conn-install", "extension_install_id": _INVOICE_INSTALL},
    {
        "connection_id": "conn-mapped",
        "extension_install_id": "inst-hooks",
        "workflow_mappings": {"order.created": INVOICE},
    },
    {"connection_id": "conn-unrelated", "extension_install_id": "inst-crm"},
    {
        "connection_id": "conn-other-flow",
        "extension_install_id": "inst-hooks",
        "workflow_mappings": {"lead.created": REPORT},
    },
]


def test_a_workflow_only_project_shows_only_connections_a_granted_workflow_needs() -> None:
    loader = _loader()

    visible = connections_for_scope(
        None, _limited_scope(), CAMPAIGNS, _CONNECTIONS, load_dependencies=loader
    )

    assert [row["connection_id"] for row in visible] == [
        "conn-bound",
        "conn-install",
        "conn-mapped",
    ]
    assert loader.calls == [(ACCOUNT, [INVOICE])]


def test_connections_of_a_project_reached_otherwise_are_all_kept() -> None:
    loader = _loader()

    assert (
        connections_for_scope(
            None, _limited_scope(), PAYROLL, _CONNECTIONS, load_dependencies=loader
        )
        == _CONNECTIONS
    )
    assert loader.calls == []


def test_a_projected_extension_workflow_needs_its_install_s_connections() -> None:
    scope = _limited_scope().model_copy(
        update={"project_workflow_ids": {CAMPAIGNS: frozenset({"extension.inst-crm.sync"})}}
    )

    visible = connections_for_scope(
        None, scope, CAMPAIGNS, _CONNECTIONS, load_dependencies=_loader([])
    )

    assert [row["connection_id"] for row in visible] == ["conn-unrelated"]


def test_a_failed_dependency_read_hides_every_connection() -> None:
    def broken(_account: str, _workflows: list[str]) -> list[dict[str, Any]]:
        raise RuntimeError("database down")

    assert (
        connections_for_scope(
            None, _limited_scope(), CAMPAIGNS, _CONNECTIONS, load_dependencies=broken
        )
        == []
    )


def test_extension_state_needs_an_install_a_granted_workflow_calls() -> None:
    require_install_needed(
        None, _limited_scope(), CAMPAIGNS, _INVOICE_INSTALL, load_dependencies=_loader()
    )
    with pytest.raises(ServiceNotFoundError):
        require_install_needed(
            None, _limited_scope(), CAMPAIGNS, "inst-crm", load_dependencies=_loader()
        )
    # Not limited in Payroll: nothing is read and nothing refused.
    require_install_needed(None, _limited_scope(), PAYROLL, "inst-crm", load_dependencies=_loader())


def test_needs_are_none_for_an_unlimited_person() -> None:
    assert workflow_connection_needs(None, ScopeContext(account_id=ACCOUNT), CAMPAIGNS) is None


def _connection_service(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]):
    from core.application.projects import workflow_only_connections

    bl = MagicMock()
    bl.get_project.return_value = _project(CAMPAIGNS)
    service = ProjectConnectionService(get_logger=lambda: bl)
    monkeypatch.setattr(service, "_list_connection_rows", lambda _bl, _pid: rows)
    monkeypatch.setattr(
        service,
        "_get_connection_row",
        lambda _bl, cid: next(r for r in rows if r["connection_id"] == cid),
    )
    monkeypatch.setattr(
        service,
        "_with_sender_flags",
        lambda _bl, _scope, row: service._normalize_connection_view_row(row),
    )
    monkeypatch.setattr(workflow_only_connections, "_db_dependency_loader", lambda _bl: _loader())
    return service


def test_the_connection_service_cuts_list_and_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{**row, "project_id": CAMPAIGNS, "account_id": ACCOUNT} for row in _CONNECTIONS]
    service = _connection_service(monkeypatch, rows)

    listed = service.list_connections(CAMPAIGNS, scope=_limited_scope())
    assert [view.connection_id for view in listed] == ["conn-bound", "conn-install", "conn-mapped"]

    assert service.get_connection("conn-bound", project_id=CAMPAIGNS, scope=_limited_scope())
    with pytest.raises(ServiceNotFoundError):
        service.get_connection("conn-unrelated", project_id=CAMPAIGNS, scope=_limited_scope())


def test_direct_external_effects_are_out_of_reach_in_a_workflow_only_project() -> None:
    from core.application.extensions.direct_effect_recovery import DirectEffectRecoveryService

    store = MagicMock()
    store.get_project.side_effect = _project
    install_store = MagicMock()
    install_store.get_extension_install_for_operator.return_value = {
        "install_account_id": ACCOUNT,
        "extension_id": "ext",
        "enabled": True,
    }
    service = DirectEffectRecoveryService(
        get_operation_store=lambda: store,
        get_install_store=lambda: install_store,
        get_project_by_id=None,
    )

    with pytest.raises(ServiceNotFoundError):
        service._authorize("inst", CAMPAIGNS, _limited_scope())
    _store, _scope, install = service._authorize("inst", PAYROLL, _limited_scope())
    assert install["extension_id"] == "ext"


def test_the_browser_view_of_a_read_only_connection_is_the_viewer_summary() -> None:
    from core.entrypoints.api.integrations_request_helpers import _browser_connection_view

    connection = {
        "connection_id": "conn-bound",
        "display_name": "Mail",
        "connection_config": {"api_base": "https://example.test", "oauth": {}},
        "secret_refs": {"refs": {"token": "secret://x"}},
    }
    full = _browser_connection_view(_limited_scope(), connection)
    summary = _browser_connection_view(_limited_scope(), connection, read_only=True)

    assert "secret_refs" in full
    assert "secret_refs" not in summary
    assert summary.get("connection_config", {}).get("api_base") is None
