"""The edit session HTTP routes on the real app and the disposable audit database.

Strict fixture: the app is built by ``create_app`` and only the database and
registry handles are replaced; an app that cannot start fails the test instead
of skipping it. The routes use the workflow's existing read and write policy
and the same authority and credential projection as both history diff routes.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from core.application.management.scope import normalize_scope
from core.application.workflows.workflow_edit_session_contract import authoring_content
from tests.unit.test_workflow_history_backend import (
    NEW_SECRET_REF,
    _changelog_count,
    _history,
    _inputs,
    _save,
    _save_settings_only,
)

#: A control plaintext that must never come back from a compare.
PASSWORD_BEFORE = "control-plaintext-before"  # pragma: allowlist secret
PASSWORD_AFTER = "control-plaintext-after"  # pragma: allowlist secret


@pytest.fixture
def edit_session_client(audit_db, monkeypatch):
    from core.entrypoints.admin import workflows_routes_edit_session as routes
    from core.entrypoints.api.app import create_app

    store, conn, workflow_id, _project_id = audit_db
    app = create_app()
    app.state.local_auth_enabled_override = lambda: False
    app.state.external_auth_enabled_override = lambda: False
    monkeypatch.setattr(routes, "_get_db", lambda: store)
    monkeypatch.setattr(routes, "_get_registry", lambda: MagicMock())
    return TestClient(app, raise_server_exceptions=True), store, conn, workflow_id


def _content(store, workflow_id: str) -> dict:
    return json.loads(json.dumps(authoring_content(store.get_workflow(workflow_id)), default=str))


def _stored_state(store, conn, workflow_id: str) -> tuple:
    """The stored workflow and its history length, to prove nothing was written."""
    return store.get_workflow(workflow_id), _changelog_count(conn, workflow_id)


def _with_password(content: dict, value: str) -> dict:
    changed = copy.deepcopy(content)
    for step in changed["steps"]:
        if step["step_id"] == "a":
            step["override"] = {"headers": {"password": value}}
    return changed


def _url(workflow_id: str, action: str) -> str:
    return f"/api/workflows/{workflow_id}/edit-session/{action}"


def test_compare_route_returns_three_redacted_comparisons_and_writes_nothing(
    edit_session_client,
) -> None:
    client, store, conn, workflow_id = edit_session_client
    _save(store, workflow_id, ("a",))
    base = _content(store, workflow_id)
    store.save_workflow(
        {**store.get_workflow(workflow_id), **_with_password(base, PASSWORD_AFTER)},
        expected_revision=1,
    )
    local = _with_password(base, PASSWORD_BEFORE)
    before = _stored_state(store, conn, workflow_id)

    response = client.post(
        _url(workflow_id, "compare"), json={"base_content": base, "local_content": local}
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["current_revision"] == 2
    assert PASSWORD_BEFORE not in response.text
    assert PASSWORD_AFTER not in response.text
    for key in ("base_to_local", "base_to_server", "local_to_server"):
        changes = payload[key]["changes"]
        assert changes, key
        assert all(
            change["to"] in ("[REDACTED]", None) and change["from"] in ("[REDACTED]", None)
            for change in changes
            if "password" in change["path"]
        )
    assert _stored_state(store, conn, workflow_id) == before


def test_compare_route_never_returns_secret_authority(edit_session_client) -> None:
    client, store, _conn, workflow_id = edit_session_client
    _save(
        store,
        workflow_id,
        ("a",),
        settings=_inputs({"name": "login", "type": "secret", "default_secret_ref": NEW_SECRET_REF}),
    )
    base = _content(store, workflow_id)

    response = client.post(
        _url(workflow_id, "compare"), json={"base_content": base, "local_content": base}
    )

    assert response.status_code == 200, response.text
    assert NEW_SECRET_REF not in response.text
    assert "default_secret_ref" not in response.text


def test_apply_route_writes_the_reviewed_copy_as_the_request_actor(edit_session_client) -> None:
    client, store, conn, workflow_id = edit_session_client
    # Disabled: the route judges readiness, and these fixture steps are drafts.
    _save(store, workflow_id, ("a",))
    base = _content(store, workflow_id)
    _save_settings_only(store, workflow_id, description="theirs")
    local = copy.deepcopy(base)
    local["description"] = "mine"
    before = _changelog_count(conn, workflow_id)

    response = client.post(
        _url(workflow_id, "apply"), json={"content": local, "expected_revision": 2}
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["applied"] is True
    assert payload["revision"] == 3
    assert payload["content"]["description"] == "mine"
    stored = store.get_workflow(workflow_id)
    assert stored["description"] == "mine"
    assert stored["enabled"] is False
    assert _changelog_count(conn, workflow_id) == before + 1
    assert _history(store, workflow_id)[0]["author"] == normalize_scope().actor_name


def test_apply_route_answers_a_newer_revision_with_a_conflict_and_no_write(
    edit_session_client,
) -> None:
    client, store, conn, workflow_id = edit_session_client
    _save(store, workflow_id, ("a",))
    base = _content(store, workflow_id)
    _save_settings_only(store, workflow_id, description="13")
    _save_settings_only(store, workflow_id, description="14")
    before = _stored_state(store, conn, workflow_id)

    response = client.post(
        _url(workflow_id, "apply"), json={"content": base, "expected_revision": 2}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "workflow_revision_conflict"
    assert _stored_state(store, conn, workflow_id) == before


@pytest.mark.parametrize(
    "body",
    [
        {"expected_revision": 1, "force": True},
        {"expected_revision": 1, "author": "someone"},
        {"expected_revision": 0},
    ],
)
def test_apply_route_takes_no_force_author_or_invalid_revision(edit_session_client, body) -> None:
    client, store, conn, workflow_id = edit_session_client
    _save(store, workflow_id, ("a",))
    before = _stored_state(store, conn, workflow_id)

    response = client.post(
        _url(workflow_id, "apply"), json={"content": _content(store, workflow_id), **body}
    )

    assert response.status_code == 422, response.text
    assert _stored_state(store, conn, workflow_id) == before


def test_apply_route_refuses_service_owned_fields_with_their_paths(edit_session_client) -> None:
    client, store, conn, workflow_id = edit_session_client
    _save(store, workflow_id, ("a",))
    content = _content(store, workflow_id)
    content["enabled"] = True
    before = _stored_state(store, conn, workflow_id)

    response = client.post(
        _url(workflow_id, "apply"), json={"content": content, "expected_revision": 1}
    )

    assert response.status_code == 400, response.text
    assert "workflow.edit_session.field_unknown" in response.text
    assert _stored_state(store, conn, workflow_id) == before


def test_apply_route_dry_run_writes_nothing(edit_session_client) -> None:
    client, store, conn, workflow_id = edit_session_client
    _save(store, workflow_id, ("a",))
    content = _content(store, workflow_id)
    content["description"] = "checked only"
    before = _stored_state(store, conn, workflow_id)

    response = client.post(
        _url(workflow_id, "apply"),
        json={"content": content, "expected_revision": 1, "dry_run": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is False
    assert _stored_state(store, conn, workflow_id) == before


@pytest.mark.parametrize("action", ["compare", "apply"])
def test_routes_answer_404_for_a_missing_workflow(edit_session_client, action) -> None:
    client, store, _conn, workflow_id = edit_session_client
    _save(store, workflow_id, ("a",))
    content = _content(store, workflow_id)
    body = (
        {"base_content": content, "local_content": content}
        if action == "compare"
        else {"content": content, "expected_revision": 1}
    )

    response = client.post(_url(f"{workflow_id}_missing", action), json=body)

    assert response.status_code == 404, response.text
