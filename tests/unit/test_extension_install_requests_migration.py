"""Extension install requests: the table and its migration (ADR 0084).

Installed databases never re-read db/schema/, so the table comes with a migration that
is a no-op on a fresh installation and is safe to run again.
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
MIGRATION = "20261127210000_extension_install_requests.sql"
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


def test_one_pending_request_per_account_and_extension() -> None:
    joined = "\n".join(_normalized(_migration_sql()))

    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS extension_install_requests_pending_key "
        "ON public.extension_install_requests USING btree (account_id, extension_id) "
        "WHERE status = 'pending'"
    ) in joined
    assert "CHECK (status IN ('pending', 'approved', 'declined', 'cancelled'))" in joined
    assert "account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE" in joined
    assert "requested_by uuid REFERENCES public.users(id) ON DELETE SET NULL" in joined
    assert "decided_by uuid REFERENCES public.users(id) ON DELETE SET NULL" in joined
    # Bounded free text, and a decision time exactly when the request is answered.
    assert "CHECK (length(note) <= 500)" in joined
    assert "CHECK (length(decline_reason) <= 500)" in joined
    assert "CHECK ((status = 'pending') = (decided_at IS NULL))" in joined


def test_the_runtime_contract_expects_the_table_and_its_pending_key() -> None:
    assert "extension_install_requests" in REQUIRED_RUNTIME_TABLES
    assert "extension_install_requests_pending_key" in REQUIRED_RUNTIME_INDEXES
    assert _REQUIRED_RUNTIME_INDEX_DEFINITIONS["extension_install_requests_pending_key"] == (
        "UNIQUE INDEX",
        "extension_install_requests",
        "(account_id, extension_id)",
        "WHERE (status = 'pending'::text)",
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
