"""The secret catalog on real PostgreSQL: the move, preflight, approvals and the use check.

Every database here is a disposable one created by this test (ADR 0054, ADR 0055).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from core.application.management import project_connection_secrets
from core.application.management.errors import ServiceConflictError, ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.secrets.approval_gate import SecretConfigurationUnapproved
from core.application.secrets.service import SecretCatalogService
from core.application.tooling.db import migration_runner, upgrade_db, upgrade_phases
from core.application.tooling.db.init_db import bootstrap_canonical_schema_if_empty
from core.application.tooling.db.upgrade_preflight import preflight
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.schema_contract import enforce_runtime_schema_contract_dsn
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
CATALOG_MIGRATION = "20261103090000_secret_catalog.sql"
OAUTH_MIGRATION = "20261101153533_inbound_oauth_tables.sql"
ACCOUNT = "00000000-0000-0000-0000-000000000002"
WORKSPACE = "00000000-0000-0000-0000-000000000003"
NEW_TABLES = (
    "catalog_secret_approvals",
    "catalog_secret_bindings",
    "catalog_secrets",
    "platform_upgrade_phases",
)


@pytest.fixture(scope="module")
def create_database():
    base = str(resolve_test_db_dsn() or "").strip()
    if not base:
        pytest.skip("FS_DB_DSN is required for disposable catalog verification")
    admin = psycopg.connect(base, autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone()[0] == "flow_steward_audit", "refuse shared product database"
    created: list[str] = []

    def create() -> str:
        name = "fs_secret_catalog_" + uuid4().hex[:12]
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


def _connect(dsn: str):
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = True
    return conn


def _shape_as_097(dsn: str) -> None:
    """A database 0.9.7 left: no catalog tables, a ledger without the catalog move."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP TABLE {}").format(
                    sql.SQL(", ").join(sql.Identifier("public", t) for t in NEW_TABLES)
                )
            )
        migration_runner.ensure_migrations_table(conn)
        checksum = migration_runner.migration_checksum(
            (ROOT / "db" / "migrations" / OAUTH_MIGRATION).read_text(encoding="utf-8")
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_schema_migrations (migration_id, checksum) VALUES (%s, %s)",
                (OAUTH_MIGRATION, checksum),
            )
    finally:
        conn.close()


def _owner_rows(cur) -> None:
    """The default account and workspace the canonical seed would create."""
    cur.execute(
        "INSERT INTO accounts (id, account_key, display_name) VALUES (%s, 'default', 'Default') "
        "ON CONFLICT DO NOTHING",
        (ACCOUNT,),
    )
    cur.execute(
        "INSERT INTO workspaces (id, account_id, workspace_key, display_name) "
        "VALUES (%s, %s, 'default', 'Default') ON CONFLICT DO NOTHING",
        (WORKSPACE, ACCOUNT),
    )


def _project(cur, name: str) -> str:
    _owner_rows(cur)
    cur.execute(
        "INSERT INTO projects (name, account_id, workspace_id) VALUES (%s, %s, %s) RETURNING id::text",
        (name, ACCOUNT, WORKSPACE),
    )
    return cur.fetchone()[0]


def _connection(cur, project_id: str, *, label: str, refs: dict[str, str], **fields) -> str:
    connection_id = "conn_" + uuid4().hex[:10]
    cur.execute(
        """
        INSERT INTO project_connections (
            connection_id, project_id, account_id, extension_id, display_name, secret_refs,
            runtime_mode, remote_base_url
        ) VALUES (%s, %s, %s, 'demo', %s, %s::jsonb, %s, %s)
        """,
        (
            connection_id,
            project_id,
            ACCOUNT,
            label,
            json.dumps({"refs": refs}),
            fields.get("runtime_mode"),
            fields.get("remote_base_url"),
        ),
    )
    return connection_id


def _value(cur, ref_key: str) -> None:
    cur.execute(
        "INSERT INTO secret_refs (account_id, ref_key, encrypted_value) VALUES (%s, %s, 'cipher')",
        (ACCOUNT, ref_key),
    )


def _move(dsn: str) -> dict:
    conn = _connect(dsn)
    try:
        applied = migration_runner.apply_pending_migrations(conn)
        phases = upgrade_phases.run_pending_phases(
            conn, applied_migrations=set(migration_runner.fetch_applied_migrations(conn))
        )
        return {"applied": applied["applied"], "phases": phases}
    finally:
        conn.close()


class _Reader:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def read(self, _context, ref_key):
        self.reads.append(ref_key)
        return f"value-of-{ref_key}"


def _row(conn, connection_id: str) -> dict:
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT *, account_id::text AS account_id, project_id::text AS project_id "
            "FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        return dict(cur.fetchone())


def _human(role: str = "operator") -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT, principal_type=role, auth_source="local", actor_id="person"
    )


def _token(*scopes: str) -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        principal_type="operator",
        auth_source="pat",
        actor_id="token",
        token_scopes=frozenset(scopes),
    )


def test_a_fresh_install_records_the_phase_and_passes_the_startup_contract(
    create_database,
) -> None:
    dsn = create_database()

    upgrade_db.baseline_migration_history(dsn=dsn, author="installer")

    conn = _connect(dsn)
    try:
        assert upgrade_phases.recorded_phases(conn) == {"catalog_secret_initial_approvals"}
    finally:
        conn.close()
    enforce_runtime_schema_contract_dsn(dsn, component="test")


def test_the_move_keeps_values_binds_and_approves_once(create_database) -> None:
    dsn = create_database()
    _shape_as_097(dsn)
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            project = _project(cur, "alpha")
            key = "project_connection:c1:api_token"
            _value(cur, key)
            connection_id = _connection(
                cur, project, label="Shop", refs={"api_token": key, "_oauth_refresh_token": "x"}
            )
            missing = _connection(cur, project, label="Broken", refs={"api_token": "gone"})
        report = preflight(conn, allow_unlabeled_sessions=True, root=ROOT)
    finally:
        conn.close()

    assert not [i for i in report["blocking"] if i["code"] != "unlabeled_sessions"]
    assert [w["code"] for w in report["warnings"]].count("secret_catalog_missing_value") == 1

    moved = _move(dsn)

    assert CATALOG_MIGRATION in moved["applied"]
    assert moved["phases"]["catalog_secret_initial_approvals"]["approved_connections"] == 1
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, storage_ref_key, project_id::text FROM catalog_secrets")
            assert cur.fetchall() == [("Shop / api_token", key, project)]
            cur.execute(
                "SELECT count(*) FROM catalog_secret_bindings WHERE project_connection_id = %s",
                (missing,),
            )
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT approved_by_kind FROM catalog_secret_approvals")
            assert cur.fetchall() == [("upgrade",)]
            cur.execute(
                "SELECT count(*) FROM command_audit_log WHERE command_type = 'secret.approve'"
            )
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT encrypted_value FROM secret_refs WHERE ref_key = %s", (key,))
            assert cur.fetchone()[0] == "cipher"
        # The move wrote the refs projection, and the use check accepts it.
        row = _row(conn, connection_id)
        assert row["secret_refs"]["catalog"]
        reader = _Reader()
        values = project_connection_secrets.resolve_connection_secrets(
            conn,
            account_id=ACCOUNT,
            secret_refs=row["secret_refs"],
            reader=reader,
            connection=row,
        )
        assert values == {"api_token": f"value-of-{key}"}
    finally:
        conn.close()

    # A rerun moves nothing and approves nothing twice.
    again = _move(dsn)
    assert again == {"applied": [], "phases": {}}


def test_a_value_shared_across_projects_blocks_preflight_and_the_migration(
    create_database,
) -> None:
    dsn = create_database()
    _shape_as_097(dsn)
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            _value(cur, "api_token")
            _connection(cur, _project(cur, "one"), label="A", refs={"api_token": "api_token"})
            _connection(cur, _project(cur, "two"), label="B", refs={"api_token": "api_token"})
        report = preflight(conn, allow_unlabeled_sessions=True, root=ROOT)
        assert "secret_catalog_shared_value" in [i["code"] for i in report["blocking"]]
        assert "cipher" not in json.dumps(report), "the report never carries a value"
        with pytest.raises(Exception, match="secret_catalog_shared_value"):
            migration_runner.apply_pending_migrations(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.catalog_secrets')")
            assert cur.fetchone()[0] is None, "the refused migration left nothing behind"
    finally:
        conn.close()


def test_colliding_long_names_still_fit_the_limit(create_database) -> None:
    dsn = create_database()
    _shape_as_097(dsn)
    conn = _connect(dsn)
    long_label = "L" * 210
    try:
        with conn.cursor() as cur:
            project = _project(cur, "names")
            for index in range(2):
                key = f"project_connection:long{index}:token"
                _value(cur, key)
                _connection(cur, project, label=long_label, refs={"token": key})
    finally:
        conn.close()

    _move(dsn)

    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM catalog_secrets ORDER BY name")
            names = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    assert len(names) == 2 and len(set(names)) == 2
    assert all(len(name) <= 200 and name.endswith(")") for name in names)


def _catalog_db(create_database):
    dsn = create_database()
    upgrade_db.baseline_migration_history(dsn=dsn, author="installer")
    conn = _connect(dsn)
    with conn.cursor() as cur:
        project = _project(cur, "approvals")
        connection_id = _connection(
            cur,
            project,
            label="Remote",
            refs={},
            runtime_mode="remote_https",
            remote_base_url="https://api.example.test/v1",
        )
    return conn, project, connection_id, SecretCatalogService(lambda: conn, encrypt=lambda v: "enc")


def test_an_unapproved_configuration_and_a_stale_snapshot_are_refused(create_database) -> None:
    conn, _project_id, connection_id, service = _catalog_db(create_database)
    try:
        status = service.save_connection_values(
            connection_id, _human(), values={"api_token": "typed"}
        )
        assert status["status"] == "approval_required"
        stale = _row(conn, connection_id)
        reader = _Reader()
        with pytest.raises(SecretConfigurationUnapproved):
            project_connection_secrets.resolve_connection_secrets(
                conn,
                account_id=ACCOUNT,
                secret_refs=stale["secret_refs"],
                reader=reader,
                connection=stale,
            )

        # A person approves a new destination while a caller still holds the old row.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE project_connections SET remote_base_url = 'https://new.example.test' "
                "WHERE connection_id = %s",
                (connection_id,),
            )
        fresh = service.connection_approval_status(connection_id, _human())
        service.approve_connections(_human(), approvals={connection_id: fresh["fingerprint"]})

        with pytest.raises(SecretConfigurationUnapproved):
            project_connection_secrets.resolve_connection_secrets(
                conn,
                account_id=ACCOUNT,
                secret_refs=stale["secret_refs"],
                reader=reader,
                connection=stale,
            )
        assert reader.reads == []
        current = _row(conn, connection_id)
        values = project_connection_secrets.resolve_connection_secrets(
            conn,
            account_id=ACCOUNT,
            secret_refs=current["secret_refs"],
            reader=reader,
            connection=current,
        )
        assert set(values) == {"api_token"}
    finally:
        conn.close()


def test_an_oauth_refresh_keeps_the_approval_and_a_new_setting_drops_it(create_database) -> None:
    conn, _project_id, connection_id, service = _catalog_db(create_database)
    try:
        status = service.save_connection_values(connection_id, _human(), values={"api_token": "v"})
        service.approve_connections(_human(), approvals={connection_id: status["fingerprint"]})
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE project_connections
                   SET connection_config = jsonb_build_object(
                       'oauth', jsonb_build_object(
                           'lifecycle_status', 'connected', 'connected_at', 1.0,
                           'expires_at', 2.0, 'granted_scopes', '["read"]'::jsonb))
                 WHERE connection_id = %s
                """,
                (connection_id,),
            )
        assert service.connection_approval_status(connection_id, _human())["status"] == "approved"
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE project_connections SET connection_config = connection_config "
                '|| \'{"site_url": "https://elsewhere.test"}\'::jsonb WHERE connection_id = %s',
                (connection_id,),
            )
        assert (
            service.connection_approval_status(connection_id, _human())["status"]
            == "approval_required"
        )
    finally:
        conn.close()


def test_machine_callers_need_exact_scopes_and_never_approve(create_database) -> None:
    conn, _project_id, connection_id, service = _catalog_db(create_database)
    try:
        with pytest.raises(ServiceForbiddenError):
            service.save_connection_values(
                connection_id, _token("admin:all", "*:*", "secrets:*"), values={"k": "v"}
            )
        status = service.save_connection_values(
            connection_id, _token("secrets:write", "secrets:bind"), values={"k": "v"}
        )
        with pytest.raises(ServiceForbiddenError):
            service.approve_connections(
                _token("secrets:write", "secrets:bind", "admin:all"),
                approvals={connection_id: status["fingerprint"]},
            )
        forged = ScopeContext(account_id=ACCOUNT, principal_type="admin", auth_source="oauth")
        with pytest.raises(ServiceForbiddenError):
            service.approve_connections(forged, approvals={connection_id: status["fingerprint"]})
    finally:
        conn.close()


def test_a_used_secret_cannot_be_deleted_and_survives_its_consumer(create_database) -> None:
    conn, project_id, connection_id, service = _catalog_db(create_database)
    try:
        service.save_connection_values(connection_id, _human(), values={"api_token": "v"})
        secret_id = _row(conn, connection_id)["secret_refs"]["catalog"]["api_token"]
        with pytest.raises(ServiceConflictError):
            service.delete_secret(secret_id, _human())

        from core.application.management.command_services_project_connection import (
            cleanup_project_connection_owned_data,
        )

        cleanup_project_connection_owned_data(
            SimpleNamespace(), connection=_row(conn, connection_id), cur=conn.cursor()
        )
        detail = service.get_secret(secret_id, _human())
        assert detail["usages"] == []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM secret_refs WHERE ref_key = %s",
                (f"catalog_secret:{secret_id}",),
            )
            assert cur.fetchone()[0] == 1, "the shared value outlives the connection"
        service.delete_secret(secret_id, _human())
        assert project_id
    finally:
        conn.close()
