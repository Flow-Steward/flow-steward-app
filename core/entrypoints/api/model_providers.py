"""Model providers API routes extracted from serve.py."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceError


class ModelProviderMutationRequest(BaseModel):
    """Closed v1 request contract for custom provider persistence."""

    model_config = ConfigDict(extra="forbid")

    name: str
    provider_type: str
    base_url: str = ""
    api_key: str = ""
    api_version: str = ""
    deployment_name: str = ""
    default_model: str = ""
    upstream_revision: str = Field(default="", max_length=512)
    config: dict[str, Any] = Field(default_factory=dict)


def create_api_model_providers_router(
    *,
    model_provider_command_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["api-model-providers"])

    @router.get("/api/model-providers")
    async def api_list_model_providers(request: Request):
        try:
            scope = request_scope(request)
            rows = await asyncio.to_thread(
                model_provider_command_service().list_model_providers,
                account_id=scope.account_id,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(jsonable_encoder({"providers": rows}))

    @router.post("/api/model-providers")
    async def api_create_model_provider(request: Request, body: ModelProviderMutationRequest):
        try:
            scope = request_scope(request)
            command = body.model_dump()
            command["account_id"] = scope.account_id
            result = await asyncio.to_thread(
                model_provider_command_service().create_model_provider, command, scope=scope
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(jsonable_encoder(result), status_code=201)

    @router.put("/api/model-providers/{provider_id}")
    async def api_update_model_provider(
        request: Request, provider_id: str, body: ModelProviderMutationRequest
    ):
        try:
            scope = request_scope(request)
            command = body.model_dump()
            command["account_id"] = scope.account_id
            result = await asyncio.to_thread(
                model_provider_command_service().update_model_provider,
                provider_id,
                command,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(jsonable_encoder(result))

    @router.delete("/api/model-providers/{provider_id}")
    async def api_delete_model_provider(request: Request, provider_id: str):
        try:
            result = await asyncio.to_thread(
                model_provider_command_service().delete_model_provider,
                provider_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(jsonable_encoder(result))

    @router.post("/api/model-providers/{provider_id}/verify")
    async def api_reverify_model_provider(request: Request, provider_id: str):
        try:
            result = await asyncio.to_thread(
                model_provider_command_service().reverify_model_provider,
                provider_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            response = service_error_response(e)
            retry_after = int((getattr(e, "payload", None) or {}).get("retry_after") or 0)
            if e.status_code == 429 and retry_after > 0:
                response.headers["Retry-After"] = str(retry_after)
            return response
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(result)

    return router
