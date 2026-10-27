"""Shared SSRF guards for outbound HTTP(S): preflight URL validation plus
connected-peer IP pinning to defeat DNS rebinding (TOCTOU).

The preflight (`assert_safe_remote_http_url`) resolves the host once and rejects
any URL that resolves to a blocked (non-global / loopback / link-local) address.
That alone is not sufficient: between the preflight resolution and the actual
socket connect, DNS can rebind to a private/link-local target. The helpers here
close that gap by *pinning* the connection to one of the exact IPs that passed
validation while preserving the original ``Host`` header and TLS SNI / cert
hostname, so a legitimate public peer keeps working and rebinding is defeated.

Two transports are supported:

* urllib (``open_pinned_url`` / ``build_pinned_opener``) — for the urllib-based
  extension descriptor / artifact / runtime probe sinks.
* requests (``pinned_requests_session``) — for ``requests``-based callers such as
  external identity-provider clients.

``_allow_private_remote_urls()`` remains the dev/test escape hatch: when set, the
preflight and the peer check are both skipped so non-prod still works.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import socket
import ssl
import urllib.request
from typing import Any
from urllib.parse import urlparse
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
)

from . import lifecycle_descriptor_io as _descriptor_io


def _allow_private_remote_urls() -> bool:
    # Looked up on the source module at call time so test monkeypatches apply.
    return _descriptor_io._allow_private_remote_urls()


def _is_blocked_ip(address: str) -> bool:
    return _descriptor_io._is_blocked_ip(address)


def _resolve_hostname_ips(hostname: str, *, port: int):
    return _descriptor_io._resolve_hostname_ips(hostname, port=port)


__all__ = [
    "PinnedPeerError",
    "RemoteURLResolutionError",
    "assert_safe_remote_http_url",
    "build_pinned_httpx_transport",
    "build_pinned_opener",
    "is_development_private_ip",
    "open_pinned_url",
    "pin_httpx_transport",
    "pinned_requests_session",
    "resolve_pinned_ips",
]


class PinnedPeerError(ValueError):
    """Raised when a connection resolves/connects to a disallowed address."""


class RemoteURLResolutionError(ValueError):
    """The configured remote hostname could not be resolved."""


def is_development_private_ip(address: str) -> bool:
    """Allow only ordinary RFC1918/ULA addresses or loopback for cleartext dev."""
    candidate = ipaddress.ip_address(str(address or "").strip().split("%", 1)[0])
    if candidate.is_loopback:
        return True
    if (
        candidate.is_link_local
        or candidate.is_unspecified
        or candidate.is_multicast
        or candidate.is_reserved
    ):
        return False
    private_networks = (
        ipaddress.ip_network((0x0A000000, 8)),
        ipaddress.ip_network((0xAC100000, 12)),
        ipaddress.ip_network((0xC0A80000, 16)),
        ipaddress.ip_network("fc00::/7"),
    )
    return any(candidate in network for network in private_networks)


def _canonical_ip(address: str) -> str:
    return ipaddress.ip_address(str(address or "").strip().split("%", 1)[0]).compressed


def assert_safe_remote_http_url(url: str, *, purpose: str) -> str:
    """Validate *url* for outbound HTTP(S): scheme, no credentials, and that the
    host does not resolve to a blocked address.

    Returns the normalized URL string. Raises ``ValueError`` otherwise.

    This is the canonical implementation; ``lifecycle._assert_safe_remote_http_url``
    re-exports it for backward compatibility.
    """
    normalized = str(url or "").strip()
    parsed = urlparse(normalized)
    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{purpose} must use http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{purpose} must not include URL credentials")
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"{purpose} must include host")
    if not _allow_private_remote_urls():
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            raise ValueError(f"{purpose} host '{host}' is not allowed")
        if host.endswith(".local"):
            raise ValueError(f"{purpose} host '{host}' is not allowed")
        try:
            ipaddress.ip_address(host.split("%", 1)[0])
            host_is_ip_address = True
        except ValueError:
            host_is_ip_address = False
        if host_is_ip_address:
            if _is_blocked_ip(host):
                raise ValueError(f"{purpose} host '{host}' is not allowed")
        else:
            port = int(parsed.port or (443 if scheme == "https" else 80))
            try:
                resolved = _resolve_hostname_ips(host, port=port)
            except Exception as exc:
                raise RemoteURLResolutionError(
                    f"{purpose} host '{host}' cannot be resolved: {exc}"
                ) from exc
            if not resolved:
                raise RemoteURLResolutionError(f"{purpose} host '{host}' cannot be resolved")
            for ip in resolved:
                if _is_blocked_ip(ip):
                    raise ValueError(
                        f"{purpose} host '{host}' resolves to disallowed address '{ip}'"
                    )
    return normalized


def assert_safe_remote_https_url(url: str, *, purpose: str) -> str:
    """Apply the canonical SSRF checks and additionally require TLS."""
    normalized = assert_safe_remote_http_url(url, purpose=purpose)
    if str(urlparse(normalized).scheme or "").lower() != "https":
        raise ValueError(f"{purpose} must use https URL")
    return normalized


PRIVATE_URLS_SETTING = "FS_ALLOW_PRIVATE_REMOTE_URLS"


def allow_private_provider_addresses() -> bool:
    """The administrator's switch for private/loopback provider endpoints."""
    return _allow_private_remote_urls()


def _url_host_ips(host: str, *, port: int) -> list[str]:
    bare = host.split("%", 1)[0]
    try:
        ipaddress.ip_address(bare)
        return [bare]
    except ValueError:
        return sorted(_resolve_hostname_ips(host, port=port))


def _private_addresses_refused(purpose: str) -> ValueError:
    return ValueError(
        f"{purpose} points to a private network address. An administrator can allow "
        f"private addresses with {PRIVATE_URLS_SETTING}=1."
    )


def assert_safe_provider_url(url: str, *, purpose: str, allow_private_addresses: bool) -> str:
    """Validate a provider endpoint such as a self-hosted LLM server's base URL.

    ``purpose`` is the field label the user sees ("Base URL"); every refusal is
    a plain sentence about that field.

    - By default only public HTTPS is accepted; a private address is refused
      with a message naming the setting that allows it.
    - With private addresses allowed, loopback and RFC1918/ULA addresses may
      use ``http`` or ``https``; public addresses still need ``https``.
    - Link-local / metadata (169.254.0.0/16, fe80::/10), CGNAT, multicast,
      reserved and unspecified addresses are refused either way.
    """
    normalized = str(url or "").strip()
    parsed = urlparse(normalized)
    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{purpose} must start with https://")
    if parsed.username or parsed.password:
        raise ValueError(f"{purpose} must not contain a user name or password")
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"{purpose} needs a host name")
    locally_named = host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".localhost", ".local")
    )
    if locally_named and not allow_private_addresses:
        raise _private_addresses_refused(purpose)
    try:
        ips = _url_host_ips(host, port=int(parsed.port or (443 if scheme == "https" else 80)))
    except Exception as exc:
        raise RemoteURLResolutionError(f"{purpose}: the host '{host}' can't be found") from exc
    if not ips:
        raise RemoteURLResolutionError(f"{purpose}: the host '{host}' can't be found")
    private = [is_development_private_ip(ip) for ip in ips]
    for ip, is_private in zip(ips, private, strict=True):
        if not is_private and not ipaddress.ip_address(ip.split("%", 1)[0]).is_global:
            raise ValueError(
                f"{purpose}: '{host}' is {ip}, a link-local, metadata or reserved "
                "address, which is never allowed"
            )
    if any(private) and not allow_private_addresses:
        raise _private_addresses_refused(purpose)
    if scheme == "http" and not all(private):
        raise ValueError(f"{purpose} must use https:// for a public address")
    return normalized


def resolve_pinned_ips(
    host: str,
    *,
    port: int,
    purpose: str,
    require_private: bool = False,
) -> list[str]:
    """Resolve *host* once and return the list of validated IPs to pin to.

    For literal IP hosts, the single literal is validated and returned. For
    DNS hostnames, every resolved address must pass the block rules; the full
    set is returned so the connector can fall back across multiple A/AAAA
    records. Raises ``PinnedPeerError`` if any resolved address is blocked.
    """
    normalized_host = str(host or "").strip().lower().rstrip(".")
    if not normalized_host:
        raise PinnedPeerError(f"{purpose} must include host")

    bare = normalized_host.split("%", 1)[0]
    try:
        ipaddress.ip_address(bare)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        if not _allow_private_remote_urls() and _is_blocked_ip(bare):
            raise PinnedPeerError(f"{purpose} host '{normalized_host}' is not allowed")
        if require_private and not is_development_private_ip(bare):
            raise PinnedPeerError(f"{purpose} cleartext development endpoint must be private")
        return [_canonical_ip(bare)]

    try:
        resolved = _resolve_hostname_ips(normalized_host, port=port)
    except Exception as exc:
        raise PinnedPeerError(
            f"{purpose} host '{normalized_host}' cannot be resolved: {exc}"
        ) from exc
    if not resolved:
        raise PinnedPeerError(f"{purpose} host '{normalized_host}' cannot be resolved")
    if not _allow_private_remote_urls():
        for ip in resolved:
            if _is_blocked_ip(ip):
                raise PinnedPeerError(
                    f"{purpose} host '{normalized_host}' resolves to disallowed address '{ip}'"
                )
    elif require_private and any(not is_development_private_ip(ip) for ip in resolved):
        raise PinnedPeerError(
            f"{purpose} cleartext development endpoint must resolve only to private addresses"
        )
    # Containers commonly have IPv6 DNS connectivity disabled even when the
    # host publishes AAAA records. Prefer IPv4, while retaining every validated
    # address as a fallback for environments where IPv6 is available.
    canonical_ips = {_canonical_ip(ip) for ip in resolved}
    return sorted(
        canonical_ips,
        key=lambda ip: (ipaddress.ip_address(ip).version != 4, ipaddress.ip_address(ip).compressed),
    )


# ---------------------------------------------------------------------------
# urllib transport: pin the socket to a validated IP, keep Host header + SNI.
# ---------------------------------------------------------------------------


def _assert_peer_not_blocked(
    sock: socket.socket,
    *,
    purpose: str,
    allowed_ips: tuple[str, ...] = (),
    require_private: bool = False,
) -> None:
    try:
        peer = sock.getpeername()
    except Exception as exc:  # pragma: no cover - defensive
        raise PinnedPeerError(f"{purpose}: unable to read connected peer") from exc
    peer_host = str((peer or ("",))[0] or "").strip()
    canonical_peer = _canonical_ip(peer_host) if peer_host else ""
    canonical_allowed = tuple(_canonical_ip(ip) for ip in allowed_ips)
    if canonical_allowed and canonical_peer not in canonical_allowed:
        raise PinnedPeerError(f"{purpose}: connected peer is outside the pinned address set")
    if canonical_peer and require_private and not is_development_private_ip(canonical_peer):
        raise PinnedPeerError(f"{purpose}: cleartext connected peer is not private")
    if not _allow_private_remote_urls() and canonical_peer and _is_blocked_ip(canonical_peer):
        raise PinnedPeerError(f"{purpose}: connection resolved to disallowed address '{peer_host}'")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials a pre-validated IP and revalidates the peer."""

    _pinned_ips: tuple[str, ...] = ()
    _pin_purpose: str = "remote URL"
    _require_private: bool = False

    def connect(self) -> None:
        ips = self._pinned_ips or (self.host,)
        last_exc: Exception | None = None
        for ip in ips:
            try:
                self.sock = self._new_sock(ip)
                _assert_peer_not_blocked(
                    self.sock,
                    purpose=self._pin_purpose,
                    allowed_ips=self._pinned_ips,
                    require_private=self._require_private,
                )
                return
            except Exception as exc:
                if self.sock is not None:
                    with contextlib.suppress(Exception):
                        self.sock.close()
                    self.sock = None
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise PinnedPeerError(f"{self._pin_purpose}: no pinned address available")

    def _new_sock(self, ip: str) -> socket.socket:
        return socket.create_connection(
            (ip, self.port),
            self.timeout,
            self.source_address,  # type: ignore[attr-defined]
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials a validated IP but uses the real hostname
    for TLS SNI and certificate verification."""

    _pinned_ips: tuple[str, ...] = ()
    _pin_purpose: str = "remote URL"
    _require_private: bool = False

    def connect(self) -> None:
        ips = self._pinned_ips or (self.host,)
        context = self._context or ssl.create_default_context()  # type: ignore[attr-defined]
        server_hostname = self.host
        last_exc: Exception | None = None
        for ip in ips:
            raw_sock: socket.socket | None = None
            try:
                raw_sock = socket.create_connection(
                    (ip, self.port),
                    self.timeout,
                    self.source_address,  # type: ignore[attr-defined]
                )
                _assert_peer_not_blocked(
                    raw_sock,
                    purpose=self._pin_purpose,
                    allowed_ips=self._pinned_ips,
                    require_private=self._require_private,
                )
                self.sock = context.wrap_socket(raw_sock, server_hostname=server_hostname)
                return
            except Exception as exc:
                if self.sock is not None:
                    with contextlib.suppress(Exception):
                        self.sock.close()
                    self.sock = None
                elif raw_sock is not None:
                    with contextlib.suppress(Exception):
                        raw_sock.close()
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise PinnedPeerError(f"{self._pin_purpose}: no pinned address available")


def _pinned_connection_classes(
    ips: tuple[str, ...], *, purpose: str, require_private: bool = False
) -> tuple[type[http.client.HTTPConnection], type[http.client.HTTPSConnection]]:
    pinned = tuple(ips)

    class _HTTP(_PinnedHTTPConnection):
        _pinned_ips = pinned
        _pin_purpose = purpose
        _require_private = require_private

    class _HTTPS(_PinnedHTTPSConnection):
        _pinned_ips = pinned
        _pin_purpose = purpose
        _require_private = require_private

    return _HTTP, _HTTPS


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _ = (req, fp, code, msg, headers, newurl)


def build_pinned_opener(
    *,
    ips: tuple[str, ...],
    purpose: str,
    follow_redirects: bool = False,
    require_private: bool = False,
):
    """Build a urllib opener whose connections are pinned to *ips*."""
    http_cls, https_cls = _pinned_connection_classes(
        ips,
        purpose=purpose,
        require_private=require_private,
    )

    class _HTTPHandler(HTTPHandler):
        def http_open(self, req):
            return self.do_open(http_cls, req)

    class _HTTPSHandler(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(https_cls, req)

    # Explicitly disable environment/system proxy discovery. These sinks carry
    # credentials and must dial only the validated, pinned destination.
    tls_context = ssl.create_default_context()
    if not tls_context.check_hostname or tls_context.verify_mode != ssl.CERT_REQUIRED:
        raise RuntimeError("TLS certificate verification is required")
    handlers: list[Any] = [
        ProxyHandler({}),
        _HTTPHandler(),
        _HTTPSHandler(context=tls_context),
    ]
    if not follow_redirects:
        handlers.insert(0, _NoRedirectHandler)
    return urllib.request.build_opener(*handlers)


def open_pinned_url(
    request: Request,
    *,
    timeout_seconds: float,
    purpose: str,
    require_private: bool = False,
):
    """Validate, pin, and open *request* with no redirects.

    The request URL is preflight-validated, the host resolved + validated once,
    and the connection forced to one of those validated IPs (SNI/Host preserved).
    """
    url = request.full_url
    safe_url = assert_safe_remote_http_url(url, purpose=purpose)
    parsed = urlparse(safe_url)
    scheme = (parsed.scheme or "").lower()
    host = str(parsed.hostname or "")
    port = int(parsed.port or (443 if scheme == "https" else 80))
    ips = tuple(
        resolve_pinned_ips(
            host,
            port=port,
            purpose=purpose,
            require_private=require_private,
        )
    )
    opener = build_pinned_opener(
        ips=ips,
        purpose=purpose,
        require_private=require_private,
    )
    return opener.open(request, timeout=timeout_seconds)


# ---------------------------------------------------------------------------
# requests transport: pin via a custom HTTPAdapter.
# ---------------------------------------------------------------------------


def pinned_requests_session(url: str, *, purpose: str):
    """Return a ``requests.Session`` that pins outbound connections for *url*'s
    host to validated IPs. Raises ``ValueError`` if the URL/host is unsafe.

    The caller should use the original *url* (hostname preserved) so TLS SNI and
    certificate verification still target the real hostname; only the dialed IP
    is pinned. Redirects are not followed by default at the call site.
    """
    import requests

    safe_url = assert_safe_remote_http_url(url, purpose=purpose)
    parsed = urlparse(safe_url)
    scheme = (parsed.scheme or "").lower()
    host = str(parsed.hostname or "")
    port = int(parsed.port or (443 if scheme == "https" else 80))
    ips = resolve_pinned_ips(host, port=port, purpose=purpose)
    pinned_ip = ips[0]

    session = requests.Session()
    # _PinnedIPAdapter duck-types BaseAdapter (delegates via __getattr__) rather
    # than subclassing it, so mypy can't see the structural match.
    session.mount(
        f"{scheme}://",
        _PinnedIPAdapter(host=host, pinned_ip=pinned_ip, purpose=purpose),  # type: ignore[arg-type]
    )
    return session


class _PinnedIPAdapter:
    """A requests transport adapter that pins the dialed IP for one host.

    Implemented by wrapping urllib3's ``create_connection`` per send() call so the
    socket dials the validated IP while TLS SNI/cert verification keep the real
    hostname (urllib3 passes ``server_hostname`` from the request host).
    """

    def __init__(self, *, host: str, pinned_ip: str, purpose: str) -> None:
        from requests.adapters import HTTPAdapter

        self._host = host
        self._pinned_ip = pinned_ip
        self._purpose = purpose
        self._inner = HTTPAdapter()

    def send(self, *args, **kwargs):
        from urllib3.util import connection as urllib3_connection

        host = self._host
        pinned_ip = self._pinned_ip
        purpose = self._purpose
        original_create = urllib3_connection.create_connection

        def _pinned_create(address, *c_args, **c_kwargs):
            req_host, req_port = address
            target = (pinned_ip, req_port) if str(req_host) == host else address
            sock = original_create(target, *c_args, **c_kwargs)
            if not _allow_private_remote_urls():
                try:
                    peer_host = str((sock.getpeername() or ("",))[0] or "").strip()
                except Exception:
                    peer_host = ""
                if peer_host and _is_blocked_ip(peer_host):
                    sock.close()
                    raise PinnedPeerError(
                        f"{purpose}: connection resolved to disallowed address '{peer_host}'"
                    )
            return sock

        urllib3_connection.create_connection = _pinned_create
        try:
            return self._inner.send(*args, **kwargs)
        finally:
            urllib3_connection.create_connection = original_create

    def close(self) -> None:
        self._inner.close()

    # Delegate any other attributes requests may touch to the inner adapter.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# httpx transport: pin the connection to a validated IP for httpx-based clients
# (e.g. the OpenAI / Azure LLM clients that dial DB-backed custom base URLs).
#
# httpcore performs the TCP connect via its network backend, then runs the TLS
# handshake with ``server_hostname`` set to the *original* request host. By
# overriding only the backend's ``connect_tcp`` to dial a pre-validated pinned IP
# (instead of re-resolving the hostname), we defeat DNS rebinding while keeping
# correct SNI / certificate verification against the real hostname.
# ---------------------------------------------------------------------------


def _pinned_ip_for_backend(host: str, port: int, *, purpose: str) -> str:
    ips = resolve_pinned_ips(str(host or ""), port=int(port or 0), purpose=purpose)
    if not ips:
        raise PinnedPeerError(f"{purpose} host '{host}' did not resolve to an allowed address")
    return ips[0]


def pin_httpx_transport(transport: Any, *, purpose: str) -> Any:
    """Install an SSRF-pinning network backend on an existing httpx **sync**
    transport and return it.

    Every connection the transport opens dials a freshly re-validated IP at
    connect time (defeating DNS rebinding *before* any request bytes are sent),
    while httpcore still drives the TLS handshake with ``server_hostname`` set to
    the original request host so SNI / certificate verification stay correct.
    Honors the ``_allow_private_remote_urls`` escape hatch via
    :func:`resolve_pinned_ips`.
    """
    import httpcore

    class _PinnedSyncBackend(httpcore.SyncBackend):
        def connect_tcp(self, host, port, *args, **kwargs):
            pinned_ips = resolve_pinned_ips(str(host or ""), port=int(port or 0), purpose=purpose)
            last_error: Exception | None = None
            for pinned in pinned_ips:
                try:
                    return super().connect_tcp(pinned, port, *args, **kwargs)
                except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            raise PinnedPeerError(f"{purpose} host '{host}' did not resolve to an allowed address")

    # ConnectionPool builds connections lazily, so swapping the backend before any
    # request is issued pins every connection this transport opens.
    transport._pool._network_backend = _PinnedSyncBackend()
    return transport


def build_pinned_httpx_transport(*, purpose: str, **transport_kwargs: Any):
    """Return a synchronous ``httpx.HTTPTransport`` whose connections are pinned to
    validated IPs at connect time. Callers remain responsible for preflight URL
    validation (scheme / credentials / host) before issuing requests.
    """
    import httpx

    return pin_httpx_transport(httpx.HTTPTransport(**transport_kwargs), purpose=purpose)


def build_pinned_httpx_client(base_url: str, *, purpose: str, **client_kwargs: Any):
    """Return a synchronous ``httpx.Client`` whose connections are pinned to a
    validated IP for *base_url*'s host (defeats DNS rebinding at use time).

    The base URL is preflight-validated first; raises ``PinnedPeerError`` /
    ``ValueError`` for unsafe URLs. Honors the ``_allow_private_remote_urls``
    escape hatch via :func:`resolve_pinned_ips`.
    """
    import httpx

    assert_safe_remote_http_url(base_url, purpose=purpose)
    transport = build_pinned_httpx_transport(purpose=purpose, **client_kwargs)
    return httpx.Client(transport=transport)


def build_pinned_async_httpx_client(base_url: str, *, purpose: str, **client_kwargs: Any):
    """Async counterpart of :func:`build_pinned_httpx_client`."""
    import httpcore
    import httpx

    assert_safe_remote_http_url(base_url, purpose=purpose)

    class _PinnedAsyncBackend(httpcore.AnyIOBackend):
        async def connect_tcp(self, host, port, *args, **kwargs):
            pinned = _pinned_ip_for_backend(host, port, purpose=purpose)
            return await super().connect_tcp(pinned, port, *args, **kwargs)

    transport = httpx.AsyncHTTPTransport(**client_kwargs)
    transport._pool._network_backend = _PinnedAsyncBackend()
    return httpx.AsyncClient(transport=transport)
