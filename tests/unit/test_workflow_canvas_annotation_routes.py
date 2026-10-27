"""Exercise note requests through the actual workflow router and service."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.management.scope import DEFAULT_ACCOUNT_ID, normalize_scope
from core.entrypoints.admin import workflows as workflow_routes
from core.entrypoints.admin import workflows_annotation_routes as routes
from tests.unit.test_workflow_canvas_annotations import NOTE, PNG, Plane, Repository


@pytest.fixture
def client(monkeypatch):
    repository, plane = Repository(), Plane()
    repository.workflow["account_id"] = DEFAULT_ACCOUNT_ID
    monkeypatch.setattr(routes, "_get_db", lambda: repository)
    monkeypatch.setattr(routes, "get_artifact_plane", lambda: plane)
    monkeypatch.setattr(routes, "_refresh_workflow_cache_after_commit", lambda: None)
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="tester",
            principal_type="admin",
            allowed_workflow_ids=["annotated"],
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def test_note_and_image_can_be_created_read_and_removed_via_http(client):
    base = "/api/workflows/annotated/annotations"
    saved = client.put(
        base + "/note-1",
        json={"annotation": NOTE, "expected_revision": 3, "expected_annotations_revision": 0},
    )
    assert saved.status_code == 200, saved.text
    assert client.get(base).json() == {
        "revision": 3,
        "annotations_revision": 1,
        "annotations": [NOTE],
    }
    attached = client.put(
        base + "/note-1/image",
        data={"expected_revision": "3", "expected_annotations_revision": "1"},
        files={"file": ("diagram.png", PNG, "image/png")},
    )
    assert attached.status_code == 200, attached.text
    preview = client.get(base + "/note-1/image")
    assert preview.status_code == 200
    assert preview.content == PNG
    assert preview.headers["content-type"] == "image/png"
    assert preview.headers["x-content-type-options"] == "nosniff"
    removed = client.delete(base + "/note-1?expected_revision=3&expected_annotations_revision=2")
    assert removed.status_code == 200
    assert client.get(base).json() == {"revision": 3, "annotations_revision": 3, "annotations": []}
    assert client.get(base + "/note-1/image").status_code == 404


def test_http_revision_conflict_has_existing_builder_error_contract(client):
    url = "/api/workflows/annotated/annotations/note-1"
    assert (
        client.put(
            url,
            json={"annotation": NOTE, "expected_revision": 3, "expected_annotations_revision": 0},
        ).status_code
        == 200
    )
    refused = client.put(
        url, json={"annotation": NOTE, "expected_revision": 3, "expected_annotations_revision": 0}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "workflow_annotations_revision_conflict"
    assert client.put(url, json={"annotation": NOTE}).status_code == 428


def test_path_and_note_identity_must_match(client):
    refused = client.put(
        "/api/workflows/annotated/annotations/another",
        json={"annotation": NOTE, "expected_revision": 3, "expected_annotations_revision": 0},
    )
    assert refused.status_code == 422


def test_protected_extension_canvas_shows_notes_read_only(client):
    repository = routes._get_db()
    repository.workflow["source_install_id"] = "private-extension"
    repository.workflow["settings"]["canvas_annotations"] = [NOTE]
    base = "/api/workflows/annotated/annotations"
    # The author's canvas is readable, notes included; it is never changed here.
    listed = client.get(base)
    assert listed.status_code == 200, listed.text
    assert listed.json()["annotations"] == [NOTE]
    for response in (
        client.put(
            base + "/note-1",
            json={"annotation": NOTE, "expected_revision": 3, "expected_annotations_revision": 0},
        ),
        client.delete(base + "/note-1?expected_revision=3"),
    ):
        assert response.status_code == 403, response.text
    assert repository.workflow["revision"] == 3
    assert repository.workflow["settings"]["canvas_annotations"] == [NOTE]


def test_note_save_uses_independent_presentation_revision_without_changing_workflow(client):
    base = "/api/workflows/annotated/annotations"
    saved = client.put(
        base + "/note-1",
        json={"annotation": NOTE, "expected_revision": 3, "expected_annotations_revision": 0},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"revision": 3, "annotations_revision": 1, "annotations": [NOTE]}
    stale = client.put(
        base + "/note-1",
        json={
            "annotation": {**NOTE, "text": "Stale"},
            "expected_revision": 3,
            "expected_annotations_revision": 0,
        },
    )
    assert stale.status_code == 409
    assert client.get(base).json()["annotations"] == [NOTE]


def test_note_mutation_requires_both_revision_guards(client):
    url = "/api/workflows/annotated/annotations/note-1"
    for guards in ({"expected_revision": 3}, {"expected_annotations_revision": 0}):
        refused = client.put(url, json={"annotation": NOTE, **guards})
        assert refused.status_code == 428, refused.text
    refused = client.put(
        url, json={"annotation": NOTE, "expected_revision": 2, "expected_annotations_revision": 0}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "workflow_revision_conflict"
