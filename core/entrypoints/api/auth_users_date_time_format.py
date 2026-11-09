"""The user's date pattern and clock: read for the session, saved from the profile."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from core.application.auth import user_profile_repository
from core.application.settings.date_time_format import (
    normalize_date_format,
    normalize_time_format,
    stored_date_format,
    stored_time_format,
)


def session_date_time_format(
    user: dict[str, Any], get_logger_only: Callable[[], Any]
) -> tuple[str, str]:
    """The user's date pattern and clock as the interface reads them (defaults when unset)."""
    stored_date, stored_time = "", ""
    user_id = str(user.get("id") or "").strip()
    bl = get_logger_only() if user_id else None
    if bl:
        with contextlib.suppress(Exception):
            stored_date, stored_time = user_profile_repository.load_user_date_time_format(
                bl, user_id
            )
    return stored_date_format(stored_date), stored_time_format(stored_time)


async def save_date_time_format(
    request: Request,
    user_id: str,
    *,
    get_logger_only: Callable[[], Any],
    err_invalid_json_body: str,
    err_database_not_configured: str,
) -> dict[str, str] | JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": err_invalid_json_body}, status_code=400)
    payload = body if isinstance(body, dict) else {}
    try:
        # A key left out keeps its stored value; an empty one goes back to the default.
        clean_date = (
            normalize_date_format(payload["date_format"]) if "date_format" in payload else None
        )
        clean_time = (
            normalize_time_format(payload["time_format"]) if "time_format" in payload else None
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    bl = get_logger_only()
    if not bl:
        return JSONResponse({"error": err_database_not_configured}, status_code=503)
    try:
        user_profile_repository.update_user_date_time_format(
            bl, user_id=user_id, date_format=clean_date, time_format=clean_time
        )
        stored_date, stored_time = user_profile_repository.load_user_date_time_format(bl, user_id)
    except Exception:
        return JSONResponse({"error": "Failed to update date and time format"}, status_code=500)
    return {
        "status": "ok",
        "date_format": stored_date_format(stored_date),
        "time_format": stored_time_format(stored_time),
    }
