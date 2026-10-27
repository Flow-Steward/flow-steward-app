"""A run paused in its checkpoint is waiting, not stuck (T21)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace

from core.infrastructure.runtime import state_reader_runtime
from core.infrastructure.runtime.control import scans

TIMER = {
    "gate_kind": "wait",
    "wait_id": "w-1",
    "kind": "timer",
    "step_id": "pause",
    "step_execution_id": "exec-1",
    "attempt": 1,
    "due_epoch_ms": 1_760_263_205_000,
}


def _checkpointer(writes, values=None):
    @contextmanager
    def _open():
        yield SimpleNamespace(
            get_tuple=lambda _config: SimpleNamespace(
                checkpoint={"channel_values": values or {"status": "in_progress"}},
                pending_writes=writes,
            )
        )

    return _open


def _read(writes, values=None):
    return state_reader_runtime.get_committed_pause(
        "thread-1",
        config={"langgraph_postgres_uri": "postgresql://checkpoints"},
        logger=logging.getLogger(__name__),
        open_checkpointer=_checkpointer(writes, values),
    )


def test_the_committed_pause_lists_unanswered_interrupts() -> None:
    pause = _read([("task-1", "__interrupt__", [SimpleNamespace(value=TIMER)])])

    (task,) = pause.tasks
    assert [interrupt.value for interrupt in task.interrupts] == [TIMER]
    assert pause.values == {"status": "in_progress"}


def test_an_answered_interrupt_is_not_a_pause() -> None:
    pause = _read(
        [
            ("task-1", "__interrupt__", [SimpleNamespace(value=TIMER)]),
            ("task-1", "__resume__", {"reason": "timer"}),
        ]
    )
    assert pause.tasks == []


def test_no_checkpoint_store_reads_nothing() -> None:
    assert (
        state_reader_runtime.get_committed_pause(
            "thread-1", config={}, logger=logging.getLogger(__name__)
        )
        is None
    )


class _Store:
    def __init__(self) -> None:
        self.meta: list[dict] = []
        self.status: list[str] = []
        self.locked = False
        self.conn = SimpleNamespace(cursor=self._cursor)

    @contextmanager
    def _cursor(self):
        yield SimpleNamespace(
            execute=self._lock,
            fetchone=lambda: {"status": "in_progress", "worker_id": None},
        )

    def _lock(self, sql, params):
        assert sql == "SELECT status, worker_id FROM jobs WHERE id = %s FOR UPDATE"
        assert params == ("job-1",)
        self.locked = True

    def merge_job_meta(self, job_id, patch):
        assert self.locked
        self.meta.append(patch)

    def update_job(self, job_id, *, status):
        assert self.locked
        self.status.append(status)


def test_a_paused_timer_is_reprojected_with_its_wait(monkeypatch) -> None:
    pause = SimpleNamespace(
        values={}, tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=TIMER)])]
    )
    monkeypatch.setattr(scans, "get_committed_pause", lambda _thread: pause)
    store = _Store()

    restored = scans._restore_pause_projection(store, "job-1", {"thread_id": "thread-1"})

    assert restored == "awaiting_timer"
    assert store.status == ["awaiting_timer"]
    (patch,) = store.meta
    assert patch["workflow_wait"]["wait_id"] == "w-1"
    assert patch["workflow_wait"]["checkpoint"] == {"thread_id": "thread-1"}


def test_a_paused_recovery_gate_is_reprojected_as_an_approval(monkeypatch) -> None:
    gate = SimpleNamespace(value={"gate_kind": "recovery", "kind": "failure"})
    pause = SimpleNamespace(values={}, tasks=[SimpleNamespace(interrupts=[gate])])
    monkeypatch.setattr(scans, "get_committed_pause", lambda _thread: pause)
    store = _Store()

    assert scans._restore_pause_projection(store, "job-1", {"thread_id": "t"}) == (
        "awaiting_approval"
    )
    assert store.meta == []


def test_a_run_without_a_pause_is_left_to_the_stuck_classification(monkeypatch) -> None:
    monkeypatch.setattr(
        scans, "get_committed_pause", lambda _thread: SimpleNamespace(values={}, tasks=[])
    )
    store = _Store()

    assert scans._restore_pause_projection(store, "job-1", {"thread_id": "t"}) is None
    assert store.status == []
