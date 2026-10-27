"""Changing a provider's key or settings invalidates only what was verified.

Regression: saving a new key marked every discovered model ``stale`` with a
generic failure, including models nobody had ever verified.
"""

from __future__ import annotations

import json
import uuid

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.settings import llm_provider_extension_repository as store
from core.infrastructure.db import psycopg_runtime
from tests._db_env import resolve_test_db_dsn
from tests.support.synthetic_llm_provider import hosted_llm_provider_manifest

_STATES = ("unverified", "failed", "passed", "pending", "stale")


@pytest.fixture
def provider_models():
    dsn = resolve_test_db_dsn()
    if not dsn:
        pytest.fail("FS_DB_DSN is required for this PostgreSQL contract")
    account_id = DEFAULT_ACCOUNT_ID
    extension_id = f"acme.llm-{uuid.uuid4().hex[:12]}"
    install_id = f"pi_{uuid.uuid4().hex}"
    release_id = f"release-{uuid.uuid4().hex}"
    descriptor = hosted_llm_provider_manifest(extension_id=extension_id)
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, descriptor_jsonb,
                    source_kind, account_id
                ) VALUES (%s, %s, '1.0.0', %s::jsonb, 'integration_test', %s)
                """,
                (release_id, extension_id, json.dumps(descriptor), account_id),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, version, account_id, state, install_id, release_id,
                    runtime_mode, descriptor_source
                ) VALUES (%s, '1.0.0', %s, 'active', %s, %s, 'subprocess', 'integration_test')
                """,
                (extension_id, account_id, install_id, release_id),
            )
        source_id = store.upsert_source(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            extension_id=extension_id,
            provider_key=f"llmext_{uuid.uuid4().hex}",
            display_label="Acme LLM",
            supplier_key="acme_llm",
            supplier_label="Acme LLM",
            upstream_owner_key=None,
            upstream_owner_label=None,
            capabilities={},
        )
        store.upsert_discovered_models(
            conn,
            account_id=account_id,
            provider_source_id=source_id,
            models=[{"model_id": f"acme/{state}"} for state in _STATES],
        )
        with conn.cursor() as cur:
            for state in _STATES:
                cur.execute(
                    """
                    UPDATE llm_provider_models
                       SET verification_status = %s,
                           enabled = %s,
                           verification_error_safe = %s,
                           verified_at = CASE WHEN %s = 'passed' THEN now() END
                     WHERE provider_source_id = %s AND model_id = %s
                    """,
                    (
                        state,
                        state == "passed",
                        "capability_mismatch" if state == "failed" else None,
                        state,
                        source_id,
                        f"acme/{state}",
                    ),
                )
        conn.commit()
        yield conn, account_id, install_id, source_id
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM llm_provider_sources WHERE extension_install_id = %s", (install_id,)
            )
            cur.execute("DELETE FROM extension_installs WHERE install_id = %s", (install_id,))
            cur.execute("DELETE FROM extension_releases WHERE release_id = %s", (release_id,))
        conn.commit()
        conn.close()


def _states(conn, source_id: str) -> dict[str, tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model_id, verification_status, verification_error_safe, enabled
              FROM llm_provider_models
             WHERE provider_source_id = %s
            """,
            (source_id,),
        )
        return {row[0].split("/", 1)[1]: tuple(row[1:]) for row in cur.fetchall()}


def test_a_new_key_invalidates_only_verified_models(provider_models) -> None:
    conn, account_id, install_id, source_id = provider_models

    changed = store.mark_install_models_stale(
        conn,
        account_id=account_id,
        extension_install_id=install_id,
        reason="credentials_changed",
    )
    conn.commit()

    assert changed == 3
    assert _states(conn, source_id) == {
        # Never checked: nothing to invalidate, and nothing "failed".
        "unverified": ("unverified", None, False),
        # A real failed result stays the model's own result.
        "failed": ("failed", "capability_mismatch", False),
        "passed": ("stale", "credentials_changed", False),
        "pending": ("stale", "credentials_changed", False),
        "stale": ("stale", "credentials_changed", False),
    }


def test_an_unknown_reason_is_refused(provider_models) -> None:
    conn, account_id, install_id, _ = provider_models

    with pytest.raises(ValueError, match="unsupported stale reason"):
        store.mark_install_models_stale(
            conn, account_id=account_id, extension_install_id=install_id, reason="failed"
        )
