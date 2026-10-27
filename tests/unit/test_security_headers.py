from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.responses import Response


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

    response = asyncio.run(http_app_lifecycle._security_headers_middleware(object(), call_next))

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


def test_extension_csp_core_is_provider_neutral_and_never_scans_bundles() -> None:
    root = Path(__file__).parents[2] / "core"
    sources = [
        root / "infrastructure" / "runtime" / "http_app_lifecycle.py",
        root / "infrastructure" / "extension_runtime" / "browser_permissions.py",
    ]
    contents = "\n".join(source.read_text(encoding="utf-8") for source in sources).lower()

    assert "google" not in contents
    assert "scan_extensions" not in contents
