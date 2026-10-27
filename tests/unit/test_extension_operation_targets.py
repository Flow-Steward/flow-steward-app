from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from core.application.extensions import operation_targets
from core.application.management.errors import ServiceValidationError


class _DirectClaimConnection:
    def __init__(self) -> None:
        self.autocommit = False
        self.closed = False
        self._row = (True,)

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, _sql, _params) -> None:
        self._row = (True,)

    def fetchone(self):
        return self._row

    def close(self) -> None:
        self.closed = True


def _oauth_connection() -> dict:
    return {
        "connection_id": "fixture-oauth-1",
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": "fixture.extension-oauth",
        "connection_type": "fixture_oauth_account",
        "connection_config": {"oauth": {"lifecycle_status": "connected"}},
        "runtime_status": "ready",
        "enabled": True,
        "secret_refs": {"refs": {"access_token": "access-ref"}},
    }


def _extension_owned_oauth_connection() -> dict:
    return {
        "connection_id": "oauth-1",
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": "fixture.extension-oauth",
        "extension_install_id": "install-1",
        "connection_type": "example_mail",
        "oauth_configuration_id": "configuration-1",
        "oauth_provider_id": "example",
        "connection_config": {"oauth": {"lifecycle_status": "connected"}},
        "enabled": True,
        "secret_refs": {
            "refs": {
                "_oauth_access_token": "project_connection:oauth-1:_oauth_access_token",
                "_oauth_refresh_token": "project_connection:oauth-1:_oauth_refresh_token",
            }
        },
    }


def test_extension_owned_oauth_target_projects_only_runtime_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock()
    store.get_project_connection.return_value = _extension_owned_oauth_connection()
    runtime_calls: list[dict] = []

    def resolve_runtime(_store, *, connection, get_project_by_id=None):
        runtime_calls.append(connection)
        assert get_project_by_id is None
        return {"access_token": "runtime-access"}

    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        resolve_runtime,
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.resolve_connection_secrets",
        lambda *_args, **_kwargs: pytest.fail("generic secret resolver must not run"),
    )
    monkeypatch.setattr(
        "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime",
        lambda *_args, **_kwargs: pytest.fail("legacy OAuth resolver must not run"),
    )

    target = operation_targets._direct_action_connection_target(
        store,
        connection_id="oauth-1",
        account_id="account-1",
        project_id="project-1",
        extension_id="fixture.extension-oauth",
        extension_install_id="install-1",
    )

    assert runtime_calls == [_extension_owned_oauth_connection()]
    assert target["secrets"] == {"access_token": "runtime-access"}
    assert "secret_refs" not in target


def test_direct_extension_action_injects_only_selected_connections_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock()
    store.get_project_connection.return_value = _oauth_connection()
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.resolve_connection_secrets",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime",
        lambda _store, *, connection: {"access_token": f"token-for-{connection['connection_id']}"},
    )

    target = operation_targets._direct_action_connection_target(
        store,
        connection_id="fixture-oauth-1",
        account_id="account-1",
        project_id="project-1",
        extension_id="fixture.extension-oauth",
    )

    assert target["connection_id"] == "fixture-oauth-1"
    assert target["secrets"] == {"access_token": "token-for-fixture-oauth-1"}


def test_direct_extension_action_fails_closed_when_oauth_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock()
    store.get_project_connection.return_value = _oauth_connection()
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.resolve_connection_secrets",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    with pytest.raises(ServiceValidationError, match="connection_oauth_resolution_failed"):
        operation_targets._direct_action_connection_target(
            store,
            connection_id="fixture-oauth-1",
            account_id="account-1",
            project_id="project-1",
            extension_id="fixture.extension-oauth",
        )


def test_oauth_refresh_claim_uses_direct_connection_outside_size_one_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bl = MagicMock()
    bl.conn = object()
    bl.dsn = "postgresql://dedicated-claim"
    bl._pool = MagicMock()
    bl._pool.getconn.side_effect = AssertionError("application pool must not be borrowed")
    bl.get_project.return_value = {"tenant_id": "tenant-1"}
    claim_connection = _DirectClaimConnection()
    connect_calls: list[str] = []

    def connect(dsn: str):
        connect_calls.append(dsn)
        return claim_connection

    monkeypatch.setattr("core.infrastructure.db.psycopg_runtime.connect", connect)

    def resolve(service, **scope):
        with service._repository.oauth_refresh_claim(**scope):
            assert claim_connection.autocommit is True
        return {"access_token": "direct-claim-access"}

    monkeypatch.setattr(
        "core.application.extensions.oauth_runtime.OAuthRuntimeTokenService.resolve_runtime_access_token",
        resolve,
    )

    result = operation_targets._resolve_extension_owned_oauth_runtime(
        bl,
        connection=_extension_owned_oauth_connection(),
    )

    assert result == {"access_token": "direct-claim-access"}
    assert connect_calls == ["postgresql://dedicated-claim"]
    assert claim_connection.closed is True
    bl._pool.getconn.assert_not_called()


def test_oauth_runtime_transaction_factory_pins_a_pooled_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.db.db_pool import PooledConnectionProxy

    class _RawConnection:
        autocommit = True
        closed = 0

        def __init__(self) -> None:
            self.transactions = 0

        def get_transaction_status(self) -> int:
            return 0

        @contextmanager
        def transaction(self):
            self.transactions += 1
            yield self

    class _Pool:
        def __init__(self) -> None:
            self.connection = _RawConnection()
            self.get_calls = 0
            self.put_calls = 0

        def getconn(self):
            self.get_calls += 1
            return self.connection

        def putconn(self, connection, close=False) -> None:
            assert connection is self.connection
            assert close is False
            self.put_calls += 1

    pool = _Pool()
    bl = MagicMock()
    bl.conn = PooledConnectionProxy(pool)
    bl.dsn = "postgresql://dedicated-claim"
    bl.get_project.return_value = {"tenant_id": "tenant-1"}

    def resolve(service, **_scope):
        with service._transaction_factory():
            pass
        return {"access_token": "pooled-access"}

    monkeypatch.setattr(
        "core.application.extensions.oauth_runtime.OAuthRuntimeTokenService.resolve_runtime_access_token",
        resolve,
    )

    result = operation_targets._resolve_extension_owned_oauth_runtime(
        bl,
        connection=_extension_owned_oauth_connection(),
    )

    assert result == {"access_token": "pooled-access"}
    assert pool.get_calls == 1
    assert pool.put_calls == 1
    assert pool.connection.transactions == 1
