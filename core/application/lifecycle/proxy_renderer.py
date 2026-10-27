"""Deterministic external reverse-proxy fragments for supported deployments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_SAFE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SAFE_BACKEND = re.compile(r"^[A-Za-z0-9_.-]+:\d{1,5}$")


@dataclass(frozen=True, slots=True)
class ProxyRenderConfig:
    kind: Literal["nginx", "apache"]
    deployment_profile: Literal["compact", "scalable"]
    public_host: str
    app_backend: str
    mcp_backend: str | None = None

    def __post_init__(self) -> None:
        if not _SAFE_HOST.fullmatch(self.public_host) or ".." in self.public_host:
            raise ValueError("public proxy host is invalid")
        for backend in (self.app_backend, self.mcp_backend):
            if backend is None:
                continue
            if not _SAFE_BACKEND.fullmatch(backend):
                raise ValueError("proxy backend must be a host and decimal port")
            port = int(backend.rsplit(":", 1)[1])
            if not 1 <= port <= 65535:
                raise ValueError("proxy backend port must be between 1 and 65535")
        if self.deployment_profile == "scalable" and self.mcp_backend is None:
            raise ValueError("Scalable proxy rendering requires an MCP backend")


def render_proxy_fragment(config: ProxyRenderConfig) -> str:
    """Render a complete syntax-checkable config without certificate material."""
    mcp = config.app_backend if config.deployment_profile == "compact" else config.mcp_backend
    assert mcp is not None
    if config.kind == "nginx":
        return _nginx(config.public_host, config.app_backend, mcp)
    return _apache(config.public_host, config.app_backend, mcp)


_DISCOVERY_PATH = "/.well-known/oauth-protected-resource"


def _nginx(host: str, app: str, mcp: str) -> str:
    shared = (
        "    proxy_http_version 1.1;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header Authorization $http_authorization;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n"
        '    proxy_set_header Connection "upgrade";\n'
        "    proxy_buffering off;\n"
    )
    return (
        "server {\n"
        "  listen 80;\n"
        f"  server_name {host};\n"
        "  location = /mcp {\n"
        f"    proxy_pass http://{mcp};\n{shared}  }}\n"
        "  location ^~ /mcp/ {\n"
        f"    proxy_pass http://{mcp};\n{shared}  }}\n"
        # P1-5: OAuth protected-resource discovery is served by the MCP process (RFC 9728
        # puts it outside /mcp). Routed to the app it answered 404 on Scalable.
        f"  location ^~ {_DISCOVERY_PATH} {{\n"
        f"    proxy_pass http://{mcp};\n{shared}  }}\n"
        "  location / {\n"
        f"    proxy_pass http://{app};\n{shared}  }}\n"
        "}\n"
    )


# The application's WebSocket endpoint (job progress) lives under it.
_WEBSOCKET_PREFIX = "/ws/"


def _apache(host: str, app: str, mcp: str) -> str:
    # External review P2: mod_proxy_http forwards a WebSocket handshake as a plain request
    # and drops the Upgrade header, so /ws/ reached the application as an ordinary GET.
    # Measured through httpd 2.4.68. The ws:// rule goes through mod_proxy_wstunnel,
    # which every 2.4 release has; the `upgrade=websocket` ProxyPass flag would need
    # 2.4.47, newer than what RHEL 8 ships.
    return (
        "# Requires mod_proxy, mod_proxy_http and mod_proxy_wstunnel.\n"
        "<VirtualHost *:80>\n"
        f"  ServerName {host}\n"
        "  ProxyPreserveHost On\n"
        f'  ProxyPass "/mcp" "http://{mcp}/mcp" nocanon\n'
        f'  ProxyPassReverse "/mcp" "http://{mcp}/mcp"\n'
        f'  ProxyPass "{_DISCOVERY_PATH}" "http://{mcp}{_DISCOVERY_PATH}" nocanon\n'
        f'  ProxyPassReverse "{_DISCOVERY_PATH}" "http://{mcp}{_DISCOVERY_PATH}"\n'
        f'  ProxyPass "{_WEBSOCKET_PREFIX}" "ws://{app}{_WEBSOCKET_PREFIX}" nocanon\n'
        f'  ProxyPassReverse "{_WEBSOCKET_PREFIX}" "ws://{app}{_WEBSOCKET_PREFIX}"\n'
        f'  ProxyPass "/" "http://{app}/" nocanon\n'
        f'  ProxyPassReverse "/" "http://{app}/"\n'
        "</VirtualHost>\n"
    )
