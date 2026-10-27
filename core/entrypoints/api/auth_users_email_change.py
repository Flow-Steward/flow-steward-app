from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import Request

from core.application.auth import user_profile_repository
from core.application.auth.serve_auth_runtime import public_base_url
from core.application.extensions.system_email import send_email_change_verification_email
from core.application.settings.ui_i18n import resolve_ui_locale


def auth_email_change_tables_available(bl: Any) -> bool:
    return user_profile_repository.auth_email_change_tables_available(bl)


def build_email_change_verification_url(
    request: Request,
    verification_token: str,
    *,
    public_base_url_fn: Callable[[], str | None] = public_base_url,
) -> str:
    base_url = public_base_url_fn() or str(request.base_url or "").strip().rstrip("/")
    if not base_url:
        return f"/verify-email-change?token={quote(str(verification_token or ''))}"
    return f"{base_url}/verify-email-change?token={quote(str(verification_token or ''))}"


def dispatch_email_change_verification_email(
    request: Request,
    *,
    user: dict[str, Any],
    new_email: str,
    verification_token: str,
    expires_in_minutes: int,
    public_base_url_fn: Callable[[], str | None] = public_base_url,
    send_email_change_verification_email_fn: Callable[
        ..., dict[str, Any]
    ] = send_email_change_verification_email,
) -> dict[str, Any]:
    verification_url = build_email_change_verification_url(
        request,
        verification_token,
        public_base_url_fn=public_base_url_fn,
    )
    display_name = str(user.get("display_name") or user.get("username") or "").strip()
    locale = resolve_ui_locale(request, auth_user=user)
    result = send_email_change_verification_email_fn(
        recipient=new_email,
        new_email=new_email,
        verification_url=verification_url,
        expires_in_minutes=int(max(1, expires_in_minutes)),
        display_name=display_name,
        locale=locale,
    )
    payload = dict(result or {})
    payload["verification_url"] = verification_url
    return payload
