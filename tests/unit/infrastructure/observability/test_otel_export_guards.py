from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from core.infrastructure.observability.otel_export_guards import (
    DEFAULT_SPAN_EXPORT_POLICY,
    FLOW_STEWARD_SCOPE,
    OwnerProcess,
    PidGuardedMetricExporter,
    SanitizingSpanExporter,
)


class _CountingExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        self.calls.append("export")
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        self.calls.append("flush")
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        self.calls.append("shutdown")


def test_guard_calls_the_delegate_from_the_owner_process() -> None:
    delegate = _CountingExporter()
    guard = PidGuardedMetricExporter(delegate, owner=OwnerProcess())

    assert guard.export(None) is MetricExportResult.SUCCESS
    assert guard.force_flush() is True
    guard.shutdown()

    assert delegate.calls == ["export", "flush", "shutdown"]


def test_guard_suppresses_every_call_from_another_process() -> None:
    delegate = _CountingExporter()
    guard = PidGuardedMetricExporter(delegate, owner=OwnerProcess(pid=-1))

    assert guard.export(None) is MetricExportResult.SUCCESS
    assert guard.force_flush() is True
    guard.shutdown()

    assert delegate.calls == []


def _export_through_policy(make_spans) -> list:
    memory = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(
        SimpleSpanProcessor(SanitizingSpanExporter(memory, DEFAULT_SPAN_EXPORT_POLICY))
    )
    make_spans(provider)
    provider.shutdown()
    return list(memory.get_finished_spans())


def test_policy_reduces_first_party_spans_to_allowed_fields() -> None:
    def make(provider: TracerProvider) -> None:
        tracer = provider.get_tracer(FLOW_STEWARD_SCOPE)
        with tracer.start_as_current_span("fs.workflow.step") as parent:
            parent_context = parent.get_span_context()
        with tracer.start_as_current_span(
            "fs.workflow.step",
            kind=SpanKind.INTERNAL,
            links=[Link(parent_context, {"note": "free text"})],
        ) as span:
            span.set_attribute("fs.step.type", "agent")
            span.set_attribute("gen_ai.provider.name", "openai")
            span.set_attribute("prompt", "secret prompt")
            span.set_attribute("http.request.header.authorization", "Bearer s3cr3t")
            span.record_exception(RuntimeError("token=s3cr3t"))
            span.set_status(Status(StatusCode.ERROR, "failed with token=s3cr3t"))

    exported = _export_through_policy(make)[-1]

    assert dict(exported.attributes) == {"fs.step.type": "agent", "gen_ai.provider.name": "openai"}
    assert exported.events == ()
    assert exported.status.status_code is StatusCode.ERROR
    assert not exported.status.description
    assert [dict(link.attributes or {}) for link in exported.links] == [{}]
    assert "s3cr3t" not in repr(exported.to_json())


def test_policy_drops_spans_of_unlisted_scopes() -> None:
    def make(provider: TracerProvider) -> None:
        with provider.get_tracer("mcp-python-sdk").start_as_current_span("tools/call x"):
            pass
        with provider.get_tracer(FLOW_STEWARD_SCOPE).start_as_current_span("fs.workflow.segment"):
            pass

    exported = _export_through_policy(make)

    assert [span.name for span in exported] == ["fs.workflow.segment"]


def test_policy_keeps_trace_identity_and_parentage() -> None:
    def make(provider: TracerProvider) -> None:
        tracer = provider.get_tracer(FLOW_STEWARD_SCOPE)
        with (
            tracer.start_as_current_span("fs.workflow.segment"),
            tracer.start_as_current_span("fs.workflow.step"),
        ):
            pass

    step, segment = _export_through_policy(make)

    assert step.parent is not None
    assert step.parent.span_id == segment.context.span_id
    assert step.context.trace_id == segment.context.trace_id
