"""Validated deterministic configuration for the Compact HTTP router."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

from core.infrastructure.config.compact_runtime import CompactPortMap
from core.infrastructure.security.forwarded_trust import TrustedProxyError, parse_trusted_proxies

_TEMPLATE_PATH = Path(__file__).with_name("router.yaml.tmpl")


def _loopback_http_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("internal service must use a loopback HTTP origin") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("internal service must use a loopback HTTP origin")
    return f"http://127.0.0.1:{port}"


def _trusted_peers(values: tuple[str, ...]) -> tuple[str, ...]:
    """The same rule as Core and the supervisor (ADR 0029, 2026-11-27): every entry an
    address or network, and together never every address."""
    try:
        networks = parse_trusted_proxies(values)
    except TrustedProxyError as exc:
        raise ValueError(f"forwarded trusted peer list refused: {exc}") from exc
    return tuple(str(network) for network in networks)


@dataclass(frozen=True, slots=True)
class CompactRouterConfig:
    public_port: int = 8080
    app_origin: str = "http://127.0.0.1:8099"
    mcp_origin: str = "http://127.0.0.1:8100"
    installer_origin: str = "http://127.0.0.1:8101"
    forwarded_trusted_ips: tuple[str, ...] = ("127.0.0.1/32", "::1/128")

    def __post_init__(self) -> None:
        CompactPortMap.build(public_port=self.public_port, worker_processes=1)
        object.__setattr__(self, "app_origin", _loopback_http_origin(self.app_origin))
        object.__setattr__(self, "mcp_origin", _loopback_http_origin(self.mcp_origin))
        object.__setattr__(
            self,
            "installer_origin",
            _loopback_http_origin(self.installer_origin),
        )
        object.__setattr__(
            self,
            "forwarded_trusted_ips",
            _trusted_peers(tuple(self.forwarded_trusted_ips)),
        )


def render_router_config(config: CompactRouterConfig) -> str:
    """Render the fixed file-provider routes used by the internal Traefik process."""
    template = Template(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.substitute(
        app_origin=config.app_origin,
        mcp_origin=config.mcp_origin,
    )
