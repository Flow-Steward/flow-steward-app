"""Workflow revision reads use current authority without replaying history."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import ScopeContext
from core.entrypoints.admin import workflows_routes_history as routes

WORKFLOW_ID = "f597069e-54f5-4cce-8324-fbbeb4bcd218"
OTHER_WORKFLOW_ID = "415b7f69-c7cb-4e7e-b0df-77e5366bbb94"
FOREIGN_ACCOUNT_ID = "00000000-0000-0000-0000-000000000099"


class _WorkflowHistoryReader:
    """Only DB reads are substituted; resolver, guards and redaction stay real."""

    dsn = ""

    def __init__(self, current, entry):
        self.current = deepcopy(current)
        self.entry = deepcopy(entry)
        self.writes = []

    def get_workflow(self, workflow_id):
        assert workflow_id == WORKFLOW_ID
        return deepcopy(self.current)

    def get_entity_revision(self, *, entity_type, entity_id, revision):
        assert (entity_type, entity_id, revision) == ("workflow", WORKFLOW_ID, 1)
        return deepcopy(self.entry)

    def get_entity_history(self, *, entity_type, entity_id, limit):
        assert (entity_type, entity_id) == ("workflow", WORKFLOW_ID)
        assert limit > 0
        return [deepcopy(self.entry)] if self.entry is not None else []

    def restore_entity_revision(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        raise AssertionError("A revision read must not restore history")

    def save_workflow(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        raise AssertionError("A revision read must not save a workflow")


def _request(*, allowed_workflow=WORKFLOW_ID):
    request = Request({"type": "http"})
    request.state.scope_context = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="workflow-history-reader",
        principal_type="user",
        allowed_workflow_ids=frozenset({allowed_workflow}),
    )
    return request


def _current(account_id=DEFAULT_ACCOUNT_ID):
    return {
        "workflow_id": WORKFLOW_ID,
        "account_id": account_id,
        "scope_kind": "account",
        "projection_mode": "manual",
        "enabled": False,
    }


def _marker():
    return {
        "id": "marker-entry",
        "entity_type": "workflow",
        "entity_id": WORKFLOW_ID,
        "action": "restore",
        "revision_kind": "lifecycle",
        "author": "original-author",
        "snapshot": {
            "lifecycle_action": "restore",
            "entity_type": "workflow",
            "entity_id": WORKFLOW_ID,
            "actor": "original-author",
        },
    }


def _content(account_id=DEFAULT_ACCOUNT_ID):
    return {
        "id": "content-entry",
        "entity_type": "workflow",
        "entity_id": WORKFLOW_ID,
        "action": "update",
        "revision_kind": "content",
        "author": "original-author",
        "snapshot": {
            "workflow_id": WORKFLOW_ID,
            "account_id": account_id,
            "projection_mode": "manual",
            "display_name": "Historical content",
            "settings": {
                "callable": {
                    "expected_inputs": [
                        {
                            "name": "fixture-input",
                            "type": "secret",
                            "default_secret_ref": ":".join(
                                ("workflow-input", "v1", "owned", "history")
                            ),
                        }
                    ],
                },
            },
        },
    }


@pytest.fixture
def bind_reader(monkeypatch):
    readers = []

    def bind(current, entry):
        reader = _WorkflowHistoryReader(current, entry)
        readers.append((reader, deepcopy(current), deepcopy(entry)))
        monkeypatch.setattr(routes, "_get_db", lambda: reader)
        return reader

    yield bind

    for reader, current, entry in readers:
        assert reader.current == current
        assert reader.entry == entry
        assert reader.writes == []


@pytest.mark.parametrize("boundary", ["workflow-allowlist", "current-account"])
def test_active_lifecycle_revision_requires_current_workflow_authority(bind_reader, boundary):
    bind_reader(
        _current(FOREIGN_ACCOUNT_ID if boundary == "current-account" else DEFAULT_ACCOUNT_ID),
        _marker(),
    )
    request = _request(
        allowed_workflow=OTHER_WORKFLOW_ID if boundary == "workflow-allowlist" else WORKFLOW_ID
    )

    with pytest.raises(HTTPException) as raised:
        routes.get_workflow_revision(WORKFLOW_ID, 1, request)

    assert raised.value.status_code == 403


def test_active_matching_workflow_returns_selected_lifecycle_revision(bind_reader):
    bind_reader(_current(), _marker())

    entry = routes.get_workflow_revision(WORKFLOW_ID, 1, _request())

    assert entry["id"] == "marker-entry"
    assert entry["action"] == "restore"
    assert entry["revision_kind"] == "lifecycle"
    assert entry["author"] == "original-author"
    assert entry["snapshot"]["lifecycle_action"] == "restore"


def test_active_current_account_wins_over_historical_account_and_preserves_redaction(bind_reader):
    bind_reader(_current(), _content(FOREIGN_ACCOUNT_ID))

    entry = routes.get_workflow_revision(WORKFLOW_ID, 1, _request())

    assert entry["id"] == "content-entry"
    assert entry["snapshot"]["display_name"] == "Historical content"
    assert "default_secret_ref" not in json.dumps(entry)


def test_deleted_content_with_exact_identity_remains_readable_and_redacted(bind_reader):
    bind_reader(None, _content())

    entry = routes.get_workflow_revision(WORKFLOW_ID, 1, _request())

    assert entry["id"] == "content-entry"
    assert entry["revision_kind"] == "content"
    assert entry["author"] == "original-author"
    assert entry["snapshot"]["workflow_id"] == WORKFLOW_ID
    assert entry["snapshot"]["display_name"] == "Historical content"
    assert "default_secret_ref" not in json.dumps(entry)


def test_deleted_content_in_foreign_account_stays_forbidden(bind_reader):
    bind_reader(None, _content(FOREIGN_ACCOUNT_ID))
    request = _request()

    with pytest.raises(HTTPException) as raised:
        routes.get_workflow_revision(WORKFLOW_ID, 1, request)

    assert raised.value.status_code == 403


def test_deleted_identity_free_marker_is_not_revision_authority(bind_reader):
    bind_reader(None, _marker())
    request = _request()

    with pytest.raises(HTTPException) as raised:
        routes.get_workflow_revision(WORKFLOW_ID, 1, request)

    assert raised.value.status_code == 404


@pytest.mark.parametrize("snapshot_workflow_id", [None, OTHER_WORKFLOW_ID])
def test_deleted_content_requires_identity_matching_requested_workflow(
    bind_reader, snapshot_workflow_id
):
    entry = _content()
    if snapshot_workflow_id is None:
        del entry["snapshot"]["workflow_id"]
    else:
        entry["snapshot"]["workflow_id"] = snapshot_workflow_id
    bind_reader(None, entry)
    request = Request({"type": "http"})
    request.state.scope_context = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="unrestricted-history-reader",
        principal_type="user",
    )

    with pytest.raises(HTTPException) as raised:
        routes.get_workflow_revision(WORKFLOW_ID, 1, request)

    assert raised.value.status_code == 404


def test_missing_revision_remains_not_found(bind_reader):
    bind_reader(_current(), None)
    request = _request()

    with pytest.raises(HTTPException) as raised:
        routes.get_workflow_revision(WORKFLOW_ID, 1, request)

    assert raised.value.status_code == 404
