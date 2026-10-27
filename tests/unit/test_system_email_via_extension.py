"""System email and workflow emails go out through a connected email extension."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.auth.user_admin.notifications import (
    SIGNUP_DELIVERY_WARNING,
    notify_signup_non_blocking,
)
from core.application.extensions import system_email
from core.application.extensions.event_email import render_event_email
from core.application.notifications import delivery as notification_delivery
from core.application.notifications import dispatch as notification_dispatch
from core.application.workflows.reaction_runtime import _notification_recipients


def _email_extension(**overrides: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "kind": "notification_provider",
        "external_effects": [{"observability": {"target_kind": "recipient"}}],
        **overrides,
    }


class _Store:
    def __init__(self, connections: list[dict], extensions: dict[str, dict]) -> None:
        self.connections = connections
        self.extensions = extensions

    def list_enabled_extension_connections(self) -> list[dict]:
        return list(self.connections)

    def get_extension(self, key: str) -> dict | None:
        return self.extensions.get(key)


def _connection(connection_id: str, extension_id: str) -> dict[str, Any]:
    return {
        "connection_id": connection_id,
        "extension_id": extension_id,
        "project_id": "11111111-1111-1111-1111-111111111111",
        "account_id": "22222222-2222-2222-2222-222222222222",
    }


def test_the_oldest_email_connection_is_used_and_chat_extensions_are_not() -> None:
    store = _Store(
        [
            _connection("chat-1", "acme.chat"),
            _connection("mail-old", "acme.mail"),
            _connection("mail-new", "acme.mail"),
        ],
        {
            "acme.chat": _email_extension(
                external_effects=[{"observability": {"target_kind": "channel"}}]
            ),
            "acme.mail": _email_extension(),
        },
    )

    found = system_email.find_email_connection(store)

    assert found is not None
    assert found[0]["connection_id"] == "mail-old"
    assert found[1] == "acme.mail"


@pytest.mark.parametrize(
    "extension",
    [
        _email_extension(enabled=False),
        _email_extension(kind="tool_provider"),
    ],
)
def test_a_disabled_or_non_notification_extension_sends_no_email(extension) -> None:
    store = _Store([_connection("mail", "acme.mail")], {"acme.mail": extension})

    assert system_email.find_email_connection(store) is None


def test_a_password_reset_is_delivered_through_the_connection(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    def deliver(bl, **kwargs):
        sent.append(kwargs)
        return {"status": "delivered"}

    monkeypatch.setattr(notification_delivery, "deliver_direct_notification", deliver)
    store = _Store([_connection("mail", "acme.mail")], {"acme.mail": _email_extension()})

    result = system_email.send_password_reset_email(
        recipient="Anna@Example.com",
        reset_url="https://fs.example/reset-password?token=t",
        expires_in_minutes=30,
        username="Anna",
        locale="lt-LT",
        bl=store,
    )

    assert result == {"ok": True, "reason": "sent", "provider": "acme.mail"}
    assert sent[0]["recipients"] == ["anna@example.com"]
    assert sent[0]["event_type"] == "system.password_reset"
    assert sent[0]["subject"] == "Slaptažodžio atkūrimas"
    assert "https://fs.example/reset-password?token=t" in sent[0]["message"]
    assert sent[0]["connection"]["connection_id"] == "mail"


def test_a_failed_delivery_is_reported_without_raising(monkeypatch) -> None:
    monkeypatch.setattr(
        notification_delivery,
        "deliver_direct_notification",
        lambda bl, **kwargs: {"status": "failed", "error_text": "smtp refused"},
    )
    store = _Store([_connection("mail", "acme.mail")], {"acme.mail": _email_extension()})

    result = system_email.send_signup_notification_email(
        recipient="new@example.com", display_name="New", bl=store
    )

    assert result == {"ok": False, "reason": "send_failed", "provider": "acme.mail"}


def test_an_invalid_recipient_is_refused_before_anything_is_sent() -> None:
    assert system_email.send_password_reset_email(
        recipient="not-an-address", reset_url="u", expires_in_minutes=5, bl=_Store([], {})
    ) == {"ok": False, "reason": "invalid_recipient"}


def test_messages_fall_back_to_english_and_name_an_anonymous_reader() -> None:
    subject, body = system_email.render_system_email(
        "email_change_verification",
        locale="de",
        new_email="new@example.com",
        verification_url="https://fs.example/verify-email-change?token=v",
        expires_in_minutes=15,
    )

    assert subject == "Verify your new sign-in email"
    assert body.startswith("Hi there,")
    assert "new@example.com" in body
    assert "15 minutes" in body


def test_availability_is_cached_briefly() -> None:
    calls: list[int] = []

    class _CountingStore(_Store):
        def list_enabled_extension_connections(self) -> list[dict]:
            calls.append(1)
            return []

    store = _CountingStore([], {})
    ticks = iter([1000.0, 1010.0, 1040.0])
    system_email._availability_cache.clear()

    assert system_email.system_email_available(store, clock=lambda: next(ticks)) is False
    assert system_email.system_email_available(store, clock=lambda: next(ticks)) is False
    assert system_email.system_email_available(store, clock=lambda: next(ticks)) is False
    assert len(calls) == 2
    system_email._availability_cache.clear()


def test_no_email_extension_means_no_signup_warning_but_a_failed_send_warns() -> None:
    def no_email(**_kwargs):
        return {"ok": False, "reason": "disabled_or_unconfigured"}

    def failed(**_kwargs):
        return {"ok": False, "reason": "send_failed"}

    common = {"user_id": "u1", "recipient": "a@example.com", "display_name": "", "locale": "en"}
    assert notify_signup_non_blocking(no_email, **common) is None
    assert notify_signup_non_blocking(failed, **common) == SIGNUP_DELIVERY_WARNING


@pytest.mark.parametrize(
    ("event_type", "subject"),
    [
        ("job.completed", "✅ Workflow completed: invoice_sync"),
        ("job.failed", "❌ Workflow failed: invoice_sync"),
        ("approval.requested", "⚠️ Needs your approval: invoice_sync"),
        ("job.step_blocked", "⚠️ Step blocked: invoice_sync"),
        ("custom.event", "🔔 Workflow notification: invoice_sync"),
    ],
)
def test_every_workflow_event_has_one_layout_and_its_own_heading(event_type, subject) -> None:
    rendered_subject, body = render_event_email(
        {
            "event_type": event_type,
            "workflow_id": "invoice_sync",
            "job_id": "job-7",
            "step_id": "post_invoices",
            "payload": {"task_summary": "Sync invoices", "error": "Timeout"},
        },
        base_url="https://fs.example/",
    )

    assert rendered_subject == subject
    assert "Workflow: invoice_sync" in body
    assert "Task: Sync invoices" in body
    assert "Step: post_invoices" in body
    assert "Error: Timeout" in body
    assert "Open the run: https://fs.example/jobs/job-7" in body


def test_an_automation_resend_is_titled_by_the_event_that_triggered_it() -> None:
    subject, body = render_event_email(
        {
            "event_type": "workflow.automation.notify",
            "workflow_id": "wf",
            "payload": {"source_event_type": "job.failed", "message": "Check the supplier feed"},
        }
    )

    assert subject == "❌ Workflow failed: wf"
    assert body.splitlines()[2] == "Check the supplier feed"
    assert "Open the run" not in body


def test_an_email_extension_gets_the_standard_text_and_a_chat_one_does_not() -> None:
    event = {"event_type": "job.completed", "workflow_id": "wf", "payload": {"message": "hi"}}

    route_config: dict[str, Any] = {}
    emailed = notification_delivery._with_standard_email(_email_extension(), event, route_config)
    assert route_config["subject"] == "✅ Workflow completed: wf"
    assert emailed["payload"]["message"].startswith("✅ Workflow completed")

    kept_subject = {"subject": "Nightly report"}
    notification_delivery._with_standard_email(_email_extension(), event, kept_subject)
    assert kept_subject["subject"] == "Nightly report"

    chat = _email_extension(external_effects=[{"observability": {"target_kind": "channel"}}])
    assert notification_delivery._with_standard_email(chat, event, {}) is event

    system = {"event_type": "system.password_reset", "payload": {"message": "reset"}}
    assert notification_delivery._with_standard_email(_email_extension(), system, {}) is system


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ops@example.com, anna@example.com", ["ops@example.com", "anna@example.com"]),
        (["ops@example.com", " ", "x@example.com"], ["ops@example.com", "x@example.com"]),
        (None, []),
    ],
)
def test_automation_recipients_accept_a_list_or_comma_separated_text(value, expected) -> None:
    assert _notification_recipients(value) == expected


def test_a_direct_message_names_its_connection_and_recipient(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def deliver(**kwargs):
        captured.update(kwargs)
        return {"status": "delivered"}

    monkeypatch.setattr(notification_delivery, "_deliver_via_extension", deliver)

    notification_delivery.deliver_direct_notification(
        object(),
        connection=_connection("mail", "acme.mail"),
        extension_key="acme.mail",
        event_type="system.password_reset",
        recipients=["anna@example.com"],
        subject="Password reset",
        message="Open the link",
    )

    assert captured["provider"]["provider_config"] == {"extension_key": "acme.mail"}
    assert captured["route"]["route_config"] == {
        "connection_id": "mail",
        "to": ["anna@example.com"],
        "subject": "Password reset",
    }
    assert captured["event"]["event_type"] == "system.password_reset"
    assert captured["event"]["payload"]["message"] == "Open the link"
    assert captured["project_id"] == "11111111-1111-1111-1111-111111111111"


def test_without_a_given_store_the_shared_integration_store_is_used(monkeypatch) -> None:
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    store = _Store([_connection("mail", "acme.mail")], {"acme.mail": _email_extension()})
    monkeypatch.setattr(
        PlatformIntegrationStore, "get_shared_instance", classmethod(lambda cls: store)
    )
    system_email._availability_cache.clear()

    assert system_email.system_email_available() is True
    system_email._availability_cache.clear()


def test_the_integration_store_can_find_and_deliver() -> None:
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    for method in ("list_enabled_extension_connections", "get_extension", "get_project_connection"):
        assert hasattr(PlatformIntegrationStore, method), method


class _ReactionStore:
    def __init__(self, rules: list[dict]) -> None:
        self.rules = rules

    def list_workflow_reactions(self, workflow_id, enabled_only=True):
        return [rule for rule in self.rules if rule["workflow_id"] == workflow_id]

    def get_job(self, job_id):
        return {"id": job_id, "project_id": "p-1", "meta": {"workflow_id": "wf"}}


def _email_rule(event_type: str) -> dict[str, Any]:
    return {
        "workflow_id": "wf",
        "reaction_key": f"mail_{event_type}",
        "event_type": event_type,
        "enabled": True,
        "scope_kind": "workflow",
        "conditions": {},
        "action": {
            "type": "notify",
            "target": "extension_channel",
            "provider_key": "acme.mail",
            "recipients": ["ops@example.com"],
        },
    }


def test_a_finished_job_runs_its_workflow_email_automation(monkeypatch) -> None:
    from core.application.workflows.job_lifecycle_reactions import run_job_lifecycle_reactions

    dispatched: list[dict[str, Any]] = []

    def dispatch(bl, **kwargs):
        dispatched.append(kwargs)
        return [{"status": "delivered"}]

    monkeypatch.setattr(notification_dispatch, "dispatch_project_notification_event", dispatch)
    store = _ReactionStore([_email_rule("job.completed"), _email_rule("job.failed")])

    results = run_job_lifecycle_reactions(
        store,
        event_type="job.completed",
        project_id="p-1",
        workflow_id="wf",
        job_id="job-1",
        event_id="evt",
        payload={"status": "completed"},
        to_json_obj=lambda value: value,
    )

    assert [result.get("executed") for result in results] == [True]
    assert len(dispatched) == 1
    route = dispatched[0]["direct_routes"][0]
    assert route["provider_key"] == "acme.mail"
    assert route["route_config"] == {"to": ["ops@example.com"]}
    assert dispatched[0]["payload"]["source_event_type"] == "job.completed"


def test_waiting_for_approval_runs_the_approval_waiting_automation_only(monkeypatch) -> None:
    from core.application.workflows.job_lifecycle_reactions import run_job_lifecycle_reactions

    dispatched: list[str] = []
    monkeypatch.setattr(
        notification_dispatch,
        "dispatch_project_notification_event",
        lambda bl, **kwargs: dispatched.append(kwargs["event_type"]) or [{"status": "delivered"}],
    )

    run_job_lifecycle_reactions(
        _ReactionStore(
            [
                _email_rule("job.approval_waiting"),
                # Retired and producer names never fire as automation events.
                _email_rule("job.gate_waiting"),
                _email_rule("job.awaiting_approval"),
            ]
        ),
        event_type="job.awaiting_approval",
        project_id="p-1",
        workflow_id="wf",
        job_id="job-1",
        event_id="evt",
        payload={"wait_id": "w-1", "approval_id": "a-1", "reason": "authored"},
        to_json_obj=lambda value: value,
    )

    assert dispatched == ["job.approval_waiting"]


def test_a_reaction_failure_never_reaches_the_job() -> None:
    from core.application.workflows.job_lifecycle_reactions import run_job_lifecycle_reactions

    class _Broken:
        def list_workflow_reactions(self, *_args, **_kwargs):
            raise RuntimeError("db down")

    results = run_job_lifecycle_reactions(
        _Broken(),
        event_type="job.failed",
        project_id="p-1",
        workflow_id="wf",
        job_id="job-1",
        event_id="evt",
        payload={},
        to_json_obj=lambda value: value,
    )

    assert results
    assert results[0]["status"] == "failed"
