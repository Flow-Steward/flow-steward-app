"""Forward authenticated archive HTTP traffic to the private writable process."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from core.application.extensions.binding_constants import MAX_EXTENSION_ARCHIVE_BYTES
from core.application.lifecycle.contracts import validate_internal_http_origin
from core.entrypoints.api.extension_installer import is_installer_route
from core.entrypoints.web.frontend_shell import PUBLIC_ROUTE_GROUPS
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_LOGGER = logging.getLogger(__name__)
_DEFAULT_INSTALLER_ORIGIN = "http://fs-extension-installer:8099"
_MAX_UPLOAD_BYTES = MAX_EXTENSION_ARCHIVE_BYTES + 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
    b"content-length",
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=300, trust_env=False, follow_redirects=False)


def _installer_origin() -> str:
    origin = str(_runtime_env.get("FS_EXTENSION_INSTALLER_ORIGIN") or _DEFAULT_INSTALLER_ORIGIN)
    parsed = validate_internal_http_origin(origin)
    return f"http://{parsed.netloc}"


def _stream(
    client: httpx.AsyncClient, scope: Scope, headers: list[tuple[bytes, bytes]], content: bytes
) -> AbstractAsyncContextManager[httpx.Response]:
    return client.stream(
        scope["method"], _installer_origin() + scope["path"], headers=headers, content=content
    )


def _needs_projection_sync(path: str) -> bool:
    if path.startswith("/api/"):
        return not path.startswith("/api/auth/") and path not in {
            "/api/health",
            "/api/app/bootstrap",
        }
    shell_path = path.removeprefix("/app/") if path.startswith("/app/") else path.lstrip("/")
    return path == "/app" or shell_path.split("/", 1)[0] in PUBLIC_ROUTE_GROUPS - {"auth"}


class ExtensionInstallerProxy:
    """Runs inside the main API's auth middleware; installer authenticates again."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        refresh: Callable[[], None],
        client_factory: Callable[[], httpx.AsyncClient] = _client,
        synchronize: Callable[[], None] | None = None,
    ) -> None:
        self.app = app
        self.refresh = refresh
        self.client_factory = client_factory
        self.synchronize = synchronize

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and self.synchronize is not None
            and _needs_projection_sync(scope["path"])
        ):
            await run_in_threadpool(self.synchronize)
        if scope["type"] != "http" or not is_installer_route(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        async for chunk in Request(scope, receive).stream():
            if len(body) + len(chunk) > _MAX_UPLOAD_BYTES:
                await JSONResponse(
                    {"error": "Extension archive request is too large"}, status_code=413
                )(scope, receive, send)
                return
            body.extend(chunk)
        # Preserve caller cookies, bearer, origin and CSRF headers. The destination
        # is fixed by the deployment contract, never by request headers or input.
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in _HOP_HEADERS]
        try:
            async with (
                self.client_factory() as client,
                _stream(client, scope, headers, bytes(body)) as upstream,
            ):
                content = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise httpx.RequestError("Installer response exceeds limit")
                    content.extend(chunk)
                response = Response(bytes(content), status_code=upstream.status_code)
                response.raw_headers.extend(
                    (k, v)
                    for k, v in upstream.headers.raw
                    if k.lower() not in _HOP_HEADERS | {b"content-encoding"}
                )
        except httpx.RequestError:
            # A lost response can follow a committed installation. Never replay
            # or silently fall back to a local installer on this path.
            await JSONResponse(
                {
                    "error": "Installer response unavailable. Check installed extensions before retrying."
                },
                status_code=502,
            )(scope, receive, send)
            return
        if scope["method"] == "POST" and 200 <= response.status_code < 300:
            try:
                await run_in_threadpool(self.refresh)
            except Exception:
                _LOGGER.exception("Extension installed but local projection refresh failed")
        await response(scope, receive, send)
