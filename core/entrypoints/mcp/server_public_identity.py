"""The public origin the MCP server advertises, derived from the one the operator set.

P1-5. On Scalable the server fell back to `http://fs-app:8099` as its issuer and
`http://localhost:8100` as its resource: development names for a service on the
container network and for the client's own machine. A 401 from a public MCP call then
pointed `resource_metadata` at the caller's localhost. Compact had already derived both
from FS_PUBLIC_BASE_URL (AC17-R56), but inside process_spec, which ships only in the
Compact image, so Scalable's fs-mcp -- which was not even given FS_PUBLIC_BASE_URL --
never had the chance.

The derivation now lives in the MCP server itself, which both profiles run. Values set
explicitly still win: Compact's supervisor sets both, and nothing here overrules it.

Standard library only: the Scalable MCP image copies this package and runtime_env and
nothing else of core.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

_DEVELOPMENT_ISSUER = "http://fs-app:8099"
_DEVELOPMENT_RESOURCE = "http://localhost:8100"
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def public_origin(environ: Mapping[str, str]) -> str:
    """`scheme://authority` clients reach, without a default port they would never send.

    AC17-R55: the installer writes the public URL with an explicit `:443`; advertising
    it would publish a URL whose Host header the transport then rejects.
    """
    raw = str(environ.get("FS_PUBLIC_BASE_URL") or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in _DEFAULT_SCHEME_PORTS or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    hostname = parsed.hostname
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if port is not None and port != _DEFAULT_SCHEME_PORTS[parsed.scheme]:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}"


def mcp_auth_urls(environ: Mapping[str, str]) -> tuple[str, str]:
    """The authorization issuer and the protected-resource URL this server advertises.

    The authorization server is the application behind the same public origin. The
    resource is this endpoint at its public address; the SDK inserts
    /.well-known/oauth-protected-resource between host and resource path, which is the
    path every ingress routes to this server.
    """
    origin = public_origin(environ)
    issuer = str(environ.get("FS_MCP_ISSUER_URL") or "").strip()
    resource = str(environ.get("FS_MCP_RESOURCE_URL") or "").strip()
    return (
        issuer or origin or _DEVELOPMENT_ISSUER,
        resource or (f"{origin}/mcp" if origin else _DEVELOPMENT_RESOURCE),
    )


__all__ = ["mcp_auth_urls", "public_origin"]
