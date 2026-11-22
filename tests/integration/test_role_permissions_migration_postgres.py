"""ADR 0076 on a real PostgreSQL: a 0.9.x database gets role permissions, users keep roles.

Every database here is a disposable one created by this test from the canonical
baseline. The "0.9.x" database is the baseline with the ADR 0076 objects taken out
again and users who hold several system roles, which the old forms allowed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from core.application.auth.permissions import SYSTEM_ROLE_DESCRIPTIONS, SYSTEM_ROLE_PERMISSIONS
from core.application.tooling.db import migration_runner
from core.application.tooling.db.init_db import bootstrap_canonical_schema_if_empty
from core.infrastructure.db import psycopg_runtime
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "20261120090000_role_permissions.sql"
TABLES = ("roles", "user_roles", "role_permissions")

#: What 0.9.20 had: no ADR 0076 objects, and one row per (user, role).
_AS_RELEASED_0920 = (
    "DROP INDEX public.ux_user_roles_one_system_role_per_scope",
    "DROP TRIGGER trg_user_roles_role_is_system ON public.user_roles",
    "DROP FUNCTION public.user_roles_copy_role_is_system()",
    "ALTER TABLE public.user_roles DROP CONSTRAINT user_roles_role_is_system_fkey",
    "ALTER TABLE public.user_roles DROP CONSTRAINT user_roles_workflow_scope_check",
    "ALTER TABLE public.user_roles DROP CONSTRAINT user_roles_workspace_id_fkey",
    "ALTER TABLE public.user_roles DROP CONSTRAINT user_roles_project_id_fkey",
    "ALTER TABLE public.user_roles DROP CONSTRAINT user_roles_workflow_id_fkey",
    "ALTER TABLE public.user_roles DROP CONSTRAINT user_roles_assignment_key",
    "ALTER TABLE public.roles DROP CONSTRAINT roles_id_is_system_key",
    "DROP TABLE public.role_permissions",
    "ALTER TABLE public.user_roles DROP COLUMN workspace_id, DROP COLUMN project_id, "
    "DROP COLUMN workflow_id, DROP COLUMN role_is_system",
    "ALTER TABLE public.roles DROP COLUMN is_system, DROP COLUMN description",
    "ALTER TABLE ONLY public.user_roles "
    "ADD CONSTRAINT user_roles_user_id_role_id_key UNIQUE (user_id, role_id)",
)


@pytest.fixture(scope="module")
def create_database():
    base = str(resolve_test_db_dsn() or "").strip()
    if not base:
        pytest.skip("FS_DB_DSN is required for disposable migration verification")
    admin = psycopg.connect(base, autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone()[0] == "flow_steward_audit", "refuse shared product database"
    created: list[str] = []

    def create() -> str:
        name = "fs_role_permissions_" + uuid4().hex[:12]
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


def _apply_migrations(dsn: str) -> dict:
    conn = psycopg_runtime.connect(dsn)
    try:
        return migration_runner.apply_pending_migrations(conn, root=ROOT)
    finally:
        conn.close()


def _catalog(dsn: str) -> dict[str, list[tuple]]:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type, column_default, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = ANY(%s)
             ORDER BY table_name, column_name
            """,
            (list(TABLES),),
        )
        columns = cur.fetchall()
        cur.execute(
            """
            SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid, true)
              FROM pg_catalog.pg_constraint AS c
              JOIN pg_catalog.pg_class AS t ON t.oid = c.conrelid
             WHERE t.relname = ANY(%s)
             ORDER BY 1, 2
            """,
            (list(TABLES),),
        )
        constraints = cur.fetchall()
        cur.execute(
            """
            SELECT tablename, indexname, indexdef FROM pg_catalog.pg_indexes
             WHERE schemaname = 'public' AND tablename = ANY(%s)
             ORDER BY 1, 2
            """,
            (list(TABLES),),
        )
        indexes = cur.fetchall()
        cur.execute(
            """
            SELECT tgname, pg_get_triggerdef(oid) FROM pg_catalog.pg_trigger
             WHERE tgrelid = 'public.user_roles'::regclass AND NOT tgisinternal
             ORDER BY 1
            """
        )
        triggers = cur.fetchall()
    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
    }


def _system_roles(dsn: str) -> dict[str, tuple[bool, str, set[str]]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT r.key, r.is_system, r.description,
                   COALESCE(array_agg(rp.permission_key) FILTER (WHERE rp.permission_key IS NOT NULL),
                            ARRAY[]::text[])
              FROM public.roles AS r
              LEFT JOIN public.role_permissions AS rp ON rp.role_id = r.id
             GROUP BY r.key, r.is_system, r.description
            """
        ).fetchall()
    return {str(key): (bool(system), str(text), set(keys)) for key, system, text, keys in rows}


def _expected_system_roles() -> dict[str, tuple[bool, str, set[str]]]:
    return {
        role: (True, description, set(SYSTEM_ROLE_PERMISSIONS.get(role, ())))
        for role, description in SYSTEM_ROLE_DESCRIPTIONS.items()
    }


def _user_roles(dsn: str) -> dict[str, list[str]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT u.username, array_agg(r.key ORDER BY r.key)
              FROM public.users AS u
              JOIN public.user_roles AS ur ON ur.user_id = u.id
              JOIN public.roles AS r ON r.id = ur.role_id
             GROUP BY u.username
            """
        ).fetchall()
    return {str(name): list(roles) for name, roles in rows}


def _shape_as_0920(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in _AS_RELEASED_0920:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO public.roles (key, label) VALUES "
            "('admin', 'Admin'), ('operator', 'Operator'), ('viewer', 'Viewer'), "
            "('auditor', 'Auditor')"
        )
        for username, roles in (
            ("everything@example.test", ("viewer", "operator", "admin")),
            ("works@example.test", ("viewer", "operator")),
            ("reads@example.test", ("viewer",)),
            ("audits@example.test", ("viewer", "auditor")),
        ):
            conn.execute(
                "INSERT INTO public.users (username, password_hash) VALUES (%s, 'x')", (username,)
            )
            conn.execute(
                """
                INSERT INTO public.user_roles (user_id, role_id)
                SELECT u.id, r.id FROM public.users AS u, public.roles AS r
                 WHERE u.username = %s AND r.key = ANY(%s)
                """,
                (username, list(roles)),
            )


def test_an_upgraded_0920_database_matches_a_fresh_install_and_keeps_the_highest_role(
    create_database,
) -> None:
    fresh = create_database()
    applied_fresh = _apply_migrations(fresh)
    assert MIGRATION.name in applied_fresh["applied"]

    upgraded = create_database()
    _shape_as_0920(upgraded)
    assert MIGRATION.name in _apply_migrations(upgraded)["applied"]

    assert _catalog(upgraded) == _catalog(fresh)
    assert _system_roles(fresh) == _expected_system_roles()
    roles = _system_roles(upgraded)
    assert {key: roles[key] for key in SYSTEM_ROLE_DESCRIPTIONS} == _expected_system_roles()
    assert roles["auditor"] == (False, "", set())
    # The highest system role stays; a role outside the ladder is untouched.
    assert _user_roles(upgraded) == {
        "everything@example.test": ["admin"],
        "works@example.test": ["operator"],
        "reads@example.test": ["viewer"],
        "audits@example.test": ["auditor", "viewer"],
    }

    # Running the same SQL again changes nothing.
    before = (_catalog(upgraded), _system_roles(upgraded), _user_roles(upgraded))
    with psycopg.connect(upgraded) as conn:
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
    assert (_catalog(upgraded), _system_roles(upgraded), _user_roles(upgraded)) == before


def test_the_schema_holds_one_system_role_per_user_and_scope(create_database) -> None:
    dsn = create_database()
    _apply_migrations(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO public.roles (key, label) VALUES ('auditor', 'Auditor')")
        user_id = conn.execute(
            "INSERT INTO public.users (username, password_hash) VALUES ('one@example.test', 'x') "
            "RETURNING id"
        ).fetchone()[0]

        def assign(role: str) -> None:
            conn.execute(
                "INSERT INTO public.user_roles (user_id, role_id) "
                "SELECT %s, id FROM public.roles WHERE key = %s",
                (user_id, role),
            )

        assign("operator")
        assign("auditor")  # a role outside the ladder is allowed beside it
        with pytest.raises(psycopg.errors.UniqueViolation):
            assign("admin")
        flags = dict(
            conn.execute(
                "SELECT r.key, ur.role_is_system FROM public.user_roles AS ur "
                "JOIN public.roles AS r ON r.id = ur.role_id WHERE ur.user_id = %s",
                (user_id,),
            ).fetchall()
        )
        assert flags == {"operator": True, "auditor": False}
        # A workflow scope needs its project.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO public.user_roles (user_id, role_id, workflow_id) "
                "SELECT %s, id, 'wf' FROM public.roles WHERE key = 'viewer'",
                (user_id,),
            )
