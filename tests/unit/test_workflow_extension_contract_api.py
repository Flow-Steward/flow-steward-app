from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.extensions.workflow_install_contract import workflow_install_connections
from core.application.management.command_models import ProjectConnectionCreateInput
from core.application.management.command_services import ProjectConnectionService
from core.application.management.scope import ScopeContext
from core.entrypoints.api import workflow_extension_contract as contract_api


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(contract_api.router)
    return TestClient(app, raise_server_exceptions=False)


def test_disabled_install_state_is_projected_by_public_contract_route() -> None:
    contract = {
        "account_id": contract_api.DEFAULT_ACCOUNT_ID,
        "install_id": "install-fixture",
        "extension_id": "fixture.extension-oauth",
        "install_state": "disabled",
        "release_id": "release-fixture",
        "descriptor_sha256": "a" * 64,
        "action_definitions": [{"action_id": "search_messages"}],
        "operation_manifest": [{"operation_id": "search_messages"}],
    }
    db = SimpleNamespace(
        get_project=lambda project_id: {
            "project_id": project_id,
            "account_id": contract_api.DEFAULT_ACCOUNT_ID,
        },
        list_project_connections=lambda _project_id, enabled_only=True: [],
    )

    with (
        patch.object(contract_api, "_resolve_extension_contract", return_value=contract),
        patch.object(contract_api, "_get_db", return_value=db),
        patch.object(contract_api, "_get_connection_store", return_value=db),
    ):
        response = _client().get(
            "/api/workflows/meta/extension-install-contract",
            params={"install_id": "install-fixture", "project_id": "project-1"},
        )

    assert response.status_code == 200
    assert response.json()["install_state"] == "disabled"
    assert response.json()["release_id"] == "release-fixture"
    assert response.json()["descriptor_sha256"] == "a" * 64


def test_removed_install_contract_remains_not_found() -> None:
    with patch.object(contract_api, "_resolve_extension_contract", return_value=None):
        response = _client().get(
            "/api/workflows/meta/extension-install-contract",
            params={"install_id": "install-removed"},
        )

    assert response.status_code == 404
    assert "Extension contract not found" in response.text


def test_install_contract_projects_operation_required_oauth_scopes_exactly() -> None:
    contract = {
        "account_id": contract_api.DEFAULT_ACCOUNT_ID,
        "install_id": "install-mail",
        "extension_id": "fixture.mail",
        "install_state": "active",
        "action_definitions": [
            {"action_id": "send_message", "workflow_visible": True},
        ],
        "operation_manifest": [
            {
                "operation_id": "send_message",
                "connection_type_ids": ["mail_account"],
                "required_oauth_scopes": ["mail.send", "profile"],
            },
        ],
    }

    with patch.object(contract_api, "_resolve_extension_contract", return_value=contract):
        response = _client().get(
            "/api/workflows/meta/extension-install-contract",
            params={"install_id": "install-mail"},
        )

    assert response.status_code == 200
    operations = response.json()["operations"]
    assert len(operations) == 1
    assert operations[0]["operation_id"] == "send_message"
    assert operations[0]["required_oauth_scopes"] == ["mail.send", "profile"]


def test_contract_route_uses_project_store_that_owns_connection_listing() -> None:
    from core.infrastructure.db.platform_stores import PlatformProjectStore

    assert callable(getattr(PlatformProjectStore, "list_project_connections", None))
    source = inspect.getsource(contract_api._connection_store_for_dsn)
    assert "create_project_store" in source
    assert "create_connection_store" not in source


def test_oauth_connection_projection_exposes_only_safe_identity_scope_and_health() -> None:
    db = SimpleNamespace(
        list_project_connections=lambda _project_id, enabled_only=True: [
            {
                "connection_id": "mail-invoices",
                "extension_install_id": "install-mail",
                "extension_id": "fixture.mail",
                "connection_type": "mail_account",
                "display_name": "Mailbox",
                "enabled": True,
                "runtime_status": "degraded",
                "health_status": "needs_attention",
                "oauth_requested_scopes": ["profile", "mail.send"],
                "oauth_granted_scopes": ["profile"],
                "connection_config": {
                    "oauth": {
                        "verified_display_name": "Invoice mailbox",
                        "verified_email": "invoices@example.test",
                        "lifecycle_status": "needs_attention",
                        "private_canary": "must-not-leak",
                    }
                },
                "secret_refs": {"refs": {"refresh_token": "secret-canary"}},
            }
        ]
    )

    rows = workflow_install_connections(
        db,
        project_id="project-1",
        install_id="install-mail",
    )

    assert rows == [
        {
            "connection_id": "mail-invoices",
            "extension_install_id": "install-mail",
            "extension_id": "fixture.mail",
            "connection_type": "mail_account",
            "display_name": "Mailbox",
            "enabled": True,
            "runtime_status": "degraded",
            "health_status": "needs_attention",
            "oauth_requested_scopes": ["profile", "mail.send"],
            "oauth_granted_scopes": ["profile"],
            "verified_display_name": "Invoice mailbox",
            "verified_email": "invoices@example.test",
            "oauth_lifecycle_status": "needs_attention",
        }
    ]
    assert "must-not-leak" not in str(rows)
    assert "secret-canary" not in str(rows)


def test_oauth_connection_projection_omits_unknown_lifecycle_and_private_metadata() -> None:
    db = SimpleNamespace(
        list_project_connections=lambda _project_id, enabled_only=True: [
            {
                "connection_id": "mail-invoices",
                "extension_install_id": "install-mail",
                "extension_id": "fixture.mail",
                "connection_type": "mail_account",
                "enabled": True,
                "connection_config": {"oauth": {"lifecycle_status": "private_future_state"}},
            }
        ]
    )

    rows = workflow_install_connections(
        db,
        project_id="project-1",
        install_id="install-mail",
    )

    assert rows == [
        {
            "connection_id": "mail-invoices",
            "extension_install_id": "install-mail",
            "extension_id": "fixture.mail",
            "connection_type": "mail_account",
            "enabled": True,
        }
    ]


def test_created_oauth_connection_is_returned_by_exact_install_contract_route() -> None:
    from core.application.extensions.install import ExtensionInstallStore

    rows: list[dict] = []
    registry_db = SimpleNamespace(
        dsn="postgresql://registry-store-does-not-own-connections",
        get_project=lambda project_id: {
            "id": project_id,
            "account_id": contract_api.DEFAULT_ACCOUNT_ID,
        },
    )
    connection_store = SimpleNamespace(
        get_project=registry_db.get_project,
        # Keep this persistence/route integration independent from the shipped
        # extension files. The core-only gate intentionally excludes executable
        # bundles; manifest contract validation has its own bundle-backed tests.
        get_extension=lambda extension_id: {
            "extension_id": extension_id,
            "schemas": {
                "config": {"type": "object", "additionalProperties": True},
                "secrets": {"type": "object", "additionalProperties": True},
            },
        },
        create_project_connection=lambda payload: rows.append(dict(payload)),
        list_project_connections=lambda project_id, enabled_only=True: [
            row
            for row in rows
            if str(row.get("project_id")) == str(project_id)
            and (not enabled_only or row.get("enabled", True))
        ],
    )
    install_store = ExtensionInstallStore()
    install = install_store.install(
        extension_id="fixture.mail",
        version="1.0.0",
        account_id=contract_api.DEFAULT_ACCOUNT_ID,
    )
    service = ProjectConnectionService(
        get_logger=lambda: connection_store,
        get_extension_install_store=lambda: install_store,
    )
    created = service.create_connection(
        "project-1",
        ProjectConnectionCreateInput(
            extension_id="fixture.mail",
            extension_install_id=install.install_id,
            connection_kind="extension",
            connection_type="mail_account",
            display_name="Mailbox",
            connection_config={},
        ),
        scope=ScopeContext(
            account_id=contract_api.DEFAULT_ACCOUNT_ID,
            actor_id="contract-test-operator",
            principal_type="operator",
        ),
    )
    assert created.extension_install_id == install.install_id
    rows[0]["connection_config"]["oauth"] = {
        "verified_email": "invoices@example.test",
        "lifecycle_status": "pending",
    }

    contract = {
        "account_id": contract_api.DEFAULT_ACCOUNT_ID,
        "install_id": install.install_id,
        "extension_id": "fixture.mail",
        "install_state": "active",
        "action_definitions": [{"action_id": "send_message"}],
        "operation_manifest": [{"operation_id": "send_message"}],
    }
    with (
        patch.object(contract_api, "_resolve_extension_contract", return_value=contract),
        patch.object(contract_api, "_get_db", return_value=registry_db),
        patch.object(contract_api, "_get_connection_store", return_value=connection_store),
    ):
        response = _client().get(
            "/api/workflows/meta/extension-install-contract",
            params={"install_id": install.install_id, "project_id": "project-1"},
        )

    assert response.status_code == 200
    assert response.json()["connections"] == [
        {
            "connection_id": created.connection_id,
            "display_name": "Mailbox",
            "extension_id": "fixture.mail",
            "extension_install_id": install.install_id,
            "connection_type": "mail_account",
            "environment": "",
            "enabled": True,
            "health_status": "",
            "runtime_status": "",
            "oauth_requested_scopes": [],
            "oauth_granted_scopes": [],
            "verified_email": "invoices@example.test",
            "oauth_lifecycle_status": "pending",
        }
    ]
