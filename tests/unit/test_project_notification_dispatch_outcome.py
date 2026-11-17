"""An undelivered project notification says why (MCP probe 2026-11-17, F10).

The answer used to be ``{"notification_deliveries": []}`` alone, so a caller could
not tell "this project has no route" from "the routes skipped this event".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management import management_runtime
from core.application.management.command_models import ProjectNotificationDispatchInput
from core.application.management.scope import ScopeContext
from core.application.notifications import dispatch as notification_dispatch


class _RouteStore:
    def __init__(self, routes: list[dict[str, Any]]) -> None:
        self.routes = routes
        self.route_reads: list[dict[str, Any]] = []

    def list_notification_routes(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.route_reads.append(kwargs)
        return [dict(row) for row in self.routes]


def _dispatch(monkeypatch: pytest.MonkeyPatch, store: _RouteStore, deliveries: list[dict]):
    project_store = MagicMock()
    project_store.get_project.return_value = {"id": "project-1", "account_id": DEFAULT_ACCOUNT_ID}
    monkeypatch.setattr(
        notification_dispatch,
        "dispatch_project_notification_event",
        MagicMock(return_value=deliveries),
    )
    service = management_runtime.create_project_command_service(
        get_logger=lambda: project_store,
        get_notification_logger=lambda: store,
    )
    return service.dispatch_project_notification(
        "project-1",
        ProjectNotificationDispatchInput(event_type="workflow.completed", severity="warning"),
        scope=ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="cli",
            principal_type="system_actor",
            project_id="project-1",
        ),
    ).dump()


def test_no_enabled_route_is_named_with_what_to_configure(monkeypatch) -> None:
    store = _RouteStore([])

    result = _dispatch(monkeypatch, store, [])

    assert result["notification_deliveries"] == []
    assert result["routes_matched"] == 0
    assert result["enabled_route_count"] == 0
    assert result["reason"] == "no_enabled_route"
    assert "Notification routes" in result["message"]
    assert store.route_reads == [{"project_id": "project-1", "enabled_only": True}]


def test_routes_that_skip_the_event_are_told_apart_from_no_route(monkeypatch) -> None:
    store = _RouteStore([{"route_id": "r1", "event_type": "job.failed", "enabled": True}])

    result = _dispatch(monkeypatch, store, [])

    assert result["routes_matched"] == 0
    assert result["enabled_route_count"] == 1
    assert result["reason"] == "no_route_matched_event"
    assert "workflow.completed" in result["message"]
    assert "warning" in result["message"]


def test_a_delivery_carries_no_reason_and_reads_no_routes(monkeypatch) -> None:
    store = _RouteStore([{"route_id": "r1"}])

    result = _dispatch(monkeypatch, store, [{"delivery_id": "d1", "status": "sent"}])

    assert result["routes_matched"] == 1
    assert result["reason"] == ""
    assert result["message"] == ""
    assert store.route_reads == []
