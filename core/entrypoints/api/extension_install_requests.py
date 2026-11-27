"""Routes for requesting an extension install from a company account (ADR 0084).

Controllers only: every rule lives in
``core.application.extensions.install_requests``. Installing the code is not here; the
installation administrator's page installs through ``POST /api/extensions/catalog/install``
first, then approves.

Plain ``def`` handlers: the store work is synchronous database access.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from core.application.extensions.install_requests import (
    MAX_NOTE_CHARS,
    MAX_REASON_CHARS,
    ExtensionInstallRequestService,
)
from core.application.management.errors import ServiceError


class ExtensionInstallRequestCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extension_id: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)


class ExtensionInstallRequestDeclineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=MAX_REASON_CHARS)


def create_extension_install_requests_router(
    *,
    install_request_service: Callable[[], ExtensionInstallRequestService],
    request_scope: Callable[[Request], Any],
    service_error_response: Callable[..., Any],
    api_error_response: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["extensions"])

    def _answer(request: Request, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)

    @router.get("/api/extensions/install-requests")
    def api_extension_install_requests(request: Request):
        """The installation administrator's pending requests, or the caller's account's."""
        return _answer(
            request,
            lambda: install_request_service().list_requests(scope=request_scope(request)),
        )

    @router.post("/api/extensions/install-requests")
    def api_extension_install_request_create(
        request: Request, body: ExtensionInstallRequestCreateBody
    ):
        return _answer(
            request,
            lambda: install_request_service().create(
                body.extension_id, note=body.note, scope=request_scope(request)
            ),
        )

    @router.post("/api/extensions/install-requests/{request_id}/cancel")
    def api_extension_install_request_cancel(request: Request, request_id: str):
        return _answer(
            request,
            lambda: install_request_service().cancel(request_id, scope=request_scope(request)),
        )

    @router.post("/api/extensions/install-requests/{request_id}/approve")
    def api_extension_install_request_approve(request: Request, request_id: str):
        return _answer(
            request,
            lambda: install_request_service().approve(request_id, scope=request_scope(request)),
        )

    @router.post("/api/extensions/install-requests/{request_id}/decline")
    def api_extension_install_request_decline(
        request: Request,
        request_id: str,
        body: ExtensionInstallRequestDeclineBody | None = None,
    ):
        reason = body.reason if body is not None else ""
        return _answer(
            request,
            lambda: install_request_service().decline(
                request_id, reason=reason, scope=request_scope(request)
            ),
        )

    return router


__all__ = [
    "ExtensionInstallRequestCreateBody",
    "ExtensionInstallRequestDeclineBody",
    "create_extension_install_requests_router",
]
