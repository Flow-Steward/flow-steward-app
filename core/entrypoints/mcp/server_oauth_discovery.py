"""OAuth discovery around the SDK Starlette app (ADR 0047 decision 3).

The SDK registers its protected-resource-metadata route before any custom route and
fills ``scopes_supported`` only from ``required_scopes``, which it then demands on
every request; neither expresses "these eight scopes exist, none is required". This
thin ASGI adapter answers the PRM ``GET`` itself (SDK ``ProtectedResourceMetadata``
model) and appends ``scope`` to the SDK's 401 ``WWW-Authenticate`` header. Everything
else passes through untouched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

from mcp.shared.auth import ProtectedResourceMetadata

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The scopes this resource accepts. Must equal
#: ``core.application.auth.inbound_oauth_policy.SUPPORTED_SCOPES`` (pinned by a test;
#: the MCP image does not ship the application package).
MCP_OAUTH_SCOPES: tuple[str, ...] = (
    "projects.read",
    "jobs.read",
    "jobs.write",
    "reports.read",
    "reports.write",
    "workflows.read",
    "workflows.write",
    "admin:all",
)
RESOURCE_NAME = "Flow Steward"
PRM_PREFIX = "/.well-known/oauth-protected-resource"
_CORS_HEADERS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-methods", b"GET, OPTIONS"),
    (b"access-control-allow-headers", b"mcp-protocol-version"),
]


def protected_resource_metadata(resource_url: str, issuer_url: str) -> bytes:
    metadata = ProtectedResourceMetadata.model_validate(
        {
            "resource": resource_url,
            "authorization_servers": [issuer_url],
            "scopes_supported": list(MCP_OAUTH_SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": RESOURCE_NAME,
        }
    )
    return metadata.model_dump_json(exclude_none=True).encode("utf-8")


def protected_resource_metadata_path(resource_url: str) -> str:
    """RFC 9728 §3.1: the well-known prefix between host and the resource path."""
    path = urlsplit(resource_url).path
    return PRM_PREFIX + ("" if path in ("", "/") else path)


class OAuthDiscoveryAdapter:
    """Serve PRM and complete the 401 challenge; pass everything else to ``app``."""

    def __init__(self, app: ASGIApp, *, resource_url: str, issuer_url: str) -> None:
        self._app = app
        self._metadata_path = protected_resource_metadata_path(resource_url)
        self._metadata = protected_resource_metadata(resource_url, issuer_url)
        self._scope_parameter = f'scope="{" ".join(MCP_OAUTH_SCOPES)}"'.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path.rstrip("/") == self._metadata_path:
            await self._metadata_response(str(scope.get("method") or "GET").upper(), send)
            return
        await self._app(scope, receive, self._challenge_send(send))

    async def _metadata_response(self, method: str, send: Send) -> None:
        if method == "OPTIONS":
            status, body, headers = 204, b"", list(_CORS_HEADERS)
        elif method in {"GET", "HEAD"}:
            status = 200
            body = self._metadata if method == "GET" else b""
            headers = [
                (b"content-type", b"application/json"),
                (b"cache-control", b"public, max-age=3600"),
                *_CORS_HEADERS,
            ]
        else:
            status, body, headers = 405, b"", [(b"allow", b"GET, OPTIONS")]
        headers.append((b"content-length", str(len(body)).encode("ascii")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    def _challenge_send(self, send: Send) -> Send:
        scope_parameter = self._scope_parameter

        async def wrapped(message: Message) -> None:
            if message.get("type") == "http.response.start" and message.get("status") == 401:
                headers = []
                for name, value in message.get("headers") or []:
                    if bytes(name).lower() == b"www-authenticate" and b"scope=" not in value:
                        value = bytes(value) + b", " + scope_parameter
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped


__all__ = [
    "MCP_OAUTH_SCOPES",
    "OAuthDiscoveryAdapter",
    "protected_resource_metadata",
    "protected_resource_metadata_path",
]
