"""Canonical website routing and certificate compatibility."""

from types import MappingProxyType

INGRESS_TLS_MODES = MappingProxyType(
    {
        "bundled_traefik": ("local", "self_signed", "provided", "acme"),
        "external_traefik": ("external_resolver", "external_certificate"),
        "external_proxy": ("reverse_proxy",),
        "platform": ("platform_managed",),
    }
)
