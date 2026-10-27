"""A serial invoke waits for its child durably and accepts only its own wake (T34-T36)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.application.orchestration.active_step import active_step
from core.application.orchestration.invoke_workflow_child_wait import (
    ChildWaitNotRecorded,
    await_child,
    child_revision_of,
    child_wake_is_valid,
)
from core.application.orchestration.wait_step_runtime import child_pending_wait
from tests._step_run_state import step_run_state

INVOKE = {"step_id": "call_child", "step_kind": "invoke_workflow"}


class _Store:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.reads = 0

    def get_job(self, job_id):
        status = self.statuses[min(self.reads, len(self.statuses) - 1)]
        self.reads += 1
        return {"id": job_id, "status": status, "meta": {}, "finished_at": "2026-10-13T00:00:00Z"}


def _state_with_wait() -> tuple[dict, dict]:
    state = step_run_state(INVOKE, agent_messages=[])
    current = active_step(state)
    pending = child_pending_wait(current, revision=4)
    return {**state, "pending_wait": pending}, pending


def _wake(pending: dict, child_job_id: str = "child-1", **changes) -> dict:
    return {
        "reason": "child",
        "wait_id": pending["wait_id"],
        "child_job_id": child_job_id,
        "child_status": "completed",
        **changes,
    }


def test_the_parent_pauses_with_its_wait_and_its_child() -> None:
    state, pending = _state_with_wait()
    store = _Store(["completed"])

    with patch("langgraph.types.interrupt", return_value=_wake(pending)) as interrupt:
        row, status = await_child(
            state,
            active_step(state),
            bl=store,
            child_job_id="child-1",
            invocation_key="77:exec",
            child_revision=3,
        )

    payload = interrupt.call_args.args[0]
    assert payload["gate_kind"] == "wait"
    assert payload["kind"] == "child"
    assert payload["wait_id"] == pending["wait_id"]
    assert payload["child_job_id"] == "child-1"
    assert payload["invocation_key"] == "77:exec"
    assert payload["child_revision"] == 3
    assert (row["id"], status) == ("child-1", "completed")


def test_a_foreign_or_stale_wake_never_advances_the_parent() -> None:
    state, pending = _state_with_wait()
    store = _Store(["completed"])
    wakes = [
        _wake(pending, child_job_id="another-child"),
        _wake(pending, wait_id="older-wait"),
        {"reason": "timer", "wait_id": pending["wait_id"], "timer_elapsed": True},
        {"approved": True},
        _wake(pending),
    ]

    with patch("langgraph.types.interrupt", side_effect=wakes) as interrupt:
        _row, status = await_child(
            state,
            active_step(state),
            bl=store,
            child_job_id="child-1",
            invocation_key="77:exec",
            child_revision=None,
        )

    assert interrupt.call_count == 5
    assert status == "completed"


def test_the_child_row_decides_not_the_wake() -> None:
    """A wake for a child that is not finished (yet) pauses again."""
    state, pending = _state_with_wait()
    store = _Store(["in_progress", "failed"])

    with patch("langgraph.types.interrupt", side_effect=[_wake(pending), _wake(pending)]):
        _row, status = await_child(
            state,
            active_step(state),
            bl=store,
            child_job_id="child-1",
            invocation_key="77:exec",
            child_revision=None,
        )

    assert status == "failed"
    assert store.reads == 2


@pytest.mark.parametrize("status", ["failed", "cancelled", "max_iterations", "stuck"])
def test_an_unsuccessful_child_ends_the_wait_with_its_status(status) -> None:
    state, pending = _state_with_wait()

    with patch("langgraph.types.interrupt", return_value=_wake(pending)):
        _row, result = await_child(
            state,
            active_step(state),
            bl=_Store([status]),
            child_job_id="child-1",
            invocation_key="k",
            child_revision=None,
        )

    assert result == status


def test_without_a_recorded_child_wait_the_step_does_not_pause() -> None:
    state = step_run_state(INVOKE, agent_messages=[])
    step = active_step(state)
    store = _Store(["completed"])
    with pytest.raises(ChildWaitNotRecorded):
        await_child(
            state,
            step,
            bl=store,
            child_job_id="child-1",
            invocation_key="k",
            child_revision=None,
        )


def test_wake_validation() -> None:
    pending = {"wait_id": "w"}
    assert child_wake_is_valid(pending, _wake(pending, "c"), child_job_id="c")
    assert not child_wake_is_valid(pending, _wake(pending, "c"), child_job_id="d")
    assert not child_wake_is_valid(pending, None, child_job_id="c")


def test_the_child_revision_is_the_one_pinned_at_creation() -> None:
    assert child_revision_of({"meta": {"workflow_definition_ref": {"revision": 9}}}) == 9
    assert child_revision_of({"meta": '{"workflow_definition_ref": {"revision": "2"}}'}) == 2
    assert child_revision_of({"meta": {}}) is None
    assert child_revision_of(None) is None


@pytest.mark.parametrize("status", ["stuck", "orphaned"])
def test_unfinished_watchdog_child_is_not_consumed_as_terminal_failure(status):
    from unittest.mock import MagicMock

    state, pending = _state_with_wait()
    store = MagicMock()
    store.get_job.side_effect = [
        {"id": "child-1", "status": status, "finished_at": None},
        {"id": "child-1", "status": "completed", "finished_at": "2026-10-13T00:00:00Z"},
    ]
    with patch("langgraph.types.interrupt", side_effect=[_wake(pending), _wake(pending)]):
        _row, result = await_child(
            state,
            active_step(state),
            bl=store,
            child_job_id="child-1",
            invocation_key="k",
            child_revision=None,
        )
    assert result == "completed"
    assert store.get_job.call_count == 2


def test_missing_child_ends_matching_wait_with_explicit_failure():
    from unittest.mock import MagicMock

    state, pending = _state_with_wait()
    store = MagicMock()
    store.get_job.return_value = None
    with patch("langgraph.types.interrupt", side_effect=[_wake(pending)]):
        row, result = await_child(
            state,
            active_step(state),
            bl=store,
            child_job_id="child-1",
            invocation_key="k",
            child_revision=None,
        )
    assert row is None
    assert result == "missing"
