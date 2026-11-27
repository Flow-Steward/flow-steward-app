"""How many accounts an installation may hold (ADR 0082, numeric limit ``accounts``).

The limit is installation-wide. Core reads it from the entitlement service as
``limit(<account of the person creating>, "accounts")``. Only an installation
administrator may create accounts, so that account is always the installation
account (:data:`INSTALLATION_ACCOUNT_ID`): a source that wants to raise the limit
answers ``limits_for(<installation account>)`` with ``{"accounts": N}``.

- No source defines the limit (``None``, for example no source is installed or
  its grant has expired): the instance default, :data:`DEFAULT_ACCOUNT_LIMIT`,
  is one account, the installation account itself. That is the single-account
  boundary of ADR 0020; Core alone never opens a second account.
- A source defines it: at most ``N`` **active** accounts, the installation account
  included. A disabled account does not count, so disabling one frees its place.
  A new account is refused while ``N`` accounts are active, whether it would be
  created active or disabled, so that no later enable step can pass the limit.
- The limit or the active count cannot be read (the service is missing or raises,
  the count fails): creation is refused. A failing source makes the service answer
  ``0``, which refuses too.

The check is pure; the caller supplies the entitlement service and a function that
counts the active accounts. Enabling a disabled account passes the same check.
:mod:`core.application.auth.account_lifecycle` runs it and the write under one
account lifecycle lock, so two requests cannot both take the last place.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID

_LOG = logging.getLogger(__name__)

#: The entitlement limit name.
ACCOUNTS_LIMIT_NAME = "accounts"
#: The limit when no entitlement source defines one: the installation account only.
DEFAULT_ACCOUNT_LIMIT = 1
ACCOUNT_LIMIT_REACHED_CODE = "account_limit_reached"
ACCOUNT_LIMIT_UNAVAILABLE_CODE = "account_limit_unavailable"


class AccountLimitError(Exception):
    """Account creation is refused because of the account limit."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        limit: int | None = None,
        active: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.limit = limit
        self.active = active

    def payload(self) -> dict[str, Any]:
        details: dict[str, Any] = {"limit_name": ACCOUNTS_LIMIT_NAME}
        if self.limit is not None:
            details["limit"] = self.limit
        if self.active is not None:
            details["active_accounts"] = self.active
        return {"error": self.message, "code": self.code, "details": details}


def account_limit_unavailable(reason: str) -> AccountLimitError:
    """The refusal when the limit cannot be checked: nothing is created or enabled."""
    return AccountLimitError(
        f"The account limit cannot be checked ({reason}), so no account was created or enabled",
        code=ACCOUNT_LIMIT_UNAVAILABLE_CODE,
        status_code=503,
    )


def effective_account_limit(
    entitlement_service: Any, account_id: str = INSTALLATION_ACCOUNT_ID
) -> int:
    """The account limit seen from ``account_id``; raises :class:`AccountLimitError` when unreadable.

    ``None`` from the service is the instance default, never "unlimited".
    """
    read_limit = getattr(entitlement_service, "limit", None)
    if not callable(read_limit):
        raise account_limit_unavailable("no entitlement service")
    try:
        value = read_limit(account_id or INSTALLATION_ACCOUNT_ID, ACCOUNTS_LIMIT_NAME)
    except Exception as exc:
        _LOG.warning("reading the account limit failed (%s); refusing", type(exc).__name__)
        raise account_limit_unavailable("the entitlement service failed") from exc
    if value is None:
        return DEFAULT_ACCOUNT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise account_limit_unavailable("the limit is not a number")
    return value


def require_account_creation_allowed(
    entitlement_service: Any,
    count_active_accounts: Callable[[], int],
    *,
    account_id: str = INSTALLATION_ACCOUNT_ID,
) -> int:
    """Refuse a new account when the active accounts already fill the limit.

    Returns the limit that allowed the creation. Raises :class:`AccountLimitError`
    with status 409 when the limit is reached and 503 when it cannot be checked.
    """
    limit = effective_account_limit(entitlement_service, account_id)
    try:
        active = count_active_accounts()
    except Exception as exc:
        _LOG.warning("counting the active accounts failed (%s); refusing", type(exc).__name__)
        raise account_limit_unavailable("the accounts cannot be counted") from exc
    if isinstance(active, bool) or not isinstance(active, int) or active < 0:
        raise account_limit_unavailable("the accounts cannot be counted")
    if active >= limit:
        noun = "account" if limit == 1 else "accounts"
        raise AccountLimitError(
            f"Account limit reached: this installation allows {limit} active {noun} "
            f"and {active} are active. Disable an account or raise the limit before "
            "creating or enabling another.",
            code=ACCOUNT_LIMIT_REACHED_CODE,
            status_code=409,
            limit=limit,
            active=active,
        )
    return limit


__all__ = [
    "ACCOUNTS_LIMIT_NAME",
    "ACCOUNT_LIMIT_REACHED_CODE",
    "ACCOUNT_LIMIT_UNAVAILABLE_CODE",
    "DEFAULT_ACCOUNT_LIMIT",
    "AccountLimitError",
    "account_limit_unavailable",
    "effective_account_limit",
    "require_account_creation_allowed",
]
