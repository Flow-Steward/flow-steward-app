"""Restricted HTTP entrypoint for the writable extension installer process."""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UNINSTALL_PATH = re.compile(r"/api/extensions/[^/]+/(uninstall|uninstall-plan)")
_EXACT_ROUTES = frozenset(
    {
        ("GET", "/api/extensions/capabilities/archive-lifecycle"),
        ("POST", "/api/extensions/install"),
        # Installing from the static catalog reaches the same installer with a
        # verified archive, so it belongs on the writable side exactly like an
        # uploaded one. Left out, it would run in the read-only API process.
        ("POST", "/api/extensions/catalog/install"),
        # A scan finishes committed uninstalls: it deletes their files from the
        # managed extensions root, which only the installer may write. Run in
        # the API process it was refused ("Permission denied:
        # '.fs-uninstall-cleanup'") and the scan answered 500.
        ("POST", "/api/extensions/scan"),
    }
)


def is_installer_route(method: str, path: str) -> bool:
    match = _UNINSTALL_PATH.fullmatch(path)
    return (method, path) in _EXACT_ROUTES or (
        match is not None and method == ("GET" if match[1] == "uninstall-plan" else "POST")
    )


class InstallerSurface:
    """Retain canonical authentication and services; expose no other API routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        method, path = scope.get("method", ""), scope.get("path", "")
        if not is_installer_route(method, path):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app() -> ASGIApp:
    """Compose the existing authenticated API inside a narrow transport boundary."""
    from core.entrypoints.api import dependencies
    from core.entrypoints.api.app_factory import create_app as create_runtime_app

    return InstallerSurface(
        create_runtime_app(
            initialize_api_dependencies=dependencies.init, runtime_service_type="installer"
        )
    )
