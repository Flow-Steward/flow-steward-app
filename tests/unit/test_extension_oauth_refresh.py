from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any

import pytest

from core.application.extensions.oauth_runtime import (
    OAuthRuntimeTokenError,
    OAuthRuntimeTokenService,
)
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
    OAuthProviderOperation,
)
from tests.unit.test_extension_oauth_runtime import _manifest, _Repository


class _RefreshRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self.connection["secret_refs"] = {
            "refs": {
                "_oauth_access_token": "project_connection:connection-a:_oauth_access_token",
                "_oauth_refresh_token": "project_connection:connection-a:_oauth_refresh_token",
                "_oauth_expires_at": "project_connection:connection-a:_oauth_expires_at",
                "api_key": "project_connection:connection-a:api_key",
            }
        }
        self.configuration["non_secret_config"] = {"client_id": "client-id"}
        self.configuration["secret_refs"] = {
            "refs": {
                "client_secret": ("extension_oauth_configuration:configuration-a:client_secret")
            }
        }
        self.refresh_updates: list[dict[str, Any]] = []
        self.failure_updates: list[dict[str, Any]] = []
        self.install_row: dict[str, Any] | None = {
            "install_id": "install-a",
            "account_id": "account-a",
            "extension_id": "fixture.extension-oauth",
            "release_id": "release-a",
            "state": "active",
        }
        self.refresh_claim = threading.Lock()
        self.claim_entries = 0

    @contextmanager
    def oauth_refresh_claim(self, **scope: Any):
        assert scope["connection_id"] == self.connection["connection_id"]
        with self.refresh_claim:
            self.claim_entries += 1
            yield

    def lock_oauth_install_for_update(self, **_scope: Any) -> dict[str, Any] | None:
        return copy.deepcopy(self.install_row)

    def update_oauth_refresh(self, **values: Any) -> dict[str, Any] | None:
        self.refresh_updates.append(copy.deepcopy(values))
        if values["expected_credential_revision"] != self.connection["credential_revision"]:
            return None
        self.connection["credential_revision"] += 1
        self.connection["secret_refs"] = copy.deepcopy(values["secret_refs"])
        self.connection["connection_config"] = copy.deepcopy(values["connection_config"])
        self.configuration["status"] = "verified"
        self.configuration["last_error"] = {}
        return copy.deepcopy(self.connection)

    def update_oauth_refresh_failure(self, **values: Any) -> dict[str, Any] | None:
        self.failure_updates.append(copy.deepcopy(values))
        if values["expected_credential_revision"] != self.connection["credential_revision"]:
            return None
        self.connection["credential_revision"] += 1
        self.connection["connection_config"] = copy.deepcopy(values["connection_config"])
        if values["mark_configuration"]:
            self.configuration["configuration_revision"] += 1
            self.configuration["status"] = "needs_attention"
            self.configuration["last_error"] = {"code": values["error_code"]}
        return copy.deepcopy(self.connection)


class _RefreshTokenStore:
    def __init__(self) -> None:
        self.snapshot = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 900.0,
        }
        self.writes: list[dict[str, Any]] = []

    def resolve_access_token(self, **_values: Any) -> str:
        return str(self.snapshot["access_token"])

    def resolve_token_snapshot(self, **_values: Any) -> dict[str, Any]:
        return copy.deepcopy(self.snapshot)

    def write_tokens(self, **values: Any) -> dict[str, Any]:
        self.writes.append(copy.deepcopy(values))
        refresh = values["token_response"].get("refresh_token") or values["existing_refresh_token"]
        self.snapshot = {
            "access_token": values["token_response"]["access_token"],
            "refresh_token": refresh,
            "expires_at": 1300.0,
        }
        connection_id = values["connection_id"]
        return {
            "refs": {
                "_oauth_access_token": f"project_connection:{connection_id}:_oauth_access_token",
                "_oauth_refresh_token": f"project_connection:{connection_id}:_oauth_refresh_token",
                "_oauth_expires_at": f"project_connection:{connection_id}:_oauth_expires_at",
            }
        }


def _service(repository, token_store, refresh, *, events):
    transaction_depth = [0]

    @contextmanager
    def transaction():
        transaction_depth[0] += 1
        events.append("tx_begin")
        try:
            yield
        finally:
            events.append("tx_end")
            transaction_depth[0] -= 1

    def refresh_outside_transaction(**values):
        assert transaction_depth[0] == 0
        events.append("refresh_http")
        return refresh(**values)

    return OAuthRuntimeTokenService(
        repository=repository,
        token_store=token_store,
        install_loader=lambda _install, _account: {
            "extension_id": "fixture.extension-oauth",
            "release_id": "release-a",
            "enabled": True,
            "compiled_manifest": _manifest(),
        },
        transaction_factory=transaction,
        configuration_secret_value=lambda **_values: "client-secret",
        refresh_access_token=refresh_outside_transaction,
        now=lambda: 1000.0,
        refresh_skew_seconds=60,
    )


def _resolve(service, *, connection_id="connection-a", recovery_purpose=None):
    return service.resolve_runtime_access_token(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        extension_install_id="install-a",
        extension_id="fixture.extension-oauth",
        provider_id="example",
        configuration_id="configuration-a",
        connection_id=connection_id,
        connection_type="example_mail",
        recovery_purpose=recovery_purpose,
    )


def test_unexpired_access_token_skips_refresh_with_expiry_skew() -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()
    token_store.snapshot["expires_at"] = 1061.0
    events: list[str] = []
    service = _service(
        repository,
        token_store,
        lambda **_values: pytest.fail("refresh must not run"),
        events=events,
    )

    assert _resolve(service) == {"access_token": "old-access"}
    assert events == ["tx_begin", "tx_end"]
    assert token_store.writes == []


def test_repaired_ready_configuration_forces_revalidation_and_returns_verified() -> None:
    repository = _RefreshRepository()
    repository.configuration["status"] = "ready_for_authorization"
    repository.configuration["last_error"] = {}
    repository.connection["connection_config"]["oauth"].update(
        lifecycle_status="needs_attention",
        last_error_code="invalid_client",
    )
    token_store = _RefreshTokenStore()
    token_store.snapshot["expires_at"] = 1400.0

    service = _service(
        repository,
        token_store,
        lambda **_values: {
            "access_token": "revalidated-access",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "mail.read mail.write",
        },
        events=[],
    )

    assert _resolve(service) == {"access_token": "revalidated-access"}
    assert repository.configuration["status"] == "verified"
    assert repository.configuration["last_error"] == {}
    assert repository.connection["connection_config"]["oauth"] == {
        "lifecycle_status": "connected",
        "last_error_code": None,
    }


def test_needs_attention_configuration_blocks_refresh_until_credentials_are_repaired() -> None:
    repository = _RefreshRepository()
    repository.configuration["status"] = "needs_attention"
    repository.configuration["last_error"] = {"code": "invalid_client"}
    repository.connection["connection_config"]["oauth"].update(
        lifecycle_status="needs_attention",
        last_error_code="invalid_client",
    )
    token_store = _RefreshTokenStore()
    service = _service(
        repository,
        token_store,
        lambda **_values: pytest.fail("known-bad credentials must not be retried"),
        events=[],
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_needs_attention"
    assert repository.refresh_updates == []


def test_post_connect_recovery_is_explicit_and_refreshes_the_exact_failed_grant() -> None:
    repository = _RefreshRepository()
    repository.configuration["status"] = "ready_for_authorization"
    repository.connection["connection_config"]["oauth"].update(
        lifecycle_status="needs_attention",
        last_error_code="provider_unavailable",
    )
    token_store = _RefreshTokenStore()
    events: list[str] = []
    service = _service(
        repository,
        token_store,
        lambda **_values: {
            "access_token": "recovered-access",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "mail.read mail.write",
        },
        events=events,
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)
    assert exc_info.value.code == "oauth_needs_attention"
    assert repository.refresh_updates == []

    assert _resolve(service, recovery_purpose="post_connect_retry") == {
        "access_token": "recovered-access"
    }
    assert repository.connection["credential_revision"] == 8
    assert repository.connection["connection_config"]["oauth"] == {
        "lifecycle_status": "connected",
        "last_error_code": None,
    }


def test_repaired_configuration_recovers_invalid_client_after_a_sibling_verifies_first() -> None:
    repository = _RefreshRepository()
    failed_connection = copy.deepcopy(repository.connection)
    failed_connection["connection_config"]["oauth"].update(
        lifecycle_status="needs_attention",
        last_error_code="invalid_client",
    )
    sibling = copy.deepcopy(repository.connection)
    sibling["connection_id"] = "connection-b"
    sibling["secret_refs"] = {
        "refs": {
            key: value.replace("connection-a", "connection-b")
            for key, value in sibling["secret_refs"]["refs"].items()
        }
    }
    repository.configuration["status"] = "ready_for_authorization"
    token_store = _RefreshTokenStore()
    token_store.snapshot["expires_at"] = 1400.0
    refresh_calls: list[str] = []
    service = _service(
        repository,
        token_store,
        lambda **_values: (
            refresh_calls.append(repository.connection["connection_id"])
            or {
                "access_token": f"access-{repository.connection['connection_id']}",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "mail.read mail.write",
            }
        ),
        events=[],
    )

    repository.connection = sibling
    assert _resolve(service, connection_id="connection-b") == {
        "access_token": "access-connection-b"
    }
    assert repository.configuration["status"] == "verified"

    repository.connection = failed_connection
    token_store.snapshot["expires_at"] = 1400.0
    assert _resolve(service) == {"access_token": "access-connection-a"}
    assert repository.connection["connection_config"]["oauth"] == {
        "lifecycle_status": "connected",
        "last_error_code": None,
    }
    assert refresh_calls == ["connection-b", "connection-a"]


@pytest.mark.parametrize("rotated_refresh", [None, "rotated-refresh"])
def test_expired_token_refreshes_outside_transaction_and_commits_atomically(
    rotated_refresh: str | None,
) -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()
    events: list[str] = []

    def refresh(**values):
        assert values["refresh_token"] == "old-refresh"
        assert values["client_id"] == "client-id"
        assert values["client_secret"] == "client-secret"
        response: dict[str, Any] = {
            "access_token": "new-access",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "mail.read mail.write",
        }
        if rotated_refresh is not None:
            response["refresh_token"] = rotated_refresh
        return response

    service = _service(repository, token_store, refresh, events=events)

    assert _resolve(service) == {"access_token": "new-access"}
    assert events == [
        "tx_begin",
        "tx_end",
        "tx_begin",
        "tx_end",
        "refresh_http",
        "tx_begin",
        "tx_end",
    ]
    assert token_store.writes[0]["existing_refresh_token"] == "old-refresh"
    expected_refresh = rotated_refresh or "old-refresh"
    assert token_store.snapshot["refresh_token"] == expected_refresh
    assert repository.refresh_updates[0]["expected_credential_revision"] == 7
    assert repository.refresh_updates[0]["secret_refs"]["refs"]["api_key"] == (
        "project_connection:connection-a:api_key"
    )


def test_stale_concurrent_refresh_response_cannot_overwrite_newer_tokens() -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()
    events: list[str] = []

    def refresh(**_values):
        repository.connection["credential_revision"] = 8
        token_store.snapshot.update(
            access_token="winner-access", refresh_token="winner-refresh", expires_at=1400.0
        )
        return {"access_token": "stale-access", "token_type": "Bearer", "expires_in": 300}

    service = _service(repository, token_store, refresh, events=events)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_runtime_blocked"
    assert token_store.writes == []
    assert token_store.snapshot["access_token"] == "winner-access"


def test_invalid_client_marks_only_exact_configuration_and_connection() -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()
    events: list[str] = []
    sibling = copy.deepcopy(repository.connection)
    sibling["connection_id"] = "connection-sibling"

    def refresh(**_values):
        raise OAuthProviderError(OAuthErrorCode.INVALID_CLIENT, OAuthProviderOperation.REFRESH)

    service = _service(repository, token_store, refresh, events=events)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_needs_attention"
    assert repository.failure_updates[0]["error_code"] == "invalid_client"
    assert repository.failure_updates[0]["connection_id"] == "connection-a"
    assert repository.configuration["status"] == "needs_attention"
    assert repository.connection["connection_config"]["oauth"]["lifecycle_status"] == (
        "needs_attention"
    )
    assert sibling["connection_config"]["oauth"]["lifecycle_status"] == "connected"
    assert "OAuthProviderError" not in repr(repository.failure_updates)


def test_transient_refresh_failure_retains_usable_credentials_without_state_mutation() -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()
    events: list[str] = []

    def refresh(**_values):
        raise OAuthProviderError(
            OAuthErrorCode.PROVIDER_UNAVAILABLE, OAuthProviderOperation.REFRESH
        )

    service = _service(repository, token_store, refresh, events=events)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_provider_unavailable"
    assert repository.refresh_updates == []
    assert repository.failure_updates == []
    assert token_store.snapshot["access_token"] == "old-access"
    assert token_store.snapshot["refresh_token"] == "old-refresh"


def test_refresh_scope_narrowing_requires_reauthorization_without_writing_tokens() -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()
    service = _service(
        repository,
        token_store,
        lambda **_values: {
            "access_token": "narrow-access",
            "token_type": "Bearer",
            "scope": "profile.read",
        },
        events=[],
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_authorization_required"
    assert repository.failure_updates[0]["error_code"] == "authorization_required"
    assert token_store.writes == []


def test_expired_token_without_refresh_grant_requires_authorization() -> None:
    repository = _RefreshRepository()
    repository.connection["secret_refs"]["refs"].pop("_oauth_refresh_token")
    token_store = _RefreshTokenStore()
    token_store.snapshot["refresh_token"] = ""
    service = _service(
        repository,
        token_store,
        lambda **_values: pytest.fail("refresh must not run"),
        events=[],
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_authorization_required"
    assert token_store.writes == []


@pytest.mark.parametrize("release_before_terminal", [False, True])
def test_concurrent_terminal_failure_cannot_beat_valid_rotated_refresh(
    release_before_terminal: bool,
) -> None:
    success_entered = threading.Event()
    release_success = threading.Event()
    terminal_claim_attempted = threading.Event()

    class ContendedRepository(_RefreshRepository):
        @contextmanager
        def oauth_refresh_claim(self, **scope: Any):
            if threading.current_thread().name == "terminal-refresh":
                terminal_claim_attempted.set()
            with super().oauth_refresh_claim(**scope):
                yield

    repository = ContendedRepository()
    token_store = _RefreshTokenStore()
    provider_calls: list[str] = []
    results: dict[str, str] = {}

    def successful_refresh(**_values: Any) -> dict[str, Any]:
        provider_calls.append("success")
        success_entered.set()
        assert release_success.wait(timeout=5)
        return {
            "access_token": "winning-access",
            "refresh_token": "winning-refresh",
            "token_type": "Bearer",
            "expires_in": 300,
        }

    def terminal_refresh(**_values: Any) -> dict[str, Any]:
        provider_calls.append("terminal")
        raise OAuthProviderError(OAuthErrorCode.INVALID_GRANT, OAuthProviderOperation.REFRESH)

    success_service = _service(repository, token_store, successful_refresh, events=[])
    terminal_service = _service(repository, token_store, terminal_refresh, events=[])

    def resolve(name: str, service: OAuthRuntimeTokenService) -> None:
        try:
            results[name] = _resolve(service)["access_token"]
        except OAuthRuntimeTokenError as exc:
            results[name] = exc.code

    success_thread = threading.Thread(target=resolve, args=("success", success_service))
    terminal_thread = threading.Thread(
        target=resolve, args=("terminal", terminal_service), name="terminal-refresh"
    )
    success_thread.start()
    try:
        assert success_entered.wait(timeout=5)
        if release_before_terminal:
            release_success.set()
            success_thread.join(timeout=5)
            assert not success_thread.is_alive()
        terminal_thread.start()
        if not release_before_terminal:
            # The fake transaction does not lock its rows like PostgreSQL FOR
            # UPDATE. Finish the contender's initial read before allowing the
            # winner to mutate those dictionaries, but keep the refresh claim
            # held so the contender must re-read the rotated token under it.
            assert terminal_claim_attempted.wait(timeout=5)
    finally:
        release_success.set()
        success_thread.join(timeout=5)
        if terminal_thread.ident is not None:
            terminal_thread.join(timeout=5)

    assert not success_thread.is_alive()
    assert not terminal_thread.is_alive()
    assert repository.claim_entries == (1 if release_before_terminal else 2)
    assert repository.failure_updates == []
    assert len(token_store.writes) == 1
    assert results == {"success": "winning-access", "terminal": "winning-access"}
    assert provider_calls == ["success"]
    assert repository.connection["connection_config"]["oauth"]["lifecycle_status"] == ("connected")
    assert token_store.snapshot["refresh_token"] == "winning-refresh"


def test_final_commit_rejects_stale_loader_after_install_is_disabled() -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()

    def refresh(**_values: Any) -> dict[str, Any]:
        assert repository.install_row is not None
        repository.install_row["state"] = "disabled"
        return {
            "access_token": "must-not-commit",
            "token_type": "Bearer",
            "expires_in": 300,
        }

    service = _service(repository, token_store, refresh, events=[])

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_runtime_blocked"
    assert token_store.writes == []
    assert repository.refresh_updates == []


@pytest.mark.parametrize(
    "install_row",
    [
        None,
        {
            "install_id": "install-a",
            "account_id": "account-a",
            "extension_id": "fixture.extension-oauth",
            "release_id": "release-a",
            "state": "disabled",
        },
        {
            "install_id": "install-a",
            "account_id": "account-a",
            "extension_id": "fixture.extension-oauth",
            "release_id": "release-new",
            "state": "active",
        },
    ],
)
def test_initial_missing_disabled_or_changed_install_blocks_runtime(
    install_row: dict[str, Any] | None,
) -> None:
    repository = _RefreshRepository()
    repository.install_row = copy.deepcopy(install_row)
    token_store = _RefreshTokenStore()
    service = _service(
        repository,
        token_store,
        lambda **_values: pytest.fail("provider must not run"),
        events=[],
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_runtime_blocked"
    assert token_store.writes == []


@pytest.mark.parametrize(
    "error_code", [OAuthErrorCode.INVALID_CLIENT, OAuthErrorCode.INVALID_GRANT]
)
@pytest.mark.parametrize("install_mutation", ["disabled", "release-change"])
def test_terminal_failure_cannot_persist_after_install_lifecycle_race(
    error_code: OAuthErrorCode,
    install_mutation: str,
) -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()

    def refresh(**_values: Any) -> dict[str, Any]:
        assert repository.install_row is not None
        if install_mutation == "disabled":
            repository.install_row["state"] = "disabled"
        else:
            repository.install_row["release_id"] = "release-new"
        raise OAuthProviderError(error_code, OAuthProviderOperation.REFRESH)

    service = _service(repository, token_store, refresh, events=[])

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_runtime_blocked"
    assert repository.failure_updates == []
    assert repository.configuration["status"] == "verified"
    assert repository.connection["connection_config"]["oauth"]["lifecycle_status"] == "connected"


def test_busy_or_uncertain_refresh_claim_fails_with_closed_error() -> None:
    repository = _RefreshRepository()
    token_store = _RefreshTokenStore()

    @contextmanager
    def unavailable_claim(**_scope: Any):
        raise TimeoutError("raw lock detail")
        yield

    repository.oauth_refresh_claim = unavailable_claim  # type: ignore[method-assign]
    service = _service(
        repository,
        token_store,
        lambda **_values: pytest.fail("provider must not run"),
        events=[],
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_provider_unavailable"
    assert "raw lock detail" not in str(exc_info.value)
