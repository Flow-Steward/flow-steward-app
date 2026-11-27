"""The account-scoped registry history migration carries the baseline's change (ADR 0020, C5).

Installed databases never re-read db/schema/, so the rule that every history entry
of a workflow, a field or a data contract names its account comes with a migration
that gives older entries their account, is a no-op on a fresh installation and is
safe to run again. The data-level behaviour is tested in
``tests/db/test_account_scoped_registry_ops_postgres.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import canonical_schema_fingerprint, split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261127090000_account_scoped_registry_ops.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"
CHECK = "registry_changelog_account_keyed_owner_check"


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
    statements = _statements(_migration_sql())
    assert len(statements) == 4
    backfills, (drop, add) = statements[:2], statements[2:]
    for backfill in backfills:
        # Only entries without an account are touched, so a second run changes nothing.
        assert backfill.startswith("UPDATE public.registry_changelog AS c SET account_id = ")
        assert "WHERE c.account_id IS NULL" in backfill
    assert drop.endswith(f"DROP CONSTRAINT IF EXISTS {CHECK}")
    assert f"ADD CONSTRAINT {CHECK} CHECK" in add


def test_older_entries_take_the_account_their_snapshot_names() -> None:
    keyed, other = _statements(_migration_sql())[:2]
    # A snapshot account is used only when it is a well-formed id of an existing
    # account; an account-keyed entry without one was the installation account's.
    for backfill in (keyed, other):
        assert "FROM public.accounts AS a" in backfill
        assert "(c.snapshot ->> 'account_id') ~* '^[0-9a-f]{8}-" in backfill
    assert "c.entity_type IN ('workflow', 'field', 'data_contract')" in keyed
    assert "'00000000-0000-0000-0000-000000000002'::uuid" in keyed
    assert "c.entity_type NOT IN ('workflow', 'field', 'data_contract')" in other
    assert "00000000-0000-0000-0000-000000000002" not in other
    for pattern in (r"\bDELETE\s+FROM\b", r"\bTRUNCATE\b", r"\bINSERT\s+INTO\b"):
        assert not re.search(pattern, " ".join((keyed, other))), pattern


def test_the_migration_makes_the_change_of_the_baseline() -> None:
    (add,) = [s for s in _statements(_migration_sql()) if s.startswith("ALTER") and "ADD" in s]
    baseline = [s for s in _baseline_statements() if CHECK in s]
    assert baseline == [add]
    for entity_type in ("workflow", "field", "data_contract"):
        assert f"'{entity_type}'::text" in add


def test_the_migration_and_the_baseline_are_recorded() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )
    assert manifest["baseline_schema_fingerprint"] == canonical_schema_fingerprint(ROOT)


def test_every_source_slice_ships_the_migration_and_the_rules() -> None:
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
