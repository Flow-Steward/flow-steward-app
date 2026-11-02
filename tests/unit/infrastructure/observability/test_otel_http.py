from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.instrumentation.fastapi")

from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from core.infrastructure.observability import otel_http
from core.infrastructure.observability.otel_export_guards import (
    DEFAULT_SPAN_EXPORT_POLICY,
    SanitizingSpanExporter,
)


def test_status_error_code_accepts_only_ascii_digits() -> None:
    assert otel_http._STATUS_ERROR.fullmatch("500")
    assert not otel_http._STATUS_ERROR.fullmatch("5\u0660\u0660")


_ENABLED = {
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces",
}


def _app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/api/v2")

    @router.get("/projects/{project_id}/jobs/{job_id}")
    def job(project_id: str, job_id: str) -> dict:
        return {"ok": True}

    @router.get("/integrations/oauth/callback")
    def oauth_callback(code: str = "", state: str = "") -> dict:
        return {"ok": True}

    @router.post("/boom")
    def boom() -> dict:
        raise RuntimeError("secret detail token=abc")

    app.include_router(router)

    @app.get("/health/live")
    def live() -> dict:
        return {"ok": True}

    @app.get("/metrics")
    def metrics() -> str:
        return "x 1"

    @app.websocket("/ws/jobs/{job_id}")
    async def ws(websocket: WebSocket, job_id: str) -> None:
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    return app


@pytest.fixture(autouse=True)
def stable_http_conventions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Production pins the mode in the process environment before the first
    # instrumentation import; a test process may have initialized it already.
    from opentelemetry.instrumentation import _semconv

    stability = _semconv._OpenTelemetrySemanticConventionStability
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    monkeypatch.setattr(stability, "_initialized", False)
    monkeypatch.setattr(stability, "_OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING", {})


@pytest.fixture
def traced():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(
        SimpleSpanProcessor(SanitizingSpanExporter(exporter, DEFAULT_SPAN_EXPORT_POLICY))
    )
    app = _app()
    otel_http._reset_for_tests()
    assert otel_http.instrument_fastapi(app, environment=dict(_ENABLED), tracer_provider=provider)
    client = TestClient(app, raise_server_exceptions=False)
    yield app, client, exporter
    FastAPIInstrumentor.uninstrument_app(app)
    otel_http._reset_for_tests()


def _only_span(exporter):
    spans = list(exporter.get_finished_spans())
    assert len(spans) == 1, [(span.name, span.kind) for span in spans]
    return spans[0]


def test_a_request_exports_one_server_span_with_the_route_template(traced) -> None:
    _, client, exporter = traced

    client.get(
        "/api/v2/projects/p-secret/jobs/j-secret?api_key=hunter2#frag",
        headers={"User-Agent": "curl/8", "Authorization": "Bearer t"},
    )

    span = _only_span(exporter)
    assert span.kind == SpanKind.SERVER
    assert span.name == "GET /api/v2/projects/{project_id}/jobs/{job_id}"
    assert set(span.attributes) <= otel_http.HTTP_SERVER_ATTRIBUTES
    assert span.attributes["http.route"] == "/api/v2/projects/{project_id}/jobs/{job_id}"
    assert span.attributes["http.response.status_code"] == 200
    exported = repr(dict(span.attributes)) + span.name
    for secret in ("p-secret", "j-secret", "hunter2", "curl", "Bearer", "127.0.0.1", "testclient"):
        assert secret not in exported


def test_oauth_callback_parameters_never_leave(traced) -> None:
    _, client, exporter = traced

    client.get("/api/v2/integrations/oauth/callback?code=AUTH-CODE&state=STATE-1")

    span = _only_span(exporter)
    assert span.name == "GET /api/v2/integrations/oauth/callback"
    assert "AUTH-CODE" not in repr(dict(span.attributes))
    assert "STATE-1" not in repr(dict(span.attributes))


def test_an_unmatched_path_is_named_by_the_method_only(traced) -> None:
    _, client, exporter = traced

    client.get("/api/v2/unknown/raw-segment")

    span = _only_span(exporter)
    assert span.name == "GET"
    assert "http.route" not in span.attributes
    assert span.attributes["http.response.status_code"] == 404


def test_a_server_error_has_a_bounded_type_and_no_exception_text(traced) -> None:
    _, client, exporter = traced

    assert client.post("/api/v2/boom").status_code == 500

    span = _only_span(exporter)
    assert span.status.description is None
    assert span.events == ()
    assert span.attributes.get("error.type") in {"500", "_OTHER"}
    assert "secret detail" not in repr(dict(span.attributes))


def test_health_metrics_and_websockets_are_not_traced(traced) -> None:
    _, client, exporter = traced

    client.get("/health/live")
    client.get("/metrics")
    with client.websocket_connect("/ws/jobs/j-1") as websocket:
        websocket.receive_text()

    assert list(exporter.get_finished_spans()) == []


def test_routes_added_at_runtime_are_recognized(traced) -> None:
    app, client, exporter = traced

    @app.get("/extensions/{extension_id}/page")
    def page(extension_id: str) -> dict:
        return {"ok": True}

    client.get("/extensions/ext-1/page")

    assert _only_span(exporter).name == "GET /extensions/{extension_id}/page"


def test_export_switched_off_leaves_the_app_uninstrumented() -> None:
    app = _app()

    assert otel_http.instrument_fastapi(app, environment={}) is False
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)


def test_the_conventions_mode_is_pinned_before_instrumenting() -> None:
    environment = {**_ENABLED, "OTEL_SEMCONV_STABILITY_OPT_IN": "http/dup"}
    app = _app()
    provider = TracerProvider(shutdown_on_exit=False)
    try:
        otel_http.instrument_fastapi(app, environment=environment, tracer_provider=provider)
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    assert environment["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"


@pytest.mark.parametrize(
    ("attributes", "name", "kept"),
    [
        ({"http.request.method": "BREW", "http.route": "/x"}, "HTTP", set()),
        ({"http.request.method": "GET", "http.route": "/raw/abc"}, "GET", {"http.request.method"}),
        (
            {"http.request.method": "GET", "error.type": "app.errors.SecretError"},
            "GET",
            {"http.request.method", "error.type"},
        ),
    ],
)
def test_server_span_normalization(attributes, name, kept) -> None:
    otel_http._reset_for_tests()
    reduced = dict(attributes)

    assert otel_http.normalize_server_span(None, reduced) == name
    assert set(reduced) == kept
    if "error.type" in reduced:
        assert reduced["error.type"] == "_OTHER"
