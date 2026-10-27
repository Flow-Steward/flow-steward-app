from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from core.application.jobs.public_workflow_snapshot_query import PublicWorkflowSnapshotQuery
from core.application.jobs.workflow_run_models import PublicWorkflowResultV1
from core.application.jobs.workflow_run_projection import (
    ArtifactResolverUnavailable,
    MultipleApprovalGatesUnsupported,
    WorkflowRunProjectionService,
    approval_notification_payload_from_state,
    ensure_single_approval_interrupt,
)

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize("authored", [True, False])
def test_approval_notification_and_progress_preserve_decision_identity(authored):
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.management.approval_decision_service import ApprovalDecisionService
    from core.application.management.command_models_api_inputs import ApprovalDecisionInput
    from core.application.management.errors import ServiceConflictError
    from core.application.management.query_services_approval import ApprovalQueryService
    from core.application.orchestration.approval_payload import approval_id_for_execution
    from tests.unit.test_approval_gate import _state_gate_command

    semantic_id = (
        approval_id_for_execution("11111111-1111-4111-8111-111111111111") if authored else None
    )
    original = {"message": "Review publication", "step_id": "approval", "group_id": "release"}
    if semantic_id is not None:
        original["approval_id"] = semantic_id
    before = dict(original)
    state = SimpleNamespace(
        values={"external_effect_resolution_ref": {"private": "never-public"}},
        tasks=[
            SimpleNamespace(interrupts=[SimpleNamespace(id="graph-occurrence", value=original)])
        ],
    )

    payload = approval_notification_payload_from_state(state)
    assert payload["group_id"] == "release"
    assert payload["group_label"] == "Release"
    assert payload["message"] == payload["summary"] == "Review publication"
    assert payload["step_id"] == "approval"

    assert original == before
    authority_state = SimpleNamespace(
        values=dict(state.values),
        tasks=[
            SimpleNamespace(interrupts=[SimpleNamespace(id="graph-occurrence", value=dict(before))])
        ],
    )
    row = {
        "id": "42",
        "thread_id": "thread-1",
        "project_id": "7",
        "status": "awaiting_approval",
        "meta": {},
    }
    logger = SimpleNamespace(
        get_job=lambda _: row, get_project=lambda _: {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
    )
    current_gate = ApprovalQueryService(
        get_logger=lambda: logger,
        get_thread_state_snapshot=lambda _: authority_state,
        approval_fallback_payload=lambda _: {"message": "fallback"},
    ).get_current_gate("42")
    command = _state_gate_command(authority_state)
    actual_gate = command._current_interrupt_payload("thread-1", attempts=1)
    assert ApprovalDecisionService.current_approval_id(actual_gate) == current_gate.approval_id
    assert payload["approval_id"] == current_gate.approval_id
    assert (
        command._validate_current_gate(
            row=row,
            payload=actual_gate,
            decision=ApprovalDecisionInput(approved=True, approval_id=payload["approval_id"]),
            token_principal=False,
        )
        == current_gate.approval_id
    )
    stale_ids = [approval_id_for_execution("22222222-2222-4222-8222-222222222222")]
    if authored:
        stale_ids.append("graph-occurrence")
    for stale_id in stale_ids:
        stale_decision = ApprovalDecisionInput(approved=True, approval_id=stale_id)
        with pytest.raises(ServiceConflictError, match="stale"):
            command._validate_current_gate(
                row=row,
                payload=actual_gate,
                decision=stale_decision,
                token_principal=False,
            )

    projected = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="awaiting_approval"), approval_gate=payload
    )
    assert projected.approval.approval_id == (semantic_id or "graph-occurrence")
    assert original == before
    assert "external_effect_resolution_ref" not in payload
    assert "never-public" not in str(payload)
    assert "never-public" not in projected.model_dump_json()


def test_approval_notification_payload_carries_the_recorded_authored_wait() -> None:
    state = SimpleNamespace(
        values={
            "pending_wait": {
                "wait_id": "wait-1",
                "kind": "approval",
                "approval_id": "exec-approval",
                "step_id": "approval",
                "step_execution_id": "exec-1",
                "group_id": None,
            }
        },
        tasks=[
            SimpleNamespace(
                interrupts=[
                    SimpleNamespace(
                        id="gate-2",
                        value={
                            "approval_id": "exec-approval",
                            "gate_kind": "approval",
                            "message": "Review",
                            "step_id": "approval",
                        },
                    )
                ]
            )
        ],
    )

    payload = approval_notification_payload_from_state(state)

    assert payload["approval_id"] == "exec-approval"
    assert payload["wait_id"] == "wait-1"
    assert payload["reason"] == "authored"
    assert "group_label" not in payload


class _Logger:
    def __init__(self, *, has_snapshot: bool = True) -> None:
        self.current = None
        self.transitions: list[dict[str, Any]] = []
        if has_snapshot:
            from core.application.jobs.public_progress_service import PublicProgressService
            from tests.unit.test_public_progress_service import MemoryProgressRepository

            self.repository = MemoryProgressRepository()
            self.current = PublicProgressService(self.repository).record_transition(
                job_id="job-1",
                project_id="project-1",
                workflow_id="workflow-1",
                job_status="in_progress",
                occurred_at=NOW,
                queued_at=NOW,
                started_at=NOW,
            )

    def get_public_progress_snapshot(self, job_id: str):
        if self.current is None:
            raise LookupError(job_id)
        return self.current

    def record_public_progress_transition(self, **transition: Any):
        from core.application.jobs.public_progress_service import PublicProgressService
        from tests.unit.test_public_progress_service import MemoryProgressRepository

        if not hasattr(self, "repository"):
            self.repository = MemoryProgressRepository()
        self.transitions.append(transition)
        self.current = PublicProgressService(self.repository).record_transition(**transition)
        return self.current


def _job(**overrides: Any) -> dict[str, Any]:
    job = {
        "id": "job-1",
        "project_id": "project-1",
        "thread_id": "thread-1",
        "selected_workflow": "workflow-1",
        "status": "in_progress",
        "queued_at": NOW,
        "started_at": NOW,
        "finished_at": None,
        "meta": {},
        "last_error": "",
        "error": "",
    }
    job.update(overrides)
    return job


def test_missing_public_snapshot_fails_closed_without_read_repair() -> None:
    logger = _Logger(has_snapshot=False)
    service = WorkflowRunProjectionService(logger, now=lambda: NOW)

    _raises_input_107_1 = _job()
    with pytest.raises(LookupError, match="snapshot is missing"):
        service.get(job=_raises_input_107_1)

    assert logger.transitions == []


def test_snapshot_query_normalizes_status_alias_without_extra_revision() -> None:
    logger = _Logger()
    state = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _thread_id: SimpleNamespace(tasks=[]),
        to_json_obj=lambda value: value,
    ).get(_job(status="running"))

    assert state.status == "in_progress"
    assert logger.transitions == []


def test_snapshot_query_never_regresses_terminal_snapshot_from_stale_job() -> None:
    logger = _Logger()
    completed = logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="completed",
        occurred_at=NOW,
        queued_at=NOW,
        started_at=NOW,
        finished_at=NOW,
        result=PublicWorkflowResultV1(outputs={}, artifacts={}),
    )
    logger.transitions.clear()

    state = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _thread_id: SimpleNamespace(tasks=[]),
        to_json_obj=lambda value: value,
    ).get(_job(status="in_progress"))

    assert state.status == "completed"
    assert state.terminal is True
    assert logger.current.revision == completed.revision
    assert logger.transitions == []


def test_snapshot_query_enriches_matching_terminal_error_from_job_error() -> None:
    logger = _Logger()
    failed = logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="failed",
        occurred_at=NOW,
        queued_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )
    logger.transitions.clear()

    state = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _thread_id: SimpleNamespace(tasks=[]),
        to_json_obj=lambda value: value,
    ).get(
        _job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "step_id": "step-review",
                "group_id": "phase-2",
                "retryable": True,
                "context_jsonb": {
                    "error_code": "provider_timeout",
                    "occurrence": 3,
                },
            }
        ],
    )

    assert state.status == "failed"
    assert logger.current.revision == failed.revision + 1
    assert state.error is not None
    assert state.error.code == "provider_timeout"
    assert state.error.step_id == "step-review"
    assert state.error.group_id == "phase-2"
    assert state.error.occurrence == 3
    assert state.error.retryable is True


def test_completed_projection_exposes_only_validated_workflow_outputs() -> None:
    logger = _Logger()
    artifacts = {
        "artifact-1": {
            "artifact_id": "artifact-1",
            "filename": "canonical-report.pdf",
            "content_type": "application/pdf",
            "size_bytes": 42,
            "sha256": "canonical-sha256",
            "retention_class": "workflow-output",
            "owner_scope": {"project_id": "project-1", "job_id": "job-1"},
            "run_id": "runtime-run-1",
        },
        "artifact-other-project": {
            "artifact_id": "artifact-other-project",
            "owner_scope": {"project_id": "project-2"},
            "run_id": "job-1",
        },
    }
    state = WorkflowRunProjectionService(
        logger,
        now=lambda: NOW,
        resolve_artifact=lambda artifact_id: artifacts.get(artifact_id),
    ).get(
        job=_job(
            status="completed",
            finished_at=NOW,
            final_report="private report",
            meta={
                "workflow_outputs": {
                    "outputs": {"ticket_id": "T-1"},
                    "artifacts": {
                        "report": {
                            "artifact_id": "artifact-1",
                            "filename": "spoofed.exe",
                            "content_type": "application/x-msdownload",
                            "size_bytes": 999999,
                            "sha256": "spoofed-sha256",
                            "object_key": "/private/storage/report.pdf",
                            "credentials": "secret",
                        },
                        "unsafe_uri": "s3://bucket/private?token=secret",
                        "missing_handle": {"filename": "untrusted.txt"},
                        "unsafe_handle": {
                            "artifact_id": "s3://bucket/private?token=secret",
                            "filename": "private.txt",
                        },
                        "credential_like_handle": {
                            "artifact_id": "AKIAIOSFODNN7EXAMPLE",
                            "filename": "secret.txt",
                        },
                        "wrong_project": {
                            "artifact_id": "artifact-other-project",
                            "filename": "other.txt",
                        },
                    },
                }
            },
        )
    )

    assert state.result is not None
    assert state.result.outputs == {"ticket_id": "T-1"}
    assert state.result.artifacts == {
        "report": {
            "artifact_id": "artifact-1",
            "filename": "canonical-report.pdf",
            "content_type": "application/pdf",
            "size_bytes": 42,
            "sha256": "canonical-sha256",
            "retention_class": "workflow-output",
        }
    }
    assert "private report" not in state.model_dump_json()
    assert "secret" not in state.model_dump_json()
    assert "s3://" not in state.model_dump_json()
    assert "AKIAIOSFODNN7EXAMPLE" not in state.model_dump_json()


def test_completed_projection_authenticates_canonical_string_artifact_handle() -> None:
    logger = _Logger()
    resolved_handles: list[str] = []

    def _resolve(artifact_id: str) -> dict[str, Any] | None:
        resolved_handles.append(artifact_id)
        if artifact_id != "art_runtime_report":
            return None
        return {
            "artifact_id": artifact_id,
            "filename": "runtime-report.json",
            "content_type": "application/json",
            "size_bytes": 64,
            "owner_scope": {"project_id": "project-1", "job_id": "job-1"},
            "run_id": "job-1",
        }

    state = WorkflowRunProjectionService(
        logger,
        now=lambda: NOW,
        resolve_artifact=_resolve,
    ).get(
        job=_job(
            status="completed",
            finished_at=NOW,
            meta={
                "workflow_outputs": {
                    "outputs": {},
                    "artifacts": {"report": "artifact:art_runtime_report"},
                }
            },
        )
    )

    assert resolved_handles == ["art_runtime_report"]
    assert state.result is not None
    assert state.result.artifacts == {
        "report": {
            "artifact_id": "art_runtime_report",
            "filename": "runtime-report.json",
            "content_type": "application/json",
            "size_bytes": 64,
        }
    }


def test_completed_projection_accepts_recorded_child_producer_after_parent_adoption() -> None:
    logger = _Logger()

    def _resolve(artifact_id: str) -> dict[str, Any] | None:
        if artifact_id != "art_child_report":
            return None
        return {
            "artifact_id": artifact_id,
            "filename": "child-report.json",
            "content_type": "application/json",
            "size_bytes": 64,
            "owner_scope": {"project_id": "project-1", "job_id": "parent-1"},
            "producer_job_id": "job-1",
            "run_id": "parent-1",
        }

    state = WorkflowRunProjectionService(
        logger,
        now=lambda: NOW,
        resolve_artifact=_resolve,
    ).get(
        job=_job(
            status="completed",
            finished_at=NOW,
            meta={
                "workflow_outputs": {
                    "outputs": {},
                    "artifacts": {"report": "artifact:art_child_report"},
                }
            },
        )
    )

    assert state.result is not None
    assert state.result.artifacts["report"]["artifact_id"] == "art_child_report"


def test_completed_projection_fails_when_repository_is_unavailable() -> None:
    logger = _Logger()
    _raises_callable_276_1 = WorkflowRunProjectionService(logger, now=lambda: NOW).get
    _raises_input_276_2 = _job(
        status="completed",
        finished_at=NOW,
        meta={
            "workflow_outputs": {
                "outputs": {},
                "artifacts": {"report": {"artifact_id": "artifact-1", "filename": "report.pdf"}},
            }
        },
    )
    with pytest.raises(ArtifactResolverUnavailable):
        _raises_callable_276_1(job=_raises_input_276_2)


def test_artifact_with_same_caller_task_id_but_different_job_is_rejected() -> None:
    logger = _Logger()
    state = WorkflowRunProjectionService(
        logger,
        now=lambda: NOW,
        resolve_artifact=lambda artifact_id: {
            "artifact_id": artifact_id,
            "filename": "foreign.pdf",
            "owner_scope": {"project_id": "project-1"},
            "run_id": "caller-task-id",
        },
    ).get(
        job=_job(
            status="completed",
            task_id="caller-task-id",
            finished_at=NOW,
            meta={
                "workflow_outputs": {
                    "outputs": {},
                    "artifacts": {"report": {"artifact_id": "artifact-foreign"}},
                }
            },
        )
    )

    assert state.result is not None
    assert state.result.artifacts == {}


def test_failure_projection_redacts_unrestricted_structured_error_context() -> None:
    logger = _Logger()
    state = WorkflowRunProjectionService(logger, now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "error_code": "provider_timeout",
                "error_message": "Provider timed out",
                "group_id": "build",
                "step_id": "call-provider",
                "category": "provider",
                "retryable": True,
                "http_status": 504,
                "request_id": "req-1",
                "context_jsonb": {"token": "secret", "raw_body": "private"},
                "traceback": "/srv/app/file.py",
            }
        ],
    )

    assert state.error is not None
    assert state.error.code == "provider_timeout"
    assert state.error.details == {"http_status": 504, "request_id": "req-1"}
    serialized = state.model_dump_json()
    assert "secret" not in serialized
    assert "traceback" not in serialized


def test_failure_projection_never_exposes_raw_exception_message() -> None:
    raw_detail = "token=secret-123 at /srv/private/app.py"
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW, last_error=raw_detail),
        job_errors=[
            {
                "error_code": "worker_exception",
                "error_message": raw_detail,
                "category": "execution",
            }
        ],
    )

    assert state.error is not None
    assert state.error.message == "Workflow execution did not complete successfully"
    assert raw_detail not in state.model_dump_json()
    assert "secret-123" not in state.model_dump_json()
    assert "/srv/private/app.py" not in state.model_dump_json()


def test_failure_projection_exposes_safe_transform_action_without_input_value() -> None:
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "error_code": "input_item_not_object",
                "error_message": "must not expose input=secret-value",
                "category": "contract",
                "group_id": "compose_email",
                "step_id": "t_email",
                "context_jsonb": {
                    "diagnostic_kind": "transform",
                    "selector": "state.seed_value",
                },
            }
        ],
    )

    assert state.error is not None
    assert state.error.code == "input_item_not_object"
    assert state.error.group_id == "compose_email"
    assert state.error.step_id == "t_email"
    assert state.error.message == ("Transform input at state.seed_value must resolve to an object.")
    assert state.error.details == {"selector": "state.seed_value"}
    assert "secret-value" not in state.model_dump_json()


def test_failure_projection_exposes_safe_tabular_state_action() -> None:
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "error_code": "state_reference_missing",
                "category": "contract",
                "group_id": "read",
                "step_id": "read-rows",
                "context_jsonb": {
                    "diagnostic_kind": "tabular_feed",
                    "selector": "state.created_dataset.dataset_handle",
                },
            }
        ],
    )

    assert state.error is not None
    assert state.error.message == (
        "Tabular input at state.created_dataset.dataset_handle references missing workflow state."
    )
    assert state.error.details == {"selector": "state.created_dataset.dataset_handle"}


def test_failure_projection_exposes_safe_dataset_query_diagnostic() -> None:
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "error_code": "query_sql_missing",
                "error_message": "SQL is required",
                "category": "contract",
                "group_id": "read",
                "step_id": "read-orders",
                "context_jsonb": {
                    "diagnostic_kind": "dataset_query",
                    "path": "query_definition_jsonb.sql",
                    "correlation_id": "corr-dataset-1",
                },
            }
        ],
    )

    assert state.error is not None
    assert state.error.code == "query_sql_missing"
    assert state.error.message == "SQL is required"
    assert state.error.details == {
        "path": "query_definition_jsonb.sql",
        "correlation_id": "corr-dataset-1",
    }


def test_failure_projection_redacts_secret_bearing_dataset_provider_error() -> None:
    raw_error = "postgresql://user:password@db/orders token=raw-token"  # pragma: allowlist secret - redaction fixture
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "error_code": "provider_rejected",
                "error_message": raw_error,
                "category": "integration",
                "context_jsonb": {
                    "diagnostic_kind": "dataset_query",
                    "path": "secret_refs.password",
                    "correlation_id": "corr-provider-1",
                },
            }
        ],
    )

    assert state.error is not None
    assert state.error.message == "Dataset query failed"
    assert state.error.details == {"correlation_id": "corr-provider-1"}
    assert raw_error not in state.model_dump_json()
    assert "raw-token" not in state.model_dump_json()


def test_failure_projection_sanitizes_code_and_allowlisted_detail_values() -> None:
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "error_code": "token=provider-secret",
                "request_id": "Bearer credential-value",
                "provider_error_code": "s3://private-bucket?token=secret",
                "provider_code": "sk_live_supersecret",
                "message_id": "message-42",
            }
        ],
    )

    assert state.error is not None
    assert state.error.code == "job_failed"
    assert state.error.details == {
        "message_id": "message-42",
    }
    serialized = state.model_dump_json()
    assert "provider-secret" not in serialized
    assert "credential-value" not in serialized
    assert "private-bucket" not in serialized
    assert "sk_live_supersecret" not in serialized


def test_failure_projection_rejects_identifier_compatible_provider_token() -> None:
    provider_token = "glpat-ABCDEFGHIJKLMNOPQRSTuv"  # pragma: allowlist secret
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[
            {
                "error_code": provider_token,
                "provider_error_code": provider_token,
                "request_id": "message-safe",
            }
        ],
    )

    assert state.error is not None
    assert state.error.code == "job_failed"
    assert state.error.details == {"request_id": "message-safe"}
    assert provider_token not in state.model_dump_json()


def test_failure_projection_checks_full_identifier_before_truncating() -> None:
    long_jwt = "eyJ" + ("a" * 55) + ".eyJ" + ("b" * 55) + "." + ("c" * 30)
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="failed", finished_at=NOW),
        job_errors=[{"error_code": long_jwt}],
    )

    assert state.error is not None
    assert state.error.code == "job_failed"
    assert long_jwt[:128] not in state.model_dump_json()


@pytest.mark.parametrize(
    "credential",
    [
        "client_secret=alpha beta; retry",
        "refresh_token=alpha beta; retry",
        "pwd=alpha beta; retry",
        "password=alpha beta; retry",
        r'password="alpha \"beta\" value"; retry',  # pragma: allowlist secret
    ],
)
def test_workflow_projection_redacts_complete_approval_credentials(credential: str) -> None:
    state = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="awaiting_approval"),
        approval_gate={
            "approval_id": "gate-1",
            "message": f"Provider {credential}",
            "content": {"summary": credential},
        },
    )

    assert state.approval is not None
    rendered = state.model_dump_json()
    assert all(part not in rendered for part in ("alpha", "beta", "value"))
    assert "REDACTED" in rendered


def test_safe_approval_is_bounded_and_identity_is_reused_from_interrupt() -> None:
    logger = _Logger()
    state = WorkflowRunProjectionService(logger, now=lambda: NOW).get(
        job=_job(status="awaiting_approval"),
        approval_gate={
            "interrupt_id": "interrupt-1",
            "message": "Approve deployment",
            "content": {"summary": "Ready", "prompt": "private", "secret": "no"},
            "allowed_actions": ["approve", "reject"],
            "step_id": "approval",
            "group_id": "release",
        },
    )

    assert state.approval is not None
    assert state.approval.approval_id == "interrupt-1"
    assert state.approval.content == {"summary": "Ready"}
    assert state.approval.allowed_actions == ["approve", "reject"]


def test_safe_approval_without_interrupt_id_has_stable_identity() -> None:
    gate = {"message": "Approve deployment", "step_id": "approval", "group_id": "release"}

    first = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="awaiting_approval"), approval_gate=gate
    )
    second = WorkflowRunProjectionService(_Logger(), now=lambda: NOW).get(
        job=_job(status="awaiting_approval"), approval_gate=gate
    )

    assert first.approval is not None
    assert second.approval is not None
    assert first.approval.approval_id == second.approval.approval_id


def test_multiple_interrupts_fail_closed() -> None:
    state = SimpleNamespace(
        tasks=[
            SimpleNamespace(interrupts=[SimpleNamespace(value={"id": "a"})]),
            SimpleNamespace(interrupts=[SimpleNamespace(value={"id": "b"})]),
        ]
    )

    with pytest.raises(MultipleApprovalGatesUnsupported) as exc_info:
        ensure_single_approval_interrupt(state)

    assert exc_info.value.code == "multiple_approval_gates_unsupported"


def test_query_reconciles_only_committed_trace_with_this_jobs_pinned_archive():
    from unittest.mock import Mock

    from core.application.jobs.public_progress_service import PublicProgressService
    from tests.unit.test_workflow_run_trace import one_trace, trace_state

    trace = one_trace()
    state, definition = trace_state()
    values = {**state, "workflow_run_trace": trace}
    archive = {
        "id": "archive-1",
        "workflow_id": "workflow-1",
        "revision": 1,
        "digest": trace["digest"],
        "definition_jsonb": definition,
    }
    logger = _Logger()
    logger.reconcile_public_run_trace = Mock(
        side_effect=lambda **kwargs: PublicProgressService(
            logger.repository
        ).record_committed_trace(**kwargs)
    )
    committed = Mock(return_value=values)
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _id: SimpleNamespace(
            values={"workflow_run_trace": {"forged": True}}, tasks=[]
        ),
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=committed,
        load_definition_snapshot=lambda _job: archive,
    )
    result = query.get(_job())
    assert result.run_trace.model_dump(mode="json") == trace
    committed.assert_called_once_with("thread-1")
    assert logger.reconcile_public_run_trace.call_count == 1
    for change in (
        {"job_id": "another-job"},
        {"workflow_definition_ref": {**state["workflow_definition_ref"], "digest": "foreign"}},
    ):
        committed.return_value = {**values, **change}
        logger.reconcile_public_run_trace.reset_mock()
        query.get(_job())
        logger.reconcile_public_run_trace.assert_not_called()


def test_terminal_job_needs_its_own_committed_terminal_trace_and_stale_reads_cannot_clear_it():
    from core.application.jobs.public_progress_service import PublicProgressService
    from tests.unit.test_workflow_run_trace import one_trace, trace_state

    trace = one_trace()
    state, definition = trace_state()
    values = {**state, "status": "in_progress", "workflow_run_trace": trace}
    archive = {
        "id": "archive-1",
        "workflow_id": "workflow-1",
        "revision": 1,
        "digest": trace["digest"],
        "definition_jsonb": definition,
    }
    logger = _Logger()
    logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="failed",
        occurred_at=NOW,
        finished_at=NOW,
        error={
            "code": "workflow_stop_fail",
            "message": "Authored stop",
            "category": "execution",
            "retryable": False,
        },
    )

    def reconcile(**evidence):
        logger.current = PublicProgressService(logger.repository).record_committed_trace(**evidence)
        return logger.current

    logger.reconcile_public_run_trace = reconcile
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _id: None,
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=lambda _id: values,
        load_definition_snapshot=lambda _id: archive,
    )
    observed = query.get(_job(status="failed", finished_at=NOW))
    assert observed.terminal is True
    assert observed.run_trace.terminal is False
    values["status"] = "failed"
    final = query.get(_job(status="failed", finished_at=NOW))
    assert final.run_trace.terminal is True
    assert final.run_trace.last_sequence == observed.run_trace.last_sequence
    assert final.error == observed.error
    assert final.finished_at == observed.finished_at
    values["status"] = "in_progress"
    stale = query.get(_job(status="failed", finished_at=NOW))
    assert stale.run_trace == final.run_trace
    assert stale.error == final.error


def test_foreign_stored_snapshot_is_refused_before_any_trace_checkpoint_or_archive_read():
    from unittest.mock import Mock

    from core.application.management import ServiceError

    logger = _Logger()
    logger.current = logger.current.model_copy(
        update={"snapshot": logger.current.snapshot.model_copy(update={"job_id": "another-job"})}
    )
    committed = Mock()
    archive = Mock()
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _id: None,
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=committed,
        load_definition_snapshot=archive,
    )
    job = _job()
    with pytest.raises(ServiceError, match="Public workflow state is unavailable"):
        query.get(job)
    committed.assert_not_called()
    archive.assert_not_called()


def test_committed_stop_retains_unfinished_container_without_current_or_finished_claim(
    monkeypatch, request
):
    import core.application.orchestration.agents.common as agents_common
    from core.application.jobs.public_progress_service import PublicProgressService
    from core.application.orchestration.run_definition import clear_run_definition_cache
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests._workflow_definitions import edge, node
    from tests.unit.test_step_hub import _loop_definition, _result, _run, _step

    clear_run_definition_cache()
    request.addfinalizer(clear_run_definition_cache)
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    definition = _loop_definition()
    definition["steps"][1]["step_kind"] = "stop_fail"
    definition["execution_graph"]["edges"] = [
        edge("a", node("g", "for_each")),
        edge("g", node("done"), source_kind="for_each"),
    ]
    initial = _run(
        definition, job_id="job-1", project_id="project-1", workflow_id="workflow-1", items=["one"]
    )
    state = _step(initial, orchestrator_hub_node(initial))
    root_result = _result(state)
    state = _step(root_result, orchestrator_hub_node(root_result))
    frame = state["run_cursor"]["frames"][-1]
    stopped = _result(state, actor="control_flow_step", terminal="fail", reason="Authored stop")
    values = _step(stopped, orchestrator_hub_node(stopped))
    assert values["status"] == "failed"
    assert values["run_cursor"]["frames"] == state["run_cursor"]["frames"]

    # Supply the optional producer contract independently of its implementation:
    # this test owns the real committed-reader + public repository projection.
    ref = initial["workflow_definition_ref"]
    values["workflow_container_progress"] = {
        "schema_version": 1,
        "job_id": "job-1",
        "project_id": "project-1",
        "workflow_id": "workflow-1",
        **ref,
        "covered_from_start": True,
        "truncated": False,
        "last_sequence": 2,
        "checkpoint_terminal": False,
        "passes": [
            {
                "container": {"kind": "for_each", "id": "g"},
                "execution_id": frame["for_each_execution_id"],
                "parent_frames": [],
                "started_sequence": 1,
                "updated_sequence": 2,
                "outcome": None,
                "for_each": {
                    "item_index": 0,
                    "source_count": 1,
                    "selected_count": 1,
                    "processed_count": 0,
                    "applied_limit": frame["applied_limit"],
                    "truncated": False,
                    "broke_at": None,
                },
            }
        ],
    }
    archive = {
        "id": ref["snapshot_id"],
        "workflow_id": "workflow-1",
        "revision": ref["revision"],
        "digest": ref["digest"],
        "definition_jsonb": definition,
    }
    logger = _Logger()
    logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="failed",
        occurred_at=NOW,
        finished_at=NOW,
        error={
            "code": "workflow_stop_fail",
            "message": "Authored stop",
            "category": "execution",
            "retryable": False,
        },
    )
    before = logger.current.snapshot

    def reconcile(**evidence):
        logger.current = PublicProgressService(logger.repository).record_committed_trace(**evidence)
        return logger.current

    logger.reconcile_public_run_trace = reconcile
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _id: SimpleNamespace(
            values={"workflow_container_progress": {"forged": True}}, tasks=[]
        ),
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=lambda _id: values,
        load_definition_snapshot=lambda _id: archive,
    )
    observed = query.get(_job(status="failed", finished_at=NOW))
    progress = observed.model_dump(mode="json").get("container_progress")
    assert progress is not None, "The public reader must project committed container evidence"
    assert progress["checkpoint_terminal"] is True
    container = progress["passes"][0]
    assert container["execution_id"] == frame["for_each_execution_id"]
    assert container["current"] is False
    assert container["outcome"] is None
    assert container.get("finished_sequence") is None
    assert container["for_each"]["processed_count"] == 0
    assert observed.error == before.error
    assert observed.finished_at == before.finished_at
    assert observed.status == before.status == "failed"


def _container_reader_fixture(monkeypatch, request):
    from copy import deepcopy

    import core.application.orchestration.agents.common as agents_common
    from core.application.jobs.public_progress_service import PublicProgressService
    from core.application.orchestration.run_definition import clear_run_definition_cache
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests.unit.test_step_hub import _group_definition, _result, _run, _step

    clear_run_definition_cache()
    request.addfinalizer(clear_run_definition_cache)
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    definition = _group_definition()
    initial = _run(
        definition,
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        status="in_progress",
    )
    state = _step(initial, orchestrator_hub_node(initial))
    root_result = _result(state)
    values = _step(root_result, orchestrator_hub_node(root_result))
    frame = values["run_cursor"]["frames"][-1]
    ref = initial["workflow_definition_ref"]
    values["workflow_container_progress"] = {
        "schema_version": 1,
        "job_id": "job-1",
        "project_id": "project-1",
        "workflow_id": "workflow-1",
        **ref,
        "covered_from_start": True,
        "truncated": False,
        "last_sequence": 3,
        "checkpoint_terminal": False,
        "passes": [
            {
                "container": {"kind": "group", "id": "g"},
                "execution_id": frame["group_execution_id"],
                "parent_frames": [],
                "started_sequence": 1,
                "updated_sequence": 3,
                "outcome": None,
            }
        ],
    }
    holder = {
        "values": values,
        "archive": {
            "id": ref["snapshot_id"],
            "workflow_id": "workflow-1",
            "revision": ref["revision"],
            "digest": ref["digest"],
            "definition_jsonb": definition,
        },
        "pending": deepcopy(values),
    }
    logger = _Logger()

    def reconcile(**evidence):
        logger.current = PublicProgressService(logger.repository).record_committed_trace(**evidence)
        return logger.current

    logger.reconcile_public_run_trace = reconcile
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _id: SimpleNamespace(values=holder["pending"], tasks=[]),
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=lambda _id: holder["values"],
        load_definition_snapshot=lambda _id: holder["archive"],
    )
    return SimpleNamespace(query=query, logger=logger, holder=holder)


@pytest.mark.parametrize(
    "scope",
    [
        "checkpoint-job",
        "checkpoint-project",
        "channel-job",
        "channel-project",
        "channel-workflow",
        "channel-snapshot",
        "channel-revision",
        "channel-digest",
        "archive-id",
        "archive-content",
    ],
)
def test_container_reader_rejects_foreign_evidence_after_valid_commit(scope, monkeypatch, request):
    from copy import deepcopy

    fixture = _container_reader_fixture(monkeypatch, request)
    accepted = fixture.query.get(_job())
    progress = accepted.model_dump(mode="json").get("container_progress")
    assert progress is not None, "A valid committed pass must first reach the public repository"
    assert progress["passes"][0]["current"] is True
    before = deepcopy(fixture.logger.repository.value)
    markers = deepcopy(fixture.logger.repository.markers)
    values = fixture.holder["values"] = deepcopy(fixture.holder["values"])
    channel = values["workflow_container_progress"]
    channel["last_sequence"] = 4
    channel["passes"][0]["updated_sequence"] = 4
    if scope.startswith("checkpoint-"):
        values[{"checkpoint-job": "job_id", "checkpoint-project": "project_id"}[scope]] = "foreign"
    elif scope.startswith("channel-"):
        field = {
            "channel-job": "job_id",
            "channel-project": "project_id",
            "channel-workflow": "workflow_id",
            "channel-snapshot": "snapshot_id",
            "channel-revision": "revision",
            "channel-digest": "digest",
        }[scope]
        channel[field] = 2 if field == "revision" else "f" * 64 if field == "digest" else "foreign"
    elif scope == "archive-id":
        fixture.holder["archive"] = {**fixture.holder["archive"], "id": "foreign"}
    else:
        archive = fixture.holder["archive"] = deepcopy(fixture.holder["archive"])
        archive["definition_jsonb"]["steps"][1]["step_id"] = "foreign-step"

    observed = fixture.query.get(_job())
    assert observed.model_dump(mode="json").get("container_progress") == progress
    assert fixture.logger.repository.value == before
    assert fixture.logger.repository.markers == markers
    assert observed.status == accepted.status
    assert observed.error == accepted.error


@pytest.mark.parametrize("change", ["older-sequence", "same-sequence-conflict", "pending-only"])
def test_container_reader_keeps_committed_pass_against_stale_or_pending_updates(
    change, monkeypatch, request
):
    from copy import deepcopy

    fixture = _container_reader_fixture(monkeypatch, request)
    accepted = fixture.query.get(_job())
    progress = accepted.model_dump(mode="json").get("container_progress")
    assert progress is not None, "The control read must persist real committed evidence"
    assert progress["last_sequence"] == 3
    assert progress["passes"][0]["outcome"] is None
    before = deepcopy(fixture.logger.repository.value)
    markers = deepcopy(fixture.logger.repository.markers)
    evidence = deepcopy(fixture.holder["values"])
    channel = evidence["workflow_container_progress"]
    if change == "older-sequence":
        channel["last_sequence"] = 2
        channel["passes"][0]["updated_sequence"] = 2
        fixture.holder["values"] = evidence
    elif change == "same-sequence-conflict":
        channel["passes"][0]["execution_id"] = "another-pass"
        fixture.holder["values"] = evidence
    else:
        channel["last_sequence"] = 99
        channel["passes"][0].update(updated_sequence=99, finished_sequence=99, outcome="success")
        evidence["status"] = "completed"
        fixture.holder["pending"] = evidence

    observed = fixture.query.get(_job())
    assert observed.model_dump(mode="json").get("container_progress") == progress
    assert fixture.logger.repository.value == before
    assert fixture.logger.repository.markers == markers
    assert observed.status == "in_progress"
    assert observed.finished_at is None


@pytest.mark.parametrize("coverage", ["missing", "legacy", "partial", "truncated", "pending-only"])
def test_container_reader_does_not_invent_passes_from_cursor_or_incomplete_history(
    coverage, monkeypatch, request
):
    from copy import deepcopy

    fixture = _container_reader_fixture(monkeypatch, request)
    values = fixture.holder["values"] = deepcopy(fixture.holder["values"])
    assert values["run_cursor"]["frames"], "There is a real active frame to tempt inference"
    if coverage in {"missing", "pending-only"}:
        values.pop("workflow_container_progress")
    elif coverage == "legacy":
        values["workflow_container_progress"]["schema_version"] = 0
    else:
        channel = values["workflow_container_progress"]
        channel["passes"] = []
        channel["covered_from_start"] = coverage != "partial"
        channel["truncated"] = coverage == "truncated"
    if coverage != "pending-only":
        fixture.holder["pending"] = deepcopy(values)

    observed = fixture.query.get(_job())
    progress = observed.model_dump(mode="json").get("container_progress")
    if coverage in {"missing", "legacy", "pending-only"}:
        assert progress is None
    elif progress is not None:
        assert progress["passes"] == []
        assert progress["covered_from_start"] is (coverage != "partial")
        assert progress["truncated"] is (coverage == "truncated")
    assert observed.status == "in_progress"
    assert observed.finished_at is None
    assert observed.error is None


@pytest.mark.parametrize(
    "corruption",
    ["foreign-container", "foreign-parent", "foreign-lane", "changed-start", "changed-finish"],
)
def test_container_reader_rejects_archive_or_immutable_pass_conflicts(
    corruption, monkeypatch, request
):
    from copy import deepcopy

    fixture = _container_reader_fixture(monkeypatch, request)
    accepted = fixture.query.get(_job())
    assert accepted.container_progress is not None
    before = deepcopy(fixture.logger.repository.value)
    markers = deepcopy(fixture.logger.repository.markers)
    values = fixture.holder["values"] = deepcopy(fixture.holder["values"])
    evidence = values["workflow_container_progress"]
    record = evidence["passes"][0]
    evidence["last_sequence"] = record["updated_sequence"] = 4
    if corruption == "foreign-container":
        record["container"]["id"] = "other"
    elif corruption == "foreign-parent":
        record["parent_frames"] = [{"kind": "group", "id": "other", "execution_id": "other-pass"}]
    elif corruption == "foreign-lane":
        record["lanes"] = {
            "rows": [{"lane_id": "other", "status": "completed"}],
            "completed_count": 1,
            "total_count": 1,
        }
    elif corruption == "changed-start":
        record["started_sequence"] = 2
    else:
        # A retained live frame contradicts a fabricated successful finish.
        record.update(outcome="success", finished_sequence=4)
    observed = fixture.query.get(_job())
    assert observed.container_progress == accepted.container_progress
    assert fixture.logger.repository.value == before
    assert fixture.logger.repository.markers == markers


def test_container_reader_merges_against_locked_current_and_rolls_back_marker_failure(
    monkeypatch, request
):
    from copy import deepcopy

    from core.application.jobs.public_progress_service import PublicProgressService

    fixture = _container_reader_fixture(monkeypatch, request)
    repository = fixture.logger.repository
    atomic = repository.atomic_update
    injected = False

    def interleave(*, job_id, update):
        nonlocal injected
        if not injected:
            injected = True
            PublicProgressService(repository).record_transition(
                job_id="job-1",
                project_id="project-1",
                workflow_id="workflow-1",
                job_status="failed",
                occurred_at=NOW,
                finished_at=NOW,
                error={
                    "code": "specific_failure",
                    "message": "Keep this reason",
                    "category": "execution",
                    "retryable": False,
                },
            )
        return atomic(job_id=job_id, update=update)

    monkeypatch.setattr(repository, "atomic_update", interleave)
    observed = fixture.query.get(_job(status="failed", finished_at=NOW))
    assert observed.container_progress is not None
    assert observed.status == "failed"
    assert observed.finished_at == NOW
    assert observed.error.code == "specific_failure"
    before = deepcopy(repository.value)
    markers = deepcopy(repository.markers)
    duplicate = PublicProgressService(repository).record_committed_trace(
        job_id="job-1",
        trace=observed.run_trace,
        container_progress=observed.container_progress,
        occurred_at=NOW,
    )
    assert duplicate.snapshot == observed
    assert duplicate.changed is False
    assert repository.value == before
    assert repository.markers == markers
    fixture.holder["values"]["workflow_container_progress"]["last_sequence"] = 4
    fixture.holder["values"]["workflow_container_progress"]["passes"][0]["updated_sequence"] = 4
    repository.fail_marker = True
    fallback = fixture.query.get(_job(status="failed", finished_at=NOW))
    assert fallback == observed
    assert repository.value == before
    assert repository.markers == markers


def test_unavailable_container_channel_does_not_block_existing_taken_edge_projection(
    monkeypatch, request
):
    fixture = _container_reader_fixture(monkeypatch, request)
    fixture.holder["values"]["workflow_container_progress"] = {"malformed": True}
    observed = fixture.query.get(_job())
    assert observed.container_progress is None
    assert observed.run_trace is not None
    assert observed.run_trace.transitions
    assert observed.run_trace.job_id == "job-1"


@pytest.mark.parametrize("corruption", ["foreign-container", "foreign-failure-step"])
def test_typed_invalid_container_cannot_block_new_valid_committed_trace(
    corruption, monkeypatch, request
):
    from copy import deepcopy

    from core.application.jobs.workflow_run_models import PublicContainerProgressV1
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests.unit.test_step_hub import _result, _step

    fixture = _container_reader_fixture(monkeypatch, request)
    accepted = fixture.query.get(_job())
    assert accepted.run_trace is not None
    assert accepted.container_progress is not None
    before = accepted.model_dump(mode="json")
    result = _result(fixture.holder["values"])
    values = _step(result, orchestrator_hub_node(result))
    assert values["workflow_run_trace"]["last_sequence"] > accepted.run_trace.last_sequence
    assert values["workflow_run_trace"]["transitions"][-1]["source"] == {"kind": "group", "id": "g"}
    record = values["workflow_container_progress"]["passes"][0]
    if corruption == "foreign-container":
        record["container"]["id"] = "absent-container"
    else:
        record["outcome"] = "error"
        record["failure"] = {
            "code": "local_failure",
            "message": "Invalid optional source",
            "boundary": "body",
            "step_id": "absent-step",
        }
    # This must pass schema validation and exercise FrozenDefinition's RunCursorError.
    PublicContainerProgressV1.model_validate(values["workflow_container_progress"])
    fixture.holder["values"] = values
    observed = fixture.query.get(_job())
    assert observed.run_trace.last_sequence == values["workflow_run_trace"]["last_sequence"]
    assert observed.run_trace.transitions[-1].source.id == "g"
    assert observed.container_progress == accepted.container_progress
    after = observed.model_dump(mode="json")
    assert {
        key: value
        for key, value in after.items()
        if key not in {"run_trace", "revision", "updated_at"}
    } == {
        key: value
        for key, value in before.items()
        if key not in {"run_trace", "revision", "updated_at"}
    }
    persisted = deepcopy(fixture.logger.repository.value)
    markers = deepcopy(fixture.logger.repository.markers)
    assert fixture.query.get(_job()) == observed
    assert fixture.logger.repository.value == persisted
    assert fixture.logger.repository.markers == markers


def test_container_reader_refuses_stale_lane_inventory_from_same_recovery_checkpoint(
    monkeypatch, request
):
    from copy import deepcopy

    import core.application.orchestration.agents.common as agents_common
    from core.application.jobs.public_progress_service import PublicProgressService
    from core.application.orchestration.run_definition import clear_run_definition_cache
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests.unit.test_parallel_executor_node import _container_recovery_return

    clear_run_definition_cache()
    request.addfinalizer(clear_run_definition_cache)
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    definition, returned = _container_recovery_return(
        "approval", job_id="job-1", workflow_id="workflow-1"
    )
    paused = orchestrator_hub_node(returned)
    state = {**returned, **paused.update}
    resumed = orchestrator_hub_node({**state, "user_approved": True})
    resumed_values = {**state, **resumed.update}
    ref = returned["workflow_definition_ref"]
    holder = {"values": state}
    logger = _Logger()

    def reconcile(**evidence):
        recorded = PublicProgressService(logger.repository).record_committed_trace(**evidence)
        if recorded.changed:
            logger.current = recorded
        return recorded

    logger.reconcile_public_run_trace = reconcile
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _id: SimpleNamespace(values=holder["values"], tasks=[]),
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=lambda _id: holder["values"],
        load_definition_snapshot=lambda _id: {
            "id": ref["snapshot_id"],
            "workflow_id": "workflow-1",
            "revision": ref["revision"],
            "digest": ref["digest"],
            "definition_jsonb": definition,
        },
    )
    accepted = query.get(_job())
    assert accepted.container_progress is not None
    stale = deepcopy(resumed_values)
    stale["workflow_container_progress"]["passes"][0]["lanes"] = deepcopy(
        state["workflow_container_progress"]["passes"][0]["lanes"]
    )
    holder["values"] = stale
    observed = query.get(_job())
    assert observed.container_progress == accepted.container_progress
    assert {
        row.lane_id: row.status for row in observed.container_progress.passes[0].lanes.rows
    } == {"lane_a": "completed", "lane_b": "escalated"}
    holder["values"] = resumed_values
    current = query.get(_job())
    assert current.container_progress.last_sequence > accepted.container_progress.last_sequence
    assert {row.lane_id: row.status for row in current.container_progress.passes[0].lanes.rows} == {
        "lane_a": "completed",
        "lane_b": "running",
    }
    assert current.container_progress.passes[0].lanes.completed_count == 1
    assert current.status == accepted.status
    assert current.steps == accepted.steps
    assert current.error == accepted.error


@pytest.mark.parametrize(
    "corruption",
    [
        "checkpoint-job",
        "checkpoint-project",
        "checkpoint-workflow",
        "archive-ref",
        "archive-content",
        "trace-target",
    ],
)
def test_invalid_authority_still_blocks_new_trace_outside_optional_container_catch(
    corruption, monkeypatch, request
):
    from copy import deepcopy

    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests.unit.test_step_hub import _result, _step

    fixture = _container_reader_fixture(monkeypatch, request)
    accepted = fixture.query.get(_job())
    result = _result(fixture.holder["values"])
    values = _step(result, orchestrator_hub_node(result))
    assert values["workflow_run_trace"]["last_sequence"] > accepted.run_trace.last_sequence
    values["workflow_container_progress"] = {"malformed": True}
    if corruption.startswith("checkpoint-"):
        values[
            {
                "checkpoint-job": "job_id",
                "checkpoint-project": "project_id",
                "checkpoint-workflow": "workflow_id",
            }[corruption]
        ] = "foreign"
    elif corruption == "archive-ref":
        values["workflow_definition_ref"] = {
            **values["workflow_definition_ref"],
            "snapshot_id": "foreign",
        }
    elif corruption == "archive-content":
        fixture.holder["archive"] = deepcopy(fixture.holder["archive"])
        fixture.holder["archive"]["definition_jsonb"]["steps"][0]["display_name"] = (
            "Different pinned content"
        )
    else:
        values["workflow_run_trace"]["transitions"][-1]["target"] = {"kind": "step", "id": "absent"}
    fixture.holder["values"] = values
    before = deepcopy(fixture.logger.repository.value)
    markers = deepcopy(fixture.logger.repository.markers)
    assert fixture.query.get(_job()) == accepted
    assert fixture.logger.repository.value == before
    assert fixture.logger.repository.markers == markers


def _terminal_stop_reader_fixture(
    monkeypatch, request, *, reason="Intentional expected audit failure"
):
    """Real hub terminal proof + actual public gateway over the existing atomic memory store."""
    from copy import deepcopy

    import core.application.jobs.public_progress_repository as progress_repository
    import core.application.orchestration.agents.common as agents_common
    from core.application.jobs.public_secret_redaction import contains_public_secret
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.application.jobs.workflow_run_models import PublicWorkflowTraceV1
    from core.application.jobs.workflow_run_projection import build_public_error
    from core.application.orchestration.container_progress import committed_container_progress
    from core.application.orchestration.run_cursor import FrozenDefinition
    from core.application.orchestration.run_definition import clear_run_definition_cache
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.infrastructure.db.platform_logger_activity_mixin import PlatformLoggerActivityMixin
    from tests._workflow_definitions import linear_graph
    from tests.unit.test_public_progress_service import MemoryProgressRepository
    from tests.unit.test_step_hub import _definition, _result, _run, _step

    clear_run_definition_cache()
    request.addfinalizer(clear_run_definition_cache)
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)

    class MarkerRepository(MemoryProgressRepository):
        def atomic_update(self, *, job_id, update):
            stored, marker_id, changed = super().atomic_update(job_id=job_id, update=update)
            if changed:
                # Production load includes the marker committed with this same row.
                self.value["marker_id"] = marker_id
                stored["marker_id"] = marker_id
            return stored, marker_id, changed

    repository = MarkerRepository()
    monkeypatch.setattr(progress_repository, "PublicProgressRepository", lambda _conn: repository)

    class Logger(PlatformLoggerActivityMixin):
        conn = object()

        def list_job_errors(self, _job_id, *, limit):
            assert limit == 200
            return deepcopy(holder["job_errors"])

    definition = _definition(
        [
            {"step_id": "a", "step_kind": "set_variable"},
            {"step_id": "b", "step_kind": "transform"},
            {
                "step_id": "done",
                "step_kind": "stop_fail",
                "override": {"stop_fail": {"message": reason}},
            },
        ],
        linear_graph(["a", "b", "done"])["edges"],
    )
    initial = _run(definition, job_id="job-1", project_id="project-1", workflow_id="workflow-1")
    state = _step(initial, orchestrator_hub_node(initial))
    executions = []
    for actor in ("control_flow_step", "transform_step"):
        executions.append(deepcopy(state["run_cursor"]))
        accepted = _result(state, actor=actor)
        state = _step(accepted, orchestrator_hub_node(accepted))
    executions.append(deepcopy(state["run_cursor"]))
    accepted_stop = _result(state, actor="control_flow_step", terminal="fail", reason=reason)
    command = orchestrator_hub_node(accepted_stop)
    values = _step(accepted_stop, command)
    assert values["status"] == "failed"
    assert values.get("unresolved_step_failure") is None
    from core.application.jobs.execution_contracts import terminal_failure_context

    assert terminal_failure_context(values) == {}
    assert values["workflow_terminal"] == {
        "kind": "stop_fail",
        "status": "fail",
        "step_id": "done",
        "step_execution_id": executions[-1]["step_execution_id"],
    }
    assert accepted_stop["agent_messages"][-1]["status"] == "pass"
    ref = initial["workflow_definition_ref"]
    archive = {
        "id": ref["snapshot_id"],
        "workflow_id": "workflow-1",
        "revision": ref["revision"],
        "digest": ref["digest"],
        "definition_jsonb": definition,
    }
    holder = {
        "values": values,
        "archive": archive,
        "job_errors": [],
        "pending_values": {"error": "pending text must not leak"},
    }
    monkeypatch.setattr(
        WorkflowDefinitionSnapshotRepository, "load_for_job", lambda _self, _job: holder["archive"]
    )
    logger = Logger()
    for cursor, step in zip(executions, definition["steps"], strict=True):
        logger.record_public_progress_transition(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            job_status="in_progress",
            occurred_at=NOW,
            queued_at=NOW,
            started_at=NOW,
            step={
                "step_id": step["step_id"],
                "step_execution_id": cursor["step_execution_id"],
                "attempt": cursor["attempt"],
                "occurrence": 1,
                "step_kind": step["step_kind"],
                "status": "completed",
                "started_at": NOW,
                "finished_at": NOW,
            },
        )
    trace = PublicWorkflowTraceV1.model_validate(values["workflow_run_trace"]).model_copy(
        update={"terminal": True}
    )
    containers = committed_container_progress(
        values,
        FrozenDefinition(definition),
        identity={"job_id": "job-1", "project_id": "project-1", "workflow_id": "workflow-1", **ref},
        trace=trace,
    )
    logger.reconcile_public_run_trace(
        job_id="job-1", trace=trace, occurred_at=NOW, container_progress=containers
    )
    logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="failed",
        occurred_at=NOW,
        finished_at=NOW,
        error=build_public_error(_job(status="failed", finished_at=NOW), "failed", []),
    )
    committed_reads = []

    def committed(thread_id):
        committed_reads.append(thread_id)
        callback = holder.pop("before_committed_return", None)
        if callback is not None:
            callback()
        return deepcopy(holder["values"])

    query = PublicWorkflowSnapshotQuery(
        logger,
        # This is deliberately a contradictory pending graph view, never committed authority.
        get_thread_state_snapshot=lambda _id: SimpleNamespace(
            values=holder["pending_values"], tasks=[]
        ),
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=committed,
        load_definition_snapshot=lambda _job: holder["archive"],
    )
    return SimpleNamespace(
        query=query,
        logger=logger,
        repository=repository,
        holder=holder,
        job=_job(status="failed", finished_at=NOW),
        stop_execution=executions[-1]["step_execution_id"],
        before=logger.get_public_progress_snapshot("job-1"),
        committed_reads=committed_reads,
        contains_public_secret=contains_public_secret,
    )


def test_stop_failure_enriches_only_public_terminal_metadata_atomically(monkeypatch, request):
    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    marker_count = len(fixture.repository.markers)
    observed = fixture.query.get(fixture.job)

    assert observed.error is not None
    assert observed.error.code == "workflow_stop_fail"
    assert observed.error.step_id == "done"
    assert observed.error.step_execution_id == fixture.stop_execution
    assert "Intentional expected audit failure" in observed.error.message
    assert observed.status == "failed"
    assert observed.terminal is True
    assert [step.status for step in observed.steps] == ["completed"] * 3
    assert observed.model_dump(
        mode="json", exclude={"error", "updated_at"}
    ) == fixture.before.snapshot.model_dump(mode="json", exclude={"error", "updated_at"})
    stored = fixture.logger.get_public_progress_snapshot("job-1")
    assert stored.snapshot == observed
    assert stored.revision == fixture.before.revision + 1
    assert stored.marker_id
    assert len(fixture.repository.markers) == marker_count + 1
    assert fixture.repository.markers[-1]["step"] is None
    again = fixture.query.get(fixture.job)
    assert again == observed
    assert fixture.logger.get_public_progress_snapshot("job-1").revision == stored.revision
    assert len(fixture.repository.markers) == marker_count + 1
    assert fixture.committed_reads == ["thread-1", "thread-1"]


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-terminal",
        "pending-only-terminal",
        "running-checkpoint",
        "success-terminal",
        "missing-execution",
        "wrong-execution",
        "cursor-execution",
        "authored-kind",
        "archive-ref",
        "archive-content",
        "job",
        "project",
        "workflow",
        "unresolved-failure",
        "missing-operation",
    ],
)
def test_stop_failure_never_borrows_conflicting_or_uncommitted_identity(
    monkeypatch, request, corruption
):
    from copy import deepcopy

    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    values = fixture.holder["values"] = deepcopy(fixture.holder["values"])
    if corruption in {"missing-terminal", "pending-only-terminal"}:
        values.pop("workflow_terminal")
        if corruption == "pending-only-terminal":
            fixture.holder["pending_values"] = {
                "workflow_terminal": {
                    "kind": "stop_fail",
                    "status": "fail",
                    "step_id": "done",
                    "step_execution_id": fixture.stop_execution,
                }
            }
    elif corruption == "running-checkpoint":
        values["status"] = "in_progress"
    elif corruption == "success-terminal":
        values["workflow_terminal"].update(kind="stop_success", status="pass")
    elif corruption == "missing-execution":
        values["workflow_terminal"].pop("step_execution_id")
    elif corruption == "wrong-execution":
        values["workflow_terminal"]["step_execution_id"] = "another-execution"
    elif corruption == "cursor-execution":
        values["run_cursor"]["step_execution_id"] = "another-execution"
    elif corruption in {"authored-kind", "archive-content"}:
        archive = fixture.holder["archive"] = deepcopy(fixture.holder["archive"])
        archive["definition_jsonb"]["steps"][-1]["step_kind"] = "transform"
        if corruption == "authored-kind":
            from core.application.workflows.workflow_definition_snapshot import definition_digest

            archive["digest"] = definition_digest(archive["definition_jsonb"])
            values["workflow_definition_ref"]["digest"] = archive["digest"]
            values.pop("workflow_run_trace", None)
            values.pop("workflow_container_progress", None)
    elif corruption == "archive-ref":
        values["workflow_definition_ref"]["snapshot_id"] = "foreign-archive"
    elif corruption in {"job", "project", "workflow"}:
        values[
            {"job": "job_id", "project": "project_id", "workflow": "workflow_id"}[corruption]
        ] = "foreign"
    elif corruption == "unresolved-failure":
        values["unresolved_step_failure"] = {"step_id": "a", "error_code": "input_item_not_object"}
    elif corruption == "missing-operation":
        fixture.repository.value["snapshot"]["steps"] = fixture.repository.value["snapshot"][
            "steps"
        ][:-1]
    before = fixture.logger.get_public_progress_snapshot("job-1").snapshot
    observed = fixture.query.get(fixture.job)
    assert observed.error == before.error
    assert observed.steps == before.steps
    assert observed.status == "failed"
    assert observed.finished_at == before.finished_at


@pytest.mark.parametrize(
    "reason",
    [
        "token=redaction-fixture-value",
        "first line\nsecond line",
        "x" * 513,  # pragma: allowlist secret - redaction fixture
    ],
)
def test_stop_failure_keeps_exact_identity_but_rejects_unsafe_authored_reason(
    monkeypatch, request, reason
):
    fixture = _terminal_stop_reader_fixture(monkeypatch, request, reason=reason)
    observed = fixture.query.get(fixture.job)
    assert observed.error is not None
    assert observed.error.code == "workflow_stop_fail"
    assert observed.error.step_execution_id == fixture.stop_execution
    assert observed.error.step_id == "done"
    assert observed.error.message
    assert "\n" not in observed.error.message
    assert len(observed.error.message) <= 512
    assert reason not in observed.error.message
    assert not fixture.contains_public_secret(observed.error.message)
    assert [step.status for step in observed.steps] == ["completed"] * 3


def test_stop_metadata_marker_failure_is_atomic_and_specific_failure_wins(monkeypatch, request):
    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    fixture.repository.fail_marker = True
    observed = fixture.query.get(fixture.job)
    assert observed == fixture.before.snapshot
    assert fixture.logger.get_public_progress_snapshot("job-1").revision == fixture.before.revision
    fixture.repository.fail_marker = False
    specific = {
        "code": "provider_timeout",
        "message": "A provider did not respond",
        "step_id": "b",
        "category": "provider",
        "retryable": True,
    }
    fixture.logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="failed",
        occurred_at=NOW,
        finished_at=NOW,
        error=specific,
    )
    before = fixture.logger.get_public_progress_snapshot("job-1").snapshot
    observed = fixture.query.get(fixture.job)
    assert observed.error == before.error
    assert observed.steps == before.steps


@pytest.mark.parametrize("field", ["project_id", "selected_workflow"])
def test_stop_query_refuses_foreign_stored_scope_before_checkpoint_read(
    monkeypatch, request, field
):
    from core.application.management import ServiceError

    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    fixture.job[field] = "foreign"
    with pytest.raises(ServiceError, match="Public workflow state is unavailable"):
        fixture.query.get(fixture.job)
    assert fixture.committed_reads == []
    assert fixture.logger.get_public_progress_snapshot("job-1").snapshot == fixture.before.snapshot


def test_stop_enrichment_respects_newer_specific_error_in_atomic_current_row(monkeypatch, request):
    fixture = _terminal_stop_reader_fixture(monkeypatch, request)

    def advance_public_error():
        fixture.logger.record_public_progress_transition(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            job_status="failed",
            occurred_at=NOW,
            finished_at=NOW,
            error={
                "code": "provider_timeout",
                "message": "A provider did not respond",
                "step_id": "b",
                "category": "provider",
                "retryable": True,
            },
        )

    fixture.holder["before_committed_return"] = advance_public_error
    observed = fixture.query.get(fixture.job)
    current = fixture.logger.get_public_progress_snapshot("job-1")
    assert observed == current.snapshot
    assert observed.error is not None
    assert observed.error.code == "provider_timeout"
    assert observed.error.step_id == "b"
    assert observed.steps == fixture.before.snapshot.steps
    assert current.revision == fixture.before.revision + 1


def test_stop_template_reason_uses_static_fallback_without_losing_proven_identity(
    monkeypatch, request
):
    reason = "{{ state.audit_reason }}"
    fixture = _terminal_stop_reader_fixture(monkeypatch, request, reason=reason)
    observed = fixture.query.get(fixture.job)
    assert observed.error is not None
    assert observed.error.code == "workflow_stop_fail"
    assert observed.error.step_id == "done"
    assert observed.error.step_execution_id == fixture.stop_execution
    assert observed.error.message
    assert reason not in observed.error.message
    assert "{{" not in observed.error.message
    assert "state.audit_reason" not in observed.error.message
    assert "\n" not in observed.error.message
    assert len(observed.error.message) <= 512
    assert not fixture.contains_public_secret(observed.error.message)
    assert [step.status for step in observed.steps] == ["completed"] * 3


@pytest.mark.parametrize(
    "field", ["job.project_id", "job.selected_workflow", "checkpoint.project_id"]
)
@pytest.mark.parametrize(
    "contradictory", [False, True], ids=["absent-compatible", "explicit-foreign"]
)
def test_stop_optional_scope_absence_never_equals_a_foreign_claim(
    monkeypatch, request, field, contradictory
):
    from core.application.management import ServiceError

    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    owner, key = field.split(".")
    target = fixture.job if owner == "job" else fixture.holder["values"]
    if contradictory:
        target[key] = "foreign"
    else:
        target.pop(key)
    before = fixture.logger.get_public_progress_snapshot("job-1")
    if contradictory and owner == "job":
        with pytest.raises(ServiceError, match="Public workflow state is unavailable"):
            fixture.query.get(fixture.job)
        assert fixture.committed_reads == []
    else:
        observed = fixture.query.get(fixture.job)
        if contradictory:
            assert observed == before.snapshot
        else:
            assert observed.error is not None
            assert observed.error.code == "workflow_stop_fail"
            assert observed.error.step_id == "done"
            assert observed.error.step_execution_id == fixture.stop_execution
            assert [step.status for step in observed.steps] == ["completed"] * 3
    if contradictory:
        after = fixture.logger.get_public_progress_snapshot("job-1")
        assert after.snapshot == before.snapshot
        assert after.revision == before.revision
        assert after.marker_id == before.marker_id


@pytest.mark.parametrize("status", ["cancelled", "completed", "in_progress"])
def test_stop_checkpoint_cannot_reclassify_another_durable_public_lifecycle(
    monkeypatch, request, status
):
    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    original = fixture.before.snapshot
    # Seed a different valid durable lifecycle through the actual recorder,
    # retaining the exact real hub operations and checkpoint as conflicting proof.
    fixture.repository.value = None
    fixture.repository.markers.clear()
    for step in original.steps:
        fixture.logger.record_public_progress_transition(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            job_status="in_progress",
            occurred_at=NOW,
            queued_at=NOW,
            started_at=NOW,
            step=step,
        )
    if status != "in_progress":
        fixture.logger.record_public_progress_transition(
            job_id="job-1",
            project_id="project-1",
            workflow_id="workflow-1",
            job_status=status,
            occurred_at=NOW,
            finished_at=NOW,
            result=PublicWorkflowResultV1() if status == "completed" else None,
        )
    fixture.logger.reconcile_public_run_trace(
        job_id="job-1",
        trace=original.run_trace,
        container_progress=original.container_progress,
        occurred_at=NOW,
    )
    fixture.job.update(status=status, finished_at=NOW if status != "in_progress" else None)
    before = fixture.logger.get_public_progress_snapshot("job-1")
    markers = len(fixture.repository.markers)
    observed = fixture.query.get(fixture.job)
    assert observed == before.snapshot
    assert observed.status == status
    assert observed.terminal is (status != "in_progress")
    assert observed.error is None or observed.error.code != "workflow_stop_fail"
    assert [step.status for step in observed.steps] == ["completed"] * 3
    after = fixture.logger.get_public_progress_snapshot("job-1")
    assert after.revision == before.revision
    assert after.marker_id == before.marker_id
    assert len(fixture.repository.markers) == markers


def test_stop_metadata_preserves_a_real_trace_advance_at_the_locked_row_boundary(
    monkeypatch, request
):
    from core.application.jobs.workflow_run_models import PublicWorkflowTraceV1
    from core.infrastructure.db.platform_logger_activity_mixin import PlatformLoggerActivityMixin

    bridge = PlatformLoggerActivityMixin.reconcile_public_run_trace
    captured = {}

    def retain_initial_prefix(self, **evidence):
        trace = evidence["trace"]
        if not captured:
            captured["full"] = trace
            assert len(trace.transitions) >= 2
            prefix = trace.model_dump(mode="python")
            prefix.update(
                transitions=trace.transitions[:-1],
                last_sequence=trace.transitions[-2].sequence,
                terminal=False,
            )
            evidence = {**evidence, "trace": PublicWorkflowTraceV1.model_validate(prefix)}
        return bridge(self, **evidence)

    monkeypatch.setattr(
        PlatformLoggerActivityMixin, "reconcile_public_run_trace", retain_initial_prefix
    )
    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    monkeypatch.setattr(PlatformLoggerActivityMixin, "reconcile_public_run_trace", bridge)
    before = fixture.logger.get_public_progress_snapshot("job-1")
    full = captured["full"]
    assert before.snapshot.run_trace.last_sequence < full.last_sequence
    atomic_update = fixture.repository.atomic_update
    advanced = []
    locked_traces = []

    def interleaved_atomic_update(*, job_id, update):
        if fixture.committed_reads and not advanced:
            advanced.append(True)
            # Publish the later facts produced by the same real hub, not a
            # fabricated edge or mutation of the callback's pre-read snapshot.
            bridge(fixture.logger, job_id=job_id, trace=full, occurred_at=NOW)

        def capture_current(current):
            locked_traces.append(current["snapshot"].get("run_trace"))
            return update(current)

        return atomic_update(job_id=job_id, update=capture_current)

    monkeypatch.setattr(fixture.repository, "atomic_update", interleaved_atomic_update)
    observed = fixture.query.get(fixture.job)
    assert observed.error is not None
    assert observed.error.code == "workflow_stop_fail"
    assert observed.error.step_execution_id == fixture.stop_execution
    assert advanced == [True]
    assert locked_traces[-1] == full.model_dump(mode="json")
    assert observed.run_trace == full
    assert observed.container_progress == before.snapshot.container_progress
    assert observed.steps == before.snapshot.steps
    assert observed.status == "failed"
    assert observed.finished_at == before.snapshot.finished_at
    after = fixture.logger.get_public_progress_snapshot("job-1")
    assert after.snapshot == observed
    assert after.revision == before.revision + 2
    assert fixture.repository.markers[-1]["step"] is None


@pytest.mark.parametrize("channel", ["workflow_run_trace", "workflow_container_progress"])
def test_stop_metadata_does_not_depend_on_an_optional_progress_channel(
    monkeypatch, request, channel
):
    fixture = _terminal_stop_reader_fixture(monkeypatch, request)
    fixture.holder["values"][channel] = {"malformed": True}
    observed = fixture.query.get(fixture.job)
    assert observed.error is not None
    assert observed.error.code == "workflow_stop_fail"
    assert observed.error.step_id == "done"
    assert observed.error.step_execution_id == fixture.stop_execution
    assert observed.run_trace == fixture.before.snapshot.run_trace
    assert observed.container_progress == fixture.before.snapshot.container_progress
    assert observed.steps == fixture.before.snapshot.steps
    assert observed.status == "failed"
    assert observed.terminal is True


class _ProjectScopeLookalike:
    def __str__(self) -> str:
        return "0e44644c-6e50-4c68-a8f0-81a7ad96c089"


def _completed_project_scope_reader(project_id: str):
    logger = _Logger(has_snapshot=False)
    before = logger.record_public_progress_transition(
        job_id="job-1",
        project_id=project_id,
        workflow_id="workflow-1",
        job_status="completed",
        occurred_at=NOW,
        finished_at=NOW,
        result=PublicWorkflowResultV1(outputs={"zero": 0, "flag": False}, artifacts={}),
    )
    logger.transitions.clear()
    committed_reads = []
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _thread_id: SimpleNamespace(tasks=[]),
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=lambda thread_id: committed_reads.append(thread_id) or {},
    )
    return logger, before, query, committed_reads


@pytest.mark.parametrize(
    ("job_project", "public_project"),
    [
        (UUID("0e44644c-6e50-4c68-a8f0-81a7ad96c089"), "0e44644c-6e50-4c68-a8f0-81a7ad96c089"),
        ("0e44644c-6e50-4c68-a8f0-81a7ad96c089", "0e44644c-6e50-4c68-a8f0-81a7ad96c089"),
        ("project-1", "project-1"),
    ],
    ids=["postgres-uuid", "canonical-string", "legacy-string"],
)
def test_public_query_accepts_matching_typed_or_string_project_scope(job_project, public_project):
    logger, before, query, committed_reads = _completed_project_scope_reader(public_project)

    observed = query.get(_job(project_id=job_project, status="completed"))

    assert observed == before.snapshot
    assert observed.project_id == public_project
    assert observed.result is not None
    assert observed.result.outputs == {"zero": 0, "flag": False}
    assert observed.result.outputs["flag"] is False
    assert committed_reads == ["thread-1"]
    assert logger.transitions == []
    assert logger.current == before


@pytest.mark.parametrize(
    "job_project",
    [
        UUID("0e44644c-6e50-4c68-a8f0-81a7ad96c088"),
        "foreign",
        None,
        0,
        False,
        "not-a-project",
        " 0e44644c-6e50-4c68-a8f0-81a7ad96c089",
        _ProjectScopeLookalike(),
    ],
    ids=[
        "foreign-uuid",
        "foreign-string",
        "null",
        "zero",
        "false",
        "malformed",
        "whitespace",
        "object-spoof",
    ],
)
def test_public_query_refuses_nonmatching_or_nonidentity_project_claim_before_evidence(job_project):
    from core.application.management import ServiceError

    logger, before, query, committed_reads = _completed_project_scope_reader(
        "0e44644c-6e50-4c68-a8f0-81a7ad96c089"
    )

    job = _job(project_id=job_project, status="completed")
    with pytest.raises(ServiceError, match="Public workflow state is unavailable") as raised:
        query.get(job)

    assert raised.value.status_code == 503
    assert committed_reads == []
    assert logger.transitions == []
    assert logger.current == before
