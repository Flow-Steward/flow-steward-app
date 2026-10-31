"""Translate application service errors into the admin routes' HTTP errors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

from fastapi import HTTPException

from core.application.management.errors import ServiceError, ServiceUnavailableError


def raise_service_http_error(exc: ServiceError) -> NoReturn:
    """Raise the HTTP error for *exc*: its structured payload, or its message."""
    payload = getattr(exc, "payload", None)
    detail = payload if isinstance(payload, dict) and set(payload) != {"error"} else str(exc)
    raise HTTPException(
        status_code=int(getattr(exc, "status_code", 500) or 500), detail=detail
    ) from exc


def service_db_getter(get_db: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap a route-level DB getter so a missing backend reaches services as a service error."""

    def _get() -> Any:
        try:
            return get_db()
        except HTTPException as exc:
            raise ServiceUnavailableError(str(exc.detail)) from exc

    return _get


__all__ = ["raise_service_http_error", "service_db_getter"]
