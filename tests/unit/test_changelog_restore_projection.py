from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

from starlette.requests import Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.entrypoints.admin import changelog as changelog_routes


def _admin_request() -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/changelog/tool/owned-tool/restore",
            "raw_path": b"/api/changelog/tool/owned-tool/restore",
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
    return "-".join(("runtime", label, "private", "value"))


def test_nonworkflow_restore_redacts_only_the_response_snapshot() -> None:
    restored_snapshot = {
        "tool_id": "owned-tool",
        "display_name": "Owned tool",
        "account_id": DEFAULT_ACCOUNT_ID,
        "implementation": {
            "kind": "http",
            "auth": {"api_key": _ordinary_credential("api-key")},
        },
        "config": {
            "password": _ordinary_credential("password"),
            "credential_secret_ref": "00000000-0000-4000-8000-000000000301",
            "timeout_seconds": 30,
        },
    }
    history_entry = {
        "id": "entry-7",
        "entity_type": "tool",
        "entity_id": "owned-tool",
        "snapshot": copy.deepcopy(restored_snapshot),
    }
    original_history = copy.deepcopy(history_entry)
    original_restored = copy.deepcopy(restored_snapshot)
    validation = {"valid": True, "errors": [], "warnings": []}
    original_validation = copy.deepcopy(validation)
    db = MagicMock()
    db.get_changelog_entry.return_value = history_entry
    db.restore_entity_revision.return_value = {
        "validation": validation,
        "restored": restored_snapshot,
    }
    registry = MagicMock()

    with (
        patch.object(changelog_routes, "_get_db", return_value=db),
        patch.object(changelog_routes, "_get_registry", return_value=registry),
    ):
        response = changelog_routes.restore_entity_revision(
            _admin_request(),
            "tool",
            "owned-tool",
            changelog_routes.RestoreRevisionRequest(entry_id="entry-7"),
        )

    assert response == {
        "status": "restored",
        "entity_type": "tool",
        "entity_id": "owned-tool",
        "restored_from_entry_id": "entry-7",
        "new_snapshot": {
            "tool_id": "owned-tool",
            "display_name": "Owned tool",
            "account_id": DEFAULT_ACCOUNT_ID,
            "implementation": {
                "kind": "http",
                "auth": {"api_key": "[REDACTED]"},
            },
            "config": {
                "password": "[REDACTED]",
                "credential_secret_ref": "00000000-0000-4000-8000-000000000301",
                "timeout_seconds": 30,
            },
        },
        "validation": original_validation,
    }
    assert history_entry == original_history
    assert restored_snapshot == original_restored
    assert validation == original_validation
    db.get_changelog_entry.assert_called_once_with(
        "entry-7",
        entity_type="tool",
        entity_id="owned-tool",
    )
    db.restore_entity_revision.assert_called_once()
    restore_call = db.restore_entity_revision.call_args
    assert restore_call.kwargs["entity_type"] == "tool"
    assert restore_call.kwargs["entity_id"] == "owned-tool"
    assert restore_call.kwargs["entry_id"] == "entry-7"
    assert restore_call.kwargs["author"] == "admin_restore"
    assert callable(restore_call.kwargs["validate_restored"])
    registry.reload_all.assert_called_once_with()
