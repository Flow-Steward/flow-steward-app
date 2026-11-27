"""The same-account references migration carries the baseline's change (ADR 0020, C4c).

Installed databases never re-read db/schema/, so the rules that a step names agents,
skills and published blocks of its own account (or platform ones) and that a hook
binding and its revisions stay in one project of one account come with a migration
that is a no-op on a fresh installation and safe to run again. The data-level
behaviour is tested in ``tests/db/test_registry_same_account_refs_postgres.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261126190000_registry_same_account_refs.sql"
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


def _baseline_statements() -> list[str]:
    return [
        statement
        for path in sorted((ROOT / "db" / "schema").glob("001_schema_part_*.sql"))
        for statement in _statements(path.read_text(encoding="utf-8"))
    ]


def test_the_migration_runs_inside_the_runner_transaction() -> None:
    sql = _migration_sql()

    assert migration_runner.is_valid_migration_filename(MIGRATION)
    assert migration_runner.parse_transactional(sql) is True
    assert migration_runner.find_explicit_transaction_control(sql) == []
    assert migration_runner.migration_has_executable_sql(sql)
    assert upgrade_db.pre_release_migrations([MIGRATION]) == []


def test_every_statement_is_safe_to_run_again() -> None:
    dropped: set[str] = set()
    triggers_dropped: set[str] = set()
    for statement in _statements(_migration_sql()):
        if drop := re.search(r"DROP CONSTRAINT IF EXISTS (\w+)$", statement):
            dropped.add(drop.group(1))
        elif add := re.search(r"ADD CONSTRAINT (\w+) ", statement):
            assert add.group(1) in dropped, f"{add.group(1)} is added without being dropped first"
        elif statement.startswith("CREATE OR REPLACE FUNCTION "):
            continue
        elif trigger := re.match(r"DROP TRIGGER IF EXISTS (\w+) ", statement):
            triggers_dropped.add(trigger.group(1))
        elif trigger := re.match(r"CREATE TRIGGER (\w+) ", statement):
            assert trigger.group(1) in triggers_dropped, statement
        elif statement.startswith("DO $$"):
            assert "RAISE EXCEPTION" in statement
        else:
            raise AssertionError(f"unexpected statement: {statement}")


def test_the_migration_changes_no_row() -> None:
    statements = " ".join(_statements(_migration_sql()))
    # Key clauses such as "ON DELETE CASCADE" are not row changes.
    for pattern in (
        r"\bUPDATE\s+(?:ONLY\s+)?public\.",
        r"\bDELETE\s+FROM\b",
        r"\bTRUNCATE\b",
        r"\bINSERT\s+INTO\b",
    ):
        assert not re.search(pattern, statements), pattern


def test_a_row_that_crosses_accounts_stops_the_migration_before_any_change() -> None:
    statements = _statements(_migration_sql())
    checks = [i for i, s in enumerate(statements) if s.startswith("DO $$")]
    assert checks == [0]
    check = statements[0]
    for table in (
        "registry_workflow_steps",
        "registry_workflow_hook_bindings",
        "registry_workflow_hook_binding_revisions",
    ):
        assert f"of public.{table} " in check, table
    for source in ("registry_agents", "registry_skills", "registry_published_block_contracts"):
        assert f"FROM public.{source} " in check, source


def test_the_migration_makes_the_change_of_the_baseline() -> None:
    baseline = _baseline_statements()
    as_baseline = {
        statement.replace("CREATE OR REPLACE FUNCTION ", "CREATE FUNCTION ", 1)
        for statement in _statements(_migration_sql())
    }
    # The baseline redefines the step trigger function later (CREATE OR REPLACE) for
    # account-scoped block keys; that redefinition is the change of the later
    # 20261127150000_account_scoped_block_keys migration, not of this one.
    block = [
        statement
        for statement in baseline
        if not statement.startswith("CREATE OR REPLACE FUNCTION ")
        and (
            "registry_workflow_steps_same_account" in statement
            or "registry_workflow_hook_bindings_project_account_fkey" in statement
            or "registry_workflow_hook_bindings_id_scope_key" in statement
            or "registry_workflow_hook_binding_revisions_scope_fkey" in statement
        )
    ]
    assert len(block) == 5
    missing = [statement for statement in block if statement not in as_baseline]
    assert missing == []


def test_the_step_trigger_checks_every_shared_reference() -> None:
    (function,) = [
        s
        for s in _statements(_migration_sql())
        if s.startswith("CREATE OR REPLACE FUNCTION public.registry_workflow_steps_same_account")
    ]
    for fragment in (
        "FROM public.registry_agents AS a WHERE a.agent_id = NEW.agent_id",
        "FROM public.registry_skills AS s WHERE s.skill_id = NEW.skill_id",
        "NEW.override ->> 'published_block_key'",
        "NEW.override ->> 'block_key'",
        "FROM public.registry_published_block_contracts AS b",
    ):
        assert fragment in function, fragment
    # A platform object (NULL account) is every account's.
    assert function.count("account_id IS NOT NULL AND") == 3
    (trigger,) = [
        s
        for s in _statements(_migration_sql())
        if s.startswith("CREATE TRIGGER trg_registry_workflow_steps_same_account")
    ]
    assert "BEFORE INSERT OR UPDATE OF account_id, agent_id, skill_id, block_key, override" in (
        trigger
    )


def test_the_migration_is_recorded() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )


def test_every_source_slice_ships_the_migration_and_the_rule() -> None:
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
        assert "core/application/registry/account_references.py" in json.dumps(payload), name
