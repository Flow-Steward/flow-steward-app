"""The scoped role limit migration carries the baseline's DDL to installed databases.

Installed databases never re-read db/schema/. The limit table and the project index
the baseline declares must therefore come with the migration too, in a form that is
a no-op on a fresh installation (the runner applies every migration after loading the
baseline there) and safe to run again on an upgraded one. The data-level behaviour is
tested on a real database in ``tests/db/test_scoped_roles_port_postgres.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_contract_catalog import REQUIRED_RUNTIME_TABLES
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261126120000_scoped_role_users.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"

_IDEMPOTENT_PREFIXES = ("CREATE TABLE IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS ")


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
    start = text.index("-- Project-scoped role limits (ADR 0076 section 8, ADR 0082).")
    # Each baseline block ends with its own marker; later blocks belong to their migrations.
    end = text.index(f"-- End of block: migration {MIGRATION}.", start)
    return _statements(text[start:end])


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
    as_baseline = [
        statement.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ", 1).replace(
            "CREATE INDEX IF NOT EXISTS ", "CREATE INDEX ", 1
        )
        for statement in _statements(_migration_sql())
    ]

    assert as_baseline == _baseline_block()


def test_the_limit_marker_survives_project_deletion_but_not_user_deletion() -> None:
    table = next(
        statement
        for statement in _statements(_migration_sql())
        if statement.startswith("CREATE TABLE IF NOT EXISTS public.scoped_role_users ")
    )

    assert "user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE" in table
    assert "public.projects" not in table


def test_the_migration_is_recorded_and_required_at_runtime() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )
    assert "scoped_role_users" in REQUIRED_RUNTIME_TABLES


def test_every_source_slice_ships_the_migration() -> None:
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
