"""Invitations to join an account: the table and its migration (ADR 0085, consent).

Installed databases never re-read db/schema/, so the table comes with a migration that
is a no-op on a fresh installation and is safe to run again. Its behaviour on
PostgreSQL 17 was checked fresh, re-run and upgraded from the 0.9.23 baseline (2635b86f3d).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_contract_catalog import (
    _REQUIRED_RUNTIME_INDEX_DEFINITIONS,
    REQUIRED_RUNTIME_INDEXES,
    REQUIRED_RUNTIME_TABLES,
)
from core.infrastructure.db.schema_state import canonical_schema_fingerprint, split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261127190000_account_invitations.sql"
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


def test_the_migration_runs_inside_the_runner_transaction() -> None:
    sql = _migration_sql()

    assert migration_runner.is_valid_migration_filename(MIGRATION)
    assert migration_runner.parse_transactional(sql) is True
    assert migration_runner.find_explicit_transaction_control(sql) == []
    assert upgrade_db.pre_release_migrations([MIGRATION]) == []


def test_every_statement_is_safe_to_run_again() -> None:
    for statement in _normalized(_migration_sql()):
        assert "IF NOT EXISTS" in statement, statement


def test_the_migration_makes_the_change_of_the_baseline() -> None:
    baseline = {
        statement
        for path in sorted((ROOT / "db" / "schema").glob("001_schema_part_*.sql"))
        for statement in _normalized(path.read_text(encoding="utf-8"))
    }
    for statement in _normalized(_migration_sql()):
        assert statement in baseline, statement
    block = (ROOT / "db" / "schema" / "001_schema_part_12.sql").read_text(encoding="utf-8")
    assert f"-- End of block: migration {MIGRATION}." in block


def test_one_pending_invitation_per_account_and_address() -> None:
    joined = "\n".join(_normalized(_migration_sql()))

    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS account_invitations_pending_key "
        "ON public.account_invitations USING btree (account_id, lower(invited_email)) "
        "WHERE status = 'pending'"
    ) in joined
    assert "CHECK (role IN ('admin', 'operator', 'viewer'))" in joined
    assert "CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled'))" in joined
    # The address is stored as typed; no person needs to exist for it.
    assert "invited_email text NOT NULL" in joined
    assert "CHECK (invited_email <> '' AND length(invited_email) <= 320)" in joined
    # user_id is the person who answered: empty until then, kept as a record after.
    assert "user_id uuid REFERENCES public.users(id) ON DELETE SET NULL" in joined
    assert "account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE" in joined
    # ADR 0085, C1: the link's token names one invitation and only its hash is stored;
    # nothing looks invitations up by the person's address any more.
    assert "token_hash text NOT NULL" in joined
    assert "CONSTRAINT account_invitations_token_hash_key UNIQUE (token_hash)" in joined
    assert "CHECK (length(token_hash) = 64)" in joined
    assert "idx_account_invitations_pending_email" not in joined


def test_the_runtime_contract_expects_the_table_and_its_pending_key() -> None:
    assert "account_invitations" in REQUIRED_RUNTIME_TABLES
    assert "account_invitations_pending_key" in REQUIRED_RUNTIME_INDEXES
    assert "account_invitations_token_hash_key" in REQUIRED_RUNTIME_INDEXES
    assert _REQUIRED_RUNTIME_INDEX_DEFINITIONS["account_invitations_token_hash_key"] == (
        "UNIQUE INDEX",
        "account_invitations",
        "(token_hash)",
    )
    assert _REQUIRED_RUNTIME_INDEX_DEFINITIONS["account_invitations_pending_key"][:3] == (
        "UNIQUE INDEX",
        "account_invitations",
        "(account_id, lower(invited_email))",
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
