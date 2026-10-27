"""Personal access token HTTP routes and their explicit request dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.auth.principal import (
    TokenAuthorityError,
    enforce_token_scope_within_caller_authority,
)
from core.application.management.requests import AuthPersonalTokenCreateRequest

CURRENT_ACCOUNT_NOT_FOUND_MESSAGE = "Current account not found"


@dataclass(frozen=True, slots=True)
class PersonalTokenRouteDependencies:
    local_auth_enabled: Callable[[], bool]
    get_logger_only: Callable[[], Any]
    auth_tables_available: Callable[[Any], bool]
    auth_pat_tables_available: Callable[[Any], bool]
    require_authenticated_user: Callable[..., dict]
    api_error_response: Callable[[Exception], JSONResponse]
    log_auth_audit_event: Callable[..., None]
    err_local_auth_disabled: str
    err_database_not_configured: str
    err_auth_tables_not_available: str
    err_pat_tables_not_available: str
    err_authentication_required: str
    err_token_not_found: str


def register_personal_token_routes(
    router: APIRouter, dependencies: PersonalTokenRouteDependencies
) -> None:
    _local_auth_enabled = dependencies.local_auth_enabled
    _get_logger_only = dependencies.get_logger_only
    _auth_tables_available = dependencies.auth_tables_available
    _auth_pat_tables_available = dependencies.auth_pat_tables_available
    _require_authenticated_user = dependencies.require_authenticated_user
    _api_error_response = dependencies.api_error_response
    _log_auth_audit_event = dependencies.log_auth_audit_event
    ERR_LOCAL_AUTH_DISABLED = dependencies.err_local_auth_disabled
    ERR_DATABASE_NOT_CONFIGURED = dependencies.err_database_not_configured
    ERR_AUTH_TABLES_NOT_AVAILABLE = dependencies.err_auth_tables_not_available
    ERR_PAT_TABLES_NOT_AVAILABLE = dependencies.err_pat_tables_not_available
    ERR_AUTHENTICATION_REQUIRED = dependencies.err_authentication_required
    ERR_TOKEN_NOT_FOUND = dependencies.err_token_not_found

    @router.get("/api/auth/tokens")
    async def api_auth_tokens_list(
        request: Request,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 25,
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_pat_tables_available(bl):
            return JSONResponse({"error": ERR_PAT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            auth_user = _require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)

        normalized_page = max(1, int(page or 1))
        normalized_per_page = max(1, min(int(per_page or 25), 200))
        offset = (normalized_page - 1) * normalized_per_page
        user_id = str(auth_user.get("id") or "").strip()

        try:
            total = bl.count_user_pats(user_id) if hasattr(bl, "count_user_pats") else 0
            if hasattr(bl, "list_user_pats_paginated"):
                tokens = bl.list_user_pats_paginated(
                    user_id,
                    limit=normalized_per_page,
                    offset=offset,
                )
            else:
                tokens = bl.list_user_pats(user_id)
                total = len(tokens)
                tokens = tokens[offset : offset + normalized_per_page]
        except Exception as e:
            return _api_error_response(e)
        return {
            "tokens": tokens,
            "total": int(total),
            "page": normalized_page,
            "per_page": normalized_per_page,
            "total_pages": max(1, -(-int(total) // normalized_per_page)),
        }

    @router.post("/api/auth/tokens")
    async def api_auth_tokens_create(request: Request, body: AuthPersonalTokenCreateRequest):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_pat_tables_available(bl):
            return JSONResponse({"error": ERR_PAT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            auth_user = _require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)

        expires_in_days = body.expires_in_days
        if expires_in_days is not None and int(expires_in_days) <= 0:
            return JSONResponse(
                {"error": "expires_in_days must be greater than 0"}, status_code=400
            )

        label = str(body.label or body.name or "").strip()
        user_id = str(auth_user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)
        account_id = str(auth_user.get("account_id") or "").strip()
        if not account_id:
            return JSONResponse({"error": CURRENT_ACCOUNT_NOT_FOUND_MESSAGE}, status_code=403)

        try:
            enforce_token_scope_within_caller_authority(
                auth_user,
                requested_scopes=list(body.scopes or []),
                requested_project_ids=list(body.allowed_project_ids or []),
                requested_workflow_ids=list(body.allowed_workflow_ids or []),
            )
        except TokenAuthorityError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status_code)

        try:
            token = bl.create_pat(
                user_id=user_id,
                account_id=account_id,
                label=label,
                scopes=list(body.scopes or []),
                allowed_project_ids=list(body.allowed_project_ids or []),
                allowed_workflow_ids=list(body.allowed_workflow_ids or []),
                expires_in_days=int(expires_in_days) if expires_in_days is not None else None,
            )
        except Exception as e:
            return _api_error_response(e)

        _log_auth_audit_event(
            bl,
            action="token_create",
            username=str(auth_user.get("username") or ""),
            actor=str(auth_user.get("username") or "auth"),
            status="ok",
            detail={
                "token_id": str(token.get("id") or "").strip(),
                "label": str(token.get("label") or ""),
                "expires_at": token.get("expires_at"),
            },
        )
        return {
            "id": str(token.get("id") or "").strip(),
            "token_id": str(token.get("id") or "").strip(),
            "token": token,
        }

    @router.post("/api/auth/tokens/{token_id}/revoke")
    async def api_auth_tokens_revoke(token_id: str, request: Request):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_pat_tables_available(bl):
            return JSONResponse({"error": ERR_PAT_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            auth_user = _require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)

        user_id = str(auth_user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)

        try:
            revoked = bl.revoke_pat(str(token_id), user_id)
        except Exception as e:
            return _api_error_response(e)
        if not revoked:
            return JSONResponse({"error": ERR_TOKEN_NOT_FOUND}, status_code=404)

        _log_auth_audit_event(
            bl,
            action="token_revoke",
            username=str(auth_user.get("username") or ""),
            actor=str(auth_user.get("username") or "auth"),
            status="ok",
            detail={"token_id": str(revoked.get("id") or token_id)},
        )
        return {
            "status": "revoked",
            "id": str(revoked.get("id") or token_id),
            "token": revoked,
        }
