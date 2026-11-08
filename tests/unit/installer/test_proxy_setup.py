"""Operator-ready proxy setup artifacts, without certificate assumptions."""

from __future__ import annotations

import re
from dataclasses import asdict

import pytest

from core.application.lifecycle.proxy_renderer import ProxyRenderConfig


def _config(*, profile: str = "scalable", kind: str = "nginx") -> ProxyRenderConfig:
    return ProxyRenderConfig(
        kind=kind,  # type: ignore[arg-type]
        deployment_profile=profile,  # type: ignore[arg-type]
        public_host="flow.example.test",
        app_backend="flow-steward-app-proxy:8099",
        mcp_backend="flow-steward-mcp-proxy:8100" if profile == "scalable" else None,
    )


def test_new_nginx_site_has_complete_http_bootstrap_without_invented_certificates() -> None:
    from core.application.installer.proxy_setup import render_proxy_setup

    setup = render_proxy_setup(_config())

    assert setup.title
    assert setup.http_config.startswith("server {\n")
    assert "listen 80;" in setup.http_config
    assert "server_name flow.example.test;" in setup.http_config
    assert "location / {" in setup.http_config
    assert "ssl_certificate" not in setup.http_config
    assert "listen 443" not in setup.http_config
    instructions = "\n".join(setup.instructions)
    assert "nginx -t" in instructions
    assert "certbot --nginx -d flow.example.test --redirect" in instructions
    assert instructions.index("nginx -t") < instructions.index("certbot --nginx")


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_existing_https_locations_preserve_routing_and_streaming(profile: str) -> None:
    from core.application.installer.proxy_setup import render_proxy_setup

    setup = render_proxy_setup(_config(profile=profile))
    locations = setup.existing_https_locations

    assert "server {" not in locations
    assert "listen " not in locations
    assert "ssl_certificate" not in locations
    blocks = re.findall(r"location ([^{]+)\{([^}]+)\}", locations)
    assert {path.strip() for path, _ in blocks} == {
        "= /mcp",
        "^~ /mcp/",
        "^~ /.well-known/oauth-protected-resource",
        "/",
    }
    for path, block in blocks:
        backend = (
            "flow-steward-mcp-proxy:8100"
            if profile == "scalable" and path.strip() != "/"
            else "flow-steward-app-proxy:8099"
        )
        assert f"proxy_pass http://{backend};" in block
        for directive in (
            "proxy_http_version 1.1;",
            "proxy_set_header Host $host;",
            "proxy_set_header Authorization $http_authorization;",
            "proxy_set_header X-Forwarded-Proto $scheme;",
            "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "proxy_set_header Upgrade $http_upgrade;",
            'proxy_set_header Connection "upgrade";',
            "proxy_buffering off;",
            "proxy_read_timeout 3600s;",
        ):
            assert directive in block
    assert "localhost" not in locations
    assert "127.0.0.1" not in locations


def test_setup_is_nonsecret_serializable_and_tells_existing_tls_operators_to_keep_certificates() -> (
    None
):
    from core.application.installer.proxy_setup import render_proxy_setup

    setup = render_proxy_setup(_config())

    payload = asdict(setup)
    assert set(payload) == {"title", "http_config", "existing_https_locations", "instructions"}
    assert "certificate" in " ".join(setup.instructions).lower()
    assert "existing" in " ".join(setup.instructions).lower()
    assert all("sudo" not in item for item in setup.instructions)


def test_apache_setup_keeps_existing_supported_fragment_and_avoids_nginx_commands() -> None:
    from core.application.installer.proxy_setup import render_proxy_setup

    setup = render_proxy_setup(_config(kind="apache"))

    assert "<VirtualHost *:80>" in setup.http_config
    assert "flow-steward-mcp-proxy:8100/mcp" in setup.http_config
    assert "ws://flow-steward-app-proxy:8099/ws/" in setup.http_config
    assert 'ProxyPass "/.well-known/acme-challenge/" "!"' in setup.http_config
    assert setup.http_config.index('ProxyPass "/.well-known/acme-challenge/"') < (
        setup.http_config.index('ProxyPass "/" ')
    )
    assert 'RequestHeader set X-Forwarded-Proto "expr=%{REQUEST_SCHEME}"' in setup.http_config
    assert "<VirtualHost" not in setup.existing_https_locations
    assert "SSLCertificateFile" not in setup.existing_https_locations
    assert "a2enmod proxy proxy_http proxy_wstunnel headers ssl" in " ".join(setup.instructions)
    assert "apachectl configtest" in " ".join(setup.instructions)
    assert "certbot --apache -d flow.example.test --redirect" in " ".join(setup.instructions)
    assert "nginx" not in " ".join(setup.instructions).lower()
    assert "certbot --nginx" not in " ".join(setup.instructions)


@pytest.mark.parametrize(
    "host", ("-evil.test", "evil.test;id", "evil.test\nreturn 200;", "a..test")
)
def test_unsafe_host_never_reaches_nginx_or_certbot_instructions(host: str) -> None:
    from core.application.installer.proxy_setup import render_proxy_setup

    with pytest.raises(ValueError, match="host"):
        render_proxy_setup(
            ProxyRenderConfig(
                kind="nginx",
                deployment_profile="compact",
                public_host=host,
                app_backend="127.0.0.1:18099",
            )
        )


def test_nginx_custom_https_port_is_preserved_in_listener_and_certbot_guidance() -> None:
    from core.application.installer.proxy_setup import render_proxy_setup

    setup = render_proxy_setup(_config(), public_port=8443)

    instructions = "\n".join(setup.instructions)
    assert "listen 8443" in instructions
    assert "--https-port 8443" in instructions
    assert "ports 80 and 8443" in instructions
    assert "listen 443" not in instructions
    assert "listen 80;" in setup.http_config


def test_apache_custom_https_port_requires_matching_listener_without_unverified_certbot_command() -> (
    None
):
    from core.application.installer.proxy_setup import render_proxy_setup

    setup = render_proxy_setup(_config(kind="apache"), public_port=8443)

    instructions = "\n".join(setup.instructions)
    assert "Listen 8443" in instructions
    assert "<VirtualHost *:8443>" in instructions
    assert "ports 80 and 8443" in instructions
    assert "certbot --apache" not in instructions
    assert "certificate" in instructions.lower()


@pytest.mark.parametrize("port", (0, 65536))
def test_setup_rejects_invalid_public_port(port: int) -> None:
    from core.application.installer.proxy_setup import render_proxy_setup

    with pytest.raises(ValueError, match="port"):
        render_proxy_setup(_config(), public_port=port)


@pytest.mark.parametrize("kind", ("nginx", "apache"))
@pytest.mark.parametrize("host", ("203.0.113.9", "2001:db8::9"))
def test_ip_address_setup_requires_an_ip_certificate_instead_of_blind_domain_certbot(
    kind: str, host: str
) -> None:
    from core.application.installer.proxy_setup import render_proxy_setup

    setup = render_proxy_setup(
        ProxyRenderConfig(
            kind=kind,  # type: ignore[arg-type]
            deployment_profile="compact",
            public_host=host,
            app_backend="127.0.0.1:18099",
        )
    )

    instructions = "\n".join(setup.instructions)
    assert "IP address" in instructions
    assert "certificate" in instructions
    assert "certbot --" not in instructions
    expected_host = "[2001:db8::9]" if ":" in host else host
    assert expected_host in setup.http_config
    if kind == "nginx" and ":" in host:
        assert "listen [::]:80;" in setup.http_config
