from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from core.application.extensions.oauth_repository import ExtensionOAuthRepository
from core.application.extensions.oauth_runtime import OAuthRuntimeTokenError
from tests.unit.test_extension_oauth_refresh import (
    _RefreshRepository,
    _RefreshTokenStore,
    _resolve,
    _service,
)


def _scope(**overrides: str) -> dict[str, str]:
    values = {
        "tenant_id": "tenant-a",
        "account_id": "account-a",
        "project_id": "project-a",
        "extension_install_id": "install-a",
        "extension_id": "fixture.extension-oauth",
        "provider_id": "example",
        "configuration_id": "configuration-a",
        "connection_id": "connection-a",
        "connection_type": "example_mail",
    }
    values.update(overrides)
    return values


class _ClaimConnection:
    def __init__(
        self,
        *,
        try_results: list[bool] | None = None,
        unlock_result: bool = True,
        unlock_error: Exception | None = None,
    ) -> None:
        self.autocommit = True
        self.closed = False
        self.try_results = list(try_results or [True])
        self.unlock_result = unlock_result
        self.unlock_error = unlock_error
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._row: tuple[bool] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def cursor(self):
        return self

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.calls.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            result = self.try_results.pop(0) if len(self.try_results) > 1 else self.try_results[0]
            self._row = (result,)
            return
        if self.unlock_error is not None:
            raise self.unlock_error
        self._row = (self.unlock_result,)

    def fetchone(self) -> tuple[bool] | None:
        return self._row

    def close(self) -> None:
        self.closed = True


def _repository(connection: _ClaimConnection, *, timeout: float = 0.1):
    @contextmanager
    def factory():
        yield connection

    return ExtensionOAuthRepository(
        object(),
        refresh_claim_connection_factory=factory,
        refresh_claim_timeout_seconds=timeout,
    )


def test_real_claim_uses_exact_scoped_key_and_unlocks_successfully() -> None:
    first = _ClaimConnection()
    second = _ClaimConnection()

    with _repository(first).oauth_refresh_claim(**_scope()):
        pass
    with _repository(second).oauth_refresh_claim(**_scope(project_id="project-b")):
        pass

    first_lock_key = first.calls[0][1][0]
    second_lock_key = second.calls[0][1][0]
    assert first_lock_key != second_lock_key
    assert first.calls[-1][1][0] == first_lock_key
    assert "pg_advisory_unlock" in first.calls[-1][0]
    assert first.closed is False


def test_real_claim_busy_wait_is_bounded_and_never_unlocks_unowned_key() -> None:
    connection = _ClaimConnection(try_results=[False])

    with (
        pytest.raises(TimeoutError, match="claim is busy"),
        _repository(connection, timeout=0.1).oauth_refresh_claim(**_scope()),
    ):
        pytest.fail("busy claim must not enter")

    assert connection.calls
    assert all("pg_advisory_unlock" not in sql for sql, _params in connection.calls)
    assert connection.closed is False


def test_real_busy_claim_returns_closed_service_error_without_provider_call() -> None:
    connection = _ClaimConnection(try_results=[False])
    claim_repository = _repository(connection, timeout=0.1)
    runtime_repository = _RefreshRepository()
    runtime_repository.oauth_refresh_claim = claim_repository.oauth_refresh_claim  # type: ignore[method-assign]
    provider_calls: list[str] = []
    service = _service(
        runtime_repository,
        _RefreshTokenStore(),
        lambda **_values: provider_calls.append("called") or {},
        events=[],
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_provider_unavailable"
    assert provider_calls == []


@pytest.mark.parametrize(
    ("unlock_result", "unlock_error"),
    [(False, None), (True, RuntimeError("unlock transport failed"))],
)
def test_real_claim_uncertain_unlock_closes_pinned_session(
    unlock_result: bool,
    unlock_error: Exception | None,
) -> None:
    connection = _ClaimConnection(
        unlock_result=unlock_result,
        unlock_error=unlock_error,
    )

    with (
        pytest.raises(RuntimeError, match="release was uncertain"),
        _repository(connection).oauth_refresh_claim(**_scope()),
    ):
        pass

    assert connection.closed is True


def test_real_uncertain_unlock_is_closed_at_service_boundary() -> None:
    connection = _ClaimConnection(unlock_error=RuntimeError("raw unlock failure"))
    claim_repository = _repository(connection)
    runtime_repository = _RefreshRepository()
    runtime_repository.oauth_refresh_claim = claim_repository.oauth_refresh_claim  # type: ignore[method-assign]
    service = _service(
        runtime_repository,
        _RefreshTokenStore(),
        lambda **_values: {
            "access_token": "new-access",
            "token_type": "Bearer",
            "expires_in": 300,
        },
        events=[],
    )

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_provider_unavailable"
    assert "raw unlock failure" not in str(exc_info.value)
    assert connection.closed is True
