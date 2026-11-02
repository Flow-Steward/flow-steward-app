"""Process-environment policy for OpenTelemetry, applied before any OTel import.

The SDK, exporters and instrumentations read ``OTEL_*`` variables on their own:
at import (propagators, context), at construction (exporter endpoints, headers,
limits) and again after every fork (resource detectors). Initialization alone
therefore cannot enforce the export contract. This module rewrites the process
environment so that every later reader sees only the supported settings.

It uses the standard library only, so entrypoints can call it first.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from urllib.parse import quote, unquote

from core.infrastructure.runtime_env import runtime_env as _runtime_env

OTEL_PREFIX = "OTEL_"

SEMCONV_STABILITY_ENV = "OTEL_SEMCONV_STABILITY_OPT_IN"
RESOURCE_ATTRIBUTES_ENV = "OTEL_RESOURCE_ATTRIBUTES"
# The stable HTTP conventions only: no legacy names, no duplicated emission.
SEMCONV_STABILITY_MODE = "http"

SIGNALS = ("traces", "metrics", "logs")

# Operator-facing settings the platform reads and documents. Everything else
# with the OTEL_ prefix is removed from the process environment.
SUPPORTED_ENV_NAMES = frozenset(
    {
        "OTEL_SDK_DISABLED",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "OTEL_EXPORTER_OTLP_TIMEOUT",
        "OTEL_EXPORTER_OTLP_COMPRESSION",
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CLIENT_KEY",
        "OTEL_TRACES_SAMPLER",
        "OTEL_TRACES_SAMPLER_ARG",
        "OTEL_BSP_SCHEDULE_DELAY",
        "OTEL_BSP_EXPORT_TIMEOUT",
        "OTEL_BSP_MAX_QUEUE_SIZE",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
        "OTEL_BLRP_SCHEDULE_DELAY",
        "OTEL_BLRP_EXPORT_TIMEOUT",
        "OTEL_BLRP_MAX_QUEUE_SIZE",
        "OTEL_BLRP_MAX_EXPORT_BATCH_SIZE",
        "OTEL_METRIC_EXPORT_INTERVAL",
        "OTEL_METRIC_EXPORT_TIMEOUT",
        RESOURCE_ATTRIBUTES_ENV,
        SEMCONV_STABILITY_ENV,
    }
)

# The only resource attribute an operator may contribute; identity, version and
# instance are owned by the platform and the SDK.
OPERATOR_RESOURCE_ATTRIBUTES = frozenset({"deployment.environment.name"})


@dataclass(frozen=True)
class EnvironmentPolicyReport:
    """Names (never values) the policy removed or rewrote."""

    removed: tuple[str, ...]
    rewritten: tuple[str, ...]


def _parse_resource_attributes(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in str(raw or "").split(","):
        key, separator, value = item.partition("=")
        key = unquote(key.strip())
        value = unquote(value.strip())
        if separator and key and value:
            parsed[key] = value
    return parsed


def allowed_operator_resource_attributes(raw: str) -> dict[str, str]:
    """Operator resource attributes that survive the policy, decoded."""
    parsed = _parse_resource_attributes(raw)
    return {key: parsed[key] for key in sorted(parsed) if key in OPERATOR_RESOURCE_ATTRIBUTES}


def _encode_resource_attributes(attributes: dict[str, str]) -> str:
    return ",".join(
        f"{quote(key, safe='.')}={quote(value, safe='')}" for key, value in attributes.items()
    )


def apply_environment_policy(
    environment: MutableMapping[str, str] | None = None,
) -> EnvironmentPolicyReport:
    """Remove unsupported ``OTEL_*`` settings and pin the conventions mode.

    Idempotent. Must run before the first OpenTelemetry import in a process and is
    safe to repeat later; repeated calls change nothing.
    """
    env = _runtime_env if environment is None else environment
    removed: list[str] = []
    rewritten: list[str] = []

    for name in sorted(key for key in env if key.startswith(OTEL_PREFIX)):
        if name not in SUPPORTED_ENV_NAMES:
            del env[name]
            removed.append(name)

    raw_attributes = env.get(RESOURCE_ATTRIBUTES_ENV)
    if raw_attributes is not None:
        allowed = allowed_operator_resource_attributes(raw_attributes)
        if not allowed:
            del env[RESOURCE_ATTRIBUTES_ENV]
            removed.append(RESOURCE_ATTRIBUTES_ENV)
        else:
            encoded = _encode_resource_attributes(allowed)
            if encoded != raw_attributes:
                env[RESOURCE_ATTRIBUTES_ENV] = encoded
                rewritten.append(RESOURCE_ATTRIBUTES_ENV)

    if env.get(SEMCONV_STABILITY_ENV) != SEMCONV_STABILITY_MODE:
        if SEMCONV_STABILITY_ENV in env:
            rewritten.append(SEMCONV_STABILITY_ENV)
        env[SEMCONV_STABILITY_ENV] = SEMCONV_STABILITY_MODE

    return EnvironmentPolicyReport(removed=tuple(removed), rewritten=tuple(rewritten))


__all__ = [
    "OPERATOR_RESOURCE_ATTRIBUTES",
    "SEMCONV_STABILITY_MODE",
    "SIGNALS",
    "SUPPORTED_ENV_NAMES",
    "EnvironmentPolicyReport",
    "allowed_operator_resource_attributes",
    "apply_environment_policy",
]
