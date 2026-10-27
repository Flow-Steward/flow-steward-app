from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import normalize_scope

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(scope="module")
def registry_client():
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app import create_app

        current_app = create_app()
        current_app.state.local_auth_enabled_override = lambda: False
        current_app.state.external_auth_enabled_override = lambda: False
        yield TestClient(current_app, raise_server_exceptions=False)
    except Exception as e:
        pytest.skip(f"Could not create TestClient: {e}")


def test_changelog_entity_history_endpoint_returns_entries(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.get_entity_history.return_value = [
        {"id": 11, "entity_type": "agent", "entity_id": "proofreader"}
    ]

    with patch.object(changelog_routes, "_get_db", return_value=mock_db):
        resp = registry_client.get("/api/changelog/agent/proofreader/history")

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    mock_db.get_entity_history.assert_called_once_with(
        entity_type="agent", entity_id="proofreader", limit=50
    )


def test_changelog_diff_endpoint_returns_404_when_revision_missing(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.diff_entity_history.side_effect = KeyError("missing")
    with patch.object(changelog_routes, "_get_db", return_value=mock_db):
        resp = registry_client.get("/api/changelog/workflow/weekly_report/diff")

    assert resp.status_code == 404


def test_changelog_diff_endpoint_returns_json_for_browser_requests(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.diff_entity_history.return_value = {
        "entity_type": "agent",
        "entity_id": "proofreader",
        "from_entry_id": 7,
        "to_entry_id": 11,
        "changes": [{"path": "prompts.system", "change": "changed", "from": "a", "to": "b"}],
        "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
    }
    mock_db.get_entity_history.return_value = [
        {"id": 11, "action": "update", "author": "alice", "created_at": "2026-03-15T10:00:00Z"},
        {"id": 7, "action": "update", "author": "bob", "created_at": "2026-03-14T08:00:00Z"},
    ]

    with patch.object(changelog_routes, "_get_db", return_value=mock_db):
        resp = registry_client.get(
            "/api/changelog/agent/proofreader/diff?from_entry_id=7",
            headers={"accept": "text/html"},
        )

    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("content-type", "")
    body = resp.json()
    assert body["from_entry_id"] == 7
    assert body["changes"][0]["path"] == "prompts.system"


def test_changelog_diff_endpoint_keeps_json_for_api_clients(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    payload = {
        "entity_type": "workflow",
        "entity_id": "weekly_report",
        "from_entry_id": 4,
        "to_entry_id": 9,
        "changes": [],
        "summary": {"total": 0, "added": 0, "removed": 0, "changed": 0},
    }
    mock_db = MagicMock()
    mock_db.diff_entity_history.return_value = payload

    with patch.object(changelog_routes, "_get_db", return_value=mock_db):
        resp = registry_client.get("/api/changelog/workflow/weekly_report/diff")

    assert resp.status_code == 200
    assert resp.json() == payload


def test_changelog_restore_rolls_back_on_invalid_validation(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
        from core.application.registry.db_store import RestoreValidationError
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.restore_entity_revision.side_effect = RestoreValidationError(
        "invalid restore",
        validation={
            "valid": False,
            "errors": [{"message": "Agent must have skill mapping"}],
            "warnings": [],
        },
    )
    mock_registry = MagicMock()

    with (
        patch.object(changelog_routes, "_get_db", return_value=mock_db),
        patch.object(changelog_routes, "_get_registry", return_value=mock_registry),
    ):
        resp = registry_client.post(
            "/api/changelog/agent/proofreader/restore", json={"entry_id": 7}
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "validation" in body["detail"]
    mock_registry.reload_all.assert_not_called()


def test_global_changelog_requires_admin_for_authenticated_viewer(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    with (
        patch.object(changelog_routes, "_get_db", return_value=mock_db),
        patch.object(
            changelog_routes,
            "_request_auth_user",
            return_value={
                "id": "viewer-1",
                "username": "viewer",
                "roles": ["viewer"],
                "account_id": DEFAULT_ACCOUNT_ID,
            },
        ),
    ):
        resp = registry_client.get("/api/changelog")

    assert resp.status_code == 403
    mock_db.get_changelog.assert_not_called()


def test_changelog_restore_rejects_snapshot_outside_account(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.get_changelog_entry.return_value = {
        "id": 7,
        "entity_type": "agent",
        "entity_id": "proofreader",
        "snapshot": {"agent_id": "proofreader", "account_id": "account-other"},
    }

    with (
        patch.object(changelog_routes, "_get_db", return_value=mock_db),
        patch.object(
            changelog_routes,
            "_request_auth_user",
            return_value={
                "id": "viewer-1",
                "username": "viewer",
                "roles": ["viewer"],
                "account_id": DEFAULT_ACCOUNT_ID,
            },
        ),
    ):
        resp = registry_client.post(
            "/api/changelog/agent/proofreader/restore", json={"entry_id": 7}
        )

    assert resp.status_code == 403
    mock_db.restore_entity_revision.assert_not_called()


def test_changelog_restore_requires_admin_for_authenticated_operator(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.get_changelog_entry.return_value = {
        "id": 7,
        "entity_type": "agent",
        "entity_id": "proofreader",
        "snapshot": {"agent_id": "proofreader", "account_id": DEFAULT_ACCOUNT_ID},
    }

    with (
        patch.object(changelog_routes, "_get_db", return_value=mock_db),
        patch.object(
            changelog_routes,
            "_request_auth_user",
            return_value={
                "id": "operator-1",
                "username": "operator",
                "roles": ["operator"],
                "account_id": DEFAULT_ACCOUNT_ID,
            },
        ),
    ):
        resp = registry_client.post(
            "/api/changelog/agent/proofreader/restore", json={"entry_id": 7}
        )

    assert resp.status_code == 403
    mock_db.restore_entity_revision.assert_not_called()


def _scoped_changelog_client(monkeypatch, mock_db, scope_context):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    monkeypatch.setattr(changelog_routes, "_get_db", lambda: mock_db)
    app = FastAPI()
    app.include_router(changelog_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        # Use a non-admin reader so these tests exercise the ownership checks;
        # authenticated admins intentionally bypass read-scope filtering.
        request.state.auth_user = {"id": "viewer-1", "username": "viewer", "roles": ["viewer"]}
        request.state.scope_context = scope_context
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("entity_type", "entity_id", "lookup_name", "id_key"),
    [
        ("agent", "scout", "get_agent", "agent_id"),
        ("tool", "search_tool", "get_tool", "tool_id"),
        ("skill", "summarize", "get_skill", "skill_id"),
    ],
)
def test_changelog_diff_uses_request_scope_context_account_for_registry_entities(
    monkeypatch, entity_type, entity_id, lookup_name, id_key
):
    mock_db = MagicMock()
    getattr(mock_db, lookup_name).return_value = {
        id_key: entity_id,
        "account_id": "account-A",
    }
    mock_db.diff_entity_history.return_value = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "from_entry_id": "1",
        "to_entry_id": "2",
        "changes": [{"path": "display_name", "change": "changed"}],
        "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
    }
    client = _scoped_changelog_client(
        monkeypatch,
        mock_db,
        normalize_scope(account_id="account-A", actor_id="admin"),
    )

    resp = client.get(
        f"/api/changelog/{entity_type}/{entity_id}/diff",
        params={"from_entry_id": "1", "to_entry_id": "2"},
    )

    assert resp.status_code == 200
    mock_db.diff_entity_history.assert_called_once_with(
        entity_type=entity_type,
        entity_id=entity_id,
        from_entry_id="1",
        to_entry_id="2",
    )


@pytest.mark.parametrize(
    ("entity_type", "entity_id", "lookup_name", "id_key"),
    [
        ("agent", "scout", "get_agent", "agent_id"),
        ("tool", "github_issue_lookup", "get_tool", "tool_id"),
        ("skill", "draft_release_notes", "get_skill", "skill_id"),
    ],
)
def test_changelog_diff_supplies_default_account_for_accountless_system_registry_entities(
    monkeypatch, entity_type, entity_id, lookup_name, id_key
):
    mock_db = MagicMock()
    getattr(mock_db, lookup_name).return_value = {
        id_key: entity_id,
        "account_id": None,
        "project_id": None,
    }
    mock_db.diff_entity_history.return_value = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "from_entry_id": "1",
        "to_entry_id": "2",
        "changes": [{"path": "display_name", "change": "changed"}],
        "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
    }
    client = _scoped_changelog_client(
        monkeypatch,
        mock_db,
        normalize_scope(account_id=DEFAULT_ACCOUNT_ID, actor_id="admin"),
    )

    resp = client.get(
        f"/api/changelog/{entity_type}/{entity_id}/diff",
        params={"from_entry_id": "1", "to_entry_id": "2"},
    )

    assert resp.status_code == 200
    mock_db.diff_entity_history.assert_called_once()


def test_changelog_diff_rejects_accountless_system_registry_entity_for_foreign_account(
    monkeypatch,
):
    mock_db = MagicMock()
    mock_db.get_agent.return_value = {
        "agent_id": "scout",
        "account_id": None,
        "project_id": None,
        "scope_kind": "system",
    }
    client = _scoped_changelog_client(
        monkeypatch,
        mock_db,
        normalize_scope(account_id="account-A", actor_id="admin"),
    )

    resp = client.get("/api/changelog/agent/scout/diff")

    assert resp.status_code == 403
    mock_db.diff_entity_history.assert_not_called()


def test_changelog_diff_resolves_project_account_for_guardrail(monkeypatch):
    mock_db = MagicMock()
    mock_db.get_guardrail.return_value = {"id": "guardrail-1", "project_id": "project-1"}
    mock_db.get_project.return_value = {"id": "project-1", "account_id": "account-A"}
    mock_db.diff_entity_history.return_value = {
        "entity_type": "guardrail",
        "entity_id": "guardrail-1",
        "from_entry_id": "1",
        "to_entry_id": "2",
        "changes": [{"path": "mode", "change": "changed"}],
        "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
    }
    client = _scoped_changelog_client(
        monkeypatch,
        mock_db,
        normalize_scope(account_id="account-A", actor_id="admin"),
    )

    resp = client.get(
        "/api/changelog/guardrail/guardrail-1/diff",
        params={"from_entry_id": "1", "to_entry_id": "2"},
    )

    assert resp.status_code == 200
    mock_db.get_project.assert_called_once_with("project-1")
    mock_db.diff_entity_history.assert_called_once()


def test_changelog_diff_supplies_default_account_for_global_guardrail(monkeypatch):
    mock_db = MagicMock()
    mock_db.get_guardrail.return_value = {
        "id": "guardrail-global",
        "account_id": None,
        "project_id": None,
    }
    mock_db.diff_entity_history.return_value = {
        "entity_type": "guardrail",
        "entity_id": "guardrail-global",
        "from_entry_id": "1",
        "to_entry_id": "2",
        "changes": [{"path": "mode", "change": "changed"}],
        "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
    }
    client = _scoped_changelog_client(
        monkeypatch,
        mock_db,
        normalize_scope(account_id=DEFAULT_ACCOUNT_ID, actor_id="admin"),
    )

    resp = client.get(
        "/api/changelog/guardrail/guardrail-global/diff",
        params={"from_entry_id": "1", "to_entry_id": "2"},
    )

    assert resp.status_code == 200
    mock_db.diff_entity_history.assert_called_once()


def test_changelog_diff_rejects_guardrail_in_foreign_project(monkeypatch):
    mock_db = MagicMock()
    mock_db.get_guardrail.return_value = {"id": "guardrail-1", "project_id": "project-1"}
    mock_db.get_project.return_value = {"id": "project-1", "account_id": "account-B"}
    client = _scoped_changelog_client(
        monkeypatch,
        mock_db,
        normalize_scope(account_id="account-A", actor_id="admin"),
    )

    resp = client.get("/api/changelog/guardrail/guardrail-1/diff")

    assert resp.status_code == 403
    mock_db.diff_entity_history.assert_not_called()


def test_agent_history_endpoint_delegates_to_db(registry_client):
    try:
        import core.entrypoints.admin.agents as agent_routes
    except Exception as e:
        pytest.skip(f"agent routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.get_agent.return_value = {"agent_id": "proofreader"}
    mock_db.get_entity_history.return_value = [{"id": 1}]

    with patch.object(agent_routes, "_get_db", return_value=mock_db):
        resp = registry_client.get("/api/agents/proofreader/history")

    assert resp.status_code == 200
    mock_db.get_entity_history.assert_called_once_with(
        entity_type="agent", entity_id="proofreader", limit=50
    )


def test_changelog_restore_tool_validation_failure_returns_400(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
        from core.application.registry.db_store import RestoreValidationError
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.restore_entity_revision.side_effect = RestoreValidationError(
        "invalid restore",
        validation={
            "valid": False,
            "errors": [{"message": "Tool display_name is required"}],
            "warnings": [],
        },
    )

    with patch.object(changelog_routes, "_get_db", return_value=mock_db):
        resp = registry_client.post("/api/changelog/tool/bad_tool/restore", json={"entry_id": 8})

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["entity_type"] == "tool"
    assert "Tool display_name is required" in body["detail"]["messages"]


def test_changelog_restore_guardrail_validation_failure_returns_400(registry_client):
    try:
        import core.entrypoints.admin.changelog as changelog_routes
        from core.application.registry.db_store import RestoreValidationError
    except Exception as e:
        pytest.skip(f"changelog routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.get_guardrail.return_value = {
        "id": "123",
        "account_id": DEFAULT_ACCOUNT_ID,
        "project_id": None,
    }
    mock_db.restore_entity_revision.side_effect = RestoreValidationError(
        "invalid restore",
        validation={
            "valid": False,
            "errors": [{"message": "Guardrail mode 'invalid' is invalid"}],
            "warnings": [],
        },
    )

    with patch.object(changelog_routes, "_get_db", return_value=mock_db):
        resp = registry_client.post("/api/changelog/guardrail/123/restore", json={"entry_id": 9})

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["entity_type"] == "guardrail"
    assert "Guardrail mode 'invalid' is invalid" in body["detail"]["messages"]


def test_workflow_restore_endpoint_rolls_back_when_validation_fails(registry_client):
    try:
        import core.entrypoints.admin.workflows as workflow_routes
    except Exception as e:
        pytest.skip(f"workflow routes not importable: {e}")

    mock_db = MagicMock()
    mock_db.get_workflow.return_value = {
        "workflow_id": "wf_bad",
        "display_name": "Invalid restore fixture",
        "projection_mode": "manual",
        "account_id": DEFAULT_ACCOUNT_ID,
        "revision": 7,
        "steps": [],
    }
    # An entry that records steps but not the groups and For each loops of a
    # definition cannot be restored: the history service refuses it before any write.
    mock_db.get_changelog_entry.return_value = {
        "id": 3,
        "entity_type": "workflow",
        "entity_id": "wf_bad",
        "snapshot": {**mock_db.get_workflow.return_value},
    }
    mock_registry = MagicMock()

    with (
        patch.object(workflow_routes, "_get_db", return_value=mock_db),
        patch.object(workflow_routes, "_get_registry", return_value=mock_registry),
    ):
        resp = registry_client.post(
            "/api/workflows/wf_bad/restore", json={"entry_id": 3, "expected_revision": 7}
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "validation" in body["detail"]
    assert body["detail"]["messages"] == [
        "This entry does not record the workflow definition and cannot be restored"
    ]
    mock_db.get_changelog_entry.assert_called_with("3", entity_type="workflow", entity_id="wf_bad")
    mock_db.restore_entity_revision.assert_not_called()
    mock_registry.reload_all.assert_not_called()
