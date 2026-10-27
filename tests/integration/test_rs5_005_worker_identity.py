from __future__ import annotations

import os
from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.worker_identity_bootstrap import WorkerIdentityBootstrapService
from core.infrastructure.db.platform_stores import PlatformIdentityStore


class _SecretStore:
    def __init__(self) -> None:
        self.value = ""

    def read(self) -> str:
        return self.value

    def replace(self, value: str) -> None:
        self.value = value


def _identity_store() -> PlatformIdentityStore:
    dsn = str(os.environ.get("FS_DB_DSN") or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required")
    return PlatformIdentityStore(dsn)


@pytest.mark.integration
def test_installer_worker_identity_covers_projects_created_after_issuance_and_rotates(
    pg_conn,
    default_account,
) -> None:
    del pg_conn
    store = _identity_store()
    secrets = _SecretStore()
    service = WorkerIdentityBootstrapService(store=store, secret_store=secrets)
    later_project_id = str(uuid4())

    try:
        first = service.ensure(account_id=DEFAULT_ACCOUNT_ID)
        first_token = secrets.value
        first_principal = store.resolve_machine_token(first_token)

        assert first.rotated is True
        assert first_principal is not None
        assert first_principal["scopes"] == ["projects.read"]
        assert first_principal["allowed_project_ids"] == []

        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (later_project_id, "RS5-005 later project", DEFAULT_ACCOUNT_ID),
            )
        unchanged = service.ensure(account_id=DEFAULT_ACCOUNT_ID)
        assert unchanged.rotated is False
        assert secrets.value == first_token
        assert store.resolve_machine_token(first_token)["allowed_project_ids"] == []

        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE machine_tokens SET scopes_jsonb = '[\"jobs.read\"]'::jsonb WHERE id = %s",
                (first_principal["token_id"],),
            )
            cur.execute(
                "UPDATE service_accounts SET scopes_jsonb = '[\"jobs.read\"]'::jsonb "
                "WHERE service_account_key = %s",
                ("flow-steward-worker",),
            )
        rotated = service.ensure(account_id=DEFAULT_ACCOUNT_ID)

        assert rotated.rotated is True
        assert secrets.value != first_token
        assert store.resolve_machine_token(first_token) is None
        replacement = store.resolve_machine_token(secrets.value)
        assert replacement is not None
        assert replacement["allowed_project_ids"] == []
    finally:
        with store.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM machine_tokens WHERE service_account_id IN "
                "(SELECT id FROM service_accounts WHERE service_account_key = %s)",
                ("flow-steward-worker",),
            )
            cur.execute(
                "DELETE FROM service_accounts WHERE service_account_key = %s",
                ("flow-steward-worker",),
            )
            cur.execute("DELETE FROM projects WHERE id = %s", (later_project_id,))
        store.conn.close()


@pytest.mark.integration
def test_project_restricted_machine_token_remains_restricted(pg_conn, default_account) -> None:
    del pg_conn
    store = _identity_store()
    key = f"rs5-005-restricted-{uuid4().hex}"
    project_id = str(uuid4())
    try:
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, "RS5-005 restricted project", DEFAULT_ACCOUNT_ID),
            )
        store.create_service_account(
            service_account_key=key,
            scopes=["projects.read"],
            allowed_project_ids=[project_id],
            account_id=DEFAULT_ACCOUNT_ID,
        )
        issued = store.create_machine_token(
            service_account_key=key,
            scopes=["projects.read"],
            allowed_project_ids=[project_id],
            account_id=DEFAULT_ACCOUNT_ID,
        )

        principal = store.resolve_machine_token(str(issued["token"]))

        assert principal is not None
        assert principal["allowed_project_ids"] == [project_id]
    finally:
        with store.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM machine_tokens WHERE service_account_id IN "
                "(SELECT id FROM service_accounts WHERE service_account_key = %s)",
                (key,),
            )
            cur.execute("DELETE FROM service_accounts WHERE service_account_key = %s", (key,))
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        store.conn.close()
