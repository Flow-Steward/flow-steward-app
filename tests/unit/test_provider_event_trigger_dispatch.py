"""A provider event starts the workflows bound to it, and only those."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from core.application.workflows import provider_event_trigger_dispatch as dispatch

PROVIDER = "acme.telegram"
EVENT = "telegram.message.received"


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "trigger_id": "trg-1",
        "workflow_id": "wf-1",
        "project_id": "proj-1",
        "provider_key": PROVIDER,
        "trigger_type_id": "message_received",
        "display_name": "New Telegram message",
        "event_name": EVENT,
        "trigger_config_jsonb": {},
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("row", "event_type", "payload", "expected"),
    [
        (_row(), EVENT, {"chat_id": "7"}, True),
        (_row(event_name=""), EVENT, {}, False),
        (_row(), "job.completed", {}, False),
        (_row(trigger_config_jsonb={"chat_id": "7"}), EVENT, {"chat_id": 7}, True),
        (_row(trigger_config_jsonb={"chat_id": "7"}), EVENT, {"chat_id": "8"}, False),
        (_row(trigger_config_jsonb={"chat_id": "7"}), EVENT, {}, False),
        (_row(trigger_config_jsonb={"chat_id": ""}), EVENT, {"chat_id": "8"}, True),
        (_row(trigger_config_jsonb={"event_name": "x"}), EVENT, {}, True),
    ],
)
def test_trigger_matches_event(row, event_type, payload, expected):
    assert dispatch.trigger_matches_event(row, event_type=event_type, payload=payload) is expected


class _Harness:
    """Replaces everything below the dispatcher so the decisions are what is tested."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self.claims: set[str] = set()
        self.validation_ok = True
        self.active = True
        self.run_cfg: dict[str, Any] = {"project_name": "Demo"}
        monkeypatch.setattr(
            dispatch, "list_enabled_provider_triggers_for_provider", lambda bl, **_: rows
        )
        monkeypatch.setattr(dispatch, "provider_trigger_admission", self._admission)
        monkeypatch.setattr(
            dispatch,
            "resolve_project_run_config_for_workflow_project",
            lambda *_a, **_k: ("proj-1", self.run_cfg),
        )
        monkeypatch.setattr(dispatch, "WorkflowInterfaceService", self._interface)
        monkeypatch.setattr(dispatch, "_claim_provider_webhook_delivery", self._claim)
        monkeypatch.setattr(dispatch, "_mark_provider_webhook_delivery_accepted", lambda *a, **k: 1)
        monkeypatch.setattr(dispatch, "_mark_provider_webhook_delivery_failed", lambda *a, **k: 1)
        monkeypatch.setattr(dispatch, "workflow_secret_launch_snapshot", lambda *a, **k: [])
        monkeypatch.setattr(dispatch, "enqueue_workflow_start", self._enqueue)

    @contextmanager
    def _admission(self, _bl, _row):
        yield self.active

    def _interface(self):
        outer = self

        class _Service:
            def validate_inputs_for_workflow_id(self, _workflow_id, inputs, *, channel=""):
                return SimpleNamespace(
                    ok=outer.validation_ok,
                    errors=[{"code": "bad"}],
                    inputs=inputs,
                    interface={"enabled": False},
                )

        return _Service()

    def _claim(self, _bl, *, delivery_key, **_):
        if delivery_key in self.claims:
            return {"claimed": False, "job_id": "job-existing"}
        self.claims.add(delivery_key)
        return {"claimed": True}

    def _enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return f"job-{len(self.enqueued)}"


def _start(rows, monkeypatch, **overrides):
    harness = _Harness(monkeypatch, rows)
    for name, value in overrides.items():
        setattr(harness, name, value)
    args: dict[str, Any] = {
        "enqueue_task_fn": lambda **_: None,
        "provider_key": PROVIDER,
        "project_id": "proj-1",
        "event_type": EVENT,
        "payload": {"chat_id": "7", "text": "hi"},
        "source_id": "100",
    }
    return harness, lambda **extra: dispatch.start_provider_event_workflows(
        object(), **{**args, **extra}
    )


def test_matching_trigger_starts_workflow_with_payload_as_inputs(monkeypatch):
    harness, run = _start([_row()], monkeypatch)
    outcomes = run()
    assert [item["status"] for item in outcomes] == ["accepted"]
    started = harness.enqueued[0]
    assert started["selected_workflow_id"] == "wf-1"
    assert started["inputs"]["text"] == "hi"
    assert started["inputs"]["event_name"] == EVENT
    assert started["extra_state"]["provider_trigger_id"] == "trg-1"


def test_other_project_other_event_or_other_chat_starts_nothing(monkeypatch):
    rows = [
        _row(trigger_id="a", project_id="proj-2"),
        _row(trigger_id="b", event_name="job.completed"),
        _row(trigger_id="c", trigger_config_jsonb={"chat_id": "99"}),
    ]
    harness, run = _start(rows, monkeypatch)
    assert run() == []
    assert harness.enqueued == []


def test_same_event_delivered_twice_starts_one_job(monkeypatch):
    harness, run = _start([_row()], monkeypatch)
    first = run()
    second = run()
    assert first[0]["status"] == "accepted"
    assert second[0]["status"] == "deduplicated"
    assert len(harness.enqueued) == 1


def test_two_messages_with_their_own_ids_start_two_jobs(monkeypatch):
    harness, run = _start([_row()], monkeypatch)
    run(source_id="100")
    run(source_id="101")
    assert len(harness.enqueued) == 2


def test_inactive_trigger_and_invalid_inputs_do_not_start(monkeypatch):
    harness, run = _start([_row()], monkeypatch, active=False)
    assert run()[0]["reason"] == "trigger_inactive"
    harness, run = _start([_row()], monkeypatch, validation_ok=False)
    outcome = run()[0]
    assert outcome["status"] == "rejected"
    assert harness.enqueued == []


def test_one_failing_trigger_does_not_stop_the_next(monkeypatch):
    rows = [_row(trigger_id="a", workflow_id="wf-a"), _row(trigger_id="b", workflow_id="wf-b")]
    harness, run = _start(rows, monkeypatch)
    original = harness._enqueue

    def flaky(**kwargs):
        if kwargs["selected_workflow_id"] == "wf-a":
            raise RuntimeError("queue down")
        return original(**kwargs)

    monkeypatch.setattr(dispatch, "enqueue_workflow_start", flaky)
    statuses = {item["trigger_id"]: item["status"] for item in run()}
    assert statuses == {"a": "failed", "b": "accepted"}


def test_without_enqueue_function_or_event_type_nothing_happens(monkeypatch):
    _harness, run = _start([_row()], monkeypatch)
    assert run(enqueue_task_fn=None) == []
    assert run(event_type="") == []
