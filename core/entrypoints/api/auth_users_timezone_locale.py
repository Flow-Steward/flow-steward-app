"""Read, validate and store the signed-in user's timezone and interface locale."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from core.application.auth import user_profile_repository
from core.application.settings.ui_i18n import normalize_ui_locale


async def store_timezone(
    request: Request,
    user_id: str,
    *,
    get_logger_only: Callable[[], Any],
    err_invalid_json_body: str,
    err_database_not_configured: str,
    only_if_empty: bool = False,
) -> str | JSONResponse:
    """The stored timezone from the request body, or the error response."""
    from core.application.settings.timezone_service import validate_timezone as _validate_tz

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": err_invalid_json_body}, status_code=400)
    raw_tz = body.get("timezone") if isinstance(body, dict) else None

    try:
        clean_tz = _validate_tz(raw_tz)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    bl = get_logger_only()
    if not bl:
        return JSONResponse({"error": err_database_not_configured}, status_code=503)
    try:
        user_profile_repository.update_user_timezone(
            bl,
            user_id=user_id,
            timezone=clean_tz,
            only_if_empty=only_if_empty,
        )
    except Exception as exc:
        message = str(exc or "").lower()
        if "timezone" in message and "users" in message and "column" in message:
            return JSONResponse(
                {"error": "Timezone schema is not applied. Run migrations and retry."},
                status_code=503,
            )
        return JSONResponse({"error": "Failed to update timezone"}, status_code=500)
    return clean_tz


async def store_locale(
    request: Request,
    user_id: str,
    *,
    get_logger_only: Callable[[], Any],
    err_invalid_json_body: str,
    err_database_not_configured: str,
) -> str | JSONResponse:
    """The stored interface locale from the request body, or the error response."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": err_invalid_json_body}, status_code=400)
    raw_locale = body.get("locale") if isinstance(body, dict) else None
    clean_locale = normalize_ui_locale(raw_locale)
    if not clean_locale:
        return JSONResponse({"error": "Unsupported interface locale"}, status_code=422)

    bl = get_logger_only()
    if not bl:
        return JSONResponse({"error": err_database_not_configured}, status_code=503)
    try:
        user_profile_repository.update_user_locale(
            bl,
            user_id=user_id,
            locale=clean_locale,
        )
    except Exception as exc:
        message = str(exc or "").lower()
        if "locale" in message and "users" in message and "column" in message:
            return JSONResponse(
                {"error": "Locale schema is not applied. Run migrations and retry."},
                status_code=503,
            )
        return JSONResponse({"error": "Failed to update interface locale"}, status_code=500)
    return clean_locale
