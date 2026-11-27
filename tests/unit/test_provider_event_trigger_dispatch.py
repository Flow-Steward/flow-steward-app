"""A provider event starts the workflows bound to it, and only those."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.application.workflows import provider_event_delivery as delivery
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
        (_row(trigger_config_jsonb={"approved": True}), EVENT, {"approved": True}, True),
        (_row(trigger_config_jsonb={"approved": True}), EVENT, {"approved": False}, False),
        (_row(trigger_config_jsonb={"approved": True}), EVENT, {}, False),
        (_row(trigger_config_jsonb={"approved": False}), EVENT, {"approved": "false"}, True),
    ],
)
def test_trigger_matches_event(row, event_type, payload, expected):
    assert dispatch.trigger_matches_event(row, event_type=event_type, payload=payload) is expected


class _Delivery:
    """In-memory stand-in for the event inbox, dispatch records and job creation.

    The database rules it mirrors (one event per source_id, one record per
    delivery key, one job per record) are tested against PostgreSQL in
    tests/integration/test_connection_poll_delivery_postgres.py.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
        self.events: set[str] = set()
        self.records: dict[str, dict[str, Any]] = {}
        self.enqueued: list[dict[str, Any]] = []
        self.active = True
        self.validation_ok = True
        self.fail_workflows: set[str] = set()
        monkeypatch.setattr(
            dispatch, "list_enabled_provider_triggers_for_provider", lambda bl, **_: rows
        )
        monkeypatch.setattr(dispatch, "record_provider_event", self._record)
        monkeypatch.setattr(delivery, "_load_record", lambda _bl, key: dict(self.records[key]))
        monkeypatch.setattr(delivery, "_mark_failed", self._mark_failed)
        monkeypatch.setattr(delivery, "_record_attempt_failure", lambda *_a, **_k: None)
        monkeypatch.setattr(delivery, "transaction_after_commit_supported", lambda _conn: False)
        from core.application.registry import workflow_contract_service, workflow_secret_defaults
        from core.application.workflows import (
            workflow_entrypoint_repository,
            workflow_start_runtime,
        )

        monkeypatch.setattr(
            workflow_entrypoint_repository,
            "resolve_project_run_config_for_workflow_project",
            lambda *_a, **_k: ("proj-1", {"project_name": "Demo"}),
        )
        monkeypatch.setattr(workflow_contract_service, "WorkflowInterfaceService", self._interface)
        monkeypatch.setattr(
            workflow_secret_defaults, "workflow_secret_launch_snapshot", lambda *a, **k: []
        )
        monkeypatch.setattr(workflow_start_runtime, "enqueue_workflow_start", self._enqueue)

    def _record(
        self,
        _bl,
        *,
        project_id,
        provider_key,
        source_id,
        event_type,
        payload,
        triggers,
        binding=None,
    ):
        if source_id in self.events:
            return delivery.RecordedEvent(new=False)
        self.events.add(source_id)
        keys = []
        for row in triggers:
            key = delivery.provider_trigger_delivery_key(
                project_id,
                row["workflow_id"],
                delivery.provider_trigger_task_id(provider_key, source_id),
            )
            self.records[key] = {
                "delivery_key": key,
                "project_id": project_id,
                "provider_key": provider_key,
                "workflow_id": row["workflow_id"],
                "trigger_id": row["trigger_id"],
                "trigger_display_name": row.get("display_name"),
                "trigger_type_id": row.get("trigger_type_id"),
                "status": "pending",
                "event_type": event_type,
                "payload": payload,
                "source_id": source_id,
            }
            keys.append(key)
        return delivery.RecordedEvent(new=True, delivery_keys=keys)

    def _mark_failed(self, _bl, key, reason):
        self.records[key].update(status="failed", reason=reason)

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

    def _enqueue(self, **kwargs):
        if kwargs["selected_workflow_id"] in self.fail_workflows:
            raise RuntimeError("queue down")
        record = next(
            item
            for item in self.records.values()
            if item["workflow_id"] == kwargs["selected_workflow_id"]
            and item["source_id"] == kwargs["source_event_id"]
        )
        if not self.active:
            raise delivery.DispatchRejected("trigger_inactive")
        job_id = f"job-{len(self.enqueued) + 1}"
        assert callable(kwargs["job_created_hook"])
        self.enqueued.append(kwargs)
        record.update(status="accepted", job_id=job_id)
        return job_id


def _start(rows, monkeypatch, **overrides):
    harness = _Delivery(monkeypatch, rows)
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
    bl = SimpleNamespace(conn=object())
    return harness, lambda **extra: dispatch.start_provider_event_workflows(bl, **{**args, **extra})


def test_matching_trigger_starts_workflow_with_payload_as_inputs(monkeypatch):
    harness, run = _start([_row()], monkeypatch)
    outcomes = run()
    assert [item["status"] for item in outcomes] == ["accepted"]
    started = harness.enqueued[0]
    assert started["selected_workflow_id"] == "wf-1"
    assert started["inputs"]["text"] == "hi"
    assert started["inputs"]["event_name"] == EVENT
    assert started["extra_state"]["provider_trigger_id"] == "trg-1"
    assert started["extra_state"]["provider_trigger_type_id"] == "message_received"
    assert started["task_summary"] == "New Telegram message"
    assert started["task_id"] == f"provider-trigger:{PROVIDER}:100"


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
    assert second == []
    assert len(harness.enqueued) == 1


def test_two_messages_with_their_own_ids_start_two_jobs(monkeypatch):
    harness, run = _start([_row()], monkeypatch)
    run(source_id="100")
    run(source_id="101")
    assert len(harness.enqueued) == 2


def test_event_without_provider_id_is_keyed_by_its_payload(monkeypatch):
    harness, run = _start([_row()], monkeypatch)
    run(source_id="")
    run(source_id="")
    assert len(harness.enqueued) == 1


def test_inactive_trigger_and_invalid_inputs_do_not_start(monkeypatch):
    harness, run = _start([_row()], monkeypatch, active=False)
    outcome = run()[0]
    assert outcome["reason"] == "trigger_inactive"
    assert next(iter(harness.records.values()))["status"] == "failed"
    harness, run = _start([_row()], monkeypatch, validation_ok=False)
    outcome = run()[0]
    assert outcome["status"] == "rejected"
    assert harness.enqueued == []


def test_one_failing_trigger_does_not_stop_the_next(monkeypatch):
    rows = [_row(trigger_id="a", workflow_id="wf-a"), _row(trigger_id="b", workflow_id="wf-b")]
    _harness, run = _start(rows, monkeypatch, fail_workflows={"wf-a"})
    statuses = {item["trigger_id"]: item["status"] for item in run()}
    assert statuses == {"a": "failed", "b": "accepted"}


def test_without_enqueue_function_or_event_type_nothing_happens(monkeypatch):
    _harness, run = _start([_row()], monkeypatch)
    assert run(enqueue_task_fn=None) == []
    assert run(event_type="") == []


def test_inside_an_open_transaction_starts_wait_for_its_commit(monkeypatch):
    harness, run = _start([_row()], monkeypatch)
    registered: list[Any] = []
    monkeypatch.setattr(delivery, "transaction_after_commit_supported", lambda _conn: True)
    monkeypatch.setattr(
        delivery, "register_transaction_after_commit", lambda _conn, fn: registered.append(fn)
    )
    assert [item["status"] for item in run()] == ["pending"]
    assert harness.enqueued == []
    [after_commit] = registered
    assert [item["status"] for item in after_commit()] == ["accepted"]
    assert len(harness.enqueued) == 1


def test_the_start_runs_in_the_events_account(monkeypatch):
    """No request or job is bound on the poll, sweep or public webhook path: the
    event's account must name the registry reads of the start (ADR 0020, C5)."""
    from core.application.registry import workflow_contract_service
    from core.application.registry.workflow_account import bound_workflow_account

    harness, run = _start([_row()], monkeypatch)
    monkeypatch.setattr(
        delivery,
        "_load_record",
        lambda _bl, key: {**harness.records[key], "account_id": "acct-event"},
    )
    bound: list[str | None] = []

    class _Service:
        def validate_inputs_for_workflow_id(self, _workflow_id, inputs, *, channel=""):
            bound.append(bound_workflow_account())
            return SimpleNamespace(ok=True, errors=[], inputs=inputs, interface={})

    monkeypatch.setattr(workflow_contract_service, "WorkflowInterfaceService", _Service)

    assert [item["status"] for item in run()] == ["accepted"]
    assert bound == ["acct-event"]


def test_the_dispatch_record_carries_the_events_account():
    executed: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, _params=None):
            executed.append(sql)

        def fetchone(self):
            return tuple(f"v-{column}" for column in delivery._RECORD_COLUMNS)

    bl = SimpleNamespace(conn=SimpleNamespace(cursor=lambda: _Cursor()))
    record = delivery._load_record(bl, "key-1")

    assert "i.account_id" in executed[0]
    assert record["account_id"] == "v-account_id"
    assert record["trigger_type_id"] == "v-trigger_type_id"
