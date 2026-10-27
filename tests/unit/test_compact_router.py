from __future__ import annotations

import pytest
import yaml

from core.infrastructure.runtime.compact.router import (
    CompactRouterConfig,
    render_router_config,
)


def test_compact_router_renders_exact_mcp_and_app_routes() -> None:
    rendered = yaml.safe_load(render_router_config(CompactRouterConfig()))

    routers = rendered["http"]["routers"]
    services = rendered["http"]["services"]
    assert routers["mcp"] == {
        "entryPoints": ["compact"],
        # AC17-R56 added the third clause. The protected-resource metadata a 401 advertises is
        # served by the MCP process and lives outside /mcp by RFC 9728, so without it every
        # public discovery attempt fell through to the app and 404'd.
        "rule": (
            "Path(`/mcp`) || PathPrefix(`/mcp/`) "
            "|| PathPrefix(`/.well-known/oauth-protected-resource`)"
        ),
        "priority": 100,
        "service": "mcp",
    }
    assert routers["app"] == {
        "entryPoints": ["compact"],
        "rule": "PathPrefix(`/`)",
        "priority": 10,
        "service": "app",
    }
    assert routers["readiness"] == {
        "entryPoints": ["compact"],
        "rule": "Path(`/health/ready`)",
        "priority": 110,
        "service": "health",
    }
    assert services["app"]["loadBalancer"]["servers"] == [{"url": "http://127.0.0.1:8099"}]
    assert services["mcp"]["loadBalancer"]["servers"] == [{"url": "http://127.0.0.1:8100"}]
    assert services["health"]["loadBalancer"]["servers"] == [{"url": "http://127.0.0.1:9100"}]
    assert "8101" not in render_router_config(CompactRouterConfig())
    assert "stripPrefix" not in render_router_config(CompactRouterConfig())
    assert "retry" not in render_router_config(CompactRouterConfig()).lower()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("app_origin", "http://user:secret@127.0.0.1:8099"),
        ("app_origin", "http://127.0.0.1:8099/api"),
        ("mcp_origin", "https://127.0.0.1:8100"),
        ("installer_origin", "http://0.0.0.0:8101"),
    ),
)
def test_compact_router_rejects_untrusted_internal_origins(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="loopback HTTP origin"):
        CompactRouterConfig(**{field: value})


@pytest.mark.parametrize("peer", ("0.0.0.0/0", "::/0", "*", "not-an-address"))
def test_compact_router_rejects_wildcard_or_invalid_trusted_peers(peer: str) -> None:
    with pytest.raises(ValueError, match="trusted peer"):
        CompactRouterConfig(forwarded_trusted_ips=(peer,))


def test_compact_router_sends_only_the_protected_resource_metadata_to_mcp() -> None:
    """AC17-R56: narrow on purpose.

    `/.well-known/oauth-authorization-server` belongs to the issuer, which on Compact is the
    application, so the MCP rule must not swallow the whole `.well-known` namespace.
    """
    rendered = yaml.safe_load(render_router_config(CompactRouterConfig()))
    mcp_rule = rendered["http"]["routers"]["mcp"]["rule"]

    assert "PathPrefix(`/.well-known/oauth-protected-resource`)" in mcp_rule
    assert "/.well-known/oauth-authorization-server" not in mcp_rule
    assert "PathPrefix(`/.well-known`)" not in mcp_rule
    assert "PathPrefix(`/.well-known/`)" not in mcp_rule
