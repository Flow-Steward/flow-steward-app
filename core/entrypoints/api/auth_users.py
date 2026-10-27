from __future__ import annotations

import contextlib
import secrets
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from core.application.auth import user_profile_repository
from core.application.auth.account_memberships import normalize_membership_account_ids
from core.application.auth.config import AUTH_PASSWORD_RESET_TTL_MINUTES, DEFAULT_ACCOUNT_ID
from core.application.auth.email import normalize_email_address
from core.application.auth.password import password_reset_token_hash
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.rate_limit import ClearUserLoginFailures
from core.application.auth.serve_auth_runtime import public_base_url
from core.application.auth.user_admin.command_service import UserAdminCommandService
from core.application.auth.user_admin.contracts import (
    UserAuditContext,
    UserCreateInput,
    UserDeleteInput,
    UserSelectorInput,
    UserUpdateInput,
)
from core.application.auth.user_admin.query_service import UserAdminQueryService
from core.application.extensions.system_email import (
    send_email_change_verification_email,
    send_signup_notification_email,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.requests import (
    AuthEmailChangeRequest,
    AuthUserCreateRequest,
    AuthUserProfileUpdateRequest,
    AuthUserUpdateRequest,
)
from core.application.settings.ui_i18n import (
    normalize_ui_locale,
    resolve_ui_locale,
)
from core.entrypoints.api import auth_users_email_change as _email_change_helpers

_ERR_EMAIL_NOT_SET_UP = (
    "Email is not set up, so the new address cannot be verified. "
    "An administrator can connect an email extension."
)
_ERR_EMAIL_UNREACHABLE = "The new email address cannot receive a verification email"

EMAIL_CHANGE_SCHEMA_NOT_APPLIED = "Email change schema is not applied. Run migrations and retry."
_auth_email_change_tables_available = _email_change_helpers.auth_email_change_tables_available


def create_auth_users_router(
    *,
    local_auth_enabled: Callable[[], bool],
    external_auth_enabled: Callable[[], bool],
    get_logger_only: Callable[[], Any],
    auth_tables_available: Callable[[Any], bool],
    require_authenticated_user: Callable[..., dict],
    request_auth_user_payload: Callable[[Request | None], dict | None],
    api_error_response: Callable[[Exception], JSONResponse],
    log_auth_audit_event: Callable[..., None],
    auth_login_clear_user_failures: ClearUserLoginFailures,
    get_user_by_id: Callable[[Any, Any], dict | None],
    get_user_by_username: Callable[[Any, str], dict | None],
    normalize_requested_roles: Callable[[list[str] | None], list[str]],
    seed_default_roles: Callable[[Any], None],
    password_hash: Callable[[str], str],
    auth_user_column_exists: Callable[[Any, str], bool],
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
    auth_admin_roles: list[str] | tuple[str, ...] | set[str],
    err_authentication_required: str,
    err_database_not_configured: str,
    err_auth_tables_not_available: str,
    err_admin_role_required: str,
    err_local_auth_disabled: str,
    err_invalid_json_body: str,
    auth_csrf_cookie_name: str,
    user_query_service: UserAdminQueryService | None = None,
    user_command_service: UserAdminCommandService | None = None,
) -> APIRouter:
    del auth_user_column_exists
    router = APIRouter(tags=["auth-users"])
    user_query_service = user_query_service or UserAdminQueryService(
        get_logger=get_logger_only,
        auth_tables_available_fn=auth_tables_available,
        get_user_by_id_fn=get_user_by_id,
        get_user_by_username_fn=get_user_by_username,
    )
    user_command_service = user_command_service or UserAdminCommandService(
        get_logger=get_logger_only,
        signup_notifier=lambda **kwargs: send_signup_notification_email(**kwargs),
        auth_tables_available_fn=auth_tables_available,
        get_user_by_id_fn=get_user_by_id,
        get_user_by_username_fn=get_user_by_username,
        normalize_requested_roles_fn=normalize_requested_roles,
        seed_default_roles_fn=seed_default_roles,
        password_hash_fn=password_hash,
        log_auth_audit_event_fn=log_auth_audit_event,
        auth_login_clear_user_failures_fn=auth_login_clear_user_failures,
    )

    def _user_service_error(exc: Exception) -> JSONResponse:
        if isinstance(exc, ServiceValidationError):
            return JSONResponse({"error": str(exc)}, status_code=400)
        if isinstance(exc, ServiceNotFoundError):
            return JSONResponse({"error": str(exc)}, status_code=404)
        if isinstance(exc, ServiceForbiddenError):
            return JSONResponse({"error": str(exc)}, status_code=403)
        if isinstance(exc, ServiceConflictError):
            return JSONResponse({"error": str(exc)}, status_code=409)
        if isinstance(exc, ServiceUnavailableError):
            return JSONResponse({"error": str(exc)}, status_code=503)
        return api_error_response(exc)

    def _user_input_error(exc: ValidationError) -> JSONResponse:
        messages = [
            str(error.get("msg") or "Invalid input") for error in exc.errors(include_input=False)
        ]
        return JSONResponse(
            {"error": "; ".join(messages) or "Invalid input"},
            status_code=400,
        )

    def _admin_managed_account_ids(admin_user: dict[str, Any] | None) -> list[str]:
        if not isinstance(admin_user, dict):
            return []
        seen: set[str] = set()
        account_ids: list[str] = []

        def add(raw: Any) -> None:
            account_id = str(raw or "").strip()
            if account_id and account_id not in seen:
                seen.add(account_id)
                account_ids.append(account_id)

        add(admin_user.get("account_id"))
        for raw in admin_user.get("account_ids") or []:
            add(raw)
        for membership in admin_user.get("account_memberships") or []:
            if isinstance(membership, dict):
                add(membership.get("account_id"))
        return account_ids

    def _accounts_allowed(account_ids: list[str], managed_account_ids: list[str]) -> bool:
        managed = {str(account_id or "").strip() for account_id in managed_account_ids}
        requested = [str(account_id or "").strip() for account_id in account_ids]
        requested = [account_id for account_id in requested if account_id]
        return bool(requested) and all(account_id in managed for account_id in requested)

    def _account_access_denied() -> JSONResponse:
        return JSONResponse({"error": "account access denied"}, status_code=403)

    @router.get("/api/auth/introspect")
    async def api_auth_introspect(request: Request):
        """Return the bounded effective principal required by transport adapters."""
        principal = getattr(request.state, "auth_user", None)
        if not isinstance(principal, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        expires_at = principal.get("expires_at")
        expires_at_isoformat = getattr(expires_at, "isoformat", None)
        if callable(expires_at_isoformat):
            expires_at = expires_at_isoformat()
        elif expires_at is not None and not isinstance(expires_at, (int, float, str)):
            expires_at = str(expires_at)

        auth_source = str(
            getattr(request.state, "auth_source", "") or principal.get("auth_source") or ""
        ).strip()
        principal_type = str(principal.get("principal_type") or "user").strip()
        scopes = [str(item) for item in principal.get("scopes") or [] if str(item).strip()]
        if (
            not scopes
            and principal_type != "service_account"
            and auth_source
            not in {
                "machine",
                "pat",
            }
        ):
            roles = {str(item).strip().lower() for item in principal.get("roles") or []}
            if "admin" in roles:
                scopes = ["admin:all"]
            elif "operator" in roles:
                scopes = ["read:*", "command:*"]
            else:
                scopes = ["read:*"]

        return {
            "active": bool(principal.get("is_active", True)),
            "principal": {
                "id": str(principal.get("id") or "").strip(),
                "username": str(principal.get("username") or "").strip(),
                "principal_type": principal_type,
                "service_account_key": str(principal.get("service_account_key") or "").strip(),
                "auth_source": auth_source,
                "account_id": str(principal.get("account_id") or "").strip(),
                "roles": list(principal.get("roles") or []),
                "scopes": scopes,
                "allowed_project_ids": list(principal.get("allowed_project_ids") or []),
                "allowed_workflow_ids": list(principal.get("allowed_workflow_ids") or []),
                "expires_at": expires_at,
            },
        }

    @router.get("/api/auth/me")
    async def api_auth_me(request: Request):
        """Return current authenticated user."""
        if not (local_auth_enabled() or external_auth_enabled()):
            return {
                "authenticated": False,
                "local_auth_enabled": False,
                "external_auth_enabled": False,
            }
        user = getattr(request.state, "auth_user", None)
        if not isinstance(user, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        auth_source = (
            str(getattr(request.state, "auth_source", "") or user.get("auth_source") or "local")
            .strip()
            .lower()
        )
        must_change_password = (
            bool(user.get("must_change_password", False)) if auth_source == "local" else False
        )
        user_timezone = str(user.get("timezone") or "").strip()
        if not user_timezone:
            user_id = str(user.get("id") or "").strip()
            bl = get_logger_only() if user_id else None
            if bl:
                try:
                    user_timezone = user_profile_repository.load_user_timezone(bl, user_id)
                    if user_timezone:
                        user["timezone"] = user_timezone
                except Exception:
                    user_timezone = str(user.get("timezone") or "").strip()
        user_locale = str(user.get("locale") or "").strip()
        if not user_locale:
            user_id = str(user.get("id") or "").strip()
            bl = get_logger_only() if user_id else None
            if bl:
                try:
                    user_locale = user_profile_repository.load_user_locale(bl, user_id)
                    if user_locale:
                        user["locale"] = user_locale
                except Exception:
                    user_locale = str(user.get("locale") or "").strip()
        user_locale = normalize_ui_locale(user_locale)
        return {
            "authenticated": True,
            "local_auth_enabled": local_auth_enabled(),
            "external_auth_enabled": external_auth_enabled(),
            "auth_source": auth_source,
            "csrf_token": str(request.cookies.get(auth_csrf_cookie_name) or "")
            if auth_source == "local"
            else "",
            "user": {
                "id": str(user.get("id") or "").strip(),
                "email": str(user.get("email") or user.get("username") or ""),
                "username": str(user.get("username") or ""),
                "display_name": str(user.get("display_name") or ""),
                "roles": list(user.get("roles") or []),
                "account_id": str(user.get("account_id") or ""),
                "account_ids": list(user.get("account_ids") or []),
                "account_memberships": list(user.get("account_memberships") or []),
                "must_change_password": must_change_password,
                "timezone": user_timezone,
                "locale": user_locale,
            },
        }

    @router.patch("/api/users/me/profile")
    async def api_user_update_profile(request: Request, body: AuthUserProfileUpdateRequest):
        """Update the authenticated user's basic profile fields."""
        if not (local_auth_enabled() or external_auth_enabled()):
            return JSONResponse({"error": "Authentication is disabled"}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            auth_user = require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        user_id = str(auth_user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        display_name = (
            str(body.display_name or "").strip()
            or str(auth_user.get("username") or "").strip()
            or "User"
        )
        try:
            user_profile_repository.update_user_display_name(bl, user_id, display_name)
        except Exception as e:
            return api_error_response(e)

        auth_user["display_name"] = display_name
        request_auth_user = getattr(request.state, "auth_user", None)
        if isinstance(request_auth_user, dict):
            request_auth_user["display_name"] = display_name

        log_auth_audit_event(
            bl,
            action="profile_update",
            username=str(auth_user.get("username") or ""),
            actor=str(auth_user.get("username") or "auth"),
            status="ok",
            detail={"display_name": display_name},
        )
        return {"status": "ok", "display_name": display_name}

    @router.get("/api/users/me/email-change-status")
    async def api_user_email_change_status(request: Request):
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
        if existing_user and str(existing_user.get("id") or "").strip() != user_id:
            return JSONResponse({"error": "Email already exists"}, status_code=409)

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
            detail={"new_email": new_email},
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

    @router.patch("/api/users/me/timezone")
    async def api_user_update_timezone(request: Request):
        """Update the authenticated user's timezone preference."""
        from core.application.settings.timezone_service import validate_timezone as _validate_tz

        user = request_auth_user_payload(request)
        if not isinstance(user, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

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
            )
        except Exception as exc:
            message = str(exc or "").lower()
            if "timezone" in message and "users" in message and "column" in message:
                return JSONResponse(
                    {"error": "Timezone schema is not applied. Run migrations and retry."},
                    status_code=503,
                )
            return JSONResponse({"error": "Failed to update timezone"}, status_code=500)

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
        from core.application.settings.timezone_service import validate_timezone as _validate_tz

        user = request_auth_user_payload(request)
        if not isinstance(user, dict):
            return JSONResponse({"error": err_authentication_required}, status_code=401)
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            return JSONResponse({"error": err_authentication_required}, status_code=401)

        existing_tz = str(user.get("timezone") or "").strip()
        if existing_tz:
            return {"status": "already_set", "timezone": existing_tz}

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
                only_if_empty=True,
            )
        except Exception as exc:
            message = str(exc or "").lower()
            if "timezone" in message and "users" in message and "column" in message:
                return JSONResponse(
                    {"error": "Timezone schema is not applied. Run migrations and retry."},
                    status_code=503,
                )
            return JSONResponse({"error": "Failed to update timezone"}, status_code=500)

        return {"status": "ok", "timezone": clean_tz}

    @router.get("/api/auth/users")
    async def api_auth_users(
        request: Request,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 25,
        search: Annotated[str, Query()] = "",
        role: Annotated[str, Query()] = "",
        active: Annotated[str, Query()] = "",
    ):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
        managed_account_ids = _admin_managed_account_ids(admin_user)
        if not managed_account_ids:
            return _account_access_denied()

        normalized_search = str(search or "").strip()
        normalized_role = str(role or "").strip().lower()
        normalized_active = str(active or "").strip().lower()
        active_filter = None
        if normalized_active in {"1", "true", "active"}:
            active_filter = True
        elif normalized_active in {"0", "false", "inactive"}:
            active_filter = False

        try:
            normalized_page = max(1, int(page or 1))
            normalized_per_page = max(1, min(int(per_page or 25), 200))
            result = user_query_service.list_users(
                search=normalized_search,
                include_inactive=True,
                include_deleted=False,
                limit=normalized_per_page,
                offset=(normalized_page - 1) * normalized_per_page,
                role=normalized_role,
                active_filter=active_filter,
                managed_account_ids=managed_account_ids,
            )
        except Exception as e:
            return _user_service_error(e)

        users = [user.model_dump(mode="json") for user in result.users]
        return {
            "users": users,
            "total": int(result.total),
            "page": normalized_page,
            "per_page": normalized_per_page,
            "total_pages": max(1, -(-int(result.total) // normalized_per_page)),
        }

    @router.post("/api/auth/users")
    async def api_auth_users_create(request: Request, body: AuthUserCreateRequest):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        primary_account_id = (
            str(body.primary_account_id or "").strip()
            or str(admin_user.get("account_id") or "").strip()
            or DEFAULT_ACCOUNT_ID
        )
        managed_account_ids = _admin_managed_account_ids(admin_user)
        requested_account_ids = normalize_membership_account_ids(
            body.account_ids,
            fallback_account_id=primary_account_id,
        )
        if not _accounts_allowed(requested_account_ids, managed_account_ids):
            return _account_access_denied()
        try:
            command = UserCreateInput(
                email=body.email,
                password=body.password,
                display_name=body.display_name,
                roles=body.roles or ["viewer"],
                is_active=bool(body.is_active),
                must_change_password=bool(body.must_change_password),
                account_id=primary_account_id,
                account_ids=requested_account_ids,
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.create_user(
                command,
                allowed_account_ids=managed_account_ids,
                notification_locale=resolve_ui_locale(request),
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except Exception as e:
            return _user_service_error(e)
        return {
            "user": result.user.model_dump(mode="json"),
            "warnings": list(result.warnings),
        }

    @router.put("/api/auth/users/{user_id}")
    async def api_auth_users_update(user_id: UUID, request: Request, body: AuthUserUpdateRequest):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        managed_account_ids = _admin_managed_account_ids(admin_user)

        try:
            command = UserUpdateInput(
                user_id=str(user_id),
                display_name=body.display_name,
                is_active=body.is_active,
                password=body.password,
                must_change_password=body.must_change_password,
                roles=body.roles,
                account_ids=body.account_ids,
                account_id=body.primary_account_id,
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.update_user(
                command,
                allowed_account_ids=managed_account_ids,
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except Exception as e:
            return _user_service_error(e)
        return {"user": result.user.model_dump(mode="json")}

    @router.post("/api/auth/users/{user_id}/unlock")
    async def api_auth_users_unlock(user_id: UUID, request: Request):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        managed_account_ids = _admin_managed_account_ids(admin_user)

        try:
            command = UserSelectorInput(
                user_id=str(user_id),
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.unlock_user(
                command,
                allowed_account_ids=managed_account_ids,
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except Exception as e:
            return _user_service_error(e)
        return {
            "status": result.status,
            "user_id": str(result.user.id or user_id),
            "username": str(result.user.username or ""),
        }

    @router.delete("/api/auth/users/{user_id}")
    async def api_auth_users_delete(user_id: UUID, request: Request):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        try:
            command = UserDeleteInput(
                user_id=str(user_id),
                actor_user_id=str(admin_user.get("id") or ""),
                actor=str(admin_user.get("username") or "admin"),
            )
            result = user_command_service.delete_user(
                command,
                allowed_account_ids=_admin_managed_account_ids(admin_user),
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except (ValidationError, ValueError) as exc:
            return (
                _user_input_error(exc)
                if isinstance(exc, ValidationError)
                else JSONResponse({"error": str(exc)}, status_code=400)
            )
        except Exception as exc:
            return _user_service_error(exc)
        return result.model_dump(mode="json")

    @router.post("/api/auth/users/{user_id}/force-password-reset")
    async def api_auth_users_force_password_reset(user_id: UUID, request: Request):
        if not local_auth_enabled():
            return JSONResponse({"error": err_local_auth_disabled}, status_code=400)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        if not auth_tables_available(bl):
            return JSONResponse({"error": err_auth_tables_not_available}, status_code=503)
        try:
            admin_user = require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)

        managed_account_ids = _admin_managed_account_ids(admin_user)
        try:
            command = UserSelectorInput(
                user_id=str(user_id),
                actor=str(admin_user.get("username") or "admin"),
            )
        except ValidationError as exc:
            return _user_input_error(exc)
        try:
            result = user_command_service.force_password_reset(
                command,
                allowed_account_ids=managed_account_ids,
                audit_context=UserAuditContext(
                    actor=str(admin_user.get("username") or "admin"),
                    source="api",
                ),
            )
        except Exception as e:
            return _user_service_error(e)
        return {
            "status": result.status,
            "user_id": str(result.user.id or user_id),
            "username": str(result.user.username or ""),
            "revoked_sessions": result.revoked_sessions,
        }

    return router
