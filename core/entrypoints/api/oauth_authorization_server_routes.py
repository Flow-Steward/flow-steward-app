"""Inbound OAuth protocol endpoints of ``fs-app`` (ADR 0047 decisions 3, 4, 6-9).

Thin adapters: parse the request, call :class:`InboundOAuthService`, serialize. The
endpoints are public; the middleware passes them through without deriving any
identity from a cookie or a bearer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from core.application.auth.inbound_oauth_policy import (
    BROWSER_BINDING_COOKIE,
    PENDING_AUTHORIZATION_LIFETIME,
)
from core.application.auth.inbound_oauth_service import InboundOAuthService, OAuthProtocolError

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


def _protocol_error(error: OAuthProtocolError) -> JSONResponse:
    headers = dict(_NO_STORE)
    if error.status == 429:
        headers["Retry-After"] = "60"
    return JSONResponse(error.to_payload(), status_code=error.status, headers=headers)


async def _form_fields(request: Request) -> dict[str, str]:
    """The urlencoded body as single values; JSON or a repeated field is refused."""
    content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type != _FORM_CONTENT_TYPE:
        raise OAuthProtocolError("invalid_request", "use application/x-www-form-urlencoded")
    form = await request.form()
    fields: dict[str, str] = {}
    for key, value in form.multi_items():
        if not isinstance(value, str) or key in fields:
            raise OAuthProtocolError("invalid_request", "repeated or invalid parameter")
        fields[key] = value
    return fields


class OAuthQueryRedactionFilter(logging.Filter):
    """Keep the authorization request's query (``state`` among it) out of access logs.

    ADR 0047 decision 21. The uvicorn access record carries the request target as
    its third argument; for ``/oauth/*`` only the path is kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            target = args[2]
            if target.startswith("/oauth/") and "?" in target:
                record.args = (*args[:2], target.split("?", 1)[0] + "?[redacted]", *args[3:])
        return True


def install_access_log_redaction() -> None:
    for name in ("uvicorn.access", "gunicorn.access"):
        access_logger = logging.getLogger(name)
        if not any(isinstance(item, OAuthQueryRedactionFilter) for item in access_logger.filters):
            access_logger.addFilter(OAuthQueryRedactionFilter())


def create_oauth_authorization_server_router(
    *,
    service_factory: Callable[[], InboundOAuthService | None],
    request_client_ip: Callable[[Request], str],
) -> APIRouter:
    router = APIRouter(tags=["oauth"])
    install_access_log_redaction()

    def _service() -> InboundOAuthService | None:
        try:
            return service_factory()
        except Exception:
            return None

    @router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    async def oauth_authorization_server_metadata() -> Response:
        service = _service()
        document = (
            await run_in_threadpool(service.authorization_server_metadata) if service else None
        )
        headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
        if document is None:
            return JSONResponse({"error": "oauth_unavailable"}, status_code=404, headers=headers)
        return JSONResponse(document, headers=headers)

    @router.get("/oauth/authorize", include_in_schema=False)
    async def oauth_authorize(request: Request) -> Response:
        service = _service()
        if service is None:
            outcome_location = "/oauth/consent?error=storage_missing"
            binding = None
        else:
            outcome = await run_in_threadpool(
                service.start_authorization,
                list(request.query_params.multi_items()),
                client_ip=request_client_ip(request),
            )
            outcome_location, binding = outcome.location, outcome.browser_binding
        response = RedirectResponse(outcome_location, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if binding:
            response.set_cookie(
                BROWSER_BINDING_COOKIE,
                binding,
                max_age=int(PENDING_AUTHORIZATION_LIFETIME.total_seconds()),
                path="/",
                secure=True,
                httponly=True,
                samesite="lax",
            )
        return response

    @router.post("/oauth/token", include_in_schema=False)
    async def oauth_token(request: Request) -> Response:
        try:
            fields = await _form_fields(request)
            service = _service()
            if service is None:
                raise OAuthProtocolError("temporarily_unavailable", status=503)
            payload: dict[str, Any] = await run_in_threadpool(
                service.token, fields, client_ip=request_client_ip(request)
            )
        except OAuthProtocolError as error:
            return _protocol_error(error)
        return JSONResponse(payload, headers=_NO_STORE)

    @router.post("/oauth/revoke", include_in_schema=False)
    async def oauth_revoke(request: Request) -> Response:
        try:
            fields = await _form_fields(request)
            service = _service()
            if service is None:
                raise OAuthProtocolError("temporarily_unavailable", status=503)
            await run_in_threadpool(
                service.revoke_token, fields, client_ip=request_client_ip(request)
            )
        except OAuthProtocolError as error:
            return _protocol_error(error)
        return Response(status_code=200, headers=_NO_STORE)

    return router


__all__ = [
    "OAuthQueryRedactionFilter",
    "create_oauth_authorization_server_router",
    "install_access_log_redaction",
]
