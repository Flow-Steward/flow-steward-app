"""Administrator-only, read-only Core release notice adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.responses import JSONResponse

from core.application.release.update_notice import CoreUpdateStatus
from core.infrastructure.release.update_notice import core_update_status


def create_system_core_updates_router(
    *,
    require_authenticated_user: Callable[..., dict[str, Any]],
    status_provider: Callable[[], CoreUpdateStatus] = core_update_status,
) -> APIRouter:
    router = APIRouter(tags=["system"])

    @router.get("/api/system/core-updates", response_model=CoreUpdateStatus)
    def core_updates(request: Request, response: Response) -> Any:
        # Sync discovery runs in FastAPI's worker pool, never on the event loop.
        # Keep a local guard as well as the global admin-only route classification.
        try:
            require_authenticated_user(request, required_roles={"admin"})
        except PermissionError:
            return JSONResponse(
                {"error": "Admin role required"},
                status_code=403,
                headers={"Cache-Control": "private, no-store"},
            )
        response.headers["Cache-Control"] = "private, no-store"
        return status_provider()

    return router
