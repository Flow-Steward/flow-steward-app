"""A changelog restore names the caller's account and writes only its own rows.

ADR 0020, C5: every changelog read names an account (there is no
installation-wide read), a restore is refused unless the live row belongs to the
caller's account (a platform object needs an installation administrator), and the
restored row keeps the owner of the live row, never the installation account by
default. An administrator of another account is held to its own account.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.db_store_revisions_mixin import RestoreOwnershipError
from core.application.registry.workflow_account import WorkflowAccountMissingError
from core.entrypoints.admin import changelog as changelog_routes

_TOOL_ENTRY = {
    "id": "entry-1",
    "entity_type": "tool",
    "entity_id": "shared-tool",
    "action": "update",
    "account_id": "account-b",
    "snapshot": {
        "tool_id": "shared-tool",
        "display_name": "Old name",
        # An old snapshot may name no account, or the installation account.
        "account_id": DEFAULT_ACCOUNT_ID,
        "scope_kind": "system",
    },
}


def _store(live: dict | None) -> RegistryDBStore:
    db = RegistryDBStore("postgres://unused")
    db.get_changelog_entry = MagicMock(return_value=dict(_TOOL_ENTRY))
    db._live_registry_ownership = MagicMock(return_value=live)
    db.apply_entity_snapshot = MagicMock(side_effect=lambda *a, **k: dict(a[2]))

    @contextmanager
    def _tx():
        yield object()

    db._transaction = _tx
    return db


def test_changelog_read_without_any_account_is_refused_not_installation_wide():
    clauses: list[str] = []
    params: list[object] = []
    with pytest.raises(WorkflowAccountMissingError):
        RegistryDBStore._append_changelog_account_clause(clauses, params, "tool", None)
    assert clauses == [] and params == []


def test_changelog_read_with_an_account_sees_its_own_and_platform_entries():
    clauses: list[str] = []
    params: list[object] = []
    RegistryDBStore._append_changelog_account_clause(clauses, params, "tool", "account-b")
    assert clauses == ["(account_id::text = %s OR account_id IS NULL)"]
    assert params == ["account-b"]


def test_restore_refuses_another_accounts_live_row():
    db = _store({"account_id": "account-a", "project_id": None, "scope_kind": "system"})

    with pytest.raises(RestoreOwnershipError):
        db.restore_entity_revision(
            entity_type="tool", entity_id="shared-tool", entry_id="entry-1", account_id="account-b"
        )
    db.apply_entity_snapshot.assert_not_called()
    db.get_changelog_entry.assert_called_once_with(
        "entry-1", entity_type="tool", entity_id="shared-tool", account_id="account-b"
    )


def test_restore_keeps_the_live_owner_not_the_snapshot_or_installation_account():
    db = _store(
        {
            "account_id": "account-b",
            "project_id": None,
            "scope_kind": "system",
            "source_install_id": None,
        }
    )

    db.restore_entity_revision(
        entity_type="tool", entity_id="shared-tool", entry_id="entry-1", account_id="account-b"
    )

    args, kwargs = db.apply_entity_snapshot.call_args
    assert kwargs["account_id"] == "account-b"
    assert args[2]["account_id"] == "account-b"
    assert args[2]["display_name"] == "Old name"


def test_platform_object_restore_needs_an_installation_administrator():
    live = {
        "account_id": None,
        "project_id": None,
        "scope_kind": "system",
        "source_install_id": "install-1",
    }
    db = _store(live)
    with pytest.raises(RestoreOwnershipError):
        db.restore_entity_revision(
            entity_type="tool", entity_id="shared-tool", entry_id="entry-1", account_id="account-b"
        )
    db.apply_entity_snapshot.assert_not_called()

    db = _store(live)
    db.restore_entity_revision(
        entity_type="tool",
        entity_id="shared-tool",
        entry_id="entry-1",
        account_id=DEFAULT_ACCOUNT_ID,
        installation_admin=True,
    )
    args, kwargs = db.apply_entity_snapshot.call_args
    # A platform object stays without an account; it never becomes the installation's.
    assert kwargs["account_id"] is None
    assert args[2]["account_id"] is None
    assert args[2]["source_install_id"] == "install-1"


def test_tool_snapshot_without_owner_is_written_without_the_installation_default():
    db = RegistryDBStore("postgres://unused")
    db.save_tool = MagicMock()
    db.get_tool = MagicMock(return_value=None)

    db.apply_entity_snapshot(
        "tool",
        "shared-tool",
        {"tool_id": "shared-tool", "source_install_id": "install-1"},
        account_id=None,
    )

    assert db.save_tool.call_args.args[0]["account_id"] is None


def _agent_upsert_sql(db: RegistryDBStore) -> str:
    executed: list[str] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append(str(sql))

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    @contextmanager
    def _tx():
        yield _Cursor()

    db._transaction = _tx
    db._exists = MagicMock(return_value=True)
    db._column_exists = MagicMock(return_value=True)
    db._lock_agent_relationship_parent = MagicMock(return_value={"account_id": "account-b"})
    db._log_change_cur = MagicMock()
    db._trigger_public_contract_projection_sync = MagicMock()
    db.save_agent(
        {"agent_id": "writer", "account_id": DEFAULT_ACCOUNT_ID, "prompts": {"system": "x"}},
        sync_relationships=False,
    )
    return next(sql for sql in executed if "INSERT INTO registry_agents" in sql)


def test_agent_upsert_never_moves_the_live_row_into_another_account():
    sql = _agent_upsert_sql(RegistryDBStore("postgres://unused"))
    assert "account_id" in sql.split("VALUES")[0]
    assert "account_id = EXCLUDED.account_id" not in sql


def _request(account_id: str, roles: list[str]) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/changelog/tool/shared-tool/restore",
            "raw_path": b"/api/changelog/tool/shared-tool/restore",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 443),
            "root_path": "",
        }
    )
    request.state.auth_user = {
        "id": f"admin-{account_id}",
        "username": f"admin-{account_id}",
        "roles": roles,
        "account_id": account_id,
        "principal_type": "user",
    }
    return request


def test_restore_route_always_passes_the_callers_account():
    db = MagicMock()
    db.get_changelog_entry.return_value = dict(_TOOL_ENTRY, snapshot={"account_id": "account-b"})
    db.get_tool.return_value = {"tool_id": "shared-tool", "account_id": "account-b"}
    db.restore_entity_revision.return_value = {
        "validation": {"valid": True, "errors": [], "warnings": []},
        "restored": {"tool_id": "shared-tool"},
    }
    with (
        patch.object(changelog_routes, "_get_db", return_value=db),
        patch.object(changelog_routes, "_get_registry", return_value=MagicMock()),
    ):
        changelog_routes.restore_entity_revision(
            _request("account-b", ["admin"]),
            "tool",
            "shared-tool",
            changelog_routes.RestoreRevisionRequest(entry_id="entry-1"),
        )

    kwargs = db.restore_entity_revision.call_args.kwargs
    assert kwargs["account_id"] == "account-b"
    assert kwargs["installation_admin"] is False


def test_admin_of_another_account_is_held_to_its_own_account():
    db = MagicMock()
    db.get_tool.return_value = {"tool_id": "shared-tool", "account_id": "account-a"}
    with pytest.raises(HTTPException) as refused:
        changelog_routes._assert_entity_scope(
            _request("account-b", ["admin"]),
            db,
            entity_type="tool",
            entity_id="shared-tool",
        )
    assert refused.value.status_code in {403, 404}


def test_installation_admin_keeps_access_to_any_account():
    db = MagicMock()
    db.get_tool.return_value = {"tool_id": "shared-tool", "account_id": "account-a"}
    changelog_routes._assert_entity_scope(
        _request(DEFAULT_ACCOUNT_ID, ["admin"]),
        db,
        entity_type="tool",
        entity_id="shared-tool",
    )


def test_restore_checks_the_owner_inside_the_write_transaction_with_the_row_locked():
    """The ownership check and the write share one transaction (no check-then-write race)."""
    db = _store(None)
    events: list[str] = []

    @contextmanager
    def _tx():
        events.append("begin")
        yield object()
        events.append("commit")

    def _live(entity_type, entity_id, *, for_update=False):
        events.append(f"ownership for_update={for_update}")
        return {"account_id": "account-b", "project_id": None, "scope_kind": "system"}

    db._transaction = _tx
    db._live_registry_ownership = MagicMock(side_effect=_live)
    db.apply_entity_snapshot = MagicMock(
        side_effect=lambda *a, **k: events.append("write") or dict(a[2])
    )

    db.restore_entity_revision(
        entity_type="tool", entity_id="shared-tool", entry_id="entry-1", account_id="account-b"
    )

    assert events == ["begin", "ownership for_update=True", "write", "commit"]


def test_a_refused_owner_inside_the_transaction_is_still_an_ownership_error():
    db = _store({"account_id": "account-a", "project_id": None, "scope_kind": "system"})

    with pytest.raises(RestoreOwnershipError):
        db.restore_entity_revision(
            entity_type="tool", entity_id="shared-tool", entry_id="entry-1", account_id="account-b"
        )
    db.apply_entity_snapshot.assert_not_called()


def test_the_live_owner_is_read_with_a_row_lock_when_asked():
    db = RegistryDBStore("postgres://unused")
    executed: list[str] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append(str(sql))

        def fetchone(self):
            return {"account_id": "account-b"}

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor
    db._column_exists = MagicMock(return_value=True)

    db._live_registry_ownership("tool", "shared-tool", for_update=True)
    db._live_registry_ownership("tool", "shared-tool")

    assert executed[0].endswith("WHERE tool_id = %s FOR UPDATE")
    assert executed[1].endswith("WHERE tool_id = %s")
