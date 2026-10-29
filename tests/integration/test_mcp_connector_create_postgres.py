"""Real PostgreSQL proof for the MCP connector create persistence path."""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from core.application.auth import (
    mcp_connector_oauth,
    mcp_connector_oauth_policy,
    mcp_connector_oauth_repository,
    oauth_client,
)
from core.application.auth.secrets import encrypt_secret
from core.application.management.command_models import (
    McpConnectorCreateInput,
    McpConnectorUpdateInput,
)
from core.application.management.errors import ServiceUnavailableError
from core.application.management.mcp_connector_header_secrets import (
    header_secret_ref_prefix,
    resolve_connector_headers,
)
from core.application.management.mcp_connector_services import (
    McpConnectorCommandService,
    McpConnectorStore,
)
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.infrastructure.db import mcp_connector_oauth_store


@pytest.mark.parametrize("action", ["replace", "settings_reset", "disconnect"])
def test_remote_oauth_revoke_runs_only_after_durable_local_commit(
    disposable_db,
    monkeypatch,
    action,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "mcp-revoke-order-key")
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    connector_id = f"mcp_revoke_{suffix}"
    conn = psycopg.connect(disposable_db.dsn, autocommit=True)
    observer = psycopg.connect(disposable_db.dsn, autocommit=True)
    scope = ScopeContext(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_id="mcp-revoke-order",
        principal_type="operator",
    )
    try:
        conn.execute(
            "INSERT INTO public.accounts (id, tenant_id, account_key, display_name) VALUES (%s,%s,%s,'MCP revoke')",
            (account_id, tenant_id, f"mcp-revoke-{suffix}"),
        )
        conn.execute(
            "INSERT INTO public.workspaces (id, tenant_id, account_id, workspace_key, display_name) VALUES (%s,%s,%s,%s,'MCP revoke')",
            (workspace_id, tenant_id, account_id, f"mcp-revoke-{suffix}"),
        )
        conn.execute(
            "INSERT INTO public.projects (id, tenant_id, account_id, workspace_id, name) VALUES (%s,%s,%s,%s,'MCP revoke')",
            (project_id, tenant_id, account_id, workspace_id),
        )
        conn.execute(
            """INSERT INTO public.mcp_connectors
               (connector_id, account_id, project_id, tenant_id, display_name,
                base_url, auth_mode, auth_status, oauth_metadata_jsonb,
                oauth_connected_identity)
               VALUES (%s,%s,%s,%s,'MCP revoke','https://mcp.example.test/mcp',
                       'oauth2','connected',%s::jsonb,'alice@example.test')""",
            (
                connector_id,
                account_id,
                project_id,
                tenant_id,
                mcp_connector_oauth_policy.json.dumps(
                    {
                        "requested_scopes": ["catalog.read"],
                        "discovery": {
                            "issuer": "https://auth.example.test",
                            "connector_endpoint": "https://mcp.example.test/mcp",
                            "authorization_server": {
                                "revocation_endpoint": "https://auth.example.test/revoke"
                            },
                        },
                        "registration": {
                            "mode": "manual",
                            "issuer": "https://auth.example.test",
                            "token_endpoint_auth_method": "client_secret_post",
                        },
                        "token": {
                            "issuer": "https://auth.example.test",
                            "connector_endpoint": "https://mcp.example.test/mcp",
                        },
                    }
                ),
            ),
        )
        refs = mcp_connector_oauth_repository._save_connector_oauth_secrets(
            conn,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            connector_id=connector_id,
            secrets_to_save={
                mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "old-access",
                mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "old-client",
                mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "old-secret",
            },
        )
        conn.execute(
            "UPDATE public.mcp_connectors SET oauth_secret_refs_jsonb=%s::jsonb WHERE connector_id=%s",
            (mcp_connector_oauth_policy.json.dumps(refs), connector_id),
        )
        logger = SimpleNamespace(conn=conn, cursor=conn.cursor)
        service = McpConnectorCommandService(get_logger=lambda: logger, store=McpConnectorStore())

        def invoke() -> None:
            row = McpConnectorStore().get_connector_row(
                conn,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
            assert row
            if action == "replace":
                mcp_connector_oauth.save_mcp_connector_oauth_credentials(
                    conn,
                    connector_row=row,
                    client_id="new-client",
                    client_secret="new-secret",
                )
            elif action == "settings_reset":
                service.update_connector(
                    connector_id,
                    McpConnectorUpdateInput(
                        oauth_config={
                            "requested_scopes": ["catalog.write"],
                            "authorization_parameters": {},
                            "refresh_token_required": True,
                        }
                    ),
                    scope=scope,
                )
            else:
                mcp_connector_oauth.revoke_mcp_connector_oauth(conn, connector_row=row)

        remote_revoke = MagicMock(return_value={})
        monkeypatch.setattr(mcp_connector_oauth, "_post_form", remote_revoke)
        monkeypatch.setattr(
            mcp_connector_oauth,
            "_resolve_connector_oauth_secrets",
            lambda *_args, **_kwargs: {
                mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "old-access",
                mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "old-client",
                mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "old-secret",
            },
        )

        if action == "settings_reset":
            original_update = service._update_connector_row

            def fail_update(*args, **kwargs):
                original_update(*args, **kwargs)
                raise RuntimeError("injected local rollback")

            failure_patch = patch.object(service, "_update_connector_row", side_effect=fail_update)
        else:
            original_update = McpConnectorStore.update_connector_row

            def fail_update(store_self, *args, **kwargs):
                original_update(store_self, *args, **kwargs)
                raise RuntimeError("injected local rollback")

            failure_patch = patch.object(
                McpConnectorStore,
                "update_connector_row",
                autospec=True,
                side_effect=fail_update,
            )
        with failure_patch, pytest.raises(RuntimeError, match="injected local rollback"):
            invoke()
        remote_revoke.assert_not_called()
        persisted = observer.execute(
            "SELECT auth_status, oauth_connected_identity FROM public.mcp_connectors WHERE connector_id=%s",
            (connector_id,),
        ).fetchone()
        assert persisted == ("connected", "alice@example.test")

        invoke()
        remote_revoke.assert_called_once()
    finally:
        conn.execute("DELETE FROM public.secret_refs WHERE account_id=%s", (account_id,))
        conn.execute("DELETE FROM public.mcp_connectors WHERE account_id=%s", (account_id,))
        conn.execute("DELETE FROM public.projects WHERE id=%s", (project_id,))
        conn.execute("DELETE FROM public.workspaces WHERE id=%s", (workspace_id,))
        conn.execute("DELETE FROM public.accounts WHERE id=%s", (account_id,))
        observer.close()
        conn.close()


def test_create_connector_persists_complete_generic_oauth_row(disposable_db_conn) -> None:
    account_id = "20000000-0000-0000-0000-000000000002"
    workspace_id = "20000000-0000-0000-0000-000000000003"
    project_id = "20000000-0000-0000-0000-000000000004"
    tenant_id = "20000000-0000-0000-0000-000000000001"
    disposable_db_conn.execute(
        "INSERT INTO public.accounts (id, tenant_id, account_key, display_name) "
        "VALUES (%s, %s, 'mcp-create-proof', 'MCP create proof')",
        (account_id, tenant_id),
    )
    disposable_db_conn.execute(
        "INSERT INTO public.workspaces "
        "(id, tenant_id, account_id, workspace_key, display_name) "
        "VALUES (%s, %s, %s, 'mcp-create-proof', 'MCP create proof')",
        (workspace_id, tenant_id, account_id),
    )
    disposable_db_conn.execute(
        "INSERT INTO public.projects (id, tenant_id, account_id, workspace_id, name) "
        "VALUES (%s, %s, %s, %s, 'MCP create proof')",
        (project_id, tenant_id, account_id, workspace_id),
    )
    service = McpConnectorCommandService(
        get_logger=lambda: SimpleNamespace(cursor=disposable_db_conn.cursor)
    )

    created = service.create_connector(
        McpConnectorCreateInput(
            display_name="Generic OAuth MCP",
            description="Persistence proof",
            transport="streamable_http",
            base_url="https://93.184.216.34/mcp",
            auth_mode="oauth2",
            oauth_config={
                "requested_scopes": ["catalog.read"],
                "authorization_parameters": {"prompt": "consent"},
                "refresh_token_required": True,
            },
            namespace_prefix="catalog",
            enabled=True,
        ),
        scope=ScopeContext(
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            actor_id="mcp-create-proof",
            principal_type="operator",
        ),
    )

    row = disposable_db_conn.execute(
        "SELECT account_id::text, project_id::text, tenant_id::text, auth_mode, "
        "header_secret_refs_jsonb, "
        "oauth_metadata_jsonb, oauth_credential_revision, last_sync_status, enabled "
        "FROM public.mcp_connectors WHERE connector_id = %s",
        (created.connector_id,),
    ).fetchone()
    assert row == (
        account_id,
        project_id,
        tenant_id,
        "oauth2",
        {},
        {
            "requested_scopes": ["catalog.read"],
            "authorization_parameters": {"prompt": "consent"},
            "refresh_token_required": True,
        },
        1,
        "never",
        True,
    )


@pytest.mark.parametrize("concurrent_action", ["replace", "trash"])
def test_callback_cannot_restore_tokens_after_concurrent_lifecycle_change(
    disposable_db, monkeypatch, concurrent_action
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "mcp-race-state-key")
    monkeypatch.setenv("ENCRYPTION_KEY", "mcp-race-encryption-key")
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    connector_id = f"mcp_race_{suffix}"
    state_payload = {
        "v": 1,
        "type": "mcp_connector",
        "nonce": "race-proof",
        "connector_id": connector_id,
        "account_id": account_id,
        "project_id": project_id,
        "tenant_id": tenant_id,
        "oauth_credential_revision": 1,
        "issuer": "https://auth.example.test",
        "registration_mode": "manual",
        "connector_endpoint": "https://mcp.example.test/mcp",
        "resource": "https://mcp.example.test/mcp",
        "exp": 2_000_000_000,
    }
    state = oauth_client._build_state_token(state_payload)
    exchange_started = threading.Event()
    release_exchange = threading.Event()
    callback_errors: list[BaseException] = []
    mutation_done = threading.Event()
    mutation_errors: list[BaseException] = []
    tool_id = f"mcp.race.{suffix}"

    setup = psycopg.connect(disposable_db.dsn)
    replacement = psycopg.connect(disposable_db.dsn)
    callback = psycopg.connect(disposable_db.dsn)
    callback.autocommit = True
    registry_store = RegistryDBStore(disposable_db.dsn)
    registry_manager = SimpleNamespace(db_store=registry_store, reload_all=lambda: None)
    manager_patch = patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry_manager,
    )
    manager_patch.start()
    try:
        setup.execute(
            "INSERT INTO public.accounts (id, tenant_id, account_key, display_name) VALUES (%s,%s,%s,'MCP race')",
            (account_id, tenant_id, f"mcp-race-{suffix}"),
        )
        setup.execute(
            "INSERT INTO public.workspaces (id, tenant_id, account_id, workspace_key, display_name) VALUES (%s,%s,%s,%s,'MCP race')",
            (workspace_id, tenant_id, account_id, f"mcp-race-{suffix}"),
        )
        setup.execute(
            "INSERT INTO public.projects (id, tenant_id, account_id, workspace_id, name) VALUES (%s,%s,%s,%s,'MCP race')",
            (project_id, tenant_id, account_id, workspace_id),
        )
        setup.execute(
            """INSERT INTO public.mcp_connectors
               (connector_id, account_id, project_id, tenant_id, display_name, base_url,
                auth_mode, auth_status, oauth_credential_revision, oauth_metadata_jsonb)
               VALUES (%s,%s,%s,%s,'MCP race','https://mcp.example.test/mcp',
                       'oauth2','auth_required',1,%s::jsonb)""",
            (
                connector_id,
                account_id,
                project_id,
                tenant_id,
                '{"discovery":{"issuer":"https://auth.example.test","resource":"https://mcp.example.test/mcp","authorization_server":{"token_endpoint":"https://auth.example.test/token","revocation_endpoint":"https://auth.example.test/revoke"}},"registration":{"mode":"manual","issuer":"https://auth.example.test","token_endpoint_auth_method":"client_secret_basic"}}',
            ),
        )
        refs = mcp_connector_oauth_repository._save_connector_oauth_secrets(
            setup,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            connector_id=connector_id,
            secrets_to_save={
                mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "old-client",
                mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "old-secret",
            },
        )
        setup.execute(
            "UPDATE public.mcp_connectors SET oauth_secret_refs_jsonb=%s::jsonb WHERE connector_id=%s",
            (mcp_connector_oauth_policy.json.dumps(refs), connector_id),
        )
        mcp_connector_oauth_store.insert_oauth_state(
            setup,
            state_hash=oauth_client._state_hash(state),
            connector_id=connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            oauth_credential_revision=1,
            code_verifier_encrypted=encrypt_secret("verifier"),
            redirect_uri="https://flow.example.test/api/integrations/oauth/callback",
            return_path=f"/registry/mcp/{connector_id}?project_id={project_id}",
            auth_payload={
                "client_id": "old-client",
                "scopes": ["catalog.read"],
                "auth_config": {
                    "token_endpoint": "https://auth.example.test/token",
                    "token_endpoint_auth_method": "client_secret_basic",
                    "connector_endpoint": "https://mcp.example.test/mcp",
                    "resource": "https://mcp.example.test/mcp",
                    "issuer": "https://auth.example.test",
                },
            },
            expires_at=2_000_000_000,
        )
        setup.commit()
        registry_store.save_tool(
            {
                "tool_id": tool_id,
                "version": "1.0.0",
                "display_name": "Race projected tool",
                "description": "Projection removed by connector trash",
                "enabled": True,
                "category": "other",
                "prompt": "",
                "interface": {"functions": []},
                "implementation": {
                    "type": "mcp",
                    "connector_id": connector_id,
                    "remote_tool_name": "catalog.read",
                    "owner_account_id": account_id,
                    "owner_project_id": project_id,
                    "owner_tenant_id": tenant_id,
                },
                "config": {
                    "owner_account_id": account_id,
                    "owner_project_id": project_id,
                    "owner_tenant_id": tenant_id,
                },
                "tags": ["mcp", "derived"],
                "account_id": account_id,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "scope_kind": "project",
            },
            author="mcp-race-test",
        )

        def _post_form(*_args, **_kwargs):
            exchange_started.set()
            assert release_exchange.wait(timeout=10)
            return {"access_token": "stale-access", "refresh_token": "stale-refresh"}

        def _run_callback() -> None:
            try:
                mcp_connector_oauth.handle_mcp_oauth_callback(
                    callback, state=state, code="old-code", now=1_900_000_000, post_form=_post_form
                )
            except BaseException as exc:
                callback_errors.append(exc)

        thread = threading.Thread(target=_run_callback)
        thread.start()
        assert exchange_started.wait(timeout=10)

        def _run_mutation() -> None:
            try:
                current = mcp_connector_oauth_repository.McpConnectorStore().get_connector_row(
                    replacement,
                    connector_id,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                )
                if not current:
                    pytest.fail("Concurrent OAuth update lost the connector row")
                if concurrent_action == "replace":
                    mcp_connector_oauth.save_mcp_connector_oauth_credentials(
                        replacement,
                        connector_row=current,
                        client_id="new-client",
                        client_secret="new-secret",
                    )
                else:
                    scope = ScopeContext(
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                        actor_id="mcp-race-test",
                        principal_type="operator",
                    )
                    service = McpConnectorCommandService(
                        get_logger=lambda: SimpleNamespace(
                            conn=replacement,
                            cursor=replacement.cursor,
                        ),
                        store=McpConnectorStore(),
                    )
                    plan = service.preview_delete_connector(connector_id, scope=scope)
                    service.delete_connector(
                        connector_id,
                        plan_revision=plan["revision"],
                        scope=scope,
                    )
                if concurrent_action == "replace":
                    replacement.commit()
            except BaseException as exc:
                mutation_errors.append(exc)
            finally:
                mutation_done.set()

        mutation_thread = threading.Thread(target=_run_mutation)
        mutation_thread.start()
        assert not mutation_done.wait(timeout=0.25), (
            "lifecycle mutation must wait for callback lock"
        )
        release_exchange.set()
        thread.join(timeout=10)
        mutation_thread.join(timeout=10)

        assert not thread.is_alive()
        assert not mutation_thread.is_alive()
        assert callback_errors == []
        assert mutation_errors == []
        setup.rollback()
        persisted = setup.execute(
            "SELECT oauth_credential_revision, auth_status, oauth_secret_refs_jsonb, deleted_at FROM public.mcp_connectors WHERE connector_id=%s",
            (connector_id,),
        ).fetchone()
        assert persisted[0] == 2
        assert persisted[1] == "auth_required"
        assert mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET not in persisted[2].get("refs", {})
        assert (persisted[3] is not None) is (concurrent_action == "trash")
        projected = setup.execute(
            "SELECT deleted_at FROM public.registry_tools WHERE tool_id=%s",
            (tool_id,),
        ).fetchone()
        assert (projected[0] is not None) is (concurrent_action == "trash")
    finally:
        release_exchange.set()
        manager_patch.stop()
        callback.rollback()
        replacement.rollback()
        setup.rollback()
        setup.execute(
            "DELETE FROM public.mcp_connector_oauth_states WHERE account_id=%s", (account_id,)
        )
        setup.execute("DELETE FROM public.secret_refs WHERE account_id=%s", (account_id,))
        setup.execute("DELETE FROM public.registry_tools WHERE tool_id=%s", (tool_id,))
        setup.execute("DELETE FROM public.mcp_connectors WHERE account_id=%s", (account_id,))
        setup.execute("DELETE FROM public.projects WHERE id=%s", (project_id,))
        setup.execute("DELETE FROM public.workspaces WHERE id=%s", (workspace_id,))
        setup.execute("DELETE FROM public.accounts WHERE id=%s", (account_id,))
        setup.commit()
        callback.close()
        replacement.close()
        setup.close()


def test_connector_header_values_live_only_in_encrypted_secret_refs(
    disposable_db_conn,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "mcp-header-secrets-key")
    conn = disposable_db_conn
    tenant_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO public.accounts (id, tenant_id, account_key, display_name) "
        "VALUES (%s, %s, %s, 'MCP header secrets')",
        (account_id, tenant_id, f"mcp-header-secrets-{account_id}"),
    )
    conn.execute(
        "INSERT INTO public.workspaces (id, tenant_id, account_id, workspace_key, display_name) "
        "VALUES (%s, %s, %s, %s, 'MCP header secrets')",
        (workspace_id, tenant_id, account_id, f"mcp-header-secrets-{workspace_id}"),
    )
    conn.execute(
        "INSERT INTO public.projects (id, tenant_id, account_id, workspace_id, name) "
        "VALUES (%s, %s, %s, %s, 'MCP header secrets')",
        (project_id, tenant_id, account_id, workspace_id),
    )
    scope = ScopeContext(
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
        actor_id="mcp-header-secrets",
        principal_type="operator",
    )
    service = McpConnectorCommandService(get_logger=lambda: SimpleNamespace(cursor=conn.cursor))
    store = McpConnectorStore()

    def stored_row(connector_id: str) -> dict:
        row = store.get_connector_row(
            conn,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        assert row
        return row

    def header_secret_rows(connector_id: str) -> list[tuple[str, str]]:
        prefix = header_secret_ref_prefix(connector_id)
        return conn.execute(
            "SELECT ref_key, encrypted_value FROM public.secret_refs "
            "WHERE account_id = %s AND left(ref_key, %s) = %s ORDER BY ref_key",
            (account_id, len(prefix), prefix),
        ).fetchall()

    created = service.create_connector(
        McpConnectorCreateInput(
            display_name="Header MCP",
            transport="streamable_http",
            base_url="https://93.184.216.34/mcp",
            auth_mode="bearer",
            headers={"Authorization": "Bearer first-secret-marker", "X-Tenant": "tenant-marker"},
            enabled=True,
        ),
        scope=scope,
    )
    connector_id = created.connector_id
    assert "first-secret-marker" not in created.model_dump_json()

    row = stored_row(connector_id)
    assert "headers" not in row
    assert set(row["header_secret_refs_jsonb"]["refs"]) == {"Authorization", "X-Tenant"}
    raw_row = conn.execute(
        "SELECT row_to_json(c)::text FROM public.mcp_connectors c WHERE connector_id = %s",
        (connector_id,),
    ).fetchone()[0]
    secrets = header_secret_rows(connector_id)
    assert len(secrets) == 2
    for marker in ("first-secret-marker", "tenant-marker"):
        assert marker not in raw_row
        assert all(marker not in encrypted for _ref, encrypted in secrets)
    assert resolve_connector_headers(conn, row) == {
        "Authorization": "Bearer first-secret-marker",
        "X-Tenant": "tenant-marker",
    }

    service.update_connector(
        connector_id,
        McpConnectorUpdateInput(display_name="Renamed", headers_mode="keep"),
        scope=scope,
    )
    assert stored_row(connector_id)["header_secret_refs_jsonb"] == row["header_secret_refs_jsonb"]

    service.update_connector(
        connector_id,
        McpConnectorUpdateInput(
            headers_mode="replace",
            headers={"Authorization": "Bearer first-secret-marker", "X-Tenant": "tenant-marker"},
        ),
        scope=scope,
    )
    assert stored_row(connector_id)["header_secret_refs_jsonb"] == row["header_secret_refs_jsonb"]

    service.update_connector(
        connector_id,
        McpConnectorUpdateInput(
            headers_mode="replace",
            headers={"Authorization": "Bearer second-secret-marker"},
        ),
        scope=scope,
    )
    replaced = stored_row(connector_id)
    assert replaced["header_secret_refs_jsonb"] != row["header_secret_refs_jsonb"]
    assert [ref for ref, _value in header_secret_rows(connector_id)] == [
        replaced["header_secret_refs_jsonb"]["refs"]["Authorization"]
    ]
    assert resolve_connector_headers(conn, replaced) == {
        "Authorization": "Bearer second-secret-marker"
    }

    conn.execute(
        "DELETE FROM public.secret_refs WHERE ref_key = %s",
        (replaced["header_secret_refs_jsonb"]["refs"]["Authorization"],),
    )
    with pytest.raises(ServiceUnavailableError, match="header secret is missing"):
        resolve_connector_headers(conn, replaced)

    service.update_connector(
        connector_id,
        McpConnectorUpdateInput(headers_mode="clear"),
        scope=scope,
    )
    assert stored_row(connector_id)["header_secret_refs_jsonb"] == {}
    assert header_secret_rows(connector_id) == []

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "UPDATE public.mcp_connectors SET header_secret_refs_jsonb = %s::jsonb "
            "WHERE connector_id = %s",
            ('{"Authorization": "Bearer plaintext-marker"}', connector_id),
        )
