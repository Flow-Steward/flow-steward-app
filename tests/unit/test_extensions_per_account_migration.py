"""The extensions-per-account migration carries the baseline's DDL to installed databases.

Installed databases never re-read db/schema/. The per-account enablement table the
baseline declares (ADR 0020, step C6) must therefore come with the migration too, in
a form that is a no-op on a fresh installation and safe to run again on an upgraded
one. The data-level behaviour is tested on a real database in
``tests/db/test_extensions_per_account_postgres.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_contract_catalog import REQUIRED_RUNTIME_TABLES
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261127130000_extensions_per_account.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"

_BASELINE_FORMS = (
    ("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE "),
    ("CREATE UNIQUE INDEX IF NOT EXISTS ", "CREATE UNIQUE INDEX "),
    ("CREATE INDEX IF NOT EXISTS ", "CREATE INDEX "),
)


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


def _baseline_block() -> list[str]:
    text = (ROOT / "db" / "schema" / "001_schema_part_12.sql").read_text(encoding="utf-8")
    start = text.index("-- Extensions per account (ADR 0020, step C6).")
    end = text.index(f"-- End of block: migration {MIGRATION}.", start)
    return _statements(text[start:end])


def _as_baseline(statement: str) -> str:
    for idempotent, plain in _BASELINE_FORMS:
        if statement.startswith(idempotent):
            return statement.replace(idempotent, plain, 1)
    return statement


def test_the_migration_runs_inside_the_runner_transaction() -> None:
    sql = _migration_sql()

    assert migration_runner.is_valid_migration_filename(MIGRATION)
    assert migration_runner.parse_transactional(sql) is True
    assert migration_runner.find_explicit_transaction_control(sql) == []
    assert migration_runner.migration_has_executable_sql(sql)
    assert upgrade_db.pre_release_migrations([MIGRATION]) == []


def test_every_statement_is_safe_on_a_database_that_already_has_the_objects() -> None:
    statements = _statements(_migration_sql())

    assert statements
    for statement in statements:
        assert statement.startswith(tuple(form for form, _ in _BASELINE_FORMS)), statement


def test_the_migration_makes_exactly_the_change_of_the_baseline_block() -> None:
    assert [_as_baseline(item) for item in _statements(_migration_sql())] == _baseline_block()


def test_an_enablement_belongs_to_one_account_and_the_install_of_its_extension() -> None:
    table = next(
        item
        for item in _statements(_migration_sql())
        if "public.extension_account_enablements (" in item
    )

    assert "PRIMARY KEY (account_id, extension_id)" in table
    assert "REFERENCES public.accounts(id) ON DELETE CASCADE" in table
    assert (
        "FOREIGN KEY (install_id, extension_id) REFERENCES"
        " public.extension_installs(install_id, extension_id)"
        " ON UPDATE CASCADE ON DELETE CASCADE" in table
    )
    assert "CHECK (state IN ('enabled', 'disabled'))" in table


def test_the_table_keeps_no_secret() -> None:
    """Settings are non-secret; credentials stay in account-qualified secret references."""
    code = "\n".join(
        line for line in _migration_sql().lower().splitlines() if not line.lstrip().startswith("--")
    )

    for word in ("password", "token", "client_secret", "encrypted", "credential"):
        assert word not in code, word


def test_the_migration_is_recorded_required_and_shipped_by_every_source_slice() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )
    assert "extension_account_enablements" in REQUIRED_RUNTIME_TABLES
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
