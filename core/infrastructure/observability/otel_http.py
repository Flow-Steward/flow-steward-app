"""HTTP server spans of the FastAPI app, reduced to a fixed public shape.

The standard FastAPI instrumentation produces the spans; this module decides
where it runs and what survives export:

* only SERVER spans, with no ASGI send/receive spans and no HTTP metrics
  (a no-op meter provider: automatic HTTP measurements are not in the contract);
* WebSocket, health and metrics routes are not traced at all;
* attributes are an allowlist of method, route template, status and protocol;
  client address, user agent, the raw path, query, fragment and headers never
  leave, so OAuth callback parameters cannot either;
* ``http.route`` and the span name keep a route only when it is a template the
  app registered; anything else falls back to the method alone, as FastAPI does
  for an unmatched request.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

_ERROR_TYPE = "error.type"
_HTTP_METHOD = "http.request.method"
_HTTP_ROUTE = "http.route"

FASTAPI_SCOPE = "opentelemetry.instrumentation.fastapi"
HTTP_SERVER_ATTRIBUTES = frozenset(
    {
        _ERROR_TYPE,
        _HTTP_METHOD,
        "http.response.status_code",
        _HTTP_ROUTE,
        "network.protocol.version",
        "url.scheme",
    }
)
# Regexes the instrumentation searches in the request URL.
EXCLUDED_URLS = ",".join(
    (
        r"/ws/",
        r"/health(?:/[a-z]+)?(?:\?|$)",
        r"/metrics(?:\?|$)",
    )
)
_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)
_STATUS_ERROR = re.compile(r"[1-5](?a:\d{2})")
_ERROR_TYPE_OTHER = "_OTHER"

_lock = threading.Lock()
_routes: set[str] = set()
# Route count last registered per app, so routes added at runtime (extension
# pages) are picked up without walking the table on every request.
_registered_counts: dict[int, int] = {}


def register_routes(templates: Iterable[str]) -> None:
    """Record route templates an app serves; only these may name a span."""
    with _lock:
        _routes.update(t for t in templates if isinstance(t, str) and t.startswith("/"))


def app_route_templates(routes: Iterable[Any], prefix: str = "") -> list[str]:
    """Route templates of *routes*, including those of mounted sub-routers."""
    templates: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            continue
        templates.append(prefix + path)
        nested = getattr(route, "routes", None)
        if nested:
            templates.extend(app_route_templates(nested, prefix + path))
    return templates


def _refresh_routes(span: Any, scope: Any) -> None:
    """Server request hook: register the app's templates whenever its table grows."""
    del span
    app = scope.get("app") if isinstance(scope, dict) else None
    routes = getattr(app, "routes", None)
    if routes is None:
        return
    count = len(routes)
    with _lock:
        if _registered_counts.get(id(app)) == count:
            return
        _registered_counts[id(app)] = count
    register_routes(app_route_templates(routes))


def normalize_server_span(span: Any, attributes: MutableMapping[str, Any]) -> str:
    """The public name of an HTTP server span; reduces *attributes* in place."""
    del span  # FastAPI names it from the same raw route this rebuilds safely.
    method = attributes.get(_HTTP_METHOD)
    if method not in _METHODS:
        method = "HTTP"
        attributes.pop(_HTTP_METHOD, None)
    route = attributes.get(_HTTP_ROUTE)
    with _lock:
        known = isinstance(route, str) and route in _routes
    if not known:
        attributes.pop(_HTTP_ROUTE, None)
    error_type = attributes.get(_ERROR_TYPE)
    if error_type is not None and not (
        isinstance(error_type, str) and _STATUS_ERROR.fullmatch(error_type)
    ):
        # Exception class names are not a bounded set.
        attributes[_ERROR_TYPE] = _ERROR_TYPE_OTHER
    return f"{method} {route}" if known else method


def instrument_fastapi(
    app: Any,
    *,
    environment: MutableMapping[str, str] | None = None,
    tracer_provider: Any | None = None,
) -> bool:
    """Instrument *app* for HTTP server spans when trace export is configured.

    Runs before the app serves anything: Starlette builds its middleware stack
    on the first ASGI call (the lifespan startup), and the tracer resolves to
    the provider that startup installs.
    """
    from core.infrastructure.observability.otel_config import load_telemetry_settings
    from core.infrastructure.observability.otel_environment import apply_environment_policy

    # Pins the HTTP semantic-conventions mode before the instrumentation reads it.
    apply_environment_policy(environment)
    if not load_telemetry_settings(environment).traces.enabled:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.metrics import NoOpMeterProvider
    except ImportError:
        logger.info("FastAPI instrumentation is not installed; HTTP spans are off")
        return False
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            server_request_hook=_refresh_routes,
            tracer_provider=tracer_provider,
            meter_provider=NoOpMeterProvider(),
            excluded_urls=EXCLUDED_URLS,
            exclude_spans=["receive", "send"],
            http_capture_headers_server_request=[],
            http_capture_headers_server_response=[],
        )
    except Exception as exc:
        logger.warning("FastAPI instrumentation unavailable: %s", type(exc).__name__)
        return False
    return True


def _reset_for_tests() -> None:
    with _lock:
        _routes.clear()
        _registered_counts.clear()


__all__ = [
    "EXCLUDED_URLS",
    "FASTAPI_SCOPE",
    "HTTP_SERVER_ATTRIBUTES",
    "app_route_templates",
    "instrument_fastapi",
    "normalize_server_span",
    "register_routes",
]
