"""Auth session/login routes extracted from serve.py."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.account_memberships import resolve_login_account_memberships
from core.application.auth.email import normalize_email_address, normalize_login_identifier
from core.application.auth.first_admin import FirstAdminAlreadyExists, FirstAdminService
from core.application.management.requests import AuthBootstrapRequest, AuthLoginRequest

_DUMMY_LOGIN_PASSWORD_HASH = (
    "pbkdf2_sha256$100000$00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def create_auth_sessions_router(
    *,
    get_logger_only: Callable[[], Any],
    session_token_hash: Callable[[str], str],
    auth_cookie_secure: Callable[[], bool],
    log_auth_audit_event: Callable[..., None],
    local_auth_enabled: Callable[[], bool],
    bootstrap_request_allowed: Callable[[Request], tuple[bool, str]],
    auth_tables_available: Callable[[Any], bool],
    resolve_actor_username: Callable[..., str],
    get_user_by_username: Callable[[Any, str], dict | None],
    request_client_ip: Callable[[Request], str],
    auth_login_lock_state: Callable[[str, str], tuple[bool, int]],
    auth_login_record_failure: Callable[[str, str], tuple[bool, int]],
    verify_password: Callable[[str, str], bool],
    password_hash: Callable[[str], str],
    auth_user_column_exists: Callable[[Any, str], bool],
    auth_login_clear_failures: Callable[[str, str], None],
    csrf_request_valid: Callable[[Request], bool],
    api_error_response: Callable[[Exception], JSONResponse],
    err_database_not_configured: str,
    err_local_auth_disabled: str,
    err_auth_tables_not_available: str,
    err_too_many_login_attempts: str,
    auth_password_min_length: int,
    auth_session_ttl_hours: int,
    auth_remember_session_ttl_hours: int,
    auth_cookie_name: str,
    auth_csrf_cookie_name: str,
    first_admin_service_factory: Callable[[Any], FirstAdminService],
) -> APIRouter:
    router = APIRouter(tags=["auth-sessions"])

    _get_logger_only = get_logger_only
    _session_token_hash = session_token_hash
    _auth_cookie_secure = auth_cookie_secure
    _log_auth_audit_event = log_auth_audit_event
    _local_auth_enabled = local_auth_enabled
    _bootstrap_request_allowed = bootstrap_request_allowed
    _auth_tables_available = auth_tables_available
    _resolve_actor_username = resolve_actor_username
    _get_user_by_username = get_user_by_username
    _request_client_ip = request_client_ip
    _auth_login_lock_state = auth_login_lock_state
    _auth_login_record_failure = auth_login_record_failure
    _verify_password = verify_password
    _password_hash = password_hash
    _auth_user_column_exists = auth_user_column_exists
    _auth_login_clear_failures = auth_login_clear_failures
    _csrf_request_valid = csrf_request_valid
    _api_error_response = api_error_response
    _first_admin_service_factory = first_admin_service_factory

    ERR_DATABASE_NOT_CONFIGURED = err_database_not_configured
    ERR_LOCAL_AUTH_DISABLED = err_local_auth_disabled
    ERR_AUTH_TABLES_NOT_AVAILABLE = err_auth_tables_not_available
    ERR_TOO_MANY_LOGIN_ATTEMPTS = err_too_many_login_attempts
    _AUTH_PASSWORD_MIN_LENGTH = auth_password_min_length
    _AUTH_SESSION_TTL_HOURS = auth_session_ttl_hours
    _AUTH_REMEMBER_SESSION_TTL_HOURS = auth_remember_session_ttl_hours
    _AUTH_COOKIE_NAME = auth_cookie_name
    _AUTH_CSRF_COOKIE_NAME = auth_csrf_cookie_name

    def _conn_commit(conn: Any) -> None:
        commit = getattr(conn, "commit", None)
        if callable(commit):
            commit()

    def _conn_rollback(conn: Any) -> None:
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()

    def _new_password_issue(*, must_change: bool, temporary: str, new: str) -> str:
        """Why ``new`` cannot replace the temporary password, or ``""``."""
        if not must_change:
            return "new_password is accepted only when a password change is required"
        if len(new) < _AUTH_PASSWORD_MIN_LENGTH:
            return f"new_password must be at least {_AUTH_PASSWORD_MIN_LENGTH} characters"
        if secrets.compare_digest(new.encode("utf-8"), temporary.encode("utf-8")):
            return "new_password must differ from the temporary password"
        return ""

    def _replace_temporary_password(bl: Any, cur: Any, *, user_id: str, new_password: str) -> int:
        """Store the new password, clear the flag and revoke every session."""
        new_hash = _password_hash(new_password)
        if _auth_user_column_exists(bl, "must_change_password"):
            cur.execute(
                "UPDATE users SET password_hash = %s, must_change_password = false WHERE id = %s",
                (new_hash, user_id),
            )
        else:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user_id))
        cur.execute(
            """
            UPDATE auth_sessions
            SET revoked = true, updated_at = now()
            WHERE user_id = %s
              AND revoked = false
            """,
            (user_id,),
        )
        return int(getattr(cur, "rowcount", 0) or 0)

    @router.post("/api/auth/bootstrap")
    async def api_auth_bootstrap(body: AuthBootstrapRequest, request: Request):
        """Create initial bootstrap admin user for local auth."""
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)
        bootstrap_allowed, bootstrap_error = _bootstrap_request_allowed(request)
        if not bootstrap_allowed:
            return JSONResponse({"error": bootstrap_error}, status_code=403)

        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)

        try:
            email = normalize_email_address(body.email, field_name="email")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        password = str(body.password or "")
        display_name = str(body.display_name or "").strip()
        if len(password) < _AUTH_PASSWORD_MIN_LENGTH:
            return JSONResponse(
                {"error": f"password must be at least {_AUTH_PASSWORD_MIN_LENGTH} characters"},
                status_code=400,
            )

        try:
            user_row = _first_admin_service_factory(bl).create(
                email=email, password=password, display_name=display_name
            )
            user_id = str(user_row["id"])
        except FirstAdminAlreadyExists:
            _log_auth_audit_event(
                bl,
                action="bootstrap",
                username=email,
                actor=_resolve_actor_username(request, "", default="system"),
                status="rejected",
                detail={"reason": "already_bootstrapped"},
            )
            return JSONResponse({"error": "Bootstrap already completed"}, status_code=409)
        except Exception as e:
            return _api_error_response(e)

        _log_auth_audit_event(
            bl,
            action="bootstrap",
            username=email,
            actor=_resolve_actor_username(request, "", default="system"),
            status="ok",
            detail={"roles": ["admin"]},
        )

        return {
            "user": {
                "id": user_id,
                "email": email,
                "username": email,
                "display_name": display_name or email,
                "roles": ["admin"],
            },
            "status": "bootstrapped",
        }

    @router.post("/api/auth/login")
    async def api_auth_login(body: AuthLoginRequest, request: Request):
        """Authenticate a local user and create the session cookie.

        A temporary password (``must_change_password``) never signs in by itself:
        it answers 403 ``password_change_required`` until the same request also
        carries ``new_password``, which replaces it, revokes the user's other
        sessions and then signs in.
        """
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)

        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)

        try:
            email = normalize_login_identifier(body.email, field_name="email")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        client_ip = _request_client_ip(request)
        locked, retry_after = _auth_login_lock_state(email, client_ip)
        if locked:
            _log_auth_audit_event(
                bl,
                action="login",
                username=email,
                actor="auth",
                status="rejected",
                detail={
                    "reason": "login_throttled",
                    "retry_after_seconds": retry_after,
                    "client_ip": client_ip,
                },
            )
            return JSONResponse(
                {
                    "error": ERR_TOO_MANY_LOGIN_ATTEMPTS,
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
                status_code=429,
            )

        user = _get_user_by_username(bl, email)
        user_active = user is not None and bool(user.get("is_active", True))
        password_hash_value = _DUMMY_LOGIN_PASSWORD_HASH
        if user_active and user is not None:
            password_hash_value = str(user.get("password_hash") or "")
        password_valid = _verify_password(body.password, password_hash_value)
        if not user_active:
            throttled, retry_after = _auth_login_record_failure(email, client_ip)
            _log_auth_audit_event(
                bl,
                action="login",
                username=email,
                actor="auth",
                status="rejected",
                detail={
                    "reason": "invalid_credentials",
                    "client_ip": client_ip,
                    "throttled": throttled,
                },
            )
            if throttled:
                return JSONResponse(
                    {
                        "error": ERR_TOO_MANY_LOGIN_ATTEMPTS,
                        "retry_after_seconds": retry_after,
                    },
                    headers={"Retry-After": str(retry_after)},
                    status_code=429,
                )
            return JSONResponse({"error": "Invalid credentials"}, status_code=401)
        assert user is not None
        if not password_valid:
            throttled, retry_after = _auth_login_record_failure(email, client_ip)
            _log_auth_audit_event(
                bl,
                action="login",
                username=email,
                actor="auth",
                status="rejected",
                detail={
                    "reason": "invalid_credentials",
                    "client_ip": client_ip,
                    "throttled": throttled,
                },
            )
            if throttled:
                return JSONResponse(
                    {
                        "error": ERR_TOO_MANY_LOGIN_ATTEMPTS,
                        "retry_after_seconds": retry_after,
                    },
                    headers={"Retry-After": str(retry_after)},
                    status_code=429,
                )
            return JSONResponse({"error": "Invalid credentials"}, status_code=401)

        _auth_login_clear_failures(email, client_ip)
        must_change_password = bool(user.get("must_change_password", False))
        new_password = str(body.new_password or "")
        if must_change_password and not new_password:
            _log_auth_audit_event(
                bl,
                action="login",
                username=email,
                actor="auth",
                status="rejected",
                detail={"reason": "password_change_required", "client_ip": client_ip},
            )
            return JSONResponse(
                {
                    "error": "Password change required",
                    "password_change_required": True,
                    "password_min_length": _AUTH_PASSWORD_MIN_LENGTH,
                },
                status_code=403,
            )
        if new_password:
            new_password_issue = _new_password_issue(
                must_change=must_change_password,
                temporary=str(body.password or ""),
                new=new_password,
            )
            if new_password_issue:
                _log_auth_audit_event(
                    bl,
                    action="password_change",
                    username=email,
                    actor="auth",
                    status="rejected",
                    detail={"reason": "new_password_rejected", "client_ip": client_ip},
                )
                return JSONResponse(
                    {"error": new_password_issue, "field": "new_password"}, status_code=400
                )

        memberships = resolve_login_account_memberships(bl, user)
        selected_account_id = str(body.account_id or "").strip()
        if len(memberships) > 1 and not selected_account_id:
            _log_auth_audit_event(
                bl,
                action="login",
                username=email,
                actor="auth",
                status="rejected",
                detail={
                    "reason": "account_selection_required",
                    "available_account_ids": [
                        str(item.get("account_id") or "").strip()
                        for item in memberships
                        if str(item.get("account_id") or "").strip()
                    ],
                },
            )
            return JSONResponse(
                {
                    "error": "Account selection required",
                    "requires_account_selection": True,
                    "account_options": memberships,
                    "email": email,
                },
                status_code=409,
            )
        if not selected_account_id:
            selected_account_id = next(
                (
                    str(item.get("account_id") or "").strip()
                    for item in memberships
                    if str(item.get("account_id") or "").strip()
                ),
                str(user.get("account_id") or "").strip(),
            )
        selected_membership = next(
            (
                item
                for item in memberships
                if str(item.get("account_id") or "").strip() == selected_account_id
            ),
            None,
        )
        if not selected_account_id or not isinstance(selected_membership, dict):
            return JSONResponse(
                {"error": "Selected account is not available for this user"}, status_code=403
            )
        if not bool(selected_membership.get("is_active", True)):
            return JSONResponse({"error": "Selected account is disabled"}, status_code=403)

        from core.application.auth.serve_auth_runtime import auth_session_issue_guard_response

        guard_response = auth_session_issue_guard_response(
            bl=bl,
            user=user,
            request=request,
            selected_account_id=selected_account_id,
            auth_source="local",
        )
        if guard_response is not None:
            return guard_response

        session_token = secrets.token_urlsafe(48)
        token_hash = _session_token_hash(session_token)
        session_ttl_hours = (
            _AUTH_REMEMBER_SESSION_TTL_HOURS if bool(body.remember_me) else _AUTH_SESSION_TTL_HOURS
        )
        revoked_sessions = 0
        try:
            with bl.conn.cursor() as cur:
                if must_change_password:
                    revoked_sessions = _replace_temporary_password(
                        bl, cur, user_id=str(user["id"]), new_password=new_password
                    )
                session_has_account_id = False
                try:
                    session_has_account_id = bool(bl._column_exists("auth_sessions", "account_id"))
                except Exception:
                    session_has_account_id = False
                if session_has_account_id:
                    cur.execute(
                        """
                        INSERT INTO auth_sessions (
                            user_id,
                            account_id,
                            session_token_hash,
                            expires_at,
                            revoked
                        )
                        VALUES (%s,%s,%s, now() + make_interval(hours => %s), false)
                        """,
                        (
                            str(user["id"]),
                            selected_account_id,
                            token_hash,
                            int(session_ttl_hours),
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO auth_sessions (user_id, session_token_hash, expires_at, revoked)
                        VALUES (%s,%s, now() + make_interval(hours => %s), false)
                        """,
                        (str(user["id"]), token_hash, int(session_ttl_hours)),
                    )
            _conn_commit(bl.conn)
        except Exception as e:
            with suppress(Exception):
                _conn_rollback(bl.conn)
            return _api_error_response(e)

        if must_change_password:
            _log_auth_audit_event(
                bl,
                action="password_change",
                username=str(user.get("username") or email),
                actor=str(user.get("username") or email),
                status="ok",
                detail={
                    "reason": "temporary_password_replaced",
                    "revoked_sessions": revoked_sessions,
                    "client_ip": client_ip,
                },
            )

        payload = {
            "user": {
                "id": str(user["id"]),
                "email": str(user.get("email") or user.get("username") or ""),
                "username": str(user.get("username") or ""),
                "display_name": str(user.get("display_name") or ""),
                "roles": list(user.get("roles") or []),
                "account_id": selected_account_id,
                "account_ids": list(user.get("account_ids") or []),
                "account_memberships": memberships,
            },
            "expires_in_hours": session_ttl_hours,
        }
        csrf_token = secrets.token_urlsafe(32)
        payload["csrf_token"] = csrf_token
        response = JSONResponse(payload)
        response.set_cookie(
            key=_AUTH_COOKIE_NAME,
            value=session_token,
            httponly=True,
            samesite="lax",
            secure=_auth_cookie_secure(),
            max_age=session_ttl_hours * 3600,
        )
        response.set_cookie(
            key=_AUTH_CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,
            samesite="lax",
            secure=_auth_cookie_secure(),
            max_age=session_ttl_hours * 3600,
        )
        _log_auth_audit_event(
            bl,
            action="login",
            username=str(user.get("username") or body.email or ""),
            actor=_resolve_actor_username(request, "", default="auth"),
            status="ok",
            detail={
                "roles": list(user.get("roles") or []),
                "account_id": selected_account_id,
                "session_ttl_hours": session_ttl_hours,
                "remember_me": bool(body.remember_me),
            },
        )
        return response

    @router.post("/api/auth/logout")
    async def api_auth_logout(request: Request):
        """Invalidate current local auth session."""
        if not _local_auth_enabled():
            return JSONResponse({"status": "logged_out"})

        bl = _get_logger_only()
        actor = _resolve_actor_username(request, "", default="auth")
        token = str(request.cookies.get(_AUTH_COOKIE_NAME) or "").strip()
        if token and not _csrf_request_valid(request):
            return JSONResponse({"error": "CSRF validation failed"}, status_code=403)
        if token and bl and _auth_tables_available(bl):
            try:
                with bl.conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE auth_sessions
                        SET revoked = true, updated_at = now()
                        WHERE session_token_hash = %s
                        """,
                        (_session_token_hash(token),),
                    )
                _conn_commit(bl.conn)
            except Exception:
                pass
        response = JSONResponse({"status": "logged_out"})
        response.delete_cookie(key=_AUTH_COOKIE_NAME)
        response.delete_cookie(key=_AUTH_CSRF_COOKIE_NAME)
        _log_auth_audit_event(
            bl,
            action="logout",
            username=actor,
            actor=actor,
            status="ok",
            detail={"had_session_token": bool(token)},
        )
        return response

    @router.post("/api/auth/switch-account")
    async def api_auth_switch_account(request: Request):
        """Switch the active account for the current auth session."""
        if not _local_auth_enabled():
            return JSONResponse({"error": ERR_LOCAL_AUTH_DISABLED}, status_code=400)

        bl = _get_logger_only()
        if not bl:
            return JSONResponse({"error": ERR_DATABASE_NOT_CONFIGURED}, status_code=503)
        if not _auth_tables_available(bl):
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)

        auth_user = getattr(request.state, "auth_user", None)
        if not isinstance(auth_user, dict):
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        session_token = str(request.cookies.get(_AUTH_COOKIE_NAME) or "").strip()
        if not session_token:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if not _csrf_request_valid(request):
            return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        selected_account_id = (
            str(body.get("account_id") or "").strip() if isinstance(body, dict) else ""
        )
        if not selected_account_id:
            return JSONResponse({"error": "account_id is required"}, status_code=400)

        memberships = resolve_login_account_memberships(bl, auth_user)
        selected_membership = next(
            (
                item
                for item in memberships
                if str(item.get("account_id") or "").strip() == selected_account_id
            ),
            None,
        )
        if not isinstance(selected_membership, dict):
            return JSONResponse(
                {"error": "Selected account is not available for this user"},
                status_code=403,
            )
        if not bool(selected_membership.get("is_active", True)):
            return JSONResponse({"error": "Selected account is disabled"}, status_code=403)

        session_has_account_id = False
        try:
            session_has_account_id = bool(bl._column_exists("auth_sessions", "account_id"))
        except Exception:
            session_has_account_id = False
        if not session_has_account_id:
            return JSONResponse(
                {"error": "Account switching schema is not applied"},
                status_code=503,
            )

        try:
            with bl.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth_sessions
                    SET account_id = %s,
                        last_seen_at = now(),
                        updated_at = now()
                    WHERE session_token_hash = %s
                      AND user_id = %s
                      AND revoked = false
                      AND expires_at > now()
                    """,
                    (
                        selected_account_id,
                        _session_token_hash(session_token),
                        str(auth_user.get("id") or "").strip(),
                    ),
                )
                if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                    return JSONResponse({"error": "Session not found"}, status_code=401)
            _conn_commit(bl.conn)
        except Exception as e:
            with suppress(Exception):
                _conn_rollback(bl.conn)
            return _api_error_response(e)

        auth_user["account_id"] = selected_account_id
        auth_user["account_ids"] = [
            str(item.get("account_id") or "").strip()
            for item in memberships
            if str(item.get("account_id") or "").strip()
        ]
        auth_user["account_memberships"] = memberships

        _log_auth_audit_event(
            bl,
            action="switch_account",
            username=str(auth_user.get("username") or ""),
            actor=_resolve_actor_username(request, "", default="auth"),
            status="ok",
            detail={
                "account_id": selected_account_id,
                "available_account_ids": auth_user["account_ids"],
            },
        )
        return {
            "status": "ok",
            "account_id": selected_account_id,
            "account_ids": list(auth_user.get("account_ids") or []),
            "account_memberships": list(auth_user.get("account_memberships") or []),
        }

    return router
