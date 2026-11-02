"""The fixed first-party spans and instruments, through the OpenTelemetry API only.

Nothing here builds providers: spans and measurements go to whatever
``otel_runtime`` installed, and are skipped outright while a signal is off or
the OpenTelemetry API is not installed (the CLI image has none).

Trace context stored outside the process (a job's ``trace_context``) is a
bounded W3C carrier: ``trace_carrier`` writes one, ``remote_parent`` and span
links read one back, and nothing but ``traceparent``/``tracestate`` survives.

A forked child must not record metrics: its inherited reader never exports
(``PidGuardedMetricExporter``), so a measurement recorded there would be lost.
Between ``begin_child_capture`` and ``drain_child_capture`` measurements of a
child are kept as bounded plain data instead, and the parent records the
returned list once with ``record_captured``.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from core.infrastructure.observability.otel_runtime import current_telemetry

logger = logging.getLogger(__name__)

FLOW_STEWARD_SCOPE = "flow_steward"

SEGMENT_DURATION = "fs.workflow.segment.duration"
STEP_DURATION = "fs.workflow.step.duration"
# gen_ai metrics of the pinned semantic conventions 1.44.0.
OPERATION_DURATION = "gen_ai.client.operation.duration"
TOKEN_USAGE = "gen_ai.client.token.usage"

# Bucket advice of the GenAI conventions, used for every duration instrument.
_DURATION_BUCKETS = (
    0.01,
    0.02,
    0.04,
    0.08,
    0.16,
    0.32,
    0.64,
    1.28,
    2.56,
    5.12,
    10.24,
    20.48,
    40.96,
    81.92,
)
_TOKEN_BUCKETS = (1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304)

_INSTRUMENTS: dict[str, tuple[str, str, tuple[float, ...]]] = {
    SEGMENT_DURATION: ("s", "Duration of one active workflow segment.", _DURATION_BUCKETS),
    STEP_DURATION: ("s", "Duration of one workflow step attempt.", _DURATION_BUCKETS),
    OPERATION_DURATION: ("s", "GenAI operation duration.", _DURATION_BUCKETS),
    TOKEN_USAGE: ("{token}", "Number of input and output tokens used.", _TOKEN_BUCKETS),
}

# Measurement attributes are fixed and their values bounded by the caller;
# anything else is dropped before recording.
MEASUREMENT_ATTRIBUTES = frozenset(
    {
        "error.type",
        "fs.llm.purpose",
        "fs.outcome",
        "fs.step.kind",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.token.type",
    }
)
_MAX_ATTRIBUTE_VALUE = 64
MAX_CAPTURED_MEASUREMENTS = 512

# A stored W3C trace context: version 00, nonzero ids, and a tracestate within
# the specification's 512-character limit. Baggage is never carried.
_TRACEPARENT = re.compile(r"00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}")
_MAX_TRACESTATE = 512
MAX_SPAN_LINKS = 4

_lock = threading.Lock()
_instruments: dict[tuple[int, str], Any] = {}
_capture: list[tuple[str, float, dict[str, str]]] | None = None
_capture_pid: int | None = None


def _signal_enabled(signal: str) -> bool:
    state = current_telemetry()
    return bool(state is not None and getattr(state.settings, signal).enabled)


def _owner_process() -> bool:
    state = current_telemetry()
    return bool(state is not None and state.owner.is_current())


def traces_enabled() -> bool:
    return _signal_enabled("traces")


def metrics_enabled() -> bool:
    return _signal_enabled("metrics")


def tracer() -> Any | None:
    """The first-party tracer, or ``None`` while traces are off."""
    state = current_telemetry()
    provider = getattr(state, "tracer_provider", None)
    if provider is None or not traces_enabled():
        return None
    return provider.get_tracer(FLOW_STEWARD_SCOPE)


def start_span(
    name: str,
    *,
    parent: Any | None = None,
    context: Any | None = None,
    links: Sequence[Mapping[str, str]] = (),
    attributes: Mapping[str, Any] | None = None,
    client: bool = False,
    server: bool = False,
) -> Any | None:
    """Start a first-party span under *parent* (a span), *context*, or the current context.

    *links* are stored trace carriers (see ``trace_carrier``). The span is not
    made current: callers end it explicitly with ``end_span``.
    """
    active = tracer()
    if active is None:
        return None
    try:
        from opentelemetry import trace

        if parent is not None:
            context = trace.set_span_in_context(parent)
        kind = trace.SpanKind.INTERNAL
        if client:
            kind = trace.SpanKind.CLIENT
        elif server:
            kind = trace.SpanKind.SERVER
        return active.start_span(
            name,
            context=context,
            kind=kind,
            attributes=dict(attributes or {}),
            links=_links(links),
        )
    except Exception as exc:
        logger.debug("telemetry span skipped: %s", type(exc).__name__)
        return None


def end_span(
    span: Any | None,
    *,
    error_type: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    """End *span*; an error sets the ERROR status with no description."""
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        if attributes:
            span.set_attributes(dict(attributes))
        if error_type:
            span.set_attribute("error.type", error_type)
            span.set_status(Status(StatusCode.ERROR))
        span.end()
    except Exception as exc:
        logger.debug("telemetry span end skipped: %s", type(exc).__name__)


ObservationsFn = Callable[[], Iterable[tuple[float, Mapping[str, str]]]]
_OBSERVABLE_KINDS = ("counter", "gauge", "up_down_counter")


def register_observable(
    name: str,
    *,
    kind: str,
    unit: str,
    description: str,
    observe: ObservationsFn,
) -> bool:
    """Register an asynchronous instrument whose callback runs only in the owner.

    The owner check runs before *observe*, so a forked child never queries a
    database or a broker for a reader that must not export. A failing *observe*
    reports nothing for that collection; an unknown value is never reported as 0.
    """
    state = current_telemetry()
    provider = getattr(state, "meter_provider", None)
    if provider is None or kind not in _OBSERVABLE_KINDS or not metrics_enabled():
        return False

    def callback(options: Any) -> list[Any]:
        del options
        if not _owner_process():
            return []
        try:
            from opentelemetry.metrics import Observation

            return [
                Observation(number, attributes=dict(attributes))
                for value, attributes in observe()
                if (number := _valid_signed(value)) is not None
            ]
        except Exception as exc:
            logger.debug("telemetry observation skipped: %s", type(exc).__name__)
            return []

    meter = provider.get_meter(FLOW_STEWARD_SCOPE)
    create = {
        "counter": meter.create_observable_counter,
        "gauge": meter.create_observable_gauge,
        "up_down_counter": meter.create_observable_up_down_counter,
    }[kind]
    create(name, callbacks=[callback], unit=unit, description=description)
    return True


def _valid_signed(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def bounded_carrier(value: Any) -> dict[str, str] | None:
    """A stored trace carrier reduced to a valid ``traceparent`` and bounded ``tracestate``."""
    if not isinstance(value, Mapping):
        return None
    traceparent = value.get("traceparent")
    if not isinstance(traceparent, str) or not _TRACEPARENT.fullmatch(traceparent):
        return None
    carrier = {"traceparent": traceparent}
    tracestate = value.get("tracestate")
    if isinstance(tracestate, str) and tracestate and len(tracestate) <= _MAX_TRACESTATE:
        carrier["tracestate"] = tracestate
    return carrier


def trace_carrier(span: Any | None = None) -> dict[str, str] | None:
    """The W3C carrier of *span*, or of the current context, while traces are on.

    An unsampled context is kept too, so its sampling decision travels with it.
    """
    if tracer() is None:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        context = trace.set_span_in_context(span) if span is not None else None
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier, context=context)
        return bounded_carrier(carrier)
    except Exception as exc:
        logger.debug("telemetry carrier skipped: %s", type(exc).__name__)
        return None


def _span_context(carrier: Any) -> Any | None:
    bounded = bounded_carrier(carrier)
    if bounded is None:
        return None
    from opentelemetry import trace
    from opentelemetry.context import Context
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    extracted = TraceContextTextMapPropagator().extract(bounded, context=Context())
    span_context = trace.get_current_span(extracted).get_span_context()
    return span_context if span_context.is_valid else None


def remote_parent(carrier: Any) -> Any | None:
    """An OpenTelemetry context whose parent is the stored *carrier*, or ``None``."""
    if tracer() is None:
        return None
    try:
        span_context = _span_context(carrier)
        if span_context is None:
            return None
        from opentelemetry import trace

        return trace.set_span_in_context(trace.NonRecordingSpan(span_context))
    except Exception as exc:
        logger.debug("telemetry remote parent skipped: %s", type(exc).__name__)
        return None


def _links(carriers: Sequence[Mapping[str, str]]) -> list[Any]:
    from opentelemetry import trace

    links = []
    for carrier in list(carriers)[:MAX_SPAN_LINKS]:
        span_context = _span_context(carrier)
        if span_context is not None:
            links.append(trace.Link(span_context))
    return links


@contextmanager
def current_span(span: Any | None) -> Iterator[None]:
    """Make *span* current for the block, so a library span nests under it."""
    if span is None:
        yield
        return
    try:
        from opentelemetry import context, trace

        token = context.attach(trace.set_span_in_context(span))
    except Exception:
        yield
        return
    try:
        yield
    finally:
        context.detach(token)


@contextmanager
def detached_context() -> Iterator[None]:
    """Run the block with an empty OpenTelemetry context, restoring it afterwards.

    For requests to third parties: no ambient trace context can reach them.
    """
    try:
        from opentelemetry import context
        from opentelemetry.context import Context

        token = context.attach(Context())
    except Exception:
        yield
        return
    try:
        yield
    finally:
        context.detach(token)


def _instrument(name: str) -> Any | None:
    state = current_telemetry()
    provider = getattr(state, "meter_provider", None)
    if provider is None:
        return None
    key = (id(provider), name)
    with _lock:
        instrument = _instruments.get(key)
        if instrument is None:
            unit, description, buckets = _INSTRUMENTS[name]
            meter = provider.get_meter(FLOW_STEWARD_SCOPE)
            instrument = meter.create_histogram(
                name,
                unit=unit,
                description=description,
                explicit_bucket_boundaries_advisory=list(buckets),
            )
            _instruments[key] = instrument
    return instrument


def _clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in (attributes or {}).items():
        if key in MEASUREMENT_ATTRIBUTES and isinstance(value, str) and value:
            cleaned[key] = value[:_MAX_ATTRIBUTE_VALUE]
    return cleaned


def _valid_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def record(name: str, value: Any, attributes: Mapping[str, Any] | None = None) -> None:
    """Record one measurement of a fixed instrument; never raises."""
    number = _valid_value(value)
    if name not in _INSTRUMENTS or number is None or not metrics_enabled():
        return
    cleaned = _clean_attributes(attributes)
    try:
        if not _owner_process():
            _capture_measurement(name, number, cleaned)
            return
        instrument = _instrument(name)
        if instrument is not None:
            instrument.record(number, attributes=cleaned)
    except Exception as exc:
        logger.debug("telemetry measurement skipped: %s", type(exc).__name__)


def _capture_measurement(name: str, value: float, attributes: dict[str, str]) -> None:
    with _lock:
        if _capture is None or _capture_pid != os.getpid():
            return
        if len(_capture) < MAX_CAPTURED_MEASUREMENTS:
            _capture.append((name, value, attributes))


def begin_child_capture() -> None:
    """Keep this forked child's measurements for its parent instead of dropping them."""
    global _capture, _capture_pid
    with _lock:
        _capture = []
        _capture_pid = os.getpid()


def drain_child_capture() -> list[list[Any]]:
    """The captured measurements as plain data for the result queue, once."""
    global _capture, _capture_pid
    with _lock:
        captured = _capture if _capture_pid == os.getpid() else None
        _capture = None
        _capture_pid = None
    return [[name, value, dict(attributes)] for name, value, attributes in captured or ()]


def record_captured(measurements: Iterable[Any] | None) -> int:
    """Record measurements a child returned; malformed entries are skipped."""
    if not _owner_process():
        return 0
    recorded = 0
    for item in list(measurements or ())[:MAX_CAPTURED_MEASUREMENTS]:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        name, value, attributes = item
        if not isinstance(name, str) or not isinstance(attributes, Mapping):
            continue
        if name in _INSTRUMENTS and _valid_value(value) is not None:
            record(name, value, attributes)
            recorded += 1
    return recorded


def _reset_for_tests() -> None:
    global _capture, _capture_pid
    with _lock:
        _instruments.clear()
        _capture = None
        _capture_pid = None


__all__ = [
    "FLOW_STEWARD_SCOPE",
    "MAX_CAPTURED_MEASUREMENTS",
    "MAX_SPAN_LINKS",
    "MEASUREMENT_ATTRIBUTES",
    "OPERATION_DURATION",
    "SEGMENT_DURATION",
    "STEP_DURATION",
    "TOKEN_USAGE",
    "begin_child_capture",
    "bounded_carrier",
    "current_span",
    "detached_context",
    "drain_child_capture",
    "end_span",
    "metrics_enabled",
    "record",
    "record_captured",
    "register_observable",
    "remote_parent",
    "start_span",
    "trace_carrier",
    "tracer",
    "traces_enabled",
]
