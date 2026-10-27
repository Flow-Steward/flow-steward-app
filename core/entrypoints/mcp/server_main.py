"""Process entrypoint for the thin API-backed MCP transport."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from core.entrypoints.mcp.server_build_revision import verify_embedded_build_revision
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


def main() -> None:
    args = _build_argument_parser().parse_args()
    verify_embedded_build_revision()
    logger.info("Starting Flow Steward MCP server — transport=%s", args.transport)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return
    mcp.run(
        transport="streamable-http",
        **_streamable_http_run_kwargs(host=args.host, port=args.port),
    )


__all__ = [
    "SUPPORTED_MCP_TRANSPORTS",
    "_allowed_hosts",
    "_build_argument_parser",
    "_streamable_http_run_kwargs",
    "main",
]
