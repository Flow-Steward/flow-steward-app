from __future__ import annotations

import os

import pytest

from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.db.platform_stores import PlatformProjectStore
from core.infrastructure.db.system_settings_store import SECURITY_POSTURE_LOOP_STATUS_KEY


def test_security_posture_repository_uses_canonical_schema_and_recovers_after_restart() -> None:
    dsn = str(os.environ.get("FS_DB_DSN") or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for canonical PostgreSQL verification")

    first = PlatformProjectStore(dsn)
    try:
        with first.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM system_settings WHERE account_id = %s AND setting_key = %s",
                (DEFAULT_ACCOUNT_ID, SECURITY_POSTURE_LOOP_STATUS_KEY),
            )

        # This repository query is deliberately executed against the clean 1.0
        # schema: projects.deleted_at exists and the obsolete archived_at does not.
        assert isinstance(first.list_active_project_ids(), list)
        first.record_security_posture_loop_attempt()
        first.record_security_posture_loop_failure("x" * 800)

        restarted = PlatformProjectStore(dsn)
        try:
            failed = restarted.get_security_posture_loop_status()
            assert failed["consecutive_failures"] == 1
            assert len(failed["last_failure_reason"]) == 512
            assert failed["last_attempt_at"]
            assert failed["last_success_at"] == ""

            restarted.record_security_posture_loop_success()
        finally:
            restarted.conn.close()

        recovered = PlatformProjectStore(dsn)
        try:
            status = recovered.get_security_posture_loop_status()
            assert status["consecutive_failures"] == 0
            assert status["last_failure_reason"] == ""
            assert status["last_success_at"]
        finally:
            with recovered.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM system_settings WHERE account_id = %s AND setting_key = %s",
                    (DEFAULT_ACCOUNT_ID, SECURITY_POSTURE_LOOP_STATUS_KEY),
                )
            recovered.conn.close()
    finally:
        first.conn.close()
