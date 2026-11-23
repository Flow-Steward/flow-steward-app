"""The general project update guards workflow bindings and policy like their own routes.

ADR 0066 amendment: a person needs the admin role to change what `PUT .../workflows`
and `PUT .../policy` guard, also through `PUT /api/projects/{id}`. Service accounts
keep the authority they had: their `projects.write` scope still updates a project.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_WORKSPACE_ID
from core.application.management.command_models import ProjectUpdateInput
from core.application.management.command_services import ProjectCommandService
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext


def _service(*, current_workflows: list[str]) -> tuple[ProjectCommandService, MagicMock]:
    bl = MagicMock()
    bl.get_project.return_value = {
        "account_id": DEFAULT_ACCOUNT_ID,
        "id": 7,
        "name": "Invoices",
        "agents_config": {},
        "workspace_id": DEFAULT_WORKSPACE_ID,
    }
    bl.list_project_data_connections.return_value = []
    bl.get_project_workflows.return_value = [
        {"workflow_id": workflow_id, "enabled": True, "is_default": index == 0}
        for index, workflow_id in enumerate(current_workflows)
    ]
    service = ProjectCommandService(
        get_logger=lambda: bl,
        list_workflow_choices=lambda **kwargs: [
            {"workflow_id": "wf.receipts"},
            {"workflow_id": "wf.invoices"},
        ],
        resolve_workflow_selection=lambda selected, project_workflows=None: (
            selected or (project_workflows[0]["workflow_id"] if project_workflows else ""),
            "explicit",
        ),
    )
    return service, bl


def _update(workflow_ids: list[str]) -> ProjectUpdateInput:
    return ProjectUpdateInput(
        name="Invoices",
        workspace_id=DEFAULT_WORKSPACE_ID,
        workflow_ids=workflow_ids,
        operator_default_workflow_id=workflow_ids[0] if workflow_ids else "",
    )


def _scope(principal_type: str, auth_source: str, scopes: frozenset[str] = frozenset()):
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id=f"{principal_type}-1",
        principal_type=principal_type,
        auth_source=auth_source,
        token_scopes=scopes,
    )


def test_service_account_with_projects_write_still_changes_bindings() -> None:
    """Regression: the guard must not change a service account's prior authority."""
    service, bl = _service(current_workflows=["wf.receipts"])

    payload = service.update_project(
        7,
        _update(["wf.receipts", "wf.invoices"]),
        scope=_scope("service_account", "machine", frozenset({"projects.write"})),
    ).dump()

    assert payload["status"] == "updated"
    assert bl.update_project.called


def test_service_account_keeps_its_plain_project_update() -> None:
    """The update a service account could make before still works."""
    service, bl = _service(current_workflows=["wf.receipts"])

    payload = service.update_project(
        7,
        _update(["wf.receipts"]),
        scope=_scope("service_account", "machine", frozenset({"projects.write"})),
    ).dump()

    assert payload["status"] == "updated"
    assert bl.update_project.called


@pytest.mark.parametrize(
    ("principal_type", "auth_source"),
    [("operator", "local"), ("operator", "external"), ("operator", "pat"), ("viewer", "local")],
)
def test_person_below_admin_cannot_change_bindings_through_project_update(
    principal_type, auth_source
) -> None:
    service, bl = _service(current_workflows=["wf.receipts"])

    with pytest.raises(ServiceForbiddenError, match="Admin role required"):
        service.update_project(
            7, _update(["wf.receipts", "wf.invoices"]), scope=_scope(principal_type, auth_source)
        )
    bl.update_project.assert_not_called()


def test_person_below_admin_cannot_change_policy_through_project_update() -> None:
    service, bl = _service(current_workflows=["wf.receipts"])
    command = _update(["wf.receipts"]).model_copy(
        update={"form_data": {"policy_autonomy_mode": "manual"}}
    )

    with pytest.raises(ServiceForbiddenError, match="Admin role required"):
        service.update_project(7, command, scope=_scope("operator", "local"))
    bl.update_project.assert_not_called()


def test_admin_changes_bindings_through_project_update() -> None:
    service, _bl = _service(current_workflows=["wf.receipts"])

    payload = service.update_project(
        7, _update(["wf.receipts", "wf.invoices"]), scope=_scope("admin", "local")
    ).dump()

    assert payload["status"] == "updated"


def test_unchanged_bindings_are_not_a_configuration_change() -> None:
    """The guard is about bindings and policy, not about who may save at all."""
    service, _bl = _service(current_workflows=["wf.receipts"])

    payload = service.update_project(
        7, _update(["wf.receipts"]), scope=_scope("operator", "local")
    ).dump()

    assert payload["status"] == "updated"
