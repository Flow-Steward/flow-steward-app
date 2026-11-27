"""External sign-in routes (ADR 0083). Transport only; the logic lives in
``core.application.auth.external_sign_in_service`` and ``external_sign_in_admin``.

Public (browser navigations, no session needed):

GET /api/auth/external/options     -- {"options": [...], "account_hint_supported": bool};
                                      without ?account= only installation-wide options,
                                      with ?account=<account id or key> also that account's
GET /api/auth/external/start       -- ?method=<kind>&key=<option>&next=<path>[&intent=link]
GET /api/auth/external/callback    -- every redirecting method returns here

Account administration (an administrator of the account they act in, never another):

GET     /api/settings/external-sign-in/identities
DELETE  /api/settings/external-sign-in/identities/{identity_id}
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from core.application.auth import external_sign_in_repository as repository
from core.application.auth.external_sign_in_admin import ExternalSignInAdmin
from core.application.auth.external_sign_in_service import (
    CALLBACK_PATH,
    OPTIONS_PATH,
    ExternalSignInRefused,
    ExternalSignInService,
    SignInResult,
    StartResult,
    safe_return_path,
)
from core.application.auth.password import session_token_hash
from core.application.auth.password_reset_rate_limit import (
    password_reset_throttle_check,
    password_reset_throttle_record,
)
from core.application.management import ServiceError

#: The short-lived cookie that binds an attempt to the browser that started it.
BINDING_COOKIE = "fs_external_sign_in"
_BINDING_COOKIE_PATH = "/api/auth/external"
_BINDING_TTL_SECONDS = 600
_NO_STORE = {"Cache-Control": "no-store"}
#: Account-hint lookups of the public option list one client address may make.
HINT_MAX_ATTEMPTS = 30
HINT_RATE_WINDOW_SECONDS = 60
HINT_LOCKOUT_SECONDS = 60
_HINT_THROTTLE_ACTION = "external_options_hint"
#: Linking needs a password sign-in this recent (in seconds).
LINK_PASSWORD_MAX_AGE_SECONDS = 600
_LINK_NEEDS_RECENT_PASSWORD = (
    "To link a sign-in, sign in with your password again and open the link right away."
)
_FAILED_FLASH = (
    "External sign-in did not complete. Try again, or ask your administrator to link "
    "your sign-in to your user."
)


def create_external_sign_in_router(
    *,
    get_logger_only: Callable[[], Any],
    connection_for: Callable[[Any], AbstractContextManager[Any]],
    external_sign_in_port: Callable[[], Any],
    entitlement_service: Callable[[], Any],
    session_issue_guard: Callable[..., Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[[ServiceError], Any],
    api_error_response: Callable[..., Any],
    log_auth_audit_event: Callable[..., None],
    public_base_url: Callable[[], str],
    auth_cookie_secure: Callable[[], bool],
    auth_cookie_name: str,
    auth_csrf_cookie_name: str,
    auth_session_ttl_hours: int,
    request_client_ip: Callable[[Request], str] | None = None,
    now_fn: Callable[[], float] = time.time,
) -> APIRouter:
    router = APIRouter(tags=["auth-external-sign-in"])
    # Light throttling of account-hint lookups: a hint names an account, so a client
    # that probes many hints is slowed down (per process, per client address).
    hint_attempts: dict[str, dict[str, Any]] = {}
    hint_lock = threading.Lock()

    def _client_ip(request: Request) -> str:
        if callable(request_client_ip):
            try:
                return str(request_client_ip(request) or "") or "unknown"
            except Exception:
                return "unknown"
        client = getattr(request, "client", None)
        return str(getattr(client, "host", "") or "") or "unknown"

    def _hint_throttled(client_ip: str) -> tuple[bool, int]:
        limits = {
            "action": _HINT_THROTTLE_ACTION,
            "attempts_store": hint_attempts,
            "attempts_lock": hint_lock,
            "rate_window_seconds": HINT_RATE_WINDOW_SECONDS,
            "max_attempts": HINT_MAX_ATTEMPTS,
            "lockout_seconds": HINT_LOCKOUT_SECONDS,
            "now_fn": now_fn,
        }
        throttled, retry_after = password_reset_throttle_check(client_ip, **limits)
        if not throttled:
            password_reset_throttle_record(client_ip, **limits)
        return throttled, retry_after

    def _session_is_recent(bl: Any, session_token: str) -> bool:
        if not session_token:
            return False
        try:
            with connection_for(bl) as conn, conn.cursor() as cur:
                return repository.session_is_recent(
                    cur, session_token_hash(session_token), LINK_PASSWORD_MAX_AGE_SECONDS
                )
        except Exception:
            return False

    def _redirect_uri() -> str:
        base = str(public_base_url() or "").strip().rstrip("/")
        return f"{base}{CALLBACK_PATH}" if base else ""

    def _entitled(account_id: str, feature: str) -> bool:
        service = entitlement_service()
        check = getattr(service, "is_entitled", None)
        return bool(callable(check) and check(account_id, feature))

    def _service(bl: Any, request: Request) -> ExternalSignInService | None:
        port = external_sign_in_port()
        if bl is None or port is None:
            return None

        def guard(user: Any, account_id: str, auth_method: str) -> str | None:
            response = session_issue_guard(
                bl=bl,
                user=dict(user),
                request=request,
                selected_account_id=account_id,
                auth_source=auth_method,
            )
            return None if response is None else "session_guard_refused"

        return ExternalSignInService(
            connection=lambda: connection_for(bl),
            port=port,
            entitled=_entitled,
            audit=lambda **kwargs: log_auth_audit_event(bl, **kwargs),
            session_ttl_hours=auth_session_ttl_hours,
            session_guard=guard,
        )

    def _to_login(return_path: str, message: str = "") -> RedirectResponse:
        # A resolver's own sentence for the person, else the generic one.
        query = {"flash": str(message or "").strip()[:240] or _FAILED_FLASH}
        if return_path and return_path != "/":
            query["next"] = return_path
        response = RedirectResponse(f"/login?{urlencode(query)}", status_code=303)
        response.delete_cookie(BINDING_COOKIE, path=_BINDING_COOKIE_PATH)
        return response

    def _signed_in(result: SignInResult) -> RedirectResponse:
        response = RedirectResponse(result.return_path, status_code=303, headers=_NO_STORE)
        response.delete_cookie(BINDING_COOKIE, path=_BINDING_COOKIE_PATH)
        if not result.session_token:
            return response
        max_age = result.session_ttl_hours * 3600
        response.set_cookie(
            key=auth_cookie_name,
            value=result.session_token,
            httponly=True,
            samesite="lax",
            secure=auth_cookie_secure(),
            max_age=max_age,
        )
        # CSRF double-submit: the SPA reads this cookie, so it cannot be HttpOnly.
        response.set_cookie(  # NOSONAR - CSRF double-submit cookie must be readable by the SPA
            key=auth_csrf_cookie_name,
            value=secrets.token_urlsafe(32),
            httponly=False,
            samesite="lax",
            secure=auth_cookie_secure(),
            max_age=max_age,
        )
        return response

    @router.get(OPTIONS_PATH)
    async def external_sign_in_options(request: Request):
        bl = get_logger_only()
        service = _service(bl, request)
        empty = {"options": [], "account_hint_supported": False}
        if service is None:
            return JSONResponse(empty, headers=_NO_STORE)
        hint = str(request.query_params.get("account") or "")[:255]
        if hint.strip():
            throttled, retry_after = _hint_throttled(_client_ip(request))
            if throttled:
                return JSONResponse(
                    {"error": "Too many sign-in lookups. Try again later."},
                    status_code=429,
                    headers={**_NO_STORE, "Retry-After": str(max(1, retry_after))},
                )
        try:
            payload = await run_in_threadpool(lambda: service.public_options(hint))
        except Exception:
            payload = empty
        return JSONResponse(payload, headers=_NO_STORE)

    @router.get("/api/auth/external/start")
    async def external_sign_in_start(request: Request):
        params = request.query_params
        next_path = safe_return_path(params.get("next"))
        bl = get_logger_only()
        service = _service(bl, request)
        if service is None:
            return _to_login(next_path)
        link_user_id = None
        if params.get("intent") == "link":
            auth_user = getattr(request.state, "auth_user", None)
            auth_source = str(getattr(request.state, "auth_source", "") or "")
            if not isinstance(auth_user, dict) or auth_source != "local":
                return _to_login(next_path)
            link_user_id = str(auth_user.get("id") or "") or None
            session_token = str(request.cookies.get(auth_cookie_name) or "")
            if not link_user_id or not await run_in_threadpool(
                lambda: _session_is_recent(bl, session_token)
            ):
                # Linking adds a way in: only right after the password was entered.
                return _to_login(next_path, _LINK_NEEDS_RECENT_PASSWORD)
        headers = {key.lower(): value for key, value in request.headers.items()}
        try:
            started = await run_in_threadpool(
                lambda: service.start(
                    method=str(params.get("method") or ""),
                    key=str(params.get("key") or ""),
                    next_path=next_path,
                    callback_url=_redirect_uri(),
                    headers=headers,
                    link_user_id=link_user_id,
                    client_address=_client_ip(request),
                )
            )
        except ExternalSignInRefused as exc:
            return _to_login(exc.return_path, exc.public_message)
        except Exception:
            return _to_login(next_path)
        if not isinstance(started, StartResult):
            # A method that does not redirect (the trusted header kind) completed at once.
            return _signed_in(started)
        response = RedirectResponse(started.authorization_url, status_code=303, headers=_NO_STORE)
        # Lax, not Strict: the method brings the browser back with a cross-site GET.
        response.set_cookie(
            key=BINDING_COOKIE,
            value=started.browser_binding,
            httponly=True,
            samesite="lax",
            secure=auth_cookie_secure(),
            max_age=_BINDING_TTL_SECONDS,
            path=_BINDING_COOKIE_PATH,
        )
        return response

    @router.get("/api/auth/external/callback")
    async def external_sign_in_callback(request: Request):
        bl = get_logger_only()
        service = _service(bl, request)
        if service is None:
            return _to_login("/")
        params = {key: str(value) for key, value in request.query_params.items()}
        binding = str(request.cookies.get(BINDING_COOKIE) or "")
        headers = {key.lower(): value for key, value in request.headers.items()}
        try:
            result = await run_in_threadpool(
                lambda: service.callback(
                    params=params,
                    browser_binding=binding,
                    headers=headers,
                    client_address=_client_ip(request),
                )
            )
        except ExternalSignInRefused as exc:
            return _to_login(exc.return_path, exc.public_message)
        except Exception:
            return _to_login("/")
        return _signed_in(result)

    # --- administration -------------------------------------------------------------

    async def _admin(request: Request, action: Callable[[ExternalSignInAdmin, Any], Any]):
        bl = get_logger_only()
        if bl is None:
            return JSONResponse({"error": "Database not configured"}, status_code=503)
        admin = ExternalSignInAdmin(
            connection=lambda: connection_for(bl),
            audit=lambda **kwargs: log_auth_audit_event(bl, **kwargs),
        )
        try:
            scope = request_scope(request)
            return await run_in_threadpool(action, admin, scope)
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    @router.get("/api/settings/external-sign-in/identities")
    async def list_external_identities(request: Request):
        return await _admin(request, lambda admin, scope: admin.list_identities(scope))

    @router.delete("/api/settings/external-sign-in/identities/{identity_id}")
    async def unlink_external_identity(identity_id: str, request: Request):
        return await _admin(request, lambda admin, scope: admin.unlink_identity(scope, identity_id))

    return router


__all__ = ["BINDING_COOKIE", "create_external_sign_in_router"]
