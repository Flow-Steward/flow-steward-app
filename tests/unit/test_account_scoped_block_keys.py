"""Published block keys are unique per account (ADR 0020).

A block derived from an account's workflow, agent or skill is that account's, so two
accounts may each publish a block with the same key; the second account's derived
block used to be dropped because keys were installation-unique. A platform block (no
account) keeps a key no account block uses and is readable by every account.
Installed databases never re-read db/schema/, so the change comes with a migration
that is a no-op on a fresh installation and is safe to run again. Its behaviour on
PostgreSQL 17 was checked fresh, re-run and upgraded from the 0.9.23 baseline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.application.registry import public_contract_projection as projection
from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import canonical_schema_fingerprint, split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261127150000_account_scoped_block_keys.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"
ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
ACCOUNT_B = "22222222-2222-2222-2222-222222222222"


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


def test_the_key_is_the_owner_and_the_block_key() -> None:
    statements = _normalized(_migration_sql())
    joined = "\n".join(statements)

    assert "PRIMARY KEY (owner_key, block_key)" in joined
    assert "owner_key text GENERATED ALWAYS AS (COALESCE((account_id)::text, ''::text))" in joined
    for child in ("registry_published_block_inputs", "registry_published_block_outputs"):
        assert f"{child}_block_field_key UNIQUE (block_owner, block_key, field_id)" in joined
        assert f"{child}_block_fkey FOREIGN KEY (block_owner, block_key)" in joined
    assert "workflow_runtime_dependencies_block_fkey FOREIGN KEY (block_owner, block_key)" in joined


def test_the_platform_or_account_key_check_is_serialized_per_key() -> None:
    """Two concurrent writers of one key cannot both pass the AFTER trigger's check."""
    statements = _normalized(_migration_sql())
    [function] = [
        # The definition, not the trigger that executes it.
        s
        for s in statements
        if "FUNCTION public.registry_published_block_key_scope" in s
        and "pg_advisory_xact_lock" in s
    ]

    lock = function.index("pg_advisory_xact_lock")
    assert "hashtextextended('flow-steward:published-block-key:' || NEW.block_key, 0)" in function
    assert lock < function.index("IF EXISTS")


def test_a_runtime_dependency_with_a_block_key_always_names_its_owner() -> None:
    """A NULL block_owner would switch the composite foreign key off for that row."""
    joined = "\n".join(_normalized(_migration_sql()))

    assert (
        "ADD CONSTRAINT workflow_runtime_dependencies_block_owner_check "
        "CHECK (((block_key IS NULL) OR (block_owner IS NOT NULL)))"
    ) in joined
    # Rows that could not be linked lost their block_key before the check is added.
    assert joined.index("SET block_key = NULL") < joined.index(
        "ADD CONSTRAINT workflow_runtime_dependencies_block_owner_check"
    )


def test_every_statement_is_safe_to_run_again_and_deletes_nothing() -> None:
    for statement in _normalized(_migration_sql()):
        upper = statement.upper()
        assert not upper.startswith(("DELETE", "TRUNCATE", "INSERT")), statement
        if " ADD COLUMN " in upper:
            assert "ADD COLUMN IF NOT EXISTS" in upper
        if " ADD CONSTRAINT " in upper:
            name = statement.split("ADD CONSTRAINT ", 1)[1].split()[0]
            assert f"DROP CONSTRAINT IF EXISTS {name}" in _migration_sql()
        if upper.startswith("UPDATE"):
            # Only rows not yet linked to their block are touched.
            assert "NOT EXISTS" in statement or "IS NULL" in statement


def test_the_migration_makes_the_change_of_the_baseline() -> None:
    baseline = set(_baseline_statements())
    for statement in _normalized(_migration_sql()):
        assert statement in baseline, statement


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


class _ProjectionDb:
    """Account A already owns ``workflow.shared``; account B derives the same key."""

    def __init__(self) -> None:
        self.rows = [
            {
                "block_key": "workflow.shared",
                "account_id": ACCOUNT_A,
                "projection_mode": "derived",
            }
        ]

    def list_fields(self, **_kwargs: Any) -> list[dict]:
        return []

    def list_data_contracts(self, **_kwargs: Any) -> list[dict]:
        return []

    def list_published_block_contracts(self, *, account_id: str | None = None, **_kwargs: Any):
        if not account_id:
            return list(self.rows)
        return [row for row in self.rows if row.get("account_id") in {None, account_id}]


def test_a_second_account_derives_its_own_block_of_the_same_key(monkeypatch) -> None:
    desired = {
        "blocks": {"workflow.shared": {"block_key": "workflow.shared", "account_id": ACCOUNT_B}},
        "fields": {},
        "contracts": {},
        "contract_backrefs": [],
    }
    monkeypatch.setattr(projection, "_build_desired_projection_state", lambda *_a, **_k: desired)

    summary = projection._reconcile_account(_ProjectionDb(), apply=False, account_id=ACCOUNT_B)

    assert summary.errors == 0
    assert summary.created == 1


def test_a_platform_block_keeps_its_key_from_an_account(monkeypatch) -> None:
    db = _ProjectionDb()
    db.rows = [{"block_key": "blkproj:i:op", "account_id": None, "projection_mode": "manual"}]
    desired = {
        "blocks": {"blkproj:i:op": {"block_key": "blkproj:i:op", "account_id": ACCOUNT_B}},
        "fields": {},
        "contracts": {},
        "contract_backrefs": [],
    }
    monkeypatch.setattr(projection, "_build_desired_projection_state", lambda *_a, **_k: desired)

    summary = projection._reconcile_account(db, apply=False, account_id=ACCOUNT_B)

    assert summary.errors == 1
    assert summary.created == 0
