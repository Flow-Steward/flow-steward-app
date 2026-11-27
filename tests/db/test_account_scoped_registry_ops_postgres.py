"""Registry history, trash, search and projection work per account (ADR 0020, C5).

On a real database two accounts own a workflow, a field and a data contract with
the same keys. Each account reads, compares and restores only its own history;
trash and restore through the trash service touch only the caller's row; a search
returns the caller's workflow; the projection writes derived fields only into the
account that owns the source; a read or write that names no account is refused; the
database refuses a workflow, field or contract history entry without an account;
and the migration gives older entries their account and runs again. Every test runs
on the disposable audit database inside a transaction that is rolled back.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.application.management.trash_account_scope import trash_account_predicate
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.public_contract_projection import (
    reconcile_public_contract_projection,
)
from core.application.registry.workflow_account import (
    WorkflowAccountMissingError,
    workflow_account_scope,
)
from core.infrastructure.db import psycopg_runtime

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261127090000_account_scoped_registry_ops.sql"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class Registry:
    conn: Any
    store: RegistryDBStore
    a: str
    b: str
    key: str


def _account(conn) -> str:
    account_id = str(uuid.uuid4())
    suffix = uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, TENANT_ID, f"acme-{suffix}", f"Acme {suffix}"),
    )
    return account_id


@pytest.fixture
def registry(disposable_db) -> Iterator[Registry]:
    conn = psycopg_runtime.connect(disposable_db.dsn)
    conn.autocommit = False
    store = RegistryDBStore(disposable_db.dsn)
    # Every store read and write joins this one transaction, rolled back below.
    store._set_tx_conn(conn)
    try:
        yield Registry(
            conn,
            store,
            _account(conn),
            _account(conn),
            f"acme.invoices_{uuid.uuid4().hex[:8]}",
        )
    finally:
        store._set_tx_conn(None)
        conn.rollback()
        conn.close()


def _workflow(key: str, display_name: str) -> dict:
    return {
        "workflow_id": key,
        "display_name": display_name,
        "enabled": False,
        "steps": [],
        "groups": [],
        "for_each": [],
    }


def _rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in cur]


def _save_workflow(registry: Registry, account_id: str, display_name: str) -> None:
    with workflow_account_scope(account_id):
        registry.store.save_workflow(_workflow(registry.key, display_name), author="test")


def test_workflow_history_is_kept_per_account(registry: Registry) -> None:
    store, a, b, key = registry.store, registry.a, registry.b, registry.key
    _save_workflow(registry, a, "A first")
    _save_workflow(registry, a, "A second")
    _save_workflow(registry, b, "B only")

    history_a = store.get_entity_history("workflow", key, account_id=a)
    history_b = store.get_entity_history("workflow", key, account_id=b)
    # Entries of one transaction share a timestamp, so their order is not compared.
    assert sorted(entry["snapshot"]["display_name"] for entry in history_a) == [
        "A first",
        "A second",
    ]
    assert [entry["snapshot"]["display_name"] for entry in history_b] == ["B only"]
    assert {str(entry["account_id"]) for entry in history_a} == {a}

    # Another account's entry is not found by id, so it can be neither compared
    # nor restored from the wrong account.
    entry_a = str(history_a[0]["id"])
    assert (
        store.get_changelog_entry(entry_a, entity_type="workflow", entity_id=key, account_id=b)
        is None
    )
    with pytest.raises(KeyError):
        store.diff_entity_history(
            "workflow", key, from_entry_id=entry_a, to_entry_id=entry_a, account_id=b
        )
    # The bound account is the default for a history read; nothing bound is refused.
    with workflow_account_scope(b):
        assert len(store.get_entity_history("workflow", key)) == 1
    with pytest.raises(WorkflowAccountMissingError):
        store.get_entity_history("workflow", key)
    # The cross-entity changelog of an account shows its own workflow entries only.
    listed = store.get_changelog(entity_type=None, limit=200, account_id=b)
    assert {str(row["account_id"]) for row in listed if row["entity_type"] == "workflow"} <= {b}


def test_field_and_contract_history_is_kept_per_account(registry: Registry) -> None:
    store, a, b = registry.store, registry.a, registry.b
    contract = {"contract_id": registry.key, "display_name": "lines", "kind": "list"}
    field = {"field_key": registry.key, "display_name": "Lines", "value_type": "text"}
    for account in (a, b):
        store.save_data_contract(contract, account_id=account)
        store.save_field(field, account_id=account)
    store.save_field({**field, "display_name": "A renamed"}, account_id=a)
    store.delete_field(registry.key, account_id=b)

    field_a = store.get_entity_history("field", registry.key, account_id=a)
    field_b = store.get_entity_history("field", registry.key, account_id=b)
    assert sorted(entry["action"] for entry in field_a) == ["create", "update"]
    assert sorted(entry["action"] for entry in field_b) == ["create", "delete"]
    assert len(store.get_entity_history("data_contract", registry.key, account_id=a)) == 1

    owners = _rows(
        registry.conn,
        "SELECT DISTINCT account_id::text FROM registry_changelog "
        "WHERE entity_id = %s AND entity_type IN ('field', 'data_contract')",
        (registry.key,),
    )
    assert sorted(owner for (owner,) in owners) == sorted([a, b])


def test_the_database_refuses_an_unowned_history_entry(registry: Registry) -> None:
    for entity_type in ("workflow", "field", "data_contract"):
        registry.conn.execute("SAVEPOINT unowned")
        with pytest.raises(psycopg.errors.CheckViolation):
            registry.conn.execute(
                "INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot) "
                "VALUES (%s, %s, 'update', '{}'::jsonb)",
                (entity_type, registry.key),
            )
        registry.conn.execute("ROLLBACK TO SAVEPOINT unowned")
    # A platform object's entry may stay without an account.
    registry.conn.execute(
        "INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot) "
        "VALUES ('agent', %s, 'update', '{}'::jsonb)",
        (registry.key,),
    )


class _TrashStore:
    """The trash service's store, on the test transaction."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def _table_exists(self, table: str) -> bool:
        return bool(
            _rows(self.conn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))[0][0]
        )

    def _column_exists(self, table: str, column: str) -> bool:
        return bool(
            _rows(
                self.conn,
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                (table, column),
            )
        )


def _trash_service(registry: Registry) -> TrashLifecycleService:
    store = _TrashStore(registry.conn)
    service = TrashLifecycleService(
        get_logger=lambda: store,
        get_audit_store=lambda: SimpleNamespace(log_action=lambda **_: None),
    )
    service._reload_workflow_registry = lambda: None
    return service


def _admin(account_id: str) -> ScopeContext:
    return ScopeContext(account_id=account_id, actor_id="c5-test", principal_type="admin")


def test_trash_and_restore_touch_only_the_callers_row(registry: Registry) -> None:
    store, a, b = registry.store, registry.a, registry.b
    contract = {"contract_id": registry.key, "display_name": "lines", "kind": "list"}
    for account in (a, b):
        store.save_data_contract(contract, account_id=account)
    service = _trash_service(registry)

    service.trash("registry_data_contract", registry.key, scope=_admin(a))
    assert store.get_data_contract(registry.key, account_id=a) is None
    assert store.get_data_contract(registry.key, account_id=b) is not None

    # B's row is not trashed, and A's trashed row is not B's to restore.
    with pytest.raises(ServiceNotFoundError):
        service.restore("registry_data_contract", registry.key, scope=_admin(b))
    assert store.get_data_contract(registry.key, account_id=a) is None

    service.restore("registry_data_contract", registry.key, scope=_admin(a))
    assert store.get_data_contract(registry.key, account_id=a) is not None
    markers = _rows(
        registry.conn,
        "SELECT action, account_id::text FROM registry_changelog "
        "WHERE entity_type = 'data_contract' AND entity_id = %s "
        "AND action IN ('trash', 'restore')",
        (registry.key,),
    )
    assert sorted(markers) == [("restore", a), ("trash", a)]


def test_a_trashed_workflow_is_restored_in_its_own_account(registry: Registry) -> None:
    store, a, b, key = registry.store, registry.a, registry.b, registry.key
    _save_workflow(registry, a, "A")
    _save_workflow(registry, b, "B")
    for account in (a, b):
        with workflow_account_scope(account):
            store.delete_workflow(key, author="test", account_id=account)

    _trash_service(registry).restore("registry_workflow", key, scope=_admin(b))

    assert store.get_workflow(key, account_id=b) is not None
    assert store.get_workflow(key, account_id=a) is None
    assert store.get_workflow(key, include_trashed=True, account_id=a) is not None


def test_the_trash_listing_predicate_keeps_one_accounts_rows(registry: Registry) -> None:
    store, a, b = registry.store, registry.a, registry.b
    contract = {"contract_id": registry.key, "display_name": "lines", "kind": "list"}
    for account in (a, b):
        store.save_data_contract(contract, account_id=account)
        store.delete_data_contract(registry.key, account_id=account)

    clause, params = trash_account_predicate(
        has_account_id=True,
        has_project_id=False,
        has_workspace_id=False,
        account_id=b,
        installation_account=False,
    )
    rows = _rows(
        registry.conn,
        f"SELECT account_id::text FROM registry_data_contracts "
        f"WHERE deleted_at IS NOT NULL AND contract_id = %s AND {clause}",
        (registry.key, *params),
    )
    assert rows == [(b,)]


def test_search_returns_the_callers_workflow(registry: Registry) -> None:
    _save_workflow(registry, registry.a, "A invoices")
    _save_workflow(registry, registry.b, "B invoices")

    hits = registry.store.search_entities(
        registry.key, entity_type="workflow", account_id=registry.b
    )

    assert [(hit["display_name"], str(hit["account_id"])) for hit in hits] == [
        ("B invoices", registry.b)
    ]
    with pytest.raises(WorkflowAccountMissingError):
        registry.store.search_entities(registry.key, entity_type="workflow")


def test_projection_writes_fields_into_the_sources_account(registry: Registry) -> None:
    conn, store, b = registry.conn, registry.store, registry.b
    skill_id = f"acme.b_skill_{uuid.uuid4().hex[:8]}"
    field_key = f"acme_b_input_{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO registry_skills (skill_id, display_name, account_id) VALUES (%s, 'B', %s)",
        (skill_id, b),
    )
    conn.execute(
        "INSERT INTO registry_skill_inputs (skill_id, name, type) VALUES (%s, %s, 'text')",
        (skill_id, field_key),
    )

    result = reconcile_public_contract_projection(store, apply=True, account_id=b)

    assert result["accounts"][b]["created"] >= 1
    derived = store.get_field(field_key, account_id=b)
    assert derived is not None
    assert derived["projection_mode"] == "derived"
    assert store.get_field(field_key, account_id=registry.a) is None
    assert store.get_field(field_key, account_id=DEFAULT_ACCOUNT_ID) is None


def test_the_migration_gives_older_entries_their_account_and_runs_again(
    registry: Registry,
) -> None:
    conn, b, key = registry.conn, registry.b, registry.key
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)

    # Entries written before the rule existed.
    conn.execute(
        "ALTER TABLE registry_changelog DROP CONSTRAINT registry_changelog_account_keyed_owner_check"
    )
    conn.execute(
        "INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot) VALUES "
        "('workflow', %s, 'update', %s::jsonb), ('field', %s, 'update', '{}'::jsonb), "
        "('agent', %s, 'update', %s::jsonb)",
        (
            key,
            json.dumps({"account_id": b}),
            key,
            key,
            json.dumps({"account_id": b}),
        ),
    )

    conn.execute(sql)

    owners = dict(
        _rows(
            conn,
            "SELECT entity_type, account_id::text FROM registry_changelog WHERE entity_id = %s",
            (key,),
        )
    )
    assert owners == {"workflow": b, "field": DEFAULT_ACCOUNT_ID, "agent": b}
    constraint = _rows(
        conn,
        "SELECT 1 FROM pg_constraint WHERE conname = 'registry_changelog_account_keyed_owner_check'",
    )
    assert constraint == [(1,)]
