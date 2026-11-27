"""The registry ownership migration carries the baseline's change to installed databases.

Installed databases never re-read db/schema/. Account ownership of registry fields and
data contracts (ADR 0020, step C4a) must therefore come with a migration that is a
no-op on a fresh installation (the runner applies every migration after loading the
baseline there) and safe to run again on an upgraded one. The data-level behaviour is
tested on a real database in ``tests/db/test_registry_account_ownership_postgres.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261126170000_registry_account_ownership.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"
DEFAULT_ACCOUNT = "'00000000-0000-0000-0000-000000000002'::uuid"


def _statements(sql: str) -> list[str]:
    statements = []
    for statement in split_sql_statements(sql):
        code = "\n".join(
            line for line in statement.splitlines() if not line.lstrip().startswith("--")
        )
        normalized = " ".join(code.split()).rstrip(";").strip()
        if normalized:
            statements.append(normalized)
    return statements


def _migration_sql() -> str:
    return (ROOT / "db" / "migrations" / MIGRATION).read_text(encoding="utf-8")


def _baseline() -> str:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "db" / "schema").glob("001_schema_part_*.sql"))
    )
    return " ".join(text.split())


def test_the_migration_runs_inside_the_runner_transaction() -> None:
    sql = _migration_sql()

    assert migration_runner.is_valid_migration_filename(MIGRATION)
    assert migration_runner.parse_transactional(sql) is True
    assert migration_runner.find_explicit_transaction_control(sql) == []
    assert migration_runner.migration_has_executable_sql(sql)
    assert upgrade_db.pre_release_migrations([MIGRATION]) == []


def test_every_statement_is_safe_to_run_again() -> None:
    dropped: set[str] = set()
    for statement in _statements(_migration_sql()):
        if " ADD COLUMN " in statement:
            assert " ADD COLUMN IF NOT EXISTS " in statement, statement
        elif drop := re.search(r"DROP CONSTRAINT IF EXISTS (\w+)$", statement):
            dropped.add(drop.group(1))
        elif add := re.search(r"ADD CONSTRAINT (\w+) ", statement):
            assert add.group(1) in dropped, f"{add.group(1)} is added without being dropped first"
        elif statement.startswith("UPDATE "):
            assert statement.endswith("AND contract_account_id IS NULL"), statement
        else:
            raise AssertionError(f"unexpected statement: {statement}")


def test_existing_rows_are_given_to_the_default_account() -> None:
    statements = _statements(_migration_sql())

    for table in ("registry_data_contracts", "registry_fields"):
        assert (
            f"ALTER TABLE ONLY public.{table} ADD COLUMN IF NOT EXISTS account_id uuid "
            f"DEFAULT {DEFAULT_ACCOUNT} NOT NULL"
        ) in statements
    for table in ("registry_skill_inputs", "registry_skill_outputs"):
        assert any(
            statement.startswith(
                f"UPDATE public.{table} SET contract_account_id = {DEFAULT_ACCOUNT}"
            )
            for statement in statements
        ), table


def test_the_migration_makes_the_change_of_the_baseline() -> None:
    baseline = _baseline()
    migration = " ".join(_statements(_migration_sql()))

    for fragment in (
        "registry_data_contracts_pkey PRIMARY KEY (account_id, contract_id)",
        "registry_fields_account_id_field_key_key UNIQUE (account_id, field_key)",
        "registry_fields_data_contract_id_fkey FOREIGN KEY (account_id, data_contract_id) "
        "REFERENCES public.registry_data_contracts(account_id, contract_id) "
        "ON DELETE SET NULL (data_contract_id)",
        "registry_skill_inputs_contract_id_fkey FOREIGN KEY (contract_account_id, contract_id) "
        "REFERENCES public.registry_data_contracts(account_id, contract_id) "
        "ON DELETE SET NULL (contract_account_id, contract_id)",
        "registry_skill_outputs_contract_id_fkey FOREIGN KEY (contract_account_id, contract_id) "
        "REFERENCES public.registry_data_contracts(account_id, contract_id) "
        "ON DELETE SET NULL (contract_account_id, contract_id)",
        "registry_skill_inputs_contract_owner_check "
        "CHECK (((contract_id IS NULL) OR (contract_account_id IS NOT NULL)))",
        "registry_skill_outputs_contract_owner_check "
        "CHECK (((contract_id IS NULL) OR (contract_account_id IS NOT NULL)))",
    ):
        assert fragment in baseline, fragment
        assert fragment in migration, fragment
    assert f"account_id uuid DEFAULT {DEFAULT_ACCOUNT} NOT NULL" in baseline
    assert "registry_fields_field_key_key" not in baseline
    assert "PRIMARY KEY (contract_id)" not in baseline


def test_the_migration_is_recorded() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )


def test_every_source_slice_ships_the_migration() -> None:
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
