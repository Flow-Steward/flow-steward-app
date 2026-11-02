"""Process entrypoint for the thin API-backed MCP transport."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

# Must stay the first import: fixes the OTEL_* environment before libraries read it.
import core.infrastructure.observability.otel_bootstrap  # noqa: F401  # isort: split
from core.entrypoints.mcp import server_runtime
from core.entrypoints.mcp.server_build_revision import verify_embedded_build_revision
from core.entrypoints.mcp.server_oauth_discovery import OAuthDiscoveryAdapter
from core.entrypoints.mcp.server_runtime import logger, mcp
from mcp.server.transport_security import TransportSecuritySettings

SUPPORTED_MCP_TRANSPORTS = ("streamable-http", "stdio")


#: Ports a client omits from the Host header, per scheme.
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _authority_forms(authority: str, scheme: str = "") -> list[str]:
    """Return the authority as written plus, for a default port, the bare-host form.

    AC17-R55: the installer writes `FS_PUBLIC_BASE_URL=https://host:443`, so the derived
    authority carried `:443` — while every standard client omits the default port from the
    Host header and sends bare `host`. The two never matched and the transport answered 421
    to the whole public surface. A non-default port is never dropped: an install on 8443 must
    keep matching `host:8443` and must not start accepting bare `host`.
    """
    parsed = urlsplit(f"//{authority}")
    if not parsed.hostname:
        raise ValueError("MCP allowed host must be an exact authority")
    forms = [authority]
    # With a scheme in hand, compare against *that* scheme's default: a client speaking http
    # to :443 sends `host:443` and must not be matched by a bare host. FS_MCP_ALLOWED_HOSTS
    # entries are bare authorities with no scheme, so there the two standard web ports are
    # the best available answer -- they are exactly the ports a client omits.
    default_port = _DEFAULT_SCHEME_PORTS.get(scheme.strip().lower())
    omitted = {default_port} if default_port else set(_DEFAULT_SCHEME_PORTS.values())
    if parsed.port in omitted and parsed.hostname != authority:
        forms.append(parsed.hostname if ":" not in parsed.hostname else f"[{parsed.hostname}]")
    return forms


def _allowed_hosts(environ: Mapping[str, str] | None = None) -> list[str]:
    source = os.environ if environ is None else environ
    hosts = ["localhost:*", "127.0.0.1:*", "fs-mcp:8100"]
    configured = str(source.get("FS_MCP_ALLOWED_HOSTS") or "").strip()
    scheme = ""
    if configured:
        candidates = [item.strip() for item in configured.split(",") if item.strip()]
    else:
        public_url = str(source.get("FS_PUBLIC_BASE_URL") or "").strip()
        parsed = urlsplit(public_url) if public_url else None
        candidates = [parsed.netloc] if parsed and parsed.hostname else []
        scheme = parsed.scheme if parsed else ""
    for candidate in candidates:
        if any(token in candidate for token in ("/", "\\", "@", "*")):
            raise ValueError("MCP allowed host must be an exact authority")
        hosts.extend(_authority_forms(candidate, scheme))
    return list(dict.fromkeys(hosts))


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flow Steward MCP Server")
    parser.add_argument(
        "--transport",
        choices=SUPPORTED_MCP_TRANSPORTS,
        default="streamable-http",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8100)
    return parser


def _streamable_http_run_kwargs(*, host: str, port: int) -> dict[str, object]:
    return {
        "host": host,
        "port": port,
        "streamable_http_path": "/mcp",
        "json_response": True,
        "stateless_http": True,
        "transport_security": TransportSecuritySettings(allowed_hosts=_allowed_hosts()),
    }


def build_streamable_http_app(*, host: str, port: int) -> Any:
    """The SDK Starlette app, wrapped in the OAuth discovery adapter when auth is on.

    ADR 0047 decision 3: built with ``mcp.streamable_http_app`` and served by uvicorn
    exactly as the SDK's ``run_streamable_http_async`` does, so the adapter can answer
    protected resource metadata and complete the 401 challenge.
    """
    kwargs = _streamable_http_run_kwargs(host=host, port=port)
    kwargs.pop("port")
    app: Any = mcp.streamable_http_app(**cast(Any, kwargs))
    if server_runtime.auth_settings is not None and server_runtime.MCP_RESOURCE_URL:
        app = OAuthDiscoveryAdapter(
            app,
            resource_url=server_runtime.MCP_RESOURCE_URL,
            issuer_url=server_runtime.MCP_ISSUER_URL,
        )
    return app


def _serve_streamable_http(*, host: str, port: int) -> None:
    import anyio
    import uvicorn

    config = uvicorn.Config(
        build_streamable_http_app(host=host, port=port),
        host=host,
        port=port,
        log_level=mcp.settings.log_level.lower(),
    )
    anyio.run(uvicorn.Server(config).serve)


def main() -> None:
    from core.infrastructure.observability.otel_runtime import (
        init_telemetry,
        shutdown_telemetry,
    )

    args = _build_argument_parser().parse_args()
    verify_embedded_build_revision()
    logger.info("Starting Flow Steward MCP server — transport=%s", args.transport)
    init_telemetry(role="mcp")
    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
            return
        _serve_streamable_http(host=args.host, port=args.port)
    finally:
        shutdown_telemetry()


__all__ = [
    "SUPPORTED_MCP_TRANSPORTS",
    "_allowed_hosts",
    "_build_argument_parser",
    "_streamable_http_run_kwargs",
    "build_streamable_http_app",
    "main",
]
