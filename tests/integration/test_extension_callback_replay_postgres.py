from __future__ import annotations

import os
from uuid import uuid4

import pytest

from core.application.auth.config import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_TENANT_ID,
    DEFAULT_WORKSPACE_ID,
)
from core.infrastructure.db.platform_stores import PlatformIdentityStore

pytestmark = pytest.mark.integration


def test_callback_delivery_claim_is_atomic_and_registration_scoped(
    pg_conn, default_account
) -> None:
    del default_account
    with pg_conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        if str(cur.fetchone()[0]) != "flow_steward_audit":
            pytest.skip("callback replay regression requires the isolated audit database")

    project_id = str(uuid4())
    registration_id = str(uuid4())
    store = PlatformIdentityStore(os.environ["FS_DB_DSN"])
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (id, tenant_id, workspace_id, account_id, name)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_WORKSPACE_ID,
                    DEFAULT_ACCOUNT_ID,
                    f"callback-replay-{project_id}",
                ),
            )
        pg_conn.commit()
        created = store.create_extension_callback_registration(
            registration_id=registration_id,
            extension_id="fixture.callback-provider",
            callback_id="updates",
            callback_token_hash=uuid4().hex,
            project_id=project_id,
            auth_type="hmac_sha256",
            auth_config={"secret_ref": "fixture"},  # pragma: allowlist secret - reference name only
        )

        assert created is not None
        assert store.claim_extension_callback_delivery(
            registration_id=registration_id,
            delivery_id="delivery-1",
            delivered_at=1_786_623_600,
        )
        assert not store.claim_extension_callback_delivery(
            registration_id=registration_id,
            delivery_id="delivery-1",
            delivered_at=1_786_623_600,
        )
        assert store.claim_extension_callback_delivery(
            registration_id=registration_id,
            delivery_id="delivery-2",
            delivered_at=1_786_623_600,
        )
    finally:
        store.conn.close()
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        pg_conn.commit()
