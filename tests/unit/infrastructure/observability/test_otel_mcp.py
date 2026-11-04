from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("mcp")

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from core.infrastructure.observability import otel_instruments, otel_mcp
from tests.unit.test_workflow_telemetry import _Telemetry
from tests.unit.test_workflow_telemetry import telemetry as telemetry

_TRACE = "1" * 32
_SPAN = "2" * 16
_CARRIER = {"traceparent": f"00-{_TRACE}-{_SPAN}-01"}


def test_error_code_accepts_only_ascii_digits() -> None:
    assert otel_mcp._ERROR_CODE.fullmatch("-32602")
    assert not otel_mcp._ERROR_CODE.fullmatch("-٣٢٦٠٢")


def _ctx(method="tools/call", params=None, meta=None, request_id=7):
    return SimpleNamespace(
        method=method,
        params={"name": "list_projects", "arguments": {"token": "s3cret"}}
        if params is None
        else params,
        meta=meta,
        request_id=request_id,
    )


def _run(ctx, call_next):
    """Run the middleware in one task and report the context it leaves behind."""

    async def task():
        before = trace.get_current_span().get_span_context()
        try:
            return await otel_mcp.McpTraceMiddleware()(ctx, call_next)
        finally:
            # The same task continues after the middleware: nothing may leak.
            assert trace.get_current_span().get_span_context() == before

    return asyncio.run(task())


def test_the_pinned_sdk_ships_its_middleware_and_fs_mcp_replaces_it() -> None:
    from mcp.server import MCPServer

    server = MCPServer(name="probe", middleware=[object()])
    # Guards the provisional middleware API of the pinned SDK version.
    assert any(type(item).__name__ == "OpenTelemetryMiddleware" for item in server.middleware)

    otel_mcp.install_mcp_tracing(server)
    otel_mcp.install_mcp_tracing(server)

    names = [type(item).__name__ for item in server.middleware]
    assert names[0] == "McpTraceMiddleware"
    assert names.count("McpTraceMiddleware") == 1
    assert "OpenTelemetryMiddleware" not in names


def test_a_request_span_continues_the_client_context_and_is_current(
    telemetry: _Telemetry,
) -> None:
    seen = {}

    async def call_next(ctx):
        seen["current"] = trace.get_current_span().get_span_context()
        return {"content": []}

    _run(_ctx(meta=dict(_CARRIER)), call_next)

    span = telemetry.span("tools/call list_projects")
    assert span.kind == SpanKind.SERVER
    assert format(span.context.trace_id, "032x") == _TRACE
    assert format(span.parent.span_id, "016x") == _SPAN
    assert seen["current"].span_id == span.context.span_id
    assert dict(span.attributes) == {
        "mcp.method.name": "tools/call",
        "gen_ai.tool.name": "list_projects",
    }
    assert "s3cret" not in repr(span.to_json())


def test_errors_keep_a_bounded_type_and_no_payload(telemetry: _Telemetry) -> None:
    class FakeMcpError(Exception):
        def __init__(self) -> None:
            super().__init__("secret message")
            self.error = SimpleNamespace(code=-32601, message="secret message")

    async def raises(ctx):
        raise FakeMcpError()

    async def crashes(ctx):
        raise RuntimeError("secret detail")

    async def tool_error(ctx):
        return {"isError": True, "content": [{"type": "text", "text": "secret output"}]}

    with pytest.raises(FakeMcpError):
        _run(_ctx(method="tools/list", params={}), raises)
    with pytest.raises(RuntimeError):
        _run(_ctx(method="tools/list", params={}), crashes)
    _run(_ctx(), tool_error)

    spans = telemetry.finished()
    assert [s.attributes.get("error.type") for s in spans] == ["-32601", "_OTHER", "tool_error"]
    for span in spans:
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description is None
        assert span.events == ()
        assert "secret" not in repr(span.to_json())


def test_cancellation_restores_the_context(telemetry: _Telemetry) -> None:
    async def cancelled(ctx):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(_ctx(), cancelled)

    assert telemetry.span("tools/call list_projects").attributes["error.type"] == "cancelled"


def test_notifications_and_unsafe_names_stay_bounded(telemetry: _Telemetry) -> None:
    async def ok(ctx):
        return None

    _run(_ctx(method="notifications/initialized", request_id=None), ok)
    assert telemetry.finished() == []

    _run(_ctx(method="Tools/Call; DROP", params={"name": "../../etc"}), ok)
    (span,) = telemetry.finished()
    assert span.name == "other"
    assert dict(span.attributes) == {"mcp.method.name": "other"}


def _sdk_request(provider) -> tuple[dict, list]:
    """Run the SDK's own client-span helper and injection under a step span."""
    from mcp.shared import _otel as sdk_otel
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    step_tracer = provider.get_tracer(otel_instruments.FLOW_STEWARD_SCOPE)
    sdk_tracer = provider.get_tracer(otel_mcp.MCP_SDK_SCOPE)
    meta: dict = {}
    original = sdk_otel._tracer
    sdk_otel._tracer = sdk_tracer
    try:
        with (
            step_tracer.start_as_current_span("fs.workflow.step") as step,
            sdk_otel.otel_span("MCP send tools/call list_projects", kind=SpanKind.CLIENT),
        ):
            sdk_otel.inject_trace_context(meta)
            step_span_id = format(step.get_span_context().span_id, "016x")
    finally:
        sdk_otel._tracer = original
    provider.shutdown()
    return {"meta": meta, "step_span_id": step_span_id}, list(exporter.get_finished_spans())


def test_the_sdk_records_no_span_and_injects_the_calling_step() -> None:
    from core.infrastructure.observability.otel_export_guards import (
        ScopeSuppressingTracerProvider,
    )

    seen, spans = _sdk_request(ScopeSuppressingTracerProvider(shutdown_on_exit=False))

    assert [span.name for span in spans] == ["fs.workflow.step"]
    # fs-mcp parents its server span to this, which is exported.
    assert seen["meta"]["traceparent"].split("-")[2] == seen["step_span_id"]


def test_without_suppression_the_sdk_injects_its_own_client_span() -> None:
    # The hazard: the injected parent is the SDK span, which must not be exported.
    from opentelemetry.sdk.trace import TracerProvider

    seen, spans = _sdk_request(TracerProvider(shutdown_on_exit=False))

    sdk_span = next(span for span in spans if span.name.startswith("MCP send"))
    assert seen["meta"]["traceparent"].split("-")[2] == format(sdk_span.context.span_id, "016x")
    assert seen["meta"]["traceparent"].split("-")[2] != seen["step_span_id"]


def test_the_export_policy_drops_any_sdk_span_that_is_recorded(telemetry: _Telemetry) -> None:
    tracer = telemetry.tracer_provider.get_tracer(otel_mcp.MCP_SDK_SCOPE)
    with tracer.start_as_current_span("MCP send tools/call x", kind=SpanKind.CLIENT):
        pass

    assert telemetry.finished() == []


def test_the_sync_runner_keeps_the_caller_context_in_its_thread() -> None:
    from core.application.orchestration.tools import mcp_tools_client

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")

    async def read_marker():
        return marker.get()

    async def caller():
        marker.set("caller")
        return mcp_tools_client._run_mcp_coro_sync(read_marker())

    assert asyncio.run(caller()) == "caller"


def test_an_external_connector_call_runs_outside_the_trace(
    telemetry: _Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.application.management import mcp_connector_client

    client = mcp_connector_client.StreamableHttpMcpClient.__new__(
        mcp_connector_client.StreamableHttpMcpClient
    )
    seen = {}

    def fake_request(method, params, *, name="", generated_transport_headers=None):
        seen["inside"] = trace.get_current_span().get_span_context().is_valid
        return {"result": {}}, 3

    monkeypatch.setattr(client, "_request", fake_request, raising=False)
    tracer = telemetry.tracer_provider.get_tracer(otel_instruments.FLOW_STEWARD_SCOPE)
    with tracer.start_as_current_span("POST /api/integrations/mcp/test") as outer:
        client.request("tools/call", {"name": "x"})

    assert seen["inside"] is False
    span = telemetry.span("mcp tools/call")
    assert span.kind == SpanKind.CLIENT
    assert span.parent.span_id == outer.get_span_context().span_id
    assert dict(span.attributes) == {"mcp.method.name": "tools/call"}


def test_external_modern_meta_carries_no_trace_context(telemetry: _Telemetry) -> None:
    from core.application.management.mcp_connector_client import _modern_meta

    tracer = telemetry.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("request"):
        meta = _modern_meta({"name": "x", "_meta": {"progressToken": 1}})["_meta"]

    assert "traceparent" not in meta
    assert "tracestate" not in meta
    assert meta["progressToken"] == 1
