"""The workflow identity migration carries the baseline's change to installed databases.

Installed databases never re-read db/schema/. Account-qualified workflow identity
(ADR 0020, step C4b) must therefore come with a migration that is a no-op on a fresh
installation (the runner applies every migration after loading the baseline there)
and safe to run again on an upgraded one. The data-level behaviour is tested on a
real database in ``tests/db/test_workflow_account_identity_postgres.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261126180000_workflow_account_identity.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"
DEFAULT_ACCOUNT = "'00000000-0000-0000-0000-000000000002'::uuid"
COMPOSITE = "REFERENCES public.registry_workflows(account_id, workflow_id)"

#: Every table whose rows name a workflow through a foreign key, with the columns
#: that now carry the workflow's account.
WORKFLOW_REFERENCES = {
    "registry_workflow_groups_workflow_id_fkey": "(account_id, workflow_id)",
    "registry_workflow_for_each_workflow_id_fkey": "(account_id, workflow_id)",
    "registry_workflow_steps_owner_workflow_id_fkey": "(account_id, owner_workflow_id)",
    "project_workflows_workflow_id_fkey": "(account_id, workflow_id)",
    "scheduled_jobs_workflow_id_fkey": "(account_id, workflow_id)",
    "workflow_entrypoints_workflow_id_fkey": "(account_id, workflow_id)",
    "workflow_reactions_workflow_id_fkey": "(account_id, workflow_id)",
    "workflow_runtime_dependencies_workflow_id_fkey": "(account_id, workflow_id)",
    "notification_routes_workflow_id_fkey": "(account_id, workflow_id)",
    "catalog_secret_bindings_workflow_id_fkey": "(account_id, workflow_id)",
    "catalog_secret_approvals_workflow_id_fkey": "(account_id, workflow_id)",
    "domain_event_ledger_workflow_id_fkey": "(account_id, workflow_id)",
    "event_dispatches_workflow_id_fkey": "(account_id, workflow_id)",
    "extension_invocation_logs_workflow_id_fkey": "(account_id, workflow_id)",
    "job_reaction_events_workflow_id_fkey": "(account_id, workflow_id)",
    "user_roles_workflow_id_fkey": "(workflow_account_id, workflow_id)",
}


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
    triggers_dropped: set[str] = set()
    for statement in _statements(_migration_sql()):
        if " ADD COLUMN " in statement:
            assert " ADD COLUMN IF NOT EXISTS " in statement, statement
        elif drop := re.search(r"DROP CONSTRAINT IF EXISTS (\w+)$", statement):
            dropped.add(drop.group(1))
        elif add := re.search(r"ADD CONSTRAINT (\w+) ", statement):
            assert add.group(1) in dropped, f"{add.group(1)} is added without being dropped first"
        elif re.search(r" ALTER COLUMN \w+ SET (DEFAULT .+|NOT NULL)$", statement):
            continue
        elif statement.startswith("UPDATE "):
            # A backfill only fills a missing account, so a second run changes nothing.
            assert re.search(r"(account_id|workflow_account_id) IS NULL\b", statement), statement
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


def test_existing_rows_keep_their_workflow_and_get_its_account() -> None:
    statements = " ".join(_statements(_migration_sql()))

    assert (
        "UPDATE public.registry_workflows SET account_id = "
        + DEFAULT_ACCOUNT
        + " WHERE account_id IS NULL"
    ) in statements
    # A projected workflow without an owner goes to its install's account first.
    assert "FROM public.extension_installs i WHERE w.account_id IS NULL" in statements
    for table, column in (
        ("registry_workflow_groups", "workflow_id"),
        ("registry_workflow_for_each", "workflow_id"),
        ("registry_workflow_steps", "owner_workflow_id"),
        ("approval_step_approvers", "workflow_id"),
        ("workflow_runtime_dependencies", "workflow_id"),
    ):
        assert (
            f"UPDATE public.{table} t SET account_id = w.account_id "
            f"FROM public.registry_workflows w WHERE t.account_id IS NULL "
            f"AND w.workflow_id = t.{column}"
        ) in statements, table
    # "ON DELETE CASCADE" in a key is fine; a DELETE statement is not.
    assert not re.search(r"\bDELETE\s+FROM\b", statements)
    assert not re.search(r"\bTRUNCATE\b", statements)


def test_a_row_of_another_accounts_workflow_stops_the_migration() -> None:
    (check,) = [s for s in _statements(_migration_sql()) if s.startswith("DO $$")]

    for table in (
        "registry_workflow_steps",
        "project_workflows",
        "scheduled_jobs",
        "user_roles",
        "job_reaction_events",
    ):
        assert f"ARRAY['{table}', " in check, table
    assert "name a workflow of another account" in check


def test_the_migration_makes_the_change_of_the_baseline() -> None:
    baseline = _baseline()
    migration = " ".join(_statements(_migration_sql()))

    for fragment in (
        "registry_workflows_account_id_workflow_key_key UNIQUE (account_id, workflow_key)",
        "registry_workflows_account_id_workflow_id_key UNIQUE (account_id, workflow_id)",
        "registry_workflow_groups_account_id_workflow_id_group_id_key "
        "UNIQUE (account_id, workflow_id, group_id)",
        "registry_workflow_for_each_account_workflow_for_each_key "
        "UNIQUE (account_id, workflow_id, for_each_id)",
        "registry_workflow_steps_account_owner_workflow_step_key "
        "UNIQUE (account_id, owner_workflow_id, step_id)",
        "workflow_runtime_dependencies_account_step_ref_key "
        "UNIQUE (account_id, workflow_id, step_id, dependency_kind, dependency_ref)",
        "catalog_secret_bindings_workflow_field_key UNIQUE (account_id, workflow_id, field_name)",
        "catalog_secret_approvals_workflow_key UNIQUE (account_id, workflow_id)",
        "approval_step_approvers_pkey PRIMARY KEY (account_id, workflow_id, step_id, user_id)",
        "registry_workflow_steps_group_fkey FOREIGN KEY (account_id, owner_workflow_id, group_id) "
        "REFERENCES public.registry_workflow_groups(account_id, workflow_id, id)",
        "registry_workflow_steps_for_each_fkey FOREIGN KEY "
        "(account_id, owner_workflow_id, for_each_id) "
        "REFERENCES public.registry_workflow_for_each(account_id, workflow_id, id)",
        "project_workflows_project_account_fkey FOREIGN KEY (project_id, account_id) "
        "REFERENCES public.projects(id, account_id)",
        "registry_workflows_source_install_account_fkey FOREIGN KEY "
        "(source_install_id, account_id) REFERENCES public.extension_installs"
        "(install_id, account_id) ON DELETE SET NULL (source_install_id)",
        "user_roles_workflow_account_check "
        "CHECK (((workflow_id IS NULL) OR (workflow_account_id IS NOT NULL)))",
    ):
        assert fragment in baseline, fragment
        assert fragment in migration, fragment
    for name, columns in WORKFLOW_REFERENCES.items():
        fragment = f"{name} FOREIGN KEY {columns} {COMPOSITE}"
        assert fragment in baseline, fragment
        assert fragment in migration, fragment
    # No reference to the installation-wide identity is left.
    assert "REFERENCES public.registry_workflows(workflow_id)" not in baseline
    assert "registry_workflows_workflow_key_key" not in baseline
    assert "registry_workflows_workflow_id_key" not in baseline
    assert "fk_scheduled_jobs_workflow" not in baseline
    assert f"account_id uuid DEFAULT {DEFAULT_ACCOUNT} NOT NULL, scope_kind" in baseline


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


_GLOBAL_WORKFLOW_KEY = re.compile(
    r"REFERENCES public\.registry_workflows\s*\(\s*workflow_id\s*\)", re.IGNORECASE
)


def _runs_on_a_fresh_baseline(statement: str) -> bool:
    """Whether *statement* changes a database built from the current baseline.

    ``CREATE TABLE IF NOT EXISTS`` finds the baseline's table and does nothing, so a
    reference inside it never reaches the database; every other statement runs.
    """
    return not statement.upper().startswith("CREATE TABLE IF NOT EXISTS")


def test_no_earlier_migration_needs_the_global_workflow_key_on_a_fresh_baseline() -> None:
    """The runner applies every migration, in file order, after loading the baseline.

    Since C4b the baseline has no unique key on ``registry_workflows(workflow_id)``,
    so an earlier migration that creates a foreign key to it unconditionally fails
    there with "no unique constraint matching given keys". Such a reference must sit
    in a ``CREATE TABLE IF NOT EXISTS`` (a no-op there) or in a ``DO`` block that
    checks the key first (20261120090000_role_permissions.sql, edited before 1.0).
    """
    order = [path.name for path in migration_runner.discover_migration_files(ROOT)]
    identity_index = order.index(MIGRATION)
    needing_global_key = [
        name
        for name in order[:identity_index]
        if any(
            _GLOBAL_WORKFLOW_KEY.search(statement)
            and _runs_on_a_fresh_baseline(statement)
            and not (
                statement.startswith("DO $$")
                and "a.attname = 'workflow_id'" in statement
                and "REFERENCES public.registry_workflows(account_id, workflow_id)" in statement
            )
            for statement in _statements(
                (ROOT / "db" / "migrations" / name).read_text(encoding="utf-8")
            )
        )
    ]
    assert needing_global_key == []
    assert "UNIQUE (workflow_id)" not in _baseline()


def test_the_identity_key_exists_before_any_composite_reference_and_after_every_drop() -> None:
    """Within the migration, references are dropped, the key re-made, then references added."""
    statements = _statements(_migration_sql())
    key = "registry_workflows_account_id_workflow_id_key"
    drop_key = next(i for i, s in enumerate(statements) if f"DROP CONSTRAINT IF EXISTS {key}" in s)
    add_key = next(i for i, s in enumerate(statements) if f"ADD CONSTRAINT {key} UNIQUE" in s)
    assert drop_key < add_key
    for name in WORKFLOW_REFERENCES:
        drop = [i for i, s in enumerate(statements) if f"DROP CONSTRAINT IF EXISTS {name}" in s]
        add = [i for i, s in enumerate(statements) if f"ADD CONSTRAINT {name} FOREIGN KEY" in s]
        assert drop and max(drop) < drop_key, name
        assert add and min(add) > add_key, name
