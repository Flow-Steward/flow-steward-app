"""Session-only HTTP routes of the "connect an AI agent" wizard (task 4).

The middleware admits only an interactive browser session (cookie + CSRF) under
``/api/auth/agent-access/``: no PAT, machine or OAuth bearer can mint a token here.
Handlers parse, call :class:`AgentConnectionService` and serialize; every response is
``no-store`` because the creation response carries the one-time secret.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from core.application.auth.agent_connection_service import (
    AgentConnectionError,
    AgentConnectionService,
)
from core.application.management.errors import ServiceValidationError
from core.application.management.idempotency_keys import validate_idempotency_key

_NO_STORE = {"Cache-Control": "no-store"}


class AgentAccessTokenRequest(BaseModel):
    """One wizard attempt; the ``Idempotency-Key`` header names the attempt."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=64)
    package_id: str = Field(min_length=1, max_length=64)
    preset: str = Field(min_length=1, max_length=64)
    expiry_days: int
    project_ids: list[str] = Field(default_factory=list, max_length=500)


def _error(error: AgentConnectionError) -> JSONResponse:
    return JSONResponse(error.to_payload(), status_code=error.status, headers=_NO_STORE)


def create_agent_connection_router(
    *,
    service_factory: Callable[[], AgentConnectionService | None],
) -> APIRouter:
    router = APIRouter(tags=["auth"])

    def _service() -> AgentConnectionService:
        try:
            service = service_factory()
        except Exception:
            service = None
        if service is None:
            raise AgentConnectionError("storage_missing", 503)
        return service

    def _user(request: Request) -> dict[str, Any] | None:
        user = getattr(request.state, "auth_user", None)
        return user if isinstance(user, dict) else None

    @router.get("/api/auth/agent-access/setup", include_in_schema=False)
    async def agent_access_setup(request: Request):
        try:
            payload = await run_in_threadpool(_service().setup_view, _user(request))
        except AgentConnectionError as error:
            return _error(error)
        return JSONResponse(payload, headers=_NO_STORE)

    @router.post("/api/auth/agent-access/tokens", include_in_schema=False)
    async def agent_access_issue_token(
        request: Request,
        body: AgentAccessTokenRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        try:
            attempt_key = validate_idempotency_key(idempotency_key)
        except ServiceValidationError as error:
            return JSONResponse(error.payload, status_code=error.status_code, headers=_NO_STORE)
        if not attempt_key:
            return JSONResponse(
                {"error": "idempotency_key_required"}, status_code=428, headers=_NO_STORE
            )
        try:
            payload = await run_in_threadpool(
                lambda: _service().issue_token(
                    _user(request),
                    attempt_key=attempt_key,
                    client_id=body.client_id,
                    package_id=body.package_id,
                    preset_id=body.preset,
                    project_ids=list(body.project_ids),
                    expiry_days=body.expiry_days,
                )
            )
        except AgentConnectionError as error:
            return _error(error)
        status = 201 if payload.get("status") == "created" else 200
        return JSONResponse(payload, status_code=status, headers=_NO_STORE)

    @router.get("/api/auth/agent-access/tokens/{token_id}", include_in_schema=False)
    async def agent_access_token_status(token_id: str, request: Request):
        try:
            payload = await run_in_threadpool(_service().token_status, _user(request), token_id)
        except AgentConnectionError as error:
            return _error(error)
        return JSONResponse(payload, headers=_NO_STORE)

    return router


__all__ = ["create_agent_connection_router"]
