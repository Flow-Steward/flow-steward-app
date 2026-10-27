"""The job row mirrors the pause the checkpoint names (T21, T27)."""

from __future__ import annotations

from types import SimpleNamespace

from core.application.jobs.workflow_wait_projection import (
    STATUS_AWAITING_APPROVAL,
    STATUS_AWAITING_TIMER,
    durable_wait_from_state,
    interrupt_matches_wait,
    pause_projection,
    wait_status,
    workflow_wait_meta,
)

TIMER = {
    "wait_id": "w-1",
    "kind": "timer",
    "step_id": "pause",
    "step_execution_id": "exec-1",
    "attempt": 1,
    "group_id": None,
    "workflow_revision": 3,
    "started_at": "2026-10-12T10:00:00+00:00",
    "duration_ms": 5000,
    "due_at": "2026-10-12T10:00:05+00:00",
    "due_epoch_ms": 1_760_263_205_000,
}


def _snapshot(*payloads, **values):
    interrupts = [SimpleNamespace(value=payload) for payload in payloads]
    return SimpleNamespace(tasks=[SimpleNamespace(interrupts=interrupts)], values=values)


def test_a_wait_interrupt_projects_as_a_timer_pause() -> None:
    state = _snapshot({**TIMER, "gate_kind": "wait"})

    wait = durable_wait_from_state(state)

    assert wait == TIMER
    assert wait_status(wait) == STATUS_AWAITING_TIMER


def test_an_authored_approval_projects_its_recorded_wait() -> None:
    recorded = {"wait_id": "w-2", "kind": "approval", "approval_id": "ap-1", "attempt": 1}
    state = _snapshot({"approval_id": "ap-1", "gate_kind": "approval"}, pending_wait=recorded)

    wait = durable_wait_from_state(state)

    assert wait == recorded
    assert wait_status(wait) == STATUS_AWAITING_APPROVAL


def test_a_recovery_gate_is_an_approval_pause_without_a_recorded_wait() -> None:
    stale = {"wait_id": "w-old", "kind": "approval", "approval_id": "ap-old"}
    state = _snapshot({"gate_kind": "recovery", "kind": "failure"}, pending_wait=stale)

    assert durable_wait_from_state(state) is None
    assert wait_status(None) == STATUS_AWAITING_APPROVAL


def test_the_meta_projection_keeps_a_numeric_due_and_a_checkpoint_reference() -> None:
    projected = workflow_wait_meta({**TIMER, "due_epoch_ms": "1760263205000"}, thread_id="t-1")

    assert projected["due_epoch_ms"] == 1_760_263_205_000
    assert projected["checkpoint"] == {"thread_id": "t-1"}
    assert projected["wait_id"] == "w-1"


def test_only_the_same_wait_matches_the_pending_interrupt() -> None:
    state = _snapshot({**TIMER, "gate_kind": "wait"})

    assert interrupt_matches_wait(state, "w-1") is True
    assert interrupt_matches_wait(state, "w-2") is False
    assert interrupt_matches_wait(_snapshot(), "w-1") is False
    assert interrupt_matches_wait(state, "") is False


def test_a_checkpoint_paused_on_a_wait_rebuilds_its_projection() -> None:
    status, wait = pause_projection(_snapshot({**TIMER, "gate_kind": "wait"}))
    assert (status, wait["wait_id"]) == (STATUS_AWAITING_TIMER, "w-1")


def test_a_checkpoint_without_an_interrupt_has_no_pause() -> None:
    assert pause_projection(_snapshot()) is None
    assert pause_projection(None) is None
