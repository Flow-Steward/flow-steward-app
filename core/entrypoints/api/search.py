"""Global search API route."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError, ServiceUnavailableError
from core.application.search.global_search_categories import (
    DEFAULT_GLOBAL_SEARCH_CATEGORY,
    GlobalSearchConfiguration,
    global_search_category_groups,
    global_search_configuration,
)

SEARCH_REQUEST_TIMEOUT_SECONDS = 12.0


def create_api_search_router(
    *,
    search_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["api-search"])

    @router.get("/api/search/config", response_model=GlobalSearchConfiguration)
    async def api_search_config(request: Request) -> GlobalSearchConfiguration | JSONResponse:
        try:
            return global_search_configuration(request_scope(request))
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    @router.get("/api/search")
    async def api_search(
        request: Request,
        query: Annotated[str, Query()] = "",
        category: Annotated[str, Query()] = DEFAULT_GLOBAL_SEARCH_CATEGORY,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        cursor: Annotated[str, Query()] = "",
        project_id: Annotated[str | None, Query()] = None,
        workspace_id: Annotated[str | None, Query()] = None,
    ):
        try:
            normalized_category, result_groups = global_search_category_groups(category)
            filters = {"result_groups": list(result_groups)}
            if workspace_id and str(workspace_id).strip():
                filters["workspace_id"] = str(workspace_id).strip()
            if normalized_category == DEFAULT_GLOBAL_SEARCH_CATEGORY:
                filters["group_item_limit"] = 5
            elif str(cursor or "").strip():
                filters["cursor"] = str(cursor or "").strip()
            service = search_service()
            scope = request_scope(request, project_id=project_id, workspace_id=workspace_id)
            try:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(
                        service.global_search,
                        query,
                        scope=scope,
                        filters=filters,
                        limit=limit,
                    ),
                    timeout=SEARCH_REQUEST_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise ServiceUnavailableError(
                    "Search request timed out",
                    payload={"error": "Search request timed out", "code": "search_timeout"},
                ) from exc
            if isinstance(payload, dict):
                payload.setdefault("category", normalized_category)
            return payload
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    return router


__all__ = ["create_api_search_router"]
