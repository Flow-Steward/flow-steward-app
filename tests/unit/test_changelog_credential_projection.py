"""Credential-safe response projections for the generic changelog API."""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.changelog_projection import redact_changelog_diff
from core.entrypoints.admin import changelog as changelog_routes


def _admin_request(path: str = "/api/changelog") -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 443),
            "root_path": "",
        }
    )
    request.state.auth_user = {
        "id": "admin-1",
        "username": "admin",
        "roles": ["admin"],
        "account_id": DEFAULT_ACCOUNT_ID,
        "principal_type": "user",
    }
    return request


def _ordinary_credential(label: str) -> str:
    """Build a non-signature value whose sensitivity comes only from its field."""
    return "-".join(("runtime", label, "private", "value"))


@pytest.mark.parametrize(
    "extra", [{}, {"changes": None}, {"changes": {}}, {"changes": []}, {"changes": "not-a-list"}]
)
def test_diff_projection_preserves_missing_or_non_actionable_changes(extra):
    payload = {"config": {"password": _ordinary_credential("diff-password")}, **extra}
    expected = {"config": {"password": "[REDACTED]"}, **extra}
    original = copy.deepcopy(payload)

    assert redact_changelog_diff(payload) == expected
    assert payload == original


def test_diff_projection_keeps_mixed_entries_and_missing_credential_side():
    payload = {
        "changes": [
            "kept-entry",
            {"path": "config.password", "from": None, "to": _ordinary_credential("new")},
        ]
    }
    original = copy.deepcopy(payload)

    assert redact_changelog_diff(payload) == {
        "changes": ["kept-entry", {"path": "config.password", "from": None, "to": "[REDACTED]"}]
    }
    assert payload == original


def _tool_entry() -> dict:
    return {
        "id": "entry-tool-1",
        "entity_type": "tool",
        "entity_id": "owned-tool",
        "action": "update",
        "snapshot": {
            "tool_id": "owned-tool",
            "display_name": "Owned tool",
            "implementation": {
                "kind": "http",
                "auth": {"api_key": _ordinary_credential("api-key")},
            },
            "config": {
                "password": _ordinary_credential("password"),
                "credential_secret_ref": "00000000-0000-4000-8000-000000000301",
                "timeout_seconds": 30,
            },
        },
    }


def _assert_tool_entry_redacted(entry: dict, original: dict) -> None:
    snapshot = entry["snapshot"]
    assert snapshot["implementation"]["auth"]["api_key"] == "[REDACTED]"
    assert snapshot["config"]["password"] == "[REDACTED]"
    assert snapshot["config"]["credential_secret_ref"] == "00000000-0000-4000-8000-000000000301"
    assert snapshot["config"]["timeout_seconds"] == 30
    assert {key: value for key, value in entry.items() if key != "snapshot"} == {
        key: value for key, value in original.items() if key != "snapshot"
    }
    assert snapshot["tool_id"] == "owned-tool"
    assert snapshot["display_name"] == "Owned tool"
    assert snapshot["implementation"]["kind"] == "http"
    encoded = json.dumps(entry)
    assert _ordinary_credential("api-key") not in encoded
    assert _ordinary_credential("password") not in encoded


@pytest.mark.parametrize("filtered", [False, True], ids=["global", "entity-filtered"])
def test_global_changelog_redacts_response_copy_without_mutating_store_snapshot(
    filtered: bool,
) -> None:
    stored_entry = _tool_entry()
    original = copy.deepcopy(stored_entry)
    db = MagicMock()
    db.get_changelog.return_value = [stored_entry]

    with patch.object(changelog_routes, "_get_db", return_value=db):
        response = changelog_routes.get_changelog(
            _admin_request(),
            entity_type="tool" if filtered else None,
            entity_id="owned-tool" if filtered else None,
        )

    assert len(response) == 1
    _assert_tool_entry_redacted(response[0], original)
    encoded = json.dumps(response)
    assert _ordinary_credential("api-key") not in encoded
    assert _ordinary_credential("password") not in encoded
    db.get_changelog.assert_called_once_with(
        entity_type="tool" if filtered else None,
        entity_id="owned-tool" if filtered else None,
        limit=50,
    )
    assert stored_entry == original


@pytest.mark.parametrize("surface", ["history", "revision"])
def test_entity_history_surfaces_redact_response_copy_without_mutating_store_snapshot(
    surface: str,
) -> None:
    stored_entry = _tool_entry()
    original = copy.deepcopy(stored_entry)
    db = MagicMock()
    db.get_entity_history.return_value = [stored_entry]
    db.get_entity_revision.return_value = stored_entry
    request = _admin_request("/api/changelog/tool/owned-tool/history")

    with patch.object(changelog_routes, "_get_db", return_value=db):
        if surface == "history":
            rows = changelog_routes.get_entity_history(
                request,
                "tool",
                "owned-tool",
            )
            assert len(rows) == 1
            response = rows[0]
        else:
            response = changelog_routes.get_entity_revision(
                request,
                "tool",
                "owned-tool",
                1,
            )

    _assert_tool_entry_redacted(response, original)
    assert stored_entry == original


def test_changelog_reference_field_does_not_bypass_credential_text_scrubbing() -> None:
    stored_entry = _tool_entry()
    credential_text = "ghp_" + "x" * 36
    stored_entry["snapshot"]["config"]["credential_secret_ref"] = credential_text
    stored_entry["snapshot"]["config"]["note"] = credential_text
    original = copy.deepcopy(stored_entry)
    db = MagicMock()
    db.get_changelog.return_value = [stored_entry]

    with patch.object(changelog_routes, "_get_db", return_value=db):
        response = changelog_routes.get_changelog(_admin_request())

    assert response[0]["snapshot"]["config"]["credential_secret_ref"] == "[REDACTED]"
    assert response[0]["snapshot"]["config"]["note"] == "[REDACTED]"
    assert credential_text not in json.dumps(response)
    assert stored_entry == original


@pytest.mark.parametrize(
    ("path", "change", "has_old", "has_new", "expected_from", "expected_to", "summary"),
    [
        pytest.param(
            "implementation.auth.api_key",
            "changed",
            True,
            True,
            "[REDACTED]",
            "[REDACTED]",
            {"total": 3, "added": 0, "removed": 0, "changed": 3},
            id="changed-credential",
        ),
        pytest.param(
            "config.password",
            "added",
            False,
            True,
            None,
            "[REDACTED]",
            {"total": 3, "added": 1, "removed": 0, "changed": 2},
            id="added-credential",
        ),
        pytest.param(
            "implementation.auth.api_key",
            "removed",
            True,
            False,
            "[REDACTED]",
            None,
            {"total": 3, "added": 0, "removed": 1, "changed": 2},
            id="removed-credential",
        ),
        pytest.param(
            "implementation.auth[0].api_key",
            "changed",
            True,
            True,
            "[REDACTED]",
            "[REDACTED]",
            {"total": 3, "added": 0, "removed": 0, "changed": 3},
            id="indexed-container-credential",
        ),
        pytest.param(
            "config.password[0]",
            "changed",
            True,
            True,
            "[REDACTED]",
            "[REDACTED]",
            {"total": 3, "added": 0, "removed": 0, "changed": 3},
            id="indexed-sensitive-field",
        ),
    ],
)
def test_entity_diff_redacts_values_by_sensitive_flattened_path_without_mutating_store_payload(
    path: str,
    change: str,
    has_old: bool,
    has_new: bool,
    expected_from: str | None,
    expected_to: str | None,
    summary: dict,
) -> None:
    old_credential = _ordinary_credential("old-api-key")
    new_credential = _ordinary_credential("new-api-key")
    stored_diff = {
        "entity_type": "tool",
        "entity_id": "owned-tool",
        "from_entry_id": "entry-1",
        "to_entry_id": "entry-2",
        "from_created_at": "2026-10-01T12:00:00Z",
        "to_created_at": "2026-10-02T12:00:00Z",
        "changes": [
            {
                "path": path,
                "change": change,
                "from": old_credential if has_old else None,
                "to": new_credential if has_new else None,
            },
            {
                "path": "config.credential_secret_ref",
                "change": "changed",
                "from": "00000000-0000-4000-8000-000000000301",
                "to": "00000000-0000-4000-8000-000000000302",
            },
            {
                "path": "config.timeout_seconds",
                "change": "changed",
                "from": 15,
                "to": 30,
            },
        ],
        "summary": dict(summary),
    }
    original = copy.deepcopy(stored_diff)
    db = MagicMock()
    db.diff_entity_history.return_value = stored_diff

    with patch.object(changelog_routes, "_get_db", return_value=db):
        response = changelog_routes.get_entity_diff(
            _admin_request("/api/changelog/tool/owned-tool/diff"),
            "tool",
            "owned-tool",
        )

    assert response["changes"] == [
        {
            "path": path,
            "change": change,
            "from": expected_from,
            "to": expected_to,
        },
        original["changes"][1],
        original["changes"][2],
    ]
    assert {key: value for key, value in response.items() if key != "changes"} == {
        key: value for key, value in original.items() if key != "changes"
    }
    encoded = json.dumps(response)
    assert old_credential not in encoded
    assert new_credential not in encoded
    assert stored_diff == original


@pytest.mark.parametrize(
    ("path", "expected_value"),
    [
        pytest.param(
            "config.credential_secret_ref[0]",
            "runtime-semantic-field-private-value",
            id="indexed-reference-is-not-a-credential",
        ),
        pytest.param(
            "config.token_env_var",
            "runtime-semantic-field-private-value",
            id="exact-environment-variable-field-exemption",
        ),
        pytest.param(
            "config.password.child_ref",
            "[REDACTED]",
            id="sensitive-ancestor-overrides-leaf-reference-exemption",
        ),
    ],
)
def test_entity_diff_preserves_nested_credential_field_semantics(
    path: str, expected_value: str
) -> None:
    value = _ordinary_credential("semantic-field")
    stored_diff = {
        "entity_type": "tool",
        "entity_id": "owned-tool",
        "from_entry_id": "entry-1",
        "to_entry_id": "entry-2",
        "changes": [{"path": path, "change": "added", "from": None, "to": value}],
        "summary": {"total": 1, "added": 1, "removed": 0, "changed": 0},
    }
    original = copy.deepcopy(stored_diff)
    db = MagicMock()
    db.diff_entity_history.return_value = stored_diff

    with patch.object(changelog_routes, "_get_db", return_value=db):
        response = changelog_routes.get_entity_diff(
            _admin_request("/api/changelog/tool/owned-tool/diff"),
            "tool",
            "owned-tool",
        )

    assert response == {
        "entity_type": "tool",
        "entity_id": "owned-tool",
        "from_entry_id": "entry-1",
        "to_entry_id": "entry-2",
        "changes": [{"path": path, "change": "added", "from": None, "to": expected_value}],
        "summary": {"total": 1, "added": 1, "removed": 0, "changed": 0},
    }
    if expected_value == "[REDACTED]":
        assert value not in json.dumps(response)
    assert stored_diff == original


def test_workflow_history_keeps_specialized_secret_authority_projection() -> None:
    secret_ref = ":".join(("workflow-input", "v1", "owned", "history"))
    stored_entry = {
        "id": "entry-workflow-1",
        "entity_type": "workflow",
        "entity_id": "owned-workflow",
        "snapshot": {
            "workflow_id": "owned-workflow",
            "settings": {
                "callable": {
                    "expected_inputs": [
                        {
                            "name": "login",
                            "type": "secret",
                            "default_secret_ref": secret_ref,
                        }
                    ]
                }
            },
        },
    }
    original = copy.deepcopy(stored_entry)
    db = MagicMock()
    db.get_entity_history.return_value = [stored_entry]
    db.get_workflow.return_value = stored_entry["snapshot"]

    with patch.object(changelog_routes, "_get_db", return_value=db):
        rows = changelog_routes.get_entity_history(
            _admin_request("/api/changelog/workflow/owned-workflow/history"),
            "workflow",
            "owned-workflow",
        )

    assert len(rows) == 1
    response = rows[0]
    projected_input = response["snapshot"]["settings"]["callable"]["expected_inputs"][0]
    assert "default_secret_ref" not in projected_input
    assert projected_input["default_configured"] is True
    assert projected_input["name"] == "login"
    assert projected_input["type"] == "secret"
    assert secret_ref not in json.dumps(response)
    assert {key: value for key, value in response.items() if key != "snapshot"} == {
        key: value for key, value in original.items() if key != "snapshot"
    }
    assert stored_entry == original
