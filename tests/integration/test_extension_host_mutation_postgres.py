from __future__ import annotations

from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.response_envelope import process_host_mutations
from core.infrastructure.db.platform_stores import PlatformIntegrationStore


@pytest.mark.integration
def test_project_connection_host_mutation_uses_transaction_bound_production_store(
    disposable_db,
) -> None:
    suffix = uuid4().hex
    project_id = str(uuid4())
    extension_id = f"fixture.host-mutation-{suffix}"
    install_id = f"install-host-mutation-{suffix}"
    release_id = f"release-host-mutation-{suffix}"
    connection_id = f"conn-host-mutation-{suffix}"
    store = PlatformIntegrationStore(disposable_db.dsn)
    try:
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, "Host mutation project", DEFAULT_ACCOUNT_ID),
            )
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, account_id,
                    source_kind, descriptor_jsonb, descriptor_sha256
                ) VALUES (%s, %s, '1.0.0', %s, 'file', '{}'::jsonb, %s)
                """,
                (release_id, extension_id, DEFAULT_ACCOUNT_ID, "a" * 64),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id
                ) VALUES (%s, %s, %s, %s)
                """,
                (extension_id, DEFAULT_ACCOUNT_ID, install_id, release_id),
            )

        store.upsert_project_connection(
            project_id=project_id,
            extension_id=extension_id,
            connection_id=connection_id,
            account_id=DEFAULT_ACCOUNT_ID,
            connection_type="chat_bot",
            extension_install_id=install_id,
            display_name="Chat bot",
            connection_config={},
        )
        extension = {
            "extension_id": extension_id,
            "extension_install_id": install_id,
            "connection_types": [
                {
                    "connection_type_id": "chat_bot",
                    "config_schema": {
                        "type": "object",
                        "properties": {"chat_id": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "ingress_schema": {
                        "type": "object",
                        "additionalProperties": False,
                    },
                }
            ],
            "install_metadata": {
                "runtime_grants": {
                    "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
                }
            },
            "action_definitions": [
                {
                    "action_id": "resolve_chat",
                    "handler": {
                        "mode": "extension",
                        "config": {"host_mutations": ["project_connection_upsert"]},
                    },
                }
            ],
        }
        invocation = {
            "status": "succeeded",
            "operation_type": "action",
            "operation_id": "resolve_chat",
            "response": {
                "ok": True,
                "result": {"chat_id": "8524426410"},
                "host_mutations": [
                    {
                        "kind": "project_connection_upsert",
                        "payload": {
                            "connection_id": connection_id,
                            "extension_install_id": install_id,
                            "connection_config": {"chat_id": "8524426410"},
                        },
                    }
                ],
            },
        }

        result = process_host_mutations(
            invocation,
            extension=extension,
            extension_id=extension_id,
            db=store,
            account_id=DEFAULT_ACCOUNT_ID,
            project_id=project_id,
            connection_id=connection_id,
            extension_install_id=install_id,
        )

        assert result == {
            "applied": [{"kind": "project_connection_upsert", "index": 0}],
            "errors": [],
        }
        assert invocation["status"] == "succeeded"
        persisted = store.get_project_connection(
            project_id=project_id,
            connection_id=connection_id,
            extension_id=extension_id,
            extension_install_id=install_id,
            enabled_only=False,
        )
        assert persisted is not None
        assert persisted["connection_config"] == {"chat_id": "8524426410"}
    finally:
        store.conn.close()
