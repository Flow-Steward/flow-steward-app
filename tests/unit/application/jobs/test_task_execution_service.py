from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, get_type_hints

import pytest

from core.application.jobs.execution_contracts import TaskClaimResult, TaskGraphOutcome
from core.application.jobs.task_execution_service import TaskExecutionService
from tests.fixtures.mcp_streamable_http import legacy_mcp_server


def test_step_test_run_context_is_restored_only_from_trusted_job_meta() -> None:
    from core.application.jobs.task_execution_request import _build_request

    message = {
        "job_id": "job-step-1",
        "task_id": "task-step-1",
        "thread_id": "thread-step-1",
        "task_summary": "Run selected step",
        "task_source": "step_test_run",
        "project_name": "Demo",
        "project_config": {"project_id": "project-1"},
        "workflow_id": "wf-acme-store",
        "extra_state": {
            "step_test_run_context": {"step_id": "forged-step"},
            "run_cursor": {"step_id": "forged-step", "step_execution_id": "forged", "attempt": 1},
        },
    }
    job_row = {
        "id": "job-step-1",
        "task_source": "step_test_run",
        "selected_workflow": "wf-acme-store",
        "meta": {
            "selected_workflow_id": "wf-acme-store",
            "step_test_run": True,
            "test_mode": True,
            "step_id": "acme-store-locations",
            "execution_id": "exec-step-1",
        },
    }

    request = _build_request(message, job_row)

    assert request.extra_state["step_test_run_context"] == {
        "workflow_id": "wf-acme-store",
        "step_id": "acme-store-locations",
        "execution_id": "exec-step-1",
    }
    # The cursor is derived from trusted meta; the delivered one is replaced.
    cursor = request.extra_state["run_cursor"]
    assert cursor["step_id"] == "acme-store-locations"
    assert cursor["step_execution_id"] != "forged"
    assert request.extra_state["workflow_test_run_execution_id"] == "exec-step-1"
    assert request.test_mode is True


def test_async_step_test_run_applies_bound_inputs_to_runtime_state() -> None:
    from core.application.jobs.task_execution_request import _build_request

    message = {
        "job_id": "job-step-binding",
        "task_id": "task-step-binding",
        "thread_id": "thread-step-binding",
        "task_summary": "Run HTTP step with a bound body value",
        "task_source": "step_test_run",
        "project_name": "Demo",
        "project_config": {"project_id": "project-1"},
        "workflow_id": "wf-http",
        "extra_state": {
            "task_envelope": {
                "inputs": {"body.source": True},
                "metadata": {
                    "source": "step_test_run",
                    "input_bindings": [
                        {
                            "name": "body.source",
                            "source": "state.workflow_started",
                            "type": "boolean",
                        }
                    ],
                },
            },
            "inputs": {"body.source": True},
            "phase_stack": [
                {
                    "phase_id": "http",
                    "steps": [{"step_id": "request", "step_kind": "http_request"}],
                    "_step_test_run_dispatch_pending": True,
                }
            ],
        },
    }
    job_row = {
        "id": "job-step-binding",
        "task_source": "step_test_run",
        "selected_workflow": "wf-http",
        "meta": {
            "selected_workflow_id": "wf-http",
            "step_test_run": True,
            "test_mode": True,
            "phase_id": "http",
            "step_id": "request",
            "execution_id": "exec-step-binding",
        },
    }

    request = _build_request(message, job_row)

    assert request.extra_state["workflow_started"] is True
    assert request.extra_state["workflow_data"]["workflow_started"] is True


class _Repository:
    available = True

    def __init__(
        self,
        job: dict[str, Any] | None = None,
        *,
        terminal_ledger: list[str] | None = None,
    ) -> None:
        self.job = job
        self.project: dict[str, Any] | None = {"id": "project-1"}
        self.project_error: Exception | None = None
        self.claim_result = TaskClaimResult(claimed=True)
        self.claim_error: Exception | None = None
        self.interrupted_result = False
        self.owner_lease_active: bool | None = None
        self.recover_result = False
        self.cancelled_during_execution = False
        self.log_error_failure: Exception | None = None
        self.events: list[tuple[str, Any]] = []
        self.terminal_ledger = terminal_ledger

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.events.append(("get_job", job_id))
        return dict(self.job) if self.job is not None else None

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        self.events.append(("get_project", project_id))
        if self.project_error is not None:
            raise self.project_error
        return dict(self.project) if self.project is not None else None

    def persist_meta(self, job_id: str, meta: dict[str, Any]) -> None:
        self.events.append(("persist_meta", (job_id, dict(meta))))

    def mark_delivery_terminal(self, job_id: str, *, status: str, error: str) -> None:
        self.events.append(("delivery_terminal", (job_id, status, error)))

    def interrupted_step_snapshot(self, job_row: dict[str, Any]) -> dict[str, Any] | None:
        self.events.append(("interrupted_snapshot", dict(job_row)))
        return None

    def mark_retryable_interrupted_step(
        self,
        job_id: str,
        job_row: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> bool:
        self.events.append(("mark_interrupted", (job_id, dict(job_row), snapshot)))
        return self.interrupted_result

    def in_progress_owner_has_active_lease(
        self,
        job_row: dict[str, Any],
    ) -> bool | None:
        self.events.append(("inspect_owner_lease", dict(job_row)))
        return self.owner_lease_active

    def recover_in_progress(
        self,
        job_id: str,
        *,
        expected_worker_id: str,
        reason: str,
    ) -> bool:
        self.events.append(("recover_in_progress", (job_id, expected_worker_id, reason)))
        return self.recover_result

    def claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        allow_in_progress: bool,
    ) -> TaskClaimResult:
        self.events.append(("claim", (job_id, worker_id, allow_in_progress)))
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim_result

    def is_cancelled(self, job_id: str) -> bool:
        self.events.append(("is_cancelled", job_id))
        return self.cancelled_during_execution

    def merge_meta(self, job_id: str, patch: dict[str, Any]) -> None:
        self.events.append(("merge_meta", (job_id, dict(patch))))

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        final_report: Any,
        error: str | None,
        root_run_id: str | None,
    ) -> None:
        self.events.append(("finish", (job_id, status, final_report, error, root_run_id)))
        if self.terminal_ledger is not None:
            self.terminal_ledger.append("finish")

    def release_lock(self, job_id: str, *, reason: str, worker_id: str) -> None:
        self.events.append(("release_lock", (job_id, reason, worker_id)))
        if self.terminal_ledger is not None:
            self.terminal_ledger.append("release_lock")

    def log_execution_error(
        self,
        job_id: str,
        error: Exception,
        *,
        worker_id: str,
        task_id: str,
        thread_id: str,
        root_run_id: str | None,
    ) -> None:
        self.events.append(("log_error", (job_id, str(error), root_run_id)))
        if self.log_error_failure is not None:
            raise self.log_error_failure


class _Graph:
    def __init__(self, outcome: TaskGraphOutcome) -> None:
        self.outcome = outcome
        self.requests: list[Any] = []

    def run(self, request: Any, *, job_row: Any, project_id: str | None) -> TaskGraphOutcome:
        self.requests.append((request, job_row, project_id))
        return self.outcome


class _OwnedMcpClient:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _McpUsingGraph(_Graph):
    def __init__(
        self,
        outcome: TaskGraphOutcome,
        *,
        run_error: Exception | None = None,
    ) -> None:
        super().__init__(outcome)
        self.run_error = run_error
        self.clients: list[_OwnedMcpClient] = []

    def run(self, request: Any, *, job_row: Any, project_id: str | None) -> TaskGraphOutcome:
        from core.application.management.mcp_connector_client import get_execution_mcp_client

        self.requests.append((request, job_row, project_id))

        def _client_factory() -> _OwnedMcpClient:
            client = _OwnedMcpClient()
            self.clients.append(client)
            return client

        get_execution_mcp_client(
            account_id=str((job_row or {}).get("account_id") or "").strip(),
            job_id=request.job_id,
            connector_id="mcp-test",
            client_factory=_client_factory,
        )
        if self.run_error is not None:
            raise self.run_error
        return self.outcome


class _Terminal:
    def __init__(self, terminal_ledger: list[str] | None = None) -> None:
        self.events: list[tuple[str, Any]] = []
        self.terminal_ledger = terminal_ledger

    def _record(self, label: str, detail: Any) -> None:
        self.events.append((label, detail))
        if self.terminal_ledger is not None:
            self.terminal_ledger.append(label)

    def sync_provider(self, job_id: str, *, final_status: str, repository: Any) -> None:
        self._record("provider", final_status)

    def notify(self, notification: Any, *, repository: Any) -> None:
        self._record("notify", notification)


class _RuntimeMode:
    def __init__(self) -> None:
        self.values: list[bool] = []

    @contextmanager
    def activate(self, enabled: bool):
        self.values.append(enabled)
        yield


def _message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "job_id": "job-1",
        "task_id": "TASK-1",
        "thread_id": "thread-1",
        "task_summary": "Characterize worker policy",
        "task_source": "manual",
        "project_name": "demo",
        "project_config": {},
        "extra_state": {},
    }
    message.update(overrides)
    return message


def _service(
    repository: _Repository | None,
    outcome: TaskGraphOutcome,
) -> tuple[TaskExecutionService, _Graph, _Terminal, _RuntimeMode]:
    graph = _Graph(outcome)
    terminal = _Terminal(repository.terminal_ledger if repository is not None else None)
    runtime_mode = _RuntimeMode()
    return (
        TaskExecutionService(
            repository=repository,
            graph=graph,
            terminal=terminal,
            runtime_mode=runtime_mode,
            worker_id=lambda: "worker-1",
        ),
        graph,
        terminal,
        runtime_mode,
    )


def _service_with_graph(
    repository: _Repository,
    graph: _Graph,
) -> TaskExecutionService:
    return TaskExecutionService(
        repository=repository,
        graph=graph,
        terminal=_Terminal(repository.terminal_ledger),
        runtime_mode=_RuntimeMode(),
        worker_id=lambda: "worker-1",
    )


def _error_with_traceback(detail: str) -> RuntimeError:
    try:
        raise RuntimeError(detail)
    except RuntimeError as exc:
        return exc


def test_task_graph_outcome_error_contract_excludes_base_exceptions() -> None:
    hints = get_type_hints(TaskGraphOutcome)
    failed_hints = get_type_hints(TaskGraphOutcome.failed)

    assert hints["error"] == Exception | None
    assert failed_hints["error"] is Exception


def test_invalid_delivery_dead_letters_without_starting_graph() -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message(task_summary="", task_source=""))

    assert result == {"action": "dead_letter", "reason": "invalid_payload"}
    assert graph.requests == []
    terminal_events = [event for event in repository.events if event[0] == "delivery_terminal"]
    assert terminal_events
    assert terminal_events[-1][1][1] == "triage_needed"
    assert "task_source, task_summary" in terminal_events[-1][1][2]


def test_missing_project_reference_dead_letters_before_claim_or_graph() -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-missing", "status": "pending", "meta": {}}
    )
    repository.project = None
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "dead_letter", "reason": "missing_project_reference"}
    assert graph.requests == []
    assert not any(event[0] == "claim" for event in repository.events)
    assert any(
        event[0] == "delivery_terminal" and event[1][1] == "triage_needed"
        for event in repository.events
    )


def test_in_progress_redelivery_with_unknown_owner_is_discarded_without_replaying_graph() -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "project_id": "project-1",
            "status": "in_progress",
            "meta": {},
        }
    )
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "duplicate_in_progress"}
    assert graph.requests == []
    assert any(event[0] == "inspect_owner_lease" for event in repository.events)
    assert not any(event[0] == "mark_interrupted" for event in repository.events)
    assert not any(event[0] == "claim" for event in repository.events)


def test_rabbit_redelivery_waits_for_the_active_owner_lease_without_losing_the_job() -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "project_id": "project-1",
            "status": "in_progress",
            "meta": {},
        }
    )
    repository.owner_lease_active = True
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute({**_message(), "_queue_redelivered": True})

    assert result == {"action": "retry", "reason": "active_owner_redelivery"}
    assert graph.requests == []
    assert any(event[0] == "inspect_owner_lease" for event in repository.events)
    assert not any(event[0] == "claim" for event in repository.events)


def test_already_actionable_redelivery_discards_without_recovery_or_graph() -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "project_id": "project-1",
            "status": "retryable_interrupted_step",
            "meta": {},
        }
    )
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {
        "action": "discard",
        "reason": "retryable_interrupted_step",
    }
    assert graph.requests == []
    assert not any(event[0] == "interrupted_snapshot" for event in repository.events)
    assert not any(event[0] == "claim" for event in repository.events)


@pytest.mark.parametrize(
    ("meta", "action"),
    [
        ({}, "retry"),
        (
            {
                "project_policy_snapshot": {"retry_max_attempts": 1},
                "queue_delivery": {
                    "retry_count": 1,
                    "policy": {"retry_max_attempts": 1},
                },
            },
            "dead_letter",
        ),
    ],
)
def test_claim_exception_retries_until_delivery_policy_is_exhausted(
    meta: dict[str, Any],
    action: str,
) -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": meta}
    )
    repository.claim_error = RuntimeError("claim store unavailable")
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": action, "reason": "job_claim_failed"}
    assert graph.requests == []
    terminal_events = [event for event in repository.events if event[0] == "delivery_terminal"]
    if action == "dead_letter":
        assert terminal_events[-1][1][1] == "triage_needed"
    else:
        assert terminal_events == []


def test_serialized_delivery_meta_preserves_exhausted_retry_disposition() -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "project_id": "project-1",
            "status": "pending",
            "meta": json.dumps(
                {
                    "project_policy_snapshot": {"retry_max_attempts": 1},
                    "queue_delivery": {
                        "retry_count": 1,
                        "policy": {"retry_max_attempts": 1},
                    },
                }
            ),
        }
    )
    repository.project_error = RuntimeError("project store unavailable")
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "dead_letter", "reason": "project_lookup_failed"}
    assert graph.requests == []
    assert any(event[0] == "delivery_terminal" for event in repository.events)


def test_cancelled_delivery_is_discarded_before_atomic_claim() -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "cancelled", "meta": {}}
    )
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "cancelled_before_claim"}
    assert graph.requests == []
    assert not any(event[0] == "claim" for event in repository.events)
    persisted = [event[1][1] for event in repository.events if event[0] == "persist_meta"]
    assert persisted[-1]["queue_delivery"]["last_failure_code"] == "cancelled_after_enqueue"


def test_missing_job_delivery_is_discarded_without_broker_retry() -> None:
    repository = _Repository(None)
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "missing_job_row"}
    assert graph.requests == []
    assert not any(event[0] == "claim" for event in repository.events)


def test_terminal_job_delivery_is_discarded_after_claim_fence() -> None:
    terminal_job = {
        "id": "job-1",
        "project_id": "project-1",
        "status": "completed",
        "meta": {},
    }
    repository = _Repository(terminal_job)
    repository.claim_result = TaskClaimResult(claimed=False, current_job=terminal_job)
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "job_status_changed"}
    assert graph.requests == []


def test_failed_atomic_claim_discards_status_race_with_stable_reason() -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    repository.claim_result = TaskClaimResult(
        claimed=False,
        current_job={
            "id": "job-1",
            "project_id": "project-1",
            "status": "cancelled",
            "meta": {},
        },
    )
    service, graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "discard", "reason": "job_status_changed"}
    assert graph.requests == []
    persisted = [event[1][1] for event in repository.events if event[0] == "persist_meta"]
    assert persisted[-1]["queue_delivery"]["last_failure_code"] == "cancelled_after_dequeue"


def test_graph_cancellation_acks_and_runs_normal_terminal_sequence() -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    service, graph, terminal, runtime_mode = _service(
        repository,
        TaskGraphOutcome.cancelled(),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "claimed": True, "final_status": "cancelled"}
    assert len(graph.requests) == 1
    assert runtime_mode.values == [False]
    assert terminal.events[0] == ("provider", "cancelled")
    assert any(event[0] == "finish" and event[1][1] == "cancelled" for event in repository.events)
    assert any(event[0] == "release_lock" for event in repository.events)
    assert [event[0] for event in terminal.events] == ["provider", "notify"]


@pytest.mark.parametrize(
    ("outcome", "run_error", "expected_status"),
    [
        (TaskGraphOutcome.from_result({"status": "completed"}), None, "completed"),
        (TaskGraphOutcome.from_result({"status": "failed"}), None, "failed"),
        (TaskGraphOutcome.cancelled(), None, "cancelled"),
        (
            TaskGraphOutcome.from_result({"status": "in_progress"}, pending_interrupt=True),
            None,
            "awaiting_approval",
        ),
        (
            TaskGraphOutcome.from_result({"status": "completed"}),
            TimeoutError("graph attempt timed out"),
            "failed",
        ),
    ],
    ids=["success", "failure", "cancellation", "awaiting-approval", "timeout"],
)
def test_run_claimed_closes_execution_mcp_clients_for_every_normal_exit(
    outcome: TaskGraphOutcome,
    run_error: Exception | None,
    expected_status: str,
) -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "account_id": "acc_alpha",
            "project_id": "project-1",
            "status": "pending",
            "meta": {},
        }
    )
    graph = _McpUsingGraph(outcome, run_error=run_error)
    service = _service_with_graph(repository, graph)

    result = service.execute(_message())

    assert result == {"action": "ack", "claimed": True, "final_status": expected_status}
    assert len(graph.clients) == 1
    assert graph.clients[0].close_calls == 1


def test_run_claimed_closes_execution_mcp_clients_before_generator_exit_reraises() -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "account_id": "acc_alpha",
            "project_id": "project-1",
            "status": "pending",
            "meta": {},
        }
    )
    graph = _McpUsingGraph(TaskGraphOutcome.generator_exit())
    service = _service_with_graph(repository, graph)

    _raises_input_665_1 = _message()
    with pytest.raises(GeneratorExit):
        service.execute(_raises_input_665_1)

    assert len(graph.clients) == 1
    assert graph.clients[0].close_calls == 1


def test_awaiting_approval_closes_real_legacy_session_at_attempt_boundary(monkeypatch) -> None:
    from core.application.auth.mcp_connector_oauth import mcp_connector_rpc_post

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    repository = _Repository(
        {
            "id": "job-1",
            "account_id": "acc_alpha",
            "project_id": "project-1",
            "status": "pending",
            "meta": {},
        }
    )

    with legacy_mcp_server() as fixture:
        connector_row = {
            "connector_id": "mcp_worker_fixture",
            "account_id": "acc_alpha",
            "transport": "streamable_http",
            "base_url": fixture.url,
            "auth_mode": "none",
            "headers": {},
        }

        class _FixtureGraph(_Graph):
            def run(
                self,
                request: Any,
                *,
                job_row: Any,
                project_id: str | None,
            ) -> TaskGraphOutcome:
                self.requests.append((request, job_row, project_id))
                mcp_connector_rpc_post(
                    object(),
                    connector_row=connector_row,
                    payload={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "browser_navigate",
                            "arguments": {"url": "https://example.test/approval"},
                        },
                    },
                    timeout_seconds=2.0,
                    runtime_context={
                        "account_id": str(job_row.get("account_id") or ""),
                        "job_id": request.job_id,
                    },
                )
                return self.outcome

        graph = _FixtureGraph(
            TaskGraphOutcome.from_result(
                {"status": "in_progress"},
                pending_interrupt=True,
            )
        )
        service = _service_with_graph(repository, graph)

        result = service.execute(_message())

        assert result == {
            "action": "ack",
            "claimed": True,
            "final_status": "awaiting_approval",
        }
        assert len(fixture.sessions) == 1
        assert fixture.sessions[0].current_url == "https://example.test/approval"
        assert fixture.sessions[0].closed is True
        assert fixture.delete_count == 1


def test_graph_exception_after_operator_cancellation_is_not_logged_as_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    repository.cancelled_during_execution = True
    service, _graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.failed(_error_with_traceback("late graph failure")),
    )

    with caplog.at_level(
        logging.ERROR,
        logger="core.application.jobs.task_execution_service",
    ):
        result = service.execute(_message())

    assert result == {"action": "ack", "claimed": True, "final_status": "cancelled"}
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert not [event for event in repository.events if event[0] == "log_error"]


def test_completed_graph_uses_one_ledger_for_normal_terminal_order() -> None:
    ledger: list[str] = []
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}},
        terminal_ledger=ledger,
    )
    service, _graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "completed"}),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "claimed": True, "final_status": "completed"}
    assert ledger == ["finish", "release_lock", "provider", "notify"]


def test_generator_exit_without_operator_cancellation_is_re_raised_after_one_check() -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    service, _graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.generator_exit(),
    )

    _raises_input_795_1 = _message()
    with pytest.raises(GeneratorExit):
        service.execute(_raises_input_795_1)

    assert [event for event in repository.events if event[0] == "is_cancelled"] == [
        ("is_cancelled", "job-1")
    ]
    assert any(event[0] == "finish" and event[1][1] == "failed" for event in repository.events)


def test_nonterminal_graph_result_is_coerced_to_failed_without_broker_retry() -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    service, _graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "in_progress"}, pending_interrupt=False),
    )

    result = service.execute(_message())

    assert result == {"action": "ack", "claimed": True, "final_status": "failed"}
    finish = [event[1] for event in repository.events if event[0] == "finish"][-1]
    assert "non-terminal status 'in_progress'" in str(finish[3])


def test_graph_exception_is_logged_with_context_and_traceback_without_repository(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = _error_with_traceback("private graph failure detail")
    service, _graph, _terminal, _mode = _service(
        None,
        TaskGraphOutcome.failed(error, root_run_id="run-42"),
    )

    with caplog.at_level(
        logging.ERROR,
        logger="core.application.jobs.task_execution_service",
    ):
        result = service.execute(_message())

    assert result == {"action": "ack", "claimed": False, "final_status": "failed"}
    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "job_id=job-1 task_id=TASK-1 thread_id=thread-1 worker_id=worker-1 "
        "root_run_id=run-42 graph execution failed"
    )
    rendered = logging.Formatter().format(records[0])
    assert "private graph failure detail" not in rendered
    assert "Traceback (most recent call last)" in rendered
    assert "_error_with_traceback" in rendered
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is not error


def test_graph_exception_still_logs_once_when_durable_error_persistence_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    repository.log_error_failure = RuntimeError("error store unavailable")
    error = _error_with_traceback("private provider response")
    service, _graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.failed(error, root_run_id="run-43"),
    )

    with caplog.at_level(
        logging.ERROR,
        logger="core.application.jobs.task_execution_service",
    ):
        result = service.execute(_message())

    assert result == {"action": "ack", "claimed": True, "final_status": "failed"}
    assert [event[0] for event in repository.events].count("log_error") == 1
    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(records) == 1
    rendered = logging.Formatter().format(records[0])
    assert "private provider response" not in rendered
    assert "Traceback (most recent call last)" in rendered
    assert "_error_with_traceback" in rendered
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is not error


def test_durable_error_persistence_debug_log_redacts_exception_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    repository.log_error_failure = RuntimeError(
        "postgresql://secret-user:secret-password@database/internal"
    )
    error = _error_with_traceback("private provider response")
    service, _graph, _terminal, _mode = _service(
        repository,
        TaskGraphOutcome.failed(error, root_run_id="run-43"),
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="core.application.jobs.task_execution_service",
    ):
        service.execute(_message())

    records = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG
        and "structured error persistence failed" in record.getMessage()
    ]
    assert len(records) == 1
    assert "secret-password" not in logging.Formatter().format(records[0])
    assert records[0].exc_info is None


def test_failed_graph_result_warns_without_sensitive_result_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _graph, _terminal, _mode = _service(
        None,
        TaskGraphOutcome.from_result(
            {"status": "failed", "error": "private graph result detail"},
            root_run_id="run-44",
        ),
    )

    with caplog.at_level(logging.WARNING, logger="core.application.jobs.task_execution_service"):
        result = service.execute(_message())

    assert result == {"action": "ack", "claimed": False, "final_status": "failed"}
    records = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.getMessage() for record in records] == [
        "job_id=job-1 task_id=TASK-1 thread_id=thread-1 worker_id=worker-1 "
        "root_run_id=run-44 graph returned failure status=failed"
    ]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "private graph result detail" not in caplog.text


def test_awaiting_approval_keeps_lock_and_test_mode_skips_external_effects() -> None:
    repository = _Repository(
        {
            "id": "job-1",
            "project_id": "project-1",
            "status": "pending",
            "meta": {"test_mode": True},
        }
    )
    service, _graph, terminal, runtime_mode = _service(
        repository,
        TaskGraphOutcome.from_result({"status": "in_progress"}, pending_interrupt=True),
    )

    result = service.execute(_message())

    assert result == {
        "action": "ack",
        "claimed": True,
        "final_status": "awaiting_approval",
    }
    assert runtime_mode.values == [True]
    assert terminal.events == []
    assert not any(event[0] == "release_lock" for event in repository.events)


def _notification_request() -> Any:
    from core.application.jobs.execution_contracts import TaskExecutionRequest

    return TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="T-1",
        thread_id="thread-1",
        task_summary="Run",
        task_source="api",
        project_name="demo",
        project_config={},
        extra_state={},
        workflow_id="wf",
        test_mode=False,
    )


def test_a_timer_wait_notifies_workflow_reactions_with_its_wait_identity() -> None:
    from types import SimpleNamespace

    from core.application.jobs.task_execution_service import _normal_notification

    wait = {
        "wait_id": "t-1",
        "kind": "timer",
        "step_id": "pause",
        "step_execution_id": "x-1",
        "attempt": 1,
        "group_id": "batch",
        "due_at": "2026-10-13T00:00:00+00:00",
        "due_epoch_ms": 1,
    }
    state = SimpleNamespace(
        values={"pending_wait": wait},
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={**wait, "gate_kind": "wait"})])],
    )

    notification = _normal_notification(
        _notification_request(),
        "p-1",
        "awaiting_timer",
        {},
        graph_outcome=TaskGraphOutcome(kind="result", interrupt_state=state),
    )

    assert notification is not None
    assert notification.event_type == "job.awaiting_timer"
    # The project's notification routes never receive a timer start.
    assert notification.dispatch_routes is False
    assert notification.payload["wait_id"] == "t-1"
    assert notification.payload["group_id"] == "batch"
    assert notification.payload["due_at"] == "2026-10-13T00:00:00+00:00"


def test_a_child_wait_sends_no_public_event() -> None:
    from core.application.jobs.task_execution_service import _normal_notification

    assert _normal_notification(_notification_request(), "p-1", "awaiting_child", {}) is None


def test_a_typed_graph_refusal_keeps_its_code_for_the_terminal_writer() -> None:
    from types import SimpleNamespace

    from core.application.orchestration.checkpoint_requirement import (
        CheckpointUnavailableError,
    )

    error = CheckpointUnavailableError("checkpoint storage is not configured")
    service, _graph, _terminal, _mode = _service(None, TaskGraphOutcome.failed(error))
    request = SimpleNamespace(job_id="job-1", task_id="TASK-1", thread_id="thread-1")

    status, result = service._interpret_graph_outcome(request, TaskGraphOutcome.failed(error))

    assert status == "failed"
    assert result["error_code"] == "checkpoint_unavailable"
    assert "not configured" in result["error"]


def test_parent_cancel_provenance_survives_normal_execution_policy():
    result = {
        "error_code": "parent_cancelled",
        "error": "Cancelled because its parent workflow was cancelled",
    }
    outcome = TaskGraphOutcome(kind="cancelled", result=result)
    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    service, _graph, terminal, _mode = _service(repository, outcome)
    persisted = []
    terminal.persist = lambda _request, **kwargs: persisted.append(kwargs) or kwargs["final_status"]

    delivery = service.execute(_message())

    assert delivery["final_status"] == "cancelled"
    assert persisted[0]["result"] == {**result, "status": "cancelled"}


def test_fence_read_outage_is_normal_infrastructure_failure_not_cancellation():
    from core.application.jobs.invoke_child_link_repository import (
        InvokeCancellationFenceUnavailable,
    )

    repository = _Repository(
        {"id": "job-1", "project_id": "project-1", "status": "pending", "meta": {}}
    )
    service, _graph, terminal, _mode = _service(
        repository, TaskGraphOutcome.failed(InvokeCancellationFenceUnavailable())
    )
    persisted = []
    terminal.persist = lambda _request, **kwargs: persisted.append(kwargs) or kwargs["final_status"]
    delivery = service.execute(_message())
    assert delivery["final_status"] == "failed"
    assert persisted[0]["result"]["error_code"] == "invoke_cancellation_fence_unavailable"
    assert not any(event[0] == "mark_interrupted" for event in repository.events)
