"""Routes that store a person's own timezone and interface locale.

Registered on the auth-users router by :func:`register_timezone_locale_routes`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.principal import ResolvedPrincipal
from core.entrypoints.api import auth_users_timezone_locale as _timezone_locale


def register_timezone_locale_routes(
    router: APIRouter,
    *,
    request_auth_user_payload: Callable[[Request | None], dict | None],
    get_logger_only: Callable[[], Any],
    err_authentication_required: str,
    err_invalid_json_body: str,
    err_database_not_configured: str,
) -> None:
    """Add the timezone, locale and detected-timezone routes to ``router``."""

    @router.patch("/api/users/me/timezone")
    async def api_user_update_timezone(request: Request):
        """Update the authenticated user's timezone preference."""
        user = request_auth_user_payload(request)
        if not isinstance(user, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        clean_tz = await _timezone_locale.store_timezone(
            request,
            user_id,
            get_logger_only=get_logger_only,
            err_invalid_json_body=err_invalid_json_body,
            err_database_not_configured=err_database_not_configured,
        )
        if isinstance(clean_tz, JSONResponse):
            return clean_tz

        auth_user = getattr(request.state, "auth_user", None)
        if isinstance(auth_user, dict):
            auth_user["timezone"] = clean_tz
        principal = getattr(request.state, "auth_principal", None)
        if isinstance(principal, ResolvedPrincipal):
            principal.timezone = clean_tz

        return {"status": "ok", "timezone": clean_tz}

    @router.patch("/api/users/me/locale")
    async def api_user_update_locale(request: Request):
        """Update the authenticated user's interface locale preference."""
        user = request_auth_user_payload(request)
        if not isinstance(user, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        clean_locale = await _timezone_locale.store_locale(
            request,
            user_id,
            get_logger_only=get_logger_only,
            err_invalid_json_body=err_invalid_json_body,
            err_database_not_configured=err_database_not_configured,
        )
        if isinstance(clean_locale, JSONResponse):
            return clean_locale

        auth_user = getattr(request.state, "auth_user", None)
        if isinstance(auth_user, dict):
            auth_user["locale"] = clean_locale
        principal = getattr(request.state, "auth_principal", None)
        if isinstance(principal, ResolvedPrincipal):
            principal.locale = clean_locale

        return {"status": "ok", "locale": clean_locale}

    @router.post("/api/users/me/detect-timezone")
    async def api_user_detect_timezone(request: Request):
        """Accept browser-detected timezone on first login / onboarding."""
        user = request_auth_user_payload(request)
        if not isinstance(user, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        existing_tz = str(user.get("timezone") or "").strip()
        if existing_tz:
            return {"status": "already_set", "timezone": existing_tz}

        clean_tz = await _timezone_locale.store_timezone(
            request,
            user_id,
            get_logger_only=get_logger_only,
            err_invalid_json_body=err_invalid_json_body,
            err_database_not_configured=err_database_not_configured,
            only_if_empty=True,
        )
        if isinstance(clean_tz, JSONResponse):
            return clean_tz

        return {"status": "ok", "timezone": clean_tz}
