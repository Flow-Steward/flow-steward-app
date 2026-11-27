"""Creating, enabling, disabling and listing accounts (ADR 0082 section 8).

Accounts are installation-wide: only an installation administrator
(:mod:`core.application.auth.installation_admin`) manages them. Creating an
account and enabling a disabled one both take a place under the ``accounts``
limit (:mod:`core.application.auth.account_limit`).

Counting the active accounts and writing the new or enabled one is a single
decision. Each write runs inside the store's ``account_lifecycle_transaction()``:
one database transaction that holds an advisory lock until it commits, so two
requests racing for the last place are serialized and the second one sees the
first one's account. A store without that transaction cannot serialize the
decision, so the write is refused as if the limit could not be read.

The same functions serve Core's account routes and :class:`AccountsPort`, the
port platform modules receive as ``container.accounts``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from core.application.auth.account_limit import (
    account_limit_unavailable,
    require_account_creation_allowed,
)
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.external_session_revocation import (
    revoke_external_sign_in_grants_of_account,
)
from core.application.auth.installation_admin import (
    INSTALLATION_ACCOUNT_ID,
    INSTALLATION_ADMIN_REQUIRED_MESSAGE,
    acting_account_id,
    is_installation_admin,
)

#: Page size used to count the active accounts.
ACCOUNT_COUNT_PAGE = 2000


class InstallationAdminRequiredError(PermissionError):
    """The caller is not an installation administrator."""


class AccountNotFoundError(LookupError):
    """No account has this id."""


class AccountAlreadyExistsError(ValueError):
    """An account with this id already exists."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def count_active_accounts(store: Any) -> int:
    """How many active accounts exist; raises when they cannot be listed."""
    list_accounts = getattr(store, "list_accounts", None)
    if not callable(list_accounts):
        raise RuntimeError("accounts cannot be listed")
    total = 0
    offset = 0
    while True:
        page = list_accounts(
            include_inactive=False, search="", limit=ACCOUNT_COUNT_PAGE, offset=offset
        )
        if not isinstance(page, list):
            raise RuntimeError("accounts cannot be listed")
        total += len(page)
        if len(page) < ACCOUNT_COUNT_PAGE:
            return total
        offset += ACCOUNT_COUNT_PAGE


def _lifecycle(store: Any) -> AbstractContextManager[Any]:
    begin = getattr(store, "account_lifecycle_transaction", None)
    if not callable(begin):
        raise account_limit_unavailable("account changes cannot be serialized")
    return begin()


def _require_place(store: Any, entitlement_service: Any, acting_account: str) -> None:
    require_account_creation_allowed(
        entitlement_service,
        lambda: count_active_accounts(store),
        account_id=acting_account or INSTALLATION_ACCOUNT_ID,
    )


#: Called inside the lifecycle transaction with the written account, e.g. to audit it.
OnWritten = Callable[[dict[str, Any]], None]


def create_account_within_limit(
    store: Any,
    entitlement_service: Any,
    *,
    account_id: str,
    display_name: str = "",
    is_active: bool = True,
    acting_account: str = INSTALLATION_ACCOUNT_ID,
    on_written: OnWritten | None = None,
) -> dict[str, Any] | None:
    """Create an account if the limit leaves a place; count and insert are one decision.

    Raises :class:`~core.application.auth.account_limit.AccountLimitError` (409 at the
    limit, 503 when it cannot be checked), :class:`AccountAlreadyExistsError`, or
    ``ValueError`` for an invalid id. ``None`` means the store has no accounts table.
    """
    with _lifecycle(store):
        _require_place(store, entitlement_service, acting_account)
        try:
            account = store.create_account(
                account_id=account_id,
                display_name=display_name,
                is_active=bool(is_active),
            )
        except ValueError as exc:
            if "already exists" in str(exc).lower():
                raise AccountAlreadyExistsError(str(exc)) from exc
            raise
        if account and on_written is not None:
            on_written(dict(account))
        return account


def enable_account_within_limit(
    store: Any,
    entitlement_service: Any,
    account_id: str,
    *,
    acting_account: str = INSTALLATION_ACCOUNT_ID,
    on_written: OnWritten | None = None,
) -> dict[str, Any] | None:
    """Enable a disabled account if the limit leaves a place.

    An account that is already active is returned unchanged, without a limit check
    and without ``on_written``. ``None`` means no such account.
    """
    with _lifecycle(store):
        current = store.get_account(account_id)
        if not current:
            return None
        if bool(dict(current).get("is_active")):
            return dict(current)
        _require_place(store, entitlement_service, acting_account)
        account = store.set_account_active(account_id, is_active=True)
        if account and on_written is not None:
            on_written(dict(account))
        return account


def disable_account(
    store: Any, account_id: str, *, on_written: OnWritten | None = None
) -> dict[str, Any] | None:
    """Disable an account, freeing its place; ``None`` means no such account.

    The installation account cannot be disabled (``ValueError`` from the store).
    """
    with _lifecycle(store):
        account = store.set_account_active(account_id, is_active=False)
        if account:
            _revoke_external_sign_in(store, dict(account).get("account_id") or account_id)
        if account and on_written is not None:
            on_written(dict(account))
        return account


def _revoke_external_sign_in(store: Any, account_id: Any) -> None:
    """A disabled account's external sessions and their people's tokens end with it
    (ADR 0083), in the lifecycle transaction. A store without a connection has none."""
    conn = getattr(store, "conn", None)
    if conn is None:
        return
    with conn.cursor() as cur:
        revoke_external_sign_in_grants_of_account(cur, account_id)


def rename_account(
    store: Any, account_id: str, *, display_name: str, on_written: OnWritten | None = None
) -> dict[str, Any] | None:
    """Change an account's display name; ``None`` means no such account."""
    with _lifecycle(store):
        account = store.update_account(account_id, display_name=display_name)
        if account and on_written is not None:
            on_written(dict(account))
        return account


def _user_counts(store: Any) -> dict[str, int] | None:
    count = getattr(store, "count_users_by_account", None)
    if not callable(count):
        return None
    return {str(key): int(value) for key, value in (count() or {}).items()}


StoreFactory = Callable[[], Any]


class AccountsPort:
    """Core's accounts port for platform modules (``container.accounts``, ADR 0082).

    Accounts are installation-wide, so every call takes the module route's
    authenticated principal as the keyword ``caller`` and refuses, with
    :class:`InstallationAdminRequiredError` (a ``PermissionError``; the module
    answers 403), anyone who is not an installation administrator, whatever rule
    guards the module route.

    Writes go through the same functions as Core's account routes, so the
    ``accounts`` limit and the lifecycle lock are the same. The port writes the
    audit event itself, inside the same transaction as the change (category
    ``auth``; actions ``account_create``, ``account_update``, ``account_disable``,
    ``account_enable``); ``created_by`` or ``actor`` names who made it. A module
    does not write a second one.

    Refusals: :class:`~core.application.auth.account_limit.AccountLimitError`
    (``status_code`` 409 or 503, ``payload()`` for the body); ``LookupError``
    (:class:`AccountNotFoundError`) for an unknown account; ``ValueError`` for an
    invalid id or name, an empty ``created_by`` or ``actor``, disabling the
    installation account, and a duplicate id (:class:`AccountAlreadyExistsError`,
    whose message says "already exists").
    """

    def __init__(self, store: StoreFactory, entitlement_service: Callable[[], Any]) -> None:
        self._store = store
        self._entitlement_service = entitlement_service

    @staticmethod
    def _require_installation_admin(caller: Any) -> None:
        if not is_installation_admin(caller):
            raise InstallationAdminRequiredError(INSTALLATION_ADMIN_REQUIRED_MESSAGE)

    @staticmethod
    def _actor(actor: Any, name: str) -> str:
        text = _text(actor)
        if not text:
            raise ValueError(f"{name} is required")
        return text

    @staticmethod
    def _account_id(account_id: Any) -> str:
        text = _text(account_id)
        if not text:
            raise ValueError("account_id is required")
        return text

    def _store_or_fail(self) -> Any:
        store = self._store()
        if store is None:
            raise RuntimeError("the account store is not available")
        return store

    @staticmethod
    def _audit(store: Any, action: str, account_id: str, actor: str, **detail: Any) -> OnWritten:
        def write(account: dict[str, Any]) -> None:
            log_auth_audit_event(
                store,
                action=action,
                username=account_id,
                actor=actor,
                detail={"account_id": _text(account.get("account_id")) or account_id, **detail},
            )

        return write

    def list_accounts(self, *, include_inactive: bool = True, caller: Any) -> list[dict[str, Any]]:
        """Every account: ``account_id``, ``display_name``, ``is_active``, ``user_count``,
        ``created_at``, ``updated_at``. ``user_count`` is ``None`` when it cannot be read.
        """
        self._require_installation_admin(caller)
        store = self._store_or_fail()
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = store.list_accounts(
                include_inactive=bool(include_inactive),
                search="",
                limit=ACCOUNT_COUNT_PAGE,
                offset=offset,
            )
            page = [dict(row) for row in page or () if isinstance(row, Mapping)]
            rows.extend(page)
            if len(page) < ACCOUNT_COUNT_PAGE:
                break
            offset += ACCOUNT_COUNT_PAGE
        counts = _user_counts(store)
        for row in rows:
            row["user_count"] = (
                None if counts is None else counts.get(_text(row.get("account_id")), 0)
            )
        return rows

    def create_account(
        self,
        account_id: str,
        /,
        *,
        display_name: str = "",
        is_active: bool = True,
        created_by: str,
        caller: Any,
    ) -> dict[str, Any]:
        """Create an account within the ``accounts`` limit."""
        self._require_installation_admin(caller)
        actor = self._actor(created_by, "created_by")
        normalized = self._account_id(account_id)
        store = self._store_or_fail()
        account = create_account_within_limit(
            store,
            self._entitlement_service(),
            account_id=normalized,
            display_name=_text(display_name),
            is_active=bool(is_active),
            acting_account=acting_account_id(caller),
            on_written=self._audit(
                store, "account_create", normalized, actor, is_active=bool(is_active)
            ),
        )
        if not account:
            raise RuntimeError("the account could not be created")
        return dict(account)

    def rename_account(
        self, account_id: str, /, *, display_name: str, actor: str, caller: Any
    ) -> dict[str, Any]:
        """Change an account's display name."""
        self._require_installation_admin(caller)
        who = self._actor(actor, "actor")
        normalized = self._account_id(account_id)
        name = _text(display_name)
        if not name:
            raise ValueError("display_name is required")
        store = self._store_or_fail()
        account = rename_account(
            store,
            normalized,
            display_name=name,
            on_written=self._audit(store, "account_update", normalized, who, display_name=name),
        )
        if not account:
            raise AccountNotFoundError(f"Account {normalized!r} was not found")
        return dict(account)

    def enable_account(self, account_id: str, /, *, actor: str, caller: Any) -> dict[str, Any]:
        """Enable a disabled account within the ``accounts`` limit; no-op when active."""
        self._require_installation_admin(caller)
        who = self._actor(actor, "actor")
        normalized = self._account_id(account_id)
        store = self._store_or_fail()
        account = enable_account_within_limit(
            store,
            self._entitlement_service(),
            normalized,
            acting_account=acting_account_id(caller),
            on_written=self._audit(store, "account_enable", normalized, who),
        )
        if not account:
            raise AccountNotFoundError(f"Account {normalized!r} was not found")
        return dict(account)

    def disable_account(
        self, account_id: str, /, *, actor: str, reason: str = "", caller: Any
    ) -> dict[str, Any]:
        """Disable an account; its place under the limit is freed."""
        self._require_installation_admin(caller)
        who = self._actor(actor, "actor")
        normalized = self._account_id(account_id)
        store = self._store_or_fail()
        account = disable_account(
            store,
            normalized,
            on_written=self._audit(store, "account_disable", normalized, who, reason=_text(reason)),
        )
        if not account:
            raise AccountNotFoundError(f"Account {normalized!r} was not found")
        return dict(account)


__all__ = [
    "ACCOUNT_COUNT_PAGE",
    "AccountAlreadyExistsError",
    "AccountNotFoundError",
    "AccountsPort",
    "InstallationAdminRequiredError",
    "count_active_accounts",
    "create_account_within_limit",
    "disable_account",
    "enable_account_within_limit",
    "rename_account",
]
