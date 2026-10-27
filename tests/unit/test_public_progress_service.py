from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Lock, Thread
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.jobs.public_progress_repository import PublicProgressRepository
from core.application.jobs.public_progress_service import (
    PublicProgressService,
    PublicStepProgressRecorder,
)
from core.application.jobs.workflow_run_models import PublicWorkflowStepV1

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


class MemoryProgressRepository:
    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.markers: list[dict[str, Any]] = []
        self._lock = Lock()
        self.fail_marker = False

    def load(self, *, job_id: str) -> dict[str, Any] | None:
        return deepcopy(self.value)

    def atomic_update(
        self,
        *,
        job_id: str,
        update: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> tuple[dict[str, Any], str | None, bool]:
        with self._lock:
            before = deepcopy(self.value)
            candidate = update(deepcopy(self.value))
            if candidate is None:
                assert self.value is not None
                return deepcopy(self.value), None, False
            if self.fail_marker:
                self.value = before
                raise RuntimeError("marker insert failed")
            self.value = deepcopy(candidate)
            marker_id = f"marker-{len(self.markers) + 1}"
            self.markers.append(deepcopy(candidate["marker_detail"]))
            return deepcopy(candidate), marker_id, True


def _service(repository: MemoryProgressRepository) -> PublicProgressService:
    return PublicProgressService(repository)


def _base(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "job_id": "job-1",
        "project_id": "project-1",
        "workflow_id": "workflow-1",
        "job_status": "in_progress",
        "occurred_at": NOW,
        "queued_at": NOW - timedelta(seconds=10),
        "started_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    return values


def _step(
    step_id: str,
    *,
    occurrence: int = 1,
    status: str = "in_progress",
    step_execution_id: str | None = None,
) -> PublicWorkflowStepV1:
    return PublicWorkflowStepV1(
        step_id=step_id,
        step_execution_id=step_execution_id,
        occurrence=occurrence,
        display_name=None,
        step_kind="agent",
        status=status,
        started_at=NOW,
        finished_at=NOW if status in {"completed", "failed", "cancelled", "skipped"} else None,
        child_job_id=None,
    )


def test_record_transition_deduplicates_unchanged_projection() -> None:
    repository = MemoryProgressRepository()
    first = _service(repository).record_transition(**_base())
    second = _service(repository).record_transition(**_base(occurred_at=NOW + timedelta(seconds=2)))

    assert first.revision == 1
    assert first.changed is True
    assert second.revision == 1
    assert second.changed is False
    assert second.marker_id is None
    assert len(repository.markers) == 1


def test_forced_transition_rematerializes_an_unchanged_projection_with_a_marker() -> None:
    repository = MemoryProgressRepository()
    service = _service(repository)
    service.record_transition(**_base())

    rematerialized = service.record_transition(
        **_base(occurred_at=NOW + timedelta(seconds=2)),
        force=True,
    )

    assert rematerialized.changed is True
    assert rematerialized.marker_id == "marker-2"
    assert rematerialized.revision == 2
    assert len(repository.markers) == 2


def test_current_snapshot_preserves_durable_marker_cursor() -> None:
    repository = MemoryProgressRepository()
    service = _service(repository)
    first = service.record_transition(**_base())
    repository.value["marker_id"] = first.marker_id

    current = service.current_snapshot(job_id="job-1")

    assert current.marker_id == "marker-1"
    assert current.changed is False


def test_parallel_transitions_merge_without_losing_a_lane_and_order_by_first_revision() -> None:
    repository = MemoryProgressRepository()
    service = _service(repository)
    service.record_transition(**_base())

    threads = [
        Thread(target=lambda: service.record_transition(**_base(step=_step("lane-b")))),
        Thread(target=lambda: service.record_transition(**_base(step=_step("lane-a")))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    current = service.current_snapshot(job_id="job-1")
    assert current.revision == 3
    assert {step.step_id for step in current.snapshot.active_steps} == {"lane-a", "lane-b"}
    assert [step.step_id for step in current.snapshot.steps] == [
        marker["step"]["step_id"] for marker in repository.markers[1:]
    ]


def test_repeated_step_occurrences_append_instead_of_overwriting_history() -> None:
    repository = MemoryProgressRepository()
    service = _service(repository)
    service.record_transition(**_base(step=_step("review", occurrence=1, status="completed")))
    result = service.record_transition(**_base(step=_step("review", occurrence=2)))

    assert [(step.step_id, step.occurrence, step.status) for step in result.snapshot.steps] == [
        ("review", 1, "completed"),
        ("review", 2, "in_progress"),
    ]
    assert [(step.step_id, step.occurrence) for step in result.snapshot.active_steps] == [
        ("review", 2)
    ]


def test_terminal_status_sets_timestamp_and_result_null_semantics() -> None:
    repository = MemoryProgressRepository()
    result = _service(repository).record_transition(
        **_base(
            job_status="completed",
            occurred_at=NOW + timedelta(minutes=1),
            finished_at=NOW + timedelta(minutes=1),
            result={"outputs": {}, "artifacts": {}},
        )
    )

    assert result.snapshot.status == "completed"
    assert result.snapshot.terminal is True
    assert result.snapshot.finished_at == NOW + timedelta(minutes=1)
    assert result.snapshot.result is not None
    assert result.snapshot.error is None
    assert result.snapshot.model_dump(mode="json")["updated_at"].endswith("Z")


def test_terminal_snapshot_does_not_expose_previous_active_steps() -> None:
    repository = MemoryProgressRepository()
    service = _service(repository)
    service.record_transition(**_base(step=_step("review")))

    result = service.record_transition(
        **_base(
            job_status="cancelled",
            occurred_at=NOW + timedelta(minutes=1),
            finished_at=NOW + timedelta(minutes=1),
        )
    )

    assert result.snapshot.terminal is True
    assert [step.step_id for step in result.snapshot.steps] == ["review"]
    assert result.snapshot.active_steps == []


def test_atomic_transition_never_replaces_terminal_snapshot_with_non_terminal_state() -> None:
    repository = MemoryProgressRepository()
    service = _service(repository)
    terminal = service.record_transition(
        **_base(
            job_status="completed",
            occurred_at=NOW + timedelta(minutes=1),
            finished_at=NOW + timedelta(minutes=1),
            result={"outputs": {}, "artifacts": {}},
        )
    )

    stale = service.record_transition(
        **_base(
            job_status="awaiting_approval",
            occurred_at=NOW + timedelta(minutes=2),
            approval={
                "approval_id": "stale-gate",
                "job_id": "job-1",
                "thread_id": "thread-1",
                "message": "Approve stale work",
                "content": {},
                "allowed_actions": ["approve", "reject"],
                "created_at": NOW + timedelta(minutes=2),
            },
        )
    )

    assert stale.changed is False
    assert stale.revision == terminal.revision
    assert stale.snapshot.status == "completed"
    assert stale.snapshot.terminal is True
    assert stale.snapshot.approval is None


def test_marker_failure_aborts_snapshot_change() -> None:
    repository = MemoryProgressRepository()
    service = _service(repository)
    first = service.record_transition(**_base())
    repository.fail_marker = True

    _raises_input_243_1 = _base(
        job_status="failed",
        error={
            "code": "step_failed",
            "message": "Failed",
            "category": "execution",
            "retryable": False,
            "details": {},
        },
    )
    with pytest.raises(RuntimeError, match="marker insert failed"):
        service.record_transition(**_raises_input_243_1)

    assert service.current_snapshot(job_id="job-1").snapshot.status == first.snapshot.status


_EXECUTION_ID = "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"


def _recorder(*steps: PublicWorkflowStepV1) -> tuple[MagicMock, PublicStepProgressRecorder]:
    gateway = MagicMock()
    gateway.get_job.return_value = {
        "project_id": "project-1",
        "selected_workflow": "workflow-1",
        "status": "in_progress",
        "queued_at": None,
        "started_at": NOW,
    }
    gateway.get_public_progress_snapshot.return_value = type(
        "Revision",
        (),
        {"snapshot": type("Snapshot", (), {"steps": list(steps)})()},
    )()
    recorder = PublicStepProgressRecorder(
        gateway=gateway,
        project_id="project-1",
        job_id="job-1",
        clock=lambda: NOW,
    )
    return gateway, recorder


def _writer_run_state(**state: Any) -> dict[str, Any]:
    from tests._step_run_state import step_run_state

    return step_run_state(
        {"step_id": "configured-step", "step_kind": "agent", "agent": "writer"},
        step_execution_id=_EXECUTION_ID,
        dispatched_step_execution={
            "step_id": "configured-step",
            "step_execution_id": _EXECUTION_ID,
            "attempt": 1,
        },
        **state,
    )


def test_public_step_recorder_owns_identity_occurrence_and_status_mapping() -> None:
    gateway, recorder = _recorder(
        _step(
            "configured-step",
            occurrence=4,
            step_execution_id="0b0c5f0e-7a51-4c1b-8f8e-2b8c4f9a6d10",
        )
    )

    recorder.record(
        run_id="run-1",
        node_name="writer",
        payload=_writer_run_state(),
        status="in_progress",
        starting=True,
    )
    recorder.record(run_id="run-1", node_name="writer", payload={}, status="completed")

    transitions = [call.kwargs for call in gateway.record_public_progress_transition.call_args_list]
    assert [
        (item["step"]["step_id"], item["step"]["step_execution_id"]) for item in transitions
    ] == [("configured-step", _EXECUTION_ID), ("configured-step", _EXECUTION_ID)]
    assert [item["step"]["occurrence"] for item in transitions] == [5, 5]
    assert [item["step"]["status"] for item in transitions] == ["in_progress", "completed"]


@pytest.mark.parametrize("pause", ["awaiting_timer", "awaiting_child"])
def test_a_step_paused_on_a_durable_wait_stays_active_in_the_run(pause: str) -> None:
    repository = MemoryProgressRepository()

    revision = _service(repository).record_transition(
        **_base(job_status=pause, step=_step("wait-step", status=pause))
    )

    snapshot = revision.snapshot
    assert snapshot.status == pause
    assert snapshot.terminal is False
    assert snapshot.finished_at is None
    assert snapshot.approval is None
    assert [(step.step_id, step.status) for step in snapshot.active_steps] == [("wait-step", pause)]


def test_public_step_recorder_keeps_one_occurrence_per_execution_across_retries() -> None:
    gateway, recorder = _recorder()
    payload = _writer_run_state()

    for run_id, status in (("attempt-1", "failed"), ("attempt-2", "completed")):
        recorder.record(
            run_id=run_id, node_name="writer", payload=payload, status="in_progress", starting=True
        )
        recorder.record(run_id=run_id, node_name="writer", payload={}, status=status)

    occurrences = {
        call.kwargs["step"]["occurrence"]
        for call in gateway.record_public_progress_transition.call_args_list
    }
    assert occurrences == {1}


def test_public_step_recorder_names_other_nodes_without_an_execution() -> None:
    gateway, recorder = _recorder()

    recorder.record(
        run_id="hub",
        node_name="orchestrator",
        payload=_writer_run_state(),
        status="in_progress",
        starting=True,
    )
    recorder.record(
        run_id="intake", node_name="task_intake", payload={}, status="in_progress", starting=True
    )

    steps = [
        call.kwargs["step"] for call in gateway.record_public_progress_transition.call_args_list
    ]
    # The hub is not the cursor step's executor, so its pass is not projected;
    # a node outside any step is recorded under its own name.
    assert [(step["step_id"], step["step_execution_id"]) for step in steps] == [
        ("task_intake", None)
    ]


def test_public_step_recorder_records_a_skipped_disabled_step() -> None:
    gateway, recorder = _recorder()
    skipped = {
        **_writer_run_state(),
        "dispatched_step_execution": {
            "step_id": "configured-step",
            "step_execution_id": _EXECUTION_ID,
            "attempt": 1,
            "step_kind": "agent",
            "skipped": "disabled",
        },
    }

    recorder.record(
        run_id="hub", node_name="orchestrator", payload={}, status="in_progress", starting=True
    )
    recorder.record(run_id="hub", node_name="orchestrator", payload=skipped, status="completed")

    steps = [
        call.kwargs["step"] for call in gateway.record_public_progress_transition.call_args_list
    ]
    skipped_rows = [step for step in steps if step["status"] == "skipped"]
    assert [
        (step["step_id"], step["step_execution_id"], step["step_kind"], step["occurrence"])
        for step in skipped_rows
    ] == [("configured-step", _EXECUTION_ID, "agent", 1)]
    assert skipped_rows[0]["started_at"] is None
    assert skipped_rows[0]["finished_at"] == NOW


def test_public_step_recorder_ignores_an_ordinary_dispatch_from_the_hub() -> None:
    gateway, recorder = _recorder()

    recorder.record(
        run_id="hub", node_name="orchestrator", payload=_writer_run_state(), status="completed"
    )

    steps = [
        call.kwargs["step"] for call in gateway.record_public_progress_transition.call_args_list
    ]
    assert all(step["status"] != "skipped" for step in steps)


class _SqlCursor:
    def __init__(self, conn: _SqlConnection) -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.calls.append((sql, params))
        if "INSERT INTO platform_events" in sql:
            raise RuntimeError("strict marker unavailable")

    def fetchone(self):
        return {"meta": {}}


class _SqlConnection:
    def __init__(self) -> None:
        self.autocommit = True
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *args: Any, **kwargs: Any) -> _SqlCursor:
        return _SqlCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_postgres_repository_rolls_back_snapshot_when_strict_marker_insert_fails() -> None:
    conn = _SqlConnection()
    repository = PublicProgressRepository(conn)

    with pytest.raises(RuntimeError, match="strict marker unavailable"):
        repository.atomic_update(
            job_id="job-1",
            update=lambda _current: {
                "revision": 1,
                "snapshot": {},
                "marker_detail": {"revision": 1},
            },
        )

    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert any("UPDATE jobs SET meta" in sql for sql, _params in conn.calls)


def test_a_long_for_each_keeps_the_newest_finished_rows_and_every_active_one() -> None:
    from core.application.jobs.public_progress_service import RETAINED_FINISHED_STEPS

    repository = MemoryProgressRepository()
    service = _service(repository)
    total = RETAINED_FINISHED_STEPS + 50
    for occurrence in range(1, total + 1):
        service.record_transition(
            **_base(
                step=_step(
                    "work",
                    occurrence=occurrence,
                    status="completed",
                    step_execution_id=f"exec-{occurrence}",
                )
            )
        )
    result = service.record_transition(
        **_base(step=_step("work", occurrence=total + 1, step_execution_id="exec-active"))
    )

    occurrences = [step.occurrence for step in result.snapshot.steps]
    assert len(occurrences) == RETAINED_FINISHED_STEPS + 1
    assert occurrences[0] == total - RETAINED_FINISHED_STEPS + 1
    assert occurrences[-1] == total + 1
    assert [step.step_execution_id for step in result.snapshot.active_steps] == ["exec-active"]
    assert len(repository.value["step_order"]) == RETAINED_FINISHED_STEPS + 1


def test_committed_trace_catches_up_terminal_snapshot_without_rewriting_lifecycle():
    from tests.unit.test_workflow_run_trace import one_trace

    repository = MemoryProgressRepository()
    service = _service(repository)
    service.record_transition(
        **_base(
            job_status="failed",
            finished_at=NOW,
            error={
                "code": "workflow_stop_fail",
                "message": "Authored failure",
                "category": "execution",
                "retryable": False,
            },
        )
    )
    before = service.current_snapshot(job_id="job-1").snapshot.model_dump(mode="json")
    trace = one_trace()
    result = service.record_committed_trace(
        job_id="job-1", trace=trace, occurred_at=NOW + timedelta(seconds=1)
    )
    after = result.snapshot.model_dump(mode="json")
    for key in ("status", "terminal", "finished_at", "result", "error", "approval", "steps"):
        assert after[key] == before[key]
    assert after["run_trace"] == trace
    duplicate = service.record_committed_trace(
        job_id="job-1", trace=trace, occurred_at=NOW + timedelta(seconds=2)
    )
    assert duplicate.changed is False
    assert len(repository.markers) == 2
    # Callback/terminal rematerialization must retain the verified trace.
    refreshed = service.record_transition(
        **_base(job_status="failed", finished_at=NOW, error=before["error"])
    )
    assert refreshed.snapshot.run_trace.model_dump(mode="json") == trace


def test_committed_trace_rejects_foreign_identity_and_conflicting_same_sequence():
    from tests.unit.test_workflow_run_trace import one_trace

    repository = MemoryProgressRepository()
    service = _service(repository)
    service.record_transition(**_base())
    trace = one_trace()
    service.record_committed_trace(job_id="job-1", trace=trace, occurred_at=NOW)
    baseline = deepcopy(repository.value)
    for patch in (
        {"job_id": "foreign-job"},
        {"workflow_id": "foreign-workflow"},
        {"digest": "another-digest"},
        {"covered_from_start": False},
    ):
        assert (
            service.record_committed_trace(
                job_id="job-1", trace={**trace, **patch}, occurred_at=NOW
            ).changed
            is False
        )
        assert repository.value == baseline
    stale = {**trace, "last_sequence": 0, "transitions": []}
    assert (
        service.record_committed_trace(job_id="job-1", trace=stale, occurred_at=NOW).changed
        is False
    )
    conflicting = deepcopy(trace)
    conflicting["transitions"][0]["target"] = {"kind": "step", "id": "different"}
    assert (
        service.record_committed_trace(job_id="job-1", trace=conflicting, occurred_at=NOW).changed
        is False
    )
    assert repository.value == baseline


def test_trace_incompleteness_can_catch_up_at_the_same_sequence_without_coverage_upgrade():
    from tests.unit.test_workflow_run_trace import one_trace

    repository = MemoryProgressRepository()
    service = _service(repository)
    service.record_transition(**_base())
    trace = one_trace()
    service.record_committed_trace(job_id="job-1", trace=trace, occurred_at=NOW)
    partial = {**trace, "truncated": True}
    changed = service.record_committed_trace(job_id="job-1", trace=partial, occurred_at=NOW)
    assert changed.changed is True
    assert changed.snapshot.run_trace.truncated is True
    assert (
        service.record_committed_trace(job_id="job-1", trace=trace, occurred_at=NOW).changed
        is False
    )
