"""The notification overview reads the live rules and subscriptions from Postgres."""

from __future__ import annotations

import json
import uuid

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.notifications.overview import build_project_notification_overview
from core.infrastructure.db.platform_stores import PlatformIntegrationStore, PlatformReactionStore
from tests._workflow_definitions import edge, node


def _rule(key, event, **action):
    return {
        "reaction_key": key,
        "event_type": event,
        "enabled": True,
        "action": {"type": "notify", **action},
    }


@pytest.fixture
def project_with_rules(audit_db):
    registry, conn, workflow_id, project_id = audit_db
    registry.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": "Orders",
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
            "enabled": False,
            "steps": [
                {
                    "step_id": "send_email",
                    "display_name": "Send Email",
                    "step_kind": "transform",
                    "group_id": "batch",
                },
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            "groups": [
                {
                    "group_id": "batch",
                    "display_name": "Batch import",
                    "body": {"kind": "sequence", "entry": node("send_email")},
                }
            ],
            "for_each": [],
            "settings": {
                "execution_graph": {
                    "schema_version": 1,
                    "entry": node("batch", "group"),
                    "edges": [
                        edge("send_email", {"kind": "group_exit", "group_id": "batch"}),
                        edge("batch", node("done"), source_kind="group"),
                    ],
                }
            },
        }
    )
    provider_key = f"acme.notify_{uuid.uuid4().hex[:8]}"
    type_id = conn.execute(
        """
        INSERT INTO provider_types (key, label) VALUES ('notification_provider', 'Notification')
        ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key
        RETURNING id
        """
    ).fetchone()[0]
    provider_id = conn.execute(
        """
        INSERT INTO providers (provider_key, provider_type_id, display_name, implementation_kind)
        VALUES (%s, %s, 'Acme Notify', 'extension') RETURNING id
        """,
        (provider_key, type_id),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO notification_routes
            (project_id, route_key, display_name, provider_id, event_type, route_config_jsonb)
        VALUES (%s, 'team_failures', 'Team failures', %s, 'job.failed', %s)
        """,
        (project_id, provider_id, json.dumps({"channel_id": "team"})),
    )
    # The audit database the fixture already verified, never another one.
    reactions = PlatformReactionStore(registry.dsn)
    reactions.save_workflow_reactions(
        workflow_id,
        [
            _rule("failed_to_team", "job.failed", target="project_routes"),
            _rule(
                "scope::group::batch::group_done",
                "job.group_completed",
                target="extension_channel",
                provider_key=provider_key,
                channel_id="ops-room",
            ),
            _rule(
                "scope::step::send_email::step_failed",
                "job.step_failed",
                target="extension_channel",
                provider_key=provider_key,
                recipients=["owner@example.com"],
            ),
            {
                "reaction_key": "scope::step::send_email::retry",
                "event_type": "job.step_failed",
                "enabled": True,
                "action": {"type": "retry_step"},
            },
        ],
        account_id=DEFAULT_ACCOUNT_ID,
    )
    try:
        yield workflow_id, project_id, provider_key, reactions, registry.dsn
    finally:
        conn.execute("DELETE FROM notification_routes WHERE project_id = %s", (project_id,))
        conn.execute("DELETE FROM providers WHERE id = %s", (provider_id,))
        conn.execute("DELETE FROM workflow_reactions WHERE workflow_id = %s", (workflow_id,))


def test_overview_reads_rules_of_every_scope_and_subscriptions(project_with_rules):
    workflow_id, project_id, provider_key, _, dsn = project_with_rules
    store = PlatformIntegrationStore(dsn)

    entries = build_project_notification_overview(
        store,
        project_id=str(project_id),
        account_id=DEFAULT_ACCOUNT_ID,
        workflows=[{"workflow_id": workflow_id, "workflow_display_name": "Orders"}],
    )
    by_name = {entry["name"]: entry for entry in entries}

    assert set(by_name) == {"failed_to_team", "group_done", "step_failed", "Team failures"}
    assert by_name["failed_to_team"]["source"]["scope_kind"] == "workflow"
    assert [
        item["route_key"] for item in by_name["failed_to_team"]["delivery"]["subscriptions"]
    ] == ["team_failures"]
    assert by_name["group_done"]["source"]["scope_name"] == "Batch import"
    assert by_name["group_done"]["settings"]["scope_id"] == "batch"
    assert by_name["group_done"]["delivery"]["channel_id"] == "ops-room"
    assert by_name["step_failed"]["source"]["scope_name"] == "Send Email"
    assert by_name["step_failed"]["delivery"]["recipients"] == ["owner@example.com"]
    # No installed extension carries this synthetic key: an honest setup problem.
    assert by_name["group_done"]["state"] == "setup_problem"
    assert by_name["group_done"]["connection"] == {"extension_id": provider_key}
    assert by_name["Team failures"]["kind"] == "subscription"
    assert by_name["Team failures"]["settings"] == {
        "kind": "subscription",
        "route_key": "team_failures",
    }


def test_overview_follows_an_edited_rule_without_a_copy(project_with_rules):
    workflow_id, project_id, _, reactions, dsn = project_with_rules
    store = PlatformIntegrationStore(dsn)
    reactions.save_workflow_reactions(
        workflow_id,
        [{**_rule("failed_to_team", "job.completed", target="project_routes"), "enabled": False}],
        account_id=DEFAULT_ACCOUNT_ID,
    )

    entries = build_project_notification_overview(
        store,
        project_id=str(project_id),
        account_id=DEFAULT_ACCOUNT_ID,
        workflows=[{"workflow_id": workflow_id, "workflow_display_name": "Orders"}],
    )

    rule = next(entry for entry in entries if entry["kind"] == "automation")
    assert [entry["kind"] for entry in entries] == ["automation", "subscription"]
    assert rule["event_type"] == "job.completed"
    assert rule["state"] == "disabled"
