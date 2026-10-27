from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.workflows.workflow_definition_service import WorkflowChangeResult
from core.application.workflows.workflow_portability import WorkflowPortabilityService


def _diff(ref: str) -> dict:
    return {
        "changes": [
            {
                "path": "settings.callable.expected_inputs[0].default_secret_ref",
                "change": "changed",
                "from": ref + "-old",
                "to": ref,
            },
            {"path": "display_name", "change": "changed", "from": "A", "to": "B"},
        ],
        "summary": {"total": 2, "added": 0, "removed": 0, "changed": 2},
    }


def _snapshot(ref: str) -> dict:
    return {
        "workflow_id": "orders",
        "settings": {
            "callable": {
                "expected_inputs": [{"name": "login", "type": "secret", "default_secret_ref": ref}]
            }
        },
    }


def test_workflow_lifecycle_diff_keeps_empty_summary_while_redacting_payload() -> None:
    ref = "workflow-input:v1:owned:lifecycle-history"
    payload = {
        "comparison_kind": "lifecycle",
        "changes": [],
        "summary": {},
        "snapshot": _snapshot(ref),
        "metadata": {"default_secret_ref": ref, "kept": "safe"},
    }
    stored_before = deepcopy(payload)

    projected = WorkflowPortabilityService(MagicMock()).redact_workflow_history_payload(payload)

    assert projected["comparison_kind"] == "lifecycle"
    assert projected["changes"] == []
    assert projected["summary"] == {}
    assert projected["metadata"] == {"kept": "safe"}
    assert "default_secret_ref" not in json.dumps(projected)
    assert payload == stored_before


def test_dedicated_workflow_diff_and_restore_routes_redact_secret_authority() -> None:
    from core.entrypoints.admin import workflows_routes_history as routes

    ref = "workflow-input:v1:owned:dedicated"
    db = MagicMock()
    db.diff_entity_history.return_value = _diff(ref)
    db.get_changelog_entry.return_value = {"id": "entry-1", "snapshot": _snapshot(ref)}
    service = WorkflowPortabilityService(db)
    service.assert_history_diff_allowed = MagicMock()
    service.assert_restore_allowed = MagicMock()
    registry = MagicMock()
    restore = MagicMock(return_value=WorkflowChangeResult(revision=8, workflow=_snapshot(ref)))

    with (
        patch.object(routes, "_get_db", return_value=db),
        patch.object(
            routes,
            "_resolve_workflow_scope_target",
            return_value=(
                {"workflow_id": "orders", "revision": 7},
                SimpleNamespace(account_id="acc-1", actor_name="ana@example.test"),
            ),
        ),
        patch.object(routes, "_assert_workflow_mutation_allowed"),
        patch.object(routes, "_change_reference_access"),
        patch.object(routes, "restore_workflow_snapshot", restore),
        patch.object(routes, "_get_workflow_portability_service", return_value=service),
        patch.object(routes, "_get_registry", return_value=registry),
    ):
        diff = routes.get_workflow_diff("orders", MagicMock())
        restored = routes.restore_workflow_revision(
            "orders",
            routes.RestoreRevisionRequest(entry_id="entry-1", expected_revision=7),
            MagicMock(),
        )

    assert diff["changes"] == [_diff(ref)["changes"][1]]
    assert "default_secret_ref" not in json.dumps(restored)
    assert restored["revision"] == 8
    assert restore.call_args.kwargs["author"] == "ana@example.test"
    assert restore.call_args.kwargs["expected_revision"] == 7


def test_generic_changelog_diff_and_restore_routes_redact_secret_authority() -> None:
    from core.entrypoints.admin import changelog as routes
    from core.entrypoints.admin import workflows_routes_history as history_routes

    ref = "workflow-input:v1:owned:generic"
    db = MagicMock()
    db.get_workflow.return_value = {"workflow_id": "orders", "revision": 7}
    db.diff_entity_history.return_value = _diff(ref)
    db.get_changelog_entry.return_value = {"id": "entry-1", "snapshot": _snapshot(ref)}
    service = WorkflowPortabilityService(db)
    service.assert_history_diff_allowed = MagicMock()
    service.assert_restore_allowed = MagicMock()
    request = MagicMock()
    scope = SimpleNamespace(account_id="acc-1", actor_id="actor-1", actor_name="actor")
    restore = MagicMock(return_value=WorkflowChangeResult(revision=8, workflow=_snapshot(ref)))

    with (
        patch.object(routes, "_get_db", return_value=db),
        patch.object(routes, "_request_scope", return_value=scope),
        patch.object(routes, "_assert_entity_scope"),
        patch.object(routes, "_require_admin_request"),
        patch.object(routes, "_lookup_entity_history_snapshot", return_value=_snapshot(ref)),
        patch.object(routes, "_get_workflow_portability_service", return_value=service),
        patch.object(routes, "_get_registry", return_value=MagicMock()),
        patch.object(history_routes, "_get_db", return_value=db),
        patch.object(
            history_routes,
            "_resolve_workflow_scope_target",
            return_value=({"workflow_id": "orders", "revision": 7}, scope),
        ),
        patch.object(history_routes, "_assert_workflow_mutation_allowed"),
        patch.object(history_routes, "_change_reference_access"),
        patch.object(history_routes, "restore_workflow_snapshot", restore),
        patch.object(history_routes, "_get_workflow_portability_service", return_value=service),
        patch.object(history_routes, "_get_registry", return_value=MagicMock()),
    ):
        diff = routes.get_entity_diff(request, "workflow", "orders")
        restored = routes.restore_entity_revision(
            request,
            "workflow",
            "orders",
            routes.RestoreRevisionRequest(entry_id="entry-1", expected_revision=7),
        )

    assert diff["changes"] == [_diff(ref)["changes"][1]]
    assert "default_secret_ref" not in json.dumps(restored)
    assert restored["entity_type"] == "workflow"
    assert restored["revision"] == 8
    assert restore.call_args.kwargs["author"] == "actor"


# --- both workflow diff HTTP paths mask credential values the same way ----------


def _plain(label: str) -> str:
    """A value that is sensitive only because of the field it sits in."""
    return "-".join(("runtime", label, "private", "value"))


def _credential_diff() -> dict:
    """A raw diff as the history store returns it, before any response projection."""
    return {
        "entity_type": "workflow",
        "entity_id": "orders",
        "from_entry_id": "entry-1",
        "to_entry_id": "entry-2",
        "changes": [
            {
                "path": 'steps["a.b"].override.headers.password',
                "change": "changed",
                "from": _plain("old-password"),
                "to": _plain("new-password"),
            },
            {
                "path": 'steps["fetch"].override.headers.Authorization',
                "change": "changed",
                "from": _plain("old-authorization"),
                "to": _plain("new-authorization"),
            },
            {
                "path": 'steps["fetch"].override.api_token',
                "change": "added",
                "from": None,
                "to": _plain("api-token"),
            },
            {
                "path": _ESCAPED_ANCESTOR_PATH,
                "change": "removed",
                "from": _plain("ancestor"),
                "to": None,
            },
            {
                "path": "settings.callable.expected_inputs[0].default_secret_ref",
                "change": "changed",
                "from": "workflow-input:v1:owned:old",
                "to": "workflow-input:v1:owned:new",
            },
            {"path": "description", "change": "changed", "from": "Before", "to": "After"},
        ],
        "summary": {"total": 6, "added": 1, "removed": 1, "changed": 4},
    }


#: A step id with a dot, a quote and a backslash, under a sensitive ancestor
#: whose leaf name alone is safe.
_ESCAPED_ANCESTOR_PATH = "steps[" + json.dumps('q"x\\y.z') + "].override.credentials.username"

_SAFE_CHANGES = [
    {
        "path": 'steps["a.b"].override.headers.password',
        "change": "changed",
        "from": "[REDACTED]",
        "to": "[REDACTED]",
    },
    {
        "path": 'steps["fetch"].override.headers.Authorization',
        "change": "changed",
        "from": "[REDACTED]",
        "to": "[REDACTED]",
    },
    {
        "path": 'steps["fetch"].override.api_token',
        "change": "added",
        "from": None,
        "to": "[REDACTED]",
    },
    {"path": _ESCAPED_ANCESTOR_PATH, "change": "removed", "from": "[REDACTED]", "to": None},
    {"path": "description", "change": "changed", "from": "Before", "to": "After"},
]


def _assert_safe_diff(result: dict) -> None:
    assert result["changes"] == _SAFE_CHANGES
    assert result["summary"] == {"total": 5, "added": 1, "removed": 1, "changed": 3}
    encoded = json.dumps(result)
    for label in (
        "old-password",
        "new-password",
        "old-authorization",
        "new-authorization",
        "api-token",
        "ancestor",
    ):
        assert _plain(label) not in encoded
    assert "workflow-input:v1:owned" not in encoded


def test_dedicated_workflow_diff_masks_password_values() -> None:
    from core.entrypoints.admin import workflows_routes_history as routes

    stored = {
        "changes": [
            {
                "path": 'steps["a.b"].override.headers.password',
                "change": "changed",
                "from": _plain("control-old"),
                "to": _plain("control-new"),
            }
        ],
        "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
    }
    original = deepcopy(stored)
    db = MagicMock()
    db.diff_entity_history.return_value = stored
    service = WorkflowPortabilityService(db)
    service.assert_history_diff_allowed = MagicMock()

    with (
        patch.object(routes, "_get_db", return_value=db),
        patch.object(routes, "_resolve_workflow_scope_target"),
        patch.object(routes, "_get_workflow_portability_service", return_value=service),
    ):
        result = routes.get_workflow_diff("orders", MagicMock(), "entry-1", "entry-2")

    assert result["changes"] == [
        {
            "path": 'steps["a.b"].override.headers.password',
            "change": "changed",
            "from": "[REDACTED]",
            "to": "[REDACTED]",
        }
    ]
    assert _plain("control-old") not in json.dumps(result)
    assert _plain("control-new") not in json.dumps(result)
    assert stored == original


def test_dedicated_lifecycle_diff_keeps_empty_changes_and_summary() -> None:
    from core.entrypoints.admin import workflows_routes_history as routes

    db = MagicMock()
    db.diff_entity_history.return_value = {
        "comparison_kind": "lifecycle",
        "changes": [],
        "summary": {},
    }
    service = WorkflowPortabilityService(db)
    service.assert_history_diff_allowed = MagicMock()

    with (
        patch.object(routes, "_get_db", return_value=db),
        patch.object(routes, "_resolve_workflow_scope_target"),
        patch.object(routes, "_get_workflow_portability_service", return_value=service),
    ):
        result = routes.get_workflow_diff("orders", MagicMock(), "entry-1", "entry-2")

    assert result == {"comparison_kind": "lifecycle", "changes": [], "summary": {}}


class _HistoryDiffDB:
    """The registry store both diff routes read: one workflow, its entries, a raw diff."""

    dsn = ""

    def __init__(self, raw_diff: dict) -> None:
        self.raw_diff = raw_diff
        self.diff_calls: list[dict] = []

    def get_workflow(self, workflow_id: str, **_filters) -> dict | None:
        if workflow_id != "orders":
            return None
        return {
            "workflow_id": "orders",
            "display_name": "Orders",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": False,
            "revision": 2,
            "settings": {},
            "steps": [],
            "groups": [],
            "for_each": [],
        }

    def get_changelog_entry(self, entry_id: str, **_filters) -> dict:
        return {
            "id": entry_id,
            "entity_type": "workflow",
            "entity_id": "orders",
            "action": "update",
            "snapshot": {"workflow_id": "orders", "account_id": DEFAULT_ACCOUNT_ID},
        }

    def get_entity_history(self, *_args, **_kwargs) -> list:
        return []

    def diff_entity_history(self, **kwargs) -> dict:
        self.diff_calls.append(kwargs)
        # The stored payload itself, not a copy: a projection that mutates it fails.
        return self.raw_diff


@pytest.fixture
def history_diff_client():
    """The real app over a scoped registry store; a setup failure fails the test."""
    from fastapi.testclient import TestClient

    from core.entrypoints.admin import changelog as changelog_routes
    from core.entrypoints.admin import workflows_routes_history as history_routes
    from core.entrypoints.api.app_factory import create_app

    db = _HistoryDiffDB(_credential_diff())
    app = create_app()
    app.state.local_auth_enabled_override = lambda: False
    app.state.external_auth_enabled_override = lambda: False
    with (
        patch.object(changelog_routes, "_get_db", return_value=db),
        patch.object(history_routes, "_get_db", return_value=db),
    ):
        yield TestClient(app, raise_server_exceptions=False), db


@pytest.mark.parametrize(
    "url",
    ["/api/workflows/orders/diff", "/api/changelog/workflow/orders/diff"],
    ids=["dedicated", "generic"],
)
def test_workflow_diff_http_routes_apply_the_same_redaction(history_diff_client, url) -> None:
    client, db = history_diff_client
    original = deepcopy(db.raw_diff)
    params = {"from_entry_id": "entry-1", "to_entry_id": "entry-2"}

    first = client.get(url, params=params)
    second = client.get(url, params=params)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert db.diff_calls == [
        {
            "entity_type": "workflow",
            "entity_id": "orders",
            "from_entry_id": "entry-1",
            "to_entry_id": "entry-2",
        }
    ] * 2
    _assert_safe_diff(first.json())
    assert second.json() == first.json()
    assert db.raw_diff == original


def test_both_workflow_diff_routes_return_the_same_safe_diff(history_diff_client) -> None:
    client, _db = history_diff_client
    params = {"from_entry_id": "entry-1", "to_entry_id": "entry-2"}

    dedicated = client.get("/api/workflows/orders/diff", params=params)
    generic = client.get("/api/changelog/workflow/orders/diff", params=params)

    assert dedicated.status_code == 200, dedicated.text
    assert generic.status_code == 200, generic.text
    assert dedicated.json()["changes"] == generic.json()["changes"]
    assert dedicated.json()["summary"] == generic.json()["summary"]
