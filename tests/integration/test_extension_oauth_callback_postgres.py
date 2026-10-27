from __future__ import annotations

import copy
import hashlib
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
import yaml
from psycopg.pq import TransactionStatus

from core.application.extensions.binding_context import ExtensionLifecycleContext
from core.application.extensions.oauth_authorization import (
    OAuthAuthorizationService,
    OAuthAuthorizationStart,
)
from core.application.extensions.oauth_callback import OAuthCallbackService
from core.application.extensions.oauth_configuration import (
    OAuthConfigurationSave,
    OAuthConfigurationService,
)
from core.application.extensions.oauth_configuration_secrets import (
    DatabaseOAuthConfigurationSecretStore,
)
from core.application.extensions.oauth_connection_lifecycle import (
    OAuthConnectionLifecycleService,
)
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_lifecycle import (
    invalidate_install_oauth_on_disable,
    record_extension_oauth_uninstall_audit,
)
from core.application.extensions.oauth_repository import ExtensionOAuthRepository
from core.application.extensions.oauth_runtime import (
    OAuthRuntimeTokenError,
    OAuthRuntimeTokenService,
)
from core.application.extensions.oauth_uninstall_cleanup import (
    cleanup_extension_oauth_configurations,
)
from core.application.extensions.oauth_uninstall_lifecycle import PreparedOAuthUninstall
from core.application.extensions.uninstall_cleanup_queries import (
    ExtensionUninstallCleanupQueries,
)
from core.application.extensions.uninstall_inventory_queries import _collect_callbacks
from core.application.extensions.uninstall_models import (
    RemovalTransactionState,
    UninstallContext,
    UninstallTarget,
)
from core.application.extensions.uninstall_persistence_service import (
    ExtensionUninstallPersistenceService,
)
from core.application.management import project_connection_secrets
from core.application.management.errors import ServicePreconditionError
from core.application.management.scope import ScopeContext
from core.infrastructure.db import model_provider_store
from core.infrastructure.extension_runtime.oauth_token_store import (
    DatabaseOAuthCallbackTokenStore,
)
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
    OAuthProviderOperation,
)
from tests import _disposable_db
from tests._db_env import resolve_test_db_dsn
from tests.support.platform_test_store import PlatformTestStore as PlatformLogger

TENANT = UUID("30000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("30000000-0000-0000-0000-000000000002")
WORKSPACE = UUID("30000000-0000-0000-0000-000000000003")
PROJECT = UUID("30000000-0000-0000-0000-000000000004")
EXTENSION = "fixture.extension-oauth"
INSTALL = "install_callback_oauth"
RELEASE = "release_callback_oauth"
PROVIDER = "example"


@contextmanager
def _claim_connection(dsn: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def oauth_callback_database() -> Iterator[Any]:
    configured_dsn = resolve_test_db_dsn()
    is_audit_database = (
        "host=pg-audit" in configured_dsn or "@pg-audit:" in configured_dsn
    ) and "flow_steward_audit" in configured_dsn
    if is_audit_database:
        yield SimpleNamespace(dsn=configured_dsn)
        return
    reason = _disposable_db.unavailable_reason()
    if reason:
        pytest.skip(f"disposable PostgreSQL unavailable: {reason}")
    database = _disposable_db.start_disposable_db()
    try:
        yield database
    finally:
        _disposable_db.stop_disposable_db(database.container)


@pytest.fixture
def pg_conn(oauth_callback_database: Any) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(oauth_callback_database.dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (id, tenant_id, account_key, display_name) VALUES (%s, %s, 'callback-oauth', 'Callback OAuth') ON CONFLICT (id) DO NOTHING",
            (ACCOUNT, TENANT),
        )
        cur.execute(
            "INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name) VALUES (%s, %s, %s, 'callback-oauth', 'Callback OAuth') ON CONFLICT (id) DO NOTHING",
            (WORKSPACE, TENANT, ACCOUNT),
        )
        cur.execute(
            "INSERT INTO projects (id, tenant_id, account_id, workspace_id, name) VALUES (%s, %s, %s, %s, 'Callback OAuth') ON CONFLICT (id) DO NOTHING",
            (PROJECT, TENANT, ACCOUNT, WORKSPACE),
        )
        cur.execute(
            "INSERT INTO extension_releases (release_id, extension_id, release_version, descriptor_sha256) VALUES (%s, %s, '1.0.0', repeat('a', 64)) ON CONFLICT (release_id) DO NOTHING",
            (RELEASE, EXTENSION),
        )
        cur.execute(
            "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id) VALUES (%s, %s, %s, %s) ON CONFLICT (extension_id, account_id) DO NOTHING",
            (EXTENSION, ACCOUNT, INSTALL, RELEASE),
        )
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM project_connections WHERE project_id = %s", (PROJECT,))
            cur.execute(
                "DELETE FROM extension_oauth_authorization_states WHERE project_id = %s",
                (PROJECT,),
            )
            cur.execute(
                "DELETE FROM extension_oauth_configurations WHERE project_id = %s", (PROJECT,)
            )
            cur.execute(
                "DELETE FROM secret_refs WHERE account_id = %s AND ref_key LIKE %s",
                (ACCOUNT, "project_connection:conn_callback_%"),
            )
            cur.execute(
                "UPDATE extension_installs SET state = 'active' WHERE install_id = %s AND account_id = %s",
                (INSTALL, ACCOUNT),
            )
        conn.close()


def _manifest() -> dict[str, Any]:
    source = Path("tests/fixtures/extensions/fixture_extension_oauth/extension.yaml")
    manifest = yaml.safe_load(source.read_text(encoding="utf-8"))
    manifest["action_definitions"] = [
        {"action_id": "verify_identity", "handler": {"mode": "extension"}}
    ]
    manifest["ui_manifest"] = {
        "pages": [
            {
                "page_id": "mail",
                "components": [
                    {
                        "type": "connection_form",
                        "data": {
                            "oauth_provider_id": "example",
                            "connection_type": "example_mail",
                            "oauth_intent": "connect",
                        },
                    }
                ],
            }
        ],
    }
    return manifest


def _seed_flow(conn: Any, *, opaque_state: str, reconnect: str | None = None) -> dict[str, Any]:
    provider = compile_oauth_providers(_manifest())[0]
    configuration_id = uuid4()
    repo = ExtensionOAuthRepository(conn)
    configuration = repo.create_configuration(
        configuration_id=configuration_id,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        project_id=PROJECT,
        extension_install_id=INSTALL,
        extension_id=EXTENSION,
        extension_release_id=RELEASE,
        provider_id=PROVIDER,
        display_name="Callback configuration",
        configuration_revision=1,
        authorization_revision=1,
        status="ready_for_authorization",
        non_secret_config={"client_id": "client-id"},
        secret_refs={
            "refs": {
                "client_secret": f"extension_oauth_configuration:{configuration_id}:client_secret"
            }
        },
        provider_security_fingerprint=provider_security_fingerprint(provider),
        provider_revocation_fingerprint=provider_revocation_fingerprint(provider),
        last_error={},
    )
    assert configuration is not None
    if reconnect:
        created = repo.create_oauth_connection(
            connection_id=reconnect,
            tenant_id=TENANT,
            account_id=ACCOUNT,
            project_id=PROJECT,
            extension_install_id=INSTALL,
            extension_id=EXTENSION,
            display_name="Existing callback mailbox",
            connection_kind="extension",
            connection_type="example_mail",
            oauth_configuration_id=configuration_id,
            oauth_provider_id=PROVIDER,
            oauth_extension_release_id=RELEASE,
            oauth_authorization_revision=1,
            oauth_requested_scopes=["mail.read"],
            oauth_granted_scopes=["mail.read"],
            oauth_provider_security_fingerprint=provider_security_fingerprint(provider),
            oauth_provider_revocation_fingerprint=provider_revocation_fingerprint(provider),
            health_status="healthy",
            runtime_status="ready",
            last_error="",
            mark_last_seen_ok=True,
            secret_refs={},
            connection_config={
                "extension_metadata": {"canary": "preserve-reconnect-metadata"},
                "oauth": {
                    "provider_subject": "preserve-provider-subject",
                    "subject": "old-generic-subject",
                    "verified_display_name": "Old Verified Person",
                    "verified_email": "old-verified@example.test",
                    "safe_metadata": {"old": "verified-identity"},
                    "lifecycle_status": "connected",
                },
            },
        )
        assert created is not None
        access_ref = project_connection_secrets.save_connection_secret(
            conn,
            account_id=str(ACCOUNT),
            connection_id=reconnect,
            secret_name="_oauth_access_token",
            secret_value="old-access-secret",
        )
        refresh_ref = project_connection_secrets.save_connection_secret(
            conn,
            account_id=str(ACCOUNT),
            connection_id=reconnect,
            secret_name="_oauth_refresh_token",
            secret_value="old-refresh-secret",
        )
        expiry_ref = project_connection_secrets.save_connection_secret(
            conn,
            account_id=str(ACCOUNT),
            connection_id=reconnect,
            secret_name="_oauth_expires_at",
            secret_value="9999999999",
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE project_connections SET credential_revision = 7, secret_refs = %s::jsonb WHERE connection_id = %s",
                (
                    '{"refs":{"_oauth_access_token":"'
                    + access_ref
                    + '","_oauth_refresh_token":"'
                    + refresh_ref
                    + '","_oauth_expires_at":"'
                    + expiry_ref
                    + '"}}',
                    reconnect,
                ),
            )
    state = repo.create_state(
        state_hash=hashlib.sha256(opaque_state.encode()).hexdigest(),
        tenant_id=TENANT,
        account_id=ACCOUNT,
        project_id=PROJECT,
        extension_install_id=INSTALL,
        extension_id=EXTENSION,
        extension_release_id=RELEASE,
        callback_id="oauth_authorization",
        provider_id=PROVIDER,
        configuration_id=configuration_id,
        configuration_revision=1,
        authorization_revision=1,
        connection_type="example_mail",
        intent_id="connect",
        existing_connection_id=reconnect,
        display_name="Callback mailbox",
        intent_payload={"connection_credential_revision": 7} if reconnect else {},
        requested_scopes=["mail.read", "mail.write"],
        redirect_uri="https://app.example.test/callback/exact",
        pkce_verifier_encrypted="encrypted-verifier",
        provider_security_fingerprint=provider_security_fingerprint(provider),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert state is not None
    conn.commit()
    return {"configuration_id": configuration_id, "provider": provider}


def _service(
    conn: Any,
    *,
    exchange,
    token_store: Any | None = None,
    connection_id: str,
    post_connect: Any | None = None,
    runtime_resolver: Any | None = None,
    transaction_factory: Any | None = None,
) -> OAuthCallbackService:
    install = {
        "extension_id": EXTENSION,
        "release_id": RELEASE,
        "enabled": True,
        "compiled_manifest": _manifest(),
    }
    return OAuthCallbackService(
        repository=ExtensionOAuthRepository(conn),
        token_store=token_store or DatabaseOAuthCallbackTokenStore(conn, now=lambda: 1000.0),
        install_loader=lambda _install, _account: copy.deepcopy(install),
        configuration_secret_value=lambda **_values: "client-secret",
        decrypt_verifier=lambda _value: "verifier-value",
        exchange_code=exchange,
        transaction_factory=transaction_factory or (lambda: conn.transaction()),
        public_base_url="https://app.example.test",
        invoke_post_connect=post_connect,
        resolve_runtime_access_token=runtime_resolver,
        connection_id_factory=lambda: connection_id,
    )


def _handle(service: OAuthCallbackService, state: str):
    return service.handle_authorization_callback(
        extension_id=EXTENSION,
        callback_id="oauth_authorization",
        registration={"account_id": str(ACCOUNT), "project_id": str(PROJECT)},
        callback_path="/callback/exact",
        opaque_state=state,
        code="one-time-code",
        provider_error=None,
    )


def _operator_scope() -> ScopeContext:
    return ScopeContext(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        project_id=str(PROJECT),
        principal_type="operator",
    )


def _configuration_service(conn: Any) -> OAuthConfigurationService:
    return OAuthConfigurationService(
        repository=ExtensionOAuthRepository(conn),
        secret_store=DatabaseOAuthConfigurationSecretStore(conn),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        confirmation_key=b"postgres-runtime-revalidation",
        transaction_factory=lambda: conn.transaction(),
    )


@pytest.mark.integration
def test_callback_commits_once_and_replay_stops_before_offline_exchange(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = "opaque-postgres-success"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    calls: list[str] = []

    def exchange(**values: Any) -> dict[str, Any]:
        calls.append("exchange")
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        assert values["redirect_uri"] == "https://app.example.test/callback/exact"
        return {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    connection_id = f"conn_callback_{uuid4().hex}"
    service = _service(pg_conn, exchange=exchange, connection_id=connection_id)
    assert _handle(service, opaque_state).outcome == "connected"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    observer = psycopg.connect(oauth_callback_database.dsn, autocommit=True)
    try:
        with observer.cursor() as cur:
            cur.execute(
                "SELECT secret_refs, health_status, runtime_status, last_seen_ok_at, last_error "
                "FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            visible_refs, health_status, runtime_status, last_seen_ok_at, last_error = (
                cur.fetchone()
            )
            assert visible_refs["refs"]["_oauth_access_token"]
            assert health_status == "healthy"
            assert runtime_status == "ready"
            assert last_seen_ok_at is not None
            assert last_error == ""
            cur.execute(
                "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key = %s",
                (ACCOUNT, visible_refs["refs"]["_oauth_access_token"]),
            )
            assert cur.fetchone() is not None
    finally:
        observer.close()
    assert _handle(service, opaque_state).outcome == "restart_required"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    assert calls == ["exchange"]

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT consumed_at IS NOT NULL FROM extension_oauth_authorization_states WHERE state_hash = %s",
            (hashlib.sha256(opaque_state.encode()).hexdigest(),),
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT oauth_configuration_id, oauth_requested_scopes, oauth_granted_scopes, secret_refs FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        row = cur.fetchone()
        assert str(row[0]) == str(seeded["configuration_id"])
        assert list(row[1]) == ["mail.read", "mail.write"]
        assert list(row[2]) == ["mail.read", "mail.write"]
        assert "access-secret" not in repr(row[3])


@pytest.mark.integration
def test_post_connect_runs_after_visible_commit_and_persists_verified_identity(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = "opaque-postgres-post-connect"
    _seed_flow(pg_conn, opaque_state=opaque_state)
    connection_id = f"conn_callback_post_connect_{uuid4().hex}"
    captured: dict[str, Any] = {}

    def post_connect(**values: Any) -> dict[str, Any]:
        assert pg_conn.info.transaction_status is TransactionStatus.INTRANS
        observer = psycopg.connect(oauth_callback_database.dsn, autocommit=True)
        try:
            with observer.cursor() as cur:
                cur.execute(
                    "SELECT connection_config->'oauth'->>'lifecycle_status', health_status, "
                    "runtime_status, last_seen_ok_at, last_error "
                    "FROM project_connections WHERE connection_id = %s",
                    (connection_id,),
                )
                assert cur.fetchone() == (
                    "post_connect_pending",
                    "unknown",
                    "unavailable",
                    None,
                    "",
                )
        finally:
            observer.close()
        captured.update(copy.deepcopy(values))
        return {
            "status": "succeeded",
            "response": {
                "result": {
                    "subject": "postgres-verified-subject",
                    "verified_display_name": "Postgres Verified",
                    "verified_email": "verified@example.test",
                    "safe_metadata": {"account_tier": "standard"},
                    "health": "ok",
                }
            },
        }

    service = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "post-connect-access-canary",
            "refresh_token": "post-connect-refresh-canary",
            "token_type": "Bearer",
            "scope": "mail.read",
        },
        connection_id=connection_id,
        post_connect=post_connect,
    )

    assert _handle(service, opaque_state).outcome == "connected"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    assert captured["target"]["connection"]["secrets"] == {
        "access_token": "post-connect-access-canary"
    }
    assert "post-connect-refresh-canary" not in repr(captured)
    observer = psycopg.connect(oauth_callback_database.dsn, autocommit=True)
    try:
        with observer.cursor() as cur:
            cur.execute(
                "SELECT credential_revision, display_name, connection_config, health_status, "
                "runtime_status, last_seen_ok_at, last_error "
                "FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            (
                revision,
                display_name,
                config,
                health_status,
                runtime_status,
                last_seen_ok_at,
                last_error,
            ) = cur.fetchone()
            assert revision == 2
            assert display_name == "Postgres Verified"
            assert config["oauth"]["subject"] == "postgres-verified-subject"
            assert config["oauth"]["lifecycle_status"] == "connected"
            assert health_status == "healthy"
            assert runtime_status == "ready"
            assert last_seen_ok_at is not None
            assert last_error == ""
            assert "post-connect-access-canary" not in repr(config)
            assert "post-connect-refresh-canary" not in repr(config)
    finally:
        observer.close()


@pytest.mark.integration
def test_tampered_expired_and_cross_scope_state_never_exchanges_or_mutates(
    pg_conn, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = "opaque-postgres-invalid-state"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    exchange_calls: list[str] = []
    connection_id = f"conn_callback_invalid_{uuid4().hex}"

    def exchange(**values: Any) -> dict[str, Any]:
        exchange_calls.append(values["code"])
        return {"access_token": "must-not-be-used", "token_type": "Bearer"}

    service = _service(pg_conn, exchange=exchange, connection_id=connection_id)
    assert _handle(service, f"{opaque_state}-tampered").outcome == "restart_required"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    assert (
        service.handle_authorization_callback(
            extension_id="fixture.peer",
            callback_id="oauth_authorization",
            registration={"account_id": str(ACCOUNT), "project_id": str(PROJECT)},
            callback_path="/callback/exact",
            opaque_state=opaque_state,
            code="one-time-code",
            provider_error=None,
        ).outcome
        == "restart_required"
    )
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    assert (
        service.handle_authorization_callback(
            extension_id=EXTENSION,
            callback_id="oauth_authorization",
            registration={"account_id": str(ACCOUNT), "project_id": str(uuid4())},
            callback_path="/callback/exact",
            opaque_state=opaque_state,
            code="one-time-code",
            provider_error=None,
        ).outcome
        == "restart_required"
    )
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_oauth_authorization_states "
            "SET created_at = now() - interval '2 seconds', "
            "expires_at = now() - interval '1 second' "
            "WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
    pg_conn.commit()

    assert _handle(service, opaque_state).outcome == "restart_required"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    assert exchange_calls == []
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM project_connections WHERE connection_id = %s", (connection_id,))
        assert cur.fetchone() is None
        cur.execute(
            "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key LIKE %s",
            (ACCOUNT, f"project_connection:{connection_id}:%"),
        )
        assert cur.fetchone() is None


@pytest.mark.integration
def test_denial_and_exchange_failure_return_with_no_open_transaction(pg_conn, monkeypatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    denied_state = "opaque-postgres-denied"
    _seed_flow(pg_conn, opaque_state=denied_state)
    denied_service = _service(
        pg_conn,
        exchange=lambda **_values: pytest.fail("denial must not exchange"),
        connection_id=f"conn_callback_denied_{uuid4().hex}",
    )
    denied = denied_service.handle_authorization_callback(
        extension_id=EXTENSION,
        callback_id="oauth_authorization",
        registration={"account_id": str(ACCOUNT), "project_id": str(PROJECT)},
        callback_path="/callback/exact",
        opaque_state=denied_state,
        code=None,
        provider_error="access_denied",
    )
    assert denied.outcome == "authorization_denied"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE

    failed_state = "opaque-postgres-provider-failure"
    _seed_flow(pg_conn, opaque_state=failed_state)

    def failed_exchange(**_values: Any) -> dict[str, Any]:
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        raise RuntimeError("offline provider failure")

    failed_service = _service(
        pg_conn,
        exchange=failed_exchange,
        connection_id=f"conn_callback_provider_fail_{uuid4().hex}",
    )
    assert _handle(failed_service, failed_state).outcome == "provider_unavailable"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE


@pytest.mark.integration
def test_exchange_invalid_client_marks_only_selected_configuration(pg_conn, monkeypatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    selected_state = "opaque-postgres-invalid-client"
    sibling_state = "opaque-postgres-invalid-client-sibling"
    selected = _seed_flow(pg_conn, opaque_state=selected_state)
    sibling = _seed_flow(pg_conn, opaque_state=sibling_state)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_oauth_configurations SET status = 'verified' "
            "WHERE configuration_id IN (%s, %s)",
            (selected["configuration_id"], sibling["configuration_id"]),
        )
    pg_conn.commit()

    def invalid_client(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(OAuthErrorCode.INVALID_CLIENT, OAuthProviderOperation.EXCHANGE)

    service = _service(
        pg_conn,
        exchange=invalid_client,
        connection_id=f"conn_callback_invalid_client_{uuid4().hex}",
    )
    result = _handle(service, selected_state)

    assert result.model_dump(exclude_none=True) == {
        "outcome": "needs_attention",
        "help_target": {"page_id": "oauth_help", "anchor": "client-credentials"},
    }
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT configuration_id, status, last_error FROM extension_oauth_configurations "
            "WHERE configuration_id IN (%s, %s) ORDER BY configuration_id",
            (selected["configuration_id"], sibling["configuration_id"]),
        )
        rows = {str(row[0]): (row[1], row[2]) for row in cur.fetchall()}
    assert rows[str(selected["configuration_id"])] == (
        "needs_attention",
        {"code": "invalid_client"},
    )
    assert rows[str(sibling["configuration_id"])] == ("verified", {})
    assert "OAuthProviderError" not in repr(result)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("code", "expected_outcome"),
    [
        (OAuthErrorCode.INVALID_GRANT, "authorization_required"),
        (OAuthErrorCode.INVALID_CLIENT, "needs_attention"),
    ],
)
def test_failed_reconnect_preserves_connection_ciphertext_and_usable_grant(
    pg_conn,
    monkeypatch,
    code: OAuthErrorCode,
    expected_outcome: str,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = f"opaque-postgres-reconnect-{code.value}"
    connection_id = f"conn_callback_reconnect_{code.value}_{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state, reconnect=connection_id)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_oauth_configurations SET status = 'verified' "
            "WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT to_jsonb(pc) FROM project_connections pc WHERE connection_id = %s",
            (connection_id,),
        )
        connection_before = cur.fetchone()[0]
        cur.execute(
            "SELECT to_jsonb(sr) FROM secret_refs sr "
            "WHERE account_id = %s AND ref_key LIKE %s ORDER BY ref_key",
            (ACCOUNT, f"project_connection:{connection_id}:%"),
        )
        secrets_before = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT to_jsonb(c) FROM extension_oauth_configurations c WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
        configuration_before = cur.fetchone()[0]

    token_store = DatabaseOAuthCallbackTokenStore(pg_conn)
    token_snapshot_before = token_store.resolve_token_snapshot(
        account_id=str(ACCOUNT),
        secret_refs=connection_before["secret_refs"],
    )
    assert token_snapshot_before == {
        "access_token": "old-access-secret",
        "refresh_token": "old-refresh-secret",
        "expires_at": 9999999999.0,
        "refresh_expires_at": None,
    }

    def failed_exchange(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(code, OAuthProviderOperation.EXCHANGE)

    result = _handle(
        _service(
            pg_conn,
            exchange=failed_exchange,
            connection_id="must-not-create",
        ),
        opaque_state,
    )
    assert result.outcome == expected_outcome

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT to_jsonb(pc) FROM project_connections pc WHERE connection_id = %s",
            (connection_id,),
        )
        connection_after = cur.fetchone()[0]
        cur.execute(
            "SELECT to_jsonb(sr) FROM secret_refs sr "
            "WHERE account_id = %s AND ref_key LIKE %s ORDER BY ref_key",
            (ACCOUNT, f"project_connection:{connection_id}:%"),
        )
        secrets_after = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT to_jsonb(c) FROM extension_oauth_configurations c WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
        configuration_after = cur.fetchone()[0]

    assert connection_after == connection_before
    assert secrets_after == secrets_before
    assert (
        token_store.resolve_token_snapshot(
            account_id=str(ACCOUNT),
            secret_refs=connection_after["secret_refs"],
        )
        == token_snapshot_before
    )
    if code is OAuthErrorCode.INVALID_CLIENT:
        assert configuration_after["status"] == "needs_attention"
        assert configuration_after["last_error"] == {"code": "invalid_client"}
    else:
        assert configuration_after == configuration_before
    runtime = OAuthRuntimeTokenService(
        repository=ExtensionOAuthRepository(pg_conn),
        token_store=DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 1000.0),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        transaction_factory=lambda: pg_conn.transaction(),
        now=lambda: 1000.0,
    )
    assert runtime.resolve_runtime_access_token(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        project_id=str(PROJECT),
        extension_install_id=INSTALL,
        extension_id=EXTENSION,
        provider_id=PROVIDER,
        configuration_id=str(seeded["configuration_id"]),
        connection_id=connection_id,
        connection_type="example_mail",
    ) == {"access_token": "old-access-secret"}


@pytest.mark.integration
def test_final_secret_failure_rolls_back_refs_and_stale_race_cannot_materialize(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_failure = "opaque-postgres-failure"
    _seed_flow(pg_conn, opaque_state=opaque_failure)
    real_store = DatabaseOAuthCallbackTokenStore(pg_conn)

    class FailingStore:
        def resolve_refresh_token(self, **values: Any) -> str:
            return real_store.resolve_refresh_token(**values)

        def write_tokens(self, **values: Any) -> dict[str, Any]:
            real_store.write_tokens(**values)
            raise RuntimeError("rollback canary")

    failed_id = f"conn_callback_fail_{uuid4().hex}"
    failure_service = _service(
        pg_conn,
        exchange=lambda **_values: {"access_token": "must-rollback", "token_type": "Bearer"},
        token_store=FailingStore(),
        connection_id=failed_id,
    )
    assert _handle(failure_service, opaque_failure).outcome == "restart_required"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM project_connections WHERE connection_id = %s", (failed_id,))
        assert cur.fetchone() is None
        cur.execute(
            "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key LIKE %s",
            (ACCOUNT, f"project_connection:{failed_id}:%"),
        )
        assert cur.fetchone() is None

    opaque_race = "opaque-postgres-race"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_race)
    observer = psycopg.connect(oauth_callback_database.dsn)
    observer.autocommit = True
    try:

        def race_exchange(**_values: Any) -> dict[str, Any]:
            assert pg_conn.info.transaction_status is TransactionStatus.IDLE
            with observer.cursor() as cur:
                cur.execute(
                    "UPDATE extension_oauth_configurations SET authorization_revision = authorization_revision + 1 WHERE configuration_id = %s",
                    (seeded["configuration_id"],),
                )
            return {"access_token": "stale-access", "token_type": "Bearer"}

        race_id = f"conn_callback_race_{uuid4().hex}"
        assert (
            _handle(
                _service(pg_conn, exchange=race_exchange, connection_id=race_id),
                opaque_race,
            ).outcome
            == "restart_required"
        )
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        with pg_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM project_connections WHERE connection_id = %s", (race_id,))
            assert cur.fetchone() is None
            cur.execute(
                "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key LIKE %s",
                (ACCOUNT, f"project_connection:{race_id}:%"),
            )
            assert cur.fetchone() is None
    finally:
        observer.close()


@pytest.mark.integration
def test_reconnect_without_rotated_refresh_preserves_old_secret_and_updates_only_target(
    pg_conn, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = "opaque-postgres-reconnect"
    connection_id = f"conn_callback_reconnect_{uuid4().hex}"
    _seed_flow(pg_conn, opaque_state=opaque_state, reconnect=connection_id)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE project_connections "
            "SET health_status = 'unknown', runtime_status = 'unavailable', "
            "last_seen_ok_at = NULL, last_error = '' "
            "WHERE connection_id = %s",
            (connection_id,),
        )
    pg_conn.commit()

    service = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "new-access-secret",
            "token_type": "Bearer",
            "scope": "mail.read",
        },
        connection_id="must-not-create",
    )
    result = _handle(service, opaque_state)
    assert result.outcome == "connected"
    assert result.connection_id == connection_id
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT credential_revision, oauth_granted_scopes, secret_refs, connection_config, "
            "health_status, runtime_status, last_seen_ok_at, last_error "
            "FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        (
            revision,
            granted,
            refs,
            connection_config,
            health_status,
            runtime_status,
            last_seen_ok_at,
            last_error,
        ) = cur.fetchone()
        assert revision == 8
        assert list(granted) == ["mail.read"]
        assert connection_config["extension_metadata"] == {"canary": "preserve-reconnect-metadata"}
        assert connection_config["oauth"]["provider_subject"] == "preserve-provider-subject"
        assert connection_config["oauth"]["lifecycle_status"] == "connected"
        assert health_status == "healthy"
        assert runtime_status == "ready"
        assert last_seen_ok_at is not None
        assert last_error == ""
    secrets = project_connection_secrets.resolve_connection_secrets(
        pg_conn,
        account_id=str(ACCOUNT),
        secret_refs=refs,
        include_internal=True,
    )
    assert secrets["_oauth_refresh_token"] == "old-refresh-secret"
    assert secrets["_oauth_access_token"] == "new-access-secret"
    assert "_oauth_expires_at" not in secrets
    assert "_oauth_expires_at" not in refs["refs"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key = %s",
            (ACCOUNT, f"project_connection:{connection_id}:_oauth_expires_at"),
        )
        assert cur.fetchone() is None


@pytest.mark.integration
def test_reconnect_post_connect_failure_clears_stale_verified_identity_and_keeps_new_token(
    pg_conn, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = "opaque-postgres-reconnect-post-connect-failure"
    connection_id = f"conn_callback_reconnect_attention_{uuid4().hex}"
    _seed_flow(pg_conn, opaque_state=opaque_state, reconnect=connection_id)
    historical_last_seen = datetime(2026, 1, 1, tzinfo=UTC)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE project_connections "
            "SET health_status = 'healthy', runtime_status = 'ready', "
            "last_seen_ok_at = %s, last_error = 'stale-error' "
            "WHERE connection_id = %s",
            (historical_last_seen, connection_id),
        )
    pg_conn.commit()

    service = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "new-reconnect-access-canary",
            "token_type": "Bearer",
            "scope": "mail.read",
        },
        connection_id="must-not-create",
        post_connect=lambda **_values: {
            "status": "failed",
            "response": {
                "ok": False,
                "result": {},
                "error_code": "provider_unavailable",
                "error": "raw-provider-copy-canary",
            },
        },
    )

    result = _handle(service, opaque_state)

    assert result.outcome == "needs_attention"
    assert result.connection_id == connection_id
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT credential_revision, display_name, secret_refs, connection_config, "
            "health_status, runtime_status, last_seen_ok_at, last_error "
            "FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        (
            revision,
            display_name,
            refs,
            config,
            health_status,
            runtime_status,
            last_seen_ok_at,
            last_error,
        ) = cur.fetchone()
    assert revision == 9
    assert display_name == "Callback mailbox"
    assert config["extension_metadata"] == {"canary": "preserve-reconnect-metadata"}
    assert config["oauth"]["provider_subject"] == "preserve-provider-subject"
    assert config["oauth"]["lifecycle_status"] == "needs_attention"
    assert config["oauth"]["last_error_code"] == "provider_unavailable"
    assert health_status == "failed"
    assert runtime_status == "unavailable"
    assert last_seen_ok_at == historical_last_seen
    assert last_error == "provider_unavailable"
    assert not {
        "subject",
        "verified_display_name",
        "verified_email",
        "safe_metadata",
    } & set(config["oauth"])
    assert "raw-provider-copy-canary" not in repr(config)
    secrets = project_connection_secrets.resolve_connection_secrets(
        pg_conn,
        account_id=str(ACCOUNT),
        secret_refs=refs,
        include_internal=True,
    )
    assert secrets["_oauth_access_token"] == "new-reconnect-access-canary"
    assert secrets["_oauth_refresh_token"] == "old-refresh-secret"


@pytest.mark.integration
def test_runtime_refresh_keeps_database_idle_during_http_and_commits_atomically(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = "opaque-postgres-runtime-refresh"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    connection_id = f"conn_callback_refresh_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "old-runtime-access",
            "refresh_token": "preserved-runtime-refresh",
            "token_type": "Bearer",
            "expires_in": 10,
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"

    observer = psycopg.connect(oauth_callback_database.dsn, autocommit=True)
    refresh_calls: list[dict[str, Any]] = []

    def refresh(**values: Any) -> dict[str, Any]:
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        with observer.cursor() as cur:
            cur.execute(
                "SELECT credential_revision FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            assert cur.fetchone()[0] == 1
        refresh_calls.append(copy.deepcopy(values))
        return {
            "access_token": "new-runtime-access",
            "token_type": "Bearer",
            "expires_in": 300,
        }

    runtime = OAuthRuntimeTokenService(
        repository=ExtensionOAuthRepository(
            pg_conn,
            refresh_claim_connection_factory=lambda: _claim_connection(oauth_callback_database.dsn),
        ),
        token_store=DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 2000.0),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        transaction_factory=lambda: pg_conn.transaction(),
        configuration_secret_value=lambda **_values: "client-secret",
        refresh_access_token=refresh,
        now=lambda: 2000.0,
    )

    try:
        assert runtime.resolve_runtime_access_token(
            tenant_id=str(TENANT),
            account_id=str(ACCOUNT),
            project_id=str(PROJECT),
            extension_install_id=INSTALL,
            extension_id=EXTENSION,
            provider_id=PROVIDER,
            configuration_id=str(seeded["configuration_id"]),
            connection_id=connection_id,
            connection_type="example_mail",
        ) == {"access_token": "new-runtime-access"}
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        assert refresh_calls[0]["refresh_token"] == "preserved-runtime-refresh"
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT credential_revision, secret_refs FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            revision, refs = cur.fetchone()
        assert revision == 2
        resolved = project_connection_secrets.resolve_connection_secrets(
            pg_conn,
            account_id=str(ACCOUNT),
            secret_refs=refs,
            include_internal=True,
        )
        assert resolved["_oauth_access_token"] == "new-runtime-access"
        assert resolved["_oauth_refresh_token"] == "preserved-runtime-refresh"
    finally:
        observer.close()


@pytest.mark.integration
def test_runtime_revalidate_save_forces_unexpired_retained_connection_refresh(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    opaque_state = f"opaque-runtime-revalidate-{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    connection_id = f"conn_callback_revalidate_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "unexpired-retained-access",
            "refresh_token": "retained-refresh",
            "token_type": "Bearer",
            "expires_in": 1000,
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"

    saved = _configuration_service(pg_conn).save_configuration(
        _operator_scope(),
        INSTALL,
        PROVIDER,
        OAuthConfigurationSave(
            configuration_id=seeded["configuration_id"],
            display_name="Callback configuration",
            secret_replacements={"client_secret": "repaired-client-secret"},
        ),
        expected_revision=1,
    )
    assert saved.status == "ready_for_authorization"
    refresh_calls: list[str] = []
    runtime = OAuthRuntimeTokenService(
        repository=ExtensionOAuthRepository(
            pg_conn,
            refresh_claim_connection_factory=lambda: _claim_connection(oauth_callback_database.dsn),
        ),
        token_store=DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 1100.0),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        transaction_factory=lambda: pg_conn.transaction(),
        configuration_secret_value=lambda **_values: "repaired-client-secret",
        refresh_access_token=lambda **_values: (
            refresh_calls.append("refresh")
            or {
                "access_token": "revalidated-retained-access",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "mail.read mail.write",
            }
        ),
        now=lambda: 1100.0,
    )

    assert runtime.resolve_runtime_access_token(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        project_id=str(PROJECT),
        extension_install_id=INSTALL,
        extension_id=EXTENSION,
        provider_id=PROVIDER,
        configuration_id=str(seeded["configuration_id"]),
        connection_id=connection_id,
        connection_type="example_mail",
    ) == {"access_token": "revalidated-retained-access"}
    assert refresh_calls == ["refresh"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT credential_revision, connection_config FROM project_connections "
            "WHERE connection_id = %s",
            (connection_id,),
        )
        revision, connection_config = cur.fetchone()
        cur.execute(
            "SELECT status FROM extension_oauth_configurations WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
        status = cur.fetchone()[0]
    assert revision == 2
    assert connection_config["oauth"]["lifecycle_status"] == "connected"
    assert status == "verified"


@pytest.mark.integration
def test_repaired_invalid_client_recovers_after_sibling_verifies_configuration(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    seeded = _seed_flow(pg_conn, opaque_state=f"opaque-sibling-{uuid4().hex}")
    provider = seeded["provider"]
    repository = ExtensionOAuthRepository(pg_conn)
    token_store = DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 1000.0)
    connection_ids = {
        "failed": f"conn_callback_failed_{uuid4().hex}",
        "sibling": f"conn_callback_sibling_{uuid4().hex}",
    }
    for name, connection_id in connection_ids.items():
        lifecycle = "needs_attention" if name == "failed" else "connected"
        last_error = "invalid_client" if name == "failed" else None
        ready = name != "failed"
        assert repository.create_oauth_connection(
            connection_id=connection_id,
            tenant_id=TENANT,
            account_id=ACCOUNT,
            project_id=PROJECT,
            extension_install_id=INSTALL,
            extension_id=EXTENSION,
            display_name=f"{name.title()} account",
            connection_kind="extension",
            connection_type="example_mail",
            oauth_configuration_id=seeded["configuration_id"],
            oauth_provider_id=PROVIDER,
            oauth_extension_release_id=RELEASE,
            oauth_authorization_revision=1,
            oauth_requested_scopes=["mail.read", "mail.write"],
            oauth_granted_scopes=["mail.read", "mail.write"],
            oauth_provider_security_fingerprint=provider_security_fingerprint(provider),
            oauth_provider_revocation_fingerprint=provider_revocation_fingerprint(provider),
            health_status="healthy" if ready else "failed",
            runtime_status="ready" if ready else "unavailable",
            last_error=last_error or "",
            mark_last_seen_ok=ready,
            connection_config={
                "oauth": {
                    "lifecycle_status": lifecycle,
                    "last_error_code": last_error,
                }
            },
            secret_refs=token_store.write_tokens(
                account_id=str(ACCOUNT),
                connection_id=connection_id,
                token_response={
                    "access_token": f"unexpired-{name}-access",
                    "refresh_token": f"{name}-refresh",
                    "expires_in": 1000,
                    "token_type": "Bearer",
                },
                existing_refresh_token=None,
            ),
        )
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_oauth_configurations SET status = 'needs_attention', "
            'last_error = \'{"code":"invalid_client"}\'::jsonb '
            "WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
    blocked_runtime = OAuthRuntimeTokenService(
        repository=ExtensionOAuthRepository(pg_conn),
        token_store=DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 1100.0),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        transaction_factory=lambda: pg_conn.transaction(),
        now=lambda: 1100.0,
    )
    assert blocked_runtime.resolve_runtime_access_token(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        project_id=str(PROJECT),
        extension_install_id=INSTALL,
        extension_id=EXTENSION,
        provider_id=PROVIDER,
        configuration_id=str(seeded["configuration_id"]),
        connection_id=connection_ids["sibling"],
        connection_type="example_mail",
    ) == {"access_token": "unexpired-sibling-access"}
    _raises_input_1336_1 = str(TENANT)
    _raises_input_1336_2 = str(ACCOUNT)
    _raises_input_1336_3 = str(PROJECT)
    _raises_input_1336_4 = str(seeded["configuration_id"])
    with pytest.raises(OAuthRuntimeTokenError, match="oauth_needs_attention"):
        blocked_runtime.resolve_runtime_access_token(
            tenant_id=_raises_input_1336_1,
            account_id=_raises_input_1336_2,
            project_id=_raises_input_1336_3,
            extension_install_id=INSTALL,
            extension_id=EXTENSION,
            provider_id=PROVIDER,
            configuration_id=_raises_input_1336_4,
            connection_id=connection_ids["failed"],
            connection_type="example_mail",
        )
    repaired = _configuration_service(pg_conn).save_configuration(
        _operator_scope(),
        INSTALL,
        PROVIDER,
        OAuthConfigurationSave(
            configuration_id=seeded["configuration_id"],
            display_name="Callback configuration",
            secret_replacements={"client_secret": "repaired-client-secret"},
        ),
        expected_revision=1,
    )
    assert repaired.status == "ready_for_authorization"
    refresh_order: list[str] = []
    runtime = OAuthRuntimeTokenService(
        repository=ExtensionOAuthRepository(
            pg_conn,
            refresh_claim_connection_factory=lambda: _claim_connection(oauth_callback_database.dsn),
        ),
        token_store=DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 1100.0),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        transaction_factory=lambda: pg_conn.transaction(),
        configuration_secret_value=lambda **_values: "repaired-client-secret",
        refresh_access_token=lambda **values: (
            refresh_order.append(str(values["refresh_token"]).split("-", 1)[0])
            or {
                "access_token": "recovered-access",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "mail.read mail.write",
            }
        ),
        now=lambda: 1100.0,
    )
    for name in ("sibling", "failed"):
        assert runtime.resolve_runtime_access_token(
            tenant_id=str(TENANT),
            account_id=str(ACCOUNT),
            project_id=str(PROJECT),
            extension_install_id=INSTALL,
            extension_id=EXTENSION,
            provider_id=PROVIDER,
            configuration_id=str(seeded["configuration_id"]),
            connection_id=connection_ids[name],
            connection_type="example_mail",
        ) == {"access_token": "recovered-access"}
    assert refresh_order == ["sibling", "failed"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT connection_id, credential_revision, connection_config "
            "FROM project_connections WHERE connection_id = ANY(%s)",
            (list(connection_ids.values()),),
        )
        rows = {row[0]: row[1:] for row in cur.fetchall()}
        cur.execute(
            "SELECT status FROM extension_oauth_configurations WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
        status = cur.fetchone()[0]
    assert status == "verified"
    for connection_id in connection_ids.values():
        revision, connection_config = rows[connection_id]
        assert revision == 2
        assert connection_config["oauth"]["lifecycle_status"] == "connected"


@pytest.mark.integration
def test_post_connect_retry_refreshes_then_uses_the_committed_revision(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = f"opaque-post-connect-retry-{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    connection_id = f"conn_post_connect_retry_{uuid4().hex}"
    runtime = OAuthRuntimeTokenService(
        repository=ExtensionOAuthRepository(
            pg_conn,
            refresh_claim_connection_factory=lambda: _claim_connection(oauth_callback_database.dsn),
        ),
        token_store=DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 2000.0),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        transaction_factory=lambda: pg_conn.transaction(),
        configuration_secret_value=lambda **_values: "client-secret",
        refresh_access_token=lambda **_values: {
            "access_token": "post-connect-refreshed-access",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "mail.read mail.write",
        },
        now=lambda: 2000.0,
    )
    invocations = 0

    def post_connect(**_values: Any) -> dict[str, Any]:
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            return {
                "status": "failed",
                "response": {"error_code": "provider_unavailable"},
            }
        return {
            "status": "succeeded",
            "response": {
                "result": {
                    "subject": "retry-subject",
                    "verified_email": "retry@example.test",
                    "safe_metadata": {},
                    "health": "ok",
                }
            },
        }

    service = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "post-connect-expired-access",
            "refresh_token": "post-connect-refresh",
            "token_type": "Bearer",
            "expires_in": 10,
        },
        connection_id=connection_id,
        post_connect=post_connect,
        runtime_resolver=runtime.resolve_runtime_access_token,
    )
    assert _handle(service, opaque_state).outcome == "needs_attention"

    result = service.retry_post_connect(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        project_id=str(PROJECT),
        extension_install_id=INSTALL,
        extension_id=EXTENSION,
        provider_id=PROVIDER,
        configuration_id=str(seeded["configuration_id"]),
        connection_id=connection_id,
        connection_type="example_mail",
    )

    assert result.outcome == "connected"
    assert invocations == 2
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT credential_revision, connection_config FROM project_connections "
            "WHERE connection_id = %s",
            (connection_id,),
        )
        revision, connection_config = cur.fetchone()
    assert revision == 4
    assert connection_config["oauth"]["lifecycle_status"] == "connected"
    assert connection_config["oauth"]["verified_email"] == "retry@example.test"


@pytest.mark.integration
def test_runtime_refresh_rejects_install_disable_race_and_releases_claim(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = "opaque-postgres-runtime-disable-race"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    connection_id = f"conn_callback_refresh_disable_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "old-disable-race-access",
            "refresh_token": "old-disable-race-refresh",
            "token_type": "Bearer",
            "expires_in": 10,
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"

    claim_sessions: list[psycopg.Connection] = []

    @contextmanager
    def observed_claim_connection() -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(oauth_callback_database.dsn, autocommit=True)
        claim_sessions.append(conn)
        try:
            yield conn
        finally:
            conn.close()

    refresh_entered = threading.Event()
    release_refresh = threading.Event()

    def paused_refresh(**_values: Any) -> dict[str, Any]:
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        assert claim_sessions[-1].info.transaction_status is TransactionStatus.IDLE
        refresh_entered.set()
        assert release_refresh.wait(timeout=5)
        return {
            "access_token": "must-not-commit-after-disable",
            "refresh_token": "must-not-rotate-after-disable",
            "token_type": "Bearer",
            "expires_in": 300,
        }

    def runtime(refresh) -> OAuthRuntimeTokenService:
        return OAuthRuntimeTokenService(
            repository=ExtensionOAuthRepository(
                pg_conn,
                refresh_claim_connection_factory=observed_claim_connection,
            ),
            token_store=DatabaseOAuthCallbackTokenStore(pg_conn, now=lambda: 2000.0),
            install_loader=lambda _install, _account: {
                "extension_id": EXTENSION,
                "release_id": RELEASE,
                "enabled": True,
                "compiled_manifest": _manifest(),
            },
            transaction_factory=lambda: pg_conn.transaction(),
            configuration_secret_value=lambda **_values: "client-secret",
            refresh_access_token=refresh,
            now=lambda: 2000.0,
        )

    result: dict[str, str] = {}

    def resolve_paused() -> None:
        try:
            runtime(paused_refresh).resolve_runtime_access_token(
                tenant_id=str(TENANT),
                account_id=str(ACCOUNT),
                project_id=str(PROJECT),
                extension_install_id=INSTALL,
                extension_id=EXTENSION,
                provider_id=PROVIDER,
                configuration_id=str(seeded["configuration_id"]),
                connection_id=connection_id,
                connection_type="example_mail",
            )
        except Exception as exc:  # asserted below without leaking provider detail
            result["error"] = getattr(exc, "code", type(exc).__name__)

    worker = threading.Thread(target=resolve_paused)
    worker.start()
    assert refresh_entered.wait(timeout=5)
    lifecycle_conn = psycopg.connect(oauth_callback_database.dsn, autocommit=True)
    try:
        with lifecycle_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET state = 'disabled' WHERE install_id = %s",
                (INSTALL,),
            )
        release_refresh.set()
        worker.join(timeout=5)
        assert result == {"error": "oauth_runtime_blocked"}
        with lifecycle_conn.cursor() as cur:
            cur.execute(
                "SELECT credential_revision, secret_refs FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            revision, refs = cur.fetchone()
        assert revision == 1
        secrets = project_connection_secrets.resolve_connection_secrets(
            lifecycle_conn,
            account_id=str(ACCOUNT),
            secret_refs=refs,
            include_internal=True,
        )
        assert secrets["_oauth_access_token"] == "old-disable-race-access"
        assert secrets["_oauth_refresh_token"] == "old-disable-race-refresh"

        with lifecycle_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET state = 'active' WHERE install_id = %s",
                (INSTALL,),
            )
        retried = runtime(
            lambda **_values: {
                "access_token": "claim-release-proved",
                "token_type": "Bearer",
                "expires_in": 300,
            }
        ).resolve_runtime_access_token(
            tenant_id=str(TENANT),
            account_id=str(ACCOUNT),
            project_id=str(PROJECT),
            extension_install_id=INSTALL,
            extension_id=EXTENSION,
            provider_id=PROVIDER,
            configuration_id=str(seeded["configuration_id"]),
            connection_id=connection_id,
            connection_type="example_mail",
        )
        assert retried == {"access_token": "claim-release-proved"}
    finally:
        release_refresh.set()
        worker.join(timeout=5)
        with lifecycle_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET state = 'active' WHERE install_id = %s",
                (INSTALL,),
            )
        lifecycle_conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_runtime_refresh_has_one_provider_owner_and_loser_resnapshots(
    pg_conn, oauth_callback_database, monkeypatch, attempt
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = f"opaque-postgres-runtime-concurrent-{attempt}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    connection_id = f"conn_callback_refresh_concurrent_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "old-concurrent-access",
            "refresh_token": "old-concurrent-refresh",
            "token_type": "Bearer",
            "expires_in": 10,
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"

    second_conn = psycopg.connect(oauth_callback_database.dsn)
    second_conn.autocommit = False
    success_entered = threading.Event()
    release_success = threading.Event()
    loser_claim_started = threading.Event()
    winner_claim_sessions: list[psycopg.Connection] = []
    loser_claim_sessions: list[psycopg.Connection] = []
    provider_calls: list[str] = []
    results: dict[str, str] = {}

    def claim_factory(
        sessions: list[psycopg.Connection],
        started: threading.Event | None = None,
    ):
        @contextmanager
        def claim_connection() -> Iterator[psycopg.Connection]:
            conn = psycopg.connect(oauth_callback_database.dsn, autocommit=True)
            sessions.append(conn)
            if started is not None:
                started.set()
            try:
                yield conn
            finally:
                conn.close()

        return claim_connection

    def runtime(conn, refresh, claim_connection) -> OAuthRuntimeTokenService:
        return OAuthRuntimeTokenService(
            repository=ExtensionOAuthRepository(
                conn,
                refresh_claim_connection_factory=claim_connection,
            ),
            token_store=DatabaseOAuthCallbackTokenStore(conn, now=lambda: 2000.0),
            install_loader=lambda _install, _account: {
                "extension_id": EXTENSION,
                "release_id": RELEASE,
                "enabled": True,
                "compiled_manifest": _manifest(),
            },
            transaction_factory=lambda: conn.transaction(),
            configuration_secret_value=lambda **_values: "client-secret",
            refresh_access_token=refresh,
            now=lambda: 2000.0,
        )

    def successful_refresh(**_values: Any) -> dict[str, Any]:
        provider_calls.append("success")
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        assert winner_claim_sessions[-1].info.transaction_status is TransactionStatus.IDLE
        success_entered.set()
        assert release_success.wait(timeout=5)
        return {
            "access_token": "winning-concurrent-access",
            "refresh_token": "winning-concurrent-refresh",
            "token_type": "Bearer",
            "expires_in": 300,
        }

    def losing_refresh(**_values: Any) -> dict[str, Any]:
        provider_calls.append("loser")
        raise OAuthProviderError(OAuthErrorCode.INVALID_GRANT, OAuthProviderOperation.REFRESH)

    services = {
        "winner": runtime(
            pg_conn,
            successful_refresh,
            claim_factory(winner_claim_sessions),
        ),
        "loser": runtime(
            second_conn,
            losing_refresh,
            claim_factory(loser_claim_sessions, loser_claim_started),
        ),
    }

    def resolve(name: str) -> None:
        try:
            result = services[name].resolve_runtime_access_token(
                tenant_id=str(TENANT),
                account_id=str(ACCOUNT),
                project_id=str(PROJECT),
                extension_install_id=INSTALL,
                extension_id=EXTENSION,
                provider_id=PROVIDER,
                configuration_id=str(seeded["configuration_id"]),
                connection_id=connection_id,
                connection_type="example_mail",
            )
            results[name] = result["access_token"]
        except Exception as exc:
            results[name] = getattr(exc, "code", type(exc).__name__)

    winner = threading.Thread(target=resolve, args=("winner",))
    loser = threading.Thread(target=resolve, args=("loser",))
    try:
        winner.start()
        assert success_entered.wait(timeout=5)
        loser.start()
        assert loser_claim_started.wait(timeout=5)
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        assert second_conn.info.transaction_status is TransactionStatus.IDLE
        assert loser_claim_sessions[-1].info.transaction_status in {
            TransactionStatus.IDLE,
            TransactionStatus.ACTIVE,
        }
        release_success.set()
        winner.join(timeout=5)
        loser.join(timeout=5)
        assert results == {
            "winner": "winning-concurrent-access",
            "loser": "winning-concurrent-access",
        }
        assert provider_calls == ["success"]
    finally:
        release_success.set()
        winner.join(timeout=5)
        loser.join(timeout=5)
        second_conn.close()


@pytest.mark.integration
def test_configuration_delete_revokes_outside_transaction_and_cleans_after_provider_failure(
    pg_conn, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    opaque_state = f"opaque-delete-{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    configuration_id = str(seeded["configuration_id"])
    configuration_secret_ref = f"extension_oauth_configuration:{configuration_id}:client_secret"
    DatabaseOAuthConfigurationSecretStore(pg_conn).write(
        account_id=str(ACCOUNT), ref_key=configuration_secret_ref, value="client-secret"
    )
    pg_conn.commit()
    connection_id = f"conn_callback_delete_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "delete-access-canary",
            "refresh_token": "delete-refresh-canary",
            "token_type": "Bearer",
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"
    token_store = DatabaseOAuthCallbackTokenStore(pg_conn)
    revoke_calls: list[str] = []

    def revoke(**values: Any) -> None:
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        assert values["token"] == "delete-refresh-canary"
        revoke_calls.append("attempted")
        raise RuntimeError("provider unavailable")

    service = OAuthConfigurationService(
        repository=ExtensionOAuthRepository(pg_conn),
        secret_store=DatabaseOAuthConfigurationSecretStore(pg_conn),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "compiled_manifest": _manifest(),
        },
        confirmation_key=b"postgres-delete-confirmation",
        transaction_factory=lambda: pg_conn.transaction(),
        token_snapshot=token_store.resolve_token_snapshot,
        configuration_secret_value=lambda **_values: "client-secret",
        revoke_token=revoke,
    )
    impact = service.preview_configuration_delete(
        _operator_scope(), INSTALL, PROVIDER, configuration_id, expected_revision=1
    )
    service.delete_configuration(
        _operator_scope(),
        INSTALL,
        PROVIDER,
        configuration_id,
        expected_revision=1,
        confirmation_token=impact.confirmation_token,
    )

    assert revoke_calls == ["attempted"]
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM project_connections WHERE connection_id = %s", (connection_id,))
        assert cur.fetchone() is None
        cur.execute(
            "SELECT 1 FROM extension_oauth_configurations WHERE configuration_id = %s",
            (configuration_id,),
        )
        assert cur.fetchone() is None
        cur.execute(
            "SELECT 1 FROM secret_refs WHERE account_id = %s AND (ref_key = %s OR ref_key LIKE %s)",
            (ACCOUNT, configuration_secret_ref, f"project_connection:{connection_id}:%"),
        )
        assert cur.fetchone() is None


@pytest.mark.integration
def test_connection_disconnect_retains_identity_but_removes_exact_grant(
    pg_conn, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    opaque_state = f"opaque-disconnect-{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    configuration_id = str(seeded["configuration_id"])
    connection_id = f"conn_callback_disconnect_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "disconnect-access-canary",
            "refresh_token": "disconnect-refresh-canary",
            "token_type": "Bearer",
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"
    token_store = DatabaseOAuthCallbackTokenStore(pg_conn)

    def revoke(**values: Any) -> None:
        assert pg_conn.info.transaction_status is TransactionStatus.IDLE
        assert values["token"] == "disconnect-refresh-canary"

    service = OAuthConnectionLifecycleService(
        repository=ExtensionOAuthRepository(pg_conn),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "compiled_manifest": _manifest(),
        },
        token_snapshot=token_store.resolve_token_snapshot,
        configuration_secret_value=lambda **_values: "client-secret",
        delete_secret=lambda **values: model_provider_store.delete_secret_ref(pg_conn, **values),
        revoke_token=revoke,
        transaction_factory=lambda: pg_conn.transaction(),
    )
    result = service.disconnect(
        _operator_scope(), INSTALL, PROVIDER, configuration_id, connection_id
    )

    assert result["remote_revocation"] == "succeeded"
    assert pg_conn.info.transaction_status is TransactionStatus.IDLE
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, oauth_granted_scopes, secret_refs, connection_config->'oauth'->>'lifecycle_status' FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        assert cur.fetchone() == (True, [], {}, "disconnected")
        cur.execute(
            "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key LIKE %s",
            (ACCOUNT, f"project_connection:{connection_id}:%"),
        )
        assert cur.fetchone() is None


@pytest.mark.integration
def test_disconnect_recovers_an_expired_database_lease_after_process_restart(
    pg_conn, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    opaque_state = f"opaque-disconnect-recovery-{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    configuration_id = str(seeded["configuration_id"])
    connection_id = f"conn_callback_disconnect_recovery_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "recovery-access-canary",
            "refresh_token": "recovery-refresh-canary",
            "token_type": "Bearer",
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"
    repository = ExtensionOAuthRepository(pg_conn)
    exact_scope = {
        "tenant_id": str(TENANT),
        "account_id": str(ACCOUNT),
        "project_id": str(PROJECT),
        "extension_install_id": INSTALL,
        "extension_id": EXTENSION,
        "extension_release_id": RELEASE,
        "provider_id": PROVIDER,
        "configuration_id": configuration_id,
        "connection_id": connection_id,
    }
    with pg_conn.transaction():
        row = repository.lock_connection_for_lifecycle(**exact_scope)
        assert row is not None
        initial_revision = int(row["credential_revision"])
        assert repository.block_connection_disconnect(
            **exact_scope,
            expected_credential_revision=initial_revision,
            disconnect_operation_id="crashed-disconnect-operation",
            lease_seconds=300,
        )
    with pg_conn.transaction(), pg_conn.cursor() as cur:
        cur.execute(
            """UPDATE project_connections
                  SET connection_config = jsonb_set(
                      connection_config,
                      '{oauth,disconnect_lease_expires_at}',
                      to_jsonb(now() - interval '1 second')
                  )
                WHERE connection_id = %s""",
            (connection_id,),
        )

    revocations: list[str] = []
    service = OAuthConnectionLifecycleService(
        repository=repository,
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "compiled_manifest": _manifest(),
        },
        token_snapshot=DatabaseOAuthCallbackTokenStore(pg_conn).resolve_token_snapshot,
        configuration_secret_value=lambda **_values: "client-secret",
        delete_secret=lambda **values: model_provider_store.delete_secret_ref(pg_conn, **values),
        revoke_token=lambda **values: revocations.append(values["token"]),
        transaction_factory=lambda: pg_conn.transaction(),
    )

    result = service.disconnect(
        _operator_scope(), INSTALL, PROVIDER, configuration_id, connection_id
    )

    assert result == {
        "status": "disconnected",
        "connection_id": connection_id,
        "remote_revocation": "succeeded",
    }
    assert revocations == ["recovery-refresh-canary"]
    with pg_conn.cursor() as cur:
        cur.execute(
            """SELECT credential_revision, enabled,
                      connection_config->'oauth'->>'lifecycle_status',
                      connection_config->'oauth'->>'disconnect_operation_id',
                      connection_config->'oauth'->>'disconnect_started_at',
                      connection_config->'oauth'->>'disconnect_lease_expires_at',
                      secret_refs
                 FROM project_connections WHERE connection_id = %s""",
            (connection_id,),
        )
        assert cur.fetchone() == (
            initial_revision + 2,
            True,
            "disconnected",
            None,
            None,
            None,
            {},
        )


@pytest.mark.integration
def test_disconnect_release_drift_during_remote_revoke_still_cleans_local(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    opaque_state = f"opaque-disconnect-drift-{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    configuration_id = str(seeded["configuration_id"])
    connection_id = f"conn_callback_disconnect_drift_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "drift-access-canary",
            "refresh_token": "drift-refresh-canary",
            "token_type": "Bearer",
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"
    pg_conn.commit()
    drift_release = f"release_disconnect_drift_{uuid4().hex}"
    with pg_conn.transaction(), pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_releases (release_id, extension_id, release_version, descriptor_sha256) VALUES (%s, %s, '2.0.0', repeat('b', 64))",
            (drift_release, EXTENSION),
        )

    service_conn = psycopg.connect(oauth_callback_database.dsn)
    drift_conn = psycopg.connect(oauth_callback_database.dsn)
    result: dict[str, Any] = {}

    def install_loader(_install: str, _account: str) -> dict[str, Any] | None:
        with service_conn.cursor() as cur:
            cur.execute(
                "SELECT release_id FROM extension_installs WHERE install_id = %s AND account_id = %s",
                (INSTALL, ACCOUNT),
            )
            row = cur.fetchone()
        if row is None:
            return None
        manifest = _manifest()
        if row[0] != RELEASE:
            manifest["runtime"]["extension_contract_v2"]["oauth_providers"] = []
        return {
            "extension_id": EXTENSION,
            "release_id": row[0],
            "compiled_manifest": manifest,
        }

    def revoke(**_values: Any) -> None:
        with drift_conn.transaction(), drift_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET release_id = %s WHERE install_id = %s AND account_id = %s",
                (drift_release, INSTALL, ACCOUNT),
            )
            cur.execute(
                "UPDATE project_connections SET oauth_extension_release_id = %s, oauth_provider_security_fingerprint = %s, oauth_provider_revocation_fingerprint = %s WHERE connection_id = %s",
                (
                    drift_release,
                    "replacement-security-fingerprint",
                    "replacement-revocation-fingerprint",
                    connection_id,
                ),
            )

    service = OAuthConnectionLifecycleService(
        repository=ExtensionOAuthRepository(service_conn),
        install_loader=install_loader,
        token_snapshot=DatabaseOAuthCallbackTokenStore(service_conn).resolve_token_snapshot,
        configuration_secret_value=lambda **_values: "client-secret",
        delete_secret=lambda **values: model_provider_store.delete_secret_ref(
            service_conn, **values
        ),
        revoke_token=revoke,
        transaction_factory=lambda: service_conn.transaction(),
    )
    try:
        result = service.disconnect(
            _operator_scope(), INSTALL, PROVIDER, configuration_id, connection_id
        )
        assert result["remote_revocation"] == "succeeded"
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT secret_refs, connection_config->'oauth'->>'lifecycle_status' FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            assert cur.fetchone() == ({}, "disconnected")
            cur.execute(
                "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key LIKE %s",
                (ACCOUNT, f"project_connection:{connection_id}:%"),
            )
            assert cur.fetchone() is None
    finally:
        with drift_conn.transaction(), drift_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET release_id = %s WHERE install_id = %s AND account_id = %s",
                (RELEASE, INSTALL, ACCOUNT),
            )
            provider = compile_oauth_providers(_manifest())[0]
            cur.execute(
                "UPDATE project_connections SET oauth_extension_release_id = %s, oauth_provider_security_fingerprint = %s, oauth_provider_revocation_fingerprint = %s WHERE connection_id = %s",
                (
                    RELEASE,
                    provider_security_fingerprint(provider),
                    provider_revocation_fingerprint(provider),
                    connection_id,
                ),
            )
            cur.execute("DELETE FROM extension_releases WHERE release_id = %s", (drift_release,))
        service_conn.close()
        drift_conn.close()


@pytest.mark.integration
def test_reconnect_is_rejected_while_disconnect_remote_revoke_is_in_flight(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    opaque_state = f"opaque-reconnect-block-{uuid4().hex}"
    seeded = _seed_flow(pg_conn, opaque_state=opaque_state)
    configuration_id = str(seeded["configuration_id"])
    configuration_ref = f"extension_oauth_configuration:{configuration_id}:client_secret"
    DatabaseOAuthConfigurationSecretStore(pg_conn).write(
        account_id=str(ACCOUNT), ref_key=configuration_ref, value="client-secret"
    )
    connection_id = f"conn_callback_reconnect_block_{uuid4().hex}"
    callback = _service(
        pg_conn,
        exchange=lambda **_values: {
            "access_token": "reconnect-block-access",
            "refresh_token": "reconnect-block-refresh",
            "token_type": "Bearer",
        },
        connection_id=connection_id,
    )
    assert _handle(callback, opaque_state).outcome == "connected"
    pg_conn.commit()

    disconnect_conn = psycopg.connect(oauth_callback_database.dsn)
    authorization_conn = psycopg.connect(oauth_callback_database.dsn)
    revoke_entered = threading.Event()
    release_revoke = threading.Event()
    disconnect_result: dict[str, Any] = {}

    def revoke(**_values: Any) -> None:
        revoke_entered.set()
        assert release_revoke.wait(timeout=5)

    disconnect_service = OAuthConnectionLifecycleService(
        repository=ExtensionOAuthRepository(disconnect_conn),
        install_loader=lambda _install, _account: {
            "extension_id": EXTENSION,
            "release_id": RELEASE,
            "compiled_manifest": _manifest(),
        },
        token_snapshot=DatabaseOAuthCallbackTokenStore(disconnect_conn).resolve_token_snapshot,
        configuration_secret_value=lambda **_values: "client-secret",
        delete_secret=lambda **values: model_provider_store.delete_secret_ref(
            disconnect_conn, **values
        ),
        revoke_token=revoke,
        transaction_factory=lambda: disconnect_conn.transaction(),
    )

    def run_disconnect() -> None:
        disconnect_result.update(
            disconnect_service.disconnect(
                _operator_scope(), INSTALL, PROVIDER, configuration_id, connection_id
            )
        )

    thread = threading.Thread(target=run_disconnect)
    thread.start()
    try:
        assert revoke_entered.wait(timeout=5)
        authorization_service = OAuthAuthorizationService(
            repository=ExtensionOAuthRepository(authorization_conn),
            install_loader=lambda _install, _account: {
                "extension_id": EXTENSION,
                "release_id": RELEASE,
                "enabled": True,
                "compiled_manifest": _manifest(),
            },
            register_callback=lambda **_values: "https://app.example.test/oauth/callback",
            secret_present=lambda **values: (
                model_provider_store.get_secret_ref_ciphertext(authorization_conn, **values)
                is not None
            ),
            encrypt_verifier=lambda value: (
                f"encrypted:{hashlib.sha256(value.encode()).hexdigest()}"
            ),
            transaction_factory=lambda: authorization_conn.transaction(),
            state_token_factory=lambda: "s" * 48,
            verifier_factory=lambda: "v" * 64,
            now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
        )
        _raises_input_2230_1 = _operator_scope()
        _raises_input_2230_2 = OAuthAuthorizationStart(
            configuration_id=UUID(configuration_id),
            connection_type="example_mail",
            intent_id="connect",
            display_name="Reconnect while disconnecting",
            return_page_id="mail",
            existing_connection_id=connection_id,
        )
        with pytest.raises(ServicePreconditionError) as exc_info:
            authorization_service.start_authorization(
                _raises_input_2230_1, INSTALL, PROVIDER, _raises_input_2230_2
            )
        assert exc_info.value.payload == {"error": "oauth_connection_disconnect_in_progress"}
        with authorization_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM extension_oauth_authorization_states WHERE existing_connection_id = %s AND consumed_at IS NULL",
                (connection_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        release_revoke.set()
        thread.join(timeout=5)
        disconnect_conn.close()
        authorization_conn.close()
    assert disconnect_result["status"] == "disconnected"


@pytest.mark.integration
def test_global_callback_cleanup_removes_target_oauth_and_hmac_secrets_only(pg_conn) -> None:
    consumer_account = uuid4()
    consumer_workspace = uuid4()
    consumer_project = uuid4()
    survivor_account = uuid4()
    survivor_workspace = uuid4()
    survivor_project = uuid4()
    target_install = f"install-callback-target-{uuid4().hex}"
    survivor_install = f"install-callback-survivor-{uuid4().hex}"
    target_release = f"release-callback-{uuid4().hex}"
    target_connection = f"conn_callback_target_{uuid4().hex}"
    survivor_connection = f"conn_callback_survivor_{uuid4().hex}"
    oauth_registration = uuid4()
    hmac_registration = uuid4()
    target_connection_registration = uuid4()
    survivor_oauth_registration = uuid4()
    survivor_connection_registration = uuid4()
    foreign_registration = uuid4()
    target_extension = f"fixture.callback-cleanup-{uuid4().hex}"
    foreign_extension = f"fixture.callback-foreign-{uuid4().hex}"
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (id, tenant_id, account_key, display_name) VALUES (%s, %s, %s, 'Consumer')",
                (consumer_account, TENANT, f"consumer-{uuid4().hex}"),
            )
            cur.execute(
                "INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name) VALUES (%s, %s, %s, %s, 'Consumer')",
                (consumer_workspace, TENANT, consumer_account, f"consumer-{uuid4().hex}"),
            )
            cur.execute(
                "INSERT INTO projects (id, tenant_id, account_id, workspace_id, name) VALUES (%s, %s, %s, %s, 'Consumer')",
                (consumer_project, TENANT, consumer_account, consumer_workspace),
            )
            cur.execute(
                "INSERT INTO accounts (id, tenant_id, account_key, display_name) VALUES (%s, %s, %s, 'Survivor')",
                (survivor_account, TENANT, f"survivor-{uuid4().hex}"),
            )
            cur.execute(
                "INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name) VALUES (%s, %s, %s, %s, 'Survivor')",
                (survivor_workspace, TENANT, survivor_account, f"survivor-{uuid4().hex}"),
            )
            cur.execute(
                "INSERT INTO projects (id, tenant_id, account_id, workspace_id, name) VALUES (%s, %s, %s, %s, 'Survivor')",
                (survivor_project, TENANT, survivor_account, survivor_workspace),
            )
            cur.execute(
                "INSERT INTO extension_releases (release_id, extension_id, release_version, descriptor_sha256) VALUES (%s, %s, '1.0.0', repeat('c', 64))",
                (target_release, target_extension),
            )
            cur.execute(
                "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id, visibility) VALUES (%s, %s, %s, %s, 'global'), (%s, %s, %s, %s, 'account')",
                (
                    target_extension,
                    ACCOUNT,
                    target_install,
                    target_release,
                    target_extension,
                    survivor_account,
                    survivor_install,
                    target_release,
                ),
            )
            cur.execute(
                "INSERT INTO project_connections (connection_id, project_id, extension_id, account_id, extension_install_id, display_name, connection_type) VALUES (%s, %s, %s, %s, %s, 'Target global connection', 'callback'), (%s, %s, %s, %s, %s, 'Surviving install connection', 'callback')",
                (
                    target_connection,
                    survivor_project,
                    target_extension,
                    survivor_account,
                    target_install,
                    survivor_connection,
                    survivor_project,
                    target_extension,
                    survivor_account,
                    survivor_install,
                ),
            )
            registrations = (
                (
                    oauth_registration,
                    target_extension,
                    "oauth_authorization",
                    "oauth-hash-" + uuid4().hex,
                    consumer_project,
                    "none",
                    None,
                ),
                (
                    hmac_registration,
                    target_extension,
                    "chat_update",
                    "hmac-hash-" + uuid4().hex,
                    PROJECT,
                    "hmac_sha256",
                    None,
                ),
                (
                    target_connection_registration,
                    target_extension,
                    "target_connection_callback",
                    "target-connection-hash-" + uuid4().hex,
                    survivor_project,
                    "hmac_sha256",
                    target_connection,
                ),
                (
                    survivor_oauth_registration,
                    target_extension,
                    "survivor_oauth",
                    "survivor-oauth-hash-" + uuid4().hex,
                    survivor_project,
                    "none",
                    None,
                ),
                (
                    survivor_connection_registration,
                    target_extension,
                    "survivor_connection_callback",
                    "survivor-connection-hash-" + uuid4().hex,
                    survivor_project,
                    "hmac_sha256",
                    survivor_connection,
                ),
                (
                    foreign_registration,
                    foreign_extension,
                    "chat_update",
                    "foreign-hash-" + uuid4().hex,
                    PROJECT,
                    "hmac_sha256",
                    None,
                ),
            )
            for registration in registrations:
                cur.execute(
                    "INSERT INTO extension_callback_registrations (id, extension_id, callback_id, callback_token_hash, project_id, auth_type, connection_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    registration,
                )
            secrets = (
                (
                    consumer_project,
                    f"extension_callback:{oauth_registration}:capability_token",
                ),
                (PROJECT, f"extension_callback:{hmac_registration}:shared_secret"),
                (
                    survivor_project,
                    f"extension_callback:{target_connection_registration}:shared_secret",
                ),
                (
                    survivor_project,
                    f"extension_callback:{survivor_oauth_registration}:capability_token",
                ),
                (
                    survivor_project,
                    f"extension_callback:{survivor_connection_registration}:shared_secret",
                ),
                (PROJECT, f"extension_callback:{foreign_registration}:shared_secret"),
            )
            for project_id, secret_key in secrets:
                cur.execute(
                    "INSERT INTO project_secrets (project_id, secret_key, secret_value_ciphertext) VALUES (%s, %s, 'ciphertext')",
                    (project_id, secret_key),
                )

            context = SimpleNamespace(
                bl=SimpleNamespace(
                    _table_exists=lambda table: (
                        table in {"extension_callback_registrations", "project_secrets"}
                    )
                ),
                extension_id=target_extension,
                account_id=str(ACCOUNT),
            )
            inventory: list[dict[str, Any]] = []
            _collect_callbacks(
                bl=context.bl,
                cur=cur,
                extension_id=target_extension,
                install_id=target_install,
                account_id=str(ACCOUNT),
                include_consumer_accounts=True,
                remove=inventory,
            )
            assert {row["id"] for row in inventory} == {
                str(oauth_registration),
                str(hmac_registration),
                str(target_connection_registration),
            }
            ExtensionUninstallCleanupQueries()._cleanup_callbacks(
                context,
                SimpleNamespace(include_consumer_accounts=True),
                RemovalTransactionState(target_install, target_release, True, False),
                cur,
                [],
            )
            cur.execute(
                "SELECT id FROM extension_callback_registrations WHERE id = ANY(%s::uuid[]) ORDER BY id",
                (
                    [
                        oauth_registration,
                        hmac_registration,
                        target_connection_registration,
                        survivor_oauth_registration,
                        survivor_connection_registration,
                        foreign_registration,
                    ],
                ),
            )
            assert {row[0] for row in cur.fetchall()} == {
                survivor_oauth_registration,
                survivor_connection_registration,
                foreign_registration,
            }
            cur.execute(
                "SELECT secret_key FROM project_secrets WHERE secret_key = ANY(%s) ORDER BY secret_key",
                ([secret_key for _project_id, secret_key in secrets],),
            )
            assert {row[0] for row in cur.fetchall()} == {
                f"extension_callback:{survivor_oauth_registration}:capability_token",
                f"extension_callback:{survivor_connection_registration}:shared_secret",
                f"extension_callback:{foreign_registration}:shared_secret",
            }
    finally:
        pg_conn.rollback()


@pytest.mark.integration
def test_configuration_cleanup_removes_orphan_owned_prefix_and_preserves_foreign(pg_conn) -> None:
    seeded = _seed_flow(pg_conn, opaque_state=f"opaque-orphan-{uuid4().hex}")
    configuration_id = str(seeded["configuration_id"])
    orphan_ref = f"extension_oauth_configuration:{configuration_id}:orphaned_secret"
    foreign_ref = f"foreign:{configuration_id}:must-survive"
    model_provider_store.upsert_secret_ref(
        pg_conn, account_id=str(ACCOUNT), ref_key=orphan_ref, encrypted_value="orphan"
    )
    model_provider_store.upsert_secret_ref(
        pg_conn, account_id=str(ACCOUNT), ref_key=foreign_ref, encrypted_value="foreign"
    )
    context = SimpleNamespace(
        bl=SimpleNamespace(
            _table_exists=lambda table: (
                table
                in {
                    "extension_oauth_configurations",
                    "extension_oauth_authorization_states",
                    "secret_refs",
                }
            )
        ),
        account_id=str(ACCOUNT),
        extension_id=EXTENSION,
    )
    try:
        with pg_conn.transaction(), pg_conn.cursor() as cur:
            cleanup_extension_oauth_configurations(
                context,
                SimpleNamespace(include_consumer_accounts=False),
                RemovalTransactionState(INSTALL, RELEASE, False, False),
                cur,
                [],
                record_removed=lambda *_args, **_kwargs: None,
            )
            cur.execute(
                "SELECT ref_key FROM secret_refs WHERE account_id = %s AND ref_key = ANY(%s) ORDER BY ref_key",
                (ACCOUNT, [orphan_ref, foreign_ref]),
            )
            assert cur.fetchall() == [(foreign_ref,)]
    finally:
        pg_conn.rollback()


@pytest.mark.integration
def test_uninstall_lifecycle_audit_survives_without_extension_metadata(
    pg_conn, oauth_callback_database
) -> None:
    extension_id = f"fixture.uninstalled-{uuid4().hex}"
    install_id = f"install-uninstalled-{uuid4().hex}"
    logger = PlatformLogger(oauth_callback_database.dsn)
    try:
        record_extension_oauth_uninstall_audit(
            logger,
            extension_id=extension_id,
            account_id=str(ACCOUNT),
            install_id=install_id,
            plan_revision="final-revision",
            actor_type="operator",
            actor_id="operator-1",
            outcome="not_available",
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT target_jsonb, payload_jsonb, result_jsonb, status FROM command_audit_log WHERE command_type = 'extension_oauth_uninstall_lifecycle' AND target_jsonb->>'install_id' = %s",
                (install_id,),
            )
            assert cur.fetchone() == (
                {"extension_id": extension_id, "install_id": install_id},
                {},
                {"remote_revocation": "not_available"},
                "executed",
            )
            cur.execute(
                "DELETE FROM command_audit_log WHERE command_type = 'extension_oauth_uninstall_lifecycle' AND target_jsonb->>'install_id' = %s",
                (install_id,),
            )
        pg_conn.commit()
    finally:
        logger.conn.close()


def _seed_uninstall_audit_candidate(pg_conn, *, suffix: str) -> dict[str, Any]:
    extension_id = f"fixture.oauth-uninstall-audit-{suffix}"
    install_id = f"install-oauth-uninstall-audit-{suffix}"
    release_id = f"release-oauth-uninstall-audit-{suffix}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_bundles (extension_key, kind, display_name, bundle_root, manifest_path, latest_version, status, account_id) VALUES (%s, 'tool_provider', %s, %s, %s, '1.0.0', 'valid', %s) RETURNING id",
            (
                extension_id,
                extension_id,
                f"/tmp/{extension_id}",
                f"/tmp/{extension_id}/extension.yaml",
                ACCOUNT,
            ),
        )
        bundle_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO extension_versions (bundle_id, version, manifest_jsonb, compiled_manifest_jsonb) VALUES (%s, '1.0.0', '{}'::jsonb, '{}'::jsonb) RETURNING id",
            (bundle_id,),
        )
        version_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO enabled_extensions (bundle_id, version_id, account_id) VALUES (%s, %s, %s)",
            (bundle_id, version_id, ACCOUNT),
        )
        cur.execute(
            "INSERT INTO extension_releases (release_id, extension_id, release_version, account_id, descriptor_sha256) VALUES (%s, %s, '1.0.0', %s, %s)",
            (release_id, extension_id, ACCOUNT, "a" * 64),
        )
        cur.execute(
            "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id) VALUES (%s, %s, %s, %s)",
            (extension_id, ACCOUNT, install_id, release_id),
        )
        cur.execute(
            "INSERT INTO extension_invocation_logs (bundle_id, version_id, invocation_type, status, request_jsonb, response_jsonb, account_id) VALUES (%s, %s, 'oauth_lifecycle', 'ok', %s::jsonb, %s::jsonb, %s)",
            (
                bundle_id,
                version_id,
                '{"token":"raw-token-canary","provider_body":"raw-provider-canary"}',
                '{"provider_error":"raw-error-canary"}',
                ACCOUNT,
            ),
        )
    pg_conn.commit()
    return {
        "extension_id": extension_id,
        "install_id": install_id,
        "release_id": release_id,
        "bundle_id": bundle_id,
        "version_id": version_id,
    }


def _persistence_service_for_audit(
    logger: Any, candidate: dict[str, Any], *, owns_oauth: bool
) -> tuple[ExtensionUninstallPersistenceService, UninstallContext, UninstallTarget]:
    service = ExtensionUninstallPersistenceService(
        context=ExtensionLifecycleContext(),
        cleanup=MagicMock(),
        managed_release=MagicMock(),
        plan=MagicMock(),
    )
    service._generic_oauth = MagicMock()
    service._generic_oauth.prepare.return_value = PreparedOAuthUninstall((), owns_oauth)
    service._generic_oauth.execute.return_value = "not_available"
    state = RemovalTransactionState(candidate["install_id"], candidate["release_id"], True, False)
    service._prepare_removal_transaction = MagicMock(return_value=state)
    real_queries = ExtensionUninstallCleanupQueries()
    service._queries = MagicMock()
    service._queries.cleanup_runtime_records.side_effect = real_queries.cleanup_runtime_records
    resolved_scope = ScopeContext(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        actor_id="operator-1",
        principal_type="operator",
    )
    context = UninstallContext(
        extension_id=candidate["extension_id"],
        scope=resolved_scope,
        resolved_scope=resolved_scope,
        plan_revision=f"revision-{candidate['install_id']}",
        account_id=str(ACCOUNT),
        bl=logger,
        runtime_store=MagicMock(),
        confirmed_plan={
            "install_id": candidate["install_id"],
            "release_id": candidate["release_id"],
        },
    )
    target = UninstallTarget(
        extension={"extension_key": candidate["extension_id"]},
        install_id=candidate["install_id"],
        release_id=candidate["release_id"],
        include_consumer_accounts=False,
    )
    return service, context, target


def _cleanup_uninstall_audit_candidate(pg_conn, candidate: dict[str, Any]) -> None:
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM command_audit_log WHERE command_type = 'extension_oauth_uninstall_lifecycle' AND target_jsonb->>'install_id' = %s",
            (candidate["install_id"],),
        )
        cur.execute(
            "DELETE FROM extension_invocation_logs WHERE bundle_id = %s", (candidate["bundle_id"],)
        )
        cur.execute(
            "DELETE FROM enabled_extensions WHERE bundle_id = %s", (candidate["bundle_id"],)
        )
        cur.execute(
            "DELETE FROM extension_installs WHERE install_id = %s", (candidate["install_id"],)
        )
        cur.execute(
            "DELETE FROM extension_releases WHERE release_id = %s", (candidate["release_id"],)
        )
        cur.execute(
            "DELETE FROM extension_versions WHERE bundle_id = %s", (candidate["bundle_id"],)
        )
        cur.execute("DELETE FROM extension_bundles WHERE id = %s", (candidate["bundle_id"],))
    pg_conn.commit()


@pytest.mark.integration
def test_persistence_uninstall_commits_last_install_cleanup_and_durable_audit_atomically(
    pg_conn, oauth_callback_database
) -> None:
    candidate = _seed_uninstall_audit_candidate(pg_conn, suffix=uuid4().hex)
    logger = PlatformLogger(oauth_callback_database.dsn)
    service, context, target = _persistence_service_for_audit(logger, candidate, owns_oauth=True)
    try:
        _intent, removed_data, _state = service.remove_installed_target(context, target)
        assert "oauth_remote_revocation_not_available" in removed_data
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT target_jsonb, payload_jsonb, result_jsonb, status FROM command_audit_log WHERE command_type = 'extension_oauth_uninstall_lifecycle' AND target_jsonb->>'install_id' = %s",
                (candidate["install_id"],),
            )
            durable = cur.fetchone()
            assert durable == (
                {
                    "extension_id": candidate["extension_id"],
                    "install_id": candidate["install_id"],
                },
                {},
                {"remote_revocation": "not_available"},
                "executed",
            )
            assert "raw-token-canary" not in repr(durable)
            assert "raw-provider-canary" not in repr(durable)
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM extension_installs WHERE install_id = %s), EXISTS (SELECT 1 FROM extension_releases WHERE release_id = %s), EXISTS (SELECT 1 FROM extension_bundles WHERE id = %s), EXISTS (SELECT 1 FROM extension_invocation_logs WHERE bundle_id = %s)",
                (
                    candidate["install_id"],
                    candidate["release_id"],
                    candidate["bundle_id"],
                    candidate["bundle_id"],
                ),
            )
            assert cur.fetchone() == (False, False, False, False)
    finally:
        _cleanup_uninstall_audit_candidate(pg_conn, candidate)
        logger.conn.close()


@pytest.mark.integration
def test_persistence_uninstall_rolls_back_cleanup_when_durable_audit_fails(
    pg_conn, oauth_callback_database
) -> None:
    candidate = _seed_uninstall_audit_candidate(pg_conn, suffix=uuid4().hex)
    logger = PlatformLogger(oauth_callback_database.dsn)
    service, context, target = _persistence_service_for_audit(logger, candidate, owns_oauth=True)
    try:
        with (
            patch.object(logger, "log_command_execution", return_value=None),
            pytest.raises(RuntimeError, match="Durable lifecycle audit storage"),
        ):
            service.remove_installed_target(context, target)
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM extension_installs WHERE install_id = %s), EXISTS (SELECT 1 FROM extension_releases WHERE release_id = %s), EXISTS (SELECT 1 FROM extension_bundles WHERE id = %s), EXISTS (SELECT 1 FROM extension_invocation_logs WHERE bundle_id = %s)",
                (
                    candidate["install_id"],
                    candidate["release_id"],
                    candidate["bundle_id"],
                    candidate["bundle_id"],
                ),
            )
            assert cur.fetchone() == (True, True, True, True)
    finally:
        _cleanup_uninstall_audit_candidate(pg_conn, candidate)
        logger.conn.close()


@pytest.mark.integration
def test_persistence_non_oauth_uninstall_emits_no_oauth_audit_or_marker(
    pg_conn, oauth_callback_database
) -> None:
    candidate = _seed_uninstall_audit_candidate(pg_conn, suffix=uuid4().hex)
    logger = PlatformLogger(oauth_callback_database.dsn)
    service, context, target = _persistence_service_for_audit(logger, candidate, owns_oauth=False)
    try:
        _intent, removed_data, _state = service.remove_installed_target(context, target)
        assert not any(value.startswith("oauth_remote_revocation_") for value in removed_data)
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM command_audit_log WHERE command_type = 'extension_oauth_uninstall_lifecycle' AND target_jsonb->>'install_id' = %s",
                (candidate["install_id"],),
            )
            assert cur.fetchone() is None
    finally:
        _cleanup_uninstall_audit_candidate(pg_conn, candidate)
        logger.conn.close()


@pytest.mark.integration
def test_disable_consumes_pending_flow_and_retains_configuration_connection_and_tokens(
    pg_conn, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    connection_id = f"conn_callback_disable_{uuid4().hex}"
    seeded = _seed_flow(
        pg_conn,
        opaque_state=f"opaque-disable-{uuid4().hex}",
        reconnect=connection_id,
    )
    with pg_conn.transaction(), pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_installs SET state = 'disabled' WHERE install_id = %s AND account_id = %s AND extension_id = %s",
            (INSTALL, ACCOUNT, EXTENSION),
        )
        assert (
            invalidate_install_oauth_on_disable(
                pg_conn,
                install_id=INSTALL,
                extension_id=EXTENSION,
                account_id=str(ACCOUNT),
            )
            == 1
        )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT credential_revision, secret_refs FROM project_connections WHERE connection_id = %s",
            (connection_id,),
        )
        revision, secret_refs = cur.fetchone()
        assert revision == 8
        assert secret_refs["refs"]["_oauth_refresh_token"]
        cur.execute(
            "SELECT 1 FROM extension_oauth_configurations WHERE configuration_id = %s",
            (seeded["configuration_id"],),
        )
        assert cur.fetchone() is not None
        cur.execute(
            "SELECT consumed_at IS NOT NULL FROM extension_oauth_authorization_states WHERE existing_connection_id = %s",
            (connection_id,),
        )
        assert cur.fetchone()[0] is True


@pytest.mark.integration
def test_disable_during_exchange_wins_before_callback_materialization(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = f"opaque-disable-exchange-race-{uuid4().hex}"
    connection_id = f"conn_callback_disable_exchange_{uuid4().hex}"
    _seed_flow(pg_conn, opaque_state=opaque_state)
    callback_conn = psycopg.connect(oauth_callback_database.dsn)
    disable_conn = psycopg.connect(oauth_callback_database.dsn)
    exchange_entered = threading.Event()
    release_exchange = threading.Event()
    post_connect_calls: list[dict[str, Any]] = []
    results: list[Any] = []
    failures: list[BaseException] = []

    def exchange(**_values: Any) -> dict[str, Any]:
        assert callback_conn.info.transaction_status is TransactionStatus.IDLE
        exchange_entered.set()
        assert release_exchange.wait(timeout=10)
        return {
            "access_token": "must-not-materialize",
            "refresh_token": "must-not-materialize-refresh",
            "token_type": "Bearer",
            "scope": "mail.read mail.write",
        }

    def run_callback() -> None:
        try:
            results.append(
                _handle(
                    _service(
                        callback_conn,
                        exchange=exchange,
                        connection_id=connection_id,
                        post_connect=lambda **values: (
                            post_connect_calls.append(copy.deepcopy(values)) or {}
                        ),
                    ),
                    opaque_state,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    callback_thread = threading.Thread(target=run_callback, daemon=True)
    try:
        callback_thread.start()
        assert exchange_entered.wait(timeout=10)
        with disable_conn.transaction(), disable_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET state = 'disabled' "
                "WHERE install_id = %s AND account_id = %s AND extension_id = %s",
                (INSTALL, ACCOUNT, EXTENSION),
            )
            invalidate_install_oauth_on_disable(
                disable_conn,
                install_id=INSTALL,
                extension_id=EXTENSION,
                account_id=str(ACCOUNT),
            )
        release_exchange.set()
        callback_thread.join(timeout=10)
        assert not callback_thread.is_alive()
        assert failures == []
        assert [result.outcome for result in results] == ["restart_required"]
        assert post_connect_calls == []
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            assert cur.fetchone() is None
            cur.execute(
                "SELECT 1 FROM secret_refs WHERE account_id = %s AND ref_key LIKE %s",
                (ACCOUNT, f"project_connection:{connection_id}:%"),
            )
            assert cur.fetchone() is None
    finally:
        release_exchange.set()
        callback_thread.join(timeout=10)
        callback_conn.close()
        disable_conn.close()


@pytest.mark.integration
def test_disable_after_grant_commit_wins_before_post_connect_dispatch(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = f"opaque-disable-dispatch-race-{uuid4().hex}"
    connection_id = f"conn_callback_disable_dispatch_{uuid4().hex}"
    _seed_flow(pg_conn, opaque_state=opaque_state)
    callback_conn = psycopg.connect(oauth_callback_database.dsn)
    disable_conn = psycopg.connect(oauth_callback_database.dsn)
    grant_committed = threading.Event()
    release_dispatch = threading.Event()
    post_connect_calls: list[dict[str, Any]] = []
    results: list[Any] = []
    failures: list[BaseException] = []

    @contextmanager
    def controlled_transaction() -> Iterator[Any]:
        with callback_conn.transaction():
            yield
        with (
            psycopg.connect(oauth_callback_database.dsn) as observer,
            observer.cursor() as cur,
        ):
            cur.execute(
                "SELECT 1 FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            grant_is_committed = cur.fetchone() is not None
        if grant_is_committed:
            grant_committed.set()
            assert release_dispatch.wait(timeout=10)

    def run_callback() -> None:
        try:
            results.append(
                _handle(
                    _service(
                        callback_conn,
                        exchange=lambda **_values: {
                            "access_token": "committed-before-disable",
                            "refresh_token": "committed-refresh-before-disable",
                            "token_type": "Bearer",
                            "scope": "mail.read mail.write",
                        },
                        connection_id=connection_id,
                        post_connect=lambda **values: (
                            post_connect_calls.append(copy.deepcopy(values)) or {}
                        ),
                        transaction_factory=controlled_transaction,
                    ),
                    opaque_state,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    callback_thread = threading.Thread(target=run_callback, daemon=True)
    try:
        callback_thread.start()
        assert grant_committed.wait(timeout=10)
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT credential_revision, secret_refs FROM project_connections "
                "WHERE connection_id = %s",
                (connection_id,),
            )
            committed_revision, committed_refs = cur.fetchone()
        assert committed_revision == 1
        committed_secrets = project_connection_secrets.resolve_connection_secrets(
            pg_conn,
            account_id=str(ACCOUNT),
            secret_refs=committed_refs,
            include_internal=True,
        )
        assert committed_secrets["_oauth_access_token"] == "committed-before-disable"
        with disable_conn.transaction(), disable_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET state = 'disabled' "
                "WHERE install_id = %s AND account_id = %s AND extension_id = %s",
                (INSTALL, ACCOUNT, EXTENSION),
            )
            assert (
                invalidate_install_oauth_on_disable(
                    disable_conn,
                    install_id=INSTALL,
                    extension_id=EXTENSION,
                    account_id=str(ACCOUNT),
                )
                == 1
            )
        release_dispatch.set()
        callback_thread.join(timeout=10)
        assert not callback_thread.is_alive()
        assert failures == []
        assert [result.outcome for result in results] == ["restart_required"]
        assert post_connect_calls == []
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT credential_revision, secret_refs FROM project_connections "
                "WHERE connection_id = %s",
                (connection_id,),
            )
            revision, secret_refs = cur.fetchone()
        assert revision == 2
        resolved = project_connection_secrets.resolve_connection_secrets(
            pg_conn,
            account_id=str(ACCOUNT),
            secret_refs=secret_refs,
            include_internal=True,
        )
        assert resolved["_oauth_access_token"] == "committed-before-disable"
    finally:
        release_dispatch.set()
        callback_thread.join(timeout=10)
        callback_conn.close()
        disable_conn.close()


@pytest.mark.integration
def test_post_connect_barrier_blocks_disable_until_dispatch_finishes(
    pg_conn, oauth_callback_database, monkeypatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "callback-integration-key")
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network used"))
    opaque_state = f"opaque-callback-first-dispatch-race-{uuid4().hex}"
    connection_id = f"conn_callback_first_dispatch_{uuid4().hex}"
    _seed_flow(pg_conn, opaque_state=opaque_state)
    callback_conn = psycopg.connect(oauth_callback_database.dsn)
    disable_conn = psycopg.connect(oauth_callback_database.dsn)
    post_connect_entered = threading.Event()
    release_post_connect = threading.Event()
    order: list[str] = []
    captured: list[dict[str, Any]] = []
    results: list[Any] = []
    failures: list[BaseException] = []

    def post_connect(**values: Any) -> dict[str, Any]:
        captured.append(copy.deepcopy(values))
        order.append("post_connect_entered")
        post_connect_entered.set()
        assert release_post_connect.wait(timeout=10)
        order.append("post_connect_released")
        return {
            "status": "succeeded",
            "response": {"result": {"subject": "callback-first", "health": "ok"}},
        }

    def run_callback() -> None:
        try:
            result = _handle(
                _service(
                    callback_conn,
                    exchange=lambda **_values: {
                        "access_token": "callback-first-access",
                        "refresh_token": "callback-first-refresh",
                        "token_type": "Bearer",
                        "scope": "mail.read mail.write",
                    },
                    connection_id=connection_id,
                    post_connect=post_connect,
                ),
                opaque_state,
            )
            results.append(result)
            order.append("callback_completed")
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    callback_thread = threading.Thread(target=run_callback, daemon=True)
    try:
        callback_thread.start()
        assert post_connect_entered.wait(timeout=10)

        def _raises_operation_3071():
            cur.execute("SET LOCAL lock_timeout = '250ms'")
            cur.execute(
                "UPDATE extension_installs SET state = 'disabled' "
                "WHERE install_id = %s AND account_id = %s AND extension_id = %s",
                (INSTALL, ACCOUNT, EXTENSION),
            )

        with (
            pytest.raises(psycopg.errors.LockNotAvailable),
            disable_conn.transaction(),
            disable_conn.cursor() as cur,
        ):
            _raises_operation_3071()
        assert callback_thread.is_alive()
        assert order == ["post_connect_entered"]

        release_post_connect.set()
        callback_thread.join(timeout=10)
        assert not callback_thread.is_alive()
        assert failures == []
        assert [result.outcome for result in results] == ["connected"]
        assert order == [
            "post_connect_entered",
            "post_connect_released",
            "callback_completed",
        ]
        assert captured[0]["target"]["connection"]["secrets"] == {
            "access_token": "callback-first-access"
        }

        with disable_conn.transaction(), disable_conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_installs SET state = 'disabled' "
                "WHERE install_id = %s AND account_id = %s AND extension_id = %s",
                (INSTALL, ACCOUNT, EXTENSION),
            )
            assert (
                invalidate_install_oauth_on_disable(
                    disable_conn,
                    install_id=INSTALL,
                    extension_id=EXTENSION,
                    account_id=str(ACCOUNT),
                )
                == 1
            )
        order.append("disable_committed")

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM extension_installs "
                "WHERE install_id = %s AND account_id = %s AND extension_id = %s",
                (INSTALL, ACCOUNT, EXTENSION),
            )
            assert cur.fetchone()[0] == "disabled"
            cur.execute(
                "SELECT credential_revision FROM project_connections WHERE connection_id = %s",
                (connection_id,),
            )
            assert cur.fetchone()[0] == 3
        assert order[-1] == "disable_committed"
    finally:
        release_post_connect.set()
        callback_thread.join(timeout=10)
        callback_conn.close()
        disable_conn.close()


@pytest.mark.integration
def test_disable_invalidation_is_exact_install_scoped_across_two_installs(pg_conn) -> None:
    first_connection = f"conn_callback_disable_exact_{uuid4().hex}"
    _seed_flow(
        pg_conn,
        opaque_state=f"opaque-disable-exact-{uuid4().hex}",
        reconnect=first_connection,
    )
    second_account = uuid4()
    second_workspace = uuid4()
    second_project = uuid4()
    second_install = f"install-disable-exact-{uuid4().hex}"
    second_configuration = uuid4()
    second_connection = f"conn_callback_disable_other_{uuid4().hex}"
    provider = compile_oauth_providers(_manifest())[0]
    try:
        with pg_conn.transaction(), pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (id, tenant_id, account_key, display_name) VALUES (%s, %s, %s, 'Second OAuth')",
                (second_account, TENANT, f"second-oauth-{uuid4().hex}"),
            )
            cur.execute(
                "INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name) VALUES (%s, %s, %s, %s, 'Second OAuth')",
                (second_workspace, TENANT, second_account, f"second-oauth-{uuid4().hex}"),
            )
            cur.execute(
                "INSERT INTO projects (id, tenant_id, account_id, workspace_id, name) VALUES (%s, %s, %s, %s, 'Second OAuth')",
                (second_project, TENANT, second_account, second_workspace),
            )
            cur.execute(
                "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id, state) VALUES (%s, %s, %s, %s, 'disabled')",
                (EXTENSION, second_account, second_install, RELEASE),
            )
            cur.execute(
                "UPDATE extension_installs SET state = 'disabled' WHERE extension_id = %s AND account_id = %s AND install_id = %s",
                (EXTENSION, ACCOUNT, INSTALL),
            )
        repository = ExtensionOAuthRepository(pg_conn)
        assert repository.create_configuration(
            configuration_id=second_configuration,
            tenant_id=TENANT,
            account_id=second_account,
            project_id=second_project,
            extension_install_id=second_install,
            extension_id=EXTENSION,
            extension_release_id=RELEASE,
            provider_id=PROVIDER,
            display_name="Second configuration",
            configuration_revision=1,
            authorization_revision=1,
            status="ready_for_authorization",
            non_secret_config={"client_id": "second-client"},
            secret_refs={"refs": {}},
            provider_security_fingerprint=provider_security_fingerprint(provider),
            provider_revocation_fingerprint=provider_revocation_fingerprint(provider),
            last_error={},
        )
        assert repository.create_oauth_connection(
            connection_id=second_connection,
            project_id=second_project,
            extension_id=EXTENSION,
            account_id=second_account,
            display_name="Second connection",
            connection_kind="extension",
            connection_type="example_mail",
            extension_install_id=second_install,
            oauth_configuration_id=second_configuration,
            oauth_provider_id=PROVIDER,
            oauth_extension_release_id=RELEASE,
            oauth_authorization_revision=1,
            oauth_requested_scopes=["mail.read"],
            oauth_granted_scopes=["mail.read"],
            oauth_provider_security_fingerprint=provider_security_fingerprint(provider),
            oauth_provider_revocation_fingerprint=provider_revocation_fingerprint(provider),
            health_status="healthy",
            runtime_status="ready",
            last_error="",
            mark_last_seen_ok=True,
            connection_config={"oauth": {"lifecycle_status": "connected"}},
            secret_refs={"refs": {}},
        )
        assert (
            invalidate_install_oauth_on_disable(
                pg_conn,
                install_id=INSTALL,
                extension_id=EXTENSION,
                account_id=str(ACCOUNT),
            )
            == 1
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT connection_id, credential_revision FROM project_connections WHERE connection_id = ANY(%s) ORDER BY connection_id",
                ([first_connection, second_connection],),
            )
            revisions = dict(cur.fetchall())
        assert revisions[first_connection] == 8
        assert revisions[second_connection] == 1
    finally:
        pg_conn.rollback()
