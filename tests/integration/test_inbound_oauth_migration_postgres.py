"""A 0.9.6-shaped database receives the inbound-OAuth tables through the upgrade path.

0.9.7 added the five tables to the db/schema/ baseline only. An installation updated
from 0.9.6 kept a database without them, `database status` called it clean because the
bootstrap seed stamps the baseline fingerprint without running DDL, and inbound OAuth
reported storage_missing. Every database here is a disposable one created by this test.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from core.application.tooling.db import migration_runner, upgrade_db, upgrade_phases
from core.application.tooling.db.init_db import bootstrap_canonical_schema_if_empty
from core.application.tooling.db.models import DatabaseStatusRequest
from core.application.tooling.db.service import DatabaseToolingService
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.schema_state import record_canonical_schema_state
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261101153533_inbound_oauth_tables.sql"
OAUTH_TABLES = (
    "oauth_clients",
    "oauth_grants",
    "oauth_authorizations",
    "oauth_access_tokens",
    "oauth_refresh_tokens",
)


@pytest.fixture(scope="module")
def create_database():
    base = str(resolve_test_db_dsn() or "").strip()
    if not base:
        pytest.skip("FS_DB_DSN is required for disposable upgrade verification")
    admin = psycopg.connect(base, autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone()[0] == "flow_steward_audit", "refuse shared product database"
    created: list[str] = []

    def create() -> str:
        name = "fs_oauth_migration_" + uuid4().hex[:12]
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        dsn = make_conninfo(base, dbname=name)
        bootstrap_canonical_schema_if_empty(dsn=dsn, root=ROOT)
        return dsn

    try:
        yield create
    finally:
        for name in created:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
        admin.close()


@pytest.fixture(scope="module")
def fresh_dsn(create_database) -> str:
    """A fresh install: the baseline, which already has the five tables."""
    return create_database()


def _catalog(dsn: str) -> dict[str, list[tuple]]:
    """Everything that defines the five tables, in a form two databases can compare."""
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, ordinal_position, data_type, udt_name,
                   character_maximum_length, column_default, is_nullable,
                   col_description(format('public.%%I', table_name)::regclass, ordinal_position)
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = ANY(%s)
             ORDER BY table_name, ordinal_position
            """,
            (list(OAUTH_TABLES),),
        )
        columns = cur.fetchall()
        cur.execute(
            """
            SELECT c.conrelid::regclass::text, c.conname, c.contype,
                   pg_get_constraintdef(c.oid, true)
              FROM pg_catalog.pg_constraint AS c
              JOIN pg_catalog.pg_class AS t ON t.oid = c.conrelid
              LEFT JOIN pg_catalog.pg_class AS f ON f.oid = c.confrelid
             WHERE t.relname = ANY(%s) OR f.relname = ANY(%s)
             ORDER BY 1, 2
            """,
            (list(OAUTH_TABLES), list(OAUTH_TABLES)),
        )
        constraints = cur.fetchall()
        cur.execute(
            """
            SELECT tablename, indexname, indexdef
              FROM pg_catalog.pg_indexes
             WHERE schemaname = 'public' AND tablename = ANY(%s)
             ORDER BY tablename, indexname
            """,
            (list(OAUTH_TABLES),),
        )
        indexes = cur.fetchall()
        cur.execute(
            """
            SELECT c.relname, c.relkind, c.relpersistence, obj_description(c.oid, 'pg_class'),
                   (SELECT count(*) FROM pg_catalog.pg_trigger AS g
                     WHERE g.tgrelid = c.oid AND NOT g.tgisinternal)
              FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relname = ANY(%s)
             ORDER BY c.relname
            """,
            (list(OAUTH_TABLES),),
        )
        tables = cur.fetchall()
    return {"tables": tables, "columns": columns, "constraints": constraints, "indexes": indexes}


def _ledger(dsn: str) -> list[tuple[str, str]]:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT migration_id, checksum FROM public.platform_schema_migrations ORDER BY 1"
        )
        return [(str(row[0]), str(row[1])) for row in cur.fetchall()]


def _released_checksum(migration: str = MIGRATION) -> str:
    payload = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))
    return str(payload["migrations"][migration])


#: Every migration this tree ships; a later one (the secret catalog) is pending too.
ALL_MIGRATIONS = sorted(path.name for path in (ROOT / "db" / "migrations").glob("*.sql"))


def _full_ledger() -> list[tuple[str, str]]:
    return [(name, _released_checksum(name)) for name in ALL_MIGRATIONS]


def _shape_as_installed_by_096(dsn: str) -> None:
    """The baseline 0.9.6 shipped, with the ledger and stamp a 0.9.7 update left."""
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = True
    try:
        migration_runner.ensure_migrations_table(conn)  # the installer's empty ledger
        with conn.cursor() as cur:
            # No CASCADE: nothing in the 0.9.6 baseline may depend on these tables.
            cur.execute(
                sql.SQL("DROP TABLE {}").format(
                    sql.SQL(", ").join(
                        sql.Identifier("public", table) for table in reversed(OAUTH_TABLES)
                    )
                )
            )
        record_canonical_schema_state(
            conn,
            root=ROOT,
            applied_by="installer",
            metadata={"source": "flow-steward database bootstrap-seed"},
        )
    finally:
        conn.close()


def _status(dsn: str):
    return DatabaseToolingService(project_root=ROOT).status(DatabaseStatusRequest(dsn=dsn))


def _apply(dsn: str) -> dict:
    """The migration step `database upgrade` runs on a database that has a ledger."""
    conn = psycopg_runtime.connect(dsn)
    try:
        applied = migration_runner.apply_pending_migrations(conn)
        conn.autocommit = True
        # database upgrade runs the data phases after the migrations (ADR 0054).
        upgrade_phases.run_pending_phases(
            conn, applied_migrations=set(migration_runner.fetch_applied_migrations(conn))
        )
        return applied
    finally:
        conn.close()


def test_an_upgraded_096_database_gets_the_tables_a_fresh_install_has(
    create_database, fresh_dsn
) -> None:
    dsn = create_database()
    _shape_as_installed_by_096(dsn)
    assert _catalog(dsn)["tables"] == []

    before = _status(dsn)
    assert before.status == "needs_upgrade"
    assert before.details["pending_migrations"] == ALL_MIGRATIONS
    # The defect: the stamped fingerprint alone reported this database as current.
    assert before.details["schema_needs_reconcile"] is False
    with pytest.raises(RuntimeError, match="does not match this release"):
        upgrade_db.baseline_migration_history(dsn=dsn, author="installer")

    first = _apply(dsn)

    assert first["applied"] == ALL_MIGRATIONS
    assert _catalog(dsn) == _catalog(fresh_dsn)
    assert len(_catalog(dsn)["tables"]) == len(OAUTH_TABLES)
    assert _ledger(dsn) == _full_ledger()

    again = _apply(dsn)

    assert again["applied"] == []
    assert again["skipped"] == ALL_MIGRATIONS
    assert _ledger(dsn) == _full_ledger()
    after = _status(dsn)
    assert after.status == "ok"
    assert after.details["pending_migrations"] == []
    upgrade_db.baseline_migration_history(dsn=dsn, author="installer")  # the precheck passes


def test_a_fresh_install_records_the_migration_once_and_changes_nothing(fresh_dsn) -> None:
    baseline = _catalog(fresh_dsn)
    assert len(baseline["tables"]) == len(OAUTH_TABLES)

    # The installer's schema step on a fresh database: no ledger yet, so it verifies the
    # baseline, creates the ledger and applies every migration -- here, a no-op.
    stamped = upgrade_db.baseline_migration_history(dsn=fresh_dsn, author="installer")
    assert stamped["applied"] == ALL_MIGRATIONS
    assert _catalog(fresh_dsn) == baseline

    # init_db.initialize_database runs the same runner on every start.
    rerun = migration_runner.run_pending_migrations(dsn=fresh_dsn)
    assert rerun["applied"] == []
    assert rerun["skipped"] == ALL_MIGRATIONS
    assert _catalog(fresh_dsn) == baseline
    assert _ledger(fresh_dsn) == _full_ledger()
