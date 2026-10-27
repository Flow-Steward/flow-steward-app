from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.application.jobs.public_progress_service import PublicProgressService
from core.application.jobs.terminal_persistence import (
    TerminalPersistenceConflict,
    TerminalPersistenceService,
)
from core.application.jobs.workflow_run_models import WorkflowRunStateV1

NOW = datetime(2026, 7, 15, 11, 0, tzinfo=UTC)


class _Repository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.value = None

    def load(self, *, job_id: str):
        return self.value

    def atomic_update(self, *, job_id: str, update):
        candidate = update(self.value)
        assert candidate is not None
        self.events.append("snapshot")
        self.events.append("marker")
        self.value = candidate
        return candidate, "marker-1", True


class _Logger:
    def __init__(self, events: list[str]) -> None:
        self.conn = object()
        self.events = events
        self.job = {
            "id": "job-1",
            "project_id": "project-1",
            "selected_workflow": "workflow-1",
            "status": "in_progress",
            "started_at": NOW,
            "queued_at": NOW,
            "meta": {},
        }
        self.job_errors: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []

    def get_job(self, job_id: str) -> dict[str, Any]:
        return dict(self.job)

    def merge_job_meta(self, job_id: str, patch: dict[str, Any]) -> None:
        self.events.append("outputs")
        self.job["meta"] = {**self.job["meta"], **patch}

    def finish_job(self, job_id: str, **kwargs: Any) -> None:
        self.events.append(f"status:{kwargs['status']}")

    def log_job_error(self, job_id: str, **kwargs: Any) -> None:
        self.events.append("job_error")
        self.job_errors.append({"job_id": job_id, **kwargs})

    def log_action(self, project_id: str, task_id: str, task_summary: str, **kwargs: Any) -> None:
        self.events.append("terminal_log")
        self.actions.append(
            {
                "project_id": project_id,
                "task_id": task_id,
                "task_summary": task_summary,
                **kwargs,
            }
        )


def _service(
    events: list[str],
    *,
    resolve_artifact=None,
    repository_holder: list[_Repository] | None = None,
) -> TerminalPersistenceService:
    repository = _Repository(events)
    if repository_holder is not None:
        repository_holder.append(repository)

    @contextmanager
    def transaction(_db):
        events.append("begin")
        cursor = MagicMock()
        cursor.fetchone.return_value = {"status": "in_progress", "worker_id": None}
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        try:
            yield conn
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    return TerminalPersistenceService(
        _Logger(events),
        transaction_factory=transaction,
        progress_repository_factory=lambda _conn: repository,
        now=lambda: NOW,
        resolve_artifact=resolve_artifact,
    )


def test_success_persists_outputs_snapshot_marker_before_observable_completed_status() -> None:
    events: list[str] = []

    result = _service(events).complete_success(
        job_id="job-1",
        workflow_outputs={"outputs": {"ticket": "T-1"}, "artifacts": {}},
        final_report="internal report",
    )

    assert result.status == "completed"
    assert events == [
        "begin",
        "outputs",
        "snapshot",
        "marker",
        "status:completed",
        "commit",
    ]


def test_success_authenticates_artifact_before_persisting_public_snapshot() -> None:
    events: list[str] = []
    repositories: list[_Repository] = []
    service = _service(
        events,
        resolve_artifact=lambda artifact_id: {
            "artifact_id": artifact_id,
            "filename": "report.pdf",
            "owner_scope": {"project_id": "project-1"},
            "run_id": "job-1",
        },
        repository_holder=repositories,
    )

    service.complete_success(
        job_id="job-1",
        workflow_outputs={
            "outputs": {},
            "artifacts": {"report": {"artifact_id": "artifact-1", "filename": "report.pdf"}},
        },
    )

    assert repositories[0].value is not None
    snapshot = WorkflowRunStateV1.model_validate(repositories[0].value["snapshot"])
    assert snapshot.result is not None
    assert snapshot.result.artifacts == {
        "report": {"artifact_id": "artifact-1", "filename": "report.pdf"}
    }


def test_success_authenticates_canonical_string_artifact_handle() -> None:
    events: list[str] = []
    repositories: list[_Repository] = []
    service = _service(
        events,
        resolve_artifact=lambda artifact_id: (
            {
                "artifact_id": artifact_id,
                "filename": "runtime-report.json",
                "owner_scope": {"project_id": "project-1", "job_id": "job-1"},
                "run_id": "job-1",
            }
            if artifact_id == "art_runtime_report"
            else None
        ),
        repository_holder=repositories,
    )

    result = service.complete_success(
        job_id="job-1",
        workflow_outputs={
            "outputs": {},
            "artifacts": {"report": "artifact:art_runtime_report"},
        },
    )

    assert result.status == "completed"
    assert repositories[0].value is not None
    snapshot = WorkflowRunStateV1.model_validate(repositories[0].value["snapshot"])
    assert snapshot.result is not None
    assert snapshot.result.artifacts == {
        "report": {
            "artifact_id": "art_runtime_report",
            "filename": "runtime-report.json",
        }
    }


def test_artifact_resolver_outage_rolls_back_without_completed_status() -> None:
    events: list[str] = []

    def _unavailable(_artifact_id: str):
        raise RuntimeError("storage unavailable")

    _raises_callable_170_1 = _service(events, resolve_artifact=_unavailable).complete_success
    with pytest.raises(RuntimeError, match="Artifact resolver is unavailable"):
        _raises_callable_170_1(
            job_id="job-1",
            workflow_outputs={
                "outputs": {},
                "artifacts": {"report": {"artifact_id": "artifact-1"}},
            },
        )

    assert "status:completed" not in events
    assert events[-1] == "rollback"


def test_missing_required_artifact_becomes_durable_failure() -> None:
    events: list[str] = []

    result = _service(events, resolve_artifact=lambda _artifact_id: None).complete_success(
        job_id="job-1",
        workflow_outputs={
            "outputs": {},
            "artifacts": {"report": {"artifact_id": "artifact-missing"}},
        },
    )

    assert result.status == "failed"
    assert result.error_code == "artifact_integrity_error"
    assert "status:completed" not in events


def test_unserializable_outputs_become_durable_failure_not_empty_success() -> None:
    events: list[str] = []

    result = _service(events).complete_success(
        job_id="job-1",
        workflow_outputs={"outputs": {"bad": object()}, "artifacts": {}},
    )

    assert result.status == "failed"
    assert result.error_code == "output_persistence_failed"
    assert "status:completed" not in events
    assert events[-2:] == ["status:failed", "commit"]


def test_failure_snapshot_marker_precede_observable_terminal_status() -> None:
    events: list[str] = []

    result = _service(events).complete_failure(
        job_id="job-1",
        status="cancelled",
        error_message="Cancelled by operator",
    )

    assert result.status == "cancelled"
    assert events.index("marker") < events.index("status:cancelled")


def test_failure_persists_queue_metadata_in_same_terminal_transaction() -> None:
    events: list[str] = []
    service = _service(events)

    result = service.complete_failure(
        job_id="job-1",
        status="failed",
        error_message="Multipart payload persistence did not complete",
        error_code="idempotency_payload_incomplete",
        category="submission",
        meta_patch={
            "queue_delivery": {"state": "dead_lettered", "dispatch_state": "not_started"},
            "runtime_journal": [{"event": "delivery_failed"}],
        },
    )

    assert result.status == "failed"
    assert service.logger.job["meta"]["queue_delivery"]["state"] == "dead_lettered"
    assert events.index("outputs") < events.index("snapshot")
    assert events[-1] == "commit"


def test_failure_persists_structured_error_before_public_snapshot_and_status() -> None:
    events: list[str] = []
    repositories: list[_Repository] = []
    service = _service(events, repository_holder=repositories)

    service.complete_failure(
        job_id="job-1",
        status="failed",
        error_message="Agent output contract failed",
        error_code="agent_output_invalid",
        category="output_contract",
        retryable=False,
        group_id="delivery",
        step_id="write-report",
        step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
        occurrence=2,
        selector="state.seed_value",
        diagnostic_kind="transform",
        diagnostic_path="query_definition_jsonb.sql",
        correlation_id="corr-terminal-1",
    )

    assert service.logger.job_errors == [
        {
            "job_id": "job-1",
            "source": "system",
            "category": "contract",
            "retryable": False,
            "error_message": "Agent output contract failed",
            "context_jsonb": {
                "error_code": "agent_output_invalid",
                "occurrence": 2,
                "selector": "state.seed_value",
                "diagnostic_kind": "transform",
                "path": "query_definition_jsonb.sql",
                "correlation_id": "corr-terminal-1",
            },
            "group_id": "delivery",
            "step_id": "write-report",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
        }
    ]
    assert events.index("job_error") < events.index("snapshot")
    assert events.index("job_error") < events.index("status:failed")
    assert service.logger.actions == [
        {
            "project_id": "project-1",
            "task_id": "",
            "task_summary": "",
            "agent": "workflow_runtime",
            "action_type": "workflow_terminal",
            "status": "failed",
            "error": "Agent output contract failed",
            "meta": {
                "error_code": "agent_output_invalid",
                "category": "output_contract",
                "retryable": False,
                "step_id": "write-report",
                "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                "group_id": "delivery",
                "occurrence": 2,
                "selector": "state.seed_value",
                "diagnostic_kind": "transform",
                "path": "query_definition_jsonb.sql",
                "correlation_id": "corr-terminal-1",
            },
            "job_id": "job-1",
        }
    ]
    assert events.index("terminal_log") < events.index("snapshot")
    snapshot = WorkflowRunStateV1.model_validate(repositories[0].value["snapshot"])
    assert snapshot.error is not None
    assert snapshot.error.step_id == "write-report"
    assert snapshot.error.group_id == "delivery"
    assert snapshot.error.step_execution_id == "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"
    assert snapshot.error.occurrence == 2
    assert [
        (step.step_id, step.group_id, step.occurrence, step.status) for step in snapshot.steps
    ] == [("write-report", "delivery", 2, "failed")]


def test_failure_replaces_latest_recorded_step_when_occurrence_is_not_reported() -> None:
    events: list[str] = []
    repositories: list[_Repository] = []
    service = _service(events, repository_holder=repositories)
    PublicProgressService(repositories[0]).record_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="in_progress",
        occurred_at=NOW,
        started_at=NOW,
        step={
            "step_id": "validate-inputs",
            "group_id": "implementation",
            "occurrence": 4,
            "display_name": "Validate inputs",
            "step_kind": "agent",
            "status": "completed",
            "started_at": NOW,
            "finished_at": NOW,
        },
    )

    service.complete_failure(
        job_id="job-1",
        status="failed",
        error_message="Required input repository is missing",
        error_code="skill_input_invalid",
        group_id="implementation",
        step_id="validate-inputs",
    )

    snapshot = WorkflowRunStateV1.model_validate(repositories[0].value["snapshot"])
    assert snapshot.error is not None
    assert snapshot.error.occurrence == 4
    assert len(snapshot.steps) == 1
    failed_step = snapshot.steps[0]
    assert failed_step.status == "failed"
    assert failed_step.occurrence == 4
    assert failed_step.display_name == "Validate inputs"
    assert failed_step.step_kind == "agent"
    assert failed_step.started_at == NOW


def test_terminal_persistence_rejects_already_terminal_job_under_row_lock() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {"status": "cancelled", "worker_id": "worker-1"}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(TerminalPersistenceConflict, match="already terminal"):
        TerminalPersistenceService._assert_terminal_write_allowed(
            conn, job_id="job-1", expected_worker_id="worker-1"
        )


def test_late_user_cancel_cannot_overwrite_preceding_business_failure() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {"status": "failed", "worker_id": "worker-1"}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(TerminalPersistenceConflict, match="already terminal"):
        TerminalPersistenceService._assert_terminal_write_allowed(
            conn, job_id="job-1", expected_worker_id="worker-1"
        )

    cursor.execute.assert_called_once_with(
        "SELECT status, worker_id FROM jobs WHERE id = %s FOR UPDATE",
        ("job-1",),
    )


def test_terminal_persistence_rejects_changed_worker_owner_under_row_lock() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {"status": "in_progress", "worker_id": "worker-2"}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(TerminalPersistenceConflict, match="ownership changed"):
        TerminalPersistenceService._assert_terminal_write_allowed(
            conn, job_id="job-1", expected_worker_id="worker-1"
        )


def test_terminal_persistence_rejects_blank_worker_when_owner_is_expected() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {"status": "in_progress", "worker_id": None}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(TerminalPersistenceConflict, match="ownership changed"):
        TerminalPersistenceService._assert_terminal_write_allowed(
            conn, job_id="job-1", expected_worker_id="worker-1"
        )


def test_terminal_persistence_rejects_worker_claim_during_pending_outbox_recovery() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = {"status": "in_progress", "worker_id": "worker-2"}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(TerminalPersistenceConflict, match="no longer pending and unowned"):
        TerminalPersistenceService._assert_terminal_write_allowed(
            conn,
            job_id="job-1",
            expected_worker_id=None,
            require_pending_unowned=True,
        )

    cursor.execute.assert_called_once_with(
        "SELECT status, worker_id FROM jobs WHERE id = %s FOR UPDATE",
        ("job-1",),
    )
