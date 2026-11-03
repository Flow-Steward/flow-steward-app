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
AGENT_MIGRATION = "20261103090000_retired_agent_type.sql"
SECRET_CATALOG_MIGRATION = "20261103090000_secret_catalog.sql"
#: Every migration this tree ships, in apply order; the secret catalog is pending too.
MIGRATIONS = [MIGRATION, AGENT_MIGRATION, SECRET_CATALOG_MIGRATION]
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


def _released_checksum(name: str = MIGRATION) -> str:
    payload = json.loads((ROOT / "db" / "released_migrations.json").read_text(encoding="utf-8"))
    return str(payload["migrations"][name])


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
        # database upgrade runs the data phases after the migrations (ADR 0056).
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
    assert before.details["pending_migrations"] == MIGRATIONS
    # The defect: the stamped fingerprint alone reported this database as current.
    assert before.details["schema_needs_reconcile"] is False
    with pytest.raises(RuntimeError, match="does not match this release"):
        upgrade_db.baseline_migration_history(dsn=dsn, author="installer")

    first = _apply(dsn)

    assert first["applied"] == MIGRATIONS
    assert _catalog(dsn) == _catalog(fresh_dsn)
    assert len(_catalog(dsn)["tables"]) == len(OAUTH_TABLES)
    assert _ledger(dsn) == [(name, _released_checksum(name)) for name in MIGRATIONS]

    again = _apply(dsn)

    assert again["applied"] == []
    assert again["skipped"] == MIGRATIONS
    assert _ledger(dsn) == [(name, _released_checksum(name)) for name in MIGRATIONS]
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
    assert stamped["applied"] == MIGRATIONS
    assert _catalog(fresh_dsn) == baseline

    # init_db.initialize_database runs the same runner on every start.
    rerun = migration_runner.run_pending_migrations(dsn=fresh_dsn)
    assert rerun["applied"] == []
    assert rerun["skipped"] == MIGRATIONS
    assert _catalog(fresh_dsn) == baseline
    assert _ledger(fresh_dsn) == [(name, _released_checksum(name)) for name in MIGRATIONS]


def test_upgrade_repairs_legacy_agent_type_without_losing_agents(create_database) -> None:
    """A stamped legacy installation must request the migration and accept new agents."""
    from core.application.registry.db_store import RegistryDBStore

    dsn = create_database()
    # Simulate a 0.9.8 installation whose prior OAuth migration has already run.
    with psycopg.connect(dsn, autocommit=True) as conn:
        migration_runner.ensure_migrations_table(conn)
        conn.execute(
            "INSERT INTO public.platform_schema_migrations (migration_id, checksum) VALUES (%s, %s)",
            (MIGRATION, _released_checksum()),
        )
        conn.execute("CREATE TABLE public.agent_types (id uuid PRIMARY KEY)")
        conn.execute(
            "INSERT INTO public.agent_types (id) VALUES ('11111111-1111-1111-1111-111111111111')"
        )
        conn.execute("ALTER TABLE public.registry_agents ADD COLUMN agent_type_id uuid")
        conn.execute(
            "INSERT INTO public.registry_agents (agent_id, display_name, agent_type_id) VALUES ('business_operator', 'Existing agent', '11111111-1111-1111-1111-111111111111')"
        )
        conn.execute("ALTER TABLE public.registry_agents ALTER COLUMN agent_type_id SET NOT NULL")
        conn.execute(
            "ALTER TABLE public.registry_agents ALTER COLUMN agent_type_id SET DEFAULT gen_random_uuid()"
        )
        conn.execute(
            "ALTER TABLE public.registry_agents ADD CONSTRAINT registry_agents_legacy_agent_type_fk FOREIGN KEY (agent_type_id) REFERENCES public.agent_types(id)"
        )
    store = RegistryDBStore(dsn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.save_agent(
            {"agent_id": "new_agent", "display_name": "New agent"},
            create_only=True,
            sync_relationships=False,
        )
    status = upgrade_db.inspect_database_upgrade(dsn=dsn)
    assert AGENT_MIGRATION in status["pending_migrations"]
    first = upgrade_db.upgrade_database(dsn=dsn, author="test", maintenance_confirmed=True)
    assert AGENT_MIGRATION in first["schema_migrations"]["applied"]
    again = upgrade_db.upgrade_database(dsn=dsn, author="test", maintenance_confirmed=True)
    assert again["schema_migrations"]["applied"] == []
    assert AGENT_MIGRATION in again["schema_migrations"]["skipped"]
    store.save_agent(
        {"agent_id": "new_agent", "display_name": "New agent"},
        create_only=True,
        sync_relationships=False,
    )
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT agent_id, display_name FROM public.registry_agents WHERE agent_id IN ('business_operator', 'new_agent') ORDER BY agent_id"
        ).fetchall()
    assert rows == [("business_operator", "Existing agent"), ("new_agent", "New agent")]
