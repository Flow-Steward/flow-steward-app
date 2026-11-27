"""ADR 0029, amendment 2026-11-27: the peers trusted to name the client, per ingress."""

from __future__ import annotations

import ipaddress

import pytest

from core.infrastructure.security.forwarded_trust import (
    UNTRUSTED_PLATFORM_WARNING,
    AttachedNetworks,
    TrustedProxyError,
    configuration_trust_warning,
    derive_forwarded_trust,
    environment_trust_warning,
    forwarded_client_address_check,
    parse_trusted_proxies,
    trusted_proxy_problems,
)

_COMPOSE_NETWORK = AttachedNetworks(
    networks=(
        ipaddress.ip_network("172.20.0.0/16"),
        ipaddress.ip_network("10.5.0.0/24"),
    ),
    gateway="172.20.0.1",
)
_NAMES = {
    "traefik": "172.20.0.3",
    "compact-runtime": "172.20.0.5",
    "flow-steward-compact-proxy": "10.5.0.4",
    "host.docker.internal": "192.168.65.254",
}


def _resolve(name: str):
    address = _NAMES.get(name)
    return (ipaddress.ip_address(address),) if address else ()


def _derive(environment: dict[str, str], **kwargs):
    return derive_forwarded_trust(
        environment, resolve=_resolve, attached=lambda: _COMPOSE_NETWORK, **kwargs
    )


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0.0/0",
        "::/0",
        "0.0.0.0/1,128.0.0.0/1",
        "::/1,8000::/1",
        "10.0.0.0/8,0.0.0.0/2,64.0.0.0/2,128.0.0.0/1",
    ],
)
def test_lists_that_add_up_to_every_address_are_refused(value: str) -> None:
    with pytest.raises(TrustedProxyError, match="wildcard network"):
        parse_trusted_proxies(value)
    assert trusted_proxy_problems(value.split(","))


@pytest.mark.parametrize("value", ["*", "not-an-address", "10.0.0.0/8,10.0.0.0/33"])
def test_unreadable_entries_are_refused_not_skipped(value: str) -> None:
    with pytest.raises(TrustedProxyError, match="not an address or network"):
        parse_trusted_proxies(value)


def test_a_large_but_partial_list_is_accepted() -> None:
    assert len(parse_trusted_proxies("0.0.0.0/1,10.0.0.0/8, ::1/128")) == 3
    assert trusted_proxy_problems(["127.0.0.1/32", "::1/128"]) == []


def test_bundled_traefik_on_compact_trusts_the_compose_network_range() -> None:
    trust = _derive(
        {"FS_FORWARDED_TRUST_MODE": "proxy_network", "FS_FORWARDED_TRUST_PEER": "compact-runtime"},
        legacy_when_unset=False,
    )

    assert trust.render() == "127.0.0.1,::1,172.20.0.0/16"
    assert trust.trusts("172.20.0.3")  # the bundled Traefik, whatever address it has now
    assert trust.trusts("172.20.0.77")
    assert not trust.trusts("10.5.0.2")
    assert not trust.trusts("203.0.113.7")
    assert trust.warnings == ()


def test_bundled_traefik_on_scalable_trusts_its_exact_address() -> None:
    trust = _derive({"FS_FORWARDED_TRUST_MODE": "bundled_proxy"})

    assert trust.exact_hosts() == {"127.0.0.1", "::1", "172.20.0.3"}
    assert trust.wider_networks() == ()
    assert not trust.trusts("172.20.0.1")  # not the gateway


def test_a_docker_proxy_network_is_found_through_the_runtime_alias() -> None:
    trust = _derive(
        {
            "FS_FORWARDED_TRUST_MODE": "proxy_network",
            "FS_FORWARDED_TRUST_PEER": "flow-steward-compact-proxy",
        }
    )

    assert trust.wider_networks() == (ipaddress.ip_network("10.5.0.0/24"),)
    assert not trust.trusts("172.20.0.3")


def test_an_unresolvable_proxy_network_warns_and_trusts_nothing_extra() -> None:
    trust = _derive({"FS_FORWARDED_TRUST_MODE": "proxy_network", "FS_FORWARDED_TRUST_PEER": "x"})

    assert trust.render() == "127.0.0.1,::1"
    assert "proxy network could not be identified" in trust.warnings[0]


def test_a_host_proxy_trusts_the_bridge_gateway_only() -> None:
    trust = _derive({"FS_FORWARDED_TRUST_MODE": "host_gateway"})

    assert trust.exact_hosts() == {"127.0.0.1", "::1", "172.20.0.1", "192.168.65.254"}
    assert not trust.trusts("172.20.0.3")


def test_a_platform_trusts_only_the_operator_and_warns_until_it_names_a_proxy() -> None:
    unset = _derive({"FS_FORWARDED_TRUST_MODE": "operator"})
    assert unset.render() == "127.0.0.1,::1"
    assert unset.warnings == (UNTRUSTED_PLATFORM_WARNING,)
    assert "share one bucket" in UNTRUSTED_PLATFORM_WARNING

    named = _derive(
        {"FS_FORWARDED_TRUST_MODE": "operator", "FS_PROXY_TRUSTED_IPS": "100.64.0.0/10"}
    )
    assert named.trusts("100.64.3.4")
    assert named.warnings == ()


def test_compact_without_a_mode_is_a_platform() -> None:
    trust = _derive({}, legacy_when_unset=False)

    assert trust.mode == "operator"
    assert trust.warnings == (UNTRUSTED_PLATFORM_WARNING,)


def test_an_unmanaged_runtime_keeps_its_previous_trust() -> None:
    trust = _derive({})

    assert trust.mode == "unmanaged"
    assert trust.exact_hosts() == {
        "127.0.0.1",
        "::1",
        "172.20.0.3",
        "172.20.0.1",
        "192.168.65.254",
    }
    uvicorn = _derive({}, legacy_host_gateway=False)
    assert uvicorn.render() == "127.0.0.1,::1,172.20.0.3"


def test_the_operator_list_always_adds() -> None:
    trust = _derive(
        {"FS_FORWARDED_TRUST_MODE": "bundled_proxy", "FS_PROXY_TRUSTED_IPS": "198.51.100.0/24"}
    )

    assert trust.trusts("198.51.100.20")
    assert trust.trusts("172.20.0.3")


def test_a_bad_operator_list_refuses_at_start_and_fails_closed_at_run_time() -> None:
    environment = {
        "FS_FORWARDED_TRUST_MODE": "bundled_proxy",
        "FS_PROXY_TRUSTED_IPS": "0.0.0.0/1,128.0.0.0/1",
    }

    with pytest.raises(TrustedProxyError):
        _derive(environment)
    lenient = _derive(environment, strict=False)
    assert not lenient.trusts("203.0.113.7")
    assert lenient.trusts("172.20.0.3")
    assert "ignored" in lenient.warnings[0]


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(TrustedProxyError, match="FS_FORWARDED_TRUST_MODE"):
        _derive({"FS_FORWARDED_TRUST_MODE": "everyone"})


def test_attached_networks_are_read_from_the_kernel_routes() -> None:
    from core.infrastructure.security import forwarded_trust

    route = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000014AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth1\t0000050A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )
    networks, gateway = forwarded_trust._route_networks_v4(route)

    assert gateway == "172.20.0.1"
    assert networks == [
        ipaddress.ip_network("172.20.0.0/16"),
        ipaddress.ip_network("10.5.0.0/24"),
    ]
    v6 = forwarded_trust._route_networks_v6(
        "fd000000000000000000000000000000 40 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000100 00000001 00000000 00000001 eth0\n"
        "00000000000000000000000000000001 80 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000000 00000001 00000000 00200001 lo\n"
    )
    assert v6 == [ipaddress.ip_network("fd00::/64")]


def test_status_and_doctor_warn_only_for_a_platform_without_a_proxy() -> None:
    assert configuration_trust_warning("platform", ("127.0.0.1/32", "::1/128"))
    assert not configuration_trust_warning("platform", ("100.64.0.0/10",))
    assert not configuration_trust_warning("bundled_traefik", ("127.0.0.1/32",))
    assert environment_trust_warning({})
    assert not environment_trust_warning({"FS_FORWARDED_TRUST_MODE": "proxy_network"})
    assert not environment_trust_warning({"FS_PROXY_TRUSTED_IPS": "100.64.0.0/10"})
    check = forwarded_client_address_check(UNTRUSTED_PLATFORM_WARNING)
    assert check["id"] == "forwarded_client_address"
    assert check["status"] == "warn"


_OVERLAYS = {
    "compact/ingress-bundled_traefik.yaml": ("compact-runtime", "proxy_network", "compact-runtime"),
    "compact/ingress-external_traefik.yaml": (
        "compact-runtime",
        "proxy_network",
        "${COMPOSE_PROJECT_NAME:-flow-steward}-compact",
    ),
    "compact/ingress-external_proxy-docker.yaml": (
        "compact-runtime",
        "proxy_network",
        "${FS_COMPOSE_PROJECT_NAME:-flow-steward}-compact-proxy",
    ),
    "compact/ingress-external_proxy-host.yaml": ("compact-runtime", "host_gateway", None),
    "compact/ingress-platform.yaml": ("compact-runtime", "operator", None),
    "scalable/ingress-external_traefik.yaml": (
        "fs-app",
        "proxy_network",
        "${COMPOSE_PROJECT_NAME:-flow-steward}-app",
    ),
    "scalable/ingress-external_proxy-docker.yaml": (
        "fs-app",
        "proxy_network",
        "${FS_COMPOSE_PROJECT_NAME:-flow-steward}-app-proxy",
    ),
    "scalable/ingress-external_proxy-host.yaml": ("fs-app", "host_gateway", None),
    "docker-compose.prod.yaml": ("fs-app", "bundled_proxy", None),
}


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_each_ingress_overlay_names_its_trust(overlay: str) -> None:
    """The peer is the runtime's own alias on the network its proxy uses."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2] / "infra/docker"
    service, mode, peer = _OVERLAYS[overlay]
    document = yaml.safe_load((root / overlay).read_text(encoding="utf-8"))
    environment = document["services"][service]["environment"]

    assert environment["FS_FORWARDED_TRUST_MODE"] == mode
    assert environment.get("FS_FORWARDED_TRUST_PEER") == peer
    if peer and peer != "compact-runtime":
        networks = document["services"][service]["networks"]
        aliases = [alias for net in networks.values() if net for alias in net.get("aliases", ())]
        assert peer in aliases
