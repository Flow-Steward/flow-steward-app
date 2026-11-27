"""The ``accounts`` entitlement limit on account creation (multi-account C2)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import pytest

from core.application.auth.account_limit import (
    ACCOUNT_LIMIT_REACHED_CODE,
    ACCOUNT_LIMIT_UNAVAILABLE_CODE,
    DEFAULT_ACCOUNT_LIMIT,
    AccountLimitError,
    effective_account_limit,
    require_account_creation_allowed,
)
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.settings.platform_contracts import CoreEntitlementService
from tests.unit.test_installation_admin import INSTALLATION_ADMIN, accounts_client


class _Source:
    """An entitlement source that grants ``accounts: N`` to the installation account."""

    def __init__(self, accounts: int | None, *, fail: bool = False) -> None:
        self.accounts = accounts
        self.fail = fail
        self.asked: list[str] = []

    def features_for(self, account_id: str) -> Collection[str]:
        return ()

    def limits_for(self, account_id: str) -> Mapping[str, int]:
        self.asked.append(account_id)
        if self.fail:
            raise RuntimeError("store unavailable")
        if self.accounts is None or account_id != DEFAULT_ACCOUNT_ID:
            return {}
        return {"accounts": self.accounts}


def _service(source: _Source | None = None) -> CoreEntitlementService:
    service = CoreEntitlementService()
    if source is not None:
        service.register_entitlement_source(source)
    return service


class _AccountsBL:
    def __init__(self, active: int, inactive: int = 0) -> None:
        self.rows = [
            {"account_id": f"acme.{index}", "is_active": index < active}
            for index in range(active + inactive)
        ]
        self.created: list[str] = []
        self.lifecycle_transactions = 0

    def account_lifecycle_transaction(self) -> AbstractContextManager[None]:
        self.lifecycle_transactions += 1
        return nullcontext()

    def list_accounts(
        self, *, include_inactive: bool = True, limit: int = 200, offset: int = 0, **_: Any
    ) -> list[dict[str, Any]]:
        rows = [row for row in self.rows if include_inactive or row["is_active"]]
        return rows[offset : offset + limit]

    def create_account(self, *, account_id: str, is_active: bool = True, **_: Any) -> dict:
        self.created.append(account_id)
        row = {"account_id": account_id, "is_active": is_active}
        self.rows.append(row)
        return row


# --- the rule ----------------------------------------------------------------------------


def test_no_source_keeps_the_single_account_default() -> None:
    assert DEFAULT_ACCOUNT_LIMIT == 1
    assert effective_account_limit(_service()) == 1
    with pytest.raises(AccountLimitError) as refused:
        require_account_creation_allowed(_service(), lambda: 1)
    assert refused.value.code == ACCOUNT_LIMIT_REACHED_CODE
    assert refused.value.status_code == 409


def test_a_source_that_answers_none_is_the_default_not_unlimited() -> None:
    source = _Source(None)
    assert effective_account_limit(_service(source)) == DEFAULT_ACCOUNT_LIMIT
    with pytest.raises(AccountLimitError):
        require_account_creation_allowed(_service(source), lambda: 1)


def test_the_limit_is_read_for_the_creators_account() -> None:
    source = _Source(3)
    require_account_creation_allowed(_service(source), lambda: 1, account_id=DEFAULT_ACCOUNT_ID)
    assert source.asked == [DEFAULT_ACCOUNT_ID]


def test_creation_is_allowed_below_the_limit_and_refused_at_it() -> None:
    service = _service(_Source(3))
    assert require_account_creation_allowed(service, lambda: 2) == 3
    with pytest.raises(AccountLimitError) as refused:
        require_account_creation_allowed(service, lambda: 3)
    assert refused.value.limit == 3
    assert refused.value.active == 3
    assert "Account limit reached" in refused.value.message
    assert refused.value.payload()["details"] == {
        "limit_name": "accounts",
        "limit": 3,
        "active_accounts": 3,
    }


@pytest.mark.parametrize(
    ("service", "count"),
    [
        (None, lambda: 1),
        (object(), lambda: 1),
        (_service(_Source(5)), lambda: (_ for _ in ()).throw(RuntimeError("db down"))),
        (_service(_Source(5)), lambda: "many"),
    ],
)
def test_an_unreadable_limit_or_count_refuses(service: Any, count: Any) -> None:
    with pytest.raises(AccountLimitError) as refused:
        require_account_creation_allowed(service, count)
    assert refused.value.code == ACCOUNT_LIMIT_UNAVAILABLE_CODE
    assert refused.value.status_code == 503


def test_a_failing_source_refuses() -> None:
    # The service answers 0 on a source failure: nothing allowed.
    service = _service(_Source(5, fail=True))
    assert effective_account_limit(service) == 0
    with pytest.raises(AccountLimitError) as refused:
        require_account_creation_allowed(service, lambda: 1)
    assert refused.value.code == ACCOUNT_LIMIT_REACHED_CODE


def test_a_raising_service_refuses() -> None:
    class Broken:
        def limit(self, *_args: Any) -> int:
            raise RuntimeError("boom")

    with pytest.raises(AccountLimitError) as refused:
        effective_account_limit(Broken())
    assert refused.value.code == ACCOUNT_LIMIT_UNAVAILABLE_CODE


# --- the route ---------------------------------------------------------------------------


def _create(client: Any, account_id: str, *, is_active: bool = True) -> Any:
    return client.post(
        "/api/auth/accounts", json={"account_id": account_id, "is_active": is_active}
    )


def test_route_refuses_a_second_account_without_a_source() -> None:
    bl = _AccountsBL(active=1)
    client = accounts_client(bl, INSTALLATION_ADMIN, entitlement_service=_service())

    response = _create(client, "acme.second")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == ACCOUNT_LIMIT_REACHED_CODE
    assert body["error"].startswith("Account limit reached")
    assert bl.created == []


def test_route_allows_accounts_up_to_the_limit() -> None:
    bl = _AccountsBL(active=1)
    client = accounts_client(bl, INSTALLATION_ADMIN, entitlement_service=_service(_Source(2)))

    assert _create(client, "acme.second").status_code == 200
    # The count and the insert ran in the store's lifecycle transaction.
    assert bl.lifecycle_transactions == 1
    refused = _create(client, "acme.third")
    assert refused.status_code == 409
    assert refused.json()["details"] == {
        "limit_name": "accounts",
        "limit": 2,
        "active_accounts": 2,
    }
    assert bl.created == ["acme.second"]


def test_route_does_not_count_disabled_accounts() -> None:
    bl = _AccountsBL(active=1, inactive=3)
    client = accounts_client(bl, INSTALLATION_ADMIN, entitlement_service=_service(_Source(2)))

    assert _create(client, "acme.second").status_code == 200


def test_route_refuses_a_disabled_account_at_the_limit() -> None:
    bl = _AccountsBL(active=2)
    client = accounts_client(bl, INSTALLATION_ADMIN, entitlement_service=_service(_Source(2)))

    assert _create(client, "acme.parked", is_active=False).status_code == 409


def test_route_fails_closed_without_an_entitlement_service() -> None:
    bl = _AccountsBL(active=1)
    client = accounts_client(bl, INSTALLATION_ADMIN, entitlement_service=None)

    response = _create(client, "acme.second")

    assert response.status_code == 503
    assert response.json()["code"] == ACCOUNT_LIMIT_UNAVAILABLE_CODE
    assert bl.created == []
