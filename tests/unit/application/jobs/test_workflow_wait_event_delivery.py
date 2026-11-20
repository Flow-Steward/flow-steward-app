"""An extension's resume_waits: trusted scope, checked items, stored once (ADR 0071)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.application.jobs.workflow_event_wait_repository import (
    EventWaitCandidate,
    WorkflowEventWaitRepository,
    match_events_to_waits,
)
from core.application.jobs.workflow_wait_contract import WAIT_EVENT_PAYLOAD_MAX_BYTES
from core.application.jobs.workflow_wait_event_delivery import deliver_wait_events

FAKE_KEY = "sk-live-not-a-real-key"  # pragma: allowlist secret

SCOPE = {
    "extension_id": "acme.crawler",
    "account_id": "00000000-0000-0000-0000-000000000002",
    "project_id": "11111111-1111-1111-1111-111111111111",
    "extension_install_id": "install-1",
}


class _Store:
    """Records deliveries; a (key, type) already stored is a duplicate."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict] = []
        self.seen: set[tuple[str, str]] = set()

    def __call__(self, _db):
        return self

    def deliver(self, **kwargs):
        if self.failure is not None:
            raise self.failure
        self.calls.append(kwargs)
        stored = []
        for item in kwargs["items"]:
            key = (item.correlation_key, item.event_type)
            stored.append(key not in self.seen)
            self.seen.add(key)
        return stored


def _invocation(resume_waits, *, ok: bool = True, **response) -> dict:
    return {
        "status": "succeeded",
        "response": {"ok": ok, "result": {}, "resume_waits": resume_waits, **response},
    }


def _deliver(invocation: dict, store: _Store, **scope) -> dict | None:
    return deliver_wait_events(
        invocation, db=object(), repository_factory=store, **{**SCOPE, **scope}
    )


def test_items_are_stored_with_the_trusted_scope_never_the_responses() -> None:
    store = _Store()
    invocation = _invocation(
        [
            {
                "correlation_key": "run-42",
                "event_type": "run_completed",
                "payload": {"rows": 3},
                # An extension cannot name another project, account or source.
                "project_id": "22222222-2222-2222-2222-222222222222",
                "source": "acme.other",
            }
        ]
    )

    outcome = _deliver(invocation, store)

    assert outcome == {"stored": 1, "duplicates": [], "rejected": []}
    call = store.calls[0]
    assert call["project_id"] == SCOPE["project_id"]
    assert call["account_id"] == SCOPE["account_id"]
    assert call["source"] == "acme.crawler"
    assert call["extension_install_id"] == "install-1"
    assert call["items"][0].correlation_key == "run-42"
    assert dict(call["items"][0].payload) == {"rows": 3}
    assert invocation["wait_events"] == outcome
    assert invocation["status"] == "succeeded"


def test_a_second_delivery_of_the_same_key_and_type_is_a_logged_duplicate(caplog) -> None:
    store = _Store()
    correlator = "fixture-correlator-42"
    item = {"correlation_key": correlator, "event_type": "run_completed"}
    _deliver(_invocation([item]), store)

    with caplog.at_level("INFO"):
        outcome = _deliver(_invocation([item, {**item, "event_type": "run_failed"}]), store)

    assert outcome == {"stored": 1, "duplicates": [0], "rejected": []}
    assert store.calls[1]["items"][0].correlation_key == correlator
    assert "duplicate event at resume_waits index 0 ignored" in caplog.text
    for field in (correlator, item["event_type"], SCOPE["extension_id"]):
        assert field not in caplog.text
        assert all(field not in repr(record.args) for record in caplog.records)


@pytest.mark.parametrize(
    ("item", "code"),
    [
        ("run-42", "resume_wait_invalid"),
        ({"event_type": "done"}, "resume_wait_correlation_key_invalid"),
        ({"correlation_key": "", "event_type": "done"}, "resume_wait_correlation_key_invalid"),
        ({"correlation_key": True, "event_type": "done"}, "resume_wait_correlation_key_invalid"),
        (
            {"correlation_key": "k" * 257, "event_type": "done"},
            "resume_wait_correlation_key_invalid",
        ),
        ({"correlation_key": "a\nb", "event_type": "done"}, "resume_wait_correlation_key_invalid"),
        ({"correlation_key": "k", "event_type": "Done"}, "resume_wait_event_type_invalid"),
        ({"correlation_key": "k"}, "resume_wait_event_type_invalid"),
        (
            {"correlation_key": "k", "event_type": "done", "payload": [1]},
            "resume_wait_payload_invalid",
        ),
        (
            {"correlation_key": "k", "event_type": "done", "payload": {"n": float("nan")}},
            "resume_wait_payload_invalid",
        ),
        (
            {
                "correlation_key": "k",
                "event_type": "done",
                "payload": {"blob": "x" * WAIT_EVENT_PAYLOAD_MAX_BYTES},
            },
            "resume_wait_payload_too_large",
        ),
    ],
)
def test_a_malformed_item_is_refused_without_failing_the_action(item, code) -> None:
    store = _Store()
    invocation = _invocation([item, {"correlation_key": "ok", "event_type": "done"}])

    outcome = _deliver(invocation, store)

    assert [row["code"] for row in outcome["rejected"]] == [code]
    assert outcome["stored"] == 1
    assert invocation["status"] == "succeeded"


def test_the_payload_is_redacted_before_it_is_stored() -> None:
    store = _Store()
    _deliver(
        _invocation(
            [
                {
                    "correlation_key": "k",
                    "event_type": "done",
                    "payload": {"api_key": FAKE_KEY, "rows": 2},
                }
            ]
        ),
        store,
    )
    stored = dict(store.calls[0]["items"][0].payload)
    assert stored["rows"] == 2
    assert FAKE_KEY not in json.dumps(stored)


def test_without_a_project_context_nothing_is_stored() -> None:
    store = _Store()
    outcome = _deliver(
        _invocation([{"correlation_key": "k", "event_type": "done"}]), store, project_id=""
    )
    assert store.calls == []
    assert outcome["rejected"][0]["code"] == "resume_waits_scope_missing"


def test_a_storage_failure_fails_the_action_so_the_sender_retries(caplog) -> None:
    invocation = _invocation([{"correlation_key": "k", "event_type": "done"}])
    diagnostic = "fixture database diagnostic detail"

    with caplog.at_level("WARNING"):
        outcome = _deliver(invocation, _Store(failure=RuntimeError(diagnostic)))

    assert outcome == {"stored": 0, "duplicates": [], "rejected": []}
    assert invocation["status"] == "failed"
    assert invocation["detail"] == "wait_event_delivery_failed"
    assert "resume_waits not stored (error_type=RuntimeError)" in caplog.text
    for field in (diagnostic, SCOPE["extension_id"]):
        assert field not in caplog.text
        assert all(field not in repr(record.args) for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize(
    "invocation",
    [
        {"status": "succeeded", "response": {"ok": True, "result": {}}},
        _invocation([{"correlation_key": "k", "event_type": "done"}], ok=False),
        {**_invocation([{"correlation_key": "k", "event_type": "done"}]), "status": "failed"},
    ],
    ids=["no-resume-waits", "ok-false", "failed-invocation"],
)
def test_only_a_successful_response_with_resume_waits_delivers(invocation) -> None:
    store = _Store()
    assert _deliver(invocation, store) is None
    assert store.calls == []


def test_one_event_wakes_one_wait_and_a_wait_takes_one_event() -> None:
    candidates = [
        EventWaitCandidate("e1", "job-a", "wa"),
        EventWaitCandidate("e1", "job-b", "wb"),  # same key parked twice
        EventWaitCandidate("e2", "job-a", "wa"),  # second type for the same wait
        EventWaitCandidate("e2", "job-b", "wb"),
        EventWaitCandidate("e3", "job-a", "wa"),
    ]

    matched = match_events_to_waits(candidates)

    assert [(m.event_id, m.job_id) for m in matched] == [("e1", "job-a"), ("e2", "job-b")]


class _ClaimCursor:
    """Returns one stored event paired with a parked job; records the writes."""

    def __init__(self, received_at: object) -> None:
        self.received_at = received_at
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self) -> _ClaimCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.calls.append((sql, params))

    def fetchall(self) -> list[tuple]:
        wait = json.dumps({"wait_id": "w-1", "kind": "event"})
        return [("e1", "job-1", "w-1", "done", "k", "{}", self.received_at, wait, 0)]

    def fetchone(self) -> None:
        return None  # the run left awaiting_event meanwhile: nothing is published


def _claimed_received_at(received_at: object) -> str:
    cursor = _ClaimCursor(received_at)
    conn = SimpleNamespace(cursor=lambda: cursor, commit=lambda: None, rollback=lambda: None)
    WorkflowEventWaitRepository(SimpleNamespace(conn=conn)).claim_delivered_events(limit=10)
    update = next(params for sql, params in cursor.calls if "UPDATE jobs" in sql)
    return json.loads(update[0])["resume_value"]["received_at"]


@pytest.mark.parametrize(
    ("received_at", "expected"),
    [
        (
            datetime(2026, 11, 15, 9, 20, 45, 56343, tzinfo=UTC),
            "2026-11-15T09:20:45.056343+00:00",
        ),
        (
            datetime(2026, 11, 15, 11, 20, 45, tzinfo=timezone(timedelta(hours=2))),
            "2026-11-15T09:20:45+00:00",
        ),
    ],
    ids=["utc", "session-time-zone"],
)
def test_the_event_received_time_is_iso_8601_utc(received_at, expected) -> None:
    # PostgreSQL text ('2026-11-15 09:20:45.056343+00') is not what the run shows.
    assert _claimed_received_at(received_at) == expected
