"""System email, sent through an installed email notification extension.

Flow Steward has no mail transport of its own. When an administrator connects
an email notification extension (one whose deliveries address a recipient, such
as an SMTP extension), the platform's own messages -- a password reset link, an
email-change verification, a welcome note -- go out through that connection.
Without one there is no system email: the sign-in page hides "Forgot
password?", and an administrator resets passwords instead.

The connection used is the oldest enabled one, so adding another connection
later does not silently move system mail elsewhere.

Every send returns ``{"ok": bool, "reason": str}``; ``reason`` is ``sent``,
``disabled_or_unconfigured`` (no email connection), ``invalid_recipient`` or
``send_failed``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_FALLBACK_LOCALE = "en"

#: Subject and plain-text body for each system message, per locale.
_MESSAGES: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "password_reset": (
            "Password reset",
            "Hi {username},\n\n"
            "A password reset was requested for your account.\n"
            "This link is valid for {expires_in_minutes} minutes.\n\n"
            "{reset_url}\n\n"
            "If you did not request this, you can safely ignore this email.",
        ),
        "email_change_verification": (
            "Verify your new sign-in email",
            "Hi {display_name},\n\n"
            "We received a request to change your sign-in email to {new_email}.\n\n"
            "Open this link to verify the new email address:\n"
            "{verification_url}\n\n"
            "This link expires in {expires_in_minutes} minutes.\n"
            "Your current sign-in email will keep working until you verify the new address.\n\n"
            "If you did not request this change, you can ignore this email.",
        ),
        "signup_notification": (
            "Welcome — your account has been created",
            "Hi {display_name},\n\n"
            "Your account has been created. You can now sign in using {email}.\n\n"
            "If you did not expect this, please contact your administrator.",
        ),
    },
    "lt": {
        "password_reset": (
            "Slaptažodžio atkūrimas",
            "Sveiki, {username},\n\n"
            "Gauta užklausa atkurti jūsų paskyros slaptažodį.\n"
            "Ši nuoroda galioja {expires_in_minutes} min.\n\n"
            "{reset_url}\n\n"
            "Jei neprašėte atkurti slaptažodžio, galite nepaisyti šio laiško.",
        ),
        "email_change_verification": (
            "Patvirtinkite naują prisijungimo el. pašto adresą",
            "Sveiki, {display_name},\n\n"
            "Gauta užklausa pakeisti jūsų prisijungimo el. pašto adresą į {new_email}.\n\n"
            "Atverkite šią nuorodą, kad patvirtintumėte naują adresą:\n"
            "{verification_url}\n\n"
            "Ši nuoroda galioja {expires_in_minutes} min.\n"
            "Dabartinis prisijungimo adresas veiks, kol patvirtinsite naująjį.\n\n"
            "Jei neprašėte šio pakeitimo, galite nepaisyti šio laiško.",
        ),
        "signup_notification": (
            "Sveiki atvykę — jūsų paskyra sukurta",
            "Sveiki, {display_name},\n\n"
            "Jūsų paskyra sukurta. Dabar galite prisijungti naudodami adresą {email}.\n\n"
            "Jei nesitikėjote šio laiško, susisiekite su administratoriumi.",
        ),
    },
}

_ANONYMOUS_NAME = {"en": "there", "lt": "naudotojau"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _locale(value: Any) -> str:
    primary = _text(value).lower().replace("_", "-").split("-", 1)[0]
    return primary if primary in _MESSAGES else _FALLBACK_LOCALE


def _normalize_email(value: Any) -> str:
    text = _text(value).lower()
    local, _, domain = text.partition("@")
    if not local or not domain or "." not in domain or ".." in text:
        return ""
    return text


def render_system_email(kind: str, *, locale: str = "", **values: Any) -> tuple[str, str]:
    """Return the subject and plain-text body of one system message."""
    normalized_locale = _locale(locale)
    subject, body = _MESSAGES[normalized_locale][kind]
    anonymous = _ANONYMOUS_NAME[normalized_locale]
    filled = {key: _text(value) for key, value in values.items()}
    for name_field in ("username", "display_name"):
        filled[name_field] = filled.get(name_field) or anonymous
    return subject, body.format(**filled)


def _platform_store() -> Any:
    # The store that holds connections, notifications and extension installs:
    # everything finding the connection and delivering through it reads.
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    return PlatformIntegrationStore.get_shared_instance()


def find_email_connection(bl: Any) -> tuple[dict[str, Any], str] | None:
    """The oldest enabled connection of an email notification extension, if any.

    Returns ``(connection, extension_key)``. An email extension is one that is a
    notification provider whose deliveries address a recipient; the platform does
    not know extensions by name.
    """
    from core.application.extensions.external_effects import notification_target_kind

    if bl is None or not hasattr(bl, "list_enabled_extension_connections"):
        return None
    extensions: dict[str, dict[str, Any] | None] = {}
    for connection in bl.list_enabled_extension_connections() or []:
        extension_key = _text(connection.get("extension_id"))
        if not extension_key:
            continue
        if extension_key not in extensions:
            try:
                extensions[extension_key] = bl.get_extension(extension_key)
            except Exception:
                extensions[extension_key] = None
        extension = extensions[extension_key] or {}
        if (
            bool(extension.get("enabled"))
            and _text(extension.get("kind")) == "notification_provider"
            and notification_target_kind(extension) == "recipient"
        ):
            return dict(connection), extension_key
    return None


_AVAILABILITY_TTL_SECONDS = 30.0
_availability_lock = threading.Lock()
_availability_cache: dict[str, float | bool] = {}


def system_email_available(bl: Any = None, *, clock: Any = time.monotonic) -> bool:
    """Whether the platform can send email: an email extension is connected.

    Asked on every app load (the sign-in page shows "Forgot password?" only
    when it is true), so the answer is kept for a short while.
    """
    now = clock()
    with _availability_lock:
        checked_at = _availability_cache.get("at")
        if isinstance(checked_at, float) and now - checked_at < _AVAILABILITY_TTL_SECONDS:
            return bool(_availability_cache.get("available"))
    try:
        available = find_email_connection(bl if bl is not None else _platform_store()) is not None
    except Exception as exc:
        logger.debug("system email availability check failed: %s", exc)
        available = False
    with _availability_lock:
        _availability_cache.update({"at": float(now), "available": available})
    return available


def _send(kind: str, *, recipient: str, locale: str, bl: Any = None, **values: Any) -> dict:
    normalized_recipient = _normalize_email(recipient)
    if not normalized_recipient:
        return {"ok": False, "reason": "invalid_recipient"}
    store = bl if bl is not None else _platform_store()
    found = find_email_connection(store)
    if found is None:
        return {"ok": False, "reason": "disabled_or_unconfigured"}
    connection, extension_key = found
    subject, body = render_system_email(kind, locale=locale, **values)
    from core.application.notifications.delivery import deliver_direct_notification

    result = deliver_direct_notification(
        store,
        connection=connection,
        extension_key=extension_key,
        event_type=f"system.{kind}",
        recipients=[normalized_recipient],
        subject=subject,
        message=body,
    )
    if _text(result.get("status")) == "delivered":
        return {"ok": True, "reason": "sent", "provider": extension_key}
    logger.warning(
        "System email %s was not delivered through %s: %s",
        kind,
        extension_key,
        _text(result.get("error_text")) or "delivery failed",
    )
    return {"ok": False, "reason": "send_failed", "provider": extension_key}


def send_password_reset_email(
    *,
    recipient: str,
    reset_url: str,
    expires_in_minutes: int,
    username: str = "",
    locale: str = "",
    bl: Any = None,
) -> dict[str, Any]:
    return _send(
        "password_reset",
        recipient=recipient,
        locale=locale,
        bl=bl,
        username=username,
        reset_url=reset_url,
        expires_in_minutes=max(1, int(expires_in_minutes or 1)),
    )


def send_email_change_verification_email(
    *,
    recipient: str,
    new_email: str,
    verification_url: str,
    expires_in_minutes: int,
    display_name: str = "",
    locale: str = "",
    bl: Any = None,
) -> dict[str, Any]:
    """*recipient* is the new address the user wants to verify."""
    return _send(
        "email_change_verification",
        recipient=recipient,
        locale=locale,
        bl=bl,
        display_name=display_name,
        new_email=new_email,
        verification_url=verification_url,
        expires_in_minutes=max(1, int(expires_in_minutes or 1)),
    )


def send_signup_notification_email(
    *,
    recipient: str,
    display_name: str = "",
    locale: str = "",
    bl: Any = None,
) -> dict[str, Any]:
    """Welcome a newly created user. Skipped quietly when there is no email."""
    return _send(
        "signup_notification",
        recipient=recipient,
        locale=locale,
        bl=bl,
        display_name=display_name,
        email=_normalize_email(recipient),
    )


__all__ = [
    "find_email_connection",
    "render_system_email",
    "send_email_change_verification_email",
    "send_password_reset_email",
    "send_signup_notification_email",
    "system_email_available",
]
