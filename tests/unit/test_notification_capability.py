"""Mixed notification providers: what each connection type addresses."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.extensions import notification_capability as capability
from core.application.extensions.compiled_manifest_policies import (
    _validate_connection_type_notification_blocks,
)
from core.application.extensions.delivery_coordinator import coordinate_extension_delivery
from core.application.notifications import sensitive_payload
from core.application.notifications.route_connections import check_route_connection

EXT = "fixture.notify"
INSTALL = "install-a"
PROJECT = "project-1"


def _effect(target_kind: str = "recipient", channel: str = "email") -> dict[str, Any]:
    return {
        "operation_id": "deliver_notification",
        "effect_kind": "message_delivery",
        "channel": channel,
        "idempotency": "required",
        "test_mode_behavior": "suppress",
        "observability": {"target_kind": target_kind},
    }


def _extension(connection_types: list[dict[str, Any]], **effect: Any) -> dict[str, Any]:
    return {
        "extension_key": EXT,
        "extension_install_id": INSTALL,
        "kind": "notification_provider",
        "compiled_manifest": {
            "kind": "notification_provider",
            "connection_types": connection_types,
            "external_effects": [_effect(**effect)],
        },
    }


MAIL = {
    "connection_type_id": "example_mail",
    "notification": {"target_kind": "recipient", "ledger_channel": "email"},
}
CHAT = {
    "connection_type_id": "example_chat",
    "notification": {"target_kind": "channel", "ledger_channel": "chat"},
}
BOUND = {
    "connection_type_id": "example_chat_bound",
    "notification": {
        "target_kind": "channel",
        "ledger_channel": "chat",
        "channel_source": "connection",
    },
}
FILES = {"connection_type_id": "example_files"}

MIXED = _extension([MAIL, CHAT, BOUND, FILES])
#: The single-kind shape an outgoing-email extension has always had.
LEGACY_EMAIL = _extension([{"connection_type_id": "example_relay"}])


def _issues(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    _validate_connection_type_notification_blocks(
        manifest, issues=issues, manifest_path="extension.yaml"
    )
    return issues


def test_an_extension_with_a_recipient_type_and_a_channel_type_compiles() -> None:
    assert _issues(MIXED["compiled_manifest"]) == []


def test_a_notification_block_on_a_non_notification_extension_is_rejected() -> None:
    manifest = {"kind": "tool_provider", "connection_types": [MAIL]}
    assert [issue["code"] for issue in _issues(manifest)] == [
        "connection_type_notification_not_allowed"
    ]


@pytest.mark.parametrize(
    ("block", "code"),
    [
        ({"target_kind": "pager"}, "unknown_notification_target_kind"),
        (
            {"target_kind": "recipient", "channel_source": "connection"},
            "invalid_notification_channel_source",
        ),
        (
            {"target_kind": "channel", "channel_source": "elsewhere"},
            "invalid_notification_channel_source",
        ),
        ({"target_kind": "channel", "fields": ["to"]}, "invalid_connection_type_notification"),
        ({"target_kind": "channel", "ledger_channel": " "}, "invalid_connection_type_notification"),
        ("recipient", "invalid_connection_type_notification"),
    ],
)
def test_a_malformed_notification_block_is_rejected(block: Any, code: str) -> None:
    manifest = {
        "kind": "notification_provider",
        "connection_types": [{"connection_type_id": "x", "notification": block}],
    }
    assert code in {issue["code"] for issue in _issues(manifest)}


def test_each_connection_type_resolves_its_own_target_kind() -> None:
    assert capability.connection_capability(
        MIXED, "example_mail"
    ) == capability.NotificationCapability(
        target_kind="recipient", ledger_channel="email", declared=True
    )
    chat = capability.connection_capability(MIXED, "example_chat")
    assert (chat.target_kind, chat.ledger_channel, chat.channel_source) == (
        "channel",
        "chat",
        "route",
    )
    bound = capability.connection_capability(MIXED, "example_chat_bound")
    assert bound.connection_bound
    # Once one type declares a block, a type without one carries no routes.
    assert capability.connection_capability(MIXED, "example_files") is None
    assert capability.provider_target_kind(MIXED) == "mixed"
    assert capability.provider_requires_route_connection(MIXED)


def test_a_provider_with_only_a_connection_bound_channel_type_needs_a_connection() -> None:
    extension = _extension([BOUND], target_kind="channel", channel="chat")
    assert capability.provider_target_kind(extension) == "channel"
    assert capability.provider_requires_route_connection(extension)


def test_an_existing_single_kind_provider_keeps_its_behaviour() -> None:
    legacy = capability.connection_capability(LEGACY_EMAIL, "example_relay")
    assert legacy == capability.NotificationCapability(
        target_kind="recipient", ledger_channel="email"
    )
    assert not legacy.declared
    assert capability.provider_target_kind(LEGACY_EMAIL) == "recipient"
    assert not capability.provider_requires_route_connection(LEGACY_EMAIL)
    # Address fields are not policed for a provider that declares nothing.
    assert capability.route_fields_error(legacy, {}) == ""


@pytest.mark.parametrize(
    ("connection_type", "route_config", "error"),
    [
        ("example_mail", {"to": ["a@example.com"]}, ""),
        ("example_mail", {}, "notification_connection_incompatible"),
        (
            "example_mail",
            {"to": "a@example.com", "channel_id": "ops"},
            "notification_connection_incompatible",
        ),
        ("example_chat", {"channel_id": "ops"}, ""),
        ("example_chat", {"to": ["a@example.com"]}, "notification_connection_incompatible"),
        ("example_chat", {"channel": "ops"}, ""),
        ("example_chat_bound", {}, ""),
        ("example_chat_bound", {"channel": "ops"}, "notification_connection_incompatible"),
        ("example_chat_bound", {"channel_id": "ops"}, "notification_connection_incompatible"),
    ],
)
def test_route_fields_must_fit_the_connection_type(connection_type, route_config, error) -> None:
    resolved = capability.connection_capability(MIXED, connection_type)
    assert capability.route_fields_error(resolved, route_config) == error


def _row(connection_type: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "connection_id": f"conn-{connection_type}",
        "connection_type": connection_type,
        "project_id": PROJECT,
        "extension_id": EXT,
        "extension_install_id": INSTALL,
        "enabled": True,
        "display_name": f"{connection_type} connection",
        "connection_config": {"channel_label": "Operations"},
    }
    row.update(overrides)
    return row


def _check(route_config: dict[str, Any], connection: dict[str, Any] | None, **kwargs: Any):
    return check_route_connection(
        extension=kwargs.get("extension", MIXED),
        extension_key=EXT,
        provider_install_id=kwargs.get("provider_install_id", INSTALL),
        project_id=PROJECT,
        route_config=route_config,
        connection=connection,
    )


def test_a_route_without_a_connection_is_refused_for_a_mixed_provider() -> None:
    decision = _check({"to": ["a@example.com"]}, None)
    assert decision.error == "notification_connection_required"
    bound_only = _extension([BOUND], target_kind="channel", channel="chat")
    assert _check({}, None, extension=bound_only).error == "notification_connection_required"
    # A legacy provider does not need one.
    assert _check({"to": ["a@example.com"]}, None, extension=LEGACY_EMAIL).ok


@pytest.mark.parametrize(
    ("connection", "route_config"),
    [
        (_row("example_chat"), {"to": ["a@example.com"]}),
        (_row("example_mail", extension_install_id="install-b"), {"to": ["a@example.com"]}),
        (_row("example_mail", project_id="project-2"), {"to": ["a@example.com"]}),
        (_row("example_files"), {"to": ["a@example.com"]}),
        (_row("example_mail", enabled=False), {"to": ["a@example.com"]}),
        (_row("example_mail", extension_id="fixture.other"), {"to": ["a@example.com"]}),
        (_row("example_chat_bound"), {"channel_id": "ops"}),
    ],
    ids=[
        "wrong-kind",
        "other-install",
        "other-project",
        "undeclared-type",
        "disabled",
        "other-extension",
        "channel-to-bound",
    ],
)
def test_an_incompatible_connection_is_refused(connection, route_config) -> None:
    decision = _check({**route_config, "connection_id": connection["connection_id"]}, connection)
    assert decision.error == "notification_connection_incompatible"


def test_a_connection_of_another_visible_install_of_the_extension_is_refused() -> None:
    other_install = {**MIXED, "extension_install_id": "install-b"}
    connection = _row("example_mail", extension_install_id="install-b")
    decision = _check(
        {"to": ["a@example.com"], "connection_id": connection["connection_id"]},
        connection,
        extension=other_install,
        provider_install_id=INSTALL,
    )
    assert decision.error == "notification_connection_incompatible"


def test_the_listing_returns_channel_source_and_label_per_connection() -> None:
    entries = [
        {
            "provider_key": EXT,
            "connection_id": row["connection_id"],
            "enabled": True,
            **capability.notification_connection_view(MIXED, row),
        }
        for row in (
            _row("example_mail"),
            _row("example_chat"),
            _row("example_chat_bound"),
            _row("example_files"),
        )
    ]
    capability.group_provider_connections(entries)
    bound = next(entry for entry in entries if entry["connection_type"] == "example_chat_bound")
    assert bound["notification_target_kind"] == "mixed"
    assert bound["requires_connection"] is True
    assert (bound["target_kind"], bound["channel_source"], bound["channel_label"]) == (
        "channel",
        "connection",
        "Operations",
    )
    chat = next(entry for entry in entries if entry["connection_type"] == "example_chat")
    assert (chat["channel_source"], chat["channel_label"]) == ("route", "")
    files = next(entry for entry in entries if entry["connection_type"] == "example_files")
    assert files["notification_eligible"] is False
    assert [item["connection_type"] for item in bound["connections"]] == [
        "example_mail",
        "example_chat",
        "example_chat_bound",
    ]


def test_an_existing_single_kind_provider_lists_as_before() -> None:
    view = capability.notification_connection_view(LEGACY_EMAIL, _row("example_relay"))
    assert view["notification_target_kind"] == "recipient"
    assert view["requires_connection"] is False
    assert (view["target_kind"], view["channel_source"], view["channel_label"]) == (
        "recipient",
        "route",
        "",
    )


def test_a_sensitive_event_keeps_only_placeholders() -> None:
    token = "reset-token-6f1c2d"
    body = f"Hi,\n\nhttps://fs.example/reset-password?token={token}\n\nBye"
    event = {
        "event_type": "system.password_reset",
        "summary": "Password reset",
        "payload_sensitive": True,
        "payload": {"message": body, "summary": "Password reset"},
    }
    redacted = sensitive_payload.redact_event(event)
    assert token not in str(redacted)
    assert redacted["payload"]["message"].startswith("<redacted length=")
    route = sensitive_payload.redact_route(
        {"route_config": {"subject": "Password reset", "to": ["a@x.io"]}}, event
    )
    assert route["route_config"]["to"] == ["a@x.io"]
    assert "Password reset" not in str(route)
    scrubbed = sensitive_payload.scrub(
        {
            "stdout": f"echo {body}",
            "detail": f"failed: {token} link https://fs.example/reset-password?token={token}",
        },
        event,
    )
    assert token not in str(scrubbed)
    # An event that is not flagged is kept as it is.
    plain = {**event, "payload_sensitive": False}
    assert sensitive_payload.redact_event(plain) == plain
    assert sensitive_payload.scrub("x " + body, plain) == "x " + body


def test_the_delivery_request_carries_the_effect_key_at_the_top_level() -> None:
    requests: list[dict[str, Any]] = []

    def invoke(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(request)
        return {"status": "succeeded", "response": {"ok": True}}

    coordinate_extension_delivery(
        extension={},
        operation_id="deliver_notification",
        runtime_connection={"connection_id": "conn_1"},
        action_payload={"action_id": "deliver_notification", "target": {}},
        provider_payload={"provider_key": EXT},
        event={"event_id": "evt_1"},
        route_payload={"route_id": "route_1"},
        external_effect_id="effect-1",
        external_effect_claim_token="claim-1",
        idempotency_key="key-1",
        invoke=invoke,
        persist=lambda _invocation: None,
        mark_running=lambda _effect_id, _token: {"effect_id": "effect-1"},
    )
    assert requests[0]["idempotency_key"] == "key-1"
    assert requests[0]["external_effect_id"] == "effect-1"
    assert list(requests[0])[:3] == ["action", "idempotency_key", "external_effect_id"]


@pytest.mark.parametrize(
    ("action", "route_config"),
    [
        # A connection bound to one channel is the whole destination.
        ({"connection_id": "conn-bound"}, {"connection_id": "conn-bound"}),
        (
            {"connection_id": "conn-mail", "recipients": ["ops@example.com"]},
            {"connection_id": "conn-mail", "to": ["ops@example.com"]},
        ),
        (
            {"connection_id": "conn-chat", "channel_id": "ops"},
            {"connection_id": "conn-chat", "channel_id": "ops", "required_capability": "notify"},
        ),
    ],
)
def test_an_automation_delivers_through_its_named_connection(
    monkeypatch, action, route_config
) -> None:
    import logging

    from core.application.notifications import dispatch
    from core.application.workflows.reaction_runtime import dispatch_notification_from_reaction

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        dispatch,
        "dispatch_project_notification_event",
        lambda _bl, **kwargs: sent.append(kwargs) or [{"status": "delivered"}],
    )

    class _Store:
        def get_job(self, _job_id):
            return None

    result = dispatch_notification_from_reaction(
        {"event_type": "job.failed", "project_id": PROJECT, "payload": {}},
        {"type": "notify", "target": "extension_channel", "provider_key": EXT, **action},
        get_logger_only=_Store,
        err_database_not_configured="no database",
        to_json_obj=lambda value: value,
        resolve_job_workflow_id=lambda _job: "",
        logger=logging.getLogger(__name__),
    )

    assert result.get("status") != "ignored", result
    assert sent[0]["direct_routes"][0]["route_config"] == route_config


def test_the_run_url_names_the_run_page_without_any_token() -> None:
    from core.application.extensions.event_email import event_run_url, render_event_email

    job = {"event_type": "job.failed", "job_id": "job-7", "payload": {}}
    approval = {
        "event_type": "approval.requested",
        "job_id": "parent-job",
        "payload": {"approval_job_id": "approval-job"},
    }
    assert event_run_url(job, base_url="https://fs.example/") == "https://fs.example/jobs/job-7"
    # A propagated approval is decided on the approval's own job.
    assert (
        event_run_url(approval, base_url="https://fs.example")
        == "https://fs.example/jobs/approval-job"
    )
    assert event_run_url(job, base_url="") == ""
    assert (
        event_run_url(
            {"event_type": "system.password_reset", "payload": {}}, base_url="https://fs.example"
        )
        == ""
    )
    # The standard email links to the same page.
    assert (
        "Open the run: https://fs.example/jobs/job-7"
        in render_event_email(job, base_url="https://fs.example")[1]
    )
