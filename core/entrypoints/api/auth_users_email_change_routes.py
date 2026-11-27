"""Routes of a person's own email change: status, request and verification link.

Registered on the auth-users router by :func:`register_email_change_routes`;
the router factory in ``auth_users`` passes the callables and error texts the
routes share with the rest of the user routes.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from core.application.auth import user_profile_repository
from core.application.auth.config import AUTH_PASSWORD_RESET_TTL_MINUTES
from core.application.auth.email import normalize_email_address
from core.application.auth.password import password_reset_token_hash
from core.application.auth.rate_limit import ClearUserLoginFailures
from core.application.auth.serve_auth_runtime import public_base_url
from core.application.extensions.system_email import (
    send_email_address_in_use_email,
    send_email_change_verification_email,
)
from core.application.management.requests import AuthEmailChangeRequest
from core.application.settings.ui_i18n import resolve_ui_locale
from core.entrypoints.api import auth_users_email_change as _email_change_helpers

_ERR_EMAIL_NOT_SET_UP = (
    "Email is not set up, so the new address cannot be verified. "
    "An administrator can connect an email extension."
)
_ERR_EMAIL_UNREACHABLE = "The new email address cannot receive a verification email"

EMAIL_CHANGE_SCHEMA_NOT_APPLIED = "Email change schema is not applied. Run migrations and retry."
_auth_email_change_tables_available = _email_change_helpers.auth_email_change_tables_available


def register_email_change_routes(
    router: APIRouter,
    *,
    local_auth_enabled: Callable[[], bool],
    get_logger_only: Callable[[], Any],
    auth_tables_available: Callable[[Any], bool],
    require_authenticated_user: Callable[..., dict],
    api_error_response: Callable[[Exception], JSONResponse],
    log_auth_audit_event: Callable[..., None],
    auth_login_clear_user_failures: ClearUserLoginFailures,
    get_user_by_username: Callable[[Any, str], dict | None],
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
    err_authentication_required: str,
    err_database_not_configured: str,
    err_auth_tables_not_available: str,
    err_local_auth_disabled: str,
) -> None:
    """Add the email change routes to ``router``."""

    @router.get("/api/users/me/email-change-status")
    def api_user_email_change_status(request: Request):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        if not _auth_email_change_tables_available(bl):
            return JSONResponse({"error": EMAIL_CHANGE_SCHEMA_NOT_APPLIED}, status_code=503)
        try:
            auth_user = require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        user_id = str(auth_user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        try:
            row = user_profile_repository.get_pending_email_change(
                bl,
                user_id=user_id,
                real_dict_cursor_kwargs=real_dict_cursor_kwargs,
            )
        except Exception as exc:
            return api_error_response(exc)

        if not row:
            return {"pending": False}
        if isinstance(row, dict):
            return {
                "pending": True,
                "new_email": str(row.get("new_email") or "").strip(),
                "expires_at": row.get("expires_at"),
                "created_at": row.get("created_at"),
            }
        values = list(row or [])
        return {
            "pending": True,
            "new_email": str((values[0] if len(values) > 0 else "") or "").strip(),
            "expires_at": values[1] if len(values) > 1 else None,
            "created_at": values[2] if len(values) > 2 else None,
        }

    @router.post("/api/users/me/email-change/request")
    async def api_user_email_change_request(request: Request, body: AuthEmailChangeRequest):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        if not _auth_email_change_tables_available(bl):
            return JSONResponse({"error": EMAIL_CHANGE_SCHEMA_NOT_APPLIED}, status_code=503)
        try:
            auth_user = require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        auth_source = (
            str(
                getattr(request.state, "auth_source", "") or auth_user.get("auth_source") or "local"
            )
            .strip()
            .lower()
        )
        if auth_source != "local":
            return JSONResponse(
                {"error": "Email changes are only available for local-auth accounts"},
                status_code=400,
            )

        user_id = str(auth_user.get("id") or "").strip()
        current_email = str(auth_user.get("email") or auth_user.get("username") or "").strip()
        if not user_id or not current_email:
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        try:
            new_email = normalize_email_address(body.email, field_name="email")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if new_email.lower() == current_email.lower():
            return JSONResponse(
                {"error": "New email must be different from your current email"},
                status_code=400,
            )
        existing_user = get_user_by_username(bl, new_email)
        # ADR 0085, C2: the answer never says whether the address is taken. A request
        # for a taken address is stored and answered like any other; the address
        # gets a notice that it is in use instead of a verification link, so the
        # change can never be verified.
        address_in_use = bool(
            existing_user and str(existing_user.get("id") or "").strip() != user_id
        )

        verification_token = secrets.token_urlsafe(48)
        verification_token_hash = password_reset_token_hash(verification_token)
        client_ip = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
        try:
            inserted_row = user_profile_repository.create_email_change_request(
                bl,
                user_id=user_id,
                current_email=current_email,
                new_email=new_email,
                token_hash=verification_token_hash,
                expires_in_minutes=int(AUTH_PASSWORD_RESET_TTL_MINUTES),
                requested_ip=client_ip,
                real_dict_cursor_kwargs=real_dict_cursor_kwargs,
            )
        except Exception as exc:
            return api_error_response(exc)

        try:
            if address_in_use:
                delivery = dict(
                    send_email_address_in_use_email(
                        recipient=new_email,
                        locale=resolve_ui_locale(request, auth_user=auth_user),
                    )
                    or {}
                )
            else:
                delivery = _email_change_helpers.dispatch_email_change_verification_email(
                    request,
                    user=auth_user,
                    new_email=new_email,
                    verification_token=verification_token,
                    expires_in_minutes=int(AUTH_PASSWORD_RESET_TTL_MINUTES),
                    public_base_url_fn=public_base_url,
                    send_email_change_verification_email_fn=send_email_change_verification_email,
                )
        except Exception:
            delivery = {"ok": False, "reason": "dispatch_exception"}
        if not bool(delivery.get("ok")):
            with contextlib.suppress(Exception):
                user_profile_repository.mark_email_change_delivery_failed(
                    bl,
                    token_hash=verification_token_hash,
                    real_dict_cursor_kwargs=real_dict_cursor_kwargs,
                )
            reason = str(delivery.get("reason") or "").strip().lower() or "send_failed"
            log_auth_audit_event(
                bl,
                action="email_change_request",
                username=current_email,
                actor=current_email,
                status="rejected",
                detail={"reason": reason, "new_email": new_email},
            )
            if reason == "disabled_or_unconfigured":
                return JSONResponse({"error": _ERR_EMAIL_NOT_SET_UP}, status_code=503)
            if reason == "invalid_recipient":
                return JSONResponse({"error": _ERR_EMAIL_UNREACHABLE}, status_code=400)
            return JSONResponse({"error": "Failed to send verification email"}, status_code=502)

        log_auth_audit_event(
            bl,
            action="email_change_request",
            username=current_email,
            actor=current_email,
            status="accepted",
            detail={"new_email": new_email, "address_in_use": address_in_use},
        )
        expires_at = None
        if isinstance(inserted_row, dict):
            expires_at = inserted_row.get("expires_at")
        elif isinstance(inserted_row, (tuple, list)) and len(inserted_row) > 1:
            expires_at = inserted_row[1]
        return {
            "status": "verification_sent",
            "email": new_email,
            "expires_at": expires_at,
            "expires_in_minutes": int(AUTH_PASSWORD_RESET_TTL_MINUTES),
        }

    @router.get("/verify-email-change")
    async def verify_email_change(request: Request, token: Annotated[str, Query()] = ""):
        def _redirect(result: str, *, status_code: int = 303) -> RedirectResponse:
            return RedirectResponse(
                f"/settings/account?email_change={quote(result)}",
                status_code=status_code,
            )

        if not local_auth_enabled():
            return _redirect("unavailable", status_code=303)
        bl = get_logger_only()
        if not bl:
            return _redirect("database_unavailable", status_code=303)
        if not auth_tables_available(bl):
            return _redirect("auth_schema_unavailable", status_code=303)
        if not _auth_email_change_tables_available(bl):
            return _redirect("email_change_schema_unavailable", status_code=303)

        verification_token = str(token or "").strip()
        if not verification_token:
            return _redirect("missing_token", status_code=303)

        verification_token_hash = password_reset_token_hash(verification_token)
        try:
            row = user_profile_repository.get_email_change_verification_request(
                bl,
                token_hash=verification_token_hash,
                real_dict_cursor_kwargs=real_dict_cursor_kwargs,
            )
        except Exception:
            return _redirect("verify_error", status_code=303)

        if not row:
            return _redirect("invalid", status_code=303)

        user_id = str(row.get("user_id") or "").strip()
        current_email = str(row.get("current_email") or "").strip()
        new_email = str(row.get("new_email") or "").strip()
        stored_username = str(row.get("username") or "").strip()
        if not user_id or not new_email:
            return _redirect("invalid", status_code=303)
        if not bool(row.get("is_active", True)):
            return _redirect("account_disabled", status_code=303)
        if current_email and stored_username and current_email.lower() != stored_username.lower():
            return _redirect("stale", status_code=303)

        existing_user = get_user_by_username(bl, new_email)
        if existing_user and str(existing_user.get("id") or "").strip() != user_id:
            with contextlib.suppress(Exception):
                user_profile_repository.mark_email_change_taken(
                    bl,
                    request_id=str(row.get("id") or ""),
                    real_dict_cursor_kwargs=real_dict_cursor_kwargs,
                )
            return _redirect("email_taken", status_code=303)

        try:
            user_profile_repository.apply_email_change(
                bl,
                user_id=user_id,
                new_email=new_email,
                request_id=str(row.get("id") or ""),
                real_dict_cursor_kwargs=real_dict_cursor_kwargs,
            )
        except Exception:
            return _redirect("apply_error", status_code=303)

        auth_login_clear_user_failures(current_email)
        auth_login_clear_user_failures(new_email)
        auth_user_state = getattr(request.state, "auth_user", None)
        if (
            isinstance(auth_user_state, dict)
            and str(auth_user_state.get("id") or "").strip() == user_id
        ):
            auth_user_state["username"] = new_email
            auth_user_state["email"] = new_email
        log_auth_audit_event(
            bl,
            action="email_change_verify",
            username=new_email,
            actor=new_email,
            status="ok",
            detail={"previous_email": current_email},
        )
        return _redirect("verified", status_code=303)
