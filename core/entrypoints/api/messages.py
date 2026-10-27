"""Message API routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError


def create_api_messages_router(
    *,
    job_query_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["api-messages"])

    @router.get("/api/messages/{message_id}")
    async def api_message_detail(request: Request, message_id: str):
        """Return one AgentMessageV1 row by id."""
        try:
            payload = job_query_service().get_message_detail(
                message_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    return router
