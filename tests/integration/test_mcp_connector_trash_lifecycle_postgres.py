"""Real PostgreSQL coverage for the MCP connector/tool Trash state machine."""

from __future__ import annotations

import json
from contextlib import suppress
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest

from core.application.auth.config import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_TENANT_ID,
    DEFAULT_WORKSPACE_ID,
)
from core.application.management.command_services_common import (
    ServiceConflictError,
    ServiceNotFoundError,
)
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.mcp_connector_header_secrets import (
    header_secret_ref_prefix,
    save_connector_header_secrets,
)
from core.application.management.mcp_connector_services import (
    McpConnectorCommandService,
    McpConnectorStore,
)
from core.application.management.mcp_projected_tool_lifecycle import (
    McpProjectedToolLifecycleService,
)
from core.application.management.scope import ScopeContext
from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.db_store import RegistryDBStore

pytestmark = pytest.mark.integration

PROJECT_ID = "20000000-0000-0000-0000-000000000010"
OTHER_PROJECT_ID = "20000000-0000-0000-0000-000000000011"
OTHER_TENANT_ID = "20000000-0000-0000-0000-000000000012"


def _scope() -> ScopeContext:
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        tenant_id=DEFAULT_TENANT_ID,
        project_id=PROJECT_ID,
        allowed_project_ids=frozenset({PROJECT_ID}),
        actor_id="mcp-trash-postgres",
        principal_type="admin",
    )


def _manifest(tool_id: str, connector_id: str) -> dict:
    return {
        "tool_id": tool_id,
        "version": "1.0.0",
        "display_name": "Projected catalog read",
        "description": "Projected MCP tool",
        "enabled": True,
        "category": "other",
        "prompt": "",
        "interface": {"functions": []},
        "implementation": {
            "type": "mcp",
            "connector_id": connector_id,
            "remote_tool_name": "catalog.read",
            "owner_account_id": DEFAULT_ACCOUNT_ID,
            "owner_project_id": PROJECT_ID,
            "owner_tenant_id": DEFAULT_TENANT_ID,
        },
        "config": {
            "owner_account_id": DEFAULT_ACCOUNT_ID,
            "owner_project_id": PROJECT_ID,
            "owner_tenant_id": DEFAULT_TENANT_ID,
        },
        "tags": ["mcp", "derived"],
        "account_id": DEFAULT_ACCOUNT_ID,
        "project_id": PROJECT_ID,
        "tenant_id": DEFAULT_TENANT_ID,
        "scope_kind": "project",
    }


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "mcp-trash-lifecycle-key")


def _header_secret_ref_count(conn, connector_id: str) -> int:
    prefix = header_secret_ref_prefix(connector_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM secret_refs WHERE left(ref_key, %s) = %s",
            (len(prefix), prefix),
        )
        return int(cur.fetchone()[0])


def _insert_connector(conn, connector_id: str, tool_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO projects (id, tenant_id, account_id, workspace_id, name)
                VALUES (%s, %s, %s, %s, 'MCP Trash lifecycle')
                ON CONFLICT (id) DO NOTHING""",
            (PROJECT_ID, DEFAULT_TENANT_ID, DEFAULT_ACCOUNT_ID, DEFAULT_WORKSPACE_ID),
        )
        cur.execute(
            """INSERT INTO mcp_connectors
                (connector_id, account_id, project_id, tenant_id,
                 display_name, base_url, auth_mode, header_secret_refs_jsonb,
                 oauth_secret_refs_jsonb, allowlisted_tools, namespace_prefix,
                 discovered_tools_jsonb, status, last_sync_status)
                VALUES (%s, %s, %s, %s, %s, %s, 'bearer', %s::jsonb, %s::jsonb,
                        %s::jsonb, 'pgtest', %s::jsonb, 'active', 'success')""",
            (
                connector_id,
                DEFAULT_ACCOUNT_ID,
                PROJECT_ID,
                DEFAULT_TENANT_ID,
                "Postgres MCP lifecycle",
                "https://mcp.example.test/mcp",
                json.dumps(
                    save_connector_header_secrets(
                        conn,
                        connector_id=connector_id,
                        account_id=DEFAULT_ACCOUNT_ID,
                        project_id=PROJECT_ID,
                        tenant_id=DEFAULT_TENANT_ID,
                        headers={"Authorization": "Bearer trash-secret-marker"},
                    )
                ),
                '{"access_token":"secret-ref-marker"}',
                f'["{tool_id}"]',
                (
                    '[{"remote_tool_name":"catalog.read",'
                    f'"projected_tool_id":"{tool_id}",'
                    '"display_name":"Catalog Read","input_schema":{},'
                    '"output_schema":{},"annotations":{}}]'
                ),
            ),
        )
        cur.execute(
            """INSERT INTO mcp_connector_oauth_states
                (state_hash, connector_id, account_id, project_id, tenant_id,
                 oauth_credential_revision, code_verifier_encrypted,
                 redirect_uri, auth_payload_jsonb, expires_at)
                VALUES (%s, %s, %s, %s, %s, 1, %s, %s,
                        '{}'::jsonb, now() + interval '5 minutes')""",
            (
                f"state-{uuid4().hex}",
                connector_id,
                DEFAULT_ACCOUNT_ID,
                PROJECT_ID,
                DEFAULT_TENANT_ID,
                "pkce-secret-marker",
                "https://app.example.test/callback",
            ),
        )


def test_known_connector_and_tool_ids_are_hidden_outside_project_and_tenant(
    disposable_db,
) -> None:
    connector_id = f"mcp_scope_{uuid4().hex}"
    tool_id = f"pgtest.scope_{uuid4().hex}"
    conn = psycopg.connect(disposable_db.dsn)
    conn.autocommit = True
    store = RegistryDBStore(disposable_db.dsn)
    logger = SimpleNamespace(conn=conn, cursor=conn.cursor)
    try:
        _insert_connector(conn, connector_id, tool_id)
        store.save_tool(_manifest(tool_id, connector_id), author="mcp-trash-postgres")
        connector_service = McpConnectorCommandService(
            get_logger=lambda: logger,
            store=McpConnectorStore(),
        )
        trash = TrashLifecycleService(get_logger=lambda: logger)
        wrong_project_scope = _scope().model_copy(
            update={
                "project_id": OTHER_PROJECT_ID,
                "allowed_project_ids": frozenset({OTHER_PROJECT_ID}),
            }
        )
        wrong_tenant_scope = _scope().model_copy(update={"tenant_id": OTHER_TENANT_ID})

        for foreign_scope in (wrong_project_scope, wrong_tenant_scope):
            with pytest.raises(ServiceNotFoundError):
                connector_service.preview_delete_connector(
                    connector_id,
                    scope=foreign_scope,
                )
            with pytest.raises(ServiceNotFoundError):
                connector_service.delete_connector(
                    connector_id,
                    plan_revision="known-id-must-not-authorize",
                    scope=foreign_scope,
                )
            for action_type in ("restore", "hard_delete"):
                with pytest.raises(ServiceNotFoundError):
                    trash.plan(
                        "mcp_connector",
                        connector_id,
                        action_type=action_type,
                        scope=foreign_scope,
                    )
            _raises_callable_182_1 = McpProjectedToolLifecycleService(db_store=store).preview_delete
            _raises_input_182_2 = str(foreign_scope.project_id or PROJECT_ID)
            _raises_input_182_3 = str(foreign_scope.tenant_id)
            with pytest.raises(ServiceNotFoundError):
                _raises_callable_182_1(
                    tool_id,
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id=_raises_input_182_2,
                    tenant_id=_raises_input_182_3,
                )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT deleted_at FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone() == (None,)
            cur.execute(
                "SELECT deleted_at FROM registry_tools WHERE tool_id = %s",
                (tool_id,),
            )
            assert cur.fetchone() == (None,)
    finally:
        with suppress(Exception), conn.cursor() as cur:
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
            cur.execute("DELETE FROM mcp_connectors WHERE connector_id = %s", (connector_id,))
        conn.close()


def _selected_ids(tree: dict) -> list[str]:
    result = [str(tree["node_id"])]
    result.extend(str(child["node_id"]) for child in tree.get("children") or [])
    return result


def test_root_and_individual_mcp_trash_lifecycle_are_atomic(disposable_db):
    connector_id = f"mcp_pg_{uuid4().hex}"
    tool_id = f"pgtest.catalog_{uuid4().hex}"
    conn = psycopg.connect(disposable_db.dsn)
    conn.autocommit = True
    store = RegistryDBStore(disposable_db.dsn)
    manager = SimpleNamespace(db_store=store, reload_all=lambda: None)
    logger = SimpleNamespace(
        conn=conn,
        cursor=conn.cursor,
        _column_exists=lambda table, column: column == "account_id",
    )
    manager_patch = patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=manager,
    )
    manager_patch.start()
    try:
        _insert_connector(conn, connector_id, tool_id)
        store.save_tool(_manifest(tool_id, connector_id), author="mcp-trash-postgres")

        connector_service = McpConnectorCommandService(
            get_logger=lambda: logger,
            store=McpConnectorStore(),
        )
        delete_plan = connector_service.preview_delete_connector(connector_id, scope=_scope())
        assert "trash-secret-marker" not in str(delete_plan)
        assert "secret-ref-marker" not in str(delete_plan)
        connector_service.delete_connector(
            connector_id,
            plan_revision=delete_plan["revision"],
            scope=_scope(),
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT enabled, header_secret_refs_jsonb, oauth_secret_refs_jsonb, deleted_at, lifecycle_child_tool_ids_jsonb FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            root = cur.fetchone()
            assert root[0] is False
            assert root[1] == {}
            assert root[2] == {}
            assert root[3] is not None
            assert root[4] == [tool_id]
        assert _header_secret_ref_count(conn, connector_id) == 0
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM mcp_connector_oauth_states WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT enabled, deleted_at FROM registry_tools WHERE tool_id = %s", (tool_id,)
            )
            child = cur.fetchone()
            assert child[0] is False
            assert child[1] is not None

        trash = TrashLifecycleService(get_logger=lambda: logger)
        trash._record_hard_delete_audit = lambda *args, **kwargs: None
        _raises_callable_273_1 = McpProjectedToolLifecycleService(db_store=store).restore_from_trash
        with pytest.raises(ServiceConflictError, match="Restore the MCP connector"):
            _raises_callable_273_1(
                tool_id,
                account_id=DEFAULT_ACCOUNT_ID,
                project_id=PROJECT_ID,
                tenant_id=DEFAULT_TENANT_ID,
            )
        restore_plan = trash.plan(
            "mcp_connector", connector_id, action_type="restore", scope=_scope()
        ).dump()
        assert any("Credentials were cleared" in warning for warning in restore_plan["warnings"])
        trash.execute_plan(
            "mcp_connector",
            connector_id,
            action_type="restore",
            selection={
                "selected_node_ids": _selected_ids(restore_plan["tree"]),
                "plan_version": restore_plan["plan_version"],
            },
            scope=_scope(),
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT enabled, auth_status, deleted_at FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone() == (False, "auth_required", None)
            cur.execute(
                "SELECT enabled, deleted_at FROM registry_tools WHERE tool_id = %s", (tool_id,)
            )
            assert cur.fetchone() == (False, None)

        individual_plan = McpProjectedToolLifecycleService(db_store=store).preview_delete(
            tool_id,
            account_id=DEFAULT_ACCOUNT_ID,
            project_id=PROJECT_ID,
            tenant_id=DEFAULT_TENANT_ID,
        )
        McpProjectedToolLifecycleService(db_store=store).delete_confirmed(
            tool_id,
            account_id=DEFAULT_ACCOUNT_ID,
            project_id=PROJECT_ID,
            tenant_id=DEFAULT_TENANT_ID,
            deleted_by="mcp-trash-postgres",
            revision=individual_plan["revision"],
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT allowlisted_tools FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert tool_id not in cur.fetchone()[0]
            cur.execute("SELECT deleted_at FROM registry_tools WHERE tool_id = %s", (tool_id,))
            assert cur.fetchone()[0] is not None

            cur.execute(
                "UPDATE mcp_connectors SET last_sync_status = 'success' WHERE connector_id = %s",
                (connector_id,),
            )
        trash.restore("registry_tool", tool_id, scope=_scope())
        with conn.cursor() as cur:
            cur.execute(
                "SELECT allowlisted_tools FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert tool_id in cur.fetchone()[0]
            cur.execute(
                "SELECT enabled, deleted_at FROM registry_tools WHERE tool_id = %s", (tool_id,)
            )
            assert cur.fetchone() == (False, None)

        restored_delete_plan = McpProjectedToolLifecycleService(db_store=store).preview_delete(
            tool_id,
            account_id=DEFAULT_ACCOUNT_ID,
            project_id=PROJECT_ID,
            tenant_id=DEFAULT_TENANT_ID,
        )
        McpProjectedToolLifecycleService(db_store=store).delete_confirmed(
            tool_id,
            account_id=DEFAULT_ACCOUNT_ID,
            project_id=PROJECT_ID,
            tenant_id=DEFAULT_TENANT_ID,
            deleted_by="mcp-trash-postgres",
            revision=restored_delete_plan["revision"],
        )

        second_delete_plan = connector_service.preview_delete_connector(
            connector_id,
            scope=_scope(),
        )
        connector_service.delete_connector(
            connector_id,
            plan_revision=second_delete_plan["revision"],
            scope=_scope(),
        )
        purge_plan = trash.plan(
            "mcp_connector", connector_id, action_type="hard_delete", scope=_scope()
        ).dump()
        assert any("permanently removes" in warning for warning in purge_plan["warnings"])
        assert not any("Credentials were cleared" in warning for warning in purge_plan["warnings"])
        trash.execute_plan(
            "mcp_connector",
            connector_id,
            action_type="hard_delete",
            selection={
                "selected_node_ids": _selected_ids(purge_plan["tree"]),
                "plan_version": purge_plan["plan_version"],
            },
            scope=_scope(),
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM mcp_connectors WHERE connector_id = %s", (connector_id,)
            )
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM registry_tools WHERE tool_id = %s", (tool_id,))
            assert cur.fetchone()[0] == 0
    finally:
        manager_patch.stop()
        with suppress(Exception), conn.cursor() as cur:
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
            cur.execute("DELETE FROM mcp_connectors WHERE connector_id = %s", (connector_id,))
        conn.close()


def test_no_auth_connector_restore_uses_non_error_auth_state_and_conditional_copy(
    disposable_db,
):
    connector_id = f"mcp_none_{uuid4().hex}"
    tool_id = f"pgtest.none_{uuid4().hex}"
    conn = psycopg.connect(disposable_db.dsn)
    conn.autocommit = True
    store = RegistryDBStore(disposable_db.dsn)
    manager = SimpleNamespace(db_store=store, reload_all=lambda: None)
    logger = SimpleNamespace(conn=conn, cursor=conn.cursor)
    manager_patch = patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=manager,
    )
    manager_patch.start()
    try:
        _insert_connector(conn, connector_id, tool_id)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE mcp_connectors
                      SET auth_mode = 'none', auth_status = 'not_configured',
                          header_secret_refs_jsonb = '{}'::jsonb, oauth_secret_refs_jsonb = '{}'::jsonb
                    WHERE connector_id = %s""",
                (connector_id,),
            )
        store.save_tool(_manifest(tool_id, connector_id), author="mcp-trash-postgres")
        connector_service = McpConnectorCommandService(
            get_logger=lambda: logger,
            store=McpConnectorStore(),
        )
        delete_plan = connector_service.preview_delete_connector(connector_id, scope=_scope())
        connector_service.delete_connector(
            connector_id,
            plan_revision=delete_plan["revision"],
            scope=_scope(),
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT enabled, auth_status, deleted_at FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            enabled, auth_status, deleted_at = cur.fetchone()
        assert enabled is False
        assert auth_status == "not_configured"
        assert deleted_at is not None

        trash = TrashLifecycleService(get_logger=lambda: logger)
        trash._record_hard_delete_audit = lambda *args, **kwargs: None
        restore_plan = trash.plan(
            "mcp_connector", connector_id, action_type="restore", scope=_scope()
        ).dump()
        assert any("Health and Sync" in warning for warning in restore_plan["warnings"])
        assert not any(
            "Credentials" in warning or "Reconnect" in warning
            for warning in restore_plan["warnings"]
        )
        trash.execute_plan(
            "mcp_connector",
            connector_id,
            action_type="restore",
            selection={
                "selected_node_ids": _selected_ids(restore_plan["tree"]),
                "plan_version": restore_plan["plan_version"],
            },
            scope=_scope(),
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT enabled, auth_status, deleted_at FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone() == (False, "not_configured", None)
    finally:
        manager_patch.stop()
        with suppress(Exception), conn.cursor() as cur:
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
            cur.execute("DELETE FROM mcp_connectors WHERE connector_id = %s", (connector_id,))
        conn.close()


def test_oauth_revoke_happens_only_after_successful_connector_delete(disposable_db):
    connector_id = f"mcp_oauth_{uuid4().hex}"
    tool_id = f"pgtest.oauth_{uuid4().hex}"
    conn = psycopg.connect(disposable_db.dsn)
    conn.autocommit = True
    store = RegistryDBStore(disposable_db.dsn)
    manager = SimpleNamespace(db_store=store, reload_all=lambda: None)
    logger = SimpleNamespace(conn=conn, cursor=conn.cursor)
    manager_patch = patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=manager,
    )
    manager_patch.start()
    try:
        _insert_connector(conn, connector_id, tool_id)
        access_ref = f"mcp_connector:{connector_id}:_oauth_access_token"
        client_ref = f"mcp_connector:{connector_id}:_oauth_client_id"
        foreign_ref = f"mcp_connector:foreign-{connector_id}:_oauth_access_token"
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO projects (id, tenant_id, account_id, workspace_id, name)
                    VALUES (%s, %s, %s, %s, 'Foreign MCP lifecycle scope')
                    ON CONFLICT (id) DO NOTHING""",
                (
                    OTHER_PROJECT_ID,
                    DEFAULT_TENANT_ID,
                    DEFAULT_ACCOUNT_ID,
                    DEFAULT_WORKSPACE_ID,
                ),
            )
            cur.execute(
                """UPDATE mcp_connectors
                      SET auth_mode = 'oauth2', auth_status = 'connected',
                          oauth_metadata_jsonb = %s::jsonb,
                          oauth_secret_refs_jsonb = %s::jsonb
                    WHERE connector_id = %s""",
                (
                    """{"requested_scopes":["files:read"],
                         "authorization_parameters":{"prompt":"consent"},
                         "refresh_token_required":true,
                         "discovery":{"issuer":"https://auth.example.test",
                           "connector_endpoint":"https://mcp.example.test/mcp",
                           "authorization_server":{"revocation_endpoint":"https://auth.example.test/revoke"}},
                         "registration":{"mode":"manual","issuer":"https://auth.example.test"},
                         "token":{"issuer":"https://auth.example.test",
                           "connector_endpoint":"https://mcp.example.test/mcp"}}""",
                    f'{{"refs":{{"_oauth_access_token":"{access_ref}","_oauth_client_id":"{client_ref}"}}}}',
                    connector_id,
                ),
            )
            cur.execute(
                """INSERT INTO secret_refs
                       (account_id, project_id, tenant_id, ref_key, encrypted_value)
                    VALUES (%s, %s, %s, %s, 'encrypted-access'),
                           (%s, %s, %s, %s, 'encrypted-client'),
                           (%s, %s, %s, %s, 'encrypted-foreign')""",
                (
                    DEFAULT_ACCOUNT_ID,
                    PROJECT_ID,
                    DEFAULT_TENANT_ID,
                    access_ref,
                    DEFAULT_ACCOUNT_ID,
                    PROJECT_ID,
                    DEFAULT_TENANT_ID,
                    client_ref,
                    DEFAULT_ACCOUNT_ID,
                    OTHER_PROJECT_ID,
                    DEFAULT_TENANT_ID,
                    foreign_ref,
                ),
            )
        store.save_tool(_manifest(tool_id, connector_id), author="mcp-trash-postgres")
        service = McpConnectorCommandService(get_logger=lambda: logger, store=McpConnectorStore())
        scope = _scope()
        plan = service.preview_delete_connector(connector_id, scope=scope)
        original_build = service._build_delete_plan
        build_calls = 0

        def _stale_on_locked_recheck(*args, **kwargs):
            nonlocal build_calls
            build_calls += 1
            payload = original_build(*args, **kwargs)
            if build_calls == 2:
                payload = dict(payload)
                payload["affected"] = {"workflows": [{"workflow_id": "wf-raced"}]}
            return payload

        revoke = MagicMock()
        with (
            patch(
                "core.application.auth.mcp_connector_oauth.revoke_mcp_connector_oauth",
                revoke,
            ),
            patch.object(service, "_build_delete_plan", side_effect=_stale_on_locked_recheck),
            pytest.raises(ServiceConflictError, match="impact changed"),
        ):
            service.delete_connector(connector_id, plan_revision=plan["revision"], scope=scope)

        revoke.assert_not_called()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT auth_status, oauth_secret_refs_jsonb, deleted_at FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone() == (
                "connected",
                {"refs": {"_oauth_access_token": access_ref, "_oauth_client_id": client_ref}},
                None,
            )

        service._build_delete_plan = original_build
        plan = service.preview_delete_connector(connector_id, scope=scope)
        revoke.reset_mock()
        with (
            patch(
                "core.application.auth.mcp_connector_oauth.revoke_mcp_connector_oauth",
                revoke,
            ),
            patch.object(
                service,
                "_trash_projected_registry_tools",
                side_effect=RuntimeError("forced lifecycle rollback"),
            ),
            pytest.raises(RuntimeError, match="forced lifecycle rollback"),
        ):
            service.delete_connector(connector_id, plan_revision=plan["revision"], scope=scope)
        revoke.assert_not_called()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT auth_status, oauth_secret_refs_jsonb, deleted_at FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone() == (
                "connected",
                {"refs": {"_oauth_access_token": access_ref, "_oauth_client_id": client_ref}},
                None,
            )

        plan = service.preview_delete_connector(connector_id, scope=scope)
        with (
            patch(
                "core.application.auth.mcp_connector_oauth._resolve_connector_oauth_secrets",
                return_value={
                    "_oauth_access_token": "provider-token-marker",
                    "_oauth_client_id": "provider-client-marker",
                },
            ),
            patch(
                "core.application.auth.mcp_connector_oauth._post_form",
                side_effect=RuntimeError("provider unavailable after commit"),
            ) as remote_revoke,
        ):
            assert service.delete_connector(
                connector_id, plan_revision=plan["revision"], scope=scope
            ) == {"status": "trashed", "connector_id": connector_id}
        remote_revoke.assert_called_once()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT oauth_metadata_jsonb, oauth_secret_refs_jsonb
                     FROM mcp_connectors WHERE connector_id = %s""",
                (connector_id,),
            )
            retained_metadata, retained_refs = cur.fetchone()
            assert retained_refs == {}
            assert retained_metadata == {
                "requested_scopes": ["files:read"],
                "authorization_parameters": {"prompt": "consent"},
                "refresh_token_required": True,
            }
            cur.execute(
                "SELECT count(*) FROM secret_refs WHERE ref_key = ANY(%s)",
                ([access_ref, client_ref],),
            )
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM secret_refs WHERE ref_key = %s", (foreign_ref,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT count(*) FROM mcp_connector_oauth_states WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone()[0] == 0
        trash = TrashLifecycleService(get_logger=lambda: logger)
        restore_plan = trash.plan(
            "mcp_connector", connector_id, action_type="restore", scope=scope
        ).dump()
        assert any("Credentials were cleared" in warning for warning in restore_plan["warnings"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT auth_status FROM mcp_connectors WHERE connector_id = %s",
                (connector_id,),
            )
            assert cur.fetchone()[0] == "auth_required"
    finally:
        manager_patch.stop()
        with suppress(Exception), conn.cursor() as cur:
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
            cur.execute("DELETE FROM mcp_connectors WHERE connector_id = %s", (connector_id,))
            cur.execute(
                "DELETE FROM secret_refs WHERE ref_key = ANY(%s)",
                (
                    [
                        f"mcp_connector:{connector_id}:_oauth_access_token",
                        f"mcp_connector:{connector_id}:_oauth_client_id",
                        f"mcp_connector:foreign-{connector_id}:_oauth_access_token",
                    ],
                ),
            )
        conn.close()


def test_individual_delete_serializes_concurrent_workflow_dependency_write(disposable_db):
    connector_id = f"mcp_race_{uuid4().hex}"
    tool_id = f"pgtest.race_{uuid4().hex}"
    workflow_id = f"wf_race_{uuid4().hex}"
    conn = psycopg.connect(disposable_db.dsn)
    conn.autocommit = True
    writer_conn = psycopg.connect(disposable_db.dsn)
    writer_conn.autocommit = True
    store = RegistryDBStore(disposable_db.dsn)
    paused = Event()
    release = Event()
    writer_done = Event()
    writer_observed_deleted: list[bool] = []
    failures: list[BaseException] = []
    try:
        _insert_connector(conn, connector_id, tool_id)
        store.save_tool(_manifest(tool_id, connector_id), author="mcp-trash-postgres")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registry_workflows (workflow_key, display_name, account_id) VALUES (%s, %s, %s)",
                (workflow_id, "Concurrent dependency", DEFAULT_ACCOUNT_ID),
            )
            cur.execute(
                "INSERT INTO registry_workflow_steps (owner_workflow_id, step_id, step_kind, connector_id) VALUES (%s, 'remote', 'connector', NULL) RETURNING id",
                (workflow_id,),
            )
            step_pk = cur.fetchone()[0]

        lifecycle = McpProjectedToolLifecycleService(db_store=store)
        plan = lifecycle.preview_delete(
            tool_id,
            account_id=DEFAULT_ACCOUNT_ID,
            project_id=PROJECT_ID,
            tenant_id=DEFAULT_TENANT_ID,
        )
        original_mark = store.mark_tool_source_unavailable

        def _pause_after_recheck(*args, **kwargs):
            paused.set()
            assert release.wait(5)
            return original_mark(*args, **kwargs)

        def _delete():
            try:
                with patch.object(store, "mark_tool_source_unavailable", _pause_after_recheck):
                    lifecycle.delete_confirmed(
                        tool_id,
                        account_id=DEFAULT_ACCOUNT_ID,
                        project_id=PROJECT_ID,
                        tenant_id=DEFAULT_TENANT_ID,
                        deleted_by="mcp-trash-postgres",
                        revision=plan["revision"],
                    )
            except BaseException as exc:  # pragma: no cover - assertion reports thread failure
                failures.append(exc)

        def _write_dependency():
            try:
                with writer_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE registry_workflow_steps SET connector_id = %s WHERE id = %s",
                        (f"connector.{tool_id}", step_pk),
                    )
                with writer_conn.cursor() as cur:
                    cur.execute(
                        "SELECT deleted_at IS NOT NULL FROM registry_tools WHERE tool_id = %s",
                        (tool_id,),
                    )
                    writer_observed_deleted.append(bool(cur.fetchone()[0]))
                writer_done.set()
            except BaseException as exc:  # pragma: no cover - assertion reports thread failure
                failures.append(exc)
                writer_done.set()

        delete_thread = Thread(target=_delete, daemon=True)
        delete_thread.start()
        assert paused.wait(5)
        writer_thread = Thread(target=_write_dependency, daemon=True)
        writer_thread.start()
        try:
            assert not writer_done.wait(0.25), "dependency write bypassed lifecycle inventory lock"
        finally:
            release.set()
        delete_thread.join(5)
        writer_thread.join(5)
        assert not failures
        assert writer_observed_deleted == [True]
        validation = validate_workflow_readiness(store, workflow_id)
        assert "workflow.step_connector_tool_unavailable" in {
            str(error.get("code") or "") for error in validation.get("errors") or []
        }
    finally:
        release.set()
        with suppress(Exception), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM registry_workflow_steps WHERE owner_workflow_id = %s", (workflow_id,)
            )
            cur.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
            cur.execute("DELETE FROM mcp_connectors WHERE connector_id = %s", (connector_id,))
        writer_conn.close()
        conn.close()
