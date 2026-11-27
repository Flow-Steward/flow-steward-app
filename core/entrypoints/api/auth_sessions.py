"""Auth session/login routes extracted from serve.py."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.account_memberships import (
    memberships_with_role,
    resolve_login_account_memberships,
)
from core.application.auth.email import normalize_email_address, normalize_login_identifier
from core.application.auth.external_sign_in_repository import is_unusable_password
from core.application.auth.first_admin import FirstAdminAlreadyExists, FirstAdminService
from core.application.auth.local_credentials import (
    UserAuthenticatorPolicy,
    authenticator_policy_available,
    authenticator_storage_available,
    installation_requires_mfa,
    lock_local_user,
    password_age_available,
    read_authenticator_policy,
    session_auth_method_available,
)
from core.application.auth.mfa_service import LoginAuthenticatorCheck, check_login_authenticator
from core.application.auth.password_history import PasswordReuseError, require_unused_password
from core.application.auth.password_policy import (
    PASSWORD_WARNING_DAYS,
    password_expires_in_days,
    password_policy_error,
)
from core.application.management.requests import AuthBootstrapRequest, AuthLoginRequest
from core.infrastructure.runtime.entrypoint_db_adapters import (
    database_transaction as transaction_connection,
)

_DUMMY_LOGIN_PASSWORD_HASH = (
    "pbkdf2_sha256$100000$00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
_NO_STORE = {"Cache-Control": "no-store"}
#: The refusal code of switching the account of a session from external sign-in.
EXTERNAL_SESSION_ACCOUNT_PINNED_CODE = "external_session_account_pinned"
#: The refusal of switching into an account where the person has no role (ADR 0085).
NO_ROLE_IN_ACCOUNT_CODE = "no_role_in_account"
NO_ROLE_IN_ACCOUNT_MESSAGE = "You have no role in this company. Ask its administrator."
_SWITCH_SESSION_ACCOUNT_SQL = """
UPDATE auth_sessions
SET account_id = %s,
    last_seen_at = now(),
    updated_at = now()
WHERE session_token_hash = %s
  AND user_id = %s
  AND revoked = false
  AND expires_at > now()
"""
#: The same, where sessions record their method: only a local session moves (ADR 0083).
_SWITCH_LOCAL_SESSION_ACCOUNT_SQL = """
UPDATE auth_sessions
SET account_id = %s,
    last_seen_at = now(),
    updated_at = now()
WHERE session_token_hash = %s
  AND user_id = %s
  AND revoked = false
  AND expires_at > now()
  AND auth_method = 'local'
"""


def _open_invitation_token(bl: Any, token: str) -> bool:
    """Whether ``token`` names an open invitation (ADR 0085, P5); ``False`` on failure."""
    from core.application.auth.user_admin.account_invitations import AccountInvitationService

    try:
        return AccountInvitationService(get_logger=lambda: bl).token_is_open(token=token)
    except Exception:
        return False


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
    invitation_token_open: Callable[[Any, str], bool] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["auth-sessions"])
    _invitation_token_open = invitation_token_open or _open_invitation_token

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
    _AUTH_PASSWORD_MIN_LENGTH = max(12, auth_password_min_length)
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

    def _new_password_issue(*, must_change: bool, current: str, new: str) -> str:
        """Why ``new`` cannot replace the temporary password, or ``""``."""
        if not must_change:
            return "new_password is accepted only when a password change is required"
        if len(new) < _AUTH_PASSWORD_MIN_LENGTH:
            return f"new_password must be at least {_AUTH_PASSWORD_MIN_LENGTH} characters"
        if secrets.compare_digest(new.encode("utf-8"), current.encode("utf-8")):
            return "new_password must differ from the current password"
        return password_policy_error(new, _AUTH_PASSWORD_MIN_LENGTH, field="new_password")

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
        issue = password_policy_error(password, _AUTH_PASSWORD_MIN_LENGTH)
        if issue:
            return JSONResponse({"error": issue}, status_code=400)

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

    def _factor_error_response(
        bl: Any, *, factor: LoginAuthenticatorCheck, email: str, client_ip: str
    ) -> JSONResponse:
        if factor.failure == "mfa_enrollment_required":
            payload: dict[str, Any] = {
                "error": "Multi-factor authentication setup is required",
                "mfa_enrollment_required": True,
            }
            if factor.enrollment is not None:
                payload["authenticator_enrollment"] = factor.enrollment
            return JSONResponse(
                payload,
                status_code=403,
                headers=_NO_STORE,
            )
        if factor.failure == "mfa_invalid_code":
            throttled, retry_after = _auth_login_record_failure(email, client_ip)
            _log_auth_audit_event(
                bl,
                action="login",
                username=email,
                actor="auth",
                status="rejected",
                detail={"reason": "mfa_invalid_code", "client_ip": client_ip},
            )
            if throttled:
                return JSONResponse(
                    {"error": ERR_TOO_MANY_LOGIN_ATTEMPTS, "retry_after_seconds": retry_after},
                    headers={**_NO_STORE, "Retry-After": str(retry_after)},
                    status_code=429,
                )
        return JSONResponse(
            {"error": "An authenticator or recovery code is required", "mfa_required": True},
            status_code=403,
            headers=_NO_STORE,
        )

    @router.post("/api/auth/login")
    def api_auth_login(body: AuthLoginRequest, request: Request):
        """Authenticate a local user and create the session cookie.

        Temporary and expired passwords can only authorize their own replacement.
        An enrolled user supplies an authenticator or recovery code before issuing
        a session or committing any password replacement.
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
        if is_unusable_password(password_hash_value):
            # A user who signs in only externally (ADR 0083) has no local password;
            # the dummy hash keeps the answer as slow as for any wrong password.
            password_hash_value = _DUMMY_LOGIN_PASSWORD_HASH
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

        days_until_expiry = password_expires_in_days(user.get("password_changed_at"))
        password_expired = days_until_expiry == 0
        change_flag = bool(user.get("must_change_password", False))
        must_change_password = change_flag or password_expired
        new_password = body.new_password
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
                    **({"password_expired": True} if password_expired else {}),
                },
                status_code=403,
            )
        if new_password:
            new_password_issue = _new_password_issue(
                must_change=must_change_password,
                current=body.password,
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

        all_memberships = resolve_login_account_memberships(bl, user)
        memberships, refused = memberships_with_role(all_memberships)
        invitation_only = False
        if refused and body.invitation_token and _invitation_token_open(bl, body.invitation_token):
            # ADR 0085, P5: a person with a role nowhere signs in only to answer the
            # invitation link they hold. The session lands in their home account, where
            # they have no role, so every route but the invitation's, the session's own
            # reads, switching accounts and signing out refuses them.
            invitation_only = True
            refused = False
            home = str(user.get("account_id") or "").strip()
            memberships = sorted(
                all_memberships,
                key=lambda item: str(item.get("account_id") or "").strip() != home,
            )[:1]
        if refused:
            # ADR 0085: the person has a role in no account; a session would be empty.
            _log_auth_audit_event(
                bl,
                action="login",
                username=email,
                actor="auth",
                status="rejected",
                detail={"reason": NO_ROLE_IN_ACCOUNT_CODE, "client_ip": client_ip},
            )
            return JSONResponse(
                {"error": NO_ROLE_IN_ACCOUNT_MESSAGE, "code": NO_ROLE_IN_ACCOUNT_CODE},
                status_code=403,
            )
        selected_account_id = str(body.account_id or "").strip()
        if (
            selected_account_id
            and not any(
                str(item.get("account_id") or "").strip() == selected_account_id
                for item in memberships
            )
            and any(
                str(item.get("account_id") or "").strip() == selected_account_id
                for item in all_memberships
            )
        ):
            # A chosen account without a role there: land where the person has one.
            selected_account_id = ""
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

        storage_available = authenticator_storage_available(bl)
        policy_available = authenticator_policy_available(bl)
        require_factor = installation_requires_mfa()
        if require_factor and not storage_available:
            return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)

        session_token = secrets.token_urlsafe(48)
        token_hash = _session_token_hash(session_token)
        session_ttl_hours = (
            _AUTH_REMEMBER_SESSION_TTL_HOURS if bool(body.remember_me) else _AUTH_SESSION_TTL_HOURS
        )
        revoked_sessions = 0
        factor = LoginAuthenticatorCheck()
        try:
            with transaction_connection(bl) as conn, conn.cursor() as cur:
                user_id = str(user["id"])
                needs_lock = (
                    storage_available
                    or policy_available
                    or (must_change_password and password_age_available(bl))
                )
                if needs_lock and not lock_local_user(
                    cur,
                    user_id=user_id,
                    password_hash=password_hash_value,
                    must_change_password=change_flag,
                ):
                    return JSONResponse({"error": "Invalid credentials"}, status_code=401)

                policy = (
                    read_authenticator_policy(cur, user_id)
                    if policy_available
                    else UserAuthenticatorPolicy()
                )
                if not storage_available and (
                    policy.enforced or body.authenticator_enrollment_token
                ):
                    return JSONResponse({"error": ERR_AUTH_TABLES_NOT_AVAILABLE}, status_code=503)
                if storage_available:
                    if body.mfa_code or body.authenticator_enrollment_token:
                        locked, retry_after = _auth_login_lock_state(email, client_ip)
                        if locked:
                            return JSONResponse(
                                {
                                    "error": ERR_TOO_MANY_LOGIN_ATTEMPTS,
                                    "retry_after_seconds": retry_after,
                                },
                                headers={**_NO_STORE, "Retry-After": str(retry_after)},
                                status_code=429,
                            )
                    factor = check_login_authenticator(
                        cur,
                        user_id=user_id,
                        username=str(user.get("username") or email),
                        account_id=selected_account_id,
                        code=body.mfa_code,
                        enrollment_token=body.authenticator_enrollment_token,
                        installation_required=require_factor,
                        policy=policy,
                    )

                if must_change_password and (not factor.failure or factor.enrollment is not None):
                    require_unused_password(
                        cur,
                        user_id=user_id,
                        password=new_password,
                        current_hash=password_hash_value,
                        verify=_verify_password,
                    )
                if factor.failure:
                    return _factor_error_response(
                        bl, factor=factor, email=email, client_ip=client_ip
                    )
                if must_change_password:
                    revoked_sessions = _replace_temporary_password(
                        bl, cur, user_id=user_id, new_password=new_password
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
        except PasswordReuseError as exc:
            return JSONResponse({"error": str(exc), "field": "new_password"}, status_code=400)
        except Exception as e:
            return _api_error_response(e)

        _auth_login_clear_failures(email, client_ip)
        if must_change_password:
            _log_auth_audit_event(
                bl,
                action="password_change",
                username=str(user.get("username") or email),
                actor=str(user.get("username") or email),
                status="ok",
                detail={
                    "reason": "expired_password_replaced"
                    if password_expired
                    else "temporary_password_replaced",
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
        if invitation_only:
            payload["invitation_only"] = True
        if (
            not must_change_password
            and days_until_expiry is not None
            and days_until_expiry <= PASSWORD_WARNING_DAYS
        ):
            payload["password_expires_in_days"] = days_until_expiry
        csrf_token = secrets.token_urlsafe(32)
        payload["csrf_token"] = csrf_token
        if factor.recovery_codes is not None:
            payload["recovery_codes"] = factor.recovery_codes
        response = JSONResponse(
            payload, headers=_NO_STORE if factor.recovery_codes is not None else None
        )
        response.set_cookie(
            key=_AUTH_COOKIE_NAME,
            value=session_token,
            httponly=True,
            samesite="lax",
            secure=_auth_cookie_secure(),
            max_age=session_ttl_hours * 3600,
        )
        # CSRF double-submit: the SPA reads this cookie and echoes it in a header, so it
        # cannot be HttpOnly. The session cookie above stays HttpOnly.
        response.set_cookie(  # NOSONAR - CSRF double-submit cookie must be readable by the SPA
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
                **({"invitation_only": True} if invitation_only else {}),
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
        auth_source = str(
            getattr(request.state, "auth_source", "") or auth_user.get("auth_source") or ""
        )
        if auth_source.strip().lower() == "external":
            # ADR 0083: an external session is pinned to the account that issued it.
            _log_auth_audit_event(
                bl,
                action="switch_account",
                username=str(auth_user.get("username") or ""),
                actor=_resolve_actor_username(request, "", default="auth"),
                status="rejected",
                detail={
                    "reason": EXTERNAL_SESSION_ACCOUNT_PINNED_CODE,
                    "account_id": str(auth_user.get("account_id") or ""),
                },
            )
            return JSONResponse(
                {
                    "error": "A session from external sign-in stays in the account that "
                    "issued it. Sign in to the other account instead.",
                    "code": EXTERNAL_SESSION_ACCOUNT_PINNED_CODE,
                },
                status_code=403,
            )

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
        if "role" in selected_membership and not str(selected_membership.get("role") or "").strip():
            # ADR 0085: a membership without a role grants nothing; a session moved
            # there would be empty. Refuse instead and say whom to ask.
            _log_auth_audit_event(
                bl,
                action="switch_account",
                username=str(auth_user.get("username") or ""),
                actor=_resolve_actor_username(request, "", default="auth"),
                status="rejected",
                detail={"reason": NO_ROLE_IN_ACCOUNT_CODE, "account_id": selected_account_id},
            )
            return JSONResponse(
                {"error": NO_ROLE_IN_ACCOUNT_MESSAGE, "code": NO_ROLE_IN_ACCOUNT_CODE},
                status_code=403,
            )

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
                    _SWITCH_LOCAL_SESSION_ACCOUNT_SQL
                    if session_auth_method_available(bl)
                    else _SWITCH_SESSION_ACCOUNT_SQL,
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
