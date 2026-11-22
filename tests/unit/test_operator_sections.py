"""The operator and viewer menu: (project, workflow) sections (ADR 0075)."""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.management.errors import ServiceNotFoundError
from core.application.management.query_services import ProjectQueryService
from core.application.management.scope import ScopeContext
from core.application.projects import operator_sections
from core.application.projects.operator_sections import (
    operator_section_detail,
    operator_workflow_from_row,
)
from core.application.projects.operator_sections import (
    operator_sections as menu,
)
from core.application.registry.workflow_contract import interface_from_definition
from core.application.registry.workflow_operator_form import contract_fingerprint

ACCOUNT = "acc-1"
P1 = "00000000-0000-0000-0000-0000000000a1"
P2 = "00000000-0000-0000-0000-0000000000a2"


def _workflow(
    workflow_id: str,
    *,
    published: bool = True,
    channels: tuple[str, ...] = ("ui", "api"),
    enabled: bool = True,
    acknowledged: bool = True,
    account_id: str = ACCOUNT,
    menu_title: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "workflow_id": workflow_id,
        "display_name": workflow_id.title(),
        "account_id": account_id,
        "revision": 3,
        "enabled": enabled,
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": list(channels),
                "expected_inputs": [{"name": "note", "type": "string"}],
            }
        },
    }
    operator: dict[str, Any] = {"published": published, "menu_title": menu_title}
    if acknowledged:
        operator["effects_acknowledged"] = {
            "by": "admin-1",
            "at": "2026-11-19T10:00:00+00:00",
            "revision": 3,
            "contract_sha": contract_fingerprint(interface_from_definition(row)),
        }
    row["settings"]["operator"] = operator
    return row


WORKFLOWS = {
    "contracts": _workflow("contracts", menu_title="Contracts"),
    "receipts": _workflow("receipts"),
    "draft": _workflow("draft", published=False),
    "api_only": _workflow("api_only", channels=("api",)),
    "paused": _workflow("paused", enabled=False),
    "unconfirmed": _workflow("unconfirmed", acknowledged=False),
    "foreign": _workflow("foreign", account_id="acc-other"),
}


def _binding(project_id: str, project_name: str, workflow_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "project_name": project_name,
        "account_id": ACCOUNT,
        "workspace_id": "ws-1",
        "workflow_id": workflow_id,
    }


def _loader(reads: list[str] | None = None):
    def load(workflow_id: str):
        if reads is not None:
            reads.append(workflow_id)
        row = WORKFLOWS.get(workflow_id)
        return operator_workflow_from_row(copy.deepcopy(row)) if row else None

    return load


def _scope(**fields: Any) -> ScopeContext:
    return ScopeContext(account_id=ACCOUNT, actor_id="op-1", principal_type="operator", **fields)


def _pairs(sections) -> list[tuple[str, str]]:
    return [(item.project_id, item.workflow_id) for item in sections]


def test_one_project_lists_its_published_ui_workflows_only() -> None:
    bindings = [_binding(P1, "Alpha", name) for name in WORKFLOWS]

    sections = menu(bindings, _scope(), load_workflow=_loader())

    assert _pairs(sections) == [
        (P1, "contracts"),
        (P1, "paused"),
        (P1, "receipts"),
        (P1, "unconfirmed"),
    ]
    by_id = {item.workflow_id: item for item in sections}
    assert by_id["contracts"].title == "Contracts"
    assert by_id["receipts"].title == "Receipts"
    assert by_id["contracts"].runnable is True
    assert by_id["contracts"].project_name == "Alpha"


def test_blocked_sections_stay_in_the_menu_with_their_reasons() -> None:
    sections = menu(
        [_binding(P1, "Alpha", "paused"), _binding(P1, "Alpha", "unconfirmed")],
        _scope(),
        load_workflow=_loader(),
    )

    reasons = {item.workflow_id: [r.code for r in item.blocked_reasons] for item in sections}
    assert all(item.runnable is False for item in sections)
    assert reasons["paused"] == ["operator.workflow_disabled"]
    assert reasons["unconfirmed"] == ["operator.effects_not_acknowledged"]


def test_one_publication_appears_in_every_bound_project_and_is_read_once() -> None:
    reads: list[str] = []
    bindings = [
        _binding(P2, "Beta", "contracts"),
        _binding(P1, "Alpha", "contracts"),
        _binding(P1, "Alpha", "receipts"),
    ]

    sections = menu(bindings, _scope(), load_workflow=_loader(reads))

    assert _pairs(sections) == [(P1, "contracts"), (P1, "receipts"), (P2, "contracts")]
    assert reads == ["contracts", "receipts"]


def test_token_limits_hide_other_projects_and_workflows() -> None:
    bindings = [
        _binding(P1, "Alpha", "contracts"),
        _binding(P1, "Alpha", "receipts"),
        _binding(P2, "Beta", "contracts"),
    ]

    by_workflow = menu(
        bindings, _scope(allowed_workflow_ids=frozenset({"receipts"})), load_workflow=_loader()
    )
    by_project = menu(
        bindings, _scope(allowed_project_ids=frozenset({P2})), load_workflow=_loader()
    )

    assert _pairs(by_workflow) == [(P1, "receipts")]
    assert _pairs(by_project) == [(P2, "contracts")]


def test_another_accounts_project_or_workflow_never_appears() -> None:
    foreign_project = {**_binding(P1, "Alpha", "contracts"), "account_id": "acc-other"}

    sections = menu(
        [foreign_project, _binding(P2, "Beta", "foreign")], _scope(), load_workflow=_loader()
    )

    assert sections == []


def test_a_detail_carries_the_resolved_form_and_revision() -> None:
    detail = operator_section_detail(
        _binding(P1, "Alpha", "contracts"), _scope(), load_workflow=_loader()
    )

    assert detail is not None
    assert detail.workflow_revision == 3
    assert detail.effects_acknowledged is True
    assert [field.name for field in detail.operator.form_fields] == ["note"]
    assert detail.operator.settings["menu_title"] == "Contracts"
    assert "contract_sha" not in detail.operator.settings["effects_acknowledged"]


@pytest.mark.parametrize("workflow_id", ["draft", "api_only", "missing"])
def test_a_detail_of_something_not_in_the_menu_is_none(workflow_id) -> None:
    assert (
        operator_section_detail(
            _binding(P1, "Alpha", workflow_id), _scope(), load_workflow=_loader()
        )
        is None
    )


# --- Through the project query service -------------------------------------------------


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(operator_sections, "load_operator_workflow", _loader())
    bl = MagicMock()
    bl.list_active_project_workflow_bindings.return_value = [
        _binding(P1, "Alpha", "contracts"),
        _binding(P2, "Beta", "contracts"),
        _binding(P2, "Beta", "draft"),
    ]
    bl.get_project.return_value = {
        "id": P1,
        "name": "Alpha",
        "account_id": ACCOUNT,
        "workspace_id": "ws-1",
    }
    bl.get_project_workflows.return_value = [{"workflow_id": "contracts", "enabled": True}]
    return (
        ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        ),
        bl,
    )


def test_the_menu_is_one_read_of_the_accounts_bindings(service) -> None:
    query, bl = service

    sections = query.list_operator_sections(scope=_scope())

    bl.list_active_project_workflow_bindings.assert_called_once_with(account_id=ACCOUNT)
    assert [(item["project_name"], item["workflow_id"]) for item in sections] == [
        ("Alpha", "contracts"),
        ("Beta", "contracts"),
    ]
    assert sections[0]["blocked_reasons"] == []


def test_a_section_page_needs_an_active_binding(service) -> None:
    query, bl = service

    detail = query.get_operator_section(P1, "contracts", scope=_scope())
    assert detail["workflow_id"] == "contracts"
    assert detail["operator"]["runnable"] is True

    bl.get_project_workflows.return_value = []
    with pytest.raises(ServiceNotFoundError):
        query.get_operator_section(P1, "contracts", scope=_scope())
