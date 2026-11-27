"""Account lifecycle under the ``accounts`` limit, and Core's accounts port (ADR 0082 s. 8).

Counting the active accounts and writing one is a single decision held under the
store's account lifecycle lock, so two requests racing for the last place cannot
both take it. Enabling a disabled account takes a place too. Platform modules get
the same calls through ``container.accounts``, refused for anyone but the
installation administrator.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from typing import Any

import pytest

from core.application.auth.account_lifecycle import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    AccountsPort,
    InstallationAdminRequiredError,
    create_account_within_limit,
    disable_account,
    enable_account_within_limit,
)
from core.application.auth.account_limit import (
    ACCOUNT_LIMIT_REACHED_CODE,
    ACCOUNT_LIMIT_UNAVAILABLE_CODE,
    AccountLimitError,
)
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.infrastructure.db.logger_extensions_accounts_accounts_mixin import (
    ACCOUNT_LIFECYCLE_LOCK_KEY,
    PlatformLoggerAccountsMixin,
)

OTHER_ACCOUNT = "acme.second-account"
INSTALLATION_ADMIN: dict[str, Any] = {
    "id": 1,
    "username": "root-admin",
    "roles": ["admin"],
    "account_id": DEFAULT_ACCOUNT_ID,
}
ACCOUNT_ADMIN: dict[str, Any] = {
    "id": 2,
    "username": "acme-admin",
    "roles": ["admin"],
    "account_id": OTHER_ACCOUNT,
}
INSTALLATION_OPERATOR: dict[str, Any] = {
    "id": 3,
    "username": "operator",
    "roles": ["operator"],
    "account_id": DEFAULT_ACCOUNT_ID,
}


class _Limit:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def limit(self, _account_id: str, _name: str) -> int | None:
        return self.value


class _Store:
    """An account store whose lifecycle transaction is a process lock, as in Postgres."""

    def __init__(self, active: int = 1, inactive: int = 0, *, locked: bool = True) -> None:
        self.rows: dict[str, dict[str, Any]] = {DEFAULT_ACCOUNT_ID: _row(DEFAULT_ACCOUNT_ID, True)}
        for index in range(1, active):
            self.rows[f"acme.active-{index}"] = _row(f"acme.active-{index}", True)
        for index in range(inactive):
            self.rows[f"acme.parked-{index}"] = _row(f"acme.parked-{index}", False)
        self.locked = locked
        self._lock = threading.Lock()
        self.transactions = 0
        self.audit: list[dict[str, Any]] = []
        #: Set by a test to make a count wait for a second request (see the race test).
        self.count_barrier: threading.Barrier | None = None

    def account_lifecycle_transaction(self) -> AbstractContextManager[None]:
        self.transactions += 1
        return self._lock if self.locked else nullcontext()

    def list_accounts(
        self, *, include_inactive: bool = True, limit: int = 200, offset: int = 0, **_: Any
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.rows.values() if include_inactive or row["is_active"]]
        if self.count_barrier is not None:
            # Hold the count open until the other request also counts. Under the
            # lock the other request cannot get here, so the wait times out.
            with suppress(threading.BrokenBarrierError):
                self.count_barrier.wait()
        return rows[offset : offset + limit]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        row = self.rows.get(account_id)
        return dict(row) if row else None

    def create_account(
        self, *, account_id: str, display_name: str = "", is_active: bool = True
    ) -> dict[str, Any]:
        if account_id in self.rows:
            raise ValueError(f"Account '{account_id}' already exists")
        self.rows[account_id] = _row(account_id, is_active)
        return dict(self.rows[account_id])

    def set_account_active(self, account_id: str, *, is_active: bool) -> dict[str, Any] | None:
        if account_id == DEFAULT_ACCOUNT_ID and not is_active:
            raise ValueError("Default account cannot be disabled")
        row = self.rows.get(account_id)
        if row is None:
            return None
        row["is_active"] = bool(is_active)
        return dict(row)

    def log_action(self, **kwargs: Any) -> None:
        self.audit.append(kwargs)

    def active(self) -> int:
        return sum(1 for row in self.rows.values() if row["is_active"])


def _row(account_id: str, is_active: bool) -> dict[str, Any]:
    return {"account_id": account_id, "display_name": account_id, "is_active": is_active}


def _race(store: _Store, limit: int) -> list[Any]:
    """Two concurrent creates for the last place; each result is an account or an error."""
    store.count_barrier = threading.Barrier(2, timeout=0.5)
    results: list[Any] = [None, None]

    def create(index: int) -> None:
        try:
            results[index] = create_account_within_limit(
                store, _Limit(limit), account_id=f"acme.racer-{index}"
            )
        except AccountLimitError as exc:
            results[index] = exc

    threads = [threading.Thread(target=create, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    return results


# --- the race on the last place --------------------------------------------------------


def test_two_concurrent_creates_cannot_both_take_the_last_place() -> None:
    store = _Store(active=1)

    results = _race(store, limit=2)

    created = [result for result in results if isinstance(result, dict)]
    refused = [result for result in results if isinstance(result, AccountLimitError)]
    assert len(created) == 1
    assert len(refused) == 1
    assert refused[0].code == ACCOUNT_LIMIT_REACHED_CODE
    assert refused[0].active == 2
    assert store.active() == 2


def test_without_the_lock_the_same_race_passes_the_limit() -> None:
    # The control: the race is real, so the lock is what holds the limit.
    store = _Store(active=1, locked=False)

    results = _race(store, limit=2)

    assert all(isinstance(result, dict) for result in results)
    assert store.active() == 3


def test_a_store_that_cannot_serialize_refuses_creation() -> None:
    class Unlocked(_Store):
        account_lifecycle_transaction = None  # type: ignore[assignment]

    store = Unlocked(active=1)
    with pytest.raises(AccountLimitError) as refused:
        create_account_within_limit(store, _Limit(5), account_id="acme.new")
    assert refused.value.code == ACCOUNT_LIMIT_UNAVAILABLE_CODE
    assert "acme.new" not in store.rows


def test_create_counts_and_inserts_in_one_lifecycle_transaction() -> None:
    store = _Store(active=1)

    account = create_account_within_limit(store, _Limit(2), account_id="acme.new")

    assert account is not None
    assert account["account_id"] == "acme.new"
    assert store.transactions == 1


def test_a_duplicate_id_is_reported_as_already_existing() -> None:
    store = _Store(active=2)
    with pytest.raises(AccountAlreadyExistsError):
        create_account_within_limit(store, _Limit(5), account_id=DEFAULT_ACCOUNT_ID)


# --- enabling and disabling ------------------------------------------------------------


def test_enabling_a_disabled_account_respects_the_limit() -> None:
    store = _Store(active=2, inactive=1)

    with pytest.raises(AccountLimitError) as refused:
        enable_account_within_limit(store, _Limit(2), "acme.parked-0")

    assert refused.value.code == ACCOUNT_LIMIT_REACHED_CODE
    assert "enabling" in refused.value.message
    assert store.rows["acme.parked-0"]["is_active"] is False


def test_enabling_below_the_limit_enables_under_the_lock() -> None:
    store = _Store(active=1, inactive=1)

    account = enable_account_within_limit(store, _Limit(2), "acme.parked-0")

    assert account is not None
    assert account["is_active"] is True
    assert store.transactions == 1


def test_enabling_an_active_account_needs_no_place() -> None:
    store = _Store(active=2)

    account = enable_account_within_limit(store, None, "acme.active-1")

    assert account is not None
    assert account["is_active"] is True


def test_enabling_an_unknown_account_returns_none() -> None:
    assert enable_account_within_limit(_Store(), _Limit(5), "acme.missing") is None


def test_disabling_frees_a_place_and_runs_under_the_lock() -> None:
    store = _Store(active=2)

    account = disable_account(store, "acme.active-1")

    assert account is not None
    assert account["is_active"] is False
    assert store.transactions == 1
    assert create_account_within_limit(store, _Limit(2), account_id="acme.next") is not None


# --- the store's lifecycle transaction --------------------------------------------------


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.events.append(("execute", sql, params))


class _Conn:
    def __init__(self) -> None:
        self.autocommit = True
        self.events: list[tuple[Any, ...]] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.events.append(("commit",))

    def rollback(self) -> None:
        self.events.append(("rollback",))


class _MixinStore(PlatformLoggerAccountsMixin):
    def __init__(self) -> None:
        self.conn = _Conn()


def test_the_store_takes_the_advisory_lock_inside_one_transaction() -> None:
    store = _MixinStore()

    with store.account_lifecycle_transaction() as conn:
        assert conn is store.conn
        assert conn.autocommit is False
        conn.events.append(("work",))

    lock = ("execute", "SELECT pg_advisory_xact_lock(hashtext(%s))", (ACCOUNT_LIFECYCLE_LOCK_KEY,))
    assert store.conn.events == [lock, ("work",), ("commit",)]
    assert store.conn.autocommit is True


def test_the_store_rolls_back_a_refused_decision() -> None:
    store = _MixinStore()

    with pytest.raises(AccountLimitError), store.account_lifecycle_transaction():
        raise AccountLimitError("full", code=ACCOUNT_LIMIT_REACHED_CODE, status_code=409)

    assert store.conn.events[-1] == ("rollback",)
    assert ("commit",) not in store.conn.events


# --- the port ----------------------------------------------------------------------------

ADMIN = INSTALLATION_ADMIN


def _port(store: _Store, limit: Any = None) -> AccountsPort:
    return AccountsPort(store=lambda: store, entitlement_service=lambda: limit)


class _AuditingStore(_Store):
    """Records whether each audit event was written inside the lifecycle transaction."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.in_transaction = False
        self.audit_in_transaction: list[bool] = []

    @contextmanager
    def account_lifecycle_transaction(self) -> Iterator[None]:
        self.transactions += 1
        with self._lock:
            self.in_transaction = True
            try:
                yield
            finally:
                self.in_transaction = False

    def log_action(self, **kwargs: Any) -> None:
        self.audit_in_transaction.append(self.in_transaction)
        super().log_action(**kwargs)

    def update_account(self, account_id: str, *, display_name: str) -> dict[str, Any] | None:
        row = self.rows.get(account_id)
        if row is None:
            return None
        row["display_name"] = display_name
        return dict(row)

    def count_users_by_account(self) -> dict[str, int]:
        return {DEFAULT_ACCOUNT_ID: 3}


@pytest.mark.parametrize("caller", [ACCOUNT_ADMIN, INSTALLATION_OPERATOR, None])
def test_the_port_refuses_anyone_but_the_installation_admin(caller: Any) -> None:
    store = _AuditingStore(active=1, inactive=1)
    port = _port(store, _Limit(10))

    calls = (
        lambda: port.list_accounts(caller=caller),
        lambda: port.create_account("acme.new", created_by="x", caller=caller),
        lambda: port.rename_account(DEFAULT_ACCOUNT_ID, display_name="X", actor="x", caller=caller),
        lambda: port.enable_account("acme.parked-0", actor="x", caller=caller),
        lambda: port.disable_account(DEFAULT_ACCOUNT_ID, actor="x", caller=caller),
    )
    for call in calls:
        with pytest.raises(InstallationAdminRequiredError):
            call()
    assert store.transactions == 0
    assert store.audit == []
    assert "acme.new" not in store.rows


def test_the_port_creates_within_the_limit_and_audits_in_the_same_transaction() -> None:
    store = _AuditingStore(active=1)
    port = _port(store, _Limit(2))

    account = port.create_account(
        " acme.new ", display_name="Acme", is_active=True, created_by="module:ops", caller=ADMIN
    )

    assert account["account_id"] == "acme.new"
    assert store.transactions == 1
    [event] = store.audit
    assert event["action_type"] == "auth_account_create"
    assert event["agent"] == "module:ops"
    assert event["result"] == {"account_id": "acme.new", "is_active": True}
    assert store.audit_in_transaction == [True]

    with pytest.raises(AccountLimitError) as refused:
        port.create_account("acme.third", created_by="module:ops", caller=ADMIN)
    assert refused.value.status_code == 409
    assert refused.value.payload()["code"] == ACCOUNT_LIMIT_REACHED_CODE
    assert len(store.audit) == 1


def test_the_port_reports_a_duplicate_as_already_existing() -> None:
    port = _port(_AuditingStore(active=1), _Limit(5))
    with pytest.raises(ValueError, match="already exists"):
        port.create_account(DEFAULT_ACCOUNT_ID, created_by="module:ops", caller=ADMIN)


def test_the_port_fails_closed_without_an_entitlement_service() -> None:
    store = _AuditingStore(active=1)
    with pytest.raises(AccountLimitError) as refused:
        _port(store, None).create_account("acme.new", created_by="module:ops", caller=ADMIN)
    assert refused.value.status_code == 503
    assert "acme.new" not in store.rows


def test_the_port_enable_respects_the_limit_and_disable_frees_a_place() -> None:
    store = _AuditingStore(active=2, inactive=1)
    port = _port(store, _Limit(2))

    with pytest.raises(AccountLimitError):
        port.enable_account("acme.parked-0", actor="module:ops", caller=ADMIN)

    disabled = port.disable_account(
        "acme.active-1", actor="module:ops", reason="moved", caller=ADMIN
    )
    enabled = port.enable_account("acme.parked-0", actor="module:ops", caller=ADMIN)
    again = port.enable_account("acme.parked-0", actor="module:ops", caller=ADMIN)

    assert disabled["is_active"] is False
    assert enabled["is_active"] is True
    assert again["is_active"] is True
    # Enabling an active account is a no-op: no second audit event.
    assert [event["action_type"] for event in store.audit] == [
        "auth_account_disable",
        "auth_account_enable",
    ]
    assert store.audit[0]["result"]["reason"] == "moved"
    assert store.audit_in_transaction == [True, True]


def test_the_port_renames_and_audits_an_update() -> None:
    store = _AuditingStore(active=1)
    port = _port(store, _Limit(5))

    account = port.rename_account(
        DEFAULT_ACCOUNT_ID, display_name=" Head office ", actor="module:ops", caller=ADMIN
    )

    assert account["display_name"] == "Head office"
    [event] = store.audit
    assert event["action_type"] == "auth_account_update"
    assert event["result"]["display_name"] == "Head office"
    with pytest.raises(ValueError, match="display_name"):
        port.rename_account(DEFAULT_ACCOUNT_ID, display_name=" ", actor="x", caller=ADMIN)


def test_the_port_reports_unknown_accounts_and_needs_an_actor() -> None:
    port = _port(_AuditingStore(), _Limit(5))

    for call in (
        lambda: port.enable_account("acme.missing", actor="module:ops", caller=ADMIN),
        lambda: port.disable_account("acme.missing", actor="module:ops", caller=ADMIN),
        lambda: port.rename_account(
            "acme.missing", display_name="X", actor="module:ops", caller=ADMIN
        ),
    ):
        with pytest.raises(LookupError):
            call()
    with pytest.raises(AccountNotFoundError):
        port.enable_account("acme.missing", actor="module:ops", caller=ADMIN)
    with pytest.raises(ValueError, match="created_by"):
        port.create_account("acme.new", created_by=" ", caller=ADMIN)
    with pytest.raises(ValueError, match="actor"):
        port.disable_account("acme.missing", actor="", caller=ADMIN)
    with pytest.raises(ValueError, match="cannot be disabled"):
        port.disable_account(DEFAULT_ACCOUNT_ID, actor="module:ops", caller=ADMIN)


def test_the_port_lists_accounts_with_user_counts() -> None:
    port = _port(_AuditingStore(active=1, inactive=1), _Limit(5))

    every = port.list_accounts(caller=ADMIN)
    active = port.list_accounts(include_inactive=False, caller=ADMIN)

    assert {row["account_id"]: row["user_count"] for row in every} == {
        DEFAULT_ACCOUNT_ID: 3,
        "acme.parked-0": 0,
    }
    assert [row["account_id"] for row in active] == [DEFAULT_ACCOUNT_ID]


def test_the_port_lists_unknown_user_counts_as_none() -> None:
    rows = _port(_Store(active=1), _Limit(5)).list_accounts(caller=ADMIN)
    assert rows[0]["user_count"] is None


def test_core_attaches_the_accounts_port_for_modules() -> None:
    from fastapi import FastAPI

    from core.entrypoints.api.app_factory import _attach_module_ports
    from core.infrastructure.runtime.app_factory import RuntimeContainer

    store = _Store(active=1)
    container = RuntimeContainer(
        app=FastAPI(),
        config={},
        service_factories={
            "identity_store": lambda: store,
            "entitlement_service": lambda: _Limit(2),
        },
    )

    _attach_module_ports(container)

    assert isinstance(container.accounts, AccountsPort)
    container.accounts.create_account("acme.new", created_by="module:ops", caller=ADMIN)
    assert "acme.new" in store.rows
