"""The secret backend migration carries the baseline's DDL to installed databases.

Installed databases never re-read db/schema/. The configuration and locator tables
the baseline declares (ADR 0066 amendment 2026-11-26) must therefore come with the
migration too, in a form that is a no-op on a fresh installation and safe to run
again on an upgraded one.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_contract_catalog import REQUIRED_RUNTIME_TABLES
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261126200000_secret_backends.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"

_IDEMPOTENT_PREFIXES = ("CREATE TABLE IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS ")
_BASELINE_FORMS = (
    ("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE "),
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
    start = text.index("-- Secret backends (ADR 0066 amendment 2026-11-26).")
    # Each baseline block ends with its own marker; later blocks belong to their migrations.
    end = text.index(f"-- End of block: migration {MIGRATION}.", start)
    return _statements(text[start:end])


def _as_baseline(statement: str) -> str:
    for idempotent, plain in _BASELINE_FORMS:
        statement = statement.replace(idempotent, plain, 1)
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
        assert statement.startswith(_IDEMPOTENT_PREFIXES), statement


def test_the_migration_makes_exactly_the_change_of_the_baseline_block() -> None:
    assert [_as_baseline(item) for item in _statements(_migration_sql())] == _baseline_block()


def test_a_locator_names_a_configuration_of_its_own_account_and_kind() -> None:
    statements = _statements(_migration_sql())
    configs = next(item for item in statements if "public.secret_backend_configs (" in item)
    locators = next(item for item in statements if "public.secret_backend_locators (" in item)

    assert "PRIMARY KEY (account_id, ref_key)" in locators
    assert (
        "FOREIGN KEY (backend_config_id, account_id, backend_kind)"
        " REFERENCES public.secret_backend_configs(id, account_id, backend_kind)" in locators
    )
    assert "backend_kind <> 'local'" in configs
    assert "UNIQUE (id, account_id, backend_kind)" in configs


def test_core_keeps_no_backend_credential_or_value() -> None:
    """Credentials come from the deployment; values stay in the backend (ADR 0066)."""
    sql = _migration_sql().lower()
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))

    for word in ("password", "token", "client_secret", "encrypted_value", "credential"):
        assert word not in code, word


def test_the_migration_is_recorded_and_required_at_runtime() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )
    for table in ("secret_backend_configs", "secret_backend_locators"):
        assert table in REQUIRED_RUNTIME_TABLES


def test_every_source_slice_ships_the_migration() -> None:
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
