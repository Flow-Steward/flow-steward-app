"""The exported resource: SDK identity, service identity and deployment environment.

``Resource.create()`` merges detector and environment output; the result is then
reduced to an allowlist and rebuilt with ``Resource(...)``, which applies nothing
further. The SDK still refreshes process-dependent detectors after a fork, which
is why ``otel_environment`` removes ``OTEL_EXPERIMENTAL_RESOURCE_DETECTORS`` from
the process environment: with it gone only ``service.instance.id`` is refreshed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

SERVICE_NAME_PREFIX = "flow-steward"
UNKNOWN_SERVICE_VERSION = "unknown"
# Read directly rather than through the configuration package, which the minimal
# MCP image does not ship.
_VERSION_FILE = Path(__file__).resolve().parents[3] / "VERSION"

# Long-lived process roles that initialize the SDK.
ROLE_SERVICE_NAMES = {
    "app": f"{SERVICE_NAME_PREFIX}-app",
    "worker": f"{SERVICE_NAME_PREFIX}-worker",
    "control": f"{SERVICE_NAME_PREFIX}-control",
    "mcp": f"{SERVICE_NAME_PREFIX}-mcp",
}

ALLOWED_RESOURCE_ATTRIBUTES = frozenset(
    {
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
        "service.name",
        "service.version",
        "service.instance.id",
        "deployment.environment.name",
    }
)


def service_name_for_role(role: str) -> str:
    try:
        return ROLE_SERVICE_NAMES[role]
    except KeyError as exc:
        raise ValueError(f"unsupported telemetry role: {role!r}") from exc


def read_service_version(version_file: Path = _VERSION_FILE) -> str:
    try:
        return version_file.read_text(encoding="utf-8").strip() or UNKNOWN_SERVICE_VERSION
    except OSError:
        return UNKNOWN_SERVICE_VERSION


def filter_resource_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: attributes[key] for key in sorted(attributes) if key in ALLOWED_RESOURCE_ATTRIBUTES
    }


def build_resource(
    *, role: str, service_version: str, operator_attributes: Mapping[str, str]
) -> Any:
    from opentelemetry.sdk.resources import Resource

    identity = {
        "service.name": service_name_for_role(role),
        "service.version": service_version,
    }
    created = Resource.create({**dict(operator_attributes), **identity})
    # Platform identity wins over anything a detector or the environment added.
    attributes = {**filter_resource_attributes(created.attributes), **identity}
    return Resource(attributes, created.schema_url)


__all__ = [
    "ALLOWED_RESOURCE_ATTRIBUTES",
    "ROLE_SERVICE_NAMES",
    "build_resource",
    "filter_resource_attributes",
    "read_service_version",
    "service_name_for_role",
]
