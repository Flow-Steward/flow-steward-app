"""Roles per account: the membership carries the role (ADR 0085).

Installed databases never re-read db/schema/, so the change comes with a migration
that is a no-op on a fresh installation and is safe to run again. Its behaviour on
PostgreSQL 17 was checked fresh, re-run and upgraded from the 0.9.23 baseline with
people in two accounts.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_contract_catalog import (
    _EXPECTED_MEMBERSHIP_CONSTRAINTS,
    _EXPECTED_USER_ADMIN_COLUMNS,
    _EXPECTED_USER_ADMIN_INDEXES,
    REQUIRED_RUNTIME_COLUMNS,
)
from core.infrastructure.db.schema_state import canonical_schema_fingerprint, split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261127170000_roles_per_account.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"


def _normalized(sql: str) -> list[str]:
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
        for statement in _normalized(path.read_text(encoding="utf-8"))
    ]


def test_the_migration_runs_inside_the_runner_transaction() -> None:
    sql = _migration_sql()

    assert migration_runner.is_valid_migration_filename(MIGRATION)
    assert migration_runner.parse_transactional(sql) is True
    assert migration_runner.find_explicit_transaction_control(sql) == []
    assert upgrade_db.pre_release_migrations([MIGRATION]) == []


def test_the_global_rows_are_deleted_only_after_the_backfill() -> None:
    joined = "\n".join(_normalized(_migration_sql()))

    backfill = joined.index("UPDATE public.user_account_memberships AS m SET role_id")
    report = joined.index("RAISE NOTICE")
    delete = joined.index("DELETE FROM public.user_roles")
    check = joined.index("ADD CONSTRAINT user_roles_scoped_only_check")
    assert backfill < report < delete < check
    # Only the global rows go; scoped rows are kept.
    assert (
        "DELETE FROM public.user_roles AS ur WHERE ur.workspace_id IS NULL "
        "AND ur.project_id IS NULL AND ur.workflow_id IS NULL"
    ) in joined


def test_the_backfill_keeps_the_highest_system_role_and_touches_unset_memberships_only() -> None:
    [backfill] = [
        s
        for s in _normalized(_migration_sql())
        if s.startswith("UPDATE public.user_account_memberships")
    ]

    assert "r.key IN ('admin', 'operator', 'viewer')" in backfill
    assert "CASE r.key WHEN 'admin' THEN 3 WHEN 'operator' THEN 2 ELSE 1 END DESC" in backfill
    assert backfill.endswith("AND m.role_id IS NULL")


def test_a_person_keeps_a_membership_of_their_home_account() -> None:
    [insert] = [
        s
        for s in _normalized(_migration_sql())
        if s.startswith("INSERT INTO public.user_account_memberships")
    ]

    assert "m.account_id = u.account_id" in insert
    assert "u.deleted_at IS NULL" in insert
    assert insert.endswith("ON CONFLICT (user_id, account_id) DO NOTHING")


def test_a_membership_without_a_role_grants_nothing_and_is_reported() -> None:
    statements = _normalized(_migration_sql())
    joined = "\n".join(statements)

    assert "ADD COLUMN IF NOT EXISTS role_id uuid" in joined
    assert "role_id uuid NOT NULL" not in joined
    assert "REFERENCES public.roles(id) ON DELETE SET NULL" in joined
    assert "membership(s) of live people have no role (no access)" in joined


def test_scope_limits_are_keyed_per_account() -> None:
    joined = "\n".join(_normalized(_migration_sql()))

    assert "ADD CONSTRAINT scoped_role_users_pkey PRIMARY KEY (user_id, account_id)" in joined


def test_every_added_constraint_is_dropped_first() -> None:
    sql = _migration_sql()
    for statement in _normalized(sql):
        if " ADD CONSTRAINT " in statement.upper():
            name = statement.split("ADD CONSTRAINT ", 1)[1].split()[0]
            assert f"DROP CONSTRAINT IF EXISTS {name}" in sql


def test_the_migration_makes_the_change_of_the_baseline() -> None:
    baseline = set(_baseline_statements())
    for statement in _normalized(_migration_sql()):
        assert statement in baseline, statement


def test_the_runtime_contract_expects_the_membership_role() -> None:
    assert ("user_account_memberships", "role_id") in REQUIRED_RUNTIME_COLUMNS
    assert _EXPECTED_USER_ADMIN_COLUMNS[("user_account_memberships", "role_id")] == (
        "uuid",
        False,
        None,
    )
    assert _EXPECTED_MEMBERSHIP_CONSTRAINTS["user_account_memberships_role_id_fkey"][1] == (
        "FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE SET NULL"
    )
    assert _EXPECTED_USER_ADMIN_INDEXES["idx_user_account_memberships_role"][-2] == (
        "account_id",
        "role_id",
    )


def test_the_migration_and_the_baseline_are_recorded() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )
    assert manifest["baseline_schema_fingerprint"] == canonical_schema_fingerprint(ROOT)


def test_every_source_slice_ships_the_migration() -> None:
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name
