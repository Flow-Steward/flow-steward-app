"""Report generation and persisted report retrieval routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError
from core.application.management.requests import ReportGenerateRequest


def create_api_reports_router(
    *,
    reporting_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["api-reports"])

    @router.post("/api/reports/generate")
    async def api_reports_generate(request: Request, body: ReportGenerateRequest):
        try:
            payload = reporting_service().report_generate(
                body.report_type,
                body.params,
                scope=request_scope(
                    request,
                    project_id=(body.params or {}).get("project_id")
                    if isinstance(body.params, dict)
                    else None,
                ),
                persist=bool(body.persist),
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)
        return payload

    @router.get("/api/reports/{report_id}")
    async def api_reports_get(request: Request, report_id: str):
        try:
            payload = reporting_service().report_get(
                report_id,
                scope=request_scope(request),
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)
        return payload

    return router
