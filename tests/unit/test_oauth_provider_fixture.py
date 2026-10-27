from __future__ import annotations

import base64
import hashlib
import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit

import pytest

from core.application.extensions.callbacks import callback_url
from tests.fixtures.oauth_provider import OAuthFixtureHandler

_REDIRECT_URI = callback_url(
    base_url="https://traefik:8099",
    extension_id="fixture.extension-oauth",
    callback_id="oauth_authorization",
    callback_token="opaque-capability",
)


def test_redirect_contract_accepts_real_production_callback_url(monkeypatch) -> None:
    monkeypatch.setenv("FS_OAUTH_ALLOWED_REDIRECT_ORIGIN", "https://traefik:8099")
    assert _REDIRECT_URI == (
        "https://traefik:8099/webhook/extensions/fixture.extension-oauth/"
        "oauth_authorization/opaque-capability"
    )
    assert OAuthFixtureHandler._redirect_is_valid(_REDIRECT_URI) is True


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://traefik:8099/api/extensions/fixture.extension-oauth/callbacks/oauth/opaque",
        "https://traefik:8099/webhook/extensions/other.extension/oauth_authorization/opaque",
        "https://traefik:8099/webhook/extensions/fixture.extension-oauth/other/opaque",
        "https://traefik:8099/webhook/extensions/fixture.extension-oauth/oauth_authorization",
        "https://traefik:8099/webhook/extensions/fixture.extension-oauth/oauth_authorization/",
        "https://traefik:8099/webhook/extensions/fixture.extension-oauth/oauth_authorization/a/b",
        "https://traefik:8099/webhook/extensions/fixture.extension-oauth/oauth_authorization/opaque?q=1",
        "https://traefik:8099/webhook/extensions/fixture.extension-oauth/oauth_authorization/opaque#fragment",
        "https://other.example.test/webhook/extensions/fixture.extension-oauth/oauth_authorization/opaque",
        (
            "https://traefik:8099/webhook/extensions/fixture.extension-oauth/"
            f"oauth_authorization/{'x' * 257}"
        ),
    ],
)
def test_redirect_contract_rejects_noncanonical_or_malformed_paths(
    monkeypatch, redirect_uri: str
) -> None:
    monkeypatch.setenv("FS_OAUTH_ALLOWED_REDIRECT_ORIGIN", "https://traefik:8099")

    assert OAuthFixtureHandler._redirect_is_valid(redirect_uri) is False


@contextmanager
def _provider() -> Iterator[tuple[str, int]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _authorize(host: str, port: int, *, verifier: str = "fixture-verifier") -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    query = urlencode(
        {
            "client_id": "primary-application-client",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "login_hint": "success@example.test",
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": "mail.read mail.write",
            "state": "opaque-state",
        }
    )
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request("GET", f"/authorize?{query}")
    response = connection.getresponse()
    assert response.status == 302
    location = response.getheader("Location") or ""
    connection.close()
    return dict(item.split("=", 1) for item in urlsplit(location).query.split("&") if "=" in item)[
        "code"
    ]


def _token(host: str, port: int, **overrides: str) -> tuple[int, str]:
    form = {
        "client_id": "primary-application-client",
        "client_secret": "fixture-credential",
        "code": "missing",
        "code_verifier": "fixture-verifier",
        "grant_type": "authorization_code",
        "redirect_uri": _REDIRECT_URI,
        **overrides,
    }
    encoded = urlencode(form)
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request(
        "POST",
        "/token",
        body=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response = connection.getresponse()
    body = response.read().decode()
    status = response.status
    connection.close()
    return status, body


def test_provider_requires_exact_s256_redirect_and_one_time_code() -> None:
    with _provider() as (host, port):
        code = _authorize(host, port)

        status, body = _token(host, port, code=code, code_verifier="wrong")
        assert status == 400
        assert "invalid_grant" in body

        status, body = _token(host, port, code=code)
        assert status == 400
        assert "invalid_grant" in body


def test_provider_accepts_exact_exchange_and_rejects_bad_client_revoke() -> None:
    with _provider() as (host, port):
        code = _authorize(host, port)
        status, body = _token(host, port, code=code)
        assert status == 200
        assert "refresh_token" in body

        encoded = urlencode(
            {
                "client_id": "primary-application-client",
                "client_secret": "wrong",
                "token": "fixture-access-1",
            }
        )
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("POST", "/revoke", body=encoded)
        response = connection.getresponse()
        assert response.status == 401
        assert "invalid_client" in response.read().decode()
        connection.close()


def test_provider_rejects_unknown_suffix_client_id() -> None:
    with _provider() as (host, port):
        code = _authorize(host, port)

        status, body = _token(host, port, code=code, client_id="unknown-client")

        assert status == 401
        assert "invalid_client" in body
