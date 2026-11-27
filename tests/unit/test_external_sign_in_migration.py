"""The external sign-in migration carries the baseline's DDL to installed databases.

Installed databases never re-read db/schema/. The identity and state tables and the
two session columns the baseline declares must therefore come with the
migration too, in a form that is a no-op on a fresh installation and safe to run again
on an upgraded one (ADR 0083).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_contract_catalog import (
    REQUIRED_RUNTIME_COLUMNS,
    REQUIRED_RUNTIME_TABLES,
)
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261126150000_external_sign_in.sql"
SLICES = ROOT / "infra" / "docker" / "source-slices"

_IDEMPOTENT_PREFIXES = (
    "CREATE TABLE IF NOT EXISTS ",
    "CREATE INDEX IF NOT EXISTS ",
    "ALTER TABLE ONLY public.auth_sessions ADD COLUMN IF NOT EXISTS ",
)
_BASELINE_FORMS = (
    ("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE "),
    ("CREATE INDEX IF NOT EXISTS ", "CREATE INDEX "),
    ("ADD COLUMN IF NOT EXISTS ", "ADD COLUMN "),
)


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
    start = text.index("-- External sign-in (ADR 0083).")
    # Each baseline block ends with its own marker; later blocks belong to their migrations.
    end = text.index(f"-- End of block: migration {MIGRATION}.", start)
    return _statements(text[start:end])


def _as_baseline(statement: str) -> str:
    for idempotent, plain in _BASELINE_FORMS:
        statement = statement.replace(idempotent, plain, 1)
    return statement


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
    assert [_as_baseline(item) for item in _statements(_migration_sql())] == _baseline_block()


def test_identities_are_keyed_by_issuer_and_subject_within_an_account() -> None:
    statements = _statements(_migration_sql())
    identities = next(
        item
        for item in statements
        if "public.external_identities (" in item and "CREATE TABLE" in item
    )
    method = next(item for item in statements if "ADD COLUMN IF NOT EXISTS auth_method" in item)

    # Unique per account: one account can never block, or reach, another account's link.
    assert "UNIQUE (account_id, issuer, subject)" in identities
    assert "UNIQUE (issuer, subject)" not in identities
    assert "account_id uuid NOT NULL REFERENCES public.accounts(id)" in identities
    assert "email" not in identities
    assert "DEFAULT 'local'::text NOT NULL" in method
    assert "ARRAY['local'::text, 'external'::text]" in method


def test_core_keeps_no_sign_in_configuration_table() -> None:
    """Sign-in methods and their configuration belong to platform modules (ADR 0083)."""
    sql = _migration_sql()
    created = [item for item in _statements(sql) if item.startswith("CREATE TABLE")]

    assert sorted(item.split()[5] for item in created) == [
        "public.external_identities",
        "public.external_sign_in_states",
    ]
    assert "client_secret" not in sql and "discovery" not in sql


def test_the_migration_is_recorded_and_required_at_runtime() -> None:
    manifest = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))

    assert manifest["migrations"][MIGRATION] == migration_runner.migration_checksum(
        _migration_sql()
    )
    for table in ("external_identities", "external_sign_in_states"):
        assert table in REQUIRED_RUNTIME_TABLES
    assert ("auth_sessions", "auth_method") in REQUIRED_RUNTIME_COLUMNS


def test_every_source_slice_ships_the_migration() -> None:
    for name in ("app.json", "compact.json", "control.json", "worker.json"):
        payload = json.loads((SLICES / name).read_text(encoding="utf-8"))
        paths = {str(item.get("path")) for item in payload.get("package_data_paths") or ()}
        assert f"db/migrations/{MIGRATION}" in paths, name


def test_the_runtime_contract_refuses_a_database_with_a_global_identity_key() -> None:
    from core.infrastructure.db.schema_contract import missing_required_indexes

    name = "external_identities_account_issuer_subject_key"

    class _Cursor:
        def __init__(self, definition: str) -> None:
            self._definition = definition

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, *_args):
            return None

        def fetchall(self):
            return [(name, self._definition)] if self._definition else []

    class _Store:
        def __init__(self, definition: str) -> None:
            self._definition = definition

        def cursor(self):
            return _Cursor(self._definition)

    per_account = (
        f"CREATE UNIQUE INDEX {name} ON public.external_identities"
        " USING btree (account_id, issuer, subject)"
    )
    assert missing_required_indexes(_Store(per_account), [name]) == []
    assert missing_required_indexes(_Store(""), [name]) == [name]
