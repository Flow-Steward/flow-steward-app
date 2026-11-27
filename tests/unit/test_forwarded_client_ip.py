"""The client address Core reads, through each shipped ingress chain (ADR 0029, 2026-11-27).

Every per-client limit and audit address comes from `request_client_ip`. Each case is
the request as it reaches Core: the immediate peer and the X-Forwarded-For the chain
in front of it wrote. A spoofed header is one the client sent itself.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from core.application.auth import serve_auth_runtime
from core.infrastructure.security.forwarded_trust import AttachedNetworks

CLIENT = "203.0.113.7"
SPOOF = "6.6.6.6"
_NETWORKS = AttachedNetworks(
    networks=(ipaddress.ip_network("172.20.0.0/16"), ipaddress.ip_network("10.5.0.0/24")),
    gateway="172.20.0.1",
)


class _Request:
    def __init__(self, peer: str, forwarded_for: str = "") -> None:
        self.client = SimpleNamespace(host=peer)
        self.headers = {"x-forwarded-for": forwarded_for} if forwarded_for else {}


@pytest.fixture
def names(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    resolved = {
        "traefik": "172.20.0.3",
        "compact-runtime": "172.20.0.5",
        "flow-steward-app-proxy": "10.5.0.4",
    }
    monkeypatch.setattr(
        serve_auth_runtime,
        "resolve_addresses",
        lambda name: (ipaddress.ip_address(resolved[name]),) if name in resolved else (),
    )
    monkeypatch.setattr(serve_auth_runtime, "attached_networks", lambda: _NETWORKS)
    for name in ("FS_FORWARDED_TRUST_MODE", "FS_FORWARDED_TRUST_PEER", "FS_PROXY_TRUSTED_IPS"):
        monkeypatch.delenv(name, raising=False)
    serve_auth_runtime.reset_forwarded_trust()
    yield resolved
    serve_auth_runtime.reset_forwarded_trust()


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str, peer: str = "", trusted: str = "") -> None:
    monkeypatch.setenv("FS_FORWARDED_TRUST_MODE", mode)
    if peer:
        monkeypatch.setenv("FS_FORWARDED_TRUST_PEER", peer)
    if trusted:
        monkeypatch.setenv("FS_PROXY_TRUSTED_IPS", trusted)
    serve_auth_runtime.reset_forwarded_trust()


def test_compact_bundled_traefik_reaches_core_with_the_real_client(names, monkeypatch) -> None:
    # Outer Traefik (172.20.0.3) -> internal router (127.0.0.1) -> app. The router now
    # trusts the Compose network, so it keeps the client and appends Traefik.
    _mode(monkeypatch, "proxy_network", "compact-runtime")

    assert (
        serve_auth_runtime.request_client_ip(_Request("127.0.0.1", f"{CLIENT}, 172.20.0.3"))
        == CLIENT
    )
    # A Traefik restarted with another address on the same network is still trusted.
    assert (
        serve_auth_runtime.request_client_ip(_Request("127.0.0.1", f"{CLIENT}, 172.20.0.9"))
        == CLIENT
    )
    # The client's own header is ignored: Traefik overwrote it, and an extra hop on
    # the left is not the rightmost untrusted one.
    assert (
        serve_auth_runtime.request_client_ip(
            _Request("127.0.0.1", f"{SPOOF}, {CLIENT}, 172.20.0.3")
        )
        == CLIENT
    )


def test_scalable_bundled_traefik_and_a_spoofing_neighbour(names, monkeypatch) -> None:
    _mode(monkeypatch, "bundled_proxy")

    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.3", CLIENT)) == CLIENT
    # Another container on the network is not the proxy: its header names nobody.
    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.8", SPOOF)) == "172.20.0.8"


def test_the_bundled_proxy_is_resolved_again_after_it_moves(names, monkeypatch) -> None:
    _mode(monkeypatch, "bundled_proxy")
    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.3", CLIENT)) == CLIENT

    names["traefik"] = "172.20.0.11"  # restarted with a new address
    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.11", CLIENT)) == "172.20.0.11"
    cache = serve_auth_runtime._FORWARDED_TRUST
    cache._derived_at -= cache.ttl_seconds + 1  # the refresh interval passes
    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.11", CLIENT)) == CLIENT
    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.3", SPOOF)) == "172.20.0.3"


def test_a_host_proxy_through_the_loopback_port_is_trusted_as_the_gateway(
    names, monkeypatch
) -> None:
    _mode(monkeypatch, "host_gateway")

    # nginx appends the client to whatever the client sent.
    assert (
        serve_auth_runtime.request_client_ip(_Request("172.20.0.1", f"{SPOOF}, {CLIENT}")) == CLIENT
    )
    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.3", SPOOF)) == "172.20.0.3"


def test_a_docker_proxy_on_the_proxy_network(names, monkeypatch) -> None:
    _mode(monkeypatch, "proxy_network", "flow-steward-app-proxy")

    assert (
        serve_auth_runtime.request_client_ip(_Request("10.5.0.2", f"{SPOOF}, {CLIENT}")) == CLIENT
    )
    assert serve_auth_runtime.request_client_ip(_Request("172.20.0.8", SPOOF)) == "172.20.0.8"


def test_a_platform_needs_the_operator_to_name_its_proxy(names, monkeypatch, caplog) -> None:
    # The router replaces the header with the platform proxy (100.64.0.3) it does not trust.
    _mode(monkeypatch, "operator")
    with caplog.at_level("WARNING"):
        assert (
            serve_auth_runtime.request_client_ip(_Request("127.0.0.1", "100.64.0.3"))
            == "100.64.0.3"
        )
    assert "share one bucket" in caplog.text

    _mode(monkeypatch, "operator", trusted="100.64.0.0/10")
    assert (
        serve_auth_runtime.request_client_ip(
            _Request("127.0.0.1", f"{SPOOF}, {CLIENT}, 100.64.0.3")
        )
        == CLIENT
    )


def test_a_split_wildcard_is_not_trusted_at_run_time(names, monkeypatch, caplog) -> None:
    _mode(monkeypatch, "operator", trusted="0.0.0.0/1,128.0.0.0/1")

    with caplog.at_level("WARNING"):
        assert (
            serve_auth_runtime.request_client_ip(_Request("198.51.100.9", SPOOF)) == "198.51.100.9"
        )
    assert "wildcard network" in caplog.text


def test_loopback_development_is_unchanged(names) -> None:
    assert (
        serve_auth_runtime.request_client_ip(_Request("127.0.0.1", f"10.0.0.5, {CLIENT}")) == CLIENT
    )
    assert serve_auth_runtime.request_client_ip(_Request("127.0.0.1")) == "127.0.0.1"
    assert serve_auth_runtime.request_client_ip(_Request("198.51.100.9", SPOOF)) == "198.51.100.9"
