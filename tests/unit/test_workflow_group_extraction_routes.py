"""The browser and MCP use the same authenticated extraction transport."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.management.scope import DEFAULT_ACCOUNT_ID, normalize_scope
from core.entrypoints.admin import workflows as workflow_routes
from core.entrypoints.admin import workflows_group_extraction_routes as routes
from tests.unit.test_workflow_group_extraction_service import service as service


@pytest.fixture
def client(monkeypatch, service):
    extraction, store = service
    extraction.account_id = DEFAULT_ACCOUNT_ID
    store.workflows["wf"]["account_id"] = DEFAULT_ACCOUNT_ID
    monkeypatch.setattr(routes, "_get_db", lambda: store)
    monkeypatch.setattr(routes, "GroupExtractionService", lambda *_args, **_kwargs: extraction)
    monkeypatch.setattr(
        routes,
        "_platform_logger_for_admin_catalog",
        lambda _db: SimpleNamespace(list_workflow_reactions=lambda **_kwargs: []),
    )
    monkeypatch.setattr(routes, "_refresh_workflow_cache_after_commit", lambda: None)
    monkeypatch.setattr("core.entrypoints.admin.workflows_routes_write._get_db", lambda: store)
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="tester",
            principal_type="admin",
            allowed_workflow_ids=["wf"],
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), store


URL = "/api/workflows/wf/groups/process/convert-to-child"
BODY = {"child_workflow_id": "extracted", "display_name": "Extracted", "expected_revision": 3}


def test_preview_and_apply_through_http_have_one_revision_contract(client):
    client, store = client
    preview = client.post(URL, json=BODY)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_convert"]
    assert set(store.workflows) == {"wf"}
    saved = client.post(
        URL, json={**BODY, "dry_run": False, "preview_digest": preview.json()["preview_digest"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 4
    assert set(store.workflows) == {"wf", "extracted"}


def test_apply_requires_a_reviewed_preview_and_positive_revision(client):
    client, store = client
    assert client.post(URL, json={**BODY, "dry_run": False}).status_code == 422
    assert client.post(URL, json={**BODY, "expected_revision": 0}).status_code == 422
    assert set(store.workflows) == {"wf"}


def test_revision_conflict_and_boundary_issues_are_actionable(client):
    client, store = client
    store.workflows["wf"]["revision"] = 4
    refused = client.post(URL, json=BODY)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "workflow_revision_conflict"
    store.workflows["wf"]["revision"] = 3
    store.workflows["wf"]["steps"][1]["step_kind"] = "agent"
    preview = client.post(URL, json=BODY)
    assert preview.status_code == 200, preview.text
    assert not preview.json()["can_convert"]
    refused = client.post(
        URL, json={**BODY, "dry_run": False, "preview_digest": preview.json()["preview_digest"]}
    )
    assert refused.status_code == 400
    assert refused.json()["detail"]["issues"]
    assert set(store.workflows) == {"wf"}
