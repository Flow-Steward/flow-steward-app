"""External agents API routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError
from core.application.orchestration.models import (
    ExternalAgentClaimRequestV1,
    ExternalAgentHeartbeatRequestV1,
    ExternalAgentRegisterRequestV1,
    ExternalAgentReportRequestV1,
)


def create_api_external_agents_router(
    *,
    external_agent_command_service: Callable[[], Any],
    request_principal: Callable[[Request], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["api-external-agents"])

    @router.post("/api/external-agents/register")
    async def api_external_agent_register(request: Request, body: ExternalAgentRegisterRequestV1):
        try:
            payload = external_agent_command_service().register(
                body,
                principal=request_principal(request),
                scope=request_scope(
                    request,
                    project_id=body.project_id,
                    workflow_id=body.workflow_id,
                ),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.post("/api/external-agents/heartbeat")
    async def api_external_agent_heartbeat(request: Request, body: ExternalAgentHeartbeatRequestV1):
        try:
            payload = external_agent_command_service().heartbeat(
                body,
                principal=request_principal(request),
                scope=request_scope(request, project_id=body.project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.post("/api/external-agents/claim")
    async def api_external_agent_claim(request: Request, body: ExternalAgentClaimRequestV1):
        try:
            payload = external_agent_command_service().claim(
                body,
                principal=request_principal(request),
                scope=request_scope(
                    request,
                    project_id=body.project_id,
                    workflow_id=body.workflow_id,
                ),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.post("/api/external-agents/report")
    async def api_external_agent_report(request: Request, body: ExternalAgentReportRequestV1):
        try:
            payload = external_agent_command_service().report(
                body,
                principal=request_principal(request),
                scope=request_scope(request, project_id=body.project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    return router
