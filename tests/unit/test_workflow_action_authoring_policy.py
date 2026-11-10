"""Marking a workflow as an action implementation requires Development (ADR 0065)."""

from __future__ import annotations

import pytest

from core.application.workflows import workflow_definition_service as definition_service
from core.application.workflows import workflow_hook_authoring_policy as policy
from core.application.workflows.workflow_hook_binding_service import _target_issues

MARKER = {"action_id": "orders.create_full", "connections": []}


def workflow(marker: dict | None = None) -> dict:
    settings = {"callable": {"enabled": True, "trigger_channels": ["parent_workflow"]}}
    if marker is not None:
        settings["action_implementation"] = marker
    return {"workflow_id": "create_full", "settings": settings, "steps": []}


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value: str) -> None:
        monkeypatch.setattr(policy, "installation_mode", lambda: value)
        monkeypatch.setattr(definition_service, "installation_mode", lambda: value)

    return set_mode


def test_development_may_mark_a_workflow(mode) -> None:
    mode("development")

    assert policy.hook_declaration_authoring_issues(workflow(MARKER)) == []
    kept = definition_service.preserve_service_owned_settings(workflow(MARKER), workflow())
    assert kept["settings"]["action_implementation"] == MARKER


def test_production_refuses_a_new_or_changed_marker(mode) -> None:
    mode("production")
    changed = {**MARKER, "action_id": "orders.other"}

    for candidate, existing in (
        (workflow(MARKER), None),
        (workflow(MARKER), workflow()),
        (workflow(changed), workflow(MARKER)),
    ):
        issues = policy.hook_declaration_authoring_issues(candidate, existing=existing)
        assert [issue["code"] for issue in issues] == [policy.ACTION_MARKER_MODE_CODE]
        assert issues[0]["path"] == "settings.action_implementation"


def test_production_keeps_an_existing_marker_untouched(mode) -> None:
    mode("production")

    assert (
        policy.hook_declaration_authoring_issues(workflow(MARKER), existing=workflow(MARKER)) == []
    )
    assert policy.hook_declaration_authoring_issues(workflow(), existing=workflow(MARKER)) == []


def test_a_metadata_write_cannot_set_or_change_the_marker_in_production(mode) -> None:
    mode("production")
    changed = {**MARKER, "action_id": "orders.other"}

    added = definition_service.preserve_service_owned_settings(workflow(MARKER), workflow())
    edited = definition_service.preserve_service_owned_settings(workflow(changed), workflow(MARKER))
    removed = definition_service.preserve_service_owned_settings(workflow(), workflow(MARKER))

    assert "action_implementation" not in added["settings"]
    assert edited["settings"]["action_implementation"] == MARKER
    assert removed["settings"]["action_implementation"] == MARKER


def test_an_installed_implementation_is_never_a_hook_target() -> None:
    implementation = {**workflow(MARKER), "enabled": True, "source_install_id": "pi_addon"}

    issues = _target_issues(implementation, account_id="acct", target="extension.pi_addon.x")

    assert [issue["code"] for issue in issues] == ["workflow.hook.target_not_callable"]
    # The author's own workflow is an ordinary callable child until it ships.
    own = {**workflow(MARKER), "enabled": True}
    assert _target_issues(own, account_id="acct", target="create_full") == []
