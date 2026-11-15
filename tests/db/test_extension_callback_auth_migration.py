"""Execute the generic callback auth migration on the disposable audit database."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261114190000_raw_body_callback_auth.sql"
CONSTRAINT = "extension_callback_registrations_auth_type_check"


def _constraint_definition(conn) -> str:
    return conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'public.extension_callback_registrations'::regclass AND conname = %s",
        (CONSTRAINT,),
    ).fetchone()[0]


def test_raw_body_callback_migration_is_idempotent_and_preserves_legacy_rows(disposable_db_conn):
    """The upgrade must admit generic auth while retaining legacy rows and checks.

    All DDL and fixture writes roll back, including on an assertion failure. The
    fixture uses lint.sh's explicitly isolated audit DB, never a product database.
    """
    conn = disposable_db_conn
    original_constraint = _constraint_definition(conn)
    registration_id, generic_id, project_id = uuid4(), uuid4(), uuid4()
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    with conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO public.projects (id, name) VALUES (%s, %s)",
            (project_id, "callback migration regression"),
        )
        # Model a released installation before the new auth profile existed.
        conn.execute(
            "ALTER TABLE public.extension_callback_registrations DROP CONSTRAINT " + CONSTRAINT
        )
        conn.execute(
            "ALTER TABLE public.extension_callback_registrations ADD CONSTRAINT "
            + CONSTRAINT
            + " CHECK (auth_type IN ('none', 'shared_secret_header', 'hmac_sha256', 'hub_signature'))"
        )
        conn.execute(
            "INSERT INTO public.extension_callback_registrations "
            "(id, extension_id, callback_id, callback_token_hash, project_id, auth_type, auth_config_jsonb) "
            "VALUES (%s, 'fixture.callback-provider', 'updates', %s, %s, 'hub_signature', '{\"secret_ref\":\"existing-ref\"}'::jsonb)",
            (registration_id, uuid4().hex, project_id),
        )
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(
                "UPDATE public.extension_callback_registrations SET auth_type = 'raw_body_hmac_sha256' WHERE id = %s",
                (registration_id,),
            )
        conn.execute(migration_sql)
        conn.execute(migration_sql)
        assert conn.execute(
            "SELECT auth_type, auth_config_jsonb FROM public.extension_callback_registrations WHERE id = %s",
            (registration_id,),
        ).fetchone() == ("hub_signature", {"secret_ref": "existing-ref"})
        conn.execute(
            "INSERT INTO public.extension_callback_registrations "
            "(id, extension_id, callback_id, callback_token_hash, project_id, auth_type) "
            "VALUES (%s, 'fixture.callback-provider', 'generic', %s, %s, 'raw_body_hmac_sha256')",
            (generic_id, uuid4().hex, project_id),
        )
        conn.execute(
            "UPDATE public.extension_callback_registrations SET auth_type = 'raw_body_hmac_sha256' WHERE id = %s",
            (registration_id,),
        )
        conn.execute(
            "UPDATE public.extension_callback_registrations SET auth_type = 'hub_signature' WHERE id = %s",
            (registration_id,),
        )
        rows = conn.execute(
            "SELECT id, auth_type FROM public.extension_callback_registrations WHERE id IN (%s, %s)",
            (registration_id, generic_id),
        ).fetchall()
        assert set(rows) == {
            (registration_id, "hub_signature"),
            (generic_id, "raw_body_hmac_sha256"),
        }
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(
                "UPDATE public.extension_callback_registrations SET auth_type = 'unsupported' WHERE id = %s",
                (generic_id,),
            )
    assert _constraint_definition(conn) == original_constraint
    assert (
        conn.execute(
            "SELECT count(*) FROM public.extension_callback_registrations WHERE id IN (%s, %s)",
            (registration_id, generic_id),
        ).fetchone()[0]
        == 0
    )
