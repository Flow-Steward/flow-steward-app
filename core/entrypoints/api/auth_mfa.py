"""Cookie-authenticated endpoints for an account's authenticator settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.auth.local_credentials import authenticator_storage_available
from core.application.auth.mfa_service import (
    AuthenticatorOperation,
    authenticator_settings_status,
    update_authenticator_settings,
)

_LOCAL_AUTHENTICATOR = "/api/auth/multi-factor"
_NO_STORE = {"Cache-Control": "no-store"}
_AUTH_REQUIRED = "Authentication required"


class _EnrollAuthenticator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=4096)
    current_code: str = Field(default="", max_length=128)


class _VerifyAuthenticator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128)


class _DisableAuthenticator(_EnrollAuthenticator):
    current_code: str = Field(min_length=1, max_length=128)


class AuthenticatorStateResponse(BaseModel):
    enabled: bool


class AuthenticatorStatusResponse(AuthenticatorStateResponse):
    required: bool


class AuthenticatorEnrollmentResponse(BaseModel):
    secret: str
    uri: str


class AuthenticatorRecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


@dataclass(frozen=True)
class _Session:
    user_id: str
    cookie_hash: str


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=_NO_STORE)


def create_auth_mfa_router(
    *,
    local_auth_enabled: Callable[[], bool],
    get_logger_only: Callable[[], Any],
    require_authenticated_user: Callable[[Request], dict],
    get_user_by_id: Callable[[Any, str], dict | None],
    verify_password: Callable[[str, str], bool],
    session_token_hash: Callable[[str], str],
    auth_cookie_name: str,
    csrf_request_valid: Callable[[Request], bool],
    request_client_ip: Callable[[Request], str],
    auth_login_lock_state: Callable[[str, str], tuple[bool, int]],
    auth_login_record_failure: Callable[[str, str], tuple[bool, int]],
    api_error_response: Callable[[Exception], JSONResponse],
    log_auth_audit_event: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix=_LOCAL_AUTHENTICATOR, tags=["auth"])

    def _session(request: Request) -> _Session | None:
        browser_token = str(request.cookies.get(auth_cookie_name) or "").strip()
        if (
            not local_auth_enabled()
            or getattr(request.state, "auth_source", None) != "local"
            or "authorization" in request.headers
            or not browser_token
        ):
            return None
        try:
            user = require_authenticated_user(request)
        except PermissionError:
            return None
        user_id = str(user.get("id") or "").strip()
        if not user_id or user.get("principal_type") not in {"user", "viewer", "operator", "admin"}:
            return None
        return _Session(user_id, session_token_hash(browser_token))

    def _server_error(exc: Exception) -> JSONResponse:
        response = api_error_response(exc)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/status", response_model=AuthenticatorStatusResponse)
    def api_authenticator_status(request: Request) -> JSONResponse:
        session = _session(request)
        if session is None:
            return _error(_AUTH_REQUIRED, 401)
        try:
            bl = get_logger_only()
            if not bl or not authenticator_storage_available(bl):
                return _error("Authenticator storage is unavailable", 503)
            status = authenticator_settings_status(
                bl, user_id=session.user_id, cookie_hash=session.cookie_hash
            )
        except Exception as exc:
            return _server_error(exc)
        if status is None:
            return _error(_AUTH_REQUIRED, 401)
        return JSONResponse(status, headers=_NO_STORE)

    def _update(
        request: Request,
        operation: AuthenticatorOperation,
        code: str,
        current_password: str = "",
    ) -> JSONResponse:
        session = _session(request)
        if session is None:
            return _error(_AUTH_REQUIRED, 401)
        if not csrf_request_valid(request):
            return _error("CSRF validation failed", 403)

        try:
            bl = get_logger_only()
            if not bl or not authenticator_storage_available(bl):
                return _error("Authenticator storage is unavailable", 503)
            user = get_user_by_id(bl, session.user_id)
            if not user or not user.get("is_active"):
                return _error(_AUTH_REQUIRED, 401)
            username = str(user["username"])
            password_hash = str(user.get("password_hash") or "")
            attempt_subject = f"local-authenticator:{session.user_id}"
            attempt_address = f"local-authenticator:{request_client_ip(request)}"
            change = update_authenticator_settings(
                bl,
                operation=operation,
                user_id=session.user_id,
                username=username,
                password_hash=password_hash,
                cookie_hash=session.cookie_hash,
                code=code,
                verify_current_password=lambda: verify_password(current_password, password_hash),
                check_attempts=lambda: auth_login_lock_state(attempt_subject, attempt_address),
                record_failure=lambda: auth_login_record_failure(attempt_subject, attempt_address),
            )
        except Exception as exc:
            return _server_error(exc)

        log_auth_audit_event(
            bl,
            action=f"authenticator_{operation}",
            actor=username,
            username=username,
            status="ok" if change.outcome == "ok" else "rejected",
            detail={"reason": change.outcome},
        )
        if change.outcome == "ok":
            return JSONResponse(change.payload, headers=_NO_STORE)
        if change.outcome == "inactive_session":
            return _error(_AUTH_REQUIRED, 401)
        if change.outcome == "required":
            return _error("Multi-factor authentication is required for this installation.", 403)
        if change.outcome == "throttled":
            retry_after = max(1, change.retry_after_seconds)
            return JSONResponse(
                {"error": "Too many authenticator attempts.", "retry_after_seconds": retry_after},
                status_code=429,
                headers={**_NO_STORE, "Retry-After": str(retry_after)},
            )
        message = (
            "The setup code is invalid or has expired."
            if operation == "verify"
            else "The password or authenticator code is invalid."
        )
        return _error(message, 403)

    @router.post("/enroll", response_model=AuthenticatorEnrollmentResponse)
    def api_authenticator_enroll(request: Request, body: _EnrollAuthenticator) -> JSONResponse:
        return _update(request, "enroll", body.current_code, body.current_password)

    @router.post("/verify", response_model=AuthenticatorRecoveryCodesResponse)
    def api_authenticator_verify(request: Request, body: _VerifyAuthenticator) -> JSONResponse:
        return _update(request, "verify", body.code)

    @router.post("/disable", response_model=AuthenticatorStateResponse)
    def api_authenticator_disable(request: Request, body: _DisableAuthenticator) -> JSONResponse:
        return _update(request, "disable", body.current_code, body.current_password)

    return router
