from __future__ import annotations

import uuid

import pytest

from core.infrastructure.db import psycopg_runtime
from core.infrastructure.extension_runtime.extension_operation_lock import extension_operation_lock
from tests._db_env import resolve_test_db_dsn


def _lock(connection, key: str, *, action_id: str = "append_rows", connection_id: str = "one"):
    return extension_operation_lock(
        connection=connection,
        extension={
            "extension_key": "fixture.concurrent",
            "action_definitions": [
                {
                    "action_id": "append_rows",
                    "contract": {
                        "serialization": {
                            "resource": "shared_rows",
                            "key_fields": ["resource_id"],
                        }
                    },
                },
                {
                    "action_id": "background_append",
                    "contract": {
                        "serialization": {
                            "resource": "shared_rows",
                            "key_fields": ["resource_id"],
                        }
                    },
                },
            ],
        },
        action_id=action_id,
        input_payload={"resource_id": key},
        runtime_context={
            "extension_install_id": "install-a",
            "connection_id": connection_id,
        },
    )


@pytest.mark.integration
def test_extension_operation_lock_is_shared_across_independent_postgres_sessions(pg_conn):
    key = f"resource-{uuid.uuid4().hex}"
    second = psycopg_runtime.connect(resolve_test_db_dsn(), autocommit=True)
    try:
        with _lock(pg_conn, key) as first_acquired:
            assert first_acquired is True
            with _lock(second, key, action_id="background_append") as second_acquired:
                assert second_acquired is False
            with _lock(second, key, connection_id="two") as independent_acquired:
                assert independent_acquired is True
        with _lock(second, key) as acquired_after_release:
            assert acquired_after_release is True
    finally:
        second.close()
