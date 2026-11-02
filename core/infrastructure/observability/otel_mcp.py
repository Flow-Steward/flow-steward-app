"""Server spans of fs-mcp, and what MCP SDK client spans may export.

The MCP SDK ships an ``OpenTelemetryMiddleware`` on every server. Its spans
record exception text, raw method names and client-chosen identifiers, so
fs-mcp removes it through the public (provisional) ``MCPServer.middleware``
list and installs ``McpTraceMiddleware`` instead:

* the parent is the W3C context the client SDK put in ``_meta`` (the client's
  "MCP send ..." span), so a tool call nests under the calling workflow step;
* the span is current while the request is handled and the previous context is
  restored on success, error and cancellation;
* arguments, raw ``_meta`` and exception payloads never reach telemetry: only a
  bounded method name, a first-party tool name and a bounded ``error.type``.

SDK spans themselves (scope ``mcp-python-sdk``) are exported only as CLIENT
spans with ``mcp.method.name`` and ``jsonrpc.request.id``; a failure becomes a
plain ``error.type`` category.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from typing import Any

from core.infrastructure.observability import otel_instruments as instruments

MCP_SDK_SCOPE = "mcp-python-sdk"
MCP_CLIENT_ATTRIBUTES = frozenset({"error.type", "jsonrpc.request.id", "mcp.method.name"})
_SDK_MIDDLEWARE = "OpenTelemetryMiddleware"
_METHOD = re.compile(r"[a-z]+(?:/[A-Za-z]+){0,2}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_ERROR_CODE = re.compile(r"-?[0-9]{1,6}")
_ERROR_TYPE_OTHER = "_OTHER"
_INVALID_PARAMS = "-32602"


def method_label(method: Any) -> str:
    if isinstance(method, str) and len(method) <= 48 and _METHOD.fullmatch(method):
        return method
    return "other"


def _tool_name(method: str, params: Any) -> str | None:
    if method != "tools/call" or not isinstance(params, Mapping):
        return None
    name = params.get("name")
    return name if isinstance(name, str) and _TOOL_NAME.fullmatch(name) else None


def _carrier(meta: Any) -> dict[str, str] | None:
    if meta is not None and not isinstance(meta, Mapping):
        dump = getattr(meta, "model_dump", None)
        meta = dump(by_alias=True) if callable(dump) else None
    return instruments.bounded_carrier(meta)


def _error_type(error: BaseException) -> str:
    code = getattr(getattr(error, "error", None), "code", None)
    if code is not None and _ERROR_CODE.fullmatch(str(code)):
        return str(code)
    if type(error).__name__ == "ValidationError":
        return _INVALID_PARAMS
    if not isinstance(error, Exception):
        return "cancelled"
    return _ERROR_TYPE_OTHER


def _tool_failed(result: Any) -> bool:
    if isinstance(result, Mapping):
        return result.get("isError") is True
    return getattr(result, "is_error", False) is True


class McpTraceMiddleware:
    """First-party SERVER span per inbound MCP request."""

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "request_id", None) is None:
            return await call_next(ctx)
        method = method_label(getattr(ctx, "method", None))
        tool = _tool_name(method, getattr(ctx, "params", None))
        attributes = {"mcp.method.name": method}
        if tool is not None:
            attributes["gen_ai.tool.name"] = tool
        span = instruments.start_span(
            f"{method} {tool}" if tool else method,
            context=instruments.remote_parent(_carrier(getattr(ctx, "meta", None))),
            attributes=attributes,
            server=True,
        )
        error_type = None
        try:
            with instruments.current_span(span):
                result = await call_next(ctx)
            if tool is not None and _tool_failed(result):
                error_type = "tool_error"
            return result
        except BaseException as exc:
            error_type = _error_type(exc)
            raise
        finally:
            instruments.end_span(span, error_type=error_type)


def install_mcp_tracing(server: Any) -> None:
    """Replace the SDK's tracing middleware of *server* with ``McpTraceMiddleware``."""
    chain = server.middleware
    chain[:] = [
        item
        for item in chain
        if not (type(item).__name__ == _SDK_MIDDLEWARE and type(item).__module__.startswith("mcp."))
        and not isinstance(item, McpTraceMiddleware)
    ]
    chain.insert(0, McpTraceMiddleware())


def normalize_client_span(span: Any, attributes: MutableMapping[str, Any]) -> str:
    """Keep the SDK's span name; a failure becomes a bounded category."""
    attributes.pop("error.type", None)
    status = getattr(span, "status", None)
    if status is not None and getattr(status.status_code, "name", "") == "ERROR":
        attributes["error.type"] = _ERROR_TYPE_OTHER
    method = attributes.get("mcp.method.name")
    if method is not None:
        attributes["mcp.method.name"] = method_label(method)
    return str(getattr(span, "name", "") or "MCP send")


__all__ = [
    "MCP_CLIENT_ATTRIBUTES",
    "MCP_SDK_SCOPE",
    "McpTraceMiddleware",
    "install_mcp_tracing",
    "method_label",
    "normalize_client_span",
]
