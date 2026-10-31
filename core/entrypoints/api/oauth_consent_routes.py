"""Session-only HTTP routes of inbound OAuth: consent, grants and the account switch.

ADR 0047 decisions 4, 6, 15, 16. The middleware admits only an interactive browser
session (cookie + CSRF) on these paths; every bearer, PAT and OAuth included, gets
403 before a handler runs. Handlers parse, call the service and map its errors.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from core.application.auth.inbound_oauth_policy import BROWSER_BINDING_COOKIE
from core.application.auth.inbound_oauth_service import ConsentError, InboundOAuthService

_NO_STORE = {"Cache-Control": "no-store"}


class OAuthConsentDecisionRequest(BaseModel):
    """Approve with the chosen access, or deny."""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(approve|deny)$")
    preset: str | None = Field(default=None, max_length=64)
    expiry_days: int | None = None
    project_ids: list[str] = Field(default_factory=list, max_length=500)


class McpOAuthSettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _consent_error(error: ConsentError) -> JSONResponse:
    payload: dict[str, Any] = {"error": error.code}
    if error.message and error.message != error.code:
        payload["message"] = error.message
    return JSONResponse(payload, status_code=error.status, headers=_NO_STORE)


def create_oauth_consent_router(
    *,
    service_factory: Callable[[], InboundOAuthService | None],
) -> APIRouter:
    router = APIRouter(tags=["oauth"])

    def _service() -> InboundOAuthService:
        service = None
        try:
            service = service_factory()
        except Exception:
            service = None
        if service is None:
            raise ConsentError("storage_missing", 503)
        return service

    def _user(request: Request) -> dict[str, Any] | None:
        user = getattr(request.state, "auth_user", None)
        return user if isinstance(user, dict) else None

    @router.get("/api/oauth/authorizations/{request_id}", include_in_schema=False)
    async def oauth_consent_view(request_id: str, request: Request):
        try:
            payload = await run_in_threadpool(
                _service().consent_view,
                request_id,
                user=_user(request),
                browser_binding=request.cookies.get(BROWSER_BINDING_COOKIE),
            )
        except ConsentError as error:
            return _consent_error(error)
        return JSONResponse(payload, headers=_NO_STORE)

    @router.post("/api/oauth/authorizations/{request_id}/decision", include_in_schema=False)
    async def oauth_consent_decision(
        request_id: str, request: Request, body: OAuthConsentDecisionRequest
    ):
        try:
            redirect_to = await run_in_threadpool(
                _service().decide,
                request_id,
                user=_user(request),
                browser_binding=request.cookies.get(BROWSER_BINDING_COOKIE),
                decision=body.decision,
                preset_id=body.preset,
                expiry_days=body.expiry_days,
                project_ids=list(body.project_ids),
            )
        except ConsentError as error:
            return _consent_error(error)
        response = JSONResponse({"redirect_to": redirect_to}, headers=_NO_STORE)
        response.delete_cookie(BROWSER_BINDING_COOKIE, path="/", secure=True, httponly=True)
        return response

    @router.get("/api/auth/oauth-grants", include_in_schema=False)
    async def oauth_grants_list(request: Request):
        try:
            payload = await run_in_threadpool(_service().list_grants, _user(request))
        except ConsentError as error:
            return _consent_error(error)
        return JSONResponse(payload, headers=_NO_STORE)

    @router.post("/api/auth/oauth-grants/{grant_id}/revoke", include_in_schema=False)
    async def oauth_grant_revoke(grant_id: str, request: Request):
        try:
            grant = await run_in_threadpool(_service().revoke_user_grant, _user(request), grant_id)
        except ConsentError as error:
            return _consent_error(error)
        return JSONResponse({"status": "revoked", "grant": grant}, headers=_NO_STORE)

    def _admin_account(request: Request) -> tuple[str, str]:
        user = _user(request) or {}
        account_id = str(user.get("account_id") or "").strip()
        if not account_id:
            raise ConsentError("authentication_required", 401)
        return account_id, str(user.get("username") or "").strip()

    @router.get("/api/settings/mcp-oauth", include_in_schema=False)
    async def mcp_oauth_setting_get(request: Request):
        try:
            account_id, _actor = _admin_account(request)
            payload = await run_in_threadpool(_service().account_setting, account_id)
        except ConsentError as error:
            return _consent_error(error)
        return JSONResponse(payload, headers=_NO_STORE)

    @router.put("/api/settings/mcp-oauth", include_in_schema=False)
    async def mcp_oauth_setting_put(request: Request, body: McpOAuthSettingRequest):
        try:
            account_id, actor = _admin_account(request)
            payload = await run_in_threadpool(
                _service().set_account_setting, account_id, enabled=body.enabled, actor=actor
            )
        except ConsentError as error:
            return _consent_error(error)
        return JSONResponse(payload, headers=_NO_STORE)

    return router


__all__ = ["create_oauth_consent_router"]
