"""Which peers may name the client address (ADR 0029, amendment of 2026-11-27).

Every per-client limit -- sign-in attempts, password reset, member lookup, external
sign-in -- and every audit address is keyed by the client address Core reads from
``X-Forwarded-For``. A header is only as good as the peer that wrote it, so the list of
peers trusted to write it decides whether each visitor counts separately (the proxy in
front is trusted) or every visitor is one address (it is not), and whether a client can
name any address it likes (a peer that is not a proxy is trusted).

The proxy in front differs per ingress, so the trust is derived from it rather than
written by hand. The Compose ingress overlays set two variables for the runtime:

``FS_FORWARDED_TRUST_MODE``
    ``bundled_proxy``  -- the proxy is a service on the Compose network; trust the
                          address its name (``FS_FORWARDED_TRUST_PEER``, default
                          ``traefik``) resolves to.
    ``proxy_network``  -- the proxy reaches the runtime over a Docker network; trust that
                          network's range. ``FS_FORWARDED_TRUST_PEER`` is the runtime's own
                          alias on it, which names the network without knowing the proxy.
    ``host_gateway``   -- the proxy runs on the Docker host and reaches a port published on
                          127.0.0.1, so the runtime sees the bridge gateway; trust it. Safe
                          only because nothing outside the host can reach that port.
    ``operator``       -- a hosting platform's proxy, which only the operator can name.
    unset              -- an unmanaged or development runtime: what was trusted before
                          (the bundled proxy, Docker's host aliases and the gateway).

``FS_PROXY_TRUSTED_IPS`` -- the operator's own addresses and networks -- is always added.
A list that cannot be read, or whose networks add up to every IPv4 or every IPv6 address
(``0.0.0.0/0``, or ``0.0.0.0/1,128.0.0.0/1``), is refused: trusting everyone lets every
client choose its own address, and every per-client limit with it.

This module is standard library only: the Compact supervisor, the uvicorn start script
and Core's request handling all read the same rules.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

CONFIGURED_ENV = "FS_PROXY_TRUSTED_IPS"
TRUST_MODE_ENV = "FS_FORWARDED_TRUST_MODE"
TRUST_PEER_ENV = "FS_FORWARDED_TRUST_PEER"

MODE_BUNDLED_PROXY = "bundled_proxy"
MODE_PROXY_NETWORK = "proxy_network"
MODE_HOST_GATEWAY = "host_gateway"
MODE_OPERATOR = "operator"
TRUST_MODES = frozenset({MODE_BUNDLED_PROXY, MODE_PROXY_NETWORK, MODE_HOST_GATEWAY, MODE_OPERATOR})

BUNDLED_PROXY_HOST = "traefik"
_HOST_ALIASES = ("host.docker.internal", "gateway.docker.internal")
LOOPBACK: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("127.0.0.1/32"),
    ipaddress.ip_network("::1/128"),
)

PROXY_TRUSTED_IPS_DESCRIPTION = (
    "Extra comma-separated addresses or networks of reverse proxies allowed to name the "
    "client address, which per-client sign-in limits and audit entries use. The proxy of "
    "each ingress is trusted without it (bundled Traefik, the proxy network, the host "
    "gateway); set it for a hosting platform's proxy. Networks that cover every address "
    "(0.0.0.0/0, or halves such as 0.0.0.0/1,128.0.0.0/1) and unreadable entries are "
    "rejected."
)

UNTRUSTED_PLATFORM_WARNING = (
    "No proxy is trusted to name the client address (FS_PROXY_TRUSTED_IPS lists only "
    "loopback). Behind a hosting platform's proxy every visitor then has the proxy's "
    "address, so per-client limits -- sign-in attempts, password reset, member lookup, "
    "external sign-in -- share one bucket and audit entries show the proxy. Set "
    "FS_PROXY_TRUSTED_IPS to the addresses or networks the platform's proxy connects from."
)


class TrustedProxyError(ValueError):
    """A trusted-proxy list that must not be used: unreadable, or trusting everyone."""


def whole_address_space_problem(networks: Iterable[IPNetwork]) -> str:
    """Name the address family the networks cover completely, or return ""."""
    by_version: dict[int, list[IPNetwork]] = {4: [], 6: []}
    for network in networks:
        by_version[network.version].append(network)
    for version, members in by_version.items():
        if not members:
            continue
        collapsed = ipaddress.collapse_addresses(members)  # type: ignore[type-var]
        if any(network.prefixlen == 0 for network in collapsed):
            space = "0.0.0.0/0" if version == 4 else "::/0"
            return (
                f"{CONFIGURED_ENV} must not trust every IPv{version} address: its networks "
                f"add up to the wildcard network {space}, which would let every client "
                "name its own address"
            )
    return ""


def parse_trusted_proxies(raw: str | Iterable[str]) -> tuple[IPNetwork, ...]:
    """Read an operator list strictly: every entry an address or network, not everyone."""
    entries = raw.split(",") if isinstance(raw, str) else [str(item) for item in raw]
    networks: list[IPNetwork] = []
    for entry in entries:
        text = entry.strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise TrustedProxyError(
                f"{CONFIGURED_ENV} entry {text!r} is not an address or network"
            ) from exc
    problem = whole_address_space_problem(networks)
    if problem:
        raise TrustedProxyError(problem)
    return tuple(networks)


def trusted_proxy_problems(entries: Iterable[str]) -> list[str]:
    """The configuration's view: what is wrong with a list, empty when it is usable."""
    try:
        parse_trusted_proxies(list(entries))
    except TrustedProxyError as exc:
        return [str(exc)]
    return []


def operator_names_a_proxy(entries: Iterable[str]) -> bool:
    """True when the operator's list trusts something beyond loopback."""
    try:
        networks = parse_trusted_proxies(list(entries))
    except TrustedProxyError:
        return False
    return any(not network.is_loopback for network in networks)


def configuration_trust_warning(ingress_mode: str, entries: Iterable[str]) -> str:
    """The warning `status`/`doctor` show for a configuration, or "".

    Only a hosting platform leaves the proxy for the operator to name: every other
    ingress derives it (bundled Traefik, the proxy network, the host gateway).
    """
    if ingress_mode != "platform" or operator_names_a_proxy(entries):
        return ""
    return UNTRUSTED_PLATFORM_WARNING


def environment_trust_warning(environment: Mapping[str, str]) -> str:
    """The same warning from a runtime's environment (a platform runtime's `status`).

    A runtime started without a Compose overlay -- what a hosting platform does -- has
    no trust mode, and its operator is the only one who can name the proxy in front.
    """
    mode = str(environment.get(TRUST_MODE_ENV) or "").strip().lower() or MODE_OPERATOR
    if mode != MODE_OPERATOR:
        return ""
    entries = str(environment.get(CONFIGURED_ENV) or "").split(",")
    return "" if operator_names_a_proxy(entries) else UNTRUSTED_PLATFORM_WARNING


def forwarded_client_address_check(warning: str) -> dict[str, str]:
    """A readiness check (`status`, `doctor`) carrying a client-address warning."""
    return {
        "id": "forwarded_client_address",
        "category": "core",
        "title": "Client addresses behind the proxy",
        "status": "warn",
        "detail": warning,
        "remediation": (
            "Name the platform proxy's addresses or networks: in the FS_PROXY_TRUSTED_IPS "
            "variable of the runtime service on a hosting platform, or with "
            "`flow-steward config set --proxy-trusted-ips <list>` and `flow-steward "
            "config apply` on a host."
        ),
    }


# -- what the container can see ------------------------------------------------------


def _route_networks_v4(text: str) -> tuple[list[IPNetwork], str]:
    networks: list[IPNetwork] = []
    gateway = ""
    for raw_line in text.splitlines()[1:]:
        fields = raw_line.split()
        if len(fields) < 8:
            continue
        try:
            destination = int.from_bytes(bytes.fromhex(fields[1]), "little")
            via = int.from_bytes(bytes.fromhex(fields[2]), "little")
            mask = int.from_bytes(bytes.fromhex(fields[7]), "little")
        except ValueError:
            continue
        if destination == 0 and mask == 0:
            if via and not gateway:
                gateway = str(ipaddress.IPv4Address(via))
            continue
        if via:
            continue  # a routed network, not one the container is attached to
        prefix = bin(mask).count("1")
        try:
            networks.append(ipaddress.IPv4Network((destination, prefix), strict=False))
        except ValueError:
            continue
    return networks, gateway


def _route_networks_v6(text: str) -> list[IPNetwork]:
    networks: list[IPNetwork] = []
    for raw_line in text.splitlines():
        fields = raw_line.split()
        if len(fields) < 10 or fields[9] == "lo":
            continue
        try:
            prefix = int(fields[1], 16)
            destination = ipaddress.IPv6Address(int(fields[0], 16))
            via = int(fields[4], 16)
        except ValueError:
            continue
        if prefix == 0 or via or prefix == 128 or destination.is_multicast:
            continue
        networks.append(ipaddress.IPv6Network((destination, prefix), strict=False))
    return networks


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


@dataclass(frozen=True)
class AttachedNetworks:
    """The networks this container is attached to, and its default gateway."""

    networks: tuple[IPNetwork, ...] = ()
    gateway: str = ""

    @classmethod
    def read(cls) -> AttachedNetworks:
        v4, gateway = _route_networks_v4(_read("/proc/net/route"))
        v6 = _route_networks_v6(_read("/proc/net/ipv6_route"))
        return cls(networks=tuple(v4 + v6), gateway=gateway)

    def containing(self, address: IPAddress) -> IPNetwork | None:
        matches = [network for network in self.networks if address in network]
        return max(matches, key=lambda network: network.prefixlen, default=None)


def resolve_addresses(hostname: str) -> tuple[IPAddress, ...]:
    """Every address a name resolves to; empty when it does not resolve."""
    try:
        answers = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return ()
    found: list[IPAddress] = []
    for answer in answers:
        try:
            address = ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if address not in found:
            found.append(address)
    return tuple(found)


def _single(address: IPAddress) -> IPNetwork:
    return ipaddress.ip_network(address)


# -- the derived trust ---------------------------------------------------------------


@dataclass(frozen=True)
class ForwardedTrust:
    mode: str
    networks: tuple[IPNetwork, ...]
    warnings: tuple[str, ...] = ()

    def trusts(self, value: str) -> bool:
        try:
            address = ipaddress.ip_address(str(value or "").strip())
        except ValueError:
            return False
        return any(address in network for network in self.networks)

    def exact_hosts(self) -> set[str]:
        return {
            str(network.network_address) for network in self.networks if network.num_addresses == 1
        }

    def wider_networks(self) -> tuple[IPNetwork, ...]:
        return tuple(network for network in self.networks if network.num_addresses > 1)

    def render(self) -> str:
        """Comma-separated, single addresses bare -- for uvicorn and Traefik."""
        return ",".join(
            str(network.network_address) if network.num_addresses == 1 else str(network)
            for network in self.networks
        )


def _add(target: list[IPNetwork], network: IPNetwork) -> None:
    covered = any(
        existing.version == network.version
        and network.network_address in existing
        and network.broadcast_address in existing
        for existing in target
    )
    if not covered:
        target.append(network)


def derive_forwarded_trust(
    environment: Mapping[str, str],
    *,
    strict: bool = True,
    legacy_when_unset: bool = True,
    legacy_host_gateway: bool = True,
    resolve: Callable[[str], Sequence[IPAddress]] = resolve_addresses,
    attached: Callable[[], AttachedNetworks] = AttachedNetworks.read,
) -> ForwardedTrust:
    """Derive the peers trusted to name the client from the runtime's environment.

    ``strict`` raises :class:`TrustedProxyError` for an unusable operator list (startup
    refuses it); otherwise the operator list is dropped with a warning, which fails
    closed. ``legacy_when_unset`` keeps the trust of an unmanaged runtime (no mode);
    the Compact supervisor passes False, so an unset mode there trusts only what the
    operator listed, as its router always has. ``legacy_host_gateway`` False keeps
    uvicorn's unmanaged list as it was (loopback and the bundled proxy only).
    """
    warnings: list[str] = []
    networks: list[IPNetwork] = list(LOOPBACK)
    try:
        configured = parse_trusted_proxies(str(environment.get(CONFIGURED_ENV) or ""))
    except TrustedProxyError as exc:
        if strict:
            raise
        warnings.append(f"{exc}; the list is ignored until it is corrected")
        configured = ()
    for network in configured:
        _add(networks, network)

    mode = str(environment.get(TRUST_MODE_ENV) or "").strip().lower()
    peer = str(environment.get(TRUST_PEER_ENV) or "").strip()
    if mode and mode not in TRUST_MODES:
        message = f"{TRUST_MODE_ENV}={mode!r} is not one of {', '.join(sorted(TRUST_MODES))}"
        if strict:
            raise TrustedProxyError(message)
        warnings.append(message + "; only FS_PROXY_TRUSTED_IPS is trusted")
        mode = MODE_OPERATOR
    if not mode:
        mode = "" if legacy_when_unset else MODE_OPERATOR

    if mode == MODE_BUNDLED_PROXY:
        for address in resolve(peer or BUNDLED_PROXY_HOST):
            _add(networks, _single(address))
    elif mode == MODE_PROXY_NETWORK:
        name = peer or BUNDLED_PROXY_HOST
        addresses = resolve(name)
        container = attached()
        found = [network for address in addresses if (network := container.containing(address))]
        for network in found:
            _add(networks, network)
        if not found:
            warnings.append(
                f"The proxy network could not be identified ({name!r} resolves to no network "
                "this container is attached to), so the proxy in front is not trusted and "
                "every visitor shares its address."
            )
    elif mode == MODE_HOST_GATEWAY:
        _add_host_gateway(networks, resolve, attached)
    elif mode == MODE_OPERATOR:
        if not any(not network.is_loopback for network in configured):
            warnings.append(UNTRUSTED_PLATFORM_WARNING)
    else:  # an unmanaged or development runtime: unchanged
        for address in resolve(BUNDLED_PROXY_HOST):
            _add(networks, _single(address))
        if legacy_host_gateway:
            _add_host_gateway(networks, resolve, attached)

    problem = whole_address_space_problem(networks)
    if problem:  # only reachable through a derived network; never trust everyone
        if strict:
            raise TrustedProxyError(problem)
        warnings.append(problem + "; only loopback is trusted")
        networks = list(LOOPBACK)
    return ForwardedTrust(
        mode=mode or "unmanaged", networks=tuple(networks), warnings=tuple(warnings)
    )


def _add_host_gateway(
    networks: list[IPNetwork],
    resolve: Callable[[str], Sequence[IPAddress]],
    attached: Callable[[], AttachedNetworks],
) -> None:
    gateway = attached().gateway
    if gateway:
        _add(networks, _single(ipaddress.ip_address(gateway)))
    for alias in _HOST_ALIASES:
        for address in resolve(alias):
            _add(networks, _single(address))


def main(argv: Sequence[str] | None = None) -> int:
    """Print the derived list for uvicorn's --forwarded-allow-ips (start_server.sh)."""
    del argv
    from core.infrastructure.runtime_env import runtime_env

    try:
        trust = derive_forwarded_trust(runtime_env, legacy_host_gateway=False)
    except TrustedProxyError as exc:
        print(f"[start_server] {exc}", file=sys.stderr)
        return 78
    for warning in trust.warnings:
        print(f"[start_server] WARNING client address: {warning}", file=sys.stderr)
    print(
        f"[start_server] Forwarded client addresses trusted from ({trust.mode}): {trust.render()}",
        file=sys.stderr,
    )
    print(trust.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
