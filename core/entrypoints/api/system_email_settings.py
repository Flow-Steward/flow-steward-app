"""Settings -> Email: the connection that sends the platform's own email.

GET    /api/settings/system-email          -- the selection, its state, and the candidates
PUT    /api/settings/system-email          -- choose and confirm a connection ({"connection_id"})
POST   /api/settings/system-email/confirm  -- confirm the current selection again
DELETE /api/settings/system-email          -- clear the selection (no system email)

Only a platform administrator may read or change it; the application layer
enforces that and audits every change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from core.application.management import ServiceError


class SystemEmailSenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(min_length=1, max_length=200)


def create_api_system_email_settings_router(
    *,
    get_logger_only: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[[ServiceError], Any],
    api_error_response: Callable[..., Any],
) -> APIRouter:
    from core.application.extensions import system_email_sender

    router = APIRouter(tags=["settings"])

    def _store() -> Any:
        return get_logger_only()

    def _require_admin(request: Request) -> Any:
        from core.application.management.errors import ServiceForbiddenError

        scope = request_scope(request)
        if not system_email_sender.is_platform_admin(scope):
            raise ServiceForbiddenError(
                "Only a platform administrator can manage the system email sender",
                payload={"error": "platform_admin_required", "code": "platform_admin_required"},
            )
        return scope

    async def _run(request: Request, action: Callable[[Any, Any], Any]) -> Any:
        bl = _store()
        if bl is None:
            return JSONResponse({"error": "Database not configured"}, status_code=503)
        try:
            scope = _require_admin(request)
            await run_in_threadpool(action, bl, scope)
            return await run_in_threadpool(system_email_sender.settings_view, bl)
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    @router.get("/api/settings/system-email")
    async def get_system_email_settings(request: Request):
        return await _run(request, lambda _bl, _scope: None)

    @router.put("/api/settings/system-email")
    async def select_system_email_sender(request: Request, body: SystemEmailSenderRequest):
        return await _run(
            request,
            lambda bl, scope: system_email_sender.select_sender(bl, scope, body.connection_id),
        )

    @router.post("/api/settings/system-email/confirm")
    async def confirm_system_email_sender(request: Request):
        return await _run(request, system_email_sender.confirm_sender)

    @router.delete("/api/settings/system-email")
    async def clear_system_email_sender(request: Request):
        return await _run(
            request, lambda bl, scope: system_email_sender.select_sender(bl, scope, "")
        )

    return router


__all__ = ["SystemEmailSenderRequest", "create_api_system_email_settings_router"]
