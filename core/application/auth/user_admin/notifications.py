"""Non-blocking notification policy for user administration."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

LOGGER = logging.getLogger(__name__)
SIGNUP_DELIVERY_WARNING = (
    "User created, but the welcome email was not delivered. Check the email extension's connection."
)
#: No email extension is connected: there is no welcome email to miss.
_NO_EMAIL_REASON = "disabled_or_unconfigured"


def notify_signup_non_blocking(
    notifier: Callable[..., Any] | None,
    *,
    user_id: str,
    recipient: str,
    display_name: str,
    locale: str,
) -> str | None:
    if notifier is None:
        return None
    try:
        result = notifier(
            recipient=recipient,
            display_name=display_name,
            locale=locale,
        )
    except Exception:
        LOGGER.warning("Signup notification delivery failed for user_id=%s", user_id)
        return SIGNUP_DELIVERY_WARNING
    if isinstance(result, Mapping) and result.get("ok") is False:
        if result.get("reason") == _NO_EMAIL_REASON:
            return None
        LOGGER.warning("Signup notification delivery failed for user_id=%s", user_id)
        return SIGNUP_DELIVERY_WARNING
    return None
