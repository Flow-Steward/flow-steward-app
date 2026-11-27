"""Notifications are delivered only for the account they belong to (multi-account step C3)."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.notifications.contracts import _resolve_authoritative_account_id
from core.application.notifications.dispatch import dispatch_project_notification_event
from core.infrastructure.config.account_scope import MissingAccountScopeError
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID

ACCOUNT_B = "22222222-2222-2222-2222-222222222222"
PROJECT = "33333333-3333-3333-3333-333333333333"


class _Store:
    """Notification store whose project row is controlled by the test."""

    def __init__(self, project: dict[str, Any] | None) -> None:
        self.project = project
        self.route_lookups = 0
        self.ledger: list[dict[str, Any]] = []

    def get_project(self, _project_id: str) -> dict[str, Any] | None:
        return self.project

    def list_notification_routes(self, **_kwargs: Any) -> list[dict[str, Any]]:
        self.route_lookups += 1
        return []

    def record_event_dispatch(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def record_domain_event(self, **kwargs: Any) -> None:
        self.ledger.append(kwargs)


def test_event_account_comes_from_the_event_then_its_project() -> None:
    assert (
        _resolve_authoritative_account_id(_Store({"account_id": ACCOUNT_B}), PROJECT, None)
        == ACCOUNT_B
    )
    assert _resolve_authoritative_account_id(_Store(None), PROJECT, ACCOUNT_B) == ACCOUNT_B


@pytest.mark.parametrize("project", [None, {}, {"account_id": ""}])
def test_event_without_any_account_is_refused_not_given_to_the_default(
    project: dict[str, Any] | None,
) -> None:
    with pytest.raises(MissingAccountScopeError):
        _resolve_authoritative_account_id(_Store(project), PROJECT, None)


def test_store_without_project_lookup_refuses_an_event_without_account() -> None:
    class _NoProjects:
        pass

    with pytest.raises(MissingAccountScopeError):
        _resolve_authoritative_account_id(_NoProjects(), PROJECT, "")


def test_dispatch_refuses_before_reading_routes_or_writing_the_ledger() -> None:
    store = _Store(None)

    with pytest.raises(ValueError, match="no account"):
        dispatch_project_notification_event(
            store,
            project_id=PROJECT,
            event_type="job.failed",
            event_id="evt-1",
        )

    assert store.route_lookups == 0
    assert store.ledger == []


def test_dispatch_records_the_project_account_on_the_ledger() -> None:
    store = _Store({"account_id": ACCOUNT_B})

    dispatch_project_notification_event(
        store,
        project_id=PROJECT,
        event_type="job.failed",
        event_id="evt-1",
    )

    assert store.route_lookups == 1
    assert [row["account_id"] for row in store.ledger] == [ACCOUNT_B]
    assert ACCOUNT_B != DEFAULT_ACCOUNT_ID


def test_dispatch_no_longer_names_the_default_account() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for relative in (
        "core/application/notifications/dispatch.py",
        "core/application/notifications/contracts.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "DEFAULT_ACCOUNT_ID" not in source, relative
