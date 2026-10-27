"""Event Inspector API routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError


def create_api_events_router(
    *,
    event_query_service: Callable[[], Any],
    event_command_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["api-events"])

    @router.get("/api/events")
    async def api_events_list(
        request: Request,
        workflow_id: Annotated[str, Query()] = "",
        event_family: Annotated[str, Query()] = "",
        event_category: Annotated[str, Query()] = "",
        event_type: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        from_date: Annotated[str, Query()] = "",
        to_date: Annotated[str, Query()] = "",
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 0,
        limit: Annotated[int, Query()] = 25,
    ):
        """List domain events for Event Inspector."""
        try:
            payload = event_query_service().list_events(
                workflow_id=workflow_id,
                event_family=event_family,
                event_category=event_category,
                event_type=event_type,
                status=status,
                from_date=from_date,
                to_date=to_date,
                page=page,
                per_page=per_page,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return JSONResponse(payload.dump())

    @router.get("/api/events/{event_ref}")
    async def api_event_detail(request: Request, event_ref: str):
        """Return one domain event with dispatch attempts and linked commands."""
        try:
            payload = event_query_service().get_event_detail(
                event_ref,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return JSONResponse(payload.dump())

    @router.post("/api/events/{event_ref}/redrive")
    async def api_event_redrive(request: Request, event_ref: str):
        """Reset failed event dispatches to queued for replay."""
        try:
            payload = event_command_service().redrive_event(
                event_ref,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return JSONResponse(payload.dump())

    return router
