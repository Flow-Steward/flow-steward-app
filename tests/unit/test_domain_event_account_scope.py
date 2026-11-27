"""Event resume and redrive never treat an event without an account as the default's."""

from __future__ import annotations

import inspect
import logging

import pytest

from core.application.workflows import domain_event_runtime
from core.infrastructure.config.account_scope import ACCOUNT_SCOPE_MISSING_CODE
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID

ACCOUNT_B = "22222222-2222-2222-2222-222222222222"


class _EventStore:
    def __init__(self, row: dict | None, rows: list[dict] | None = None) -> None:
        self.row = row
        self.rows = rows or []

    def get_domain_event(self, event_ref: str, *, account_id: str | None = None) -> dict | None:
        return self.row

    def list_domain_events(self, *, account_id: str | None = None, limit: int = 0) -> list[dict]:
        return list(self.rows)


def test_event_carrying_its_account_matches_that_account() -> None:
    row = {"id": "7", "event_id": "evt-1", "account_id": ACCOUNT_B}

    assert domain_event_runtime.event_row_matches_scope(row, account_id=ACCOUNT_B)
    assert not domain_event_runtime.event_row_matches_scope(row, account_id=DEFAULT_ACCOUNT_ID)


def test_event_without_account_is_refused_even_for_the_default_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = {"id": "7", "event_id": "evt-legacy"}

    with caplog.at_level(logging.WARNING):
        matched = domain_event_runtime.event_row_matches_scope(row, account_id=DEFAULT_ACCOUNT_ID)

    assert matched is False
    assert ACCOUNT_SCOPE_MISSING_CODE in caplog.text
    assert "evt-legacy" in caplog.text


def test_unscoped_lookup_still_sees_the_row() -> None:
    assert domain_event_runtime.event_row_matches_scope({"id": "7"}, account_id=None)


def test_resolve_by_ref_refuses_a_row_without_account_on_both_lookup_paths() -> None:
    legacy = {"id": "7", "event_id": "evt-legacy"}
    store = _EventStore(row=legacy, rows=[legacy])

    assert (
        domain_event_runtime.resolve_domain_event_by_ref(store, "7", account_id=DEFAULT_ACCOUNT_ID)
        is None
    )


def test_resolve_by_ref_returns_the_scoped_event_unchanged() -> None:
    row = {"id": "7", "event_id": "evt-1", "account_id": ACCOUNT_B}
    store = _EventStore(row=row)

    assert domain_event_runtime.resolve_domain_event_by_ref(store, "7", account_id=ACCOUNT_B) == row


def test_event_scope_helpers_no_longer_take_a_default_account() -> None:
    for function in (
        domain_event_runtime.event_row_matches_scope,
        domain_event_runtime.resolve_domain_event_by_ref,
    ):
        assert "default_account_id" not in inspect.signature(function).parameters


def test_worker_and_http_event_resume_do_not_inject_the_default_account() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for relative in (
        "core/entrypoints/cli/worker_event_ingress.py",
        "core/infrastructure/runtime/http_app_events_resume.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "default_account_id" not in source, relative
