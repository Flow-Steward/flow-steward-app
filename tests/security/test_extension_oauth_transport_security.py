from __future__ import annotations

import io
import logging
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from core.application.extensions.oauth_contract import OAuthEndpointPermission
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
    OAuthTimeouts,
    exchange_code,
)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http._resolve_hostname_ips",
        lambda _host, *, port: ["203.0.113.10"],
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http._is_blocked_ip",
        lambda _address: False,
    )


def test_http_error_metadata_and_credentials_never_escape_closed_error(caplog) -> None:
    raw_canaries = (
        "query-code-canary",
        "location-canary",
        "header-secret-canary",
        "body-secret-canary",
        "client-secret-canary",
    )
    provider_error = HTTPError(
        "https://identity.example.test/token?code=query-code-canary",
        401,
        "provider-reason-canary",
        {
            "Location": "https://attacker.test/location-canary",
            "X-Provider-Secret": "header-secret-canary",
            "Content-Type": "application/json",
        },
        io.BytesIO(b'{"error":"invalid_client","detail":"body-secret-canary"}'),
    )

    def opener(*_args, **_kwargs):
        raise provider_error

    caplog.set_level(logging.DEBUG)
    _raises_input_55_1 = OAuthEndpointPermission(
        url="https://identity.example.test/token", client_auth_method="client_secret_post"
    )
    with pytest.raises(OAuthProviderError) as exc_info:
        exchange_code(
            endpoint=_raises_input_55_1,
            code="authorization-code-canary",
            redirect_uri="https://app.example.test/oauth/callback",
            code_verifier="verifier-canary",
            client_id="client-id-canary",
            client_secret="client-secret-canary",
            opener=opener,
        )

    error = exc_info.value
    assert error.code is OAuthErrorCode.INVALID_CLIENT
    assert error.__cause__ is None
    exposed = f"{error!r} {error} {caplog.text}"
    assert all(canary not in exposed for canary in raw_canaries)


def test_http_error_socket_gets_remaining_read_timeout_before_body_read(monkeypatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.oauth_transport.time.monotonic",
        lambda: clock["now"],
    )

    class SocketSpy:
        timeout: float | None = None

        def settimeout(self, value: float) -> None:
            self.timeout = value

    socket_spy = SocketSpy()
    body = io.BytesIO(b'{"error":"invalid_client","detail":"body-canary"}')
    body.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket_spy))
    provider_error = HTTPError(
        "https://identity.example.test/token?code=query-canary",
        401,
        "reason-canary",
        {"Content-Type": "application/json", "X-Secret": "header-canary"},
        body,
    )

    def opener(*_args, **_kwargs):
        clock["now"] += 2.0
        raise provider_error

    _raises_input_104_1 = OAuthEndpointPermission(
        url="https://identity.example.test/token", client_auth_method="client_secret_post"
    )
    _raises_input_104_2 = OAuthTimeouts(connect_seconds=5, read_seconds=3, overall_seconds=10)
    with pytest.raises(OAuthProviderError) as exc_info:
        exchange_code(
            endpoint=_raises_input_104_1,
            code="authorization-code-canary",
            redirect_uri="https://app.example.test/oauth/callback",
            code_verifier="verifier-canary",
            client_id="client-id-canary",
            client_secret="client-secret-canary",
            opener=opener,
            timeouts=_raises_input_104_2,
        )

    error = exc_info.value
    assert error.code is OAuthErrorCode.INVALID_CLIENT
    assert socket_spy.timeout == 3.0
    assert error.__cause__ is None
    assert error.__context__ is None
    exposed = f"{error!r} {error}"
    for canary in ("query-canary", "reason-canary", "header-canary", "body-canary"):
        assert canary not in exposed


def test_expired_http_error_is_closed_before_closed_failure(monkeypatch) -> None:
    clock = {"now": 200.0}
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.oauth_transport.time.monotonic",
        lambda: clock["now"],
    )
    body = io.BytesIO(b'{"error":"invalid_client","detail":"expired-body-canary"}')
    body.fp = SimpleNamespace(
        raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda _value: None))
    )
    provider_error = HTTPError(
        "https://identity.example.test/token?code=expired-query-canary",
        401,
        "expired-reason-canary",
        {"Content-Type": "application/json", "X-Secret": "expired-header-canary"},
        body,
    )

    def opener(*_args, **_kwargs):
        clock["now"] += 20.0
        raise provider_error

    _raises_input_151_1 = OAuthEndpointPermission(
        url="https://identity.example.test/token", client_auth_method="client_secret_post"
    )
    _raises_input_151_2 = OAuthTimeouts(connect_seconds=5, read_seconds=3, overall_seconds=10)
    with pytest.raises(OAuthProviderError) as exc_info:
        exchange_code(
            endpoint=_raises_input_151_1,
            code="authorization-code-canary",
            redirect_uri="https://app.example.test/oauth/callback",
            code_verifier="verifier-canary",
            client_id="client-id-canary",
            client_secret="client-secret-canary",
            opener=opener,
            timeouts=_raises_input_151_2,
        )

    error = exc_info.value
    assert error.code is OAuthErrorCode.PROVIDER_UNAVAILABLE
    assert error.__cause__ is None
    assert error.__context__ is None
    assert body.closed is True
    exposed = f"{error!r} {error}"
    assert "expired-" not in exposed
