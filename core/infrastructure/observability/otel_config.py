"""Telemetry export settings parsed from the supported ``OTEL_*`` variables.

Export is off unless a signal is explicitly set to ``otlp`` *and* has an
endpoint: there is no implicit ``localhost`` destination. Values the SDK would
otherwise read from the environment on its own are resolved here and passed
explicitly, so the platform, not the library default, decides them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from core.infrastructure.observability.otel_environment import (
    RESOURCE_ATTRIBUTES_ENV,
    SIGNALS,
    allowed_operator_resource_attributes,
)
from core.infrastructure.runtime_env import runtime_env as _runtime_env

SUPPORTED_PROTOCOL = "http/protobuf"
SUPPORTED_COMPRESSIONS = frozenset({"none", "gzip"})
EXPORTER_OTLP = "otlp"
EXPORTER_NONE = "none"

_SIGNAL_PATHS = {"traces": "v1/traces", "metrics": "v1/metrics", "logs": "v1/logs"}
_EXPORTER_ENV = {
    "traces": "OTEL_TRACES_EXPORTER",
    "metrics": "OTEL_METRICS_EXPORTER",
    "logs": "OTEL_LOGS_EXPORTER",
}
_ENDPOINT_ENV = {
    "traces": "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "metrics": "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "logs": "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
}

SUPPORTED_SAMPLERS = frozenset(
    {"parentbased_always_on", "parentbased_always_off", "parentbased_traceidratio"}
)
DEFAULT_SAMPLER = "parentbased_always_on"

# Bounded defaults; the export never holds more than these in memory.
DEFAULT_TIMEOUT_MILLIS = 10_000
DEFAULT_BATCH_SCHEDULE_DELAY_MILLIS = 5_000
DEFAULT_BATCH_EXPORT_TIMEOUT_MILLIS = 30_000
DEFAULT_BATCH_MAX_QUEUE_SIZE = 2_048
DEFAULT_BATCH_MAX_EXPORT_BATCH_SIZE = 512
DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS = 60_000
DEFAULT_METRIC_EXPORT_TIMEOUT_MILLIS = 30_000
MAX_QUEUE_SIZE_LIMIT = 65_536
# Shutdown and child flush must never hold a process for long.
SHUTDOWN_TIMEOUT_SECONDS = 5.0
CHILD_FLUSH_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class BatchSettings:
    schedule_delay_millis: int
    export_timeout_millis: int
    max_queue_size: int
    max_export_batch_size: int


@dataclass(frozen=True)
class SignalSettings:
    name: str
    enabled: bool
    endpoint: str = ""
    # Why a requested signal stays off; a code, never a value.
    disabled_reason: str = ""


@dataclass(frozen=True)
class TelemetrySettings:
    traces: SignalSettings
    metrics: SignalSettings
    logs: SignalSettings
    timeout_seconds: float
    compression: str
    sampler: str
    sampler_ratio: float
    span_batch: BatchSettings
    log_batch: BatchSettings
    metric_export_interval_millis: int
    metric_export_timeout_millis: int
    resource_attributes: Mapping[str, str] = field(default_factory=dict)
    certificate_file: str | None = None
    client_certificate_file: str | None = None
    client_key_file: str | None = None

    @property
    def any_enabled(self) -> bool:
        return self.traces.enabled or self.metrics.enabled or self.logs.enabled

    def signal(self, name: str) -> SignalSettings:
        return {"traces": self.traces, "metrics": self.metrics, "logs": self.logs}[name]


def _text(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name) or "").strip()


def _positive_int(
    env: Mapping[str, str], name: str, default: int, *, limit: int | None = None
) -> int:
    try:
        value = int(float(_text(env, name)))
    except ValueError:
        return default
    if value <= 0:
        return default
    return min(value, limit) if limit is not None else value


def _batch(env: Mapping[str, str], prefix: str) -> BatchSettings:
    max_queue_size = _positive_int(
        env, f"{prefix}_MAX_QUEUE_SIZE", DEFAULT_BATCH_MAX_QUEUE_SIZE, limit=MAX_QUEUE_SIZE_LIMIT
    )
    return BatchSettings(
        schedule_delay_millis=_positive_int(
            env, f"{prefix}_SCHEDULE_DELAY", DEFAULT_BATCH_SCHEDULE_DELAY_MILLIS
        ),
        export_timeout_millis=_positive_int(
            env, f"{prefix}_EXPORT_TIMEOUT", DEFAULT_BATCH_EXPORT_TIMEOUT_MILLIS
        ),
        max_queue_size=max_queue_size,
        max_export_batch_size=min(
            _positive_int(
                env, f"{prefix}_MAX_EXPORT_BATCH_SIZE", DEFAULT_BATCH_MAX_EXPORT_BATCH_SIZE
            ),
            max_queue_size,
        ),
    )


def _signal_endpoint(env: Mapping[str, str], signal: str) -> str:
    specific = _text(env, _ENDPOINT_ENV[signal])
    if specific:
        return specific
    base = _text(env, "OTEL_EXPORTER_OTLP_ENDPOINT")
    if not base:
        return ""
    return f"{base.removesuffix('/')}/{_SIGNAL_PATHS[signal]}"


def _valid_endpoint(endpoint: str) -> bool:
    scheme, separator, rest = endpoint.partition("://")
    return bool(separator and rest) and scheme.lower() in {"http", "https"}


def _signal(
    env: Mapping[str, str], signal: str, *, sdk_disabled: bool, protocol_ok: bool
) -> SignalSettings:
    exporter = _text(env, _EXPORTER_ENV[signal]).lower() or EXPORTER_NONE
    if exporter == EXPORTER_NONE:
        return SignalSettings(name=signal, enabled=False)
    if exporter != EXPORTER_OTLP:
        return SignalSettings(name=signal, enabled=False, disabled_reason="unsupported_exporter")
    if sdk_disabled:
        return SignalSettings(name=signal, enabled=False, disabled_reason="sdk_disabled")
    if not protocol_ok:
        return SignalSettings(name=signal, enabled=False, disabled_reason="unsupported_protocol")
    endpoint = _signal_endpoint(env, signal)
    if not endpoint:
        return SignalSettings(name=signal, enabled=False, disabled_reason="missing_endpoint")
    if not _valid_endpoint(endpoint):
        return SignalSettings(name=signal, enabled=False, disabled_reason="invalid_endpoint")
    return SignalSettings(name=signal, enabled=True, endpoint=endpoint)


def _sampler(env: Mapping[str, str]) -> tuple[str, float]:
    sampler = _text(env, "OTEL_TRACES_SAMPLER").lower() or DEFAULT_SAMPLER
    if sampler not in SUPPORTED_SAMPLERS:
        sampler = DEFAULT_SAMPLER
    try:
        ratio = float(_text(env, "OTEL_TRACES_SAMPLER_ARG") or "1.0")
    except ValueError:
        ratio = 1.0
    return sampler, min(max(ratio, 0.0), 1.0)


def load_telemetry_settings(environment: Mapping[str, str] | None = None) -> TelemetrySettings:
    """Resolve the export settings; never raises on malformed input."""
    env = _runtime_env if environment is None else environment
    sdk_disabled = _text(env, "OTEL_SDK_DISABLED").lower() == "true"
    protocol = _text(env, "OTEL_EXPORTER_OTLP_PROTOCOL").lower() or SUPPORTED_PROTOCOL
    protocol_ok = protocol == SUPPORTED_PROTOCOL
    signals = {
        signal: _signal(env, signal, sdk_disabled=sdk_disabled, protocol_ok=protocol_ok)
        for signal in SIGNALS
    }
    compression = _text(env, "OTEL_EXPORTER_OTLP_COMPRESSION").lower() or "none"
    sampler, ratio = _sampler(env)
    return TelemetrySettings(
        traces=signals["traces"],
        metrics=signals["metrics"],
        logs=signals["logs"],
        # The specification defines this timeout in milliseconds; the Python
        # exporters read the same variable as seconds, so it is passed explicitly.
        timeout_seconds=_positive_int(env, "OTEL_EXPORTER_OTLP_TIMEOUT", DEFAULT_TIMEOUT_MILLIS)
        / 1000.0,
        compression=compression if compression in SUPPORTED_COMPRESSIONS else "none",
        sampler=sampler,
        sampler_ratio=ratio,
        span_batch=_batch(env, "OTEL_BSP"),
        log_batch=_batch(env, "OTEL_BLRP"),
        metric_export_interval_millis=_positive_int(
            env, "OTEL_METRIC_EXPORT_INTERVAL", DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS
        ),
        metric_export_timeout_millis=_positive_int(
            env, "OTEL_METRIC_EXPORT_TIMEOUT", DEFAULT_METRIC_EXPORT_TIMEOUT_MILLIS
        ),
        resource_attributes=allowed_operator_resource_attributes(
            _text(env, RESOURCE_ATTRIBUTES_ENV)
        ),
        certificate_file=_text(env, "OTEL_EXPORTER_OTLP_CERTIFICATE") or None,
        client_certificate_file=_text(env, "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE") or None,
        client_key_file=_text(env, "OTEL_EXPORTER_OTLP_CLIENT_KEY") or None,
    )


__all__ = [
    "CHILD_FLUSH_TIMEOUT_SECONDS",
    "SHUTDOWN_TIMEOUT_SECONDS",
    "BatchSettings",
    "SignalSettings",
    "TelemetrySettings",
    "load_telemetry_settings",
]
