"""Registry fields and data contracts belong to an account (ADR 0020, step C4a).

Two accounts own the same field key and the same contract id without clashing, a
reference across accounts is refused by the store and by the database, and the
migration upgrades a database with the previous global keys without losing rows.
Every test runs on the disposable audit database inside a transaction that is
rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.db_store import RegistryDBStore
from core.infrastructure.db import psycopg_runtime

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261126170000_registry_account_ownership.sql"
FIELD = "acme.invoice_lines"
CONTRACT = "acme.invoice_lines.v1"


@dataclass(frozen=True)
class Registry:
    conn: object
    store: RegistryDBStore
    a: str
    b: str


@pytest.fixture
def registry(disposable_db) -> Iterator[Registry]:
    conn = psycopg_runtime.connect(disposable_db.dsn)
    conn.autocommit = False
    store = RegistryDBStore(disposable_db.dsn)
    # Every store read and write joins this one transaction, rolled back below.
    store._set_tx_conn(conn)
    try:
        yield Registry(conn, store, str(uuid.uuid4()), str(uuid.uuid4()))
    finally:
        store._set_tx_conn(None)
        conn.rollback()
        conn.close()


def _contract(display_name: str, kind: str = "list") -> dict:
    return {
        "contract_id": CONTRACT,
        "display_name": display_name,
        "kind": kind,
        "schema": {"type": "array"},
    }


def _field(display_name: str, contract_id: str | None = CONTRACT) -> dict:
    return {
        "field_key": FIELD,
        "display_name": display_name,
        "value_type": "list",
        "data_contract_id": contract_id,
    }


def _rows(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [tuple(row) for row in cur.fetchall()]


def test_two_accounts_own_the_same_keys_without_clashing(registry: Registry) -> None:
    store, a, b = registry.store, registry.a, registry.b

    store.save_data_contract(_contract("A lines"), account_id=a, create_only=True)
    store.save_data_contract(_contract("B lines", kind="json"), account_id=b, create_only=True)
    store.save_field(_field("A field"), account_id=a)
    store.save_field(_field("B field"), account_id=b)

    assert store.get_data_contract(CONTRACT, account_id=a)["display_name"] == "A lines"
    assert store.get_data_contract(CONTRACT, account_id=b)["kind"] == "json"
    assert store.get_field(FIELD, account_id=a)["display_name"] == "A field"
    assert store.get_field(FIELD, account_id=b)["display_name"] == "B field"
    assert [
        row["field_key"] for row in store.list_fields(ensure_projection=False, account_id=a)
    ] == [FIELD]
    assert [
        row["contract_id"]
        for row in store.list_data_contracts(ensure_projection=False, account_id=b)
    ] == [CONTRACT]

    # A second create in the same account is refused; the other account is untouched.
    from core.application.registry.db_store_common import RegistryCreateConflictError

    with pytest.raises(RegistryCreateConflictError):
        store.save_data_contract(_contract("A again"), account_id=a, create_only=True)


def test_update_toggle_delete_and_restore_stay_in_their_account(registry: Registry) -> None:
    store, a, b = registry.store, registry.a, registry.b
    for account in (a, b):
        store.save_data_contract(_contract("lines"), account_id=account)
        store.save_field(_field("field"), account_id=account)

    store.save_field(_field("A renamed"), account_id=a)
    assert store.toggle_field(FIELD, account_id=a) is False
    assert store.toggle_data_contract(CONTRACT, account_id=a) is False
    store.delete_data_contract(CONTRACT, account_id=a)

    assert store.get_field(FIELD, account_id=b)["display_name"] == "field"
    assert store.get_field(FIELD, account_id=b)["enabled"] is True
    assert store.get_data_contract(CONTRACT, account_id=b)["enabled"] is True
    assert store.get_data_contract(CONTRACT, account_id=a) is None
    assert store.get_data_contract(CONTRACT, include_trashed=True, account_id=a) is not None

    # The trash surface works on the caller's account (step C5): its restore
    # statement, keyed by B's account, restores B's contract and leaves A's alone.
    from core.application.management.command_services_common import (
        _TRASH_ENTITY_CONFIG,
        _trash_key_params,
        _trash_key_sql,
    )

    cfg = _TRASH_ENTITY_CONFIG["registry_data_contract"]
    store.delete_data_contract(CONTRACT, account_id=b)
    with registry.conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_data_contracts SET deleted_at = NULL "
            f"WHERE contract_id = %s{_trash_key_sql(cfg)} AND deleted_at IS NOT NULL",
            _trash_key_params(cfg, CONTRACT, b),
        )
    assert store.get_data_contract(CONTRACT, account_id=a) is None
    assert store.get_data_contract(CONTRACT, account_id=b) is not None

    store.delete_field(FIELD, account_id=a)
    assert store.get_field(FIELD, account_id=a) is None
    assert store.get_field(FIELD, account_id=b) is not None


def test_a_field_cannot_name_another_accounts_contract(registry: Registry) -> None:
    store, conn, a, b = registry.store, registry.conn, registry.a, registry.b
    store.save_data_contract(_contract("B only"), account_id=b)

    with pytest.raises(ValueError, match="does not exist"):
        store.save_field(_field("A field"), account_id=a)
    assert store.get_field(FIELD, account_id=a) is None

    # The database refuses the same reference when a writer bypasses the store.
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO registry_fields (account_id, field_key, display_name, data_contract_id)
               VALUES (%s, %s, 'raw', %s)""",
            (a, FIELD, CONTRACT),
        )


def test_a_skill_names_contracts_of_its_own_account_only(registry: Registry) -> None:
    store, conn, a, b = registry.store, registry.conn, registry.a, registry.b
    skill_id = f"acme.skill_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """INSERT INTO registry_skills (skill_id, display_name, account_id, scope_kind)
           VALUES (%s, 'Lines', %s, 'account')""",
        (skill_id, a),
    )
    store.save_data_contract(_contract("B only"), account_id=b)

    with store._cursor() as cur, pytest.raises(ValueError, match="does not exist"):
        store._skill_contract_owner_cur(cur, skill_id, CONTRACT)

    store.save_data_contract(_contract("A lines"), account_id=a)
    with store._cursor() as cur:
        assert store._skill_contract_owner_cur(cur, skill_id, CONTRACT) == a

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO registry_skill_outputs (skill_id, name, contract_id, contract_account_id)
               VALUES (%s, 'lines', %s, %s)""",
            (skill_id, f"{CONTRACT}.absent", a),
        )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO registry_skill_outputs (skill_id, name, contract_id) VALUES (%s, 'x', %s)",
            (skill_id, CONTRACT),
        )


def test_a_published_block_cannot_bind_another_accounts_field(registry: Registry) -> None:
    store, a = registry.store, registry.a
    store.save_field({"field_key": FIELD, "display_name": "Default"}, account_id=DEFAULT_ACCOUNT_ID)

    with store._cursor() as cur:
        assert store._field_id_for_key_cur(cur, FIELD, a) is None
        assert store._field_id_for_key_cur(cur, FIELD, DEFAULT_ACCOUNT_ID) is not None


def test_the_migration_upgrades_global_keys_and_runs_again(registry: Registry) -> None:
    conn = registry.conn
    skill_id = f"acme.legacy_{uuid.uuid4().hex[:8]}"
    # Put the two tables back into the previous global-key shape.
    conn.execute(
        """
        ALTER TABLE registry_fields DROP CONSTRAINT registry_fields_data_contract_id_fkey;
        ALTER TABLE registry_skill_inputs DROP CONSTRAINT registry_skill_inputs_contract_id_fkey;
        ALTER TABLE registry_skill_outputs DROP CONSTRAINT registry_skill_outputs_contract_id_fkey;
        ALTER TABLE registry_skill_inputs DROP CONSTRAINT registry_skill_inputs_contract_owner_check;
        ALTER TABLE registry_skill_outputs DROP CONSTRAINT registry_skill_outputs_contract_owner_check;
        ALTER TABLE registry_skill_inputs DROP COLUMN contract_account_id;
        ALTER TABLE registry_skill_outputs DROP COLUMN contract_account_id;
        ALTER TABLE registry_data_contracts DROP CONSTRAINT registry_data_contracts_pkey;
        ALTER TABLE registry_fields DROP CONSTRAINT registry_fields_account_id_field_key_key;
        ALTER TABLE registry_data_contracts DROP COLUMN account_id;
        ALTER TABLE registry_fields DROP COLUMN account_id;
        ALTER TABLE registry_data_contracts ADD CONSTRAINT registry_data_contracts_pkey
            PRIMARY KEY (contract_id);
        ALTER TABLE registry_fields ADD CONSTRAINT registry_fields_field_key_key UNIQUE (field_key);
        ALTER TABLE registry_fields ADD CONSTRAINT registry_fields_data_contract_id_fkey
            FOREIGN KEY (data_contract_id) REFERENCES registry_data_contracts(contract_id)
            ON DELETE SET NULL;
        ALTER TABLE registry_skill_outputs ADD CONSTRAINT registry_skill_outputs_contract_id_fkey
            FOREIGN KEY (contract_id) REFERENCES registry_data_contracts(contract_id)
            ON DELETE SET NULL;
        """
    )
    conn.execute(
        "INSERT INTO registry_data_contracts (contract_id, display_name) VALUES (%s, 'Legacy')",
        (CONTRACT,),
    )
    conn.execute(
        """INSERT INTO registry_fields (field_key, display_name, value_type, data_contract_id)
           VALUES (%s, 'Legacy', 'list', %s)""",
        (FIELD, CONTRACT),
    )
    conn.execute(
        "INSERT INTO registry_skills (skill_id, display_name) VALUES (%s, 'Legacy')", (skill_id,)
    )
    conn.execute(
        "INSERT INTO registry_skill_outputs (skill_id, name, contract_id) VALUES (%s, 'lines', %s)",
        (skill_id, CONTRACT),
    )

    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)

    default = "00000000-0000-0000-0000-000000000002"
    assert _rows(
        conn,
        "SELECT account_id::text FROM registry_data_contracts WHERE contract_id = %s",
        (CONTRACT,),
    ) == [(default,)]
    assert _rows(
        conn,
        "SELECT account_id::text, data_contract_id FROM registry_fields WHERE field_key = %s",
        (FIELD,),
    ) == [(default, CONTRACT)]
    assert _rows(
        conn,
        "SELECT contract_account_id::text FROM registry_skill_outputs WHERE skill_id = %s",
        (skill_id,),
    ) == [(default,)]

    # The upgraded database accepts a second account using the same keys.
    other = registry.b
    registry.store.save_data_contract(_contract("Other"), account_id=other)
    registry.store.save_field(_field("Other"), account_id=other)
    assert registry.store.get_field(FIELD, account_id=other)["display_name"] == "Other"
