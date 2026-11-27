"""The multilevel scoped role migration carries the baseline's indexes to installed databases.

Installed databases never re-read db/schema/. The workspace and workflow indexes on
user_roles that the baseline declares (ADR 0076 section 8a) must therefore come with the
migration too, idempotent so a fresh installation and an upgraded one both accept it.
The workflow index follows the account-qualified workflow key of user_roles (ADR 0020,
step C4b). The data-level behaviour is tested on a real database in
``tests/db/test_multilevel_scoped_roles_postgres.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261126210000_multilevel_scoped_roles.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"


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
    start = text.index("-- Scoped role limits on workspaces, projects and workflows (ADR 0076")
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


def test_the_migration_makes_exactly_the_change_of_the_baseline_block() -> None:
    statements = _statements(_migration_sql())

    assert statements
    assert all(item.startswith("CREATE INDEX IF NOT EXISTS ") for item in statements)
    as_baseline = [
        item.replace("CREATE INDEX IF NOT EXISTS ", "CREATE INDEX ", 1) for item in statements
    ]
    assert as_baseline == _baseline_block()


def test_the_workflow_index_follows_the_account_qualified_workflow_key() -> None:
    workflow_index = next(item for item in _baseline_block() if "idx_user_roles_workflow " in item)

    assert "(workflow_account_id, workflow_id)" in workflow_index


def test_the_migration_is_recorded_and_shipped_by_every_source_slice() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
