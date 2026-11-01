"""The same identifier/name conflict reaches every HTTP writer and the authoring paths."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.management.errors import ServiceConflictError
from core.application.management.registry_name_conflicts import NAME_CONFLICT_CODE
from core.application.management.scope import normalize_scope

VALID = {"valid": True, "errors": [], "warnings": []}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from core.entrypoints.admin.api import create_admin_router
    from core.entrypoints.api.app_factory import create_app

    app = create_app()
    paths = {str(getattr(route, "path", "")) for route in app.router.routes}
    if "/api/agents" not in paths or "/api/workflows/{workflow_id}" not in paths:
        app.include_router(create_admin_router())
    app.state.local_auth_enabled_override = lambda: False
    app.state.external_auth_enabled_override = lambda: False
    return TestClient(app, raise_server_exceptions=False)


def _detail(response):
    assert response.status_code == 409, response.text
    return response.json()["detail"]


def test_agent_create_and_patch_return_the_service_name_conflict(client):
    import core.entrypoints.admin.agents as agent_routes

    db = MagicMock()
    rows = {
        "writer": {"agent_id": "writer", "display_name": "Report Writer", "enabled": True},
        "editor": {"agent_id": "editor", "display_name": "Editor", "enabled": False},
    }
    db.get_agent.side_effect = lambda agent_id: copy.deepcopy(rows.get(agent_id))
    db.list_agents.return_value = list(rows.values())

    with (
        patch.object(agent_routes, "_get_db", return_value=db),
        patch.object(agent_routes, "_get_registry", return_value=MagicMock()),
        patch.object(agent_routes, "_get_agent_validation", return_value=VALID),
    ):
        created = client.post(
            "/api/agents",
            json={
                "agent_id": "writer_two",
                "display_name": "report  writer",
                "category": "developer-tools",
            },
        )
        patched = client.patch("/api/agents/editor", json={"display_name": "REPORT WRITER"})

    for response in (created, patched):
        detail = _detail(response)
        assert detail["code"] == NAME_CONFLICT_CODE
        assert detail["entity_type"] == "agent"
        assert [match["id"] for match in detail["matches"]] == ["writer"]
        assert detail["suggested_display_name"]
    db.save_agent.assert_not_called()


def test_skill_create_returns_the_service_name_conflict(client):
    import core.entrypoints.admin.skills as skill_routes

    db = MagicMock()
    db.get_skill.return_value = None
    db.list_skills.return_value = [
        {"skill_id": "summary", "display_name": "Summarize", "enabled": True}
    ]

    with (
        patch.object(skill_routes, "_get_db", return_value=db),
        patch.object(skill_routes, "_get_registry", return_value=MagicMock()),
        patch.object(skill_routes, "_get_skill_validation", return_value=VALID),
    ):
        response = client.post(
            "/api/skills",
            json={
                "skill_id": "summary_two",
                "display_name": " SUMMARIZE ",
                "expected_outputs": [{"name": "result", "type": "text", "required": True}],
            },
        )

    detail = _detail(response)
    assert detail["code"] == NAME_CONFLICT_CODE
    assert detail["entity_type"] == "skill"
    assert detail["matches"][0]["id"] == "summary"
    db.save_skill.assert_not_called()


def _workflow_db(existing=None):
    db = MagicMock()
    rows = {
        "weekly_digest": {
            "workflow_id": "weekly_digest",
            "display_name": "Quick Fix",
            "enabled": False,
            "trigger": {},
            "settings": {},
            "tags": [],
            "steps": [],
            "groups": [],
        },
        "taken": {"workflow_id": "taken", "display_name": "Taken Name", "enabled": True},
        "legacy_duplicate": {
            "workflow_id": "legacy_duplicate",
            "display_name": "quick fix",
            "enabled": False,
        },
    }
    db.get_workflow.side_effect = lambda workflow_id: copy.deepcopy(rows.get(workflow_id))
    db.list_workflows.return_value = list(rows.values())
    return db


def test_workflow_create_put_and_patch_refuse_a_taken_name(client):
    import core.entrypoints.admin.workflows as workflow_routes

    db = _workflow_db()
    with (
        patch.object(workflow_routes, "_get_db", return_value=db),
        patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        patch.object(workflow_routes, "_get_workflow_validation", return_value=VALID),
    ):
        created = client.post(
            "/api/workflows", json={"workflow_id": "fresh", "display_name": "taken   name"}
        )
        replaced = client.put(
            "/api/workflows/weekly_digest",
            json={"workflow_id": "weekly_digest", "display_name": "TAKEN NAME"},
        )
        patched = client.patch("/api/workflows/weekly_digest", json={"display_name": "Taken name"})

    for response in (created, replaced, patched):
        detail = _detail(response)
        assert detail["code"] == NAME_CONFLICT_CODE
        assert detail["entity_type"] == "workflow"
        assert [match["id"] for match in detail["matches"]] == ["taken"]
    db.save_workflow.assert_not_called()


def test_workflow_patch_keeps_an_existing_duplicate_editable(client):
    import core.entrypoints.admin.workflows as workflow_routes

    db = _workflow_db()
    with (
        patch.object(workflow_routes, "_get_db", return_value=db),
        patch.object(workflow_routes, "_get_registry", return_value=MagicMock()),
        patch.object(workflow_routes, "_get_workflow_validation", return_value=VALID),
    ):
        response = client.patch("/api/workflows/weekly_digest", json={"description": "edited"})

    assert response.status_code == 200, response.text
    db.save_workflow.assert_called_once()


# --------------------------------------------------------------------------- #
# Authoring draft save and group-to-child conversion                          #
# --------------------------------------------------------------------------- #


def test_draft_save_refuses_a_visible_workflow_name_and_writes_nothing():
    from tests.unit.test_workflow_authoring_drafts import SCOPE, DraftDB, bundle, service

    class NamedDraftDB(DraftDB):
        def list_workflows(self, **_):
            return [copy.deepcopy(row) for row in self.workflows.values()]

    db = NamedDraftDB()
    db.workflows["existing"] = {
        "workflow_id": "existing",
        "display_name": "Order Sync",
        "account_id": SCOPE["account_id"],
        "enabled": False,
    }
    draft_service = service(db)
    bundle_yaml = bundle(display_name="order  SYNC")

    with pytest.raises(ServiceConflictError) as refused:
        draft_service.save(scope=SCOPE, project_id="project", bundle_yaml=bundle_yaml)

    assert refused.value.payload["code"] == NAME_CONFLICT_CODE
    assert refused.value.payload["matches"][0]["id"] == "existing"
    assert set(db.workflows) == {"existing"}
    assert db.bindings == []


def test_group_conversion_refuses_a_visible_child_name(monkeypatch):
    from core.application.workflows.workflow_change_authorization import ChangeReferenceAccess
    from core.application.workflows.workflow_group_extraction_service import (
        GroupExtractionService,
    )
    from tests.unit.test_workflow_definition_service import _Cursor
    from tests.unit.test_workflow_group_extraction_service import Store, preview

    class LockCursor(_Cursor):
        def execute(self, sql, params=()):
            if "pg_advisory_xact_lock" in sql:
                self.store.locks.append(params[0])
                return
            if "workflow_runtime_dependencies" in sql or "hook_bindings" in sql:
                self._row = None
                return
            super().execute(sql, params)

    class NamedStore(Store):
        def __init__(self):
            super().__init__()
            self.locks = []
            self.workflows["other"] = {
                "workflow_id": "other",
                "display_name": "extracted",
                "account_id": "account-a",
                "enabled": False,
                "revision": 1,
            }

        @contextmanager
        def _transaction(self):
            snapshot = copy.deepcopy(self.workflows)
            try:
                yield LockCursor(self)
            except Exception:
                self.workflows = snapshot
                raise

        def list_workflows(self, **_):
            return [copy.deepcopy(row) for row in self.workflows.values()]

    store = NamedStore()
    module = "core.application.workflows.workflow_group_extraction_service"
    monkeypatch.setattr(
        f"{module}.IOResolver",
        lambda _db: SimpleNamespace(
            get_step_state_writes=lambda *_args, **_kwargs: [],
            get_step_outputs=lambda *_args: [],
            get_step_state_outputs=lambda *_args: [
                {"name": "response", "type": "object", "source": "http_request"}
            ],
            get_available_type_map_for_step=lambda *_args: {"state.items": "array"},
        ),
    )
    monkeypatch.setattr(
        f"{module}.WorkflowPortabilityService",
        lambda _db: SimpleNamespace(assert_structure_change_allowed=lambda *_args, **_kwargs: None),
    )
    extraction = GroupExtractionService(
        store,
        account_id="account-a",
        reference_access=ChangeReferenceAccess(
            **{key: lambda _value: True for key in ("agent", "workflow", "connection", "install")}
        ),
        project_visible=lambda _project: True,
        reactions=lambda *_args: [],
        validate=lambda *_args: {"errors": []},
        scope=normalize_scope(account_id="account-a", actor_id="operator"),
    )
    result = preview(extraction)

    with pytest.raises(ServiceConflictError) as refused:
        extraction.apply(
            workflow_id="wf",
            group_id="process",
            child_workflow_id="extracted",
            display_name="Extracted",
            expected_revision=3,
            preview_digest=result["preview_digest"],
            author="test",
        )

    assert refused.value.payload["matches"][0]["id"] == "other"
    assert "extracted" not in store.workflows
    assert store.locks == ["registry_display_name:account-a:workflow:extracted"]
    assert store.bindings == []
