from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from core.application.jobs.execution_contracts import (
    WorkflowResumeGraphOutcome,
    WorkflowResumeRequest,
)
from core.application.jobs.workflow_resume_execution_service import (
    WORKFLOW_RESUME_LEASE_SECONDS,
    WorkflowResumeExecutionService,
)
from core.application.jobs.workflow_resume_state import WorkflowResumeClaim


class _LeaseHandle:
    def __init__(
        self,
        events: list[str],
        *,
        lost: Any = False,
        label: str = "lease",
    ) -> None:
        self._events = events
        self._lost = lost
        self._label = label

    def cancelled(self) -> bool:
        return bool(self._lost() if callable(self._lost) else self._lost)

    def stop(self) -> None:
        self._events.append(f"stop:{self._label}")


class _LeasePort:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.execution_lost = False
        self.finalizer_lost = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_execution(self, **kwargs: Any) -> _LeaseHandle:
        self.events.append("start:execution_heartbeat")
        self.calls.append(("start_execution", dict(kwargs)))
        return _LeaseHandle(
            self.events,
            lost=lambda: self.execution_lost,
            label="execution_heartbeat",
        )

    def start_finalizer(self, **kwargs: Any) -> _LeaseHandle:
        self.events.append("start:finalizer_heartbeat")
        self.calls.append(("start_finalizer", dict(kwargs)))
        return _LeaseHandle(
            self.events,
            lost=lambda: self.finalizer_lost,
            label="finalizer_heartbeat",
        )


class _Repository:
    available = True

    def __init__(self, job: dict[str, Any] | None, events: list[str]) -> None:
        self.job = job
        self.events = events
        self.lookup_error: Exception | None = None
        self.completed = True
        self.finalizing = True
        self.claim = WorkflowResumeClaim(claimed=True, lease_token="execution-token")
        self.finalizer_claimed = True
        self.pending: list[dict[str, Any]] = []
        self.publish_states: list[dict[str, Any]] = []
        self.publish_mark_result = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.journal_results: dict[str, bool] = {}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.events.append("get_job")
        if self.lookup_error is not None:
            raise self.lookup_error
        return dict(self.job) if self.job is not None else None

    def claim_execution(self, **kwargs: Any) -> WorkflowResumeClaim:
        self.events.append("claim_execution")
        self.calls.append(("claim_execution", dict(kwargs)))
        return self.claim

    def complete_failed(self, **kwargs: Any) -> bool:
        self.events.append("complete_failed")
        self.calls.append(("complete_failed", dict(kwargs)))
        return True

    def mark_retryable(self, **kwargs: Any) -> bool:
        self.events.append("mark_retryable")
        self.calls.append(("mark_retryable", dict(kwargs)))
        return True

    def mark_finalizing(self, **kwargs: Any) -> bool:
        self.events.append("mark_finalizing")
        self.calls.append(("mark_finalizing", dict(kwargs)))
        return self.finalizing

    def claim_finalizer(self, **kwargs: Any) -> bool:
        self.events.append("claim_finalizer")
        self.calls.append(("claim_finalizer", dict(kwargs)))
        return self.finalizer_claimed

    def mark_finalization_step_completed(self, *, step: str, **kwargs: Any) -> bool:
        self.events.append(f"journal:{step}")
        self.calls.append(("mark_finalization_step_completed", {**kwargs, "step": step}))
        return self.journal_results.get(step, True)

    def complete(self, **kwargs: Any) -> bool:
        self.events.append("complete")
        self.calls.append(("complete", dict(kwargs)))
        return self.completed

    def release_finalizer(self, **kwargs: Any) -> bool:
        self.events.append("release_finalizer")
        self.calls.append(("release_finalizer", dict(kwargs)))
        return True

    def pending_rows(self, *, batch_size: int) -> list[dict[str, Any]]:
        self.events.append(f"pending:{batch_size}")
        return [dict(row) for row in self.pending]

    def mark_publish_state(
        self,
        *,
        job_id: str,
        resume_revision: int,
        publish_token: str,
        expected_state: str,
        state: str,
        error: str = "",
    ) -> bool:
        self.publish_states.append(
            {
                "job_id": job_id,
                "resume_revision": resume_revision,
                "publish_token": publish_token,
                "expected_state": expected_state,
                "state": state,
                "error": error,
            }
        )
        return self.publish_mark_result


class _Graph:
    def __init__(self, events: list[str], outcome: WorkflowResumeGraphOutcome) -> None:
        self.events = events
        self.outcome = outcome
        self.calls: list[Any] = []

    def resume(self, request: Any, *, cancelled: Any) -> WorkflowResumeGraphOutcome:
        self.events.append("graph")
        self.calls.append((request, cancelled))
        return self.outcome


class _RaisingGraph:
    def __init__(self, events: list[str], error: BaseException) -> None:
        self.events = events
        self.error = error

    def resume(self, request: Any, *, cancelled: Any) -> WorkflowResumeGraphOutcome:
        self.events.append("graph")
        raise self.error


class _Terminal:
    def __init__(self, events: list[str], *, after_effect: Any = None) -> None:
        self.events = events
        self.after_effect = after_effect
        self.calls: list[tuple[str, Any]] = []

    def _record(self, effect: str, context: Any) -> None:
        self.events.append(f"effect:{effect}")
        self.calls.append((effect, context))
        if self.after_effect is not None:
            self.after_effect(effect)

    def release_lock(self, context: Any) -> None:
        self._record("lock_release", context)

    def notify(self, context: Any) -> None:
        self._record("notification", context)

    def sync_provider(self, context: Any) -> None:
        self._record("provider_sync", context)


def _job(
    *,
    status: str = "resume_pending",
    state: str = "pending",
    revision: int = 7,
    finalization_steps: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "job-42",
        "project_id": "project-42",
        "task_id": "TASK-42",
        "thread_id": "thread-42",
        "status": status,
        "meta": {
            "workflow_resume": {
                "state": state,
                "revision": revision,
                "approval_id": "gate-1",
                "resume_value": {"approved": True},
                "approved": True,
                "action": "approve",
                "comment": "ship it",
                "interrupt_payload": {"stage": "review"},
                "actor_username": "alice",
                "final_status": status if status == "completed" else "",
                "result": {"status": status} if status == "completed" else {},
                "finalization_steps": dict(finalization_steps or {}),
            }
        },
    }


def _message(*, revision: int = 7) -> dict[str, Any]:
    return {
        "message_type": "workflow_resume",
        "job_id": "job-42",
        "thread_id": "thread-42",
        "resume_revision": revision,
    }


def _service(
    job: dict[str, Any],
    outcome: WorkflowResumeGraphOutcome,
) -> tuple[
    WorkflowResumeExecutionService,
    _Repository,
    _Graph,
    _LeasePort,
    list[str],
]:
    events: list[str] = []
    repository = _Repository(job, events)
    graph = _Graph(events, outcome)
    lease = _LeasePort(events)
    service = WorkflowResumeExecutionService(
        repository=repository,
        graph=graph,
        lease=lease,
        terminal=_Terminal(events),
        worker_id=lambda: "worker-1",
        token_factory=iter(("claim-proposal-token", "finalizer-token")).__next__,
    )
    return service, repository, graph, lease, events


def test_invalid_resume_payload_dead_letters_after_base_job_lookup() -> None:
    service, repository, graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(status="completed", result={"status": "completed"}),
    )

    result = service.execute({"message_type": "workflow_resume", "job_id": "job-42"})

    assert result == {"action": "dead_letter", "reason": "invalid_workflow_resume_payload"}
    assert events == ["get_job"]
    assert graph.calls == []
    assert repository.publish_states == []


def test_unavailable_approval_repository_retries_without_graph_or_lease() -> None:
    events: list[str] = []
    graph = _Graph(
        events,
        WorkflowResumeGraphOutcome.accepted(
            status="awaiting_approval",
            result={"status": "awaiting_approval"},
        ),
    )
    lease = _LeasePort(events)
    service = WorkflowResumeExecutionService(
        repository=None,
        graph=graph,
        lease=lease,
        terminal=_Terminal(events),
        worker_id=lambda: "worker-1",
        token_factory=lambda: "unused-token",
    )

    result = service.execute(_message())

    assert result == {
        "action": "retry",
        "reason": "workflow_resume_job_lookup_failed",
    }
    assert graph.calls == []
    assert lease.calls == []


@pytest.mark.parametrize(
    ("expected_state", "publish_fails", "target_state", "recovered"),
    [
        ("pending", False, "published", 1),
        ("publish_failed", False, "published", 1),
        ("pending", True, "publish_failed", 0),
        ("running", False, "running", 1),
        ("running", True, "running", 0),
        ("finalizing", False, "finalizing", 1),
        ("finalizing", True, "finalizing", 0),
    ],
)
def test_outbox_publish_reservation_preserves_execution_lifecycle_state(
    expected_state: str,
    publish_fails: bool,
    target_state: str,
    recovered: int,
) -> None:
    service, repository, _graph, _lease, _events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="awaiting_approval",
            result={"status": "awaiting_approval"},
        ),
    )
    repository.pending = [
        {
            "id": "job-42",
            "project_id": "project-42",
            "task_id": "TASK-42",
            "thread_id": "thread-42",
            "resume_revision": 7,
            "publish_state": expected_state,
            "publish_token": "publish-token-7",
        }
    ]
    published: list[dict[str, Any]] = []

    def publisher(**kwargs: Any) -> None:
        published.append(dict(kwargs))
        if publish_fails:
            raise RuntimeError("broker unavailable")

    result = service.dispatch_pending(publisher=publisher, batch_size=5)

    assert result == recovered
    assert published == [
        {
            "project_id": "project-42",
            "job_id": "job-42",
            "task_id": "TASK-42",
            "thread_id": "thread-42",
            "resume_revision": 7,
        }
    ]
    assert repository.publish_states == [
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "publish_token": "publish-token-7",
            "expected_state": expected_state,
            "state": target_state,
            "error": "broker unavailable" if publish_fails else "",
        }
    ]


def test_outbox_publish_cas_loss_is_not_counted_as_recovered() -> None:
    service, repository, _graph, _lease, _events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="awaiting_approval",
            result={"status": "awaiting_approval"},
        ),
    )
    repository.pending = [
        {
            "id": "job-42",
            "project_id": "project-42",
            "task_id": "TASK-42",
            "thread_id": "thread-42",
            "resume_revision": 7,
            "publish_state": "pending",
            "publish_token": "publish-token-7",
        }
    ]
    repository.publish_mark_result = False

    recovered = service.dispatch_pending(publisher=lambda **_kwargs: None)

    assert recovered == 0


def test_orphan_resume_delivery_uses_generic_missing_job_disposition() -> None:
    service, repository, graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
        ),
    )
    repository.job = None

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "missing_job_row"}
    assert events == ["get_job"]
    assert graph.calls == []


def test_resume_job_lookup_exception_uses_generic_retry_disposition() -> None:
    service, repository, graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
        ),
    )
    repository.lookup_error = RuntimeError("database unavailable")

    result = service.execute(_message())

    assert result == {"action": "retry", "reason": "job_lookup_failed"}
    assert events == ["get_job"]
    assert graph.calls == []


def test_invalid_orphan_resume_is_discarded_before_payload_validation() -> None:
    service, repository, graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
        ),
    )
    repository.job = None

    result = service.execute(
        {
            "message_type": "workflow_resume",
            "job_id": "job-42",
        }
    )

    assert result == {"action": "discard", "reason": "missing_job_row"}
    assert events == ["get_job"]
    assert graph.calls == []


def test_revision_mismatch_discards_without_claiming_fence() -> None:
    service, _repository, graph, _lease, events = _service(
        _job(revision=8),
        WorkflowResumeGraphOutcome.accepted(status="completed", result={"status": "completed"}),
    )

    result = service.execute(_message(revision=7))

    assert result == {"action": "discard", "reason": "workflow_resume_revision_mismatch"}
    assert "claim_execution" not in events
    assert graph.calls == []


@pytest.mark.parametrize("status", ["pending", "triage_needed", "retryable_interrupted_step"])
def test_disallowed_job_status_discards_without_claiming_or_effects(status: str) -> None:
    service, _repository, graph, lease, events = _service(
        _job(status=status),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )

    result = service.execute(_message())

    assert result == {
        "action": "discard",
        "reason": "workflow_resume_status_changed",
    }
    assert graph.calls == []
    assert lease.calls == []
    assert not any(event.startswith("effect:") for event in events)


def test_terminal_status_without_finalizing_state_discards_without_recovery() -> None:
    service, _repository, graph, lease, events = _service(
        _job(status="completed", state="pending"),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )

    result = service.execute(_message())

    assert result == {
        "action": "discard",
        "reason": "workflow_resume_status_changed",
    }
    assert graph.calls == []
    assert lease.calls == []
    assert "claim_finalizer" not in events


def test_not_started_resume_defers_and_acknowledges_original_delivery() -> None:
    service, repository, _graph, lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.not_started(reason="checkpointer_unavailable"),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_deferred"}
    assert events.index("graph") < events.index("stop:execution_heartbeat")
    assert events.index("stop:execution_heartbeat") < events.index("mark_retryable")
    assert "complete" not in events
    assert repository.calls[0] == (
        "claim_execution",
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "worker_id": "worker-1",
            "lease_token": "claim-proposal-token",
            "lease_seconds": WORKFLOW_RESUME_LEASE_SECONDS,
        },
    )
    assert lease.calls == [
        (
            "start_execution",
            {
                "job_id": "job-42",
                "resume_revision": 7,
                "lease_token": "execution-token",
            },
        )
    ]
    assert repository.calls[-1] == (
        "mark_retryable",
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "lease_token": "execution-token",
            "error": "checkpointer_unavailable",
        },
    )


def test_execution_claim_loss_discards_without_graph_or_heartbeat() -> None:
    service, repository, graph, lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="awaiting_approval",
            result={"status": "awaiting_approval"},
        ),
    )
    repository.claim = WorkflowResumeClaim(claimed=False, lease_token="")

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "workflow_resume_claim_lost"}
    assert graph.calls == []
    assert lease.calls == []
    assert events == ["get_job", "claim_execution"]


def test_graph_exception_defers_resume_and_acknowledges_original_delivery() -> None:
    events: list[str] = []
    repository = _Repository(_job(), events)
    service = WorkflowResumeExecutionService(
        repository=repository,
        graph=_RaisingGraph(events, RuntimeError("graph unavailable")),
        lease=_LeasePort(events),
        terminal=_Terminal(events),
        worker_id=lambda: "worker-1",
        token_factory=lambda: "execution-token",
    )

    result = service.execute(_message())

    assert result["action"] == "ack"
    assert "mark_retryable" in events
    assert "complete" not in events
    assert events.index("mark_retryable") < events.index("stop:execution_heartbeat")


@pytest.mark.parametrize(
    "interruption",
    [GeneratorExit(), SystemExit(2), KeyboardInterrupt()],
    ids=["generator-exit", "system-exit", "keyboard-interrupt"],
)
def test_graph_base_exception_propagates_after_stopping_execution_heartbeat_once(
    interruption: BaseException,
) -> None:
    events: list[str] = []
    repository = _Repository(_job(), events)
    service = WorkflowResumeExecutionService(
        repository=repository,
        graph=_RaisingGraph(events, interruption),
        lease=_LeasePort(events),
        terminal=_Terminal(events),
        worker_id=lambda: "worker-1",
        token_factory=lambda: "execution-token",
    )

    _raises_input_608_1 = _message()
    with pytest.raises(type(interruption)) as raised:
        service.execute(_raises_input_608_1)

    assert raised.value is interruption
    assert events.count("stop:execution_heartbeat") == 1
    assert events.index("graph") < events.index("stop:execution_heartbeat")
    assert "mark_retryable" not in events


def test_lost_execution_lease_discards_without_marking_complete() -> None:
    service, _repository, _graph, lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.for_retry(reason="workflow_resume_lease_lost"),
    )
    lease.execution_lost = True

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "workflow_resume_lease_lost"}
    assert "mark_retryable" not in events
    assert "complete" not in events


def test_repeated_approval_gate_uses_fenced_notification_finalization() -> None:
    service, repository, _graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="awaiting_approval",
            result={
                "status": "awaiting_approval",
                "approval": {
                    "approval_id": "gate-2",
                    "summary": "Review deployment plan",
                    "phase_id": "review",
                    "step_id": "approval",
                },
            },
        ),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_accepted"}
    assert "mark_finalizing" in events
    assert "effect:notification" in events
    assert not any(event in {"effect:lock_release", "effect:provider_sync"} for event in events)
    assert events.count("effect:notification") == 1
    assert repository.calls[-1] == (
        "complete",
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "finalizer_token": "finalizer-token",
        },
    )


def test_terminal_resume_persists_finalizing_then_journals_each_fenced_effect() -> None:
    service, repository, graph, lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed", "final_report": "done"},
            terminal=True,
        ),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_accepted"}
    assert events == [
        "get_job",
        "claim_execution",
        "start:execution_heartbeat",
        "graph",
        "stop:execution_heartbeat",
        "mark_finalizing",
        "claim_finalizer",
        "start:finalizer_heartbeat",
        "get_job",
        "effect:lock_release",
        "journal:lock_release",
        "effect:notification",
        "journal:notification",
        "effect:provider_sync",
        "journal:provider_sync",
        "complete",
        "stop:finalizer_heartbeat",
    ]
    assert graph.calls[0][0] == WorkflowResumeRequest(
        message=_message(),
        job_id="job-42",
        thread_id="thread-42",
        resume_revision=7,
        approval_id="gate-1",
        resume_value={"approved": True},
        approved=True,
        action="approve",
        comment="ship it",
        interrupt_payload={"stage": "review"},
        actor_username="alice",
    )
    assert repository.calls[:3] == [
        (
            "claim_execution",
            {
                "job_id": "job-42",
                "resume_revision": 7,
                "worker_id": "worker-1",
                "lease_token": "claim-proposal-token",
                "lease_seconds": WORKFLOW_RESUME_LEASE_SECONDS,
            },
        ),
        (
            "mark_finalizing",
            {
                "job_id": "job-42",
                "resume_revision": 7,
                "lease_token": "execution-token",
                "final_status": "completed",
                "result": {"status": "completed", "final_report": "done"},
                "error": None,
            },
        ),
        (
            "claim_finalizer",
            {
                "job_id": "job-42",
                "resume_revision": 7,
                "finalizer_token": "finalizer-token",
                "lease_seconds": WORKFLOW_RESUME_LEASE_SECONDS,
            },
        ),
    ]
    assert lease.calls == [
        (
            "start_execution",
            {
                "job_id": "job-42",
                "resume_revision": 7,
                "lease_token": "execution-token",
            },
        ),
        (
            "start_finalizer",
            {
                "job_id": "job-42",
                "resume_revision": 7,
                "finalizer_token": "finalizer-token",
            },
        ),
    ]
    assert [call for call in repository.calls if call[0] == "mark_finalization_step_completed"] == [
        (
            "mark_finalization_step_completed",
            {
                "job_id": "job-42",
                "resume_revision": 7,
                "finalizer_token": "finalizer-token",
                "step": step,
            },
        )
        for step in ("lock_release", "notification", "provider_sync")
    ]
    assert repository.calls[-1] == (
        "complete",
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "finalizer_token": "finalizer-token",
        },
    )


def test_terminal_resume_mark_finalizing_cas_loss_stops_before_finalizer() -> None:
    service, repository, _graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )
    repository.finalizing = False

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "workflow_resume_lease_lost"}
    assert events.count("stop:execution_heartbeat") == 1
    assert "claim_finalizer" not in events
    assert not any(event.startswith("effect:") for event in events)
    assert "complete" not in events


def test_terminal_resume_finalizer_claim_loss_runs_no_effects() -> None:
    service, repository, _graph, lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )
    repository.finalizer_claimed = False

    result = service.execute(_message())

    assert result == {
        "action": "discard",
        "reason": "workflow_resume_finalizer_claim_lost",
    }
    assert [name for name, _kwargs in lease.calls] == ["start_execution"]
    assert not any(event.startswith("effect:") for event in events)
    assert not any(event.startswith("journal:") for event in events)
    assert "complete" not in events


def test_terminal_recovery_finalizer_claim_loss_runs_no_effects() -> None:
    service, repository, graph, lease, events = _service(
        _job(status="completed", state="finalizing"),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )
    repository.finalizer_claimed = False

    result = service.execute(_message())

    assert result == {
        "action": "discard",
        "reason": "workflow_resume_finalizer_claim_lost",
    }
    assert graph.calls == []
    assert lease.calls == []
    assert not any(event.startswith("effect:") for event in events)
    assert not any(event.startswith("journal:") for event in events)
    assert "complete" not in events


def test_terminal_resume_journal_cas_loss_releases_exact_finalizer_fence() -> None:
    service, repository, _graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )
    repository.journal_results["lock_release"] = False

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_finalization_deferred"}
    assert "effect:lock_release" in events
    assert "journal:lock_release" in events
    assert "effect:notification" not in events
    assert "complete" not in events
    assert repository.calls[-1] == (
        "release_finalizer",
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "finalizer_token": "finalizer-token",
            "error": ("Lost approval-resume finalizer fence while recording lock_release"),
        },
    )


@pytest.mark.parametrize("effect_raises", [False, True])
def test_terminal_resume_lease_loss_between_effect_and_journal_skips_further_writes(
    effect_raises: bool,
) -> None:
    events: list[str] = []
    repository = _Repository(_job(), events)
    lease = _LeasePort(events)
    graph = _Graph(
        events,
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )

    def lose_fence(effect: str) -> None:
        lease.finalizer_lost = effect == "lock_release"
        if effect_raises:
            raise RuntimeError("side effect failed while ownership was lost")

    terminal = _Terminal(events, after_effect=lose_fence)
    service = WorkflowResumeExecutionService(
        repository=repository,
        graph=graph,
        lease=lease,
        terminal=terminal,
        worker_id=lambda: "worker-1",
        token_factory=iter(("claim-proposal-token", "finalizer-token")).__next__,
    )

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "workflow_resume_finalizer_lease_lost"}
    assert "effect:lock_release" in events
    assert "journal:lock_release" not in events
    assert "effect:notification" not in events
    assert "complete" not in events
    assert "release_finalizer" not in events
    assert events.count("stop:finalizer_heartbeat") == 1


def test_terminal_resume_preexisting_finalizer_lease_loss_runs_no_effects() -> None:
    service, _repository, _graph, lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )
    lease.finalizer_lost = True

    result = service.execute(_message())

    assert result == {
        "action": "discard",
        "reason": "workflow_resume_finalizer_lease_lost",
    }
    assert not any(event.startswith("effect:") for event in events)
    assert not any(event.startswith("journal:") for event in events)
    assert "complete" not in events
    assert "release_finalizer" not in events


def test_terminal_resume_unknown_nonempty_test_mode_skips_notification() -> None:
    job = _job()
    job["meta"]["test_mode"] = "enabled"
    service, _repository, _graph, _lease, events = _service(
        job,
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_accepted"}
    assert "effect:notification" not in events
    assert "journal:notification" in events


def test_new_terminal_completion_fence_loss_discards_as_execution_lease_loss() -> None:
    service, repository, _graph, _lease, _events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
            terminal=True,
        ),
    )
    repository.completed = False

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "workflow_resume_lease_lost"}


def test_explicit_terminal_flag_with_unknown_status_skips_terminal_effects() -> None:
    service, _repository, _graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="custom_terminal",
            result={"status": "custom_terminal"},
            terminal=True,
        ),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_accepted"}
    assert not any(event.startswith("effect:") for event in events)
    assert not any(event.startswith("journal:") for event in events)


def test_terminal_redelivery_recovers_remaining_journal_without_graph_replay() -> None:
    service, _repository, graph, _lease, events = _service(
        _job(
            status="completed",
            state="finalizing",
            finalization_steps={"lock_release": "completed"},
        ),
        WorkflowResumeGraphOutcome.accepted(status="completed", result={"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_finalized"}
    assert graph.calls == []
    assert "effect:lock_release" not in events
    assert events.index("claim_finalizer") < events.index("effect:notification")
    assert events.index("journal:provider_sync") < events.index("complete")


def test_repeated_gate_redelivery_does_not_repeat_completed_notification() -> None:
    service, _repository, graph, _lease, events = _service(
        _job(
            status="awaiting_approval",
            state="finalizing",
            finalization_steps={"notification": "completed"},
        ),
        WorkflowResumeGraphOutcome.accepted(
            status="awaiting_approval",
            result={"status": "awaiting_approval"},
        ),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_finalized"}
    assert graph.calls == []
    assert "effect:notification" not in events
    assert "journal:notification" not in events
    assert "complete" in events
    assert events.index("complete") < events.index("stop:finalizer_heartbeat")


def test_terminal_redelivery_with_incomplete_completion_retries_recovery() -> None:
    service, repository, graph, _lease, _events = _service(
        _job(status="completed", state="finalizing"),
        WorkflowResumeGraphOutcome.accepted(
            status="completed",
            result={"status": "completed"},
        ),
    )
    repository.completed = False

    result = service.execute(_message())

    assert result == {
        "action": "discard",
        "reason": "workflow_resume_finalization_incomplete",
    }
    assert graph.calls == []


def test_nonterminal_workflow_resume_uses_durable_notification_finalizer() -> None:
    service, repository, _graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status="awaiting_approval",
            result={
                "status": "awaiting_approval",
                "approval": {"approval_id": "gate-2", "summary": "Review again"},
            },
            terminal=False,
        ),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "reason": "workflow_resume_accepted"}
    assert "mark_finalizing" in events
    assert "claim_finalizer" in events
    assert "effect:notification" in events
    assert repository.calls[-1] == (
        "complete",
        {
            "job_id": "job-42",
            "resume_revision": 7,
            "finalizer_token": "finalizer-token",
        },
    )


@pytest.mark.parametrize("state", ["pending", "published", "publish_failed"])
def test_a_timer_wake_after_a_cancel_never_continues_the_run(state: str) -> None:
    """T20: the timer scan stored the wake, then a cancel (a reaction, the API)
    terminalized the job before the worker claimed it. One outcome: cancelled."""
    job = _job(status="cancelled", state=state)
    job["meta"]["workflow_resume"].update(
        {
            "reason": "timer",
            "approval_id": "",
            "resume_value": {"reason": "timer", "wait_id": "w-1", "timer_elapsed": True},
            "approved": False,
            "action": None,
            "actor_username": "workflow_timer",
        }
    )
    service, _repository, graph, lease, events = _service(
        job,
        WorkflowResumeGraphOutcome.accepted(status="completed", result={}, terminal=True),
    )

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "workflow_resume_status_changed"}
    assert graph.calls == []
    assert lease.calls == []
    assert "claim_execution" not in events


@pytest.mark.parametrize(
    ("status", "event_type"),
    [("awaiting_timer", "job.awaiting_timer"), ("awaiting_child", "")],
)
def test_a_resume_that_parks_again_reports_its_wait_through_the_notification_step(
    status: str, event_type: str
) -> None:
    """A Wait timer reaches workflow reactions; a child wait catches up a pending
    child approval. Neither releases the lock or syncs the provider."""
    service, _repository, _graph, _lease, events = _service(
        _job(),
        WorkflowResumeGraphOutcome.accepted(
            status=status,
            result={"status": status, "workflow_wait": {"kind": status.split("_")[1]}},
            terminal=False,
        ),
    )

    service.execute(_message())

    assert "effect:notification" in events
    assert "effect:lock_release" not in events
    assert "effect:provider_sync" not in events
    context = service._terminal.calls[-1][1]
    assert context.notification_event_type == event_type


def test_deferred_poison_resume_does_not_block_next_project_queue_message() -> None:
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.infrastructure.runtime.worker.queue import _drain_project_queue

    events: list[str] = []
    service = WorkflowResumeExecutionService(
        repository=_Repository(_job(), events),
        graph=_RaisingGraph(events, RuntimeError("poisoned resume")),
        lease=_LeasePort(events),
        terminal=_Terminal(events),
        worker_id=lambda: "worker-1",
        token_factory=lambda: "execution-token",
    )
    channel = MagicMock()
    channel.basic_get.side_effect = [
        (SimpleNamespace(delivery_tag="resume"), None, json.dumps(_message()).encode()),
        (SimpleNamespace(delivery_tag="next"), None, b'{"job_id":"next-job"}'),
        (None, None, None),
    ]
    following = []

    def callback(message):
        if message["job_id"] == "next-job":
            following.append(message["job_id"])
            return {"action": "ack"}
        return service.execute(message)

    assert _drain_project_queue(
        channel, project_id="project-1", callback=callback, worker_id="worker-1"
    ) == (2, False)
    assert following == ["next-job"]
    assert channel.basic_ack.call_count == 2
    channel.basic_nack.assert_not_called()


def test_exhausted_resume_runs_canonical_terminal_cleanup_before_completion() -> None:
    events: list[str] = []
    repository = _Repository(_job(), events)
    repository.claim = WorkflowResumeClaim(claimed=True, lease_token="execution-token", attempts=3)
    service = WorkflowResumeExecutionService(
        repository=repository,
        graph=_RaisingGraph(events, RuntimeError("poisoned resume")),
        lease=_LeasePort(events),
        terminal=_Terminal(events),
        worker_id=lambda: "worker-1",
        token_factory=lambda: "finalizer-token",
    )

    result = service.execute(_message())

    assert result["action"] == "ack"
    assert "complete_failed" in events
    assert "effect:lock_release" in events
    assert "effect:notification" in events
    assert "effect:provider_sync" in events
    assert events.index("complete_failed") < events.index("effect:lock_release")
    assert events.index("effect:provider_sync") < events.index("complete")
