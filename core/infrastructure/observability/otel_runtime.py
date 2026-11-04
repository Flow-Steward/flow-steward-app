"""Process lifecycle of the optional OpenTelemetry export.

``init_telemetry`` is safe to call from every long-lived role and never raises:
with no signal configured it installs nothing and the OpenTelemetry API stays a
no-op. Each enabled signal gets one standard OTLP/HTTP exporter behind the
platform guards; shutdown and child flush are bounded in time.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from core.infrastructure.observability.otel_config import (
    CHILD_FLUSH_TIMEOUT_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
    TelemetrySettings,
    load_telemetry_settings,
)
from core.infrastructure.observability.otel_environment import (
    SIGNALS,
    apply_environment_policy,
)
from core.infrastructure.observability.otel_logs import safe_event

logger = logging.getLogger(__name__)

# Provider exit hooks are disabled in favour of the bounded shutdown below.
# Span limits are explicit so the SDK does not take them from the environment.
_SPAN_LIMITS = {
    "max_attributes": 64,
    "max_events": 0,
    "max_links": 16,
    "max_span_attributes": 64,
    "max_event_attributes": 0,
    "max_link_attributes": 0,
    "max_attribute_length": 512,
    "max_span_attribute_length": 512,
}

ExporterFactory = Callable[[str, TelemetrySettings], Any]


@dataclass
class TelemetryState:
    role: str
    settings: TelemetrySettings
    owner: Any
    tracer_provider: Any | None = None
    meter_provider: Any | None = None
    logger_provider: Any | None = None


_lock = threading.Lock()
_state: TelemetryState | None = None


def current_telemetry() -> TelemetryState | None:
    return _state


def _otlp_exporter(signal: str, settings: TelemetrySettings) -> Any:
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.metrics import NoOpMeterProvider

    kwargs: dict[str, Any] = {
        "endpoint": settings.signal(signal).endpoint,
        "timeout": settings.timeout_seconds,
        "compression": Compression.Gzip
        if settings.compression == "gzip"
        else Compression.NoCompression,
        "certificate_file": settings.certificate_file,
        "client_certificate_file": settings.client_certificate_file,
        "client_key_file": settings.client_key_file,
        # The exporter's own self-observation metrics are not part of the contract.
        "meter_provider": NoOpMeterProvider(),
    }
    if signal == "traces":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(**kwargs)
    if signal == "metrics":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        return OTLPMetricExporter(**kwargs)
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    return OTLPLogExporter(**kwargs)


def _sampler(settings: TelemetrySettings) -> Any:
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    if settings.sampler == "parentbased_always_off":
        return ParentBased(ALWAYS_OFF)
    if settings.sampler == "parentbased_traceidratio":
        return ParentBased(TraceIdRatioBased(settings.sampler_ratio))
    return ParentBased(ALWAYS_ON)


def _tracer_provider(resource: Any, settings: TelemetrySettings, exporter: Any) -> Any:
    from opentelemetry.metrics import NoOpMeterProvider
    from opentelemetry.sdk.trace import SpanLimits
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from core.infrastructure.observability.otel_export_guards import (
        DEFAULT_SPAN_EXPORT_POLICY,
        SanitizingSpanExporter,
        ScopeSuppressingTracerProvider,
    )

    provider = ScopeSuppressingTracerProvider(
        resource=resource,
        sampler=_sampler(settings),
        span_limits=SpanLimits(**_SPAN_LIMITS),
        shutdown_on_exit=False,
    )
    batch = settings.span_batch
    provider.add_span_processor(
        BatchSpanProcessor(
            SanitizingSpanExporter(exporter, DEFAULT_SPAN_EXPORT_POLICY),
            max_queue_size=batch.max_queue_size,
            schedule_delay_millis=batch.schedule_delay_millis,
            max_export_batch_size=batch.max_export_batch_size,
            export_timeout_millis=batch.export_timeout_millis,
            meter_provider=NoOpMeterProvider(),
        )
    )
    return provider


def _meter_provider(resource: Any, settings: TelemetrySettings, exporter: Any, owner: Any) -> Any:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    from core.infrastructure.observability.otel_export_guards import PidGuardedMetricExporter

    reader = PeriodicExportingMetricReader(
        PidGuardedMetricExporter(exporter, owner=owner),
        export_interval_millis=settings.metric_export_interval_millis,
        export_timeout_millis=settings.metric_export_timeout_millis,
    )
    return MeterProvider(
        resource=resource, metric_readers=[reader], views=[], shutdown_on_exit=False
    )


def _logger_provider(resource: Any, settings: TelemetrySettings, exporter: Any) -> Any:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    batch = settings.log_batch
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            exporter,
            max_queue_size=batch.max_queue_size,
            schedule_delay_millis=batch.schedule_delay_millis,
            max_export_batch_size=batch.max_export_batch_size,
            export_timeout_millis=batch.export_timeout_millis,
        )
    )
    return provider


def _log_disabled_signals(role: str, settings: TelemetrySettings) -> None:
    for signal in SIGNALS:
        reason = settings.signal(signal).disabled_reason
        if reason:
            logger.warning(
                "OpenTelemetry %s export stays disabled (role=%s reason=%s)", signal, role, reason
            )


def _install_globals(state: TelemetryState) -> None:
    from opentelemetry import propagate, trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    # W3C trace context only; baggage is neither accepted nor forwarded.
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    if state.tracer_provider is not None:
        trace.set_tracer_provider(state.tracer_provider)
    if state.meter_provider is not None:
        from opentelemetry import metrics

        metrics.set_meter_provider(state.meter_provider)
    if state.logger_provider is not None:
        from opentelemetry._logs import set_logger_provider

        set_logger_provider(state.logger_provider)


def init_telemetry(
    *,
    role: str,
    environment: MutableMapping[str, str] | None = None,
    exporter_factory: ExporterFactory | None = None,
    install_globals: bool = True,
) -> TelemetryState | None:
    """Start the configured signals for this process; never raises."""
    global _state

    apply_environment_policy(environment)
    settings = load_telemetry_settings(environment)
    with _lock:
        if _state is not None:
            return _state
        _log_disabled_signals(role, settings)
        if not settings.any_enabled:
            logger.info("OpenTelemetry export disabled (role=%s)", role)
            return None
        try:
            from core.infrastructure.observability.otel_export_guards import OwnerProcess
            from core.infrastructure.observability.otel_resource import (
                build_resource,
                read_service_version,
            )

            factory = exporter_factory or _otlp_exporter
            resource = build_resource(
                role=role,
                service_version=read_service_version(),
                operator_attributes=settings.resource_attributes,
            )
            state = TelemetryState(role=role, settings=settings, owner=OwnerProcess())
            if settings.traces.enabled:
                state.tracer_provider = _tracer_provider(
                    resource, settings, factory("traces", settings)
                )
            if settings.metrics.enabled:
                state.meter_provider = _meter_provider(
                    resource, settings, factory("metrics", settings), state.owner
                )
            if settings.logs.enabled:
                state.logger_provider = _logger_provider(
                    resource, settings, factory("logs", settings)
                )
            if install_globals:
                _install_globals(state)
                # The SDK's own exit hooks are unbounded; this one is not.
                atexit.register(shutdown_telemetry)
        except Exception as exc:
            logger.warning(
                "OpenTelemetry export unavailable (role=%s error=%s)", role, type(exc).__name__
            )
            return None
        _state = state
    if state.logger_provider is not None:
        try:
            from core.infrastructure.observability.otel_logs import install_log_export

            install_log_export(state.logger_provider)
        except Exception as exc:
            logger.warning("Log export unavailable: %s", type(exc).__name__)
    logger.info(
        "OpenTelemetry export enabled (role=%s traces=%s metrics=%s logs=%s)",
        role,
        settings.traces.enabled,
        settings.metrics.enabled,
        settings.logs.enabled,
        extra=safe_event("fs.telemetry.export_enabled", {"fs.role": role}),
    )
    if state.meter_provider is not None:
        # After the state is published: the callbacks read it to check the owner.
        try:
            from core.infrastructure.observability.otel_process_metrics import (
                register_process_metrics,
            )

            register_process_metrics()
        except Exception as exc:
            logger.warning("Process metrics unavailable: %s", type(exc).__name__)
    return state


def _bounded(action: Callable[[], Any], timeout_seconds: float) -> bool:
    """Run ``action`` on a daemon thread; give up waiting after the timeout."""
    done = threading.Event()

    def _run() -> None:
        try:
            action()
        except Exception as exc:
            logger.debug("OpenTelemetry lifecycle step failed: %s", type(exc).__name__)
        finally:
            done.set()

    threading.Thread(target=_run, name="otel-lifecycle", daemon=True).start()
    return done.wait(timeout_seconds)


def flush_child_telemetry(timeout_seconds: float = CHILD_FLUSH_TIMEOUT_SECONDS) -> bool:
    """Flush traces and logs from a forked child before it exits.

    Metrics are never flushed here: a child records no metrics of its own and the
    inherited metric exporter is guarded to the owner process.
    """
    state = _state
    if state is None:
        return True
    millis = int(timeout_seconds * 1000)

    def _flush() -> None:
        for provider in (state.tracer_provider, state.logger_provider):
            if provider is not None:
                provider.force_flush(millis)

    return _bounded(_flush, timeout_seconds)


def shutdown_telemetry(timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS) -> bool:
    """Flush and stop the providers of the owner process within the timeout."""
    global _state

    with _lock:
        state = _state
        _state = None
    if state is None:
        return True
    if not state.owner.is_current():
        return True
    millis = int(timeout_seconds * 1000)

    def _shutdown() -> None:
        for provider in (state.tracer_provider, state.logger_provider):
            if provider is not None:
                provider.shutdown()
        if state.meter_provider is not None:
            state.meter_provider.shutdown(timeout_millis=millis)

    finished = _bounded(_shutdown, timeout_seconds)
    if not finished:
        logger.warning(
            "OpenTelemetry shutdown exceeded %.1fs; pending telemetry dropped", timeout_seconds
        )
    return finished


def telemetry_enabled_signals(state: TelemetryState | None = None) -> Mapping[str, bool]:
    current = state or _state
    if current is None:
        return dict.fromkeys(SIGNALS, False)
    return {signal: current.settings.signal(signal).enabled for signal in SIGNALS}


def _reset_for_tests() -> None:
    global _state
    with _lock:
        _state = None


__all__ = [
    "TelemetryState",
    "current_telemetry",
    "flush_child_telemetry",
    "init_telemetry",
    "shutdown_telemetry",
    "telemetry_enabled_signals",
]
