"""Exact private-network origins an operator lets provider connections reach (ADR 0078).

``FS_ALLOWED_PRIVATE_ORIGINS`` lists origins, ``scheme://host:port``, separated by
commas. It is empty by default, which allows none. Each entry admits exactly one
scheme, one host name (or private IP address) and one port: a Base URL with another
host, another port or the other scheme is not covered, and nothing else about the
private-address protection changes. ``FS_ALLOW_PRIVATE_REMOTE_URLS`` stays the
separate, installation-wide switch.

Values are checked strictly when they are set (``config set``) and again when they
are read: a value that does not parse allows nothing.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import urlsplit

from core.infrastructure.config.runtime_env import runtime_env

logger = logging.getLogger(__name__)

SETTING = "FS_ALLOWED_PRIVATE_ORIGINS"
MAX_ORIGINS = 16
EXAMPLE = "http://host.docker.internal:11434"

_SEPARATORS = re.compile(r"[,\s]+")
# A DNS label, or a Compose service name (which may contain underscores).
_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
# The address ranges an entry may name directly: loopback, RFC 1918 and ULA.
# Link-local (the cloud metadata service), CGNAT, multicast, reserved and
# unspecified addresses are never admitted, whatever the setting says.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class PrivateOriginError(ValueError):
    """An entry of the setting that is not one exact private origin."""


def _is_private_literal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    if address.is_loopback:
        return True
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    return any(address in network for network in _PRIVATE_NETWORKS)


def _host_text(host: str, entry: str) -> str:
    if "*" in host:
        raise PrivateOriginError(f"'{entry}' has a wildcard; name one exact host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not _is_private_literal(address):
            raise PrivateOriginError(
                f"'{entry}' is not a loopback or private address (10.x, 172.16-31.x, "
                "192.168.x, fc00::/7); link-local and metadata addresses are never allowed"
            )
        return f"[{address.compressed}]" if address.version == 6 else address.compressed
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.match(label) for label in labels):
        raise PrivateOriginError(f"'{entry}' does not name a valid host")
    if labels[-1].isdigit():
        # "127.1" or "10.1" would resolve to an address no one wrote down.
        raise PrivateOriginError(f"'{entry}' is neither an IP address nor a host name")
    return host


def parse_origin(entry: str) -> str:
    """One entry as its canonical ``scheme://host:port``, or ``PrivateOriginError``."""
    text = str(entry or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise PrivateOriginError(f"'{text}' is not an origin such as {EXAMPLE}") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise PrivateOriginError(f"'{text}' must start with http:// or https://")
    if "@" in parts.netloc:
        raise PrivateOriginError(f"'{text}' must not contain a user name or password")
    if parts.path not in {"", "/"} or parts.query or parts.fragment or text.endswith(("?", "#")):
        raise PrivateOriginError(f"'{text}' must be an origin only, without a path or query")
    host = str(parts.hostname or "").rstrip(".")
    if not host:
        raise PrivateOriginError(f"'{text}' needs a host")
    if port is None or port == 0:
        raise PrivateOriginError(f"'{text}' needs its port, for example {EXAMPLE}")
    return f"{scheme}://{_host_text(host, text)}:{port}"


def parse_allowed_private_origins(raw: object) -> tuple[str, ...]:
    """Every entry of the setting, canonical and without duplicates; empty allows none."""
    entries = [item for item in _SEPARATORS.split(str(raw or "").strip()) if item]
    origins = tuple(dict.fromkeys(parse_origin(item) for item in entries))
    if len(origins) > MAX_ORIGINS:
        raise PrivateOriginError(f"at most {MAX_ORIGINS} origins can be listed")
    return origins


def url_origin(url: str) -> str | None:
    """The canonical origin of an http(s) URL, the scheme's port filled in; else ``None``."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = str(parts.hostname or "").rstrip(".")
    if scheme not in {"http", "https"} or not host or "@" in parts.netloc:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"


def allowed_private_origins() -> tuple[str, ...]:
    """The operator's allowlist as read now; a value that does not parse allows nothing."""
    raw = runtime_env.get(SETTING)
    if not str(raw or "").strip():
        return ()
    try:
        return parse_allowed_private_origins(raw)
    except PrivateOriginError as exc:
        logger.warning("%s is ignored: %s", SETTING, exc)
        return ()


__all__ = [
    "EXAMPLE",
    "MAX_ORIGINS",
    "SETTING",
    "PrivateOriginError",
    "allowed_private_origins",
    "parse_allowed_private_origins",
    "parse_origin",
    "url_origin",
]
