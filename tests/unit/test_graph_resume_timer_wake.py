"""A timer wake reaches only its own wait, once (T20, T21)."""

from __future__ import annotations

from types import SimpleNamespace

from langgraph.types import Command

from core.application.jobs.workflow_wait_projection import WAIT_RESUME_APPLIED_KEY
from core.infrastructure.runtime.graph_resume_runtime import _resume_timer_wait

WAKE = {"reason": "timer", "wait_id": "w-1", "timer_elapsed": True}


def _snapshot(*payloads):
    interrupts = [SimpleNamespace(value=payload) for payload in payloads]
    return SimpleNamespace(tasks=[SimpleNamespace(interrupts=interrupts)])


def _resume(snapshot, values):
    calls: list = []
    result = _resume_timer_wait(
        snapshot=snapshot,
        values=values,
        wait_id="w-1",
        resume_value=WAKE,
        repair_update=lambda: {"inputs": {"restored": True}},
        run=lambda graph_input: calls.append(graph_input) or {"status": "completed"},
    )
    return result, calls


def test_the_wake_resumes_the_matching_wait_and_records_itself() -> None:
    result, calls = _resume(_snapshot({"gate_kind": "wait", "wait_id": "w-1"}), {})

    assert result == {"status": "completed"}
    (command,) = calls
    assert isinstance(command, Command)
    assert command.resume == WAKE
    assert command.update[WAIT_RESUME_APPLIED_KEY] == "w-1"
    assert command.update["inputs"] == {"restored": True}


def test_a_redelivered_wake_continues_from_the_checkpoint_without_a_second_wake() -> None:
    # The first delivery applied the wake and the worker died mid-continuation.
    result, calls = _resume(_snapshot(), {WAIT_RESUME_APPLIED_KEY: "w-1"})

    assert calls == [None]
    assert result == {"status": "completed"}


def test_a_wake_for_another_pause_is_never_delivered() -> None:
    approval = _snapshot({"gate_kind": "approval", "approval_id": "ap-1"})

    result, calls = _resume(approval, {"status": "in_progress"})

    assert calls == []
    assert result == {"status": "in_progress"}


def test_a_wake_for_an_older_wait_is_never_delivered() -> None:
    _result, calls = _resume(_snapshot({"gate_kind": "wait", "wait_id": "w-2"}), {})
    assert calls == []
