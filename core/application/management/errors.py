from __future__ import annotations

import hashlib
from typing import Any

ACCESS_DENIED_MESSAGE = "Access denied"


def safe_operator_exception(exc: BaseException, *, limit: int = 1000) -> str:
    """Return a bounded diagnostic identity without projecting exception content."""
    detail_digest = hashlib.sha256(str(exc or "").encode("utf-8", errors="replace")).hexdigest()
    diagnostic = f"{type(exc).__name__}[detail_sha256={detail_digest}]"
    return diagnostic[: max(1, int(limit))]


class ServiceError(Exception):
    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.payload = payload or {"error": message}
        if status_code is not None:
            self.status_code = status_code


class ServiceUnavailableError(ServiceError):
    status_code = 503


class ServiceNotFoundError(ServiceError):
    status_code = 404


class ServiceScopeNotFoundError(ServiceNotFoundError):
    """A scope denial intentionally concealed behind a non-enumerating 404."""


class ServiceValidationError(ServiceError):
    status_code = 400


class ServiceForbiddenError(ServiceError):
    status_code = 403


class ServiceConflictError(ServiceError):
    status_code = 409


class ServicePreconditionError(ServiceError):
    status_code = 412
