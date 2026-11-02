from __future__ import annotations

import time

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from core.infrastructure.observability import otel_runtime
from core.infrastructure.observability.otel_export_guards import FLOW_STEWARD_SCOPE


class _HangingSpanExporter(SpanExporter):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.exported: list[str] = []

    def export(self, spans) -> SpanExportResult:
        self.exported.extend(span.name for span in spans)
        time.sleep(self.seconds)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        time.sleep(self.seconds)


class _NullMetricExporter(MetricExporter):
    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolated_state():
    otel_runtime._reset_for_tests()
    yield
    otel_runtime._reset_for_tests()


def _enabled_env(**extra: str) -> dict[str, str]:
    return {
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid:4318",
        **extra,
    }


def test_disabled_export_installs_nothing_and_builds_no_exporter() -> None:
    built: list[str] = []

    state = otel_runtime.init_telemetry(
        role="app",
        environment={},
        exporter_factory=lambda signal, settings: built.append(signal),
        install_globals=False,
    )

    assert state is None
    assert built == []
    assert otel_runtime.telemetry_enabled_signals() == {
        "traces": False,
        "metrics": False,
        "logs": False,
    }


def test_enabled_signals_get_one_exporter_each() -> None:
    built: list[str] = []

    def factory(signal, settings):
        built.append(signal)
        return _HangingSpanExporter(0) if signal == "traces" else _NullMetricExporter()

    state = otel_runtime.init_telemetry(
        role="worker", environment=_enabled_env(), exporter_factory=factory, install_globals=False
    )

    assert state is not None
    assert built == ["traces", "metrics"]
    assert state.logger_provider is None
    resource = dict(state.tracer_provider.resource.attributes)
    assert resource["service.name"] == "flow-steward-worker"
    assert set(resource) <= {
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
        "service.name",
        "service.version",
        "service.instance.id",
        "deployment.environment.name",
    }
    assert otel_runtime.shutdown_telemetry(timeout_seconds=2) is True


def test_init_is_idempotent_per_process() -> None:
    factory = lambda signal, settings: _HangingSpanExporter(0)  # noqa: E731
    env = _enabled_env(OTEL_METRICS_EXPORTER="none")

    first = otel_runtime.init_telemetry(
        role="app", environment=env, exporter_factory=factory, install_globals=False
    )
    second = otel_runtime.init_telemetry(
        role="app", environment=env, exporter_factory=factory, install_globals=False
    )

    assert first is second
    otel_runtime.shutdown_telemetry(timeout_seconds=2)


def test_exporter_failure_leaves_the_process_running_without_export() -> None:
    def broken(signal, settings):
        raise RuntimeError("Bearer s3cr3t")

    state = otel_runtime.init_telemetry(
        role="app", environment=_enabled_env(), exporter_factory=broken, install_globals=False
    )

    assert state is None


def test_shutdown_with_a_hanging_receiver_is_bounded() -> None:
    exporter = _HangingSpanExporter(5.0)
    state = otel_runtime.init_telemetry(
        role="app",
        environment=_enabled_env(OTEL_METRICS_EXPORTER="none"),
        exporter_factory=lambda signal, settings: exporter,
        install_globals=False,
    )
    assert state is not None
    with state.tracer_provider.get_tracer(FLOW_STEWARD_SCOPE).start_as_current_span("fs.x"):
        pass

    started = time.monotonic()
    finished = otel_runtime.shutdown_telemetry(timeout_seconds=0.5)

    assert finished is False
    assert time.monotonic() - started < 2.0


def test_unknown_role_is_rejected_without_raising() -> None:
    state = otel_runtime.init_telemetry(
        role="installer",
        environment=_enabled_env(),
        exporter_factory=lambda signal, settings: _NullMetricExporter(),
        install_globals=False,
    )

    assert state is None
