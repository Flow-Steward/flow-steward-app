"""The project's Notifications tab lists automatic notifications, read-only.

The overview is read from the live settings: Automation rules that send a
notification at workflow, group and step scope, and the project's own event
subscriptions. Message-sending steps are not listed, nothing is written, and
delivery itself is unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceForbiddenError
from core.application.management.query_services import ProjectQueryService
from core.application.management.scope import ScopeContext
from core.application.notifications import dispatch as notification_dispatch
from core.application.workflows import reaction_runtime

PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def _notify_rule(key: str, *, event: str = "job.failed", enabled: bool = True, **action: Any):
    return {
        "reaction_key": key,
        "display_name": "",
        "event_type": event,
        "conditions": {},
        "action": {"type": "notify", **action},
        "debounce_seconds": 0,
        "enabled": enabled,
    }


class _Store:
    """A read-only store: any write or dispatch attempt fails the test."""

    def __init__(
        self,
        *,
        workflows: list[dict[str, Any]] | None = None,
        reactions: dict[str, list[dict[str, Any]]] | None = None,
        routes: list[dict[str, Any]] | None = None,
        extensions: list[dict[str, Any]] | None = None,
        connections: list[dict[str, Any]] | None = None,
        scope_names: dict[str, dict[str, dict[str, str]]] | None = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        self.workflows = workflows or []
        self.reactions = reactions or {}
        self.routes = routes or []
        self.extensions = extensions or []
        self.connections = connections or []
        self.scope_names = scope_names or {}
        self.account_id = account_id
        self.reaction_reads: list[tuple[str, Any]] = []

    def get_project(self, project_id):
        return {"id": project_id, "name": "Demo", "account_id": self.account_id}

    def get_project_workflows(self, project_id, enabled_only=False, include_deleted=False):
        return [dict(row) for row in self.workflows]

    def list_workflow_reactions(self, workflow_id, enabled_only=False, account_id=None):
        self.reaction_reads.append((workflow_id, account_id))
        return [dict(row) for row in self.reactions.get(workflow_id, [])]

    def list_notification_routes(self, project_id, enabled_only=True):
        assert enabled_only is False
        return [dict(row) for row in self.routes]

    def list_extensions(self, enabled_only=False):
        return [dict(row) for row in self.extensions]

    def list_project_connections(self, project_id, enabled_only=True):
        return [dict(row) for row in self.connections]

    def list_workflow_reaction_scope_names(self, workflow_id):
        return self.scope_names.get(workflow_id, {"group": {}, "step": {}})

    def __getattr__(self, name):  # pragma: no cover - only reached on a regression
        if name.startswith(
            ("save_", "replace_", "update_", "delete_", "record_", "log_", "dispatch")
        ):
            raise AssertionError(f"the overview must not call {name}")
        raise AttributeError(name)


def _service(store) -> ProjectQueryService:
    return ProjectQueryService(
        get_logger=lambda: store,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda _project_id: 0,
        get_ready_queue_depth=lambda: 0,
    )


def _overview(store, scope: ScopeContext | None = None) -> list[dict[str, Any]]:
    return _service(store).get_notification_overview(PROJECT_ID, scope=scope).dump()["entries"]


def _by_name(entries):
    return {entry["name"]: entry for entry in entries}


def _full_store() -> _Store:
    return _Store(
        workflows=[
            {"workflow_id": "wf-orders", "workflow_display_name": "Orders", "enabled": True},
            {"workflow_id": "wf-off", "workflow_display_name": "Unlinked", "enabled": False},
        ],
        reactions={
            "wf-orders": [
                _notify_rule("failed_to_team", target="project_routes"),
                _notify_rule(
                    "scope::group::batch::group_done",
                    event="job.group_completed",
                    target="extension_channel",
                    provider_key="acme.chat",
                    channel_id="ops-room",
                ),
                _notify_rule(
                    "scope::step::send_mail::step_failed",
                    event="job.step_failed",
                    target="extension_channel",
                    provider_key="acme.mailer",
                    recipients=["owner@example.com", "ops@example.com"],
                ),
                _notify_rule("by_key", route_keys=["team_failures"], severity="error"),
                # Not a notification: a retry rule never appears.
                {
                    "reaction_key": "scope::step::send_mail::retry",
                    "event_type": "job.step_failed",
                    "action": {"type": "retry_step"},
                    "enabled": True,
                },
            ],
            "wf-off": [_notify_rule("never_listed", target="project_routes")],
        },
        routes=[
            {
                "route_key": "team_failures",
                "display_name": "Team failures",
                "provider_key": "acme.chat",
                "provider_display_name": "Acme Chat",
                "event_type": "job.failed",
                "severity": "any",
                "workflow_id": "wf-orders",
                "route_config": {"channel_id": "team"},
                "enabled": True,
            }
        ],
        extensions=[
            {"extension_key": "acme.chat", "enabled": True},
            {"extension_key": "acme.mailer", "enabled": True},
        ],
        scope_names={
            "wf-orders": {
                "group": {"batch": "Batch import"},
                "step": {"send_mail": "Send Email"},
            }
        },
    )


def test_overview_lists_automation_of_all_three_scopes_and_subscriptions() -> None:
    entries = _overview(_full_store())
    names = _by_name(entries)

    assert set(names) == {"failed_to_team", "group_done", "step_failed", "by_key", "Team failures"}
    assert [entry["kind"] for entry in entries].count("subscription") == 1

    workflow = names["failed_to_team"]
    assert workflow["source"] == {
        "scope_kind": "workflow",
        "workflow_id": "wf-orders",
        "workflow_name": "Orders",
        "scope_id": "",
        "scope_name": "",
    }
    assert workflow["settings"] == {
        "kind": "automation",
        "workflow_id": "wf-orders",
        "scope_kind": "workflow",
        "scope_id": "",
        "rule_key": "failed_to_team",
    }
    # Default target: every matching subscription, decided when the event fires.
    assert workflow["delivery"]["method"] == "matching_subscriptions"
    assert workflow["delivery"]["decided_at_run_time"] is True
    assert [item["name"] for item in workflow["delivery"]["subscriptions"]] == ["Team failures"]
    assert workflow["state"] == "enabled"

    group = names["group_done"]
    assert group["source"]["scope_kind"] == "group"
    assert group["source"]["scope_name"] == "Batch import"
    assert group["settings"]["scope_id"] == "batch"
    assert group["settings"]["rule_key"] == "group_done"
    # A direct extension channel with no project subscription behind it.
    assert group["delivery"]["method"] == "channel"
    assert group["delivery"]["provider_key"] == "acme.chat"
    assert group["delivery"]["channel_id"] == "ops-room"
    assert group["state"] == "enabled"

    step = names["step_failed"]
    assert step["source"]["scope_kind"] == "step"
    assert step["source"]["scope_name"] == "Send Email"
    assert step["delivery"]["method"] == "recipients"
    assert step["delivery"]["recipients"] == ["owner@example.com", "ops@example.com"]
    assert step["delivery"]["decided_at_run_time"] is False

    by_key = names["by_key"]
    assert by_key["delivery"]["method"] == "subscriptions"
    assert by_key["delivery"]["subscriptions"] == [
        {"route_key": "team_failures", "name": "Team failures", "found": True}
    ]
    assert by_key["conditions"]["severity"] == "error"

    subscription = names["Team failures"]
    assert subscription["source"]["scope_kind"] == "project"
    assert subscription["settings"] == {"kind": "subscription", "route_key": "team_failures"}
    assert subscription["conditions"]["workflow_name"] == "Orders"
    assert subscription["delivery"]["channel_id"] == "team"


def test_message_sending_steps_and_unused_extensions_are_not_notifications() -> None:
    store = _Store(
        workflows=[{"workflow_id": "wf-mail", "workflow_display_name": "Mailer", "enabled": True}],
        reactions={
            "wf-mail": [
                {
                    "reaction_key": "scope::step::send_email::retry",
                    "event_type": "job.step_failed",
                    "action": {"type": "retry_step"},
                    "enabled": True,
                },
                {
                    "reaction_key": "cancel",
                    "event_type": "job.failed",
                    "action": {"type": "cancel_job"},
                },
            ]
        },
        extensions=[{"extension_key": "acme.mailer", "enabled": True}],
        connections=[{"connection_id": "c-1", "extension_id": "acme.mailer", "enabled": True}],
        scope_names={"wf-mail": {"group": {}, "step": {"send_email": "Send Email"}}},
    )

    assert _overview(store) == []


def test_rules_sharing_one_channel_stay_separate_entries() -> None:
    rule = {"target": "extension_channel", "provider_key": "acme.chat", "channel_id": "ops"}
    store = _Store(
        workflows=[{"workflow_id": "wf", "workflow_display_name": "Wf", "enabled": True}],
        reactions={"wf": [_notify_rule("first", **rule), _notify_rule("second", **rule)]},
        extensions=[{"extension_key": "acme.chat", "enabled": True}],
    )

    entries = _overview(store)

    assert [entry["name"] for entry in entries] == ["first", "second"]
    assert len({entry["entry_id"] for entry in entries}) == 2


def test_disabled_and_unavailable_destinations_have_a_clear_state() -> None:
    store = _Store(
        workflows=[{"workflow_id": "wf", "workflow_display_name": "Wf", "enabled": True}],
        reactions={
            "wf": [
                _notify_rule("switched_off", enabled=False, route_keys=["team"]),
                _notify_rule("unknown_key", route_keys=["gone"]),
                _notify_rule(
                    "disabled_extension",
                    target="extension_channel",
                    provider_key="acme.chat",
                    channel_id="ops",
                ),
                _notify_rule(
                    "missing_extension",
                    target="extension_channel",
                    provider_key="acme.unknown",
                    recipients="a@example.com",
                ),
                _notify_rule(
                    "no_destination", target="extension_channel", provider_key="acme.pager"
                ),
                _notify_rule("nothing_matches", event="job.completed", target="project_routes"),
                _notify_rule(
                    "scope::phase::p1::retired", target="project_routes", event="job.failed"
                ),
            ]
        },
        routes=[
            {
                "route_key": "team",
                "display_name": "Team",
                "provider_key": "acme.mail",
                "event_type": "job.failed",
                "route_config": {"to": ["team@example.com"], "connection_id": "conn-off"},
                "enabled": True,
            },
            {
                "route_key": "paused",
                "display_name": "Paused",
                "provider_key": "acme.mail",
                "event_type": "job.failed",
                "route_config": {"to": ["x@example.com"]},
                "enabled": False,
            },
        ],
        extensions=[
            {"extension_key": "acme.chat", "enabled": False},
            {"extension_key": "acme.mail", "enabled": True},
            {"extension_key": "acme.pager", "enabled": True},
        ],
        connections=[
            {"connection_id": "conn-off", "extension_id": "acme.mail", "enabled": False},
        ],
    )

    entries = _by_name(_overview(store))

    assert entries["switched_off"]["state"] == "disabled"
    assert entries["unknown_key"]["state"] == "setup_problem"
    assert entries["unknown_key"]["problems"] == ["subscription_missing"]
    assert entries["unknown_key"]["delivery"]["subscriptions"] == [
        {"route_key": "gone", "name": "gone", "found": False}
    ]
    assert entries["disabled_extension"]["problems"] == ["extension_disabled"]
    assert entries["disabled_extension"]["connection"] == {"extension_id": "acme.chat"}
    assert entries["missing_extension"]["problems"] == ["extension_missing"]
    assert entries["no_destination"]["problems"] == ["destination_missing"]
    assert entries["no_destination"]["connection"] is None
    assert entries["nothing_matches"]["problems"] == ["no_matching_subscription"]
    assert entries["retired"]["problems"][0] == "scope_retired"
    # A subscription whose own connection is switched off.
    assert entries["Team"]["state"] == "setup_problem"
    assert entries["Team"]["problems"] == ["connection_disabled"]
    assert entries["Team"]["connection"] == {"extension_id": "acme.mail"}
    assert entries["Paused"]["state"] == "disabled"


def test_template_recipients_are_decided_at_run_time() -> None:
    store = _Store(
        workflows=[{"workflow_id": "wf", "workflow_display_name": "Wf", "enabled": True}],
        reactions={
            "wf": [
                _notify_rule(
                    "dynamic",
                    target="extension_channel",
                    provider_key="acme.mail",
                    recipients=["{{ payload.owner_email }}"],
                )
            ]
        },
        extensions=[{"extension_key": "acme.mail", "enabled": True}],
    )

    (entry,) = _overview(store)

    assert entry["delivery"]["decided_at_run_time"] is True


def test_an_edited_rule_shows_on_the_next_read() -> None:
    store = _full_store()
    assert _by_name(_overview(store))["group_done"]["delivery"]["channel_id"] == "ops-room"

    store.reactions["wf-orders"][1]["action"]["channel_id"] = "night-shift"
    store.reactions["wf-orders"][1]["enabled"] = False

    entry = _by_name(_overview(store))["group_done"]
    assert entry["delivery"]["channel_id"] == "night-shift"
    assert entry["state"] == "disabled"


def test_rules_are_read_in_the_callers_account_and_allowed_workflows_only() -> None:
    store = _full_store()
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="svc",
        principal_type="service_account",
        allowed_workflow_ids=frozenset({"wf-other"}),
    )

    entries = _overview(store, scope)

    assert [entry["kind"] for entry in entries] == ["subscription"]
    assert store.reaction_reads == []


def test_reads_rules_with_the_resolved_account() -> None:
    store = _full_store()
    _overview(
        store, ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="a", principal_type="user")
    )
    assert store.reaction_reads == [("wf-orders", DEFAULT_ACCOUNT_ID)]


def test_another_accounts_project_is_refused() -> None:
    store = _full_store()
    scope = ScopeContext(account_id="acc_other", actor_id="eve", principal_type="user")
    with pytest.raises(ServiceForbiddenError):
        _overview(store, scope)
    assert store.reaction_reads == []


def test_the_overview_destination_is_what_the_runtime_delivers_to(monkeypatch) -> None:
    """Delivery regression: the runtime still sends a listed rule where the overview says."""
    store = _full_store()
    entries = _by_name(_overview(store))
    captured: list[dict[str, Any]] = []

    def _dispatch(_bl, **kwargs):
        captured.append(kwargs)
        return [{"delivery_id": "d-1"}]

    monkeypatch.setattr(notification_dispatch, "dispatch_project_notification_event", _dispatch)
    bl = MagicMock()
    bl.get_job.return_value = {"id": "job-1", "project_id": PROJECT_ID}
    for rule in store.reactions["wf-orders"][1:3]:
        result = reaction_runtime.dispatch_notification_from_reaction(
            {"project_id": PROJECT_ID, "job_id": "job-1", "event_type": rule["event_type"]},
            rule["action"],
            get_logger_only=lambda: bl,
            err_database_not_configured="db unavailable",
            to_json_obj=lambda value: value,
            resolve_job_workflow_id=lambda _job: "wf-orders",
            logger=MagicMock(),
        )
        assert result["status"] == "completed"

    group_route, step_route = (call["direct_routes"][0] for call in captured)
    assert group_route["provider_key"] == entries["group_done"]["delivery"]["provider_key"]
    assert (
        group_route["route_config"]["channel_id"] == entries["group_done"]["delivery"]["channel_id"]
    )
    assert step_route["route_config"]["to"] == entries["step_failed"]["delivery"]["recipients"]
    assert all(call["ignore_filters"] is True for call in captured)
    assert len(captured) == 2
