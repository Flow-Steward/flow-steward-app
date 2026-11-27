"""The ADR 0076 migration carries the baseline's role-permission DDL to installed databases.

Installed databases never re-read db/schema/. Every object the baseline declares for
role permissions and assignment scopes must therefore come with the migration too, in
a form that is a no-op on a fresh installation (the runner applies every migration
after loading the baseline there) and safe to run on an upgraded one.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261120090000_role_permissions.sql"
#: The later migration that makes the account-qualified workflow identity part of the
#: same baseline block (ADR 0020, step C4b).
WORKFLOW_IDENTITY_MIGRATION = "20261126180000_workflow_account_identity.sql"

#: Statement prefixes that are a no-op when the object already exists.
_IDEMPOTENT_PREFIXES = (
    "ALTER TABLE public.roles ADD COLUMN IF NOT EXISTS ",
    "ALTER TABLE public.user_roles ADD COLUMN IF NOT EXISTS ",
    "CREATE TABLE IF NOT EXISTS ",
    "CREATE OR REPLACE FUNCTION ",
    "CREATE UNIQUE INDEX IF NOT EXISTS ",
    "DROP TRIGGER IF EXISTS ",
    "INSERT INTO ",
    "DELETE FROM ",
    "UPDATE ",
)
_DROP_CONSTRAINT_RE = re.compile(r"^ALTER TABLE ONLY (\S+) DROP CONSTRAINT IF EXISTS (\w+)$")
_ADD_CONSTRAINT_RE = re.compile(r"^ALTER TABLE ONLY (\S+) ADD CONSTRAINT (\w+) ")
_CREATE_TRIGGER_RE = re.compile(r"^CREATE TRIGGER (\w+) .* ON (\S+) ")
_INNER_ADD_CONSTRAINT_RE = re.compile(
    r"ALTER TABLE ONLY (\S+) ADD CONSTRAINT (\w+) .*?(?:ON DELETE CASCADE|\))(?=; | END IF|; ELSE)"
)


def _do_block_statements(statement: str) -> list[str]:
    """The ``ALTER TABLE`` statements a ``DO`` block may run, one per branch."""
    return [match.group(0) for match in _INNER_ADD_CONSTRAINT_RE.finditer(statement)]


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
    """The ADR 0076 statements at the end of baseline part 12."""
    text = (ROOT / "db" / "schema" / "001_schema_part_12.sql").read_text(encoding="utf-8")
    start = text.index("-- Role permissions and the scope of a role assignment (ADR 0076).")
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
    dropped: set[tuple[str, str]] = set()
    dropped_triggers: set[tuple[str, str]] = set()
    for statement in _statements(_migration_sql()):
        if drop := _DROP_CONSTRAINT_RE.match(statement):
            dropped.add((drop.group(1), drop.group(2)))
            continue
        if add := _ADD_CONSTRAINT_RE.match(statement):
            assert (add.group(1), add.group(2)) in dropped, statement
            continue
        if statement.startswith("DO $$"):
            # Picks the form of a key the database can hold; every key it adds was
            # dropped above, so it runs again safely.
            inner = _do_block_statements(statement)
            assert inner, statement
            for added in inner:
                add = _ADD_CONSTRAINT_RE.match(added)
                assert add and (add.group(1), add.group(2)) in dropped, added
            continue
        if statement.startswith("DROP TRIGGER IF EXISTS "):
            name, _, table = statement.removeprefix("DROP TRIGGER IF EXISTS ").partition(" ON ")
            dropped_triggers.add((name, table))
            continue
        if trigger := _CREATE_TRIGGER_RE.match(statement):
            assert (trigger.group(1), trigger.group(2)) in dropped_triggers, statement
            continue
        assert statement.startswith(_IDEMPOTENT_PREFIXES), statement


def test_the_migration_makes_every_change_of_the_baseline_block() -> None:
    """Together with the workflow identity migration, which owns part of the block."""
    identity = (ROOT / "db" / "migrations" / WORKFLOW_IDENTITY_MIGRATION).read_text(
        encoding="utf-8"
    )
    migration = [
        inner
        for statement in _statements(_migration_sql()) + _statements(identity)
        for inner in (
            _do_block_statements(statement) if statement.startswith("DO $$") else [statement]
        )
    ]
    as_baseline = {
        statement.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ", 1)
        .replace("CREATE OR REPLACE FUNCTION ", "CREATE FUNCTION ", 1)
        .replace("CREATE UNIQUE INDEX IF NOT EXISTS ", "CREATE UNIQUE INDEX ", 1)
        for statement in migration
    }

    missing = [statement for statement in _baseline_block() if statement not in as_baseline]
    assert missing == []


def test_the_migration_adds_the_baseline_columns_and_assignment_key() -> None:
    sql = " ".join(_migration_sql().split())
    part_04 = " ".join(
        (ROOT / "db" / "schema" / "001_schema_part_04.sql").read_text(encoding="utf-8").split()
    )
    part_07 = " ".join(
        (ROOT / "db" / "schema" / "001_schema_part_07.sql").read_text(encoding="utf-8").split()
    )

    for table, column in (
        ("roles", "is_system boolean DEFAULT false NOT NULL"),
        ("roles", "description text DEFAULT ''::text NOT NULL"),
        ("user_roles", "workspace_id uuid"),
        ("user_roles", "project_id uuid"),
        ("user_roles", "workflow_id text"),
        ("user_roles", "role_is_system boolean DEFAULT false NOT NULL"),
    ):
        assert column in part_04
        assert f"ALTER TABLE public.{table} ADD COLUMN IF NOT EXISTS {column};" in sql
    key = (
        "ADD CONSTRAINT user_roles_assignment_key UNIQUE NULLS NOT DISTINCT "
        "(user_id, role_id, workspace_id, project_id, workflow_id);"
    )
    assert key in part_07
    assert key in sql
    assert "user_roles_user_id_role_id_key" not in part_07
    assert "DROP CONSTRAINT IF EXISTS user_roles_user_id_role_id_key;" in sql


def test_duplicate_system_roles_are_removed_before_the_unique_index_exists() -> None:
    statements = _statements(_migration_sql())
    delete = next(
        i for i, s in enumerate(statements) if s.startswith("DELETE FROM public.user_roles")
    )
    index = next(
        i for i, s in enumerate(statements) if "ux_user_roles_one_system_role_per_scope" in s
    )
    backfill = next(i for i, s in enumerate(statements) if s.startswith("UPDATE public.user_roles"))
    foreign_key = next(
        i
        for i, s in enumerate(statements)
        if s.startswith(
            "ALTER TABLE ONLY public.user_roles ADD CONSTRAINT user_roles_role_is_system_fkey"
        )
    )

    assert delete < backfill < foreign_key < index
