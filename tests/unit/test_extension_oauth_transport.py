from __future__ import annotations

import base64
import copy
import logging
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request

import pytest
import yaml

from core.application.extensions.oauth_contract import (
    OAuthEndpointPermission,
    compile_oauth_providers,
)
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
    OAuthProviderOperation,
    OAuthTimeouts,
    exchange_code,
    refresh_token,
    revoke_token,
)


class _Response:
    def __init__(
        self,
        body: bytes = b'{"access_token":"access","token_type":"Bearer","expires_in":3600}',
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = {"content-type": "application/json", **(headers or {})}
        self._body = body
        self._offset = 0

    def read(self, size: int) -> bytes:
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _Opener:
    def __init__(self, response: _Response | Exception | None = None) -> None:
        self.response = response or _Response()
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, request, **kwargs: Any):
        self.calls.append((request, dict(kwargs)))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _endpoint(method: str = "client_secret_post") -> OAuthEndpointPermission:
    return OAuthEndpointPermission(
        url="https://identity.example.test/token",
        client_auth_method=method,
    )


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http._resolve_hostname_ips",
        lambda _host, *, port: ["203.0.113.10"],
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http._is_blocked_ip",
        lambda address: address.startswith(("10.", "127.", "169.254.", "192.0.2.")),
    )


def _exchange(opener: _Opener, *, method: str = "client_secret_post") -> dict[str, Any]:
    return exchange_code(
        endpoint=_endpoint(method),
        code="authorization-code-canary",
        redirect_uri="https://app.example.test/callback",
        code_verifier="verifier-canary",
        client_id="client:name",
        client_secret="secret value",
        opener=opener,
    )


def test_client_secret_basic_uses_oauth_encoded_basic_and_never_duplicates_credentials() -> None:
    opener = _Opener()
    result = _exchange(opener, method="client_secret_basic")
    request, options = opener.calls[0]
    body = parse_qs(request.data.decode("ascii"), keep_blank_values=True)
    expected = base64.b64encode(b"client%3Aname:secret+value").decode("ascii")

    assert result["access_token"] == "access"
    assert request.get_header("Authorization") == f"Basic {expected}"
    assert "client_id" not in body
    assert "client_secret" not in body
    assert body["code"] == ["authorization-code-canary"]
    assert isinstance(options.pop("overall_deadline"), float)
    assert options == {
        "connect_timeout_seconds": 5.0,
        "read_timeout_seconds": 5.0,
        "purpose": "extension OAuth exchange",
    }


def test_client_secret_post_and_public_none_have_exact_credential_semantics() -> None:
    posted = _Opener()
    _exchange(posted)
    post_request = posted.calls[0][0]
    post_body = parse_qs(post_request.data.decode("ascii"), keep_blank_values=True)
    assert post_request.get_header("Authorization") is None
    assert post_body["client_id"] == ["client:name"]
    assert post_body["client_secret"] == ["secret value"]

    public = _Opener()
    exchange_code(
        endpoint=_endpoint("none"),
        code="code",
        redirect_uri="https://app.example.test/callback",
        code_verifier="v" * 43,
        client_id="public-client",
        opener=public,
    )
    public_body = parse_qs(public.calls[0][0].data.decode("ascii"), keep_blank_values=True)
    assert public_body["client_id"] == ["public-client"]
    assert "client_secret" not in public_body
    assert public.calls[0][0].get_header("Authorization") is None

    _raises_input_142_1 = _endpoint("none")
    _raises_input_142_2 = _Opener()
    with pytest.raises(OAuthProviderError) as exc_info:
        exchange_code(
            endpoint=_raises_input_142_1,
            code="code",
            redirect_uri="https://app.example.test/callback",
            code_verifier="v" * 43,
            client_id="public-client",
            client_secret="must-not-send",
            opener=_raises_input_142_2,
        )
    assert exc_info.value.code is OAuthErrorCode.INTERNAL_FAILURE


@pytest.mark.parametrize(
    ("status", "body", "headers", "code", "retry"),
    [
        (400, b'{"error":"invalid_client","client_secret":"raw"}', {}, "invalid_client", None),
        (400, b'{"error":"invalid_grant","code":"raw"}', {}, "invalid_grant", None),
        (429, b'{"error":"slow_down"}', {"retry-after": "999999"}, "rate_limited", 300),
        (
            429,
            b'{"error":"slow_down"}',
            {"retry-after": "Fri, 31 Dec 9999 23:59:59 GMT"},
            "rate_limited",
            300,
        ),
        (503, b"provider-body-canary", {}, "provider_unavailable", None),
        (418, b'{"error":"unknown_vendor_error"}', {}, "provider_unavailable", None),
    ],
)
def test_provider_failures_are_closed_and_retry_after_is_capped(
    caplog,
    status: int,
    body: bytes,
    headers: dict[str, str],
    code: str,
    retry: int | None,
) -> None:
    caplog.set_level(logging.DEBUG)
    canaries = ("raw", "provider-body-canary", "secret value", "authorization-code-canary")
    _raises_input_182_1 = _Opener(_Response(body, status=status, headers=headers))
    with pytest.raises(OAuthProviderError) as exc_info:
        _exchange(_raises_input_182_1)
    error = exc_info.value
    assert error.code.value == code
    assert error.operation is OAuthProviderOperation.EXCHANGE
    assert error.retry_after_seconds == retry
    exposed = f"{error!r} {error} {caplog.text}"
    assert all(canary not in exposed for canary in canaries)


@pytest.mark.parametrize(
    "response",
    [
        _Response(b"not-json"),
        _Response(b'{"access_token":"first","access_token":"second","token_type":"Bearer"}'),
        _Response(b"\xff"),
        _Response(
            b'{"access_token":"access","token_type":"Bearer"}',
            headers={"content-type": "text/html"},
        ),
        _Response(
            b'{"access_token":"access","token_type":"Bearer"}', headers={"content-encoding": "gzip"}
        ),
        _Response(b"{" + (b"x" * 70_000) + b"}"),
        _Response(b'{"access_token":"","token_type":"Bearer"}'),
        _Response(b'{"access_token":"access","token_type":"MAC"}'),
        _Response(b'{"access_token":"access","token_type":"Bearer","expires_in":-1}'),
        _Response(b'{"access_token":"access","token_type":"Bearer","expires_in":1.5}'),
        _Response(b'{"access_token":"access","token_type":"Bearer","ignored":NaN}'),
        _Response(b'{"access_token":"access","token_type":"Bearer","ignored":Infinity}'),
        _Response(b'{"access_token":"access","token_type":"Bearer","ignored":-Infinity}'),
        _Response(b'{"access_token":"access","token_type":"Bearer","ignored":{"nested":NaN}}'),
    ],
)
def test_response_contract_rejects_malformed_or_unbounded_payloads(response: _Response) -> None:
    _raises_input_217_1 = _Opener(response)
    with pytest.raises(OAuthProviderError) as exc_info:
        _exchange(_raises_input_217_1)
    assert exc_info.value.code is OAuthErrorCode.MALFORMED_RESPONSE


def test_redirects_are_never_followed_and_location_is_not_exposed() -> None:
    response = _Response(
        b"redirect-body-canary",
        status=302,
        headers={"location": "https://attacker.test/?code=location-canary"},
    )
    _raises_input_228_1 = _Opener(response)
    with pytest.raises(OAuthProviderError) as exc_info:
        _exchange(_raises_input_228_1)
    assert exc_info.value.code is OAuthErrorCode.PROVIDER_UNAVAILABLE
    assert "attacker" not in repr(exc_info.value)
    assert "location-canary" not in str(exc_info.value)


def test_runtime_dns_recheck_rejects_private_and_link_local_before_opener(monkeypatch) -> None:
    for address in ("127.0.0.1", "169.254.169.254", "10.0.0.2", "192.0.2.1"):
        monkeypatch.setattr(
            "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http._resolve_hostname_ips",
            lambda _host, *, port, selected=address: [selected],
        )
        opener = _Opener()
        with pytest.raises(OAuthProviderError) as exc_info:
            _exchange(opener)
        assert exc_info.value.code is OAuthErrorCode.PROVIDER_UNAVAILABLE
        assert opener.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://identity.example.test/token",
        "https://user:password@identity.example.test/token",
        "https://identity.example.test/token?tenant=one",
        "https://identity.example.test/token#fragment",
        "https://identity.example.test/{tenant}/token",
    ],
)
def test_transport_rejects_forged_nonimmutable_endpoint_permissions(url: str) -> None:
    opener = _Opener()
    _raises_input_260_1 = OAuthEndpointPermission(url=url, client_auth_method="client_secret_post")
    with pytest.raises(OAuthProviderError) as exc_info:
        exchange_code(
            endpoint=_raises_input_260_1,
            code="code",
            redirect_uri="https://app.example.test/callback",
            code_verifier="v" * 43,
            client_id="client",
            client_secret="secret",
            opener=opener,
        )
    assert exc_info.value.code is OAuthErrorCode.PROVIDER_UNAVAILABLE
    assert opener.calls == []


def test_refresh_and_revoke_have_strict_forms_and_success_statuses() -> None:
    refreshed = _Opener()
    refresh_token(
        endpoint=_endpoint(),
        refresh_token="refresh-canary",
        client_id="client",
        client_secret="secret",
        opener=refreshed,
    )
    refresh_body = parse_qs(refreshed.calls[0][0].data.decode("ascii"))
    assert refresh_body == {
        "client_id": ["client"],
        "client_secret": ["secret"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-canary"],
    }

    for status in (200, 204):
        revoked = _Opener(_Response(b"", status=status))
        assert (
            revoke_token(
                endpoint=OAuthEndpointPermission(
                    url="https://identity.example.test/revoke",
                ),
                token_endpoint_permission=_endpoint(),
                token="refresh-canary",
                client_id="client",
                client_secret="secret",
                opener=revoked,
            )
            is None
        )
    _raises_input_309_1 = OAuthEndpointPermission(url="https://identity.example.test/revoke")
    _raises_input_309_2 = _endpoint()
    _raises_input_309_3 = _Opener(_Response(b"{}", status=201))
    with pytest.raises(OAuthProviderError):
        revoke_token(
            endpoint=_raises_input_309_1,
            token_endpoint_permission=_raises_input_309_2,
            token="refresh-canary",
            client_id="client",
            client_secret="secret",
            opener=_raises_input_309_3,
        )


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post", "none"])
def test_revoke_uses_real_compiled_endpoint_and_token_auth_permissions(method: str) -> None:
    fixture_path = Path("tests/fixtures/extensions/fixture_extension_oauth/extension.yaml")
    manifest = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    manifest["action_definitions"] = [
        {"action_id": "verify_identity", "handler": {"mode": "extension"}}
    ]
    provider_row = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
    provider_row = copy.deepcopy(provider_row)
    provider_row["token_endpoint"]["client_auth_method"] = method
    if method == "none":
        provider_row["credential_fields"] = [
            row for row in provider_row["credential_fields"] if row["field_id"] != "client_secret"
        ]
    manifest["runtime"]["extension_contract_v2"]["oauth_providers"] = [provider_row]
    provider = compile_oauth_providers(manifest)[0]
    assert provider.revocation_endpoint is not None

    opener = _Opener(_Response(b"", status=200))
    revoke_token(
        endpoint=provider.revocation_endpoint,
        token_endpoint_permission=provider.token_endpoint,
        token="refresh-canary",
        client_id="client:name",
        client_secret="" if method == "none" else "secret value",
        opener=opener,
    )

    request = opener.calls[0][0]
    body = parse_qs(request.data.decode("ascii"))
    if method == "client_secret_basic":
        expected = base64.b64encode(b"client%3Aname:secret+value").decode("ascii")
        assert request.get_header("Authorization") == f"Basic {expected}"
        assert "client_id" not in body
        assert "client_secret" not in body
    elif method == "client_secret_post":
        assert request.get_header("Authorization") is None
        assert body["client_id"] == ["client:name"]
        assert body["client_secret"] == ["secret value"]
    else:
        assert request.get_header("Authorization") is None
        assert body["client_id"] == ["client:name"]
        assert "client_secret" not in body


@pytest.mark.parametrize(
    ("operation", "field"),
    [
        ("exchange", "client_id"),
        ("exchange", "client_secret"),
        ("exchange", "code"),
        ("exchange", "redirect_uri"),
        ("exchange", "code_verifier"),
        ("refresh", "refresh_token"),
        ("revoke", "token"),
    ],
)
def test_invalid_unicode_inputs_never_escape_closed_error(operation: str, field: str) -> None:
    bad = "unicode-canary-\ud800"
    common = {"client_id": "client", "client_secret": "secret"}
    common[field] = bad

    def _raises_operation_364():
        if operation == "exchange":
            exchange_code(
                endpoint=_endpoint(),
                code=str(common.get("code", "code")),
                redirect_uri=str(common.get("redirect_uri", "https://app.example.test/callback")),
                code_verifier=str(common.get("code_verifier", "v" * 43)),
                client_id=str(common["client_id"]),
                client_secret=str(common["client_secret"]),
                opener=_Opener(),
            )
        elif operation == "refresh":
            refresh_token(
                endpoint=_endpoint(),
                refresh_token=str(common["refresh_token"]),
                client_id=str(common["client_id"]),
                client_secret=str(common["client_secret"]),
                opener=_Opener(),
            )
        else:
            revoke_token(
                endpoint=OAuthEndpointPermission(url="https://identity.example.test/revoke"),
                token_endpoint_permission=_endpoint(),
                token=str(common["token"]),
                client_id=str(common["client_id"]),
                client_secret=str(common["client_secret"]),
                opener=_Opener(),
            )

    with pytest.raises(OAuthProviderError) as exc_info:
        _raises_operation_364()
    error = exc_info.value
    assert error.code is OAuthErrorCode.INTERNAL_FAILURE
    assert error.__cause__ is None
    assert error.__context__ is None


def test_dns_validation_time_counts_against_overall_deadline(monkeypatch) -> None:
    clock = {"now": 10.0}
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.oauth_transport.time.monotonic",
        lambda: clock["now"],
    )

    def slow_resolve(_host, *, port):
        clock["now"] += 2.0
        return ["203.0.113.10"]

    monkeypatch.setattr(
        "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http._resolve_hostname_ips",
        slow_resolve,
    )
    opener = _Opener()
    _raises_input_433_1 = _endpoint()
    _raises_input_433_2 = OAuthTimeouts(connect_seconds=1, read_seconds=1, overall_seconds=1)
    with pytest.raises(OAuthProviderError) as exc_info:
        exchange_code(
            endpoint=_raises_input_433_1,
            code="code",
            redirect_uri="https://app.example.test/callback",
            code_verifier="v" * 43,
            client_id="client",
            client_secret="secret",
            opener=opener,
            timeouts=_raises_input_433_2,
        )
    assert exc_info.value.code is OAuthErrorCode.PROVIDER_UNAVAILABLE
    assert opener.calls == []


def test_elapsed_validation_reduces_connect_and_read_budgets(monkeypatch) -> None:
    clock = {"now": 30.0}
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.oauth_transport.time.monotonic",
        lambda: clock["now"],
    )

    def slow_resolve(_host, *, port):
        clock["now"] += 8.0
        return ["203.0.113.10"]

    monkeypatch.setattr(
        "core.infrastructure.extension_sdk.public.flowsteward_extension_sdk.http._resolve_hostname_ips",
        slow_resolve,
    )
    opener = _Opener()
    exchange_code(
        endpoint=_endpoint(),
        code="code",
        redirect_uri="https://app.example.test/callback",
        code_verifier="v" * 43,
        client_id="client",
        client_secret="secret",
        opener=opener,
        timeouts=OAuthTimeouts(connect_seconds=5, read_seconds=5, overall_seconds=10),
    )
    options = opener.calls[0][1]
    assert options["connect_timeout_seconds"] == 2.0
    assert options["read_timeout_seconds"] == 2.0
    assert options["overall_deadline"] == 40.0


def test_elapsed_read_time_stops_before_another_read(monkeypatch) -> None:
    clock = {"now": 20.0}
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.oauth_transport.time.monotonic",
        lambda: clock["now"],
    )

    class SlowResponse(_Response):
        reads = 0

        def read(self, size: int) -> bytes:
            self.reads += 1
            clock["now"] += 20.0
            return super().read(size)

    response = SlowResponse()
    _raises_input_496_1 = _Opener(response)
    with pytest.raises(OAuthProviderError) as exc_info:
        _exchange(_raises_input_496_1)
    assert exc_info.value.code is OAuthErrorCode.PROVIDER_UNAVAILABLE
    assert response.reads == 1


def test_returned_http_response_socket_gets_read_timeout() -> None:
    class SocketSpy:
        timeout: float | None = None

        def settimeout(self, value: float) -> None:
            self.timeout = value

    socket_spy = SocketSpy()
    response = _Response()
    response.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket_spy))
    exchange_code(
        endpoint=_endpoint(),
        code="code",
        redirect_uri="https://app.example.test/callback",
        code_verifier="v" * 43,
        client_id="client",
        client_secret="secret",
        opener=_Opener(response),
        timeouts=OAuthTimeouts(connect_seconds=5, read_seconds=3, overall_seconds=10),
    )
    assert socket_spy.timeout == 3.0


def test_timeouts_are_bounded_and_network_failures_are_closed() -> None:
    with pytest.raises(ValueError):
        OAuthTimeouts(connect_seconds=0, read_seconds=5, overall_seconds=10)
    _raises_input_530_1 = _Opener(TimeoutError("timeout with secret-canary"))
    with pytest.raises(OAuthProviderError) as exc_info:
        _exchange(_raises_input_530_1)
    assert exc_info.value.code is OAuthErrorCode.PROVIDER_UNAVAILABLE
    assert "secret-canary" not in repr(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_pinned_opener_disables_ambient_proxy_configuration(monkeypatch) -> None:
    from core.application.extensions.url_safety import build_pinned_opener

    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:proxy-secret@proxy.example.test")
    opener = build_pinned_opener(
        ips=("203.0.113.10",),
        purpose="extension OAuth exchange",
    )
    proxy_handlers = [handler for handler in opener.handlers if isinstance(handler, ProxyHandler)]
    # urllib omits an explicitly empty ProxyHandler from the installed list;
    # importantly it does not synthesize the environment-backed default.
    assert proxy_handlers == []
    https_handler = next(
        handler for handler in opener.handlers if isinstance(handler, HTTPSHandler)
    )
    assert https_handler._context.check_hostname is True
    assert https_handler._context.verify_mode == ssl.CERT_REQUIRED
    redirect_handler = next(
        handler for handler in opener.handlers if isinstance(handler, HTTPRedirectHandler)
    )
    assert (
        redirect_handler.redirect_request(
            Request("https://identity.example.test/token"),
            None,
            302,
            "Found",
            {},
            "https://attacker.test/redirect",
        )
        is None
    )
