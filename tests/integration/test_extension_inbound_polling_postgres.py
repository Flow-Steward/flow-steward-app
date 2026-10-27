from __future__ import annotations

import json
from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.infrastructure.db.platform_stores import PlatformInboundPollingStore
from core.infrastructure.extension_runtime.extension_runtime_entrypoints import (
    _hydrate_declared_runtime_resources,
)
from core.infrastructure.extension_runtime.extension_runtime_helpers import (
    resolve_extension_runtime_record_by_install_id,
)


@pytest.mark.integration
def test_install_bound_polling_runtime_receives_manifest_granted_connection_and_secret(
    disposable_db,
) -> None:
    suffix = uuid4().hex
    project_id = str(uuid4())
    extension_id = f"acme.inbound-provider-{suffix}"
    install_id = f"install-inbound-{suffix}"
    release_id = f"release-inbound-{suffix}"
    connection_id = f"conn-inbound-poll-{suffix}"
    runtime_grants = {
        "resources": {
            "callback_registrations": {"scope": "owning_extension"},
            "notification_deliveries": {"provider_key": extension_id},
            "notification_routes": {"provider_key": extension_id},
            "project_connection": {"scope": "owning_extension"},
            "provider_secrets": {
                "provider_key": extension_id,
                "secret_keys": ["access_token"],
            },
        }
    }
    descriptor = {
        "extension_id": extension_id,
        "version": "1.0.0",
        "kind": "notification_provider",
        # The extension's own manifest declares grants over its own namespace.
        "runtime_grants": runtime_grants,
        "action_definitions": [
            {
                "action_id": "poll_updates",
                "handler": {
                    "mode": "extension",
                    "config": {
                        "runtime_resources": {
                            "project_connection": True,
                            "provider_secrets": ["access_token"],
                        }
                    },
                },
            }
        ],
    }
    store = PlatformInboundPollingStore(disposable_db.dsn)
    try:
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
                (project_id, "Chat polling project", DEFAULT_ACCOUNT_ID),
            )
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, account_id,
                    source_kind, descriptor_jsonb, descriptor_sha256
                ) VALUES (%s, %s, '1.0.0', %s, 'file', %s::jsonb, %s)
                """,
                (
                    release_id,
                    extension_id,
                    DEFAULT_ACCOUNT_ID,
                    json.dumps(descriptor),
                    "a" * 64,
                ),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id, state
                ) VALUES (%s, %s, %s, %s, 'active')
                """,
                (extension_id, DEFAULT_ACCOUNT_ID, install_id, release_id),
            )
            cur.execute(
                """
                INSERT INTO provider_types (key, label)
                VALUES ('notification_provider', 'Notification Provider')
                ON CONFLICT (key) DO NOTHING
                """
            )

        store.save_provider(
            {
                "provider_key": extension_id,
                "provider_type_key": "notification_provider",
                "display_name": "Fixture inbound provider",
            }
        )
        store.upsert_project_connection(
            project_id=project_id,
            extension_id=extension_id,
            connection_id=connection_id,
            account_id=DEFAULT_ACCOUNT_ID,
            connection_type="chat_bot",
            extension_install_id=install_id,
            display_name="Fixture inbound connection",
            connection_config={"inbound_mode": "polling"},
        )
        assert store.save_provider_secret(
            provider_key=extension_id,
            secret_key="access_token",
            secret_value="inbound-test-token",
            project_id=project_id,
        )

        extension, resolved_store = resolve_extension_runtime_record_by_install_id(
            install_id,
            get_logger_only=lambda: store,
            account_id=DEFAULT_ACCOUNT_ID,
        )

        assert resolved_store is store
        assert extension is not None
        assert set(extension["install_metadata"]["runtime_grants"]["resources"]) == {
            "callback_registrations",
            "notification_deliveries",
            "notification_routes",
            "project_connection",
            "provider_secrets",
        }

        runtime_context = _hydrate_declared_runtime_resources(
            extension,
            operation_type="action",
            operation_id="poll_updates",
            context={"project_id": project_id},
            runtime_context={"logger": store},
        )

        assert runtime_context["resources"]["project_connection"]["connection_id"] == connection_id
        assert runtime_context["resources"]["provider_secrets"] == {
            "access_token": "inbound-test-token"
        }
    finally:
        store.conn.close()
