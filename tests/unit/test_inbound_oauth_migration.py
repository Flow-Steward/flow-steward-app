"""The 0.9.8 inbound-OAuth migration and the release decision it drives.

0.9.7 put five OAuth tables into the db/schema/ baseline only. An installation updated
from 0.9.6 never got them: its installer check compared db/migrations/ (empty) with the
ledger (empty), found nothing to do and printed "this update carried no database
migration", and every later step stamped the new baseline fingerprint without running
its DDL. The migration below carries the same DDL to such databases; these tests pin
that it is the baseline's DDL and that the release check now stops on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.application.tooling.db import migration_runner, upgrade_db
from core.infrastructure.db.schema_state import split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261101153533_inbound_oauth_tables.sql"
OAUTH_TABLES = (
    "oauth_clients",
    "oauth_grants",
    "oauth_authorizations",
    "oauth_access_tokens",
    "oauth_refresh_tokens",
)
# The text the installer gateway turns into release_migration_required.
_RELEASE_MIGRATION_REFUSAL = "Migration history already exists but does not match this release"


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


def _baseline_oauth_statements() -> list[str]:
    baseline = (ROOT / "db" / "schema" / "001_schema_part_12.sql").read_text(encoding="utf-8")
    return [
        statement
        for statement in _statements(baseline)
        if re.match(r"CREATE (TABLE|INDEX \w+ ON) public\.oauth_", statement)
    ]


def _migration_sql() -> str:
    return (ROOT / "db" / "migrations" / MIGRATION).read_text(encoding="utf-8")


def test_the_migration_is_the_baseline_oauth_ddl_made_idempotent() -> None:
    migration = _statements(_migration_sql())

    assert all(
        statement.startswith(("CREATE TABLE IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS "))
        for statement in migration
    ), "every statement must be a no-op on a fresh install that already has the baseline"
    assert [statement.replace(" IF NOT EXISTS", "", 1) for statement in migration] == (
        _baseline_oauth_statements()
    )
    created = re.findall(r"CREATE TABLE IF NOT EXISTS public\.(\w+)", _migration_sql())
    assert tuple(created) == OAUTH_TABLES


def test_the_migration_runs_inside_the_runner_transaction() -> None:
    sql = _migration_sql()

    assert migration_runner.is_valid_migration_filename(MIGRATION)
    assert migration_runner.parse_transactional(sql) is True
    assert migration_runner.find_explicit_transaction_control(sql) == []
    assert migration_runner.migration_has_executable_sql(sql)
    assert upgrade_db.pre_release_migrations([MIGRATION]) == []


class _Connection:
    autocommit = False

    def cursor(self):
        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, *_args, **_kwargs) -> None:
                return None

            def fetchone(self):
                # No upgrade-phase table yet, as on a 0.9.7 database.
                return None

            def fetchall(self):
                return []

        return _Cursor()

    def close(self) -> None:
        return None


@pytest.fixture
def released_097_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger a 0.9.6 or 0.9.7 installation has: present and empty."""
    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.init_db, "validate_bootstrap_layout", lambda: None)
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _Connection())
    monkeypatch.setattr(upgrade_db.migration_runner, "migrations_table_exists", lambda _c: True)
    monkeypatch.setattr(upgrade_db.migration_runner, "fetch_applied_migrations", lambda _c: {})
    # What the 0.9.7 update left behind: the bootstrap seed stamped the new baseline.
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda _conn: {
            "current": {"schema_fingerprint": "stamped"},
            "expected_fingerprint": "stamped",
            "schema_needs_reconcile": False,
            "seed_needs_upgrade": False,
            "needs_upgrade": False,
        },
    )


@pytest.mark.usefixtures("released_097_database")
def test_the_release_check_stops_an_update_that_carries_the_migration() -> None:
    """The installer's precheck (`upgrade_db --baseline-migration-history`) on both profiles."""
    with pytest.raises(RuntimeError) as raised:
        upgrade_db.baseline_migration_history(author="installer")

    assert _RELEASE_MIGRATION_REFUSAL in str(raised.value)


@pytest.mark.usefixtures("released_097_database")
def test_database_status_reports_the_migration_as_pending_despite_a_stamped_fingerprint() -> None:
    status = upgrade_db.inspect_database_upgrade()

    assert status["pending_migrations"] == [
        MIGRATION,
        "20261103090000_retired_agent_type.sql",
        "20261103090000_secret_catalog.sql",
    ]
    assert status["needs_upgrade"] is True
