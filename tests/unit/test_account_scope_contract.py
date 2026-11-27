"""Background units of work carry their account explicitly (multi-account step C3)."""

from __future__ import annotations

import logging

import pytest

from core.infrastructure.config.account_scope import (
    ACCOUNT_SCOPE_MISSING_CODE,
    MissingAccountScopeError,
    active_account_ids_from_store,
    require_account_id,
    resolve_unit_account_id,
    sole_account_id,
)

ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
ACCOUNT_B = "22222222-2222-2222-2222-222222222222"


def test_require_account_id_returns_the_carried_account() -> None:
    assert require_account_id(f"  {ACCOUNT_A} ", unit="job") == ACCOUNT_A


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_require_account_id_refuses_and_logs_a_missing_account(
    missing: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(MissingAccountScopeError) as excinfo:
        require_account_id(missing, unit="notification event")

    assert excinfo.value.code == ACCOUNT_SCOPE_MISSING_CODE
    assert excinfo.value.unit == "notification event"
    assert ACCOUNT_SCOPE_MISSING_CODE in caplog.text


def test_sole_account_id_needs_exactly_one_distinct_account() -> None:
    assert sole_account_id([ACCOUNT_A]) == ACCOUNT_A
    assert sole_account_id([ACCOUNT_A, ACCOUNT_A, ""]) == ACCOUNT_A
    assert sole_account_id([]) == ""
    assert sole_account_id([ACCOUNT_A, ACCOUNT_B]) == ""


def test_in_flight_unit_without_account_runs_under_the_only_active_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        resolved = resolve_unit_account_id(
            None, unit="provider response", list_active_account_ids=lambda: [ACCOUNT_A]
        )

    assert resolved == ACCOUNT_A
    assert "only active account" in caplog.text


def test_in_flight_unit_without_account_is_refused_with_several_active_accounts() -> None:
    with pytest.raises(MissingAccountScopeError):
        resolve_unit_account_id(
            "",
            unit="provider response",
            list_active_account_ids=lambda: [ACCOUNT_A, ACCOUNT_B],
        )


def test_in_flight_unit_is_refused_when_accounts_cannot_be_listed() -> None:
    def broken() -> list[str]:
        raise RuntimeError("database unavailable")

    with pytest.raises(MissingAccountScopeError):
        resolve_unit_account_id(None, unit="provider response", list_active_account_ids=broken)
    with pytest.raises(MissingAccountScopeError):
        resolve_unit_account_id(None, unit="provider response")


def test_carried_account_wins_without_any_lookup() -> None:
    def must_not_run() -> list[str]:
        raise AssertionError("lookup must not run when the unit carries its account")

    assert (
        resolve_unit_account_id(ACCOUNT_B, unit="job", list_active_account_ids=must_not_run)
        == ACCOUNT_B
    )


def test_active_account_lookup_reads_two_active_accounts_from_the_store() -> None:
    calls: list[dict[str, object]] = []

    class _Store:
        def list_accounts(self, **kwargs: object) -> list[dict[str, str]]:
            calls.append(kwargs)
            return [{"account_id": ACCOUNT_A}]

    lookup = active_account_ids_from_store(_Store())

    assert lookup is not None
    assert lookup() == [ACCOUNT_A]
    assert calls == [{"include_inactive": False, "limit": 2}]
    assert active_account_ids_from_store(object()) is None


def test_account_scope_contract_does_not_import_the_application_layer() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "core/infrastructure/config/account_scope.py"
    ).read_text(encoding="utf-8")
    assert "core.application" not in source
