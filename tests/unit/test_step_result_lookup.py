"""The hub's per-step result lookup reads the run state, not the whole history.

After every step the hub asks for the result of exactly the execution and attempt
that just ran. It used to validate every message of the run history and read and
validate every stored message of the job for that, so each step cost more than
the one before. These tests pin both halves of the change: the result is the one
the full lookup returns (including a resume whose checkpoint lost it), and the
work no longer depends on how long the run already is.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any

import pytest

import core.application.orchestration.agent_messages as agent_messages
from core.application.orchestration.active_step import ActiveStep, message_matches_execution
from core.application.orchestration.agent_messages import build_agent_message, get_state_messages
from core.application.orchestration.message_bus import MessageBus
from core.application.orchestration.step_hub_support import accepted_message

PAYLOAD = {f"field_{index}": "x" * 100 for index in range(20)}


class _JobStore:
    """platform_agent_messages for one job: every published envelope is a row."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.reads = 0

    def add(self, message: dict[str, Any], **changes: Any) -> None:
        envelope = {**deepcopy(message), **changes}
        self.rows.append(
            {
                "id": len(self.rows) + 1,
                "envelope": json.dumps(envelope),
                "timestamp": "2026-11-09T00:00:00+00:00",
            }
        )

    def get_agent_messages(self, job_id, since_id=0, limit=2000, **_kwargs):
        self.reads += 1
        return list(self.rows[:limit])


@pytest.fixture
def store(monkeypatch) -> _JobStore:
    store = _JobStore()
    bus = MessageBus("postgres://test", logger_client=store)
    monkeypatch.setattr(MessageBus, "for_state", classmethod(lambda cls, state: bus))
    return store


def _message(
    step_id: str,
    execution: str,
    attempt: int = 1,
    *,
    correlation_id: str | None = None,
    published_at: str = "2026-11-09T10:00:00Z",
    status: str = "pass",
    summary: str = "done",
) -> dict[str, Any]:
    return build_agent_message(
        from_agent="control_flow_step",
        to_agent="orchestrator_hub",
        skill="set_variable",
        status=status,
        correlation_id=correlation_id or f"corr-{execution}-{attempt}-{published_at}",
        artifacts={"payload": PAYLOAD},
        summary=summary,
        published_at=published_at,
        step_id=step_id,
        step_execution_id=_execution_id(execution),
        attempt=attempt,
    )


def _execution_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"step-execution/{label}"))


def _current(step_id: str, execution: str, attempt: int = 1) -> ActiveStep:
    return ActiveStep(
        step={"step_id": step_id},
        step_id=step_id,
        step_execution_id=_execution_id(execution),
        attempt=attempt,
    )


def _lookup_before(state: dict[str, Any], current: ActiveStep) -> dict[str, Any] | None:
    """The lookup as it was: newest match of the merged, validated full history."""
    for message in reversed(get_state_messages(state)):
        if message_matches_execution(message, current):
            return message
    return None


def _history(length: int, store: _JobStore) -> list[dict[str, Any]]:
    """A run of *length* earlier steps, each one in the state and in the job store."""
    messages = []
    for index in range(length):
        message = _message(
            f"s{index}", f"exec-{index}", published_at=f"2026-11-09T09:{index % 60:02}:00Z"
        )
        messages.append(message)
        store.add(message)
    return messages


def _state(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"job_id": "job-1", "agent_messages": list(messages)}


class TestTheLookupReturnsWhatTheFullLookupReturned:
    def test_the_result_the_step_just_added(self, store):
        history = _history(5, store)
        result = _message("next", "exec-next")
        store.add(result)
        state = _state([*history, result])
        current = _current("next", "exec-next")

        assert accepted_message(state, current) == _lookup_before(state, current) == result

    def test_a_result_followed_by_other_steps(self, store):
        history = _history(5, store)
        state = _state(history)
        current = _current("s2", "exec-2")

        assert accepted_message(state, current) == _lookup_before(state, current) == history[2]

    @pytest.mark.parametrize("attempt", [1, 2])
    def test_each_attempt_finds_its_own_result(self, store, attempt):
        first = _message("step", "exec-a", 1, status="fail", published_at="2026-11-09T10:00:00Z")
        second = _message("step", "exec-a", 2, published_at="2026-11-09T10:01:00Z")
        for message in (first, second):
            store.add(message)
        state = _state([*_history(3, store), first, second])
        current = _current("step", "exec-a", attempt)

        found = accepted_message(state, current)
        assert found == _lookup_before(state, current)
        assert found["attempt"] == attempt

    def test_the_newest_of_two_results_of_one_attempt_wins_by_time_not_position(self, store):
        newer = _message(
            "step", "exec-a", correlation_id="newer", published_at="2026-11-09T10:05:00Z"
        )
        older = _message(
            "step", "exec-a", correlation_id="older", published_at="2026-11-09T10:00:00Z"
        )
        state = _state([newer, older])
        current = _current("step", "exec-a")

        assert accepted_message(state, current) == _lookup_before(state, current)
        assert accepted_message(state, current)["correlation_id"] == "newer"

    def test_an_invalid_state_entry_is_skipped_like_before(self, store):
        valid = _message(
            "step", "exec-a", correlation_id="valid", published_at="2026-11-09T10:00:00Z"
        )
        broken = {**_message("step", "exec-a", correlation_id="broken"), "status": "unknown"}
        state = _state([valid, broken])
        current = _current("step", "exec-a")

        assert accepted_message(state, current) == _lookup_before(state, current) == valid

    def test_the_state_copy_wins_over_the_redacted_stored_copy(self, store):
        result = _message("step", "exec-a", summary="bind 10.0.0.7:8443")
        store.add(result, summary="bind [REDACTED]")
        state = _state([*_history(3, store), result])
        current = _current("step", "exec-a")

        found = accepted_message(state, current)
        assert found == _lookup_before(state, current)
        assert found["summary"] == "bind 10.0.0.7:8443"

    def test_no_result_anywhere(self, store):
        state = _state(_history(3, store))
        current = _current("never", "exec-never")

        assert accepted_message(state, current) is None
        assert _lookup_before(state, current) is None


class TestResumeAfterTheCheckpointLostTheResult:
    def test_the_stored_result_is_found_when_the_state_has_none(self, store):
        history = _history(4, store)
        result = _message("step", "exec-a")
        store.add(result)
        state = _state(history)  # the checkpoint was written before the result
        current = _current("step", "exec-a")

        found = accepted_message(state, current)
        assert found == _lookup_before(state, current)
        assert found is not None and found["correlation_id"] == result["correlation_id"]
        assert store.reads >= 1

    def test_an_invalid_state_copy_falls_back_to_the_stored_result(self, store):
        result = _message("step", "exec-a")
        store.add(result)
        state = _state([{**result, "status": "unknown"}])
        current = _current("step", "exec-a")

        found = accepted_message(state, current)
        assert found == _lookup_before(state, current)
        assert found is not None and found["status"] == "pass"

    def test_a_resumed_state_without_a_job_id_has_no_stored_messages(self, store):
        current = _current("step", "exec-a")

        assert accepted_message({"agent_messages": []}, current) is None
        assert store.reads == 0


class TestTheLookupCostDoesNotGrowWithTheRun:
    @pytest.mark.parametrize("length", [5, 300])
    def test_a_result_in_the_state_validates_one_message_and_reads_no_stored_rows(
        self, store, monkeypatch, length
    ):
        history = _history(length, store)
        result = _message("next", "exec-next")
        store.add(result)
        state = _state([*history, result])
        validated: list[dict[str, Any]] = []
        real_validate = agent_messages.validate_agent_message_payload

        def counting_validate(payload):
            validated.append(payload)
            return real_validate(payload)

        monkeypatch.setattr(agent_messages, "validate_agent_message_payload", counting_validate)
        store.reads = 0

        assert accepted_message(state, _current("next", "exec-next")) == result
        assert len(validated) == 1
        assert store.reads == 0
