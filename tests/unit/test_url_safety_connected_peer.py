"""SSRF connected-peer pinning regression tests (FS-DEEP-CAND-007/008/011/012/
041/050/051/009/049).

These exercise the shared ``url_safety`` guard that resolves a host once,
validates every resolved IP, and pins the socket connection to a validated IP
(preserving Host header + TLS SNI) so DNS rebinding between the preflight and the
actual connect cannot reach an internal target. All network primitives are
mocked so no real sockets/DNS are used.
"""

from __future__ import annotations

import socket

import pytest

from core.application.extensions import url_safety

# ---------------------------------------------------------------------------
# Preflight validation (assert_safe_remote_http_url)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "ftp://example.com/x",
        "http://user:pass@example.com/x",
    ],
)
def test_assert_safe_remote_http_url_rejects_unsafe(monkeypatch, url):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    with pytest.raises(ValueError):
        url_safety.assert_safe_remote_http_url(url, purpose="test")


def test_assert_safe_remote_http_url_rejects_public_host_resolving_private(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"10.0.0.5"},
    )
    with pytest.raises(ValueError, match="disallowed address"):
        url_safety.assert_safe_remote_http_url("https://evil.example.com/x", purpose="test")


def test_assert_safe_remote_http_url_allows_public(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"93.184.216.34"},
    )
    out = url_safety.assert_safe_remote_http_url("https://example.com/x", purpose="test")
    assert out == "https://example.com/x"


def test_assert_safe_remote_https_url_rejects_plain_http(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with pytest.raises(ValueError, match="https"):
        url_safety.assert_safe_remote_https_url("http://1.1.1.1/runtime", purpose="runtime")


# ---------------------------------------------------------------------------
# resolve_pinned_ips
# ---------------------------------------------------------------------------


def test_resolve_pinned_ips_blocks_private_dns_answer(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"93.184.216.34", "10.0.0.5"},
    )
    with pytest.raises(url_safety.PinnedPeerError, match="disallowed address"):
        url_safety.resolve_pinned_ips("mixed.example.com", port=443, purpose="test")


def test_resolve_pinned_ips_returns_validated_public_ips(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"93.184.216.34"},
    )
    ips = url_safety.resolve_pinned_ips("example.com", port=443, purpose="test")
    assert ips == ["93.184.216.34"]


def test_resolve_pinned_ips_prefers_ipv4_when_dns_returns_ipv6_first(monkeypatch):
    # Globally routable addresses: documentation ranges (2001:db8::/32, TEST-NET-3)
    # are not global, and the guard rightly refuses them before any ordering. The
    # IPv6 one sorts first as text, so only the IPv4-first rule puts 8.8.8.8 ahead.
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"2606:4700:4700::1111", "8.8.8.8"},
    )
    assert url_safety.resolve_pinned_ips("dual-stack.example.com", port=443, purpose="test") == [
        "8.8.8.8",
        "2606:4700:4700::1111",
    ]


def test_resolve_pinned_ips_literal_private_blocked(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    with pytest.raises(url_safety.PinnedPeerError):
        url_safety.resolve_pinned_ips("127.0.0.1", port=80, purpose="test")


def test_resolve_pinned_ips_allow_private_escape_hatch(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"127.0.0.1"},
    )
    # Escape hatch: private allowed for dev/test.
    assert url_safety.resolve_pinned_ips("localhost", port=80, purpose="test") == ["127.0.0.1"]


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "0.0.0.0", "224.0.0.1", "192.0.2.10", "fe80::1", "::"],
)
def test_cleartext_dev_pin_rejects_non_private_special_addresses(monkeypatch, address):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with pytest.raises(url_safety.PinnedPeerError, match="must be private"):
        url_safety.resolve_pinned_ips(
            address,
            port=80,
            purpose="test",
            require_private=True,
        )


def test_cleartext_dev_pin_rejects_dns_with_link_local_answer(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"10.0.0.5", "169.254.169.254"},
    )
    with pytest.raises(url_safety.PinnedPeerError, match="only to private"):
        url_safety.resolve_pinned_ips(
            "dev.example.test",
            port=80,
            purpose="test",
            require_private=True,
        )


def test_peer_pin_normalizes_equivalent_ipv6_forms(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    sock = _FakeSocket(peer_ip="fd00::1")
    url_safety._assert_peer_not_blocked(
        sock,
        purpose="test",
        allowed_ips=("fd00:0:0:0:0:0:0:1",),
        require_private=True,
    )


# ---------------------------------------------------------------------------
# Connection-time peer pinning: simulate DNS rebinding (preflight resolves
# public, connect-time peer is private) and assert it is blocked before any
# data is exchanged.
# ---------------------------------------------------------------------------


class _FakeSocket:
    def __init__(self, peer_ip: str) -> None:
        self._peer_ip = peer_ip
        self.closed = False

    def getpeername(self):
        return (self._peer_ip, 443)

    def close(self):
        self.closed = True


def test_pinned_connection_blocks_rebound_private_peer(monkeypatch):
    """preflight/pin resolves a public IP, but the socket actually connects to a
    private peer (rebinding/route change) — the peer check must reject it and
    close the socket before any request is sent."""
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    created: list[_FakeSocket] = []

    def _fake_create_connection(address, *args, **kwargs):
        # Pinned IP is public, but the actual connected peer is private.
        sock = _FakeSocket(peer_ip="10.0.0.9")
        created.append(sock)
        return sock

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)

    http_cls, _https_cls = url_safety._pinned_connection_classes(("93.184.216.34",), purpose="test")
    conn = http_cls("example.com", 443)
    with pytest.raises(url_safety.PinnedPeerError, match="outside the pinned address set"):
        conn.connect()
    assert created
    assert created[0].closed is True


def test_pinned_connection_allows_public_peer(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    def _fake_create_connection(address, *args, **kwargs):
        # Dialed pinned IP; connected peer is the same public IP.
        assert address[0] == "93.184.216.34"
        return _FakeSocket(peer_ip="93.184.216.34")

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)

    http_cls, _https_cls = url_safety._pinned_connection_classes(("93.184.216.34",), purpose="test")
    conn = http_cls("example.com", 443)
    conn.connect()  # should not raise
    assert conn.sock is not None


def test_open_pinned_url_blocks_rebinding_before_network(monkeypatch):
    """End-to-end through the opener: a host that passes preflight as public but
    rebinds to a private peer at connect time is blocked with no HTTP exchange."""
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    # Preflight + pin resolve to a public IP.
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"93.184.216.34"},
    )

    # But the real socket connects to a private (rebound) peer.
    def _fake_create_connection(address, *args, **kwargs):
        return _FakeSocket(peer_ip="169.254.169.254")

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)

    from urllib.request import Request

    req = Request("http://rebind.example.com/x")
    with pytest.raises(url_safety.PinnedPeerError):
        url_safety.open_pinned_url(req, timeout_seconds=1.0, purpose="test")


def test_open_pinned_url_rejects_private_url_at_preflight(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    def _must_not_connect(*_a, **_k):  # pragma: no cover - guard
        raise AssertionError("socket must not be opened for a blocked URL")

    monkeypatch.setattr(socket, "create_connection", _must_not_connect)

    from urllib.request import Request

    req = Request("http://127.0.0.1/x")
    with pytest.raises(ValueError):
        url_safety.open_pinned_url(req, timeout_seconds=1.0, purpose="test")


# ---------------------------------------------------------------------------
# Store-time validation: extension_runtime connection remote_base_url (CAND-050)
# and model provider base_url (CAND-058) reject unsafe URLs before persistence.
# ---------------------------------------------------------------------------


def test_validate_remote_url_rejects_private_base_url_at_store_time(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    from core.application.extensions.service_support import validate_remote_url

    with pytest.raises(ValueError):
        validate_remote_url("http://169.254.169.254/", purpose="remote_base_url")


def test_validate_remote_url_rejects_dns_private_base_url_at_store_time(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"10.1.2.3"},
    )
    from core.application.extensions.service_support import validate_remote_url

    with pytest.raises(ValueError):
        validate_remote_url("https://internal.example.com/", purpose="remote_base_url")


# ---------------------------------------------------------------------------
# requests transport (identity-provider client, CAND-029): the dialed IP is rewritten to the pinned,
# validated IP while the hostname is preserved; a private DNS answer is rejected.
# ---------------------------------------------------------------------------


def test_pinned_requests_session_rewrites_dialed_ip_to_pinned(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    import urllib3.util.connection as u3

    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"93.184.216.34"},
    )

    dialed: dict = {}

    class _FakeConnSock:
        def getpeername(self):
            return ("93.184.216.34", 443)

        def close(self):
            return None

    def _fake_create(address, *args, **kwargs):
        dialed["address"] = address
        return _FakeConnSock()

    # The adapter captures urllib3.create_connection at send() time, so patch it
    # before building the session.
    monkeypatch.setattr(u3, "create_connection", _fake_create)

    session = url_safety.pinned_requests_session(
        "https://idp.example.com/.well-known/openid-configuration",
        purpose="identity provider discovery_url",
    )
    adapter = session.adapters["https://"]

    # Drive the pinned create_connection exactly as the real transport would,
    # using the original hostname (SNI/cert hostname preserved).
    captured = {}

    def _inner_send(*_a, **_k):
        captured["sock"] = u3.create_connection(("idp.example.com", 443))
        return captured["sock"]

    adapter._inner.send = _inner_send
    adapter.send()

    # Host rewritten to the pinned validated IP; hostname is not dialed directly.
    assert dialed["address"] == ("93.184.216.34", 443)


def test_pinned_requests_session_blocks_private_dns(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        url_safety._descriptor_io,
        "_resolve_hostname_ips",
        lambda _host, *, port: {"169.254.169.254"},
    )
    with pytest.raises(ValueError):
        url_safety.pinned_requests_session("https://idp.example.com/", purpose="identity provider")
