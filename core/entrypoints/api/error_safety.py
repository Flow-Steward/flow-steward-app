"""Safe API error responses for routes without the injected runtime factory."""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi.responses import JSONResponse


def safe_error_response(
    logger: logging.Logger,
    exc: Exception,
    *,
    message: str,
    status_code: int = 500,
) -> JSONResponse:
    correlation_id = uuid4().hex
    logger.error(
        "API error correlation_id=%s status=%d",
        correlation_id,
        status_code,
        exc_info=exc,
    )
    return JSONResponse(
        {"error": str(message or "Request failed"), "correlation_id": correlation_id},
        status_code=status_code,
    )
