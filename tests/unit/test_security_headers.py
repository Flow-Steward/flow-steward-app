from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import Response


def _request(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "server": (host, 443),
            "headers": [(b"host", host.encode())],
        }
    )


async def _ok(_request) -> Response:
    return Response("ok")


def test_security_headers_middleware_uses_declared_extension_browser_origins(
    monkeypatch,
) -> None:
    from core.infrastructure.runtime import http_app_lifecycle

    monkeypatch.setattr(
        http_app_lifecycle,
        "extension_browser_permission_snapshot",
        lambda: {
            "script-src": ("https://script.fixture.test",),
            "connect-src": ("https://connect.fixture.test",),
            "frame-src": ("https://frame.fixture.test",),
        },
    )

    async def call_next(_request) -> Response:
        return Response("ok")

    response = asyncio.run(
        http_app_lifecycle._security_headers_middleware(_request("flow.example.com"), call_next)
    )

    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "style-src 'self'" in response.headers["content-security-policy"]
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert (
        "script-src 'self' https://script.fixture.test"
        in response.headers["content-security-policy"]
    )
    assert (
        "connect-src 'self' https://connect.fixture.test"
        in response.headers["content-security-policy"]
    )
    assert (
        "frame-src 'self' https://frame.fixture.test" in response.headers["content-security-policy"]
    )
    assert "*.fixture.test" not in response.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline'" not in response.headers["content-security-policy"]
    assert "style-src 'self' 'unsafe-inline'" not in response.headers["content-security-policy"]
    assert "camera=()" in response.headers["permissions-policy"]
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-embedder-policy"] == "credentialless"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8443",
        "LOCALHOST",
        "app.localhost",
        "127.0.0.1:8443",
        "[::1]:8443",
        "10.0.0.5",
    ],
)
def test_no_hsts_for_localhost_or_ip_hosts(host: str) -> None:
    """A two-year HSTS on localhost would force HTTPS on every other local app."""
    from core.infrastructure.runtime import http_app_lifecycle

    response = asyncio.run(http_app_lifecycle._security_headers_middleware(_request(host), _ok))

    assert "strict-transport-security" not in response.headers
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.parametrize(
    "host", ["flow.example.com", "flow.example.com:8443", "localhost.example.com"]
)
def test_hsts_for_domain_names(host: str) -> None:
    from core.infrastructure.runtime import http_app_lifecycle

    response = asyncio.run(http_app_lifecycle._security_headers_middleware(_request(host), _ok))

    assert response.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"


def test_extension_csp_core_is_provider_neutral_and_never_scans_bundles() -> None:
    root = Path(__file__).parents[2] / "core"
    sources = [
        root / "infrastructure" / "runtime" / "http_app_lifecycle.py",
        root / "infrastructure" / "extension_runtime" / "browser_permissions.py",
    ]
    contents = "\n".join(source.read_text(encoding="utf-8") for source in sources).lower()

    assert "google" not in contents
    assert "scan_extensions" not in contents
