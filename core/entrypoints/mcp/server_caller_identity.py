"""Request-local caller bearer propagation for the MCP transport."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

_TOKEN68_RE = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")
_transport_bearer: ContextVar[str] = ContextVar("mcp_transport_caller_bearer", default="")


def _authorization_values(headers: object) -> list[str]:
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        try:
            return [str(value) for value in getlist("authorization")]
        except (AttributeError, TypeError, ValueError):
            return []
    if not isinstance(headers, Mapping):
        return []
    values = [
        value for key, value in headers.items() if str(key).strip().lower() == "authorization"
    ]
    if len(values) != 1 or isinstance(values[0], (list, tuple, set, frozenset)):
        return []
    return [str(values[0])]


def transport_caller_bearer(ctx: object | None) -> str:
    """Parse one strict caller-supplied Authorization Bearer value."""
    request = getattr(ctx, "request", None)
    if request is None:
        request_context = getattr(ctx, "request_context", None)
        request = getattr(request_context, "request", None)
    values = _authorization_values(getattr(request, "headers", None))
    if len(values) != 1:
        return ""
    parts = values[0].strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    token = parts[1]
    return token if _TOKEN68_RE.fullmatch(token) else ""


def current_caller_bearer() -> str:
    """Return verified auth identity, or the explicitly bound dev transport bearer."""
    access_token = get_access_token()
    verified = str(access_token.token if access_token is not None else "").strip()
    if verified:
        return verified
    return _transport_bearer.get()


class McpCallerIdentityMiddleware:
    """Bind one inbound transport bearer for the lifetime of an MCP request."""

    def __init__(self, *, auth_enabled: bool) -> None:
        self._auth_enabled = bool(auth_enabled)

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        bearer = "" if self._auth_enabled else transport_caller_bearer(ctx)
        reset_token = _transport_bearer.set(bearer)
        try:
            return await call_next(ctx)
        finally:
            _transport_bearer.reset(reset_token)


__all__ = [
    "McpCallerIdentityMiddleware",
    "current_caller_bearer",
    "transport_caller_bearer",
]
