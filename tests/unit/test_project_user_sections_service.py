"""Saving, reading and restoring project user sections (ADR 0075)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_WORKSPACE_ID
from core.application.management.command_models import ProjectUpdateInput
from core.application.management.command_services import ProjectCommandService
from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.projects import user_sections_service
from core.application.projects.user_sections import SectionContract
from core.infrastructure.db import project_settings_store
from core.infrastructure.db.project_user_sections_store import stored_user_sections

SECTION = {
    "id": "receipts",
    "title": "Receipts",
    "workflow_id": "wf.receipts",
    "fields": [{"path": "receipt"}],
    "columns": [{"id": "status", "source": "job", "path": "status"}],
    "submission": {"mode": "foreach", "target_input": "receipt"},
    "effects_acknowledged": True,
}


def _contract(workflow_id: str) -> SectionContract:
    return SectionContract(
        workflow_id=workflow_id,
        name="Receipts",
        revision=2,
        input_schema={
            "type": "object",
            "properties": {"receipt": {"type": "object", "x-fs-type": "file"}},
            "required": ["receipt"],
        },
        output_schema={"type": "object", "properties": {}},
    )


@pytest.fixture
def saved(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        user_sections_service, "registry_contract_loader", lambda _linked: _contract
    )
    monkeypatch.setattr(
        user_sections_service,
        "save_user_sections",
        lambda conn, **kwargs: calls.append(kwargs),
    )
    return calls


def _service() -> tuple[ProjectCommandService, MagicMock]:
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
        {"workflow_id": "wf.receipts", "enabled": True, "is_default": True}
    ]
    service = ProjectCommandService(
        get_logger=lambda: bl,
        list_workflow_choices=lambda **kwargs: [{"workflow_id": "wf.receipts"}],
        resolve_workflow_selection=lambda selected, project_workflows=None: (selected, "explicit"),
    )
    return service, bl


def _update(user_sections: Any = None) -> ProjectUpdateInput:
    fields: dict[str, Any] = {
        "name": "Invoices",
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "workflow_ids": ["wf.receipts"],
        "operator_default_workflow_id": "wf.receipts",
    }
    if user_sections is not None:
        fields["user_sections"] = user_sections
    return ProjectUpdateInput(**fields)


def _scope(role: str) -> ScopeContext:
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id=f"{role}-1",
        principal_type=role,
        auth_source="local",
    )


def test_admin_saves_sections_with_the_project(saved) -> None:
    service, _bl = _service()

    service.update_project(7, _update([SECTION]), scope=_scope("admin"))

    assert len(saved) == 1
    assert saved[0]["project_id"] == "7"
    assert saved[0]["account_id"] == DEFAULT_ACCOUNT_ID
    assert [section.id for section in saved[0]["sections"]] == ["receipts"]


def test_omitted_sections_are_kept_and_an_empty_list_clears_them(saved) -> None:
    service, _bl = _service()

    service.update_project(7, _update(), scope=_scope("admin"))
    assert saved == []

    service.update_project(7, _update([]), scope=_scope("admin"))
    assert saved[0]["sections"] == []


def test_invalid_sections_are_refused_before_anything_is_written(saved) -> None:
    service, bl = _service()
    broken = {**SECTION, "fields": [{"path": "missing"}]}

    with pytest.raises(ServiceValidationError, match="no input 'missing'"):
        service.update_project(7, _update([broken]), scope=_scope("admin"))
    assert saved == []
    bl.update_project.assert_not_called()


def test_an_operator_cannot_configure_sections(saved) -> None:
    service, bl = _service()

    with pytest.raises(ServiceForbiddenError, match="Admin role required"):
        service.update_project(7, _update([SECTION]), scope=_scope("operator"))
    assert saved == []
    bl.update_project.assert_not_called()


def test_stored_sections_read_from_the_project_row() -> None:
    assert stored_user_sections({"config": {"other": 1}}) is None
    assert stored_user_sections({"config": '{"user_sections": [{"id": "a"}, 3]}'}) == [{"id": "a"}]
    assert stored_user_sections({"config": {"user_sections": []}}) == []


def _restore(snapshot: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    restored: list[tuple[str, list[dict[str, Any]]]] = []
    project_settings_store.restore_project_settings_revision(
        "9",
        "7",
        as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
        as_list=lambda value: list(value) if isinstance(value, list) else [],
        normalize_string_list=lambda value: list(value or []),
        get_project_settings_revision=lambda _project, _revision: {"snapshot_jsonb": snapshot},
        get_project=lambda _project: {"workspace_id": "ws"},
        update_project=MagicMock(),
        set_project_data_connections=None,
        get_project_provider_bindings=lambda *_args, **_kwargs: [],
        save_project_provider_binding=MagicMock(),
        create_project_settings_revision=lambda *_args, **_kwargs: 11,
        save_user_sections=lambda project, sections: restored.append((project, sections)),
    )
    return restored


def test_restoring_a_revision_restores_its_sections() -> None:
    snapshot = {"project": {"name": "Demo"}, "user_sections": [SECTION]}

    assert _restore(snapshot) == [("9", [SECTION])]


def test_an_older_revision_without_sections_keeps_the_current_ones() -> None:
    assert _restore({"project": {"name": "Demo"}}) == []
