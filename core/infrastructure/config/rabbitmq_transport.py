"""Deployment-owned RabbitMQ transport boundary shared by both broker sinks."""

from __future__ import annotations

import ipaddress
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from core.infrastructure.config.runtime_env import runtime_env

_TRANSPORT_MODES = frozenset({"tls", "compose-local", "audit-local", "loopback-local"})
_REJECTION = "RabbitMQ transport is invalid or disallowed by FS_RABBITMQ_TRANSPORT_MODE"


@dataclass(frozen=True, slots=True)
class RabbitMQEndpoint:
    host: str
    port: int
    tls: bool


def rabbitmq_transport_mode(env: Mapping[str, str] | None = None) -> str:
    raw = (
        runtime_env.get("FS_RABBITMQ_TRANSPORT_MODE", "tls")
        if env is None
        else env.get("FS_RABBITMQ_TRANSPORT_MODE", "tls")
    )
    mode = str(raw or "tls").strip()
    if mode not in _TRANSPORT_MODES:
        raise ValueError(_REJECTION)
    return mode


def validate_rabbitmq_endpoint(url: str, env: Mapping[str, str] | None = None) -> RabbitMQEndpoint:
    """Local permission is an explicit deployment assertion, never DNS inference."""
    try:
        mode = rabbitmq_transport_mode(env)
        if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
            raise ValueError(_REJECTION)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme not in {"amqp", "amqps"} or not host or "%" in host or parsed.fragment:
            raise ValueError(_REJECTION)
        tls = parsed.scheme == "amqps"
        default_port = 5671 if tls else 5672
        port = parsed.port if parsed.port is not None else default_port
        if not 1 <= port <= 65535:
            raise ValueError(_REJECTION)
        if not tls:
            # Plaintext always names an explicit port, including loopback tools.
            if parsed.port is None:
                raise ValueError(_REJECTION)
            if mode == "compose-local":
                allowed = host == "rabbitmq-fs" and port == 5672
            elif mode == "audit-local":
                allowed = host == "rabbitmq-audit" and port == 5672
            elif mode == "loopback-local":
                allowed = ipaddress.ip_address(host).is_loopback
            else:
                allowed = False
            if not allowed:
                raise ValueError(_REJECTION)
        return RabbitMQEndpoint(host=host, port=port, tls=tls)
    except (ValueError, TypeError, AttributeError):
        # URL/port parsers and SSL errors may include URI credentials in detail.
        raise ValueError(_REJECTION) from None


def build_rabbitmq_parameters(
    url: str,
    *,
    parameter_factory: Callable[[str], Any],
    env: Mapping[str, str] | None = None,
) -> Any:
    """Validate both the authored URL and the effective client configuration."""
    endpoint = validate_rabbitmq_endpoint(url, env)
    try:
        parameters = parameter_factory(url)
        if parameters.host != endpoint.host or parameters.port != endpoint.port:
            raise ValueError(_REJECTION)
        options = parameters.ssl_options
        if endpoint.tls:
            if (
                options is None
                or options.context.verify_mode != ssl.CERT_REQUIRED
                or options.context.check_hostname is not True
            ):
                raise ValueError(_REJECTION)
            # Pika's connection workflow defaults an omitted server name to
            # parameters.host. Make that verified peer explicit for every user
            # of these parameters; an authored different name remains invalid.
            if options.server_hostname is None:
                options.server_hostname = endpoint.host
            if options.server_hostname != endpoint.host:
                raise ValueError(_REJECTION)
        elif options is not None:
            # Do not reinterpret amqp query options as a different transport.
            raise ValueError(_REJECTION)
        return parameters
    except Exception:
        # A client parser's diagnostic must not expose the URI or its userinfo.
        raise ValueError(_REJECTION) from None
