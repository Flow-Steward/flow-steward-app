"""P1-5: MCP on Scalable answered its public address with 421 and 404.

Measured on fs-scal13 (Scalable, bundled Traefik, https://162.55.214.63:8611):

    POST /mcp initialize, valid token             -> 421 Invalid Host header
    GET  /.well-known/oauth-protected-resource/mcp -> 404
    fs-mcp environment: no FS_PUBLIC_BASE_URL, no FS_MCP_ALLOWED_HOSTS

Compact fixed both in AC17-R55/R56 inside process_spec, which ships only in the Compact
image; Scalable's fs-mcp never received the public origin at all. The two halves:
what the server needs to know (the public origin, delivered by compose on Scalable and
by the supervisor on Compact), and where discovery is routed (every ingress).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DISCOVERY = "/.well-known/oauth-protected-resource"


def _prod_services() -> dict:
    return yaml.safe_load((ROOT / "infra/docker/docker-compose.prod.yaml").read_text())["services"]


def test_scalable_delivers_the_public_origin_to_the_mcp_server() -> None:
    environment = _prod_services()["fs-mcp"]["environment"]

    assert environment.get("FS_PUBLIC_BASE_URL") == "${FS_PUBLIC_BASE_URL}"


def test_scalable_does_not_pin_the_mcp_urls_to_development_names() -> None:
    """`${FS_MCP_ISSUER_URL:-http://fs-app:8099}` is never empty, so no derivation can win."""
    environment = _prod_services()["fs-mcp"]["environment"]

    for name in ("FS_MCP_ISSUER_URL", "FS_MCP_RESOURCE_URL"):
        value = str(environment.get(name, ""))
        assert "fs-app" not in value, (name, value)
        assert "localhost" not in value, (name, value)


@pytest.mark.parametrize(
    ("public", "issuer", "resource"),
    (
        (
            "https://162.55.214.63:8611",
            "https://162.55.214.63:8611",
            "https://162.55.214.63:8611/mcp",
        ),
        # The installer writes the default port out; clients never send it (AC17-R55).
        ("https://flow.example.com:443", "https://flow.example.com", "https://flow.example.com/mcp"),
        ("https://flow.example.com", "https://flow.example.com", "https://flow.example.com/mcp"),
    ),
)
def test_the_mcp_server_derives_its_public_urls_from_the_public_origin(
    public: str, issuer: str, resource: str
) -> None:
    from core.entrypoints.mcp.server_public_identity import mcp_auth_urls

    assert mcp_auth_urls({"FS_PUBLIC_BASE_URL": public}) == (issuer, resource)


def test_explicit_mcp_urls_still_win_over_the_derivation() -> None:
    """Compact's supervisor sets both explicitly; nothing here may overrule it."""
    from core.entrypoints.mcp.server_public_identity import mcp_auth_urls

    urls = mcp_auth_urls(
        {
            "FS_PUBLIC_BASE_URL": "https://ignored.example",
            "FS_MCP_ISSUER_URL": "https://issuer.example",
            "FS_MCP_RESOURCE_URL": "https://issuer.example/mcp",
        }
    )

    assert urls == ("https://issuer.example", "https://issuer.example/mcp")


def test_the_public_origin_alone_admits_the_host_clients_send() -> None:
    """The green direction of the 421: the existing allowlist needs only the variable."""
    from core.entrypoints.mcp.server_main import _allowed_hosts

    assert "162.55.214.63:8611" in _allowed_hosts(
        {"FS_PUBLIC_BASE_URL": "https://162.55.214.63:8611"}
    )


def _routes_discovery_to_mcp(rule: str) -> bool:
    return f"PathPrefix(`{DISCOVERY}`)" in rule


def test_bundled_scalable_traefik_routes_discovery_to_mcp() -> None:
    dynamic = yaml.safe_load((ROOT / "infra/traefik/fs.dynamic.yaml").read_text())
    router = dynamic["http"]["routers"]["fs-mcp"]

    assert _routes_discovery_to_mcp(router["rule"]), router["rule"]


def test_scalable_behind_an_existing_traefik_routes_discovery_to_mcp() -> None:
    text = (ROOT / "infra/docker/scalable/ingress-external_traefik.yaml").read_text()
    rules = re.findall(r'routers\.[^"]*-mcp\.rule=([^"]+)"', text)

    assert rules, "no MCP router label found; this test would be vacuous"
    assert all(_routes_discovery_to_mcp(rule) for rule in rules), rules


@pytest.mark.parametrize("kind", ("nginx", "apache"))
def test_scalable_behind_nginx_or_apache_routes_discovery_to_mcp(kind: str) -> None:
    from core.application.lifecycle.proxy_renderer import ProxyRenderConfig, render_proxy_fragment

    fragment = render_proxy_fragment(
        ProxyRenderConfig(
            kind=kind,  # type: ignore[arg-type]
            deployment_profile="scalable",
            public_host="fs.example.test",
            app_backend="127.0.0.1:18099",
            mcp_backend="127.0.0.1:18100",
        )
    )

    discovery_lines = [line for line in fragment.splitlines() if DISCOVERY in line]
    assert discovery_lines, fragment
    if kind == "nginx":
        # The location block that matches discovery forwards to the MCP backend.
        block = fragment.split(DISCOVERY, 1)[1].split("}", 1)[0]
        assert "proxy_pass http://127.0.0.1:18100" in block, block
    else:
        assert all("127.0.0.1:18100" in line for line in discovery_lines), discovery_lines


def test_compact_routes_discovery_to_mcp() -> None:
    """The reference: Compact's own router has had this since AC17-R56."""
    template = (ROOT / "core/infrastructure/runtime/compact/router.yaml.tmpl").read_text()

    assert f"PathPrefix(`{DISCOVERY}`)" in template
