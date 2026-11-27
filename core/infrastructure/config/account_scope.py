"""Explicit account scope for background units of work.

A job, event, message or notification carries its account from the moment it
is created. A consumer that receives one without an account refuses it: it
logs the refusal and fails that unit of work. It never runs it under the
default account, because on an installation with several accounts that would
hand one account's work to another.

Rows in the database always carry ``account_id`` (the columns are
``NOT NULL``). Only messages that were already in flight when this rule
arrived can lack it. :func:`resolve_unit_account_id` accepts such a message
only when the installation has exactly one active account, which is the only
case where the owner is not in doubt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

ACCOUNT_SCOPE_MISSING_CODE = "account_scope_missing"


class MissingAccountScopeError(ValueError):
    """A unit of work arrived without the account it belongs to.

    A ``ValueError`` so existing handlers of invalid input fail the unit of work.
    """

    code = ACCOUNT_SCOPE_MISSING_CODE

    def __init__(self, unit: str, detail: str = "") -> None:
        self.unit = str(unit or "").strip() or "unit of work"
        self.detail = str(detail or "").strip()
        message = f"{self.unit} has no account; refusing to run it under the default account"
        if self.detail:
            message = f"{message} ({self.detail})"
        super().__init__(message)


def account_id_text(value: Any) -> str:
    """Return the account id as trimmed text, or an empty string."""
    return str(value or "").strip()


def require_account_id(value: Any, *, unit: str, detail: str = "") -> str:
    """Return the account id, or log and raise :class:`MissingAccountScopeError`."""
    account_id = account_id_text(value)
    if account_id:
        return account_id
    error = MissingAccountScopeError(unit, detail)
    logger.warning("%s: %s", ACCOUNT_SCOPE_MISSING_CODE, error)
    raise error


def sole_account_id(account_ids: Iterable[Any]) -> str:
    """Return the one account id when exactly one is given, else an empty string."""
    distinct = {account_id_text(item) for item in account_ids or ()}
    distinct.discard("")
    if len(distinct) == 1:
        return next(iter(distinct))
    return ""


def active_account_ids_from_store(store: Any) -> Callable[[], list[str]] | None:
    """Return a lookup of up to two active account ids through ``list_accounts``.

    Two are enough to tell "exactly one" from "several". ``None`` when the
    store cannot list accounts, so the caller refuses instead of guessing.
    """
    list_accounts = getattr(store, "list_accounts", None)
    if not callable(list_accounts):
        return None

    def lookup() -> list[str]:
        rows = list_accounts(include_inactive=False, limit=2) or []
        return [
            account_id_text(row.get("account_id") or row.get("id"))
            for row in rows
            if isinstance(row, dict)
        ]

    return lookup


def resolve_unit_account_id(
    value: Any,
    *,
    unit: str,
    list_active_account_ids: Callable[[], Iterable[Any]] | None = None,
) -> str:
    """Return the account a unit of work carries, with the in-flight rule.

    A unit without an account is accepted only when ``list_active_account_ids``
    reports exactly one active account; it then runs under that account and a
    warning records the legacy message. Otherwise it is refused.
    """
    account_id = account_id_text(value)
    if account_id:
        return account_id
    if list_active_account_ids is not None:
        try:
            sole = sole_account_id(list_active_account_ids())
        except Exception as exc:
            logger.warning("%s: active account lookup failed: %s", ACCOUNT_SCOPE_MISSING_CODE, exc)
            sole = ""
        if sole:
            logger.warning(
                "%s: %s has no account; accepted under the only active account %s",
                ACCOUNT_SCOPE_MISSING_CODE,
                unit,
                sole,
            )
            return sole
    return require_account_id(
        "",
        unit=unit,
        detail="the installation does not have exactly one active account",
    )


__all__ = [
    "ACCOUNT_SCOPE_MISSING_CODE",
    "MissingAccountScopeError",
    "account_id_text",
    "active_account_ids_from_store",
    "require_account_id",
    "resolve_unit_account_id",
    "sole_account_id",
]
