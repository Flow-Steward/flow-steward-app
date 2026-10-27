from __future__ import annotations

from core.application.tooling.ops.deployment_doctor_common import _NoAuthRedirectHandler


def test_authenticated_doctor_probe_refuses_redirects() -> None:
    handler = _NoAuthRedirectHandler()

    assert (
        handler.redirect_request(
            object(), None, 302, "Found", {}, "https://other.example/mcp"
        )
        is None
    )
