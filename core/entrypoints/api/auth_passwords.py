"""Auth password management routes extracted from serve.py."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.email import normalize_email_address
from core.application.auth.rate_limit import ClearUserLoginFailures
from core.application.management.requests import (
    AuthChangePasswordRequest,
    AuthPasswordResetConfirmRequest,
    AuthPasswordResetRequest,
    AuthPasswordResetVerifyRequest,
)


def create_auth_passwords_router(
    *,
    local_auth_enabled: Callable[[], bool],
    password_reset_enabled: Callable[[], bool],
    get_logger_only: Callable[[], Any],
    auth_tables_available: Callable[[Any], bool],
    auth_password_reset_tables_available: Callable[[Any], bool],
    auth_user_column_exists: Callable[[Any, str], bool],
    require_authenticated_user: Callable[..., dict],
    get_user_by_username: Callable[[Any, str], dict | None],
    verify_password: Callable[[str, str], bool],
    password_hash: Callable[[str], str],
    request_auth_token: Callable[[Request], str],
    session_token_hash: Callable[[str], str],
    request_client_ip: Callable[[Request], str],
    password_reset_throttle_check: Callable[..., tuple[bool, int]],
    password_reset_throttle_record: Callable[..., None],
    password_reset_token_hash: Callable[[str], str],
    auth_user_deleted_condition: Callable[..., str],
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
    dispatch_password_reset_email: Callable[..., dict],
    password_reset_debug_reveal_token: Callable[[], bool],
    auth_login_clear_user_failures: ClearUserLoginFailures,
    resolve_actor_username: Callable[..., str],
    api_error_response: Callable[[Exception], JSONResponse],
    log_auth_audit_event: Callable[..., None],
    sql_update_user_password_by_id: str,
    sql_user_username_column: str,
    sql_limit_1: str,
    auth_password_min_length: int,
    auth_password_reset_ttl_minutes: int,
    err_local_auth_disabled: str,
    err_password_reset_flow_disabled: str,
    err_database_not_configured: str,
    err_auth_tables_not_available: str,
    err_auth_password_reset_tables_not_available: str,
    err_authentication_required: str,
    err_account_disabled: str,
    err_username_required: str,
    err_too_many_password_reset_requests: str,
) -> APIRouter:
    _ = err_username_required
    router = APIRouter(tags=["auth-passwords"])

    _local_auth_enabled = local_auth_enabled
    _password_reset_enabled = password_reset_enabled
    _get_logger_only = get_logger_only
    _auth_tables_available = auth_tables_available
    _auth_password_reset_tables_available = auth_password_reset_tables_available
    _auth_user_column_exists = auth_user_column_exists
    _require_authenticated_user = require_authenticated_user
    _get_user_by_username = get_user_by_username
    _verify_password = verify_password
    _password_hash = password_hash
    _request_auth_token = request_auth_token
    _session_token_hash = session_token_hash
    _request_client_ip = request_client_ip
    _password_reset_throttle_check = password_reset_throttle_check
    _password_reset_throttle_record = password_reset_throttle_record
    _password_reset_token_hash = password_reset_token_hash
    _auth_user_deleted_condition = auth_user_deleted_condition
    _real_dict_cursor_kwargs = real_dict_cursor_kwargs
    _dispatch_password_reset_email = dispatch_password_reset_email
    _password_reset_debug_reveal_token = password_reset_debug_reveal_token
    _auth_login_clear_user_failures = auth_login_clear_user_failures
    _resolve_actor_username = resolve_actor_username
    _api_error_response = api_error_response
    _log_auth_audit_event = log_auth_audit_event

    SQL_UPDATE_USER_PASSWORD_BY_ID = sql_update_user_password_by_id
    SQL_USER_USERNAME_COLUMN = sql_user_username_column
    SQL_LIMIT_1 = sql_limit_1

    _AUTH_PASSWORD_MIN_LENGTH = auth_password_min_length
    _AUTH_PASSWORD_RESET_TTL_MINUTES = auth_password_reset_ttl_minutes

    ERR_LOCAL_AUTH_DISABLED = err_local_auth_disabled
    ERR_PASSWORD_RESET_FLOW_DISABLED = err_password_reset_flow_disabled
    ERR_DATABASE_NOT_CONFIGURED = err_database_not_configured
    ERR_AUTH_TABLES_NOT_AVAILABLE = err_auth_tables_not_available
    ERR_PASSWORD_RESET_TABLES_NOT_AVAILABLE = err_auth_password_reset_tables_not_available
    ERR_AUTHENTICATION_REQUIRED = err_authentication_required
    ERR_ACCOUNT_DISABLED = err_account_disabled
    ERR_TOO_MANY_PASSWORD_RESET_REQUESTS = err_too_many_password_reset_requests

    @router.post("/api/auth/change-password")
    async def api_auth_change_password(request: Request, body: AuthChangePasswordRequest):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)

        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        try:
            auth_user = _require_authenticated_user(request)
        except PermissionError:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)

        username = str(auth_user.get("username") or "").strip()
        if not username:
            return JSONResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status_code=401)
        if len(str(body.new_password or "")) < _AUTH_PASSWORD_MIN_LENGTH:
            return JSONResponse(
                {"error": f"new_password must be at least {_AUTH_PASSWORD_MIN_LENGTH} characters"},
                status_code=400,
            )

        user = _get_user_by_username(bl, username)
        if not user or not bool(user.get("is_active", True)):
            return JSONResponse({"error": ERR_ACCOUNT_DISABLED}, status_code=403)
        if not _verify_password(
            str(body.current_password or ""), str(user.get("password_hash") or "")
        ):
            _log_auth_audit_event(
                bl,
                action="password_change",
                username=username,
                actor=username,
                status="rejected",
                detail={"reason": "invalid_current_password"},
            )
            return JSONResponse({"error": "Invalid current password"}, status_code=401)

        revoked_sessions = 0
        try:
            with bl.conn.cursor() as cur:
                if _auth_user_column_exists(bl, "must_change_password"):
                    cur.execute(
                        "UPDATE users SET password_hash = %s, must_change_password = false WHERE id = %s",
                        (_password_hash(str(body.new_password or "")), str(user["id"])),
                    )
                else:
                    cur.execute(
                        SQL_UPDATE_USER_PASSWORD_BY_ID,
                        (_password_hash(str(body.new_password or "")), str(user["id"])),
                    )
                if bool(body.revoke_other_sessions):
                    current_token = _request_auth_token(request)
                    if current_token:
                        cur.execute(
                            """
                            UPDATE auth_sessions
                            SET revoked = true, updated_at = now()
                            WHERE user_id = %s
                              AND revoked = false
                              AND session_token_hash <> %s
                            """,
                            (str(user["id"]), _session_token_hash(current_token)),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE auth_sessions
                            SET revoked = true, updated_at = now()
                            WHERE user_id = %s
                              AND revoked = false
                            """,
                            (str(user["id"]),),
                        )
                    revoked_sessions = int(getattr(cur, "rowcount", 0) or 0)
        except Exception as e:
            return _api_error_response(e)

        _log_auth_audit_event(
            bl,
            action="password_change",
            username=username,
            actor=username,
            status="ok",
            detail={
                "revoked_other_sessions": bool(body.revoke_other_sessions),
                "revoked_sessions": revoked_sessions,
            },
        )
        return {"status": "password_updated", "revoked_sessions": revoked_sessions}

    @router.post("/api/auth/password-reset/request")
    async def api_auth_password_reset_request(body: AuthPasswordResetRequest, request: Request):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        if not _password_reset_enabled():
            return JSONResponse({"error": ERR_PASSWORD_RESET_FLOW_DISABLED}, status_code=400)

        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_password_reset_tables_available(bl):
            return JSONResponse({"error": ERR_PASSWORD_RESET_TABLES_NOT_AVAILABLE}, status_code=503)

        try:
            email = normalize_email_address(body.email, field_name="email")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        client_ip = _request_client_ip(request)

        # Throttle password reset requests per IP
        throttled, retry_after = _password_reset_throttle_check(
            client_ip, action="request", subject=email
        )
        if throttled:
            return JSONResponse(
                {"error": ERR_TOO_MANY_PASSWORD_RESET_REQUESTS},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        _password_reset_throttle_record(client_ip, action="request", subject=email)

        response_payload: dict[str, Any] = {"status": "accepted"}
        user = _get_user_by_username(bl, email)
        if not user or not bool(user.get("is_active", True)):
            _log_auth_audit_event(
                bl,
                action="password_reset_request",
                username=email,
                actor="auth",
                status="accepted",
                detail={"user_found": False, "client_ip": client_ip},
            )
            return response_payload

        reset_token = secrets.token_urlsafe(48)
        reset_token_hash = _password_reset_token_hash(reset_token)
        user_id = str(user.get("id") or "").strip()
        try:
            with bl.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth_password_resets
                    SET
                        used_at = now(),
                        used_by = 'auth',
                        invalidated_reason = 'superseded'
                    WHERE user_id = %s
                      AND used_at IS NULL
                      AND expires_at > now()
                    """,
                    (user_id,),
                )
                cur.execute(
                    """
                    INSERT INTO auth_password_resets (
                        user_id,
                        token_hash,
                        expires_at,
                        requested_by,
                        request_ip,
                        metadata_jsonb
                    ) VALUES (%s,%s, now() + make_interval(mins => %s), %s, %s, %s)
                    """,
                    (
                        user_id,
                        reset_token_hash,
                        int(_AUTH_PASSWORD_RESET_TTL_MINUTES),
                        "self_service",
                        client_ip,
                        json.dumps({"flow": "password_reset_request"}),
                    ),
                )
            _auth_login_clear_user_failures(email)
        except Exception as e:
            return _api_error_response(e)

        delivery = _dispatch_password_reset_email(request, user, reset_token)
        delivery_status = (
            "sent"
            if bool(delivery.get("ok"))
            else str(delivery.get("reason") or "").strip().lower() or "not_sent"
        )

        if _password_reset_debug_reveal_token():
            response_payload["reset_token"] = reset_token
            response_payload["expires_in_minutes"] = int(_AUTH_PASSWORD_RESET_TTL_MINUTES)
        _log_auth_audit_event(
            bl,
            action="password_reset_request",
            username=email,
            actor="auth",
            status="ok",
            detail={
                "user_found": True,
                "client_ip": client_ip,
                "email_delivery": delivery_status,
            },
        )
        return response_payload

    @router.post("/api/auth/password-reset/verify")
    async def api_auth_password_reset_verify(
        body: AuthPasswordResetVerifyRequest, request: Request
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        if not _password_reset_enabled():
            return JSONResponse({"error": ERR_PASSWORD_RESET_FLOW_DISABLED}, status_code=400)

        client_ip = _request_client_ip(request)
        throttled, retry_after = _password_reset_throttle_check(client_ip, action="verify")
        if throttled:
            return JSONResponse(
                {"error": ERR_TOO_MANY_PASSWORD_RESET_REQUESTS},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_password_reset_tables_available(bl):
            return JSONResponse({"error": ERR_PASSWORD_RESET_TABLES_NOT_AVAILABLE}, status_code=503)

        token = str(body.token or "").strip()
        if not token:
            return JSONResponse({"error": "token is required"}, status_code=400)
        _password_reset_throttle_record(client_ip, action="verify")

        row = None
        deleted_condition = _auth_user_deleted_condition(bl, alias="u")
        try:
            cursor_kwargs: dict[str, Any] = {}
            try:
                from core.infrastructure.runtime.entrypoint_db_adapters import (
                    dict_cursor_kwargs,
                )

                cursor_kwargs.update(dict_cursor_kwargs())
            except Exception:
                pass

            with bl.conn.cursor(**cursor_kwargs) as cur:
                query = "\n".join(
                    [
                        "SELECT",
                        "    pr.id,",
                        "    pr.user_id,",
                        "    pr.expires_at,",
                        "    pr.used_at,",
                        SQL_USER_USERNAME_COLUMN,
                        "    u.is_active",
                        "FROM auth_password_resets pr",
                        "JOIN users u ON u.id = pr.user_id",
                        "WHERE pr.token_hash = %s",
                        deleted_condition,
                        SQL_LIMIT_1,
                    ]
                )
                cur.execute(
                    query,
                    (_password_reset_token_hash(token),),
                )
                row = cur.fetchone()
        except Exception as e:
            return _api_error_response(e)

        if not row:
            return {"valid": False}
        is_valid = (
            bool(row.get("is_active", True))
            and row.get("used_at") is None
            and bool(row.get("expires_at"))
            and row.get("expires_at") > datetime.now(UTC)
        )
        # Deliberately no email/username in the response. This endpoint is
        # unauthenticated and takes only a token, so echoing the address turns any
        # leaked reset link — browser history, a referer header, a forwarded
        # message — into a disclosure of which account it belongs to. The reset
        # page only needs to know whether the token is usable.
        return {"valid": bool(is_valid)}

    @router.post("/api/auth/password-reset/confirm")
    async def api_auth_password_reset_confirm(
        body: AuthPasswordResetConfirmRequest, request: Request
    ):
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        if not _password_reset_enabled():
            return JSONResponse({"error": ERR_PASSWORD_RESET_FLOW_DISABLED}, status_code=400)

        client_ip = _request_client_ip(request)
        throttled, retry_after = _password_reset_throttle_check(client_ip, action="confirm")
        if throttled:
            return JSONResponse(
                {"error": ERR_TOO_MANY_PASSWORD_RESET_REQUESTS},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        _password_reset_throttle_record(client_ip, action="confirm")

        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
        if not _auth_password_reset_tables_available(bl):
            return JSONResponse({"error": ERR_PASSWORD_RESET_TABLES_NOT_AVAILABLE}, status_code=503)

        token = str(body.token or "").strip()
        if not token:
            return JSONResponse({"error": "token is required"}, status_code=400)
        if len(str(body.new_password or "")) < _AUTH_PASSWORD_MIN_LENGTH:
            return JSONResponse(
                {"error": f"new_password must be at least {_AUTH_PASSWORD_MIN_LENGTH} characters"},
                status_code=400,
            )

        reset_row = None
        revoked_sessions = 0
        actor = _resolve_actor_username(request, "", default="auth")
        token_hash = _password_reset_token_hash(token)
        deleted_condition = _auth_user_deleted_condition(bl, alias="u")
        try:
            with bl.conn.cursor(**_real_dict_cursor_kwargs()) as cur:
                query = "\n".join(
                    [
                        "SELECT",
                        "    pr.id,",
                        "    pr.user_id,",
                        SQL_USER_USERNAME_COLUMN,
                        "    u.is_active",
                        "FROM auth_password_resets pr",
                        "JOIN users u ON u.id = pr.user_id",
                        "WHERE pr.token_hash = %s",
                        "  AND pr.used_at IS NULL",
                        "  AND pr.expires_at > now()",
                        deleted_condition,
                        SQL_LIMIT_1,
                        "FOR UPDATE",
                    ]
                )
                cur.execute(
                    query,
                    (token_hash,),
                )
                reset_row = cur.fetchone()
                if not reset_row or not bool(reset_row.get("is_active", True)):
                    _log_auth_audit_event(
                        bl,
                        action="password_reset_confirm",
                        username="",
                        actor=actor,
                        status="rejected",
                        detail={"reason": "invalid_or_expired_token"},
                    )
                    return JSONResponse(
                        {"error": "Invalid or expired reset token"}, status_code=400
                    )

                user_id = str(reset_row.get("user_id") or "").strip()
                username = str(reset_row.get("username") or "").strip()
                if _auth_user_column_exists(bl, "must_change_password"):
                    cur.execute(
                        "UPDATE users SET password_hash = %s, must_change_password = false WHERE id = %s",
                        (_password_hash(str(body.new_password or "")), user_id),
                    )
                else:
                    cur.execute(
                        SQL_UPDATE_USER_PASSWORD_BY_ID,
                        (_password_hash(str(body.new_password or "")), user_id),
                    )
                if bool(body.revoke_other_sessions):
                    cur.execute(
                        """
                        UPDATE auth_sessions
                        SET revoked = true, updated_at = now()
                        WHERE user_id = %s
                          AND revoked = false
                        """,
                        (user_id,),
                    )
                    revoked_sessions = int(getattr(cur, "rowcount", 0) or 0)

                cur.execute(
                    """
                    UPDATE auth_password_resets
                    SET
                        used_at = now(),
                        used_by = %s,
                        invalidated_reason = 'used'
                    WHERE id = %s
                    """,
                    (actor, str(reset_row.get("id") or "")),
                )
                cur.execute(
                    """
                    UPDATE auth_password_resets
                    SET
                        used_at = now(),
                        used_by = %s,
                        invalidated_reason = 'superseded'
                    WHERE user_id = %s
                      AND id <> %s
                      AND used_at IS NULL
                    """,
                    (actor, user_id, str(reset_row.get("id") or "")),
                )
                _auth_login_clear_user_failures(username)
        except Exception as e:
            return _api_error_response(e)

        _log_auth_audit_event(
            bl,
            action="password_reset_confirm",
            username=str((reset_row or {}).get("username") or ""),
            actor=actor,
            status="ok",
            detail={
                "revoked_other_sessions": bool(body.revoke_other_sessions),
                "revoked_sessions": revoked_sessions,
            },
        )
        return {"status": "password_reset_confirmed", "revoked_sessions": revoked_sessions}

    return router
