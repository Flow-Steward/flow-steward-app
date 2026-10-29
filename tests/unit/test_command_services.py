from __future__ import annotations

import asyncio
import builtins
import contextlib
import gc
import hashlib
import io
import json
import os
import sys
import threading
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from tests.support.extension_icon import EXTENSION_ICON_PNG

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_WORKSPACE_ID
from core.application.extensions.identity import extension_folder_name
from core.application.jobs.workflow_resume_state import WorkflowResumeQueueRow
from core.application.management import command_services as command_services_module
from core.application.management.command_models import (
    ApprovalDecisionInput,
    ApprovalResumeAcceptedView,
    CommandDispatchInput,
    ExtensionProjectCreateInput,
    JobAcknowledgeInput,
    JobDecisionOverrideInput,
    JobRetryInterruptedStepInput,
    JobStepReplayExecutionInput,
    ProjectCreateInput,
    ProjectJobsBulkDeleteInput,
    ProjectPolicyUpdateInput,
    ProjectScheduledJobCreateInput,
    ProjectScheduledJobToggleInput,
    ProjectSettingsRestoreInput,
    ProjectTaskEnqueueInput,
    ProjectTaskFileInput,
    ProjectUpdateInput,
    ProjectWorkflowBindingsInput,
    ProviderEventIngestInput,
    ProviderWebhookIngestInput,
    ReactionEventIngestInput,
    ToolCloneInput,
    ToolInvocationInput,
    ToolPatchInput,
    ToolRestoreRevisionInput,
    ToolSaveInput,
    WorkflowReactionsUpdateInput,
)
from core.application.management.command_services import (
    ApprovalCommandService,
    CommandDispatchService,
    EventCommandService,
    ExtensionBindingService,
    ExternalAgentCommandService,
    JobCommandService,
    ModelProviderCommandService,
    ProjectCommandService,
    ToolCommandService,
    ToolExecutionService,
)
from core.application.management.command_services_common import _persist_file_refs
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.requests import (
    ExtensionActionRequest,
    ExtensionInvokeRequest,
    ExtensionQueryRequest,
)
from core.application.management.scope import ScopeContext
from core.application.orchestration.models import (
    ExternalAgentClaimRequestV1,
    ExternalAgentRegisterRequestV1,
    ExternalAgentReportRequestV1,
)


def _agent_clone_projection_lookup(source):
    def lookup(_db, artifact_kind, _id_key, artifact_id, fallback, **_kwargs):
        if artifact_kind == "agent" and artifact_id == source.get("agent_id"):
            return source
        return fallback

    return lookup


EXTERNAL_PROJECT_ID = "00000000-0000-0000-0000-000000000701"
EXTERNAL_PROJECT_ID_DENIED = "00000000-0000-0000-0000-000000000799"
TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV = "FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS"


def test_idempotency_result_retention_is_capped() -> None:
    from core.application.management.command_services_project_tasks_mixin import (
        _idempotency_result_expires_at,
    )

    expires_at = _idempotency_result_expires_at(retention_days=999999)

    assert expires_at <= datetime.now(UTC) + timedelta(days=3650, seconds=1)


def test_approval_lock_registry_releases_unused_thread_locks() -> None:
    from core.application.management import approval_decision_service as decision_module

    decision_module._locks.clear()
    lock = decision_module.approval_lock("thread-ephemeral")
    assert len(decision_module._locks) == 1

    del lock
    gc.collect()

    assert len(decision_module._locks) == 0


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result["value"]


EXTERNAL_JOB_ID = "00000000-0000-0000-0000-000000000741"


class TestRoleGuards:
    @pytest.mark.parametrize(
        ("principal_type", "minimum_role", "allowed", "message"),
        [
            ("viewer", "operator", False, "Operator role required"),
            ("user", "operator", False, "Operator role required"),
            ("operator", "operator", True, ""),
            ("service_account", "operator", True, ""),
            ("admin", "operator", True, ""),
            ("operator", "admin", False, "Admin role required"),
            ("admin", "admin", True, ""),
            ("system_actor", "admin", True, ""),
        ],
    )
    def test_require_role_uses_explicit_hierarchy(
        self,
        principal_type: str,
        minimum_role: str,
        allowed: bool,
        message: str,
    ):
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="tester",
            principal_type=principal_type,
        )

        if allowed:
            command_services_module._require_role(scope, minimum_role)
            return

        with pytest.raises(ServiceForbiddenError, match=message):
            command_services_module._require_role(scope, minimum_role)


class TestApprovalDecisionInputResolution:
    """Action-only decisions must resolve `approved` correctly (no silent approve)."""

    def test_action_only_reject_resolves_to_not_approved(self):
        decision = ApprovalDecisionInput.model_validate(
            {"action": "reject", "approval_id": "gate-1"}
        )
        assert decision.approved is False
        assert decision.action == "reject"

    def test_action_only_rework_resolves_to_not_approved(self):
        assert (
            ApprovalDecisionInput.model_validate(
                {"action": "rework", "approval_id": "gate-1"}
            ).approved
            is False
        )

    def test_action_only_approve_resolves_to_approved(self):
        assert (
            ApprovalDecisionInput.model_validate(
                {"action": "approve", "approval_id": "gate-1"}
            ).approved
            is True
        )

    def test_action_only_resume_resolves_to_approved(self):
        assert (
            ApprovalDecisionInput.model_validate(
                {"action": "resume", "approval_id": "gate-1"}
            ).approved
            is True
        )

    def test_explicit_approved_overrides_action_hint(self):
        # An explicit decision is authoritative even if it contradicts the action.
        decision = ApprovalDecisionInput.model_validate(
            {"approved": True, "action": "reject", "approval_id": "gate-1"}
        )
        assert decision.approved is True
        decision = ApprovalDecisionInput.model_validate(
            {"approved": False, "action": "approve", "approval_id": "gate-1"}
        )
        assert decision.approved is False

    def test_bare_decision_is_rejected(self):
        with pytest.raises(ValueError, match="explicit approval decision is required"):
            ApprovalDecisionInput.model_validate({"comment": "ok", "approval_id": "gate-1"})
        with pytest.raises(ValueError, match="explicit approval decision is required"):
            ApprovalDecisionInput()

    def test_none_approved_with_reject_action_resolves_false(self):
        # Mirrors the HTTP merge where the query default is now None.
        decision = ApprovalDecisionInput.model_validate(
            {"approved": None, "action": "reject", "approval_id": "gate-1"}
        )
        assert decision.approved is False

    def test_approval_decision_rejects_unknown_fields_and_oversized_action(self):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            ApprovalDecisionInput.model_validate(
                {
                    "approval_id": "gate-1",
                    "approved": True,
                    "unknown": "not-part-of-contract",
                }
            )

        with pytest.raises(ValueError, match="action exceeds maximum length"):
            ApprovalDecisionInput.model_validate(
                {"approval_id": "gate-1", "approved": True, "action": "a" * 256}
            )

        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            ApprovalDecisionInput.model_validate(
                {
                    "approval_id": "gate-1",
                    "approved": True,
                    "external_actor": {"id": "manager-1", "unknown": "value"},
                }
            )

    @pytest.mark.parametrize(
        "payload",
        [
            {"approval_id": "gate-1", "approved": "false"},
            {"approval_id": "gate-1", "approved": 0},
            {"approval_id": 7, "approved": True},
            {"approval_id": "gate-1", "approved": True, "comment": {"raw": "no"}},
            {
                "approval_id": "gate-1",
                "approved": True,
                "external_actor": {"id": 7},
            },
        ],
    )
    def test_approval_decision_rejects_coercive_public_values(self, payload):
        with pytest.raises(ValueError):
            ApprovalDecisionInput.model_validate(payload)


def test_persist_file_refs_removes_created_artifacts_after_later_failure() -> None:
    def _create_artifact(**kwargs):
        if kwargs["filename"] == "second.txt":
            raise RuntimeError("second upload failed")
        return (
            SimpleNamespace(artifact_id=kwargs["artifact_id"]),
            {
                "filename": "first.txt",
                "size_bytes": 5,
                "sha256": hashlib.sha256(b"first").hexdigest(),
            },
        )

    create_artifact = MagicMock(side_effect=_create_artifact)
    delete_artifact = MagicMock()
    plane = SimpleNamespace(create_artifact=create_artifact, delete_artifact=delete_artifact)

    _raises_input_304_1 = MagicMock()
    with pytest.raises(RuntimeError, match="second upload failed"):
        _persist_file_refs(
            _enqueue_logger=_raises_input_304_1,
            job_id="job-1",
            file_rows=[
                {"filename": "first.txt", "content": b"first"},
                {"filename": "second.txt", "content": b"second"},
            ],
            artifact_plane=plane,
            artifact_owner_scope={},
            artifact_run_id="run-1",
            artifact_step_id="manual_enqueue",
            source_ref_prefix="manual_file",
        )

    assert delete_artifact.call_count == 2
    assert {call.args[0] for call in delete_artifact.call_args_list} == {
        invocation.kwargs["artifact_id"] for invocation in create_artifact.call_args_list
    }


def test_persist_file_refs_journals_bytes_artifact_before_storage_write() -> None:
    events: list[tuple[str, str]] = []

    def _journal(ref: dict[str, str]) -> None:
        events.append(("journal", ref["artifact_id"]))

    def _create_artifact(**kwargs):
        events.append(("create", kwargs["artifact_id"]))
        return (
            SimpleNamespace(artifact_id=kwargs["artifact_id"]),
            {
                "filename": "provider.txt",
                "size_bytes": 4,
                "sha256": hashlib.sha256(b"data").hexdigest(),
            },
        )

    _persist_file_refs(
        _enqueue_logger=MagicMock(),
        job_id="job-1",
        file_rows=[{"filename": "provider.txt", "content": b"data"}],
        artifact_plane=SimpleNamespace(create_artifact=_create_artifact),
        artifact_owner_scope={},
        artifact_run_id="run-1",
        artifact_step_id="provider_ingest",
        source_ref_prefix="provider_file",
        on_artifact_created=_journal,
    )

    assert events[0][0] == "journal"
    assert events[1] == ("create", events[0][1])


def test_persist_file_refs_streams_staged_file_and_verifies_storage_identity(tmp_path) -> None:
    staged_path = tmp_path / "invoice.pdf"
    body = b"invoice-body"
    staged_path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()

    def _create_from_stream(**kwargs):
        artifact_id = kwargs["artifact_id"]
        assert b"".join(kwargs["chunks"]) == body
        assert kwargs["max_size_bytes"] == len(body)
        assert kwargs["allow_in_memory"] is False
        return (
            SimpleNamespace(artifact_id=artifact_id),
            {
                "filename": "invoice.pdf",
                "content_type": "application/pdf",
                "size_bytes": len(body),
                "sha256": digest,
                "object_key": f"artifacts/{artifact_id}/invoice.pdf",
                "retention_class": "run_window",
            },
        )

    plane = SimpleNamespace(
        create_artifact_from_stream=MagicMock(side_effect=_create_from_stream),
        delete_artifact=MagicMock(),
    )
    heartbeat = MagicMock()

    result = _persist_file_refs(
        _enqueue_logger=MagicMock(),
        job_id="job-1",
        file_rows=[
            {
                "filename": "invoice.pdf",
                "mime_type": "application/pdf",
                "content_path": str(staged_path),
                "size_bytes": len(body),
                "sha256": digest,
            }
        ],
        artifact_plane=plane,
        artifact_owner_scope={},
        artifact_run_id="run-1",
        artifact_step_id="manual_enqueue",
        source_ref_prefix="manual_file",
        lease_heartbeat=heartbeat,
    )

    assert result[0]["sha256"] == digest
    assert heartbeat.call_count >= 2
    plane.delete_artifact.assert_not_called()


def test_persist_file_refs_accepts_zero_byte_staged_file(tmp_path) -> None:
    staged_path = tmp_path / "empty.txt"
    staged_path.write_bytes(b"")
    digest = hashlib.sha256(b"").hexdigest()

    def _create_empty_from_stream(**kwargs):
        artifact_id = kwargs["artifact_id"]
        return (
            SimpleNamespace(artifact_id=artifact_id),
            {
                "filename": "empty.txt",
                "content_type": "text/plain",
                "size_bytes": 0,
                "sha256": digest,
                "object_key": f"artifacts/{artifact_id}/empty.txt",
                "retention_class": "run_window",
            },
        )

    plane = SimpleNamespace(
        create_artifact_from_stream=MagicMock(side_effect=_create_empty_from_stream),
        delete_artifact=MagicMock(),
    )

    result = _persist_file_refs(
        _enqueue_logger=MagicMock(),
        job_id="job-empty-1",
        file_rows=[
            {
                "filename": "empty.txt",
                "mime_type": "text/plain",
                "content_path": str(staged_path),
                "size_bytes": 0,
                "sha256": digest,
            }
        ],
        artifact_plane=plane,
        artifact_owner_scope={"entry_path": "manual_api"},
        artifact_run_id="run-empty-1",
        artifact_step_id="manual_enqueue",
        source_ref_prefix="manual_file",
    )

    assert result[0]["size_bytes"] == 0
    assert result[0]["sha256"] == digest
    plane.delete_artifact.assert_not_called()


def test_persist_file_refs_allows_memory_adapter_for_manual_ui_staged_file(tmp_path) -> None:
    staged_path = tmp_path / "manual-note.txt"
    body = b"manual-ui-note"
    staged_path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()

    def _create_from_stream(**kwargs):
        artifact_id = kwargs["artifact_id"]
        assert kwargs["allow_in_memory"] is True
        assert b"".join(kwargs["chunks"]) == body
        return (
            SimpleNamespace(artifact_id=artifact_id),
            {
                "filename": "manual-note.txt",
                "content_type": "text/plain",
                "size_bytes": len(body),
                "sha256": digest,
                "object_key": f"artifacts/{artifact_id}/manual-note.txt",
                "retention_class": "run_window",
            },
        )

    plane = SimpleNamespace(
        create_artifact_from_stream=MagicMock(side_effect=_create_from_stream),
        delete_artifact=MagicMock(),
    )

    _persist_file_refs(
        _enqueue_logger=MagicMock(),
        job_id="job-ui-1",
        file_rows=[
            {
                "filename": "manual-note.txt",
                "mime_type": "text/plain",
                "content_path": str(staged_path),
                "size_bytes": len(body),
                "sha256": digest,
            }
        ],
        artifact_plane=plane,
        artifact_owner_scope={"entry_path": "manual_ui"},
        artifact_run_id="run-ui-1",
        artifact_step_id="manual_enqueue",
        source_ref_prefix="manual_file",
    )


def test_persist_file_refs_rolls_back_staged_artifact_on_integrity_mismatch(tmp_path) -> None:
    staged_path = tmp_path / "invoice.pdf"
    staged_path.write_bytes(b"invoice-body")

    def _create_mismatched_from_stream(**kwargs):
        return (
            SimpleNamespace(artifact_id=kwargs["artifact_id"]),
            {"size_bytes": 1, "sha256": "not-the-staged-digest"},
        )

    plane = SimpleNamespace(
        create_artifact_from_stream=MagicMock(side_effect=_create_mismatched_from_stream),
        delete_artifact=MagicMock(),
    )

    _raises_input_522_1 = MagicMock()
    _raises_input_522_2 = [
        {
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "content_path": str(staged_path),
            "size_bytes": len(b"invoice-body"),
            "sha256": hashlib.sha256(b"invoice-body").hexdigest(),
        }
    ]
    with pytest.raises(RuntimeError, match="artifact_integrity_error"):
        _persist_file_refs(
            _enqueue_logger=_raises_input_522_1,
            job_id="job-1",
            file_rows=_raises_input_522_2,
            artifact_plane=plane,
            artifact_owner_scope={},
            artifact_run_id="run-1",
            artifact_step_id="manual_enqueue",
            source_ref_prefix="manual_file",
        )

    deleted_artifact_id = plane.delete_artifact.call_args.args[0]
    assert deleted_artifact_id.startswith("art_")


def test_merge_attachment_metadata_fails_before_publish_when_job_meta_cannot_be_persisted() -> None:
    bl = MagicMock()
    bl.get_job.return_value = {"meta": {"task_envelope": {"files": []}}}
    bl.update_job.side_effect = RuntimeError("jobs.meta unavailable")

    with pytest.raises(RuntimeError, match=r"jobs\.meta unavailable"):
        ProjectCommandService._merge_attachment_metadata(
            bl,
            "job-1",
            [
                {
                    "artifact_id": "artifact-1",
                    "filename": "invoice.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 12,
                    "sha256": "a" * 64,
                }
            ],
        )


class TestApprovalCommandService:
    def test_durable_job_decision_queues_worker_resume(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "project-durable",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
            "workflow-1",
            "job-durable",
        )
        fake_logger = SimpleNamespace(conn=MagicMock())
        fake_logger.conn.cursor.return_value.__enter__.return_value = cursor
        resume_row = WorkflowResumeQueueRow(
            job_id="job-durable",
            project_id="project-durable",
            task_id="TASK-9",
            resume_revision=3,
            publish_token="publish-3",
        )
        repository = MagicMock()
        enqueue_resume = MagicMock()
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: fake_logger,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=lambda _bl: repository,
        )
        service._claim_and_record_decision = MagicMock(
            return_value=(
                {"approval_id": "gate-9", "allowed_actions": ["approve"]},
                "gate-9",
                None,
                None,
                True,
                True,
                resume_row,
            )
        )

        result = service.resume_task(
            thread_id="thread-durable",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="chat:alice"),
            decision=ApprovalDecisionInput(
                approved=True,
                action="approve",
                comment="approved from chat",
                approval_id="gate-9",
            ),
            idempotency_key="chat-update-9",
        )

        assert result.status == "resume_pending"
        assert result.approval_id == "gate-9"
        enqueue_resume.assert_called_once_with(
            project_id="project-durable",
            job_id="job-durable",
            task_id="TASK-9",
            thread_id="thread-durable",
            resume_revision=3,
        )
        repository.mark_published.assert_called_once_with(
            thread_id="thread-durable",
            resume_revision=3,
            publish_token="publish-3",
        )

    def test_durable_resume_accepts_after_publish_when_marker_write_fails(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "project-durable",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
            "workflow-1",
            "job-durable",
        )
        fake_logger = SimpleNamespace(conn=MagicMock())
        fake_logger.conn.cursor.return_value.__enter__.return_value = cursor
        resume_row = WorkflowResumeQueueRow(
            job_id="job-durable",
            project_id="project-durable",
            task_id="TASK-9",
            resume_revision=3,
            publish_token="publish-3",
        )
        repository = MagicMock()
        repository.mark_published.side_effect = RuntimeError("marker database unavailable")
        enqueue_resume = MagicMock()
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: fake_logger,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=lambda _bl: repository,
        )
        service._claim_and_record_decision = MagicMock(
            return_value=(
                {"approval_id": "gate-9", "allowed_actions": ["approve"]},
                "gate-9",
                None,
                None,
                True,
                True,
                resume_row,
            )
        )

        result = service.resume_task(
            thread_id="thread-durable",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="chat:alice"),
            decision=ApprovalDecisionInput(
                approved=True,
                action="approve",
                comment="approved from chat",
                approval_id="gate-9",
            ),
            idempotency_key="chat-update-9",
        )

        assert result.status == "resume_pending"
        enqueue_resume.assert_called_once()

    def test_exact_replay_is_returned_while_resume_runtime_is_shutting_down(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "project-replay",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
            "workflow-1",
            "job-replay",
        )
        fake_logger = SimpleNamespace(conn=MagicMock())
        fake_logger.conn.cursor.return_value.__enter__.return_value = cursor
        replay = ApprovalResumeAcceptedView(
            job_id="job-replay",
            thread_id="thread-replay",
            approval_id="gate-replay",
            deduplicated=True,
            approved=True,
            comment="ok",
            actor="machine-1",
            status="in_progress",
        )
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: True,
            get_checkpointer=lambda: None,
            get_logger=lambda: fake_logger,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )
        service._claim_and_record_decision = MagicMock(
            return_value=({}, "", None, replay, True, True, None)
        )

        result = service.resume_task(
            thread_id="thread-replay",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="machine-1",
                principal_type="service_account",
                auth_source="machine",
            ),
            decision=ApprovalDecisionInput(
                approved=True,
                comment="ok",
                approval_id="gate-replay",
            ),
            idempotency_key="idem-replay",
        )

        assert result == replay

    def test_failed_resume_publish_keeps_accepted_decision_for_outbox_recovery(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "project-pending-resume",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
            "workflow-1",
            "job-pending-resume",
        )
        fake_logger = SimpleNamespace(conn=MagicMock())
        fake_logger.conn.cursor.return_value.__enter__.return_value = cursor
        resume_row = WorkflowResumeQueueRow(
            job_id="job-pending-resume",
            project_id="project-pending-resume",
            task_id="TASK-PENDING",
            resume_revision=2,
            publish_token="publish-pending",
        )
        repository = MagicMock()
        enqueue_resume = MagicMock(side_effect=RuntimeError("broker unavailable"))
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: fake_logger,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=lambda _bl: repository,
        )
        service._claim_and_record_decision = MagicMock(
            return_value=(
                {"approval_id": "gate-pending", "allowed_actions": ["approve"]},
                "gate-pending",
                None,
                None,
                True,
                True,
                resume_row,
            )
        )

        result = service.resume_task(
            thread_id="thread-pending-resume",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="operator"),
            decision=ApprovalDecisionInput(approved=True, comment="ok", approval_id="gate-pending"),
        )

        assert result.status == "resume_pending"
        enqueue_resume.assert_called_once()
        repository.mark_publish_failed.assert_called_once_with(
            thread_id="thread-pending-resume",
            resume_revision=2,
            publish_token="publish-pending",
            error="broker unavailable",
        )
        repository.mark_published.assert_not_called()

    def test_token_gate_requires_a_current_approval_id(self):
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: None,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )
        _raises_input_940_1 = ApprovalDecisionInput(approved=True, approval_id="gate-1")
        with pytest.raises(ServiceConflictError, match="Approval gate is stale"):
            service._validate_current_gate(
                row={"status": "awaiting_approval"},
                payload={},
                decision=_raises_input_940_1,
                token_principal=True,
            )

    def test_browser_gate_requires_the_current_approval_id(self):
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: None,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )

        _raises_input_957_1 = ApprovalDecisionInput.model_construct(
            approved=True,
            comment="",
            action=None,
            override_content="",
            approval_id="",
            external_actor=None,
        )
        with pytest.raises(ServiceConflictError, match="Approval gate is stale"):
            service._validate_current_gate(
                row={"status": "awaiting_approval"},
                payload={"approval_id": "gate-current"},
                decision=_raises_input_957_1,
                token_principal=False,
            )

    def test_token_decision_accepts_wrapper_interrupt_id_returned_by_public_get(self):
        interrupt = SimpleNamespace(id="interrupt-wrapper-1", value={"message": "Review"})
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: None,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
            get_thread_state_snapshot=lambda _thread_id: SimpleNamespace(
                tasks=[SimpleNamespace(interrupts=[interrupt])]
            ),
        )

        payload = service._current_interrupt_payload("thread-wrapper-id", attempts=1)

        assert payload["interrupt_id"] == "interrupt-wrapper-1"
        assert (
            service._validate_current_gate(
                row={"status": "awaiting_approval"},
                payload=payload,
                decision=ApprovalDecisionInput(
                    approved=True,
                    approval_id="interrupt-wrapper-1",
                ),
                token_principal=True,
            )
            == "interrupt-wrapper-1"
        )

    def test_canonical_decision_records_marker_in_its_pinned_transaction(self):
        marker_transaction = object()
        bl = SimpleNamespace(conn=MagicMock())
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: bl,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )
        service._record_in_progress_marker = MagicMock()

        service._persist_canonical_decision(
            bl=bl,
            transaction_conn=marker_transaction,
            row={
                "id": "job-marker",
                "project_id": "project-1",
                "selected_workflow": "workflow-1",
                "meta": {},
            },
            thread_id="thread-marker",
            approval_id="gate-marker",
            payload={"message": "approve"},
            decision=ApprovalDecisionInput(approved=True, comment="ok", approval_id="gate-marker"),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="operator"),
        )

        service._record_in_progress_marker.assert_called_once()
        assert (
            service._record_in_progress_marker.call_args.kwargs["transaction_conn"]
            is marker_transaction
        )

    def test_canonical_decision_atomically_persists_singular_resume_occurrence(self):
        cursor = MagicMock()
        bl = SimpleNamespace(conn=MagicMock())
        bl.conn.cursor.return_value.__enter__.return_value = cursor
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: bl,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )
        service._record_in_progress_marker = MagicMock()

        resume_row = service._persist_canonical_decision(
            bl=bl,
            transaction_conn=object(),
            row={
                "id": "job-atomic",
                "project_id": "project-1",
                "task_id": "TASK-1",
                "selected_workflow": "workflow-1",
                "meta": {},
            },
            thread_id="thread-atomic",
            approval_id="gate-authoritative",
            payload={
                "message": "approve",
                "allowed_actions": ["approve"],
                "gate_kind": "autonomy_gate",
                "gate_meta": {"step_id": "release", "target_agent": "reviewer"},
            },
            decision=ApprovalDecisionInput(
                approved=True,
                action="approve",
                comment="ok",
                approval_id="gate-authoritative",
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="chat:alice"),
        )

        sql, params = cursor.execute.call_args.args
        persisted_meta = json.loads(params[0])
        assert "status = %s" in sql
        assert params[1] == "resume_pending"
        assert persisted_meta["workflow_resume"]["reason"] == "approval"
        assert persisted_meta["workflow_resume"]["approval_id"] == "gate-authoritative"
        assert persisted_meta["workflow_resume"]["state"] == "pending"
        assert persisted_meta["workflow_resume"]["resume_value"] == {
            "reason": "approval",
            "approved": True,
            "action": "approve",
            "comment": "ok",
        }
        assert resume_row is not None
        assert resume_row.job_id == "job-atomic"
        assert resume_row.task_id == "TASK-1"

    def test_canonical_decision_rejects_lost_awaiting_approval_cas(self):
        cursor = MagicMock()
        cursor.rowcount = 0
        bl = SimpleNamespace(conn=MagicMock())
        bl.conn.cursor.return_value.__enter__.return_value = cursor
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: bl,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )

        _raises_input_1114_1 = object()
        _raises_input_1114_2 = ApprovalDecisionInput(
            approved=True, comment="ok", approval_id="gate-raced"
        )
        _raises_input_1114_3 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="operator")
        with pytest.raises(ServiceConflictError, match="no longer awaiting approval"):
            service._persist_canonical_decision(
                bl=bl,
                transaction_conn=_raises_input_1114_1,
                row={"id": "job-raced", "project_id": "project-1", "task_id": "TASK-1", "meta": {}},
                thread_id="thread-raced",
                approval_id="gate-raced",
                payload={"message": "approve"},
                decision=_raises_input_1114_2,
                scope=_raises_input_1114_3,
            )

    def test_canonical_decision_resolves_the_active_approval_step(self):
        from core.application.jobs.workflow_run_models import PublicWorkflowStepV1

        progress = MagicMock()
        progress.current_snapshot.return_value = SimpleNamespace(
            snapshot=SimpleNamespace(
                active_steps=[
                    PublicWorkflowStepV1(
                        step_id="review",
                        step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                        occurrence=1,
                        step_kind="approval",
                        status="awaiting_approval",
                        started_at=datetime.now(UTC),
                    )
                ]
            )
        )
        with (
            patch("core.application.jobs.public_progress_repository.PublicProgressRepository"),
            patch(
                "core.application.jobs.public_progress_service.PublicProgressService",
                return_value=progress,
            ),
        ):
            ApprovalCommandService._record_in_progress_marker(
                transaction_conn=object(),
                row={
                    "id": "job-1",
                    "project_id": "project-1",
                    "selected_workflow": "workflow-1",
                },
                payload={
                    "step_id": "review",
                    "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                },
            )

        resolved_step = progress.record_transition.call_args.kwargs["step"]
        assert resolved_step.status == "completed"
        assert resolved_step.finished_at is not None

    def test_lock_job_row_propagates_database_failure(self):
        """A failed row lock must abort instead of falling through to resume."""

        class _BrokenCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args, **_kwargs):
                raise RuntimeError("database lock unavailable")

        class _BrokenConn:
            def cursor(self):
                return _BrokenCursor()

        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: None,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )

        _raises_input_1199_1 = SimpleNamespace(conn=_BrokenConn())
        with pytest.raises(ServiceUnavailableError, match="Could not lock approval job"):
            service._lock_job_row(_raises_input_1199_1, "thread-lock-failure")

    def test_decision_without_recorded_workflow_resume_is_refused_without_publishing(self):
        """An accepted decision must never resume unless a durable resume row was recorded."""

        project_cursor = MagicMock()
        project_cursor.fetchone.return_value = (
            "project-accept-before-launch",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
            "workflow-1",
            "job-accept-before-launch",
        )
        fake_logger = SimpleNamespace(conn=MagicMock())
        fake_logger.conn.cursor.return_value.__enter__.return_value = project_cursor
        repository = MagicMock()
        enqueue_resume = MagicMock()
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: fake_logger,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=lambda _bl: repository,
        )
        service._claim_and_record_decision = MagicMock(
            return_value=(
                {"approval_id": "gate-1", "allowed_actions": ["approve"]},
                "gate-1",
                None,
                SimpleNamespace(claimed=True),
                True,
                True,
                None,
            )
        )

        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="machine-1",
            principal_type="service_account",
            auth_source="machine",
        )
        decision = ApprovalDecisionInput(
            approved=True,
            comment="ok",
            approval_id="gate-1",
        )
        with pytest.raises(ServiceError, match="did not record a workflow resume"):
            service.resume_task(
                thread_id="thread-accept-before-launch",
                scope=scope,
                decision=decision,
                idempotency_key="idem-accept-before-launch",
            )

        enqueue_resume.assert_not_called()
        repository.mark_published.assert_not_called()
        repository.mark_publish_failed.assert_not_called()

    def test_jobless_resume_is_rejected(self):
        enqueue_resume = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        logger = MagicMock()
        logger.conn.cursor.return_value.__enter__.return_value = cursor
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: logger,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=MagicMock(),
        )

        _raises_input_1326_1 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="operator")
        _raises_input_1326_2 = ApprovalDecisionInput(
            approved=True, comment="ok", approval_id="gate-jobless"
        )
        with pytest.raises(ServiceNotFoundError, match="Approval job not found"):
            service.resume_task(
                thread_id="thread-jobless-with-connection",
                scope=_raises_input_1326_1,
                decision=_raises_input_1326_2,
            )

        enqueue_resume.assert_not_called()

    def test_token_approve_action_cannot_conflict_with_explicit_rejection(self):
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: None,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )
        _raises_input_1365_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="machine-1",
            principal_type="service_account",
            auth_source="machine",
        )
        _raises_input_1365_2 = ApprovalDecisionInput(
            approved=False, action="approve", comment="conflicting decision", approval_id="gate-1"
        )
        with pytest.raises(ServiceValidationError, match="conflicts with approve action"):
            service.resume_task(
                thread_id="thread-conflicting-approval",
                scope=_raises_input_1365_1,
                decision=_raises_input_1365_2,
                idempotency_key="idem-1",
            )

    def test_token_abort_without_comment_is_accepted_by_canonical_service(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "project-abort",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
            "workflow-1",
            "job-abort",
        )
        fake_logger = SimpleNamespace(conn=MagicMock())
        fake_logger.conn.cursor.return_value.__enter__.return_value = cursor
        resume_row = WorkflowResumeQueueRow(
            job_id="job-abort",
            project_id="project-abort",
            task_id="TASK-ABORT",
            resume_revision=1,
            publish_token="publish-abort",
        )
        repository = MagicMock()
        enqueue_resume = MagicMock()
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: fake_logger,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=lambda _bl: repository,
        )
        service._claim_and_record_decision = MagicMock(
            return_value=(
                {"approval_id": "gate-abort", "allowed_actions": ["abort"]},
                "gate-abort",
                None,
                None,
                True,
                True,
                resume_row,
            )
        )

        result = service.resume_task(
            thread_id="thread-abort",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="chat-bot",
                principal_type="service_account",
                auth_source="machine",
            ),
            decision=ApprovalDecisionInput(
                approved=False,
                action="abort",
                comment="",
                approval_id="gate-abort",
            ),
            idempotency_key="chat-abort-1",
        )

        assert result.status == "resume_pending"
        recorded = service._claim_and_record_decision.call_args.kwargs["decision"]
        assert recorded.action == "abort"
        assert recorded.comment == ""
        enqueue_resume.assert_called_once()

    @pytest.mark.parametrize("action", [None, "reject", "deny", "rework", "custom_negative"])
    def test_token_non_abort_negative_action_without_comment_is_rejected(self, action):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "project-negative",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
            "workflow-1",
            "job-negative",
        )
        fake_logger = SimpleNamespace(conn=MagicMock())
        fake_logger.conn.cursor.return_value.__enter__.return_value = cursor
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: fake_logger,
            enqueue_workflow_resume=MagicMock(),
            workflow_resume_repository_factory=MagicMock(),
        )
        service._claim_and_record_decision = MagicMock(
            side_effect=AssertionError("invalid decision reached persistence")
        )

        _raises_input_1472_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="machine-1",
            principal_type="service_account",
            auth_source="machine",
        )
        _raises_input_1472_2 = ApprovalDecisionInput(
            approved=False, action=action, comment="", approval_id="gate-negative"
        )
        with pytest.raises(ServiceValidationError, match="comment is required"):
            service.resume_task(
                thread_id="thread-negative",
                scope=_raises_input_1472_1,
                decision=_raises_input_1472_2,
                idempotency_key="negative-1",
            )

        service._claim_and_record_decision.assert_not_called()

    def test_resume_task_rejects_project_outside_restricted_principal_allow_list(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (
            "denied-project",
            DEFAULT_ACCOUNT_ID,
            DEFAULT_WORKSPACE_ID,
        )
        mock_bl = MagicMock()
        mock_bl.conn.cursor.return_value.__enter__.return_value = mock_cursor
        enqueue_resume = MagicMock()

        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: mock_bl,
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=MagicMock(),
        )

        _raises_input_1601_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="restricted",
            principal_type="service_account",
            allowed_project_ids=frozenset({"allowed-project"}),
        )
        _raises_input_1601_2 = ApprovalDecisionInput(
            approved=False, action="rework", approval_id="gate-denied"
        )
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.resume_task(
                thread_id="thread-denied", scope=_raises_input_1601_1, decision=_raises_input_1601_2
            )

        enqueue_resume.assert_not_called()

    def test_resume_task_denies_workflow_outside_token_allowlist(self):
        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args, **_kwargs):
                return None

            def fetchone(self):
                return ("7", DEFAULT_ACCOUNT_ID, "ws-1", "wf-denied")

        class _Connection:
            def cursor(self):
                return _Cursor()

        enqueue_resume = MagicMock()
        service = ApprovalCommandService(
            resume_shutdown_in_progress=lambda: False,
            get_checkpointer=lambda: object(),
            get_logger=lambda: SimpleNamespace(conn=_Connection()),
            enqueue_workflow_resume=enqueue_resume,
            workflow_resume_repository_factory=MagicMock(),
        )

        _raises_input_1645_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="machine-1",
            principal_type="service_account",
            auth_source="machine",
            allowed_workflow_ids=frozenset({"wf-allowed"}),
        )
        _raises_input_1645_2 = ApprovalDecisionInput(
            approved=True, comment="approved", approval_id="gate-1"
        )
        with pytest.raises(ServiceForbiddenError):
            service.resume_task(
                thread_id="workflow-thread-denied",
                scope=_raises_input_1645_1,
                decision=_raises_input_1645_2,
                idempotency_key="decision-1",
            )
        enqueue_resume.assert_not_called()

    def test_non_chat_provider_event_uses_exact_project_connection_binding(self):
        bl = MagicMock()
        bl.db_store = MagicMock()
        bl.db_store.get_workflow.return_value = None
        bl.get_project_connection.return_value = {
            "extension_id": "acme.task-provider",
            "enabled": True,
            "connection_config": {},
        }
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
        )

        assert (
            service._provider_event_channel_ref(
                provider_key="acme.task-provider",
                payload={"comment": "ready"},
            )
            == ""
        )
        assert (
            service._provider_event_binding_authorized(
                bl,
                project_id="11",
                provider_key="acme.task-provider",
            )
            is True
        )
        bl.get_project_connection.assert_called_once_with(
            project_id="11",
            extension_id="acme.task-provider",
            enabled_only=True,
        )

    def test_non_chat_provider_event_rejects_cross_provider_connection(self):
        bl = MagicMock()
        bl.get_project_connection.return_value = {
            "extension_id": "acme.other-provider",
            "enabled": True,
        }
        bl.list_extension_callback_registrations.return_value = []
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
        )

        assert (
            service._provider_event_binding_authorized(
                bl,
                project_id="11",
                provider_key="acme.task-provider",
            )
            is False
        )

    def test_provider_webhook_binding_requires_exact_provider_key(self):
        service = EventCommandService(
            get_logger=lambda: MagicMock(),
            resolve_domain_event_by_ref=lambda *_args, **_kwargs: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
        )

        assert not service._project_provider_binding_matches(
            binding={"enabled": True},
            provider_key="github",
            project_account_id=DEFAULT_ACCOUNT_ID,
        )
        assert not service._project_provider_binding_matches(
            binding={"integration_key": "slack", "enabled": True},
            provider_key="github",
            project_account_id=DEFAULT_ACCOUNT_ID,
        )
        assert service._project_provider_binding_matches(
            binding={"provider_key": "github", "enabled": True},
            provider_key="github",
            project_account_id=DEFAULT_ACCOUNT_ID,
        )

    def test_redrive_event_requeues_failed_dispatches(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.list_event_dispatches.return_value = [
            {"dispatch_id": "dispatch-1", "status": "failed"},
            {"dispatch_id": "dispatch-2", "status": "dead_lettered"},
            {"dispatch_id": "dispatch-3", "status": "delivered"},
        ]
        bl.update_event_dispatch_status.side_effect = [
            {"dispatch_id": "dispatch-1"},
            {"dispatch_id": "dispatch-2"},
        ]
        event = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "event_id": "evt-1",
            "event_type": "workflow.failed",
            "severity": "error",
            "status": "failed",
            "summary": "Workflow failed",
            "payload": {"detail": "stack"},
            "actor": {"username": "system"},
            "urls": {},
            "envelope": {},
            "project_id": "7",
            "job_id": 9,
            "occurred_at": now,
            "created_at": now,
        }

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: (
                event if event_ref == "evt-1" else None
            ),
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
        )

        payload = service.redrive_event(
            "evt-1",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, principal_type="service_account"),
        ).dump()

        assert payload["status"] == "accepted"
        assert payload["event_id"] == "evt-1"
        assert payload["requested_dispatches"] == ["dispatch-1", "dispatch-2"]
        assert payload["redriven_dispatches"] == ["dispatch-1", "dispatch-2"]
        assert bl.update_event_dispatch_status.call_count == 2
        bl.record_domain_event.assert_called_once()
        assert bl.list_event_dispatches.call_args.kwargs["account_id"] == DEFAULT_ACCOUNT_ID

    def test_redrive_event_rejects_cross_account_event(self):
        bl = MagicMock()
        now = datetime.now(UTC)
        event = {
            "event_id": "evt-1",
            "event_type": "workflow.failed",
            "severity": "error",
            "status": "failed",
            "summary": "Workflow failed",
            "payload": {"detail": "stack"},
            "actor": {"username": "system"},
            "urls": {},
            "envelope": {},
            "account_id": "acc_beta",
            "project_id": "7",
            "job_id": 9,
            "occurred_at": now,
            "created_at": now,
        }

        def _resolver(_bl, _event_ref, **kwargs):
            assert kwargs["account_id"] == "acc_alpha"
            return event

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=_resolver,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
        )

        _raises_input_1832_1 = ScopeContext(
            account_id="acc_alpha", principal_type="service_account"
        )
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.redrive_event("evt-1", scope=_raises_input_1832_1)

        bl.list_event_dispatches.assert_not_called()

    def test_redrive_event_requires_failed_or_dead_lettered_event_state(self):
        bl = MagicMock()
        now = datetime.now(UTC)
        event = {
            "event_id": "evt-1",
            "event_type": "workflow.completed",
            "severity": "info",
            "status": "succeeded",
            "summary": "Workflow completed",
            "payload": {},
            "actor": {"username": "system"},
            "urls": {},
            "envelope": {},
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "7",
            "job_id": 9,
            "occurred_at": now,
            "created_at": now,
        }

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda _bl, _event_ref, **_kwargs: event,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
        )

        _raises_input_1869_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, principal_type="service_account"
        )
        with pytest.raises(ServiceConflictError, match="failed/dead_lettered"):
            service.redrive_event("evt-1", scope=_raises_input_1869_1)

        bl.list_event_dispatches.assert_not_called()

    def test_ingest_provider_webhook_enqueues_shared_task_contract(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": project_id,
            "name": "Demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project_connection.return_value = {
            "extension_id": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
        }
        enqueue_task = MagicMock(return_value=job_id)
        get_project_run_config = MagicMock(return_value={"project_name": "Demo"})

        service = EventCommandService(
            get_logger=lambda: bl,
            get_project_run_config=get_project_run_config,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
        )

        payload = service.ingest_provider_webhook(
            ProviderWebhookIngestInput(
                provider_key="sample_tasks",
                project_id=project_id,
                project_config={},
                task_id="cu-1",
                source_id="cu-1",
                title="Fix bug",
                description="Nested payload",
                task_type="feature",
                trigger_config={"event": "taskCreated", "workflow_id": "wf.sample_tasks.intake"},
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                project_id=project_id,
                actor_id="sample_tasks_webhook",
            ),
        ).dump()

        assert payload["provider_key"] == "sample_tasks"
        assert payload["task_id"] == "cu-1"
        assert payload["job_id"] == job_id
        assert payload["queue"] == f"platform.project.{project_id}"
        assert bl.get_project.call_args.args[0] == project_id
        get_project_run_config.assert_called_once_with(project_id)
        bl.get_project_run_config.assert_not_called()
        assert enqueue_task.call_args.kwargs["task_summary"] == "Fix bug: Nested payload"
        assert enqueue_task.call_args.kwargs["task_source"] == "provider"
        assert (
            enqueue_task.call_args.kwargs["extra_state"]["workflow_id"] == "wf.sample_tasks.intake"
        )
        assert (
            enqueue_task.call_args.kwargs["extra_state"]["external_provider_key"] == "sample_tasks"
        )
        assert enqueue_task.call_args.kwargs["extra_state"]["scope_actor"] == "sample_tasks_webhook"
        bl.get_project_connection.assert_called_once_with(
            project_id=project_id,
            extension_id="sample_tasks",
            enabled_only=True,
        )

    def test_ingest_provider_webhook_rejects_provider_account_mismatch(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": project_id,
            "name": "Demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "account_id": "acc_other",
        }
        enqueue_task = MagicMock()

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
        )

        _raises_input_1986_1 = ProviderWebhookIngestInput(
            provider_key="sample_tasks",
            project_id=project_id,
            project_config={"project_name": "Demo"},
            task_id="cu-1",
            source_id="cu-1",
            title="Fix bug",
        )
        _raises_input_1986_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, project_id=project_id, actor_id="sample_tasks_webhook"
        )
        with pytest.raises(ServiceForbiddenError):
            service.ingest_provider_webhook(_raises_input_1986_1, scope=_raises_input_1986_2)

        enqueue_task.assert_not_called()

    def test_ingest_provider_webhook_persists_fetched_attachments_before_enqueue(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 11, "name": "Demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project_connection.return_value = {
            "extension_id": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
        }
        bl.get_job.return_value = {
            "workflow_definition_snapshot_id": "snapshot-upload",
            "meta": {
                "workflow_definition_ref": {"revision": 4, "digest": "a" * 64},
                "attachment_persistence_artifacts": [
                    {"artifact_id": "art-old", "object_key": "old/blob"}
                ],
                "existing": "yes",
                "invocation": {
                    "files": [],
                    "request_context_summary": {"file_count": 0, "file_names": []},
                },
            },
        }
        plane = SimpleNamespace(
            create_artifact=MagicMock(
                side_effect=lambda **kwargs: (
                    SimpleNamespace(artifact_id=kwargs["artifact_id"]),
                    {
                        "filename": "spec.png",
                        "content_type": "image/png",
                        "size_bytes": 4,
                        "sha256": "abc123",
                        "object_key": "provider/spec.png",
                        "retention_class": "run_window",
                    },
                )
            )
        )

        prepared_patch = {}

        def _enqueue_task(**kwargs):
            prepared_patch.update(kwargs["pre_publish_fn"](909, bl))
            return 909

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=_enqueue_task,
            fetch_provider_attachments=lambda **kwargs: [
                {
                    "attachment_id": "att-1",
                    "filename": "spec.png",
                    "mime_type": "image/png",
                    "content": b"data",
                }
            ],
            allowed_attachment_mimes={"image/png"},
            allowed_attachment_extensions={".png"},
            artifact_plane=plane,
        )

        payload = service.ingest_provider_webhook(
            ProviderWebhookIngestInput(
                provider_key="sample_tasks",
                project_id="11",
                project_config={"project_name": "Demo"},
                task_id="cu-1",
                source_id="cu-1",
                title="Fix bug",
                description="Nested payload",
                task_type="feature",
                trigger_config={"event": "taskCreated", "workflow_id": "wf.sample_tasks.intake"},
                files=[
                    {
                        "attachment_id": "att-1",
                        "filename": "spec.png",
                        "mime_type": "image/png",
                        "url": "https://attachments.sample_tasks.com/spec.png",
                    }
                ],
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID, project_id="11", actor_id="sample_tasks_webhook"
            ),
        ).dump()

        assert payload["file_count"] == 1
        assert plane.create_artifact.call_count == 1
        assert plane.create_artifact.call_args.kwargs["filename"] == "spec.png"
        assert prepared_patch["files"][0]["filename"] == "spec.png"
        assert (
            prepared_patch["files"][0]["artifact_id"]
            == (plane.create_artifact.call_args.kwargs["artifact_id"])
        )
        invocation = prepared_patch["invocation"]
        assert invocation["request_context_summary"] == {
            "file_count": 1,
            "file_names": ["spec.png"],
        }
        assert invocation["files"] == [
            {
                "artifact_id": plane.create_artifact.call_args.kwargs["artifact_id"],
                "content_type": "image/png",
                "field_name": None,
                "filename": "spec.png",
                "size_bytes": 4,
            }
        ]
        assert "object_key" not in json.dumps(invocation)

        assert prepared_patch["existing"] == "yes"
        assert prepared_patch["workflow_definition_ref"] == {"revision": 4, "digest": "a" * 64}
        assert prepared_patch["attachment_persistence_artifacts"][0] == {
            "artifact_id": "art-old",
            "object_key": "old/blob",
        }
        assert prepared_patch["task_envelope"]["payload_complete"] is True
        assert prepared_patch["task_envelope"]["files"] == prepared_patch["files"]

    def test_ingest_provider_webhook_routes_comment_events_to_reaction_flow(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 11, "name": "Demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project_connection.return_value = {
            "extension_id": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
        }
        bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
            "id": "55",
            "project_id": "11",
            "thread_id": "thread-55",
            "status": "awaiting_approval",
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        enqueue_task = MagicMock(return_value=909)

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
        )
        reaction_payload = MagicMock()
        reaction_payload.dump.return_value = {
            "event": {"id": 91, "event_type": "external.comment.received"},
            "reactions": [{"reaction_key": "comment_resume", "status": "resumed"}],
            "notification_deliveries": [],
        }

        with patch.object(
            service, "ingest_reaction_event", return_value=reaction_payload
        ) as ingest_reaction_event:
            payload = service.ingest_provider_webhook(
                ProviderWebhookIngestInput(
                    provider_key="sample_tasks",
                    project_id="11",
                    project_config={"project_name": "Demo"},
                    task_id="cu-55",
                    source_id="comment-1",
                    title="Sample Tasks comment",
                    description="Please retry the review",
                    task_type="",
                    metadata={
                        "comment_text": "Please retry the review",
                        "comment_id": "comment-1",
                        "event": "taskCommentPosted",
                    },
                    trigger_config={"event": "taskCommentPosted"},
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, project_id="11", actor_id="sample_tasks_webhook"
                ),
            ).dump()

        assert payload["status"] == "accepted"
        assert payload["task_id"] == "cu-55"
        assert payload["job_id"] == "55"
        assert payload["comment_length"] == len("Please retry the review")
        assert payload["reaction"]["event"]["event_type"] == "external.comment.received"
        enqueue_task.assert_not_called()
        reaction_request = ingest_reaction_event.call_args.args[0]
        assert reaction_request.event_type == "external.comment.received"
        assert reaction_request.job_id == "55"
        assert reaction_request.workflow_id == "weekly_report"
        assert reaction_request.source == "sample_tasks"
        assert reaction_request.source_id == "comment-1"
        assert reaction_request.payload["text"] == "Please retry the review"
        assert reaction_request.payload["task_id"] == "cu-55"

    def test_ingest_provider_webhook_ignores_comment_job_from_other_project(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": "11", "name": "Demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
        }
        bl.get_provider.return_value = {
            "provider_key": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project_connection.return_value = {
            "extension_id": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
        }
        bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
            "id": "55",
            "project_id": "22",
            "thread_id": "thread-55",
            "status": "awaiting_approval",
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        enqueue_task = MagicMock(return_value=909)

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
        )

        with patch.object(service, "ingest_reaction_event") as ingest_reaction_event:
            payload = service.ingest_provider_webhook(
                ProviderWebhookIngestInput(
                    provider_key="sample_tasks",
                    project_id="11",
                    project_config={"project_name": "Demo"},
                    task_id="cu-55",
                    source_id="comment-1",
                    title="Sample Tasks comment",
                    description="Please retry the review",
                    task_type="",
                    metadata={
                        "comment_text": "Please retry the review",
                        "comment_id": "comment-1",
                        "event": "taskCommentPosted",
                    },
                    trigger_config={"event": "taskCommentPosted"},
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="11",
                    actor_id="sample_tasks_webhook",
                ),
            ).dump()

        assert payload["status"] == "ignored"
        enqueue_task.assert_not_called()
        ingest_reaction_event.assert_not_called()
        sql_args = bl.conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
        assert sql_args[0] == "cu-55"
        assert sql_args[1] == "11"

    def test_ingest_reaction_event_persists_reactions_and_notifications(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": job_id,
            "project_id": project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project.return_value = {
            "id": project_id,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.log_job_reaction_event.return_value = {
            "id": 91,
            "job_id": job_id,
            "workflow_id": "weekly_report",
            "event_type": "external.comment.received",
            "payload": {"text": "please retry"},
            "processed": False,
        }
        bl.mark_job_reaction_event_processed.return_value = {
            "id": 91,
            "job_id": job_id,
            "workflow_id": "weekly_report",
            "event_type": "external.comment.received",
            "payload": {"text": "please retry"},
            "processed": True,
        }

        mock_dispatch = MagicMock(return_value=[{"delivery_id": 3, "status": "delivered"}])

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
            evaluate_workflow_reactions=lambda current_bl, event, *, scope=None: [
                {"reaction_key": "comment_resume", "executed": True}
            ],
            reaction_action_result_executed=lambda item: bool(item.get("executed")),
            dispatch_project_notification_event=mock_dispatch,
        )

        payload = service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="external.comment.received",
                job_id=job_id,
                source="sample_tasks",
                source_id="comment-1",
                event_key="comment.created",
                dedupe_key="job55-comment1",
                payload={"text": "please retry"},
            )
        ).dump()

        assert payload["event"]["processed"] is True
        assert payload["event"]["account_id"] == DEFAULT_ACCOUNT_ID
        assert payload["event"]["correlation_id"] == "job55-comment1"
        assert payload["event"]["envelope"]["schema_version"] == "event_envelope_v1"
        assert payload["reactions"][0]["reaction_key"] == "comment_resume"
        assert payload["notification_deliveries"][0]["status"] == "delivered"
        bl.log_job_reaction_event.assert_called_once()
        assert bl.get_job.call_args.args[0] == job_id
        assert bl.log_job_reaction_event.call_args.kwargs["job_id"] == job_id
        bl.enqueue_domain_event.assert_called_once()
        bl.mark_job_reaction_event_processed.assert_called_once_with("91", processed=True)
        enqueue_call = bl.enqueue_domain_event.call_args.kwargs
        assert enqueue_call["account_id"] == DEFAULT_ACCOUNT_ID
        assert enqueue_call["correlation_id"] == "job55-comment1"
        assert enqueue_call["envelope"]["event_type"] == "external.comment.received"
        assert enqueue_call["project_id"] == project_id
        assert enqueue_call["job_id"] == job_id
        dispatch_call = mock_dispatch.call_args.kwargs
        assert dispatch_call["account_id"] == DEFAULT_ACCOUNT_ID
        assert dispatch_call["correlation_id"] == "job55-comment1"
        assert dispatch_call["project_id"] == project_id
        assert dispatch_call["job_id"] == job_id

    def test_ingest_reaction_event_rejects_job_outside_project_scope(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": job_id,
            "project_id": project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project.return_value = {
            "id": project_id,
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
        )

        _raises_input_2379_1 = ReactionEventIngestInput(
            event_type="external.comment.received",
            job_id=job_id,
            source="sample_tasks",
            source_id="comment-1",
            event_key="comment.created",
            dedupe_key="job55-comment1",
            payload={"text": "please retry"},
        )
        _raises_input_2379_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, allowed_project_ids=frozenset({"different-project"})
        )
        with pytest.raises(ServiceForbiddenError):
            service.ingest_reaction_event(_raises_input_2379_1, scope=_raises_input_2379_2)

        bl.log_job_reaction_event.assert_not_called()

    def test_ingest_reaction_event_uses_explicit_job_lookup_port(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        reaction_store = MagicMock(
            spec=[
                "get_project",
                "log_job_reaction_event",
                "enqueue_domain_event",
            ]
        )
        reaction_store.get_project.return_value = {
            "id": project_id,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        reaction_store.log_job_reaction_event.return_value = {
            "id": 92,
            "job_id": job_id,
            "workflow_id": "weekly_report",
            "event_type": "external.comment.received",
            "processed": True,
        }
        job_store = MagicMock(spec=["get_job"])
        job_store.get_job.return_value = {
            "id": job_id,
            "project_id": project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }

        service = EventCommandService(
            get_logger=lambda: reaction_store,
            get_job_store=lambda: job_store,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
        )

        payload = service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="external.comment.received",
                job_id=job_id,
                source="sample_tasks",
                source_id="comment-2",
                dedupe_key="job55-comment2",
                payload={"text": "retry"},
            )
        ).dump()

        assert payload["event"]["job_id"] == job_id
        job_store.get_job.assert_called_once_with(job_id)
        assert not hasattr(reaction_store, "get_job")

    def test_ingest_reaction_event_rejects_cross_project_job_id(self):
        """FS-DEEP-CAND-048/039: a job in a different (same-account) project than
        the callback registration project must be rejected before resume."""
        registration_project_id = "22222222-2222-2222-2222-222222222222"
        other_project_id = "99999999-9999-9999-9999-999999999999"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        # Attacker-selected job belongs to a DIFFERENT project (same account).
        bl.get_job.return_value = {
            "id": job_id,
            "project_id": other_project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project.return_value = {
            "id": other_project_id,
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
        )

        _raises_input_2478_1 = ReactionEventIngestInput(
            event_type="notification.interaction_received",
            job_id=job_id,
            source="fixture_events",
            source_id="cb-1",
            event_key="fixture_events.callback_query.approve",
            dedupe_key="job-cb1",
            payload={"approved": True, "action": "approve"},
        )
        _raises_input_2478_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceForbiddenError, match="registration project"):
            service.ingest_reaction_event(
                _raises_input_2478_1,
                scope=_raises_input_2478_2,
                expected_project_id=registration_project_id,
            )

        # Resume sink must NOT be reached for a cross-project job id.
        bl.log_job_reaction_event.assert_not_called()

    def test_ingest_reaction_event_allows_same_project_job_id(self):
        """FS-DEEP-CAND-048/039 positive: a job in the callback registration
        project resumes normally when expected_project_id matches."""
        registration_project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": job_id,
            "project_id": registration_project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project.return_value = {
            "id": registration_project_id,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.log_job_reaction_event.return_value = {
            "id": 91,
            "job_id": job_id,
            "workflow_id": "weekly_report",
            "event_type": "notification.interaction_received",
            "payload": {"approved": True},
            "processed": False,
        }
        bl.mark_job_reaction_event_processed.return_value = {
            "id": 91,
            "processed": True,
        }

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
            evaluate_workflow_reactions=lambda current_bl, event, *, scope=None: [
                {"reaction_key": "comment_resume", "executed": True}
            ],
            reaction_action_result_executed=lambda item: bool(item.get("executed")),
            dispatch_project_notification_event=MagicMock(return_value=[]),
        )

        payload = service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="notification.interaction_received",
                job_id=job_id,
                source="fixture_events",
                source_id="cb-1",
                event_key="fixture_events.callback_query.approve",
                dedupe_key="job-cb1",
                payload={"approved": True, "action": "approve"},
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=registration_project_id,
        ).dump()

        assert payload["reactions"][0]["reaction_key"] == "comment_resume"
        bl.log_job_reaction_event.assert_called_once()

    def test_ingest_reaction_event_dispatches_authoritative_approval_resolution(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": job_id,
            "project_id": project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
        bl.log_job_reaction_event.return_value = {
            "id": 93,
            "job_id": job_id,
            "workflow_id": "weekly_report",
            "event_type": "notification.interaction_received",
            "processed": False,
        }
        bl.mark_job_reaction_event_processed.return_value = {
            "id": 93,
            "job_id": job_id,
            "processed": True,
        }
        dispatch = MagicMock(return_value=[{"status": "delivered"}])
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
            evaluate_workflow_reactions=lambda current_bl, event, *, scope=None: [
                {"status": "resumed", "job_status": "in_progress", "executed": True}
            ],
            reaction_action_result_executed=lambda item: bool(item.get("executed")),
            dispatch_project_notification_event=dispatch,
        )

        result = service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="notification.interaction_received",
                job_id=job_id,
                source="acme.chat",
                source_id="cb-approve-1",
                event_key="chat.callback_query.approve",
                dedupe_key="chat:cb-approve-1",
                payload={
                    "interaction_kind": "approval_decision",
                    "approved": True,
                    "action": "approve",
                    "approval_id": "gate-1",
                    "reply_target": {
                        "provider_key": "acme.chat",
                        "channel_id": "approvals",
                        "external_conversation_ref": "-100123",
                        "external_message_ref": "44",
                    },
                },
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=project_id,
        ).dump()

        assert result["notification_deliveries"] == [{"status": "delivered"}]
        kwargs = dispatch.call_args.kwargs
        assert kwargs["event_type"] == "approval.resolved"
        assert kwargs["ignore_filters"] is True
        assert kwargs["payload"] == {
            "approval_id": "gate-1",
            "action": "approve",
            "accepted": True,
            "applied": True,
            "outcome_code": "applied",
            "reason": "",
            "job_status": "in_progress",
            "next_transition": "continue",
            "comment": "",
            "reply_target": {
                "provider_key": "acme.chat",
                "channel_id": "approvals",
                "external_conversation_ref": "-100123",
                "external_message_ref": "44",
            },
        }
        assert kwargs["direct_routes"][0]["provider_key"] == "acme.chat"
        assert kwargs["direct_routes"][0]["route_config"] == {
            "channel_id": "approvals",
            "required_capability": "human_decision",
        }
        bl.mark_job_reaction_event_processed.assert_called_once_with("93", processed=True)

    def test_ingest_reaction_event_dispatches_terminal_stale_resolution(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": job_id,
            "project_id": project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
        bl.log_job_reaction_event.return_value = {"id": 94, "processed": False}
        bl.mark_job_reaction_event_processed.return_value = {"id": 94, "processed": True}
        dispatch = MagicMock(return_value=[])
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
            evaluate_workflow_reactions=lambda current_bl, event, *, scope=None: [
                {
                    "status": "ignored",
                    "reason": "stale_approval_gate",
                    "outcome_code": "stale",
                    "executed": False,
                }
            ],
            reaction_action_result_executed=lambda item: bool(item.get("executed")),
            dispatch_project_notification_event=dispatch,
        )

        service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="notification.interaction_received",
                job_id=job_id,
                workflow_id="weekly_report",
                source="acme.chat",
                dedupe_key="chat:cb-stale-1",
                payload={
                    "interaction_kind": "approval_decision",
                    "action": "reject",
                    "approved": False,
                    "comment": "Please fix & retry",
                    "approval_id": "gate-old",
                    "reply_target": {
                        "provider_key": "acme.chat",
                        "channel_id": "approvals",
                        "external_conversation_ref": "-100123",
                        "external_message_ref": "44",
                    },
                },
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=project_id,
        )

        payload = dispatch.call_args.kwargs["payload"]
        assert payload["applied"] is False
        assert payload["accepted"] is False
        assert payload["outcome_code"] == "stale"
        assert payload["reason"] == "stale_approval_gate"
        assert payload["comment"] == "Please fix & retry"
        assert payload["next_transition"] == "none"
        bl.mark_job_reaction_event_processed.assert_called_once_with("94", processed=True)

    def test_ingest_reaction_event_does_not_redispatch_processed_interaction(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {"id": job_id, "project_id": project_id, "meta": {}}
        bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
        bl.log_job_reaction_event.return_value = {"id": 95, "processed": True}
        dispatch = MagicMock(return_value=[])
        evaluate = MagicMock(return_value=[])
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: True,
            evaluate_workflow_reactions=evaluate,
            dispatch_project_notification_event=dispatch,
        )

        service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="notification.interaction_received",
                job_id=job_id,
                source="acme.chat",
                payload={
                    "interaction_kind": "approval_decision",
                    "action": "approve",
                    "approval_id": "gate-1",
                },
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=project_id,
        )

        evaluate.assert_not_called()
        dispatch.assert_not_called()

    def test_ingest_reaction_event_leaves_transient_failure_retryable(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {"id": job_id, "project_id": project_id, "meta": {}}
        bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
        bl.log_job_reaction_event.return_value = {"id": 96, "processed": False}
        dispatch = MagicMock(return_value=[])
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: True,
            evaluate_workflow_reactions=lambda current_bl, event, *, scope=None: [
                {"status": "failed", "reason": "database unavailable", "executed": False}
            ],
            reaction_action_result_executed=lambda item: bool(item.get("executed")),
            dispatch_project_notification_event=dispatch,
        )

        service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="notification.interaction_received",
                job_id=job_id,
                source="acme.chat",
                payload={
                    "interaction_kind": "approval_decision",
                    "action": "approve",
                    "approval_id": "gate-1",
                },
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=project_id,
        )

        bl.mark_job_reaction_event_processed.assert_not_called()
        dispatch.assert_not_called()

    def test_ingest_reaction_event_retries_transient_decision_then_resolves_once(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {"id": job_id, "project_id": project_id, "meta": {}}
        bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
        bl.log_job_reaction_event.return_value = {"id": 98, "processed": False}
        bl.mark_job_reaction_event_processed.return_value = {"id": 98, "processed": True}
        evaluate = MagicMock(
            side_effect=[
                [
                    {
                        "status": "ignored",
                        "reason": "checkpointer not configured",
                        "outcome_code": "retryable_error",
                        "executed": False,
                    }
                ],
                [{"status": "resumed", "job_status": "in_progress", "executed": True}],
            ]
        )
        dispatch = MagicMock(return_value=[{"status": "delivered"}])
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: True,
            evaluate_workflow_reactions=evaluate,
            reaction_action_result_executed=lambda item: bool(item.get("executed")),
            dispatch_project_notification_event=dispatch,
        )
        command = ReactionEventIngestInput(
            event_type="notification.interaction_received",
            job_id=job_id,
            source="acme.chat",
            source_id="cb-retry",
            dedupe_key="chat:cb-retry",
            payload={
                "interaction_kind": "approval_decision",
                "action": "approve",
                "approval_id": "gate-1",
                "reply_target": {
                    "channel_id": "approvals",
                    "external_conversation_ref": "-100123",
                    "external_message_ref": "44",
                },
            },
        )

        first = service.ingest_reaction_event(
            command,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=project_id,
        ).dump()
        second = service.ingest_reaction_event(
            command,
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=project_id,
        ).dump()

        assert first["notification_deliveries"] == []
        assert second["notification_deliveries"] == [{"status": "delivered"}]
        assert evaluate.call_count == 2
        bl.mark_job_reaction_event_processed.assert_called_once_with("98", processed=True)
        assert dispatch.call_count == 1

    def test_ingest_reaction_event_preserves_original_dispatch_for_non_approval_interaction(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = MagicMock()
        bl.get_job.return_value = {"id": job_id, "project_id": project_id, "meta": {}}
        bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
        bl.log_job_reaction_event.return_value = {"id": 99, "processed": False}
        dispatch = MagicMock(return_value=[{"status": "delivered"}])
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: True,
            evaluate_workflow_reactions=lambda current_bl, event, *, scope=None: [],
            dispatch_project_notification_event=dispatch,
        )

        result = service.ingest_reaction_event(
            ReactionEventIngestInput(
                event_type="notification.interaction_received",
                job_id=job_id,
                source="acme.fixture",
                payload={"action": "approve"},
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            expected_project_id=project_id,
        ).dump()

        assert result["notification_deliveries"] == [{"status": "delivered"}]
        assert dispatch.call_args.kwargs["event_type"] == "notification.interaction_received"

    def test_ingest_provider_events_binds_reaction_to_registration_project(self):
        """FS-DEEP-CAND-039: provider ingress pins the reaction to the routing/
        registration project via expected_project_id so a cross-project job id
        cannot resume a foreign job."""
        bl = MagicMock()
        bl.get_project.return_value = {"id": 11, "account_id": DEFAULT_ACCOUNT_ID}
        bl.record_domain_event.return_value = {"event_id": "evt-9"}
        bl.get_job.return_value = {
            "id": 55,
            "project_id": "11",
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project_connection.return_value = {
            "connection_config": {
                "channels": [
                    {
                        "channel_id": "ops",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True, "human_decision": True},
                    }
                ]
            }
        }

        reaction_payload = MagicMock()
        reaction_payload.notification_deliveries = []

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            dispatch_project_notification_event=MagicMock(return_value=[]),
        )

        with patch.object(
            service, "ingest_reaction_event", return_value=reaction_payload
        ) as ingest_reaction_event:
            service.ingest_provider_events(
                ProviderEventIngestInput(
                    provider_key="fixture_events",
                    project_id="11",
                    result={},
                    events=[
                        {
                            "event_type": "notification.interaction_received",
                            "source": "fixture_events.callback_query",
                            "payload": {
                                "text": "approve",
                                "approved": True,
                                "action": "approve",
                                "job_id": 55,
                                "source_id": "cb-9",
                                "event_key": "fixture_events.callback_query.approve",
                                "fixture_events": {"chat_id": -100123},
                            },
                        }
                    ],
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="11",
                    actor_id="fixture_events_webhook",
                ),
            )

        assert ingest_reaction_event.call_args.kwargs["expected_project_id"] == "11"

    def test_ingest_provider_events_routes_external_comment_to_reaction_flow(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 11, "account_id": DEFAULT_ACCOUNT_ID}
        bl.record_domain_event.return_value = {"event_id": "evt-1"}
        bl.get_job.return_value = {
            "id": 55,
            "project_id": "11",
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project_connection.return_value = {
            "connection_config": {
                "channels": [
                    {
                        "channel_id": "ops",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True, "human_decision": True},
                    }
                ]
            }
        }

        mock_dispatch = MagicMock(return_value=[])
        reaction_payload = MagicMock()
        reaction_payload.notification_deliveries = [{"delivery_id": 9, "status": "delivered"}]

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            dispatch_project_notification_event=mock_dispatch,
        )

        with patch.object(
            service, "ingest_reaction_event", return_value=reaction_payload
        ) as ingest_reaction_event:
            payload = service.ingest_provider_events(
                ProviderEventIngestInput(
                    provider_key="fixture_events",
                    project_id="11",
                    result={},
                    events=[
                        {
                            "event_type": "external.comment.received",
                            "source": "fixture_events.callback_query",
                            "payload": {
                                "text": "/reject 55 needs fixes",
                                "comment": "needs fixes",
                                "approved": False,
                                "job_id": 55,
                                "source_id": "cb-1",
                                "event_key": "fixture_events.callback_query.reject",
                                "fixture_events": {"chat_id": -100123},
                            },
                        }
                    ],
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="11",
                    actor_id="fixture_events_webhook",
                ),
            ).dump()

        assert payload["event_count"] == 1
        assert payload["event_ids"] == ["evt-1"]
        assert payload["notification_deliveries"][0]["status"] == "delivered"
        mock_dispatch.assert_not_called()
        reaction_request = ingest_reaction_event.call_args.args[0]
        assert reaction_request.event_type == "external.comment.received"
        assert reaction_request.job_id == "55"
        assert reaction_request.source == "fixture_events"
        assert reaction_request.source_id == "cb-1"
        assert reaction_request.payload["approved"] is False

    def test_ingest_provider_events_routes_interaction_to_reaction_flow(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 11, "account_id": DEFAULT_ACCOUNT_ID}
        bl.record_domain_event.return_value = {"event_id": "evt-3"}
        bl.get_job.return_value = {
            "id": 55,
            "project_id": "11",
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project_connection.return_value = {
            "connection_config": {
                "channels": [
                    {
                        "channel_id": "ops",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True, "human_decision": True},
                    }
                ]
            }
        }

        reaction_payload = MagicMock()
        reaction_payload.notification_deliveries = []

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            dispatch_project_notification_event=MagicMock(return_value=[]),
        )

        with patch.object(
            service, "ingest_reaction_event", return_value=reaction_payload
        ) as ingest_reaction_event:
            payload = service.ingest_provider_events(
                ProviderEventIngestInput(
                    provider_key="fixture_events",
                    project_id="11",
                    result={},
                    events=[
                        {
                            "event_type": "notification.interaction_received",
                            "source": "fixture_events.callback_query",
                            "payload": {
                                "text": "approve",
                                "approved": True,
                                "action": "approve",
                                "job_id": 55,
                                "source_id": "cb-2",
                                "event_key": "fixture_events.callback_query.approve",
                                "fixture_events": {"chat_id": -100123},
                            },
                        }
                    ],
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="11",
                    actor_id="fixture_events_webhook",
                ),
            ).dump()

        assert payload["event_count"] == 1
        reaction_request = ingest_reaction_event.call_args.args[0]
        assert reaction_request.event_type == "notification.interaction_received"
        assert reaction_request.job_id == "55"
        assert reaction_request.source == "fixture_events"
        assert reaction_request.source_id == "cb-2"

    def test_ingest_provider_events_dispatches_one_authoritative_resolution_end_to_end(self):
        project_id = "11"
        job_id = "55"
        bl = MagicMock()
        bl.get_project.return_value = {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}
        bl.record_domain_event.return_value = {"event_id": "11111111-1111-4111-8111-111111111111"}
        bl.get_job.return_value = {
            "id": job_id,
            "project_id": project_id,
            "meta": {"selected_workflow_id": "weekly_report"},
        }
        bl.get_project_connection.return_value = {
            "extension_id": "acme.chat",
            "enabled": True,
            "connection_config": {
                "provider_key": "acme.chat",
                "channels": [
                    {
                        "channel_id": "approvals",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"human_decision": True},
                    }
                ],
            },
        }
        bl.log_job_reaction_event.return_value = {
            "id": 97,
            "job_id": job_id,
            "processed": False,
        }
        bl.mark_job_reaction_event_processed.return_value = {
            "id": 97,
            "job_id": job_id,
            "processed": True,
        }
        dispatch = MagicMock(return_value=[{"status": "delivered"}])
        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: value,
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
            evaluate_workflow_reactions=lambda current_bl, event, *, scope=None: [
                {"status": "resumed", "job_status": "in_progress", "executed": True}
            ],
            reaction_action_result_executed=lambda item: bool(item.get("executed")),
            dispatch_project_notification_event=dispatch,
        )

        result = service.ingest_provider_events(
            ProviderEventIngestInput(
                provider_key="acme.chat",
                project_id=project_id,
                events=[
                    {
                        "event_type": "notification.interaction_received",
                        "source": "chat.callback_query",
                        "payload": {
                            "interaction_kind": "approval_decision",
                            "action": "approve",
                            "approved": True,
                            "approval_id": "gate-1",
                            "job_id": job_id,
                            "source_id": "cb-end-to-end",
                            "event_key": "chat.callback_query.approve",
                            "chat": {
                                "channel_id": "approvals",
                                "chat_id": -100123,
                            },
                            "reply_target": {
                                "provider_key": "acme.chat",
                                "channel_id": "approvals",
                                "external_conversation_ref": "-100123",
                                "external_message_ref": "44",
                            },
                        },
                    }
                ],
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                project_id=project_id,
                actor_id="chat_webhook",
            ),
        ).dump()

        assert result["notification_deliveries"] == [{"status": "delivered"}]
        resolution_calls = [
            call
            for call in dispatch.call_args_list
            if call.kwargs["event_type"] == "approval.resolved"
        ]
        assert len(resolution_calls) == 1
        assert resolution_calls[0].kwargs["payload"]["applied"] is True
        assert resolution_calls[0].kwargs["direct_routes"][0]["route_config"] == {
            "channel_id": "approvals",
            "required_capability": "human_decision",
        }

    def test_ingest_provider_events_rejects_unauthorized_fixture_events_decision_chat(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 11, "account_id": DEFAULT_ACCOUNT_ID}
        bl.record_domain_event.return_value = {"event_id": "evt-3"}
        bl.get_project_connection.return_value = {
            "connection_config": {
                "channels": [
                    {
                        "channel_id": "alerts",
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"notify": True, "human_decision": False},
                    }
                ]
            }
        }

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
        )

        _raises_input_3233_1 = ProviderEventIngestInput(
            provider_key="fixture_events",
            project_id="11",
            result={},
            events=[
                {
                    "event_type": "notification.interaction_received",
                    "source": "fixture_events.callback_query",
                    "payload": {
                        "approved": True,
                        "job_id": 55,
                        "source_id": "cb-2",
                        "event_key": "fixture_events.callback_query.approve",
                        "fixture_events": {"chat_id": -100123},
                    },
                }
            ],
        )
        _raises_input_3233_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, project_id="11", actor_id="fixture_events_webhook"
        )
        with pytest.raises(ServiceForbiddenError, match="human decision channel"):
            service.ingest_provider_events(_raises_input_3233_1, scope=_raises_input_3233_2)

        bl.record_domain_event.assert_not_called()

    def test_ingest_provider_events_builds_canonical_dedupe_for_external_comment(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 11, "account_id": DEFAULT_ACCOUNT_ID}
        bl.record_domain_event.return_value = {"event_id": "evt-2"}
        bl.get_job.return_value = {"id": 88, "project_id": "11", "meta": {}}
        bl.get_project_connection.return_value = {
            "connection_config": {
                "channels": [
                    {
                        "chat_id": "-100123",
                        "enabled": True,
                        "capabilities": {"human_decision": True},
                    }
                ]
            }
        }

        reaction_payload = MagicMock()
        reaction_payload.notification_deliveries = []

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            dispatch_project_notification_event=MagicMock(return_value=[]),
        )

        with patch.object(
            service, "ingest_reaction_event", return_value=reaction_payload
        ) as ingest_reaction_event:
            service.ingest_provider_events(
                ProviderEventIngestInput(
                    provider_key="fixture_events",
                    project_id="11",
                    result={},
                    events=[
                        {
                            "event_type": "external.comment.received",
                            "payload": {
                                "payload": {"job_id": 88, "approved": True},
                                "fixture_events": {"chat_id": -100123},
                            },
                        }
                    ],
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="11",
                    actor_id="fixture_events_webhook",
                ),
            )

        reaction_request = ingest_reaction_event.call_args.args[0]
        assert reaction_request.job_id == "88"

    def test_process_extension_response_envelope_routes_project_scoped_events_through_event_service(
        self,
    ):
        invocation = {
            "response": {
                "ok": True,
                "events": [
                    {
                        "event_type": "external.comment.received",
                        "project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                        "workflow_id": "monthly_review",
                        "payload": {
                            "job_id": "6434ac0d-8323-4746-a5f4-3305d42abeb9",
                            "approved": True,
                            "action": "approve",
                            "source_id": "cb-approve-1",
                            "event_key": "fixture_events.callback_query.approve",
                        },
                    }
                ],
            }
        }
        extension = {
            "compiled_manifest": {
                "supported_events": [
                    "external.comment.received",
                    "notification.interaction_received",
                ]
            }
        }

        ingest_provider_events = MagicMock()
        fake_event_service = SimpleNamespace(ingest_provider_events=ingest_provider_events)

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.emit_extension_event",
        ) as emit_extension_event:
            command_services_module._process_extension_response_envelope(
                invocation,
                extension,
                "fixture_events",
                scope=command_services_module.ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="58eeaeb3-a9c1-4c51-b292-14eb86899d86",
                    actor_id="test-actor",
                ),
                audit={"project_id": "58eeaeb3-a9c1-4c51-b292-14eb86899d86"},
                bl=MagicMock(),
                get_logger=lambda: MagicMock(),
                event_command_service_factory=lambda: fake_event_service,
            )

        assert "_envelope" in invocation
        ingest_provider_events.assert_called_once()
        call = ingest_provider_events.call_args
        request = call.args[0]
        assert request.provider_key == "fixture_events"
        assert request.project_id == "58eeaeb3-a9c1-4c51-b292-14eb86899d86"
        assert request.events[0]["event_type"] == "external.comment.received"
        emit_extension_event.assert_not_called()

    @staticmethod
    def _reaction_service(bl):
        return EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
        )

    @staticmethod
    def _reactions_input(event_type: str, reaction_key: str = "group_wait"):
        return WorkflowReactionsUpdateInput.model_validate(
            {
                "reactions": [
                    {
                        "reaction_key": reaction_key,
                        "display_name": "Notify on approval wait",
                        "event_type": event_type,
                        "conditions": {},
                        "action": {"type": "notify"},
                        "debounce_seconds": 0,
                        "enabled": True,
                    }
                ]
            }
        )

    def test_update_workflow_reactions_delegates_target_scope_and_uses_persisted_rows(self):
        bl = MagicMock()
        bl.replace_scoped_workflow_reactions.return_value = (
            "group",
            "design",
            [
                {
                    "reaction_key": "group_wait_2",
                    "display_name": "Persisted",
                    "event_type": "job.approval_waiting",
                    "action": {"type": "notify"},
                    "scope_kind": "group",
                    "scope_id": "design",
                }
            ],
        )
        payload = (
            self._reaction_service(bl)
            .update_workflow_reactions(
                "weekly_report",
                self._reactions_input("job.approval_waiting"),
                scope_kind="group",
                scope_id="design",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, workflow_id="weekly_report"),
            )
            .dump()
        )
        call = bl.replace_scoped_workflow_reactions.call_args
        assert call.args[0] == "weekly_report"
        assert [r["reaction_key"] for r in call.args[1]] == ["group_wait"]
        assert call.kwargs == {
            "scope_kind": "group",
            "scope_id": "design",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.list_workflow_reactions.assert_not_called()
        assert payload["scope_kind"] == "group"
        assert payload["scope_id"] == "design"
        assert payload["reactions"][0]["reaction_key"] == "group_wait_2"
        assert payload["reactions"][0]["display_name"] == "Persisted"

    def test_update_workflow_reactions_rejects_the_retired_phase_scope_and_events(self):
        bl = MagicMock()
        bl.list_workflow_reactions.return_value = []
        bl.replace_scoped_workflow_reactions.return_value = ("workflow", "", [])
        service = self._reaction_service(bl)
        scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, workflow_id="weekly_report")

        approval_reactions = self._reactions_input("job.approval_waiting")
        with pytest.raises(ServiceValidationError, match="phase automation scope was removed"):
            service.update_workflow_reactions(
                "weekly_report",
                approval_reactions,
                scope_kind="phase",
                scope_id="design",
                scope=scope,
            )
        for retired, replacement in (
            ("job.phase_started", "job.group_started"),
            ("job.phase_completed", "job.group_completed"),
            ("job.phase_failed", "job.group_failed"),
            ("job.gate_waiting", "job.approval_waiting"),
            ("job.awaiting_approval", "job.approval_waiting"),
        ):
            retired_reactions = self._reactions_input(retired)
            with pytest.raises(ServiceValidationError, match=replacement):
                service.update_workflow_reactions(
                    "weekly_report",
                    retired_reactions,
                    scope_kind="workflow",
                    scope=scope,
                )
        bl.replace_scoped_workflow_reactions.assert_not_called()

    def test_update_workflow_reactions_offers_each_scope_its_own_events(self):
        bl = MagicMock()
        bl.list_workflow_reactions.return_value = []
        bl.replace_scoped_workflow_reactions.return_value = ("workflow", "", [])
        service = self._reaction_service(bl)
        scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, workflow_id="weekly_report")

        # A terminal job event belongs to the workflow scope only.
        completed_reactions = self._reactions_input("job.completed")
        with pytest.raises(ServiceValidationError, match="not available for step"):
            service.update_workflow_reactions(
                "weekly_report",
                completed_reactions,
                scope_kind="step",
                scope_id="review",
                scope=scope,
            )
        bl.replace_scoped_workflow_reactions.return_value = ("step", "wait_a_day", [])
        payload = service.update_workflow_reactions(
            "weekly_report",
            self._reactions_input("job.timer_waiting"),
            scope_kind="step",
            scope_id="wait_a_day",
            scope=scope,
        ).dump()
        sources = payload["available_trigger_sources"]
        events = {row["event_type"] for row in sources[0]["events"]}
        assert events == {
            "job.step_started",
            "job.step_completed",
            "job.step_failed",
            "job.step_blocked",
            "job.approval_waiting",
            "job.timer_waiting",
        }

    def test_update_workflow_reactions_rejects_extension_sources_without_selected_project(self):
        bl = MagicMock()
        bl.list_workflow_reactions.return_value = []
        bl.replace_scoped_workflow_reactions.return_value = ("workflow", "", [])

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
        )

        _raises_input_3448_1 = WorkflowReactionsUpdateInput.model_validate(
            {
                "reactions": [
                    {
                        "reaction_key": "mail_comment",
                        "display_name": "Mail comment",
                        "event_type": "external.comment.received",
                        "conditions": {"source": "mail_hook"},
                        "action": {"type": "resume_job"},
                        "enabled": True,
                    }
                ]
            }
        )
        _raises_input_3448_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            workflow_id="weekly_report",
            principal_type="service_account",
        )
        with pytest.raises(ServiceValidationError, match="mail_hook"):
            service.update_workflow_reactions(
                "weekly_report", _raises_input_3448_1, scope=_raises_input_3448_2
            )

    def test_update_workflow_reactions_accepts_extension_sources_for_selected_project(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 7, "account_id": DEFAULT_ACCOUNT_ID, "name": "Demo"}
        bl.list_workflow_reactions.return_value = []
        bl.replace_scoped_workflow_reactions.return_value = ("workflow", "", [])
        bl.list_project_connections.return_value = [
            {"connection_id": "conn_mail", "extension_id": "mail_hook", "enabled": True},
        ]
        bl.get_extension.return_value = {
            "extension_key": "mail_hook",
            "display_name": "Mail Hook",
            "enabled": True,
            "compiled_manifest": {"supported_events": ["external.comment.received"]},
        }

        service = EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            to_json_obj=lambda value: value,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
        )

        payload = service.update_workflow_reactions(
            "weekly_report",
            WorkflowReactionsUpdateInput.model_validate(
                {
                    "reactions": [
                        {
                            "reaction_key": "mail_comment",
                            "display_name": "Mail comment",
                            "event_type": "external.comment.received",
                            "conditions": {"source": "mail_hook"},
                            "action": {"type": "resume_job"},
                            "enabled": True,
                        }
                    ]
                }
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                project_id="7",
                workflow_id="weekly_report",
                principal_type="service_account",
            ),
        ).dump()

        assert payload["project_id"] == "7"
        assert payload["available_trigger_sources"][1]["source_id"] == "mail_hook"
        saved = bl.replace_scoped_workflow_reactions.call_args.args
        assert saved[1][0]["conditions"]["source"] == "mail_hook"


class TestExternalAgentCommandService:
    def test_register_returns_normalized_principal_scope_shape(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": EXTERNAL_PROJECT_ID,
            "account_id": "acc_ops",
            "name": "Demo",
        }
        service = ExternalAgentCommandService(get_logger=lambda: bl)

        payload = service.register(
            ExternalAgentRegisterRequestV1(
                agent_name="ext-worker",
                worker_instance_id="worker-1",
                project_id=EXTERNAL_PROJECT_ID,
                workflow_id="wf_demo",
                skills=["review", "review"],
            ),
            principal={
                "principal_type": "service_account",
                "service_account_key": "svc-ext",
                "account_id": "acc_ops",
                "auth_source": "machine",
                "is_active": True,
                "roles": ["operator"],
                "scopes": ["agent:register"],
                "allowed_project_ids": [EXTERNAL_PROJECT_ID],
                "allowed_workflow_ids": ["wf_demo"],
            },
            scope=ScopeContext(
                account_id="acc_ops", actor_id="svc-ext", principal_type="service_account"
            ),
        ).dump()

        assert payload["account_id"] == "acc_ops"
        assert payload["principal_type"] == "service_account"
        assert payload["allowed_project_ids"] == [EXTERNAL_PROJECT_ID]
        assert payload["allowed_workflow_ids"] == ["wf_demo"]
        assert payload["worker_instance_id"] == "worker-1"

    def test_claim_rejects_project_outside_allowed_scope(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": EXTERNAL_PROJECT_ID,
            "account_id": "acc_ops",
            "name": "Demo",
        }
        service = ExternalAgentCommandService(get_logger=lambda: bl)

        _raises_input_3575_1 = ExternalAgentClaimRequestV1(
            worker_instance_id="worker-1", project_id=EXTERNAL_PROJECT_ID
        )
        _raises_input_3575_2 = ScopeContext(
            account_id="acc_ops",
            actor_id="svc-ext",
            principal_type="service_account",
            allowed_project_ids=frozenset({EXTERNAL_PROJECT_ID_DENIED}),
        )
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.claim(
                _raises_input_3575_1,
                principal={
                    "principal_type": "service_account",
                    "service_account_key": "svc-ext",
                    "account_id": "acc_ops",
                    "auth_source": "machine",
                    "is_active": True,
                    "roles": ["operator"],
                    "scopes": ["agent:claim"],
                    "allowed_project_ids": [EXTERNAL_PROJECT_ID_DENIED],
                },
                scope=_raises_input_3575_2,
            )

        bl.acquire_project_execution_lease.assert_not_called()

    def test_report_logs_agent_message_and_finishes_job(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": EXTERNAL_PROJECT_ID,
            "account_id": "acc_ops",
            "name": "Demo",
        }
        bl.get_project_execution_lease.return_value = {
            "project_id": EXTERNAL_PROJECT_ID,
            "worker_instance_id": "worker-1",
            "lease_token": "lease-1",
            "active_job_id": EXTERNAL_JOB_ID,
        }
        bl.get_job.return_value = {
            "id": EXTERNAL_JOB_ID,
            "project_id": EXTERNAL_PROJECT_ID,
            "task_id": "TASK-1",
            "task_summary": "Review task",
            "selected_workflow": "wf_demo",
            "status": "in_progress",
            "meta": {
                "external_agent": {
                    "enabled": True,
                    "worker_instance_id": "worker-1",
                    "claim_token": "claim-1",
                    "claimed_at": "2026-03-24T10:00:00+00:00",
                }
            },
        }
        bl.log_agent_message.return_value = True
        bl.release_project_execution_lease.return_value = True
        terminal_calls: list[dict] = []

        class _TerminalPersistence:
            def complete_success(self, **kwargs):
                terminal_calls.append({"operation": "success", **dict(kwargs)})

        service = ExternalAgentCommandService(
            get_logger=lambda: bl,
            terminal_persistence_factory=lambda _logger: _TerminalPersistence(),
        )

        payload = service.report(
            ExternalAgentReportRequestV1(
                worker_instance_id="worker-1",
                project_id=EXTERNAL_PROJECT_ID,
                job_id=EXTERNAL_JOB_ID,
                lease_token="lease-1",
                claim_token="claim-1",
                agent_name="ext-worker",
                step_id="final_review",
                skill="review",
                status="pass",
                summary="Looks good",
                artifacts={"score": 1},
            ),
            principal={
                "principal_type": "service_account",
                "service_account_key": "svc-ext",
                "account_id": "acc_ops",
                "auth_source": "machine",
                "is_active": True,
                "roles": ["operator"],
                "scopes": ["agent:report"],
                "allowed_project_ids": [EXTERNAL_PROJECT_ID],
                "allowed_workflow_ids": ["wf_demo"],
            },
            scope=ScopeContext(
                account_id="acc_ops",
                actor_id="svc-ext",
                principal_type="service_account",
                allowed_project_ids=frozenset({EXTERNAL_PROJECT_ID}),
                allowed_workflow_ids=frozenset({"wf_demo"}),
            ),
        ).dump()

        assert payload["job_status"] == "completed"
        assert payload["message_published"] is True
        envelope = bl.log_agent_message.call_args.args[1]
        assert envelope["step_id"] == "final_review"
        assert envelope["skill"] == "review"
        assert envelope["status"] == "pass"
        bl.finish_job.assert_not_called()
        assert terminal_calls == [
            {
                "operation": "success",
                "job_id": EXTERNAL_JOB_ID,
                "workflow_outputs": {},
                "final_report": "Looks good",
            }
        ]
        bl.release_project_execution_lease.assert_called_once_with(
            project_id=EXTERNAL_PROJECT_ID,
            worker_instance_id="worker-1",
            lease_token="lease-1",
        )

    def test_report_is_refused_for_another_step_execution_than_claimed(self):
        claimed_execution = "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": EXTERNAL_PROJECT_ID,
            "account_id": "acc_ops",
            "name": "Demo",
        }
        bl.get_project_execution_lease.return_value = {
            "project_id": EXTERNAL_PROJECT_ID,
            "worker_instance_id": "worker-1",
            "lease_token": "lease-1",
        }
        bl.get_job.return_value = {
            "id": EXTERNAL_JOB_ID,
            "project_id": EXTERNAL_PROJECT_ID,
            "selected_workflow": "wf_demo",
            "status": "in_progress",
            "meta": {
                "external_agent": {
                    "worker_instance_id": "worker-1",
                    "claim_token": "claim-1",
                    "step_id": "final_review",
                    "step_execution_id": claimed_execution,
                    "attempt": 1,
                }
            },
        }
        service = ExternalAgentCommandService(get_logger=lambda: bl)

        report_request = ExternalAgentReportRequestV1(
            worker_instance_id="worker-1",
            project_id=EXTERNAL_PROJECT_ID,
            job_id=EXTERNAL_JOB_ID,
            lease_token="lease-1",
            claim_token="claim-1",
            agent_name="ext-worker",
            step_id="final_review",
            step_execution_id="0b0c5f0e-7a51-4c1b-8f8e-2b8c4f9a6d10",
            attempt=1,
            skill="review",
            status="pass",
        )
        scope = ScopeContext(
            account_id="acc_ops",
            actor_id="svc-ext",
            principal_type="service_account",
            allowed_project_ids=frozenset({EXTERNAL_PROJECT_ID}),
            allowed_workflow_ids=frozenset({"wf_demo"}),
        )
        with pytest.raises(ServiceConflictError, match="claimed step execution"):
            service.report(
                report_request,
                principal={
                    "principal_type": "service_account",
                    "service_account_key": "svc-ext",
                    "account_id": "acc_ops",
                    "auth_source": "machine",
                    "is_active": True,
                    "roles": ["operator"],
                    "scopes": ["agent:report"],
                    "allowed_project_ids": [EXTERNAL_PROJECT_ID],
                    "allowed_workflow_ids": ["wf_demo"],
                },
                scope=scope,
            )
        bl.log_agent_message.assert_not_called()

    def test_report_request_requires_execution_and_attempt_together(self):
        with pytest.raises(ValueError, match="step_execution_id and attempt"):
            ExternalAgentReportRequestV1(
                worker_instance_id="worker-1",
                project_id=EXTERNAL_PROJECT_ID,
                job_id=EXTERNAL_JOB_ID,
                lease_token="lease-1",
                claim_token="claim-1",
                agent_name="ext-worker",
                step_id="final_review",
                step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                skill="review",
                status="pass",
            )

    def test_report_action_log_payload_uses_sanitized_fields(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": EXTERNAL_PROJECT_ID,
            "account_id": "acc_ops",
            "name": "Demo",
        }
        bl.get_project_execution_lease.return_value = {
            "project_id": EXTERNAL_PROJECT_ID,
            "worker_instance_id": "worker-1",
            "lease_token": "lease-1",
            "active_job_id": EXTERNAL_JOB_ID,
        }
        bl.get_job.return_value = {
            "id": EXTERNAL_JOB_ID,
            "project_id": EXTERNAL_PROJECT_ID,
            "task_id": "TASK-1",
            "task_summary": "Review task",
            "selected_workflow": "wf_demo",
            "status": "in_progress",
            "meta": {
                "external_agent": {
                    "enabled": True,
                    "worker_instance_id": "worker-1",
                    "claim_token": "claim-1",
                    "claimed_at": "2026-03-24T10:00:00+00:00",
                }
            },
        }
        bl.log_agent_message.return_value = True
        bl.release_project_execution_lease.return_value = True
        service = ExternalAgentCommandService(
            get_logger=lambda: bl,
            terminal_persistence_factory=lambda _logger: MagicMock(),
        )

        def _mock_scan_outbound(text, *args, **kwargs):
            redacted = str(text)
            redacted = redacted.replace("AKIAIOSFODNN7EXAMPLE", "[REDACTED:SECRET]")
            redacted = redacted.replace("john@example.com", "[REDACTED:EMAIL]")
            return SimpleNamespace(redacted_text=redacted)

        with (
            patch(
                "core.infrastructure.security.integration.scan_inbound",
                return_value=SimpleNamespace(redacted_text="summary [REDACTED:SECRET]"),
            ),
            patch(
                "core.infrastructure.security.integration.scan_outbound",
                side_effect=_mock_scan_outbound,
            ),
        ):
            payload = service.report(
                ExternalAgentReportRequestV1(
                    worker_instance_id="worker-1",
                    project_id=EXTERNAL_PROJECT_ID,
                    job_id=EXTERNAL_JOB_ID,
                    lease_token="lease-1",
                    claim_token="claim-1",
                    agent_name="ext-worker",
                    step_id="final_review",
                    skill="review",
                    status="pass",
                    summary="summary AKIAIOSFODNN7EXAMPLE",
                    artifacts={"token": "AKIAIOSFODNN7EXAMPLE"},
                    errors=["contact john@example.com"],
                ),
                principal={
                    "principal_type": "service_account",
                    "service_account_key": "svc-ext",
                    "account_id": "acc_ops",
                    "auth_source": "machine",
                    "is_active": True,
                    "roles": ["operator"],
                    "scopes": ["agent:report"],
                    "allowed_project_ids": [EXTERNAL_PROJECT_ID],
                    "allowed_workflow_ids": ["wf_demo"],
                },
                scope=ScopeContext(
                    account_id="acc_ops",
                    actor_id="svc-ext",
                    principal_type="service_account",
                    allowed_project_ids=frozenset({EXTERNAL_PROJECT_ID}),
                    allowed_workflow_ids=frozenset({"wf_demo"}),
                ),
            ).dump()

        assert payload["job_status"] == "completed"
        assert bl.log_action.called
        logged_payload = bl.log_action.call_args.kwargs["payload"]
        serialized_payload = json.dumps(logged_payload)
        assert "AKIAIOSFODNN7EXAMPLE" not in serialized_payload
        assert "john@example.com" not in serialized_payload
        assert '"lease_token": "[REDACTED]"' in serialized_payload
        assert '"claim_token": "[REDACTED]"' in serialized_payload

        envelope = bl.log_agent_message.call_args.args[1]
        serialized_envelope = json.dumps(envelope)
        assert "AKIAIOSFODNN7EXAMPLE" not in serialized_envelope
        assert "john@example.com" not in serialized_envelope


class TestJobCommandService:
    def test_acknowledge_job_uses_actor_username(self):
        bl = MagicMock()
        bl.get_job.return_value = {"id": 41, "project_id": "1"}
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.acknowledge_job.return_value = True
        service = JobCommandService(get_logger=lambda: bl)

        payload = service.acknowledge_job(41, JobAcknowledgeInput(actor="alice")).dump()

        assert payload["status"] == "acknowledged"
        assert payload["job_id"] == "41"
        bl.acknowledge_job.assert_called_once_with("41", "alice")

    def test_cancel_job_does_not_resume_graph_after_terminal_cancellation(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 12,
            "status": "awaiting_approval",
            "thread_id": "thread-12",
            "project_id": "7",
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        start_thread = MagicMock(return_value=MagicMock(name="cancel-thread"))
        service = JobCommandService(
            get_logger=lambda: bl,
        )

        terminal_service = MagicMock()
        with (
            patch(
                "core.infrastructure.runtime.subprocess_lifecycle.terminalize_running_worker_subprocesses",
                return_value=1,
            ) as terminalize,
            patch(
                "core.application.jobs.terminal_persistence.TerminalPersistenceService",
                return_value=terminal_service,
            ),
        ):
            payload = service.cancel_job(12, actor="alice").dump()

        assert payload["status"] == "cancelled"
        assert payload["project_id"] == "7"
        assert payload["thread_id"] == "thread-12"
        terminal_service.complete_failure.assert_called_once_with(
            job_id="12",
            status="cancelled",
            error_message="Cancelled by user",
            error_code="cancelled",
            category="cancellation",
            final_report="Cancelled by user",
        )
        terminalize.assert_called_once()
        bl.update_job_runtime.assert_called_once()
        assert bl.update_job_runtime.call_args.args[0] == "12"
        assert bl.update_job_runtime.call_args.kwargs["current_agent"] == ""
        assert bl.update_job_runtime.call_args.kwargs["current_step_id"] == ""
        start_thread.assert_not_called()

    def test_cancel_job_is_possible_when_its_project_was_deleted(self):
        """A job whose project is gone must stay recoverable.

        Refusing every job command with "Project not found" left a dead-lettered
        job — one that is dead-lettered precisely because its queue payload
        references a deleted project — with no way out: acknowledge, restart and
        cancel all answered 404, and /health/ready stayed 503 forever with a
        remediation the operator could not perform.

        Authorization is not weakened: the job's own account is checked instead,
        and require_account_access fails closed on a missing, empty or foreign
        account id. See the cross-account case below.
        """
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 12,
            "status": "awaiting_approval",
            "thread_id": "thread-12",
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project.return_value = None
        service = JobCommandService(
            get_logger=lambda: bl,
        )

        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="alice", principal_type="admin"
        )
        resolved = service._require_job_in_scope(bl, "12", scope)

        assert resolved["id"] == 12, "scoping refused a job whose project was deleted"

    def test_orphaned_job_still_refuses_a_caller_from_another_account(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 12,
            "status": "awaiting_approval",
            "thread_id": "thread-12",
            "project_id": "7",
            "account_id": "00000000-0000-0000-0000-0000000000ff",
        }
        bl.get_project.return_value = None
        service = JobCommandService(
            get_logger=lambda: bl,
        )

        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="alice", principal_type="admin"
        )
        with pytest.raises(ServiceForbiddenError):
            service._require_job_in_scope(bl, "12", scope)

        bl.finish_job.assert_not_called()

    def test_restart_job_reenqueues_with_rerun_context(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 15,
            "task_id": "task-15",
            "task_summary": "Fix flaky flow",
            "task_source": "manual",
            "project_id": "3",
            "mode": "brownfield",
            "meta": {"replay_snapshot": {"artifact_id": "artifact-a"}},
        }
        bl.get_project.return_value = {"id": 3, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_run_config.return_value = {
            "project_name": "alpha",
        }
        get_project_run_config = MagicMock(
            return_value={
                "project_name": "alpha",
            }
        )
        enqueue_task = MagicMock(return_value=88)
        replay_snapshot = {
            "workflow_id": "wf-1",
            "inputs": {"message": "original"},
            "files": [{"artifact_id": "upload-1", "sha256": "abc"}],
            "test_mode": True,
        }
        load_replay_snapshot = MagicMock(return_value=replay_snapshot)
        build_rerun_workflow_selection = MagicMock(
            return_value=({"workflow_id": "wf-1"}, {"source_job_id": 15})
        )
        service = JobCommandService(
            get_logger=lambda: bl,
            build_rerun_workflow_selection=build_rerun_workflow_selection,
            load_replay_snapshot=load_replay_snapshot,
            enqueue_task=enqueue_task,
            get_project_run_config=get_project_run_config,
        )

        payload = service.restart_job(15).dump()

        assert payload["status"] == "pending"
        assert payload["new_job_id"] == "88"
        assert payload["enqueued"] is True
        assert enqueue_task.call_args.kwargs["project_name"] == "alpha"
        assert enqueue_task.call_args.kwargs["project_config"] == {"project_name": "alpha"}
        assert enqueue_task.call_args.kwargs["extra_state"] == {"workflow_id": "wf-1"}
        assert enqueue_task.call_args.kwargs["meta"] == {"source_job_id": 15}
        assert enqueue_task.call_args.kwargs["replay_snapshot"] == replay_snapshot
        assert enqueue_task.call_args.kwargs["test_mode"] is True
        build_rerun_workflow_selection.assert_called_once_with(
            bl.get_job.return_value,
            replay_snapshot=replay_snapshot,
        )
        get_project_run_config.assert_called_once_with("3")
        bl.get_project_run_config.assert_not_called()

    def test_restart_job_rejects_legacy_meta_without_replay_snapshot(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 15,
            "task_id": "task-15",
            "task_summary": "Secret workflow",
            "project_id": "3",
            "meta": {
                "execution_envelope": {"inputs": {"token": "legacy-secret"}},
                "task_envelope": {"inputs": {"token": "legacy-secret"}},
                "mapped_inputs": {"token": "legacy-secret"},
            },
        }
        bl.get_project.return_value = {
            "id": 3,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        service = JobCommandService(
            get_logger=lambda: bl,
            build_rerun_workflow_selection=MagicMock(),
            load_replay_snapshot=MagicMock(),
            enqueue_task=MagicMock(),
            get_project_run_config=MagicMock(return_value={"project_name": "alpha"}),
        )

        with pytest.raises(ServiceValidationError, match=r"job\.replay_snapshot_absent") as exc:
            service.restart_job(15)

        assert exc.value.payload["code"] == "job.replay_snapshot_absent"
        service._load_replay_snapshot.assert_not_called()
        service._build_rerun_workflow_selection.assert_not_called()
        service._enqueue_task.assert_not_called()

    @pytest.mark.parametrize(
        "code",
        [
            "job.replay_snapshot_partial",
            "job.replay_snapshot_expired",
            "job.replay_snapshot_scope_mismatch",
            "job.replay_snapshot_digest_mismatch",
        ],
    )
    def test_restart_job_preserves_stable_replay_snapshot_error_code(self, code):
        from core.application.jobs.job_invocation_artifacts import JobReplaySnapshotError

        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 15,
            "task_id": "task-15",
            "task_summary": "Retry exact launch",
            "project_id": "3",
            "meta": {"replay_snapshot": {"artifact_id": "artifact-a"}},
        }
        bl.get_project.return_value = {
            "id": 3,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        enqueue_task = MagicMock()
        service = JobCommandService(
            get_logger=lambda: bl,
            build_rerun_workflow_selection=MagicMock(),
            load_replay_snapshot=MagicMock(side_effect=JobReplaySnapshotError(code)),
            enqueue_task=enqueue_task,
            get_project_run_config=MagicMock(return_value={"project_name": "alpha"}),
        )

        with pytest.raises(ServiceValidationError, match=code) as exc:
            service.restart_job(15)

        assert exc.value.payload == {"error": code, "code": code}
        enqueue_task.assert_not_called()

    def test_launch_snapshot_detail_and_retry_keep_original_exact_inputs(self, monkeypatch):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.jobs.job_invocation_artifacts import (
            build_effective_input_projection,
            capture_replay_snapshot,
            load_replay_snapshot,
        )
        from core.application.management.query_services import JobQueryService
        from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
        from core.application.workflows.serve_workflow_helpers import (
            build_rerun_workflow_selection,
        )
        from core.infrastructure.runtime.test_mode import parse_test_mode
        from core.infrastructure.security import secret_codec
        from core.infrastructure.security.secret_reference import (
            SecretReference,
            inline_secret_ref_fingerprint,
        )

        monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "retry-test-key"})
        plane = ArtifactDatasetPlane()
        configured_fingerprint = secret_ref_fingerprint("workflow/wf-original/api_key/v1")
        inline_fingerprint = inline_secret_ref_fingerprint(
            SecretReference(name="ephemeral_token", ciphertext="encrypted-inline-token")
        )
        exact_snapshot = {
            "workflow_id": "wf-original",
            "inputs": {
                "api_key": "launch-secret",  # pragma: allowlist secret - replay fixture
                "count": 7,
            },
            "files": [],
            "test_mode": True,
            "input_contract": "direct_inputs",
            "task_envelope": {
                "inputs": {
                    "api_key": "launch-secret",  # pragma: allowlist secret - replay fixture
                    "count": 7,
                },
                "files": [],
            },
            "_workflow_secret_ref_fingerprints": sorted(
                [configured_fingerprint, inline_fingerprint]
            ),
        }
        pointer = capture_replay_snapshot(
            artifact_plane=plane,
            project_id="3",
            job_id="15",
            snapshot=exact_snapshot,
        )
        job = {
            "id": 15,
            "task_id": "task-15",
            "task_summary": "Retry original launch",
            "task_source": "manual",
            "project_id": "3",
            "status": "failed",
            "meta": {
                "replay_snapshot": pointer,
                "invocation": build_effective_input_projection(
                    inputs=exact_snapshot["inputs"],
                    declared_secret_inputs={"api_key"},
                ),
                "execution_envelope": {
                    "inputs": {
                        "api_key": "changed-meta",  # pragma: allowlist secret - replay fixture
                        "count": 99,
                    }
                },
            },
        }
        bl = MagicMock()
        bl.get_job.return_value = job
        bl.get_project.return_value = {
            "id": 3,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_job_cost_summary.return_value = {}
        bl.get_project_workflows.return_value = []
        query = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: str(status),
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        detail = asyncio.run(query.get_job_status("15")).dump()

        assert detail["invocation"]["mapped_inputs"]["api_key"] == "[REDACTED]"
        assert "launch-secret" not in json.dumps(detail["invocation"])

        enqueue_task = MagicMock(return_value=88)
        service = JobCommandService(
            get_logger=lambda: bl,
            build_rerun_workflow_selection=lambda current_job, *, replay_snapshot: (
                build_rerun_workflow_selection(
                    current_job,
                    replay_snapshot=replay_snapshot,
                    to_json_obj=lambda value: value,
                    parse_test_mode=parse_test_mode,
                )
            ),
            load_replay_snapshot=lambda **kwargs: load_replay_snapshot(
                artifact_plane=plane,
                **kwargs,
            ),
            enqueue_task=enqueue_task,
            get_project_run_config=MagicMock(return_value={"project_name": "alpha"}),
        )

        service.restart_job("15")

        assert enqueue_task.call_args.kwargs["extra_state"]["inputs"] == {
            "api_key": "launch-secret",  # pragma: allowlist secret - replay fixture
            "count": 7,
        }
        assert enqueue_task.call_args.kwargs["extra_state"]["workflow_id"] == "wf-original"
        assert enqueue_task.call_args.kwargs["extra_state"][
            "_workflow_secret_ref_fingerprints"
        ] == sorted([configured_fingerprint, inline_fingerprint])
        assert enqueue_task.call_args.kwargs["test_mode"] is True
        assert enqueue_task.call_args.kwargs["replay_snapshot"] == exact_snapshot

    def test_bulk_delete_project_jobs_reports_deleted_and_skipped(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 9,
            "name": "project-9",
        }
        bl.get_job.side_effect = [
            {
                "id": 1,
                "project_id": "9",
                "status": "completed",
                "finished_at": "2026-04-11T08:00:00Z",
            },
            {
                "id": 2,
                "project_id": "10",
                "status": "completed",
                "finished_at": "2026-04-11T08:00:00Z",
            },
            None,
        ]
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        bl.conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        bl.conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        service = JobCommandService(get_logger=lambda: bl)

        payload = service.bulk_delete_project_jobs(
            ProjectJobsBulkDeleteInput(project_id="9", ids=["1", "2", "3"], confirm="DELETE")
        ).dump()

        assert payload["requested"] == ["1", "2", "3"]
        assert payload["deleted"] == ["1"]
        assert payload["deleted_count"] == 1
        assert payload["skipped"] == [
            {"job_id": "2", "reason": "wrong_project"},
            {"job_id": "3", "reason": "not_found"},
        ]
        # Verify soft-delete, not hard delete
        bl.delete_job.assert_not_called()

    def test_delete_job_raises_conflict_for_active_rows(self):
        bl = MagicMock()
        bl.get_job.return_value = {"id": 22, "status": "in_progress", "project_id": "1"}
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = JobCommandService(get_logger=lambda: bl)

        with pytest.raises(ServiceConflictError) as exc:
            service.delete_job(22)

        assert "active" in str(exc.value).lower()

    def test_delete_job_allows_terminal_job_still_quiescing(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 22,
            "status": "cancelled",
            "finished_at": None,
            "project_id": "1",
        }
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        bl.conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        bl.conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        service = JobCommandService(get_logger=lambda: bl)

        payload = service.delete_job(22).dump()
        assert payload["job_id"] == "22"
        mock_cur.execute.assert_called_once()

    def test_bulk_delete_project_jobs_deletes_quiescing_rows(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 9,
            "name": "project-9",
        }
        bl.get_job.side_effect = [
            {"id": 1, "project_id": "9", "status": "cancelled", "finished_at": None},
            {
                "id": 2,
                "project_id": "9",
                "status": "completed",
                "finished_at": "2026-04-11T08:00:00Z",
            },
        ]
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        bl.conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        bl.conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        service = JobCommandService(get_logger=lambda: bl)

        payload = service.bulk_delete_project_jobs(
            ProjectJobsBulkDeleteInput(project_id="9", ids=["1", "2"], confirm="DELETE")
        ).dump()

        assert payload["deleted"] == ["1", "2"]
        assert payload["skipped"] == []
        assert mock_cur.execute.call_count == 2

    def test_create_decision_override_rejects_workflow_selection_domain(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 78,
            "project_id": "1",
            "task_summary": "Build export endpoint",
            "thread_id": "thread-78",
            "meta": {},
        }
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "project_workflows": [{"workflow_id": "weekly_digest", "enabled": True}],
        }
        bl.create_job_decision_override.return_value = {
            "id": 12,
            "job_id": 78,
            "domain": "workflow_selection",
            "original_value": {},
            "override_value": {"workflow_id": "weekly_digest"},
            "reason": "Force quick fix",
            "created_by": "tester",
            "active": True,
        }
        service = JobCommandService(
            get_logger=lambda: bl,
            get_thread_state_values=lambda thread_id: {},
            runtime_step_scope_id=lambda runtime_state: "",
            latest_runtime_message_id=lambda runtime_state, step_execution_id="": None,
            override_workflow_id_from_value=lambda value: str(
                (value or {}).get("workflow_id") or ""
            ),
            is_empty_original_value=lambda value: value in (None, "", {}),
            derive_override_original_value=lambda **kwargs: {"workflow_id": None},
            validate_explicit_workflow_override=lambda **kwargs: None,
            to_json_obj=lambda value: value,
            serialize_decision_override_row=lambda row: {
                "id": row["id"],
                "domain": row["domain"],
                "override_value": row["override_value"],
            },
        )

        _raises_input_4323_1 = JobDecisionOverrideInput(
            domain="workflow_selection",
            override_value={"workflow_id": "weekly_digest"},
            reason="Force quick fix",
            created_by="tester",
        )
        with pytest.raises(
            ServiceValidationError,
            match="workflow_selection decision overrides are no longer supported",
        ):
            service.create_decision_override(78, _raises_input_4323_1)

        bl.create_job_decision_override.assert_not_called()
        bl.update_job.assert_not_called()
        bl.update_job_runtime.assert_not_called()

    def test_create_decision_override_rejects_verdict_specific_domains(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 79,
            "project_id": "1",
            "task_summary": "Build export endpoint",
            "thread_id": "thread-79",
            "meta": {},
        }
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_run_config.return_value = {"project_name": "Demo"}
        service = JobCommandService(
            get_logger=lambda: bl,
            get_thread_state_values=lambda thread_id: {},
            runtime_step_scope_id=lambda runtime_state: "",
            latest_runtime_message_id=lambda runtime_state, step_execution_id="": None,
            override_workflow_id_from_value=lambda value: str(
                (value or {}).get("workflow_id") or ""
            ),
            is_empty_original_value=lambda value: value in (None, "", {}),
            derive_override_original_value=lambda **kwargs: {},
            validate_explicit_workflow_override=lambda **kwargs: None,
            to_json_obj=lambda value: value,
            serialize_decision_override_row=lambda row: row,
        )

        for domain in ("review_verdict", "approval_verdict"):
            _raises_input_4368_1 = JobDecisionOverrideInput(
                domain=domain,
                override_value={domain: "pass"},
                reason="Retired override",
                created_by="tester",
            )
            with pytest.raises(
                ServiceValidationError,
                match=rf"{domain} decision overrides are no longer supported",
            ):
                service.create_decision_override(79, _raises_input_4368_1)

        bl.create_job_decision_override.assert_not_called()

    def test_execute_step_replay_rejects_prompt_reinvoke_without_prompt_text(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {"id": 42, "project_id": "1", "meta": {}}
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = JobCommandService(
            get_logger=lambda: bl,
            to_json_obj=lambda value: value,
            resolve_replay_message_row=lambda current_bl, **kwargs: (
                {
                    "id": 900,
                    "job_id": 42,
                    "correlation_id": "corr-900",
                    "from_agent": "writer",
                    "skill": "draft_summary",
                    "step_id": "implementation",
                },
                None,
                200,
            ),
            collect_replay_step_trace=lambda current_bl, job_id, row: {
                "message": {
                    "id": 900,
                    "correlation_id": "corr-900",
                    "from_agent": "writer",
                    "skill": "draft_summary",
                    "step_id": "implementation",
                    "artifacts": {"raw_output": "original output"},
                },
                "prompt_request": {
                    "payload": {},
                    "meta": {"provider": "openai", "skill": "draft_summary"},
                    "model_used": "gpt-4o-mini",
                },
            },
            extract_replay_prompt_capture=lambda trace_payload: {
                "prompt_version": "v1",
                "provider": "openai",
                "system_prompt": "",
                "user_prompt": "",
                "skill": "draft_summary",
                "step_id": "implementation",
                "model_used": "gpt-4o-mini",
            },
            derive_original_replay_outcome=lambda trace_payload: {
                "message_status": "success",
                "message_summary": "implemented",
                "response_text": "original output",
                "response_excerpt": "original output",
            },
            serialize_job_step_replay_row=lambda row: {
                "id": row["id"],
                "status": row["status"],
            },
        )
        bl.create_job_step_replay.return_value = {
            "id": 55,
            "status": "failed",
            "created_at": now,
        }

        _raises_input_4472_1 = JobStepReplayExecutionInput(
            message_id=900, replay_mode="prompt_reinvoke"
        )
        with pytest.raises(ServiceValidationError) as exc:
            service.execute_step_replay(42, _raises_input_4472_1)

        assert "missing system/user prompt text" in str(exc.value)
        assert exc.value.payload["replay"]["status"] == "failed"

    def test_execute_step_replay_persists_successful_run(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "1",
            "meta": {"project_config_snapshot": {"project_name": "snap-demo"}},
        }
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        created_row = {"id": 55, "status": "running", "created_at": now}
        updated_row = {"id": 55, "status": "succeeded", "finished_at": now}
        bl.create_job_step_replay.return_value = created_row
        bl.update_job_step_replay.return_value = updated_row

        service = JobCommandService(
            get_logger=lambda: bl,
            to_json_obj=lambda value: value,
            resolve_replay_message_row=lambda current_bl, **kwargs: (
                {
                    "id": 901,
                    "job_id": 42,
                    "correlation_id": "corr-901",
                    "from_agent": "writer",
                    "skill": "draft_summary",
                    "step_id": "implementation",
                },
                None,
                200,
            ),
            collect_replay_step_trace=lambda current_bl, job_id, row: {
                "message": {
                    "id": 901,
                    "correlation_id": "corr-901",
                    "from_agent": "writer",
                    "skill": "draft_summary",
                    "step_id": "implementation",
                    "artifacts": {"raw_output": "original output"},
                },
            },
            extract_replay_prompt_capture=lambda trace_payload: {
                "prompt_version": "v1",
                "provider": "openai",
                "system_prompt": "You are developer",
                "user_prompt": "Implement change",
                "skill": "draft_summary",
                "step_id": "implementation",
                "model_used": "gpt-4o-mini",
            },
            derive_original_replay_outcome=lambda trace_payload: {
                "message_status": "success",
                "message_summary": "implemented",
                "response_text": "original output",
                "response_excerpt": "original output",
            },
            invoke_step_replay_llm=lambda **kwargs: {
                "response_text": "replayed output",
                "response_excerpt": "replayed output",
                "replay_message": {
                    "artifacts": {"summary": "visible"},
                    "output_contract": {"schema_version": "agent_output_contract_v1"},
                },
            },
            build_replay_diff_excerpt=lambda original_text, replayed_text: "---diff---",
            serialize_job_step_replay_row=lambda row: {"id": row["id"], "status": row["status"]},
        )

        payload = service.execute_step_replay(
            42,
            JobStepReplayExecutionInput(
                message_id=901, requested_by="alice", replay_mode="prompt_reinvoke"
            ),
        ).dump()

        assert payload["replay_id"] == "55"
        assert payload["replay_outcome"]["response_text"] == "replayed output"
        assert bl.create_job_step_replay.call_args.kwargs["requested_by"] == "alice"
        assert bl.update_job_step_replay.call_args.kwargs["status"] == "succeeded"

        assert payload["replay_outcome"]["replay_message"] == {"artifacts": {"summary": "visible"}}
        assert bl.update_job_step_replay.call_args.kwargs["replay_outcome"]["replay_message"][
            "output_contract"
        ] == {"schema_version": "agent_output_contract_v1"}

    def test_retry_interrupted_step_requeues_original_job(self):
        from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
        from core.application.workflows.serve_workflow_helpers import (
            build_rerun_workflow_selection,
        )
        from core.infrastructure.runtime.test_mode import parse_test_mode
        from core.infrastructure.security.secret_reference import (
            SecretReference,
            inline_secret_ref_fingerprint,
        )

        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "1",
            "status": "retryable_interrupted_step",
            "thread_id": "thread-42",
            "task_id": "TASK-42",
            "task_summary": "Recover interrupted step",
            "task_source": "manual",
            "mode": "brownfield",
            "meta": {
                "replay_snapshot": {"artifact_id": "artifact-a"},
                "retryable_interrupted_step": {
                    "detected_at": now.isoformat(),
                    "agent_id": "writer",
                    "skill": "draft_summary",
                    "step_id": "implementation",
                },
                "project_config_snapshot": {"project_name": "snap-demo"},
            },
        }
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_run_config.return_value = {
            "project_name": "snap-demo",
        }
        enqueue_task_in_queue = MagicMock()
        append_runtime_journal = MagicMock(return_value={"event": "manual_retry_step"})
        fingerprints = sorted(
            [
                secret_ref_fingerprint("workflow/wf-1/configured/v1"),
                inline_secret_ref_fingerprint(
                    SecretReference(name="inline", ciphertext="encrypted-inline")
                ),
            ]
        )
        replay_snapshot = {
            "workflow_id": "wf-1",
            "inputs": {"message": "original"},
            "files": [],
            "test_mode": False,
            "input_contract": "direct_inputs",
            "task_envelope": {"inputs": {"message": "original"}, "files": []},
            "_workflow_secret_ref_fingerprints": fingerprints,
        }
        service = JobCommandService(
            get_logger=lambda: bl,
            build_rerun_workflow_selection=lambda job, *, replay_snapshot: (
                build_rerun_workflow_selection(
                    job,
                    replay_snapshot=replay_snapshot,
                    to_json_obj=lambda value: value,
                    parse_test_mode=parse_test_mode,
                )
            ),
            load_replay_snapshot=MagicMock(return_value=replay_snapshot),
            enqueue_task_in_queue=enqueue_task_in_queue,
            to_json_obj=lambda value: value,
            sanitize_retryable_interrupted_step_snapshot=lambda value: value,
            resolve_job_project_name=lambda current_bl, job: "snap-demo",
            append_runtime_journal=append_runtime_journal,
        )

        payload = service.retry_interrupted_step(
            42,
            JobRetryInterruptedStepInput(requested_by="alice"),
        ).dump()

        assert payload["status"] == "pending"
        assert payload["enqueued"] is True
        assert payload["thread_id"] == "thread-42"
        assert payload["queue_name"] == "platform.project.1"
        assert (
            enqueue_task_in_queue.call_args.kwargs["extra_state"]["retry_interrupted_step"] is True
        )
        assert (
            enqueue_task_in_queue.call_args.kwargs["extra_state"][
                "_workflow_secret_ref_fingerprints"
            ]
            == fingerprints
        )
        append_runtime_journal.assert_called_once()

    def test_retry_interrupted_step_rejects_missing_replay_snapshot(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "1",
            "status": "retryable_interrupted_step",
            "thread_id": "thread-42",
            "meta": {
                "retryable_interrupted_step": {
                    "detected_at": now.isoformat(),
                    "agent_id": "writer",
                    "skill": "draft_summary",
                    "step_id": "implementation",
                }
            },
        }
        bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        enqueue_task_in_queue = MagicMock()
        service = JobCommandService(
            get_logger=lambda: bl,
            build_rerun_workflow_selection=MagicMock(),
            load_replay_snapshot=MagicMock(),
            enqueue_task_in_queue=enqueue_task_in_queue,
            to_json_obj=lambda value: value,
            sanitize_retryable_interrupted_step_snapshot=lambda value: value,
            resolve_job_project_name=lambda current_bl, job: "demo",
        )

        _raises_input_4683_1 = JobRetryInterruptedStepInput(requested_by="alice")
        with pytest.raises(ServiceValidationError, match=r"job\.replay_snapshot_absent") as exc:
            service.retry_interrupted_step(42, _raises_input_4683_1)

        assert exc.value.payload["code"] == "job.replay_snapshot_absent"
        service._load_replay_snapshot.assert_not_called()
        enqueue_task_in_queue.assert_not_called()


class TestExtensionCommandServices:
    @pytest.fixture(autouse=True)
    def _isolate_extension_lifecycle_coordinator(self, tmp_path: Path):
        """Keep command-service tests focused on their existing lifecycle concerns."""

        from core.application.extensions.binding_context import ExtensionLifecycleContext

        (tmp_path / "extensions").mkdir()
        original_query_service = ExtensionLifecycleContext._extension_query_service

        def query_service(context, bl, *, install_store=None, runtime_store=None):
            if context._extension_query_service_factory is not None:
                return original_query_service(
                    context,
                    bl,
                    install_store=install_store,
                    runtime_store=runtime_store,
                )
            from core.application.extensions.query_service import ExtensionQueryService

            return ExtensionQueryService(
                get_logger=lambda: bl,
                get_install_store=lambda: (
                    install_store
                    if install_store is not None
                    else context._extension_install_store(bl)
                ),
                get_runtime_store=lambda: (
                    runtime_store
                    if runtime_store is not None
                    else context._extension_runtime_store(bl)
                ),
            )

        def sync_scan(logger, scanned):
            logger.sync_extension_scan(scanned)
            return list(scanned or [])

        def sync_enabled(logger, extension_key, *, version, enabled, config):
            return logger.enable_extension(
                extension_key,
                version=version,
                enabled=enabled,
                config=config,
            )

        with (
            patch(
                "core.application.extensions.extension_scan_lifecycle."
                "sync_extension_scan_with_llm_lifecycle",
                side_effect=sync_scan,
            ),
            patch(
                "core.application.extensions.extension_scan_lifecycle."
                "sync_enabled_extension_with_llm_lifecycle",
                side_effect=sync_enabled,
            ),
            patch.object(
                ExtensionLifecycleContext,
                "_extension_query_service",
                query_service,
            ),
        ):
            yield

    @staticmethod
    def _elf64(machine: int) -> bytes:
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        header[16:18] = (2).to_bytes(2, "little")
        header[18:20] = machine.to_bytes(2, "little")
        header[20:24] = (1).to_bytes(4, "little")
        return bytes(header)

    @staticmethod
    def _synthetic_wheel_bytes(
        *,
        name: str = "fsdemo",
        version: str = "1.2.3",
    ) -> tuple[str, bytes, str]:
        buffer = io.BytesIO()
        dist_info = f"{name}-{version}.dist-info"
        wheel_name = f"{name}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                f"{name}/__init__.py",
                f"VALUE = 'from-installed-wheel'\n__version__ = '{version}'\n",
            )
            archive.writestr(
                f"{dist_info}/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
            archive.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: flow-steward-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(f"{dist_info}/RECORD", "")
        content = buffer.getvalue()
        return wheel_name, content, "sha256:" + hashlib.sha256(content).hexdigest()

    @staticmethod
    def _managed_scan_rows(extensions_root: Path, extension_id: str) -> list[dict[str, Any]]:
        import yaml

        bundle_root = extensions_root / extension_folder_name(extension_id)
        manifest_path = bundle_root / "extension.yaml"
        if not manifest_path.is_file():
            return []
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        return [
            {
                "extension_key": extension_id,
                "enabled": True,
                "status": "valid",
                "version": str(manifest["version"]),
                "bundle_root": str(bundle_root),
                "compiled_manifest": {"callbacks": []},
            }
        ]

    @staticmethod
    def _persist_scanned_extension_rows(bl: MagicMock) -> None:
        """Mirror the DB scan projection used by lifecycle command tests."""
        persisted: dict[str, Any] = {}

        def sync_extension_scan(scanned: list[dict[str, Any]]) -> None:
            persisted.clear()
            if scanned:
                persisted.update(dict(scanned[0]))

        def enable_extension(
            _extension_id: str,
            *,
            version: str | None,
            enabled: bool,
            config: dict[str, Any],
        ) -> dict[str, Any]:
            persisted.update(
                enabled=enabled,
                state="active" if enabled else "disabled",
                config=dict(config or {}),
            )
            if version:
                persisted["version"] = version
            return dict(persisted)

        bl.sync_extension_scan.side_effect = sync_extension_scan
        bl.enable_extension.side_effect = enable_extension
        bl.get_extension.side_effect = lambda _extension_id: dict(persisted) if persisted else None

    @staticmethod
    def _confirmed_uninstall_plan(
        extension_id: str,
        *,
        revision: str = "confirmed-revision",
        install_id: str = "",
        release_id: str = "",
    ) -> dict[str, Any]:
        install_suffix = extension_folder_name(extension_id)
        return {
            "extension_id": extension_id,
            "install_id": install_id or f"install_{install_suffix}",
            "release_id": release_id or f"release_{install_suffix}",
            "revision": revision,
            "remove": [],
            "affected": [],
        }

    @staticmethod
    def _confirm_database_advisory_lock(bl: MagicMock) -> None:
        cursor = bl.conn.cursor.return_value.__enter__.return_value
        fallback = cursor.fetchone.return_value

        def fetchone():
            call = cursor.execute.call_args
            statement = str(call.args[0]) if call and call.args else ""
            if "pg_try_advisory_lock" in statement or "pg_advisory_unlock" in statement:
                return (True,)
            return fallback

        cursor.fetchone.side_effect = fetchone

    @staticmethod
    def _extension_zip_bytes(
        *,
        extension_id: str = "acme.vendor-demo",
        author: str = "Acme",
        version: str = "1.0.0",
        python_requirements: list[dict[str, Any]] | None = None,
        wheels: dict[str, bytes] | None = None,
    ) -> bytes:
        buffer = io.BytesIO()
        bundle_dir = extension_folder_name(extension_id)
        dependency_lines: list[str] = []
        if python_requirements is not None:
            dependency_lines.append("python_requirements:")
            for requirement in python_requirements:
                dependency_lines.extend(
                    [
                        f"  - name: {requirement['name']}",
                        f'    version: "{requirement["version"]}"',
                        "    hashes:",
                    ]
                )
                for digest in requirement.get("hashes") or []:
                    dependency_lines.append(f"      - {digest}")
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                f"{bundle_dir}/extension.yaml",
                "\n".join(
                    [
                        "manifest_version: 2",
                        "runtime: {contract_version: extension_host_v2}",
                        "lifecycle: {data_schema_version: 1, data_migrations: []}",
                        f"extension_id: {extension_id}",
                        "kind: tool_provider",
                        f"version: {version}",
                        "display_name: Vendor Demo",
                        "description: Test-only vendor demo extension.",
                        f"author: {author}",
                        "entrypoint:",
                        "  mode: subprocess",
                        "  command:",
                        "    - python3",
                        "    - main.py",
                        "health:",
                        "  command:",
                        "    - python3",
                        "    - health.py",
                        "ui_manifest: ui/ui_manifest.yaml",
                        "features:",
                        "  - tool",
                        *dependency_lines,
                    ]
                )
                + "\n",
            )
            archive.writestr(
                f"{bundle_dir}/main.py",
                "import json, sys\nprint(json.dumps({'ok': True, 'request': sys.stdin.read()}))\n",
            )
            archive.writestr(f"{bundle_dir}/health.py", "print('ok')\n")
            archive.writestr(f"{bundle_dir}/ui/ui_manifest.yaml", "version: '1'\npages: []\n")
            archive.writestr(f"{bundle_dir}/icon.png", EXTENSION_ICON_PNG)
            for wheel_name, wheel_content in (wheels or {}).items():
                archive.writestr(f"{bundle_dir}/wheels/{wheel_name}", wheel_content)
        return buffer.getvalue()

    @staticmethod
    def _extension_descriptor_zip_bytes(
        *,
        extension_id: str = "acme.runtime-extension",
        version: str = "1.0.0",
        workflow_id: str = "wf_sync",
        agent_id: str = "agent_sync",
    ) -> bytes:
        buffer = io.BytesIO()
        descriptor = {
            "schema_version": "extension_descriptor_v1",
            "extension_id": extension_id,
            "release_version": version,
            "extension_type": "service_extension",
            "identity": {
                "display_name": "Runtime Extension",
                "description": "desc",
                "tags": ["runtime"],
            },
            "vendor": {
                "vendor_name": "Acme",
                "support_url": "https://acme.example.com/support",
            },
            "distribution": {
                "channel": "private_byo",
                "execution_modes": ["local"],
                "allows_self_hosted": True,
                "requires_connected_control_plane": False,
                "import_sources": ["file", "url"],
            },
            "runtime": {
                "contract_version": "extension_host_v2",
                "compatibility": {"platform_min": "0.1.0"},
            },
            "artifacts": {
                "workflows": [
                    {
                        "workflow_id": workflow_id,
                        "ref": "artifacts/workflows/wf_sync.yaml",
                        "publish": True,
                    }
                ],
                "agents": [
                    {
                        "agent_id": agent_id,
                        "ref": "artifacts/agents/agent_sync.yaml",
                        "publish": True,
                    }
                ],
            },
            "provenance": {"origin": "imported"},
        }
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("extension-descriptor.yaml", json.dumps(descriptor))
            archive.writestr(
                "artifacts/workflows/wf_sync.yaml",
                f"workflow_id: {workflow_id}\n",
            )
            archive.writestr(
                "artifacts/agents/agent_sync.yaml",
                f"agent_id: {agent_id}\n",
            )
        return buffer.getvalue()

    @classmethod
    def _unified_extension_zip_bytes(
        cls,
        *,
        extension_id: str = "acme.vendor-demo",
        version: str = "1.0.0",
        descriptor_extension_id: str = "",
        descriptor_version: str = "",
        workflow_id: str = "wf_unified",
        agent_id: str = "agent_unified",
    ) -> bytes:
        buffer = io.BytesIO(cls._extension_zip_bytes(extension_id=extension_id, version=version))
        bundle_dir = extension_folder_name(extension_id)
        descriptor = {
            "schema_version": "extension_descriptor_v1",
            "extension_id": descriptor_extension_id or extension_id,
            "release_version": descriptor_version or version,
            "extension_type": "service_extension",
            "identity": {"display_name": "Unified Extension", "description": "desc"},
            "vendor": {"vendor_name": "Acme"},
            "distribution": {
                "channel": "private_byo",
                "execution_modes": ["local"],
                "allows_self_hosted": True,
                "requires_connected_control_plane": False,
                "import_sources": ["file"],
            },
            "runtime": {
                "contract_version": "extension_host_v2",
                "compatibility": {"platform_min": "0.1.0"},
            },
            "artifacts": {
                "workflows": [
                    {
                        "workflow_id": workflow_id,
                        "ref": "artifacts/workflows/wf_unified.yaml",
                        "publish": True,
                    }
                ],
                "agents": [
                    {
                        "agent_id": agent_id,
                        "ref": "artifacts/agents/agent_unified.yaml",
                        "publish": True,
                    }
                ],
            },
            "provenance": {"origin": "imported"},
        }
        with zipfile.ZipFile(buffer, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                f"{bundle_dir}/extension-descriptor.yaml",
                json.dumps(descriptor),
            )
            archive.writestr(
                f"{bundle_dir}/artifacts/workflows/wf_unified.yaml",
                f"workflow_id: {workflow_id}\n",
            )
            archive.writestr(
                f"{bundle_dir}/artifacts/agents/agent_unified.yaml",
                f"agent_id: {agent_id}\n",
            )
        return buffer.getvalue()

    def test_scan_extensions_syncs_results(self):
        bl = MagicMock()
        bl.conn = None
        scanned = [{"extension_key": "acme.demo", "enabled": False}]
        bl.list_extensions.return_value = scanned

        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value="/tmp/extensions",
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.scan_extensions",
                return_value=scanned,
            ),
        ):
            payload = service.scan_extensions(root="").dump()

        assert payload["root"] == "/tmp/extensions"
        assert payload["scanned_count"] == 1
        bl.sync_extension_scan.assert_called_once_with(scanned)

    def test_scan_extensions_enriches_descriptor_bundle_with_canonical_import(
        self, tmp_path: Path
    ) -> None:
        extensions_root = tmp_path / "extensions"
        bundle_root = extensions_root / "acme_demo"
        bundle_root.mkdir(parents=True)
        descriptor_path = bundle_root / "extension-descriptor.yaml"
        workflow_path = bundle_root / "artifacts" / "workflows" / "wf_scan.yaml"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text("workflow_id: wf_scan\n", encoding="utf-8")
        descriptor_path.write_text(
            json.dumps(
                {
                    "schema_version": "extension_descriptor_v1",
                    "extension_id": "acme.demo",
                    "release_version": "1.0.0",
                    "extension_type": "service_extension",
                    "identity": {"display_name": "Scan Demo", "description": "desc"},
                    "vendor": {"vendor_name": "Acme"},
                    "distribution": {
                        "channel": "private_byo",
                        "execution_modes": ["local"],
                        "allows_self_hosted": True,
                        "requires_connected_control_plane": False,
                        "import_sources": ["file"],
                    },
                    "runtime": {
                        "contract_version": "extension_host_v2",
                        "compatibility": {"platform_min": "0.1.0"},
                    },
                    "artifacts": {
                        "workflows": [
                            {
                                "workflow_id": "wf_scan",
                                "ref": "artifacts/workflows/wf_scan.yaml",
                                "publish": True,
                            }
                        ]
                    },
                    "provenance": {"origin": "imported"},
                }
            ),
            encoding="utf-8",
        )
        scanned = [
            {
                "extension_key": "acme.demo",
                "enabled": False,
                "status": "valid",
                "bundle_root": str(bundle_root),
            }
        ]
        bl = MagicMock()
        bl.conn = None
        bl.list_extensions.return_value = scanned
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin",
            principal_type="admin",
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.scan_extensions",
                return_value=scanned,
            ),
        ):
            service.scan_extensions(root="", scope=scope)

        releases = runtime_store.list_releases(extension_id="acme.demo")
        assert len(releases) == 1
        assert releases[0].source_kind == "extension_scan"
        assert releases[0].source_ref == "acme.demo"
        snapshots = runtime_store.list_artifact_snapshots(releases[0].release_id)
        assert [(row.artifact_kind, row.artifact_id) for row in snapshots] == [
            ("workflow", "wf_scan")
        ]
        projections = runtime_store.list_projections(
            account_id=DEFAULT_ACCOUNT_ID,
            only_active=False,
        )
        assert [(row.artifact_kind, row.artifact_id) for row in projections] == [
            ("workflow", "wf_scan")
        ]
        install = install_store.get(extension_id="acme.demo", account_id=DEFAULT_ACCOUNT_ID)
        assert install is not None
        assert install.release_id == releases[0].release_id

    def test_scan_extensions_rejects_unmanaged_root(self, tmp_path):
        bl = MagicMock()
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        managed_root = tmp_path / "extensions"
        outside_root = tmp_path / "elsewhere"

        _raises_input_5220_1 = str(outside_root)
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(managed_root),
            ),
            pytest.raises(ServiceValidationError, match="managed extensions root"),
        ):
            service.scan_extensions(root=_raises_input_5220_1)

    def test_install_extension_archive_extracts_zip_and_enables_extension(self, tmp_path):
        bl = MagicMock()
        bl.conn = None
        installed_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
        }
        bl.list_extensions.return_value = [installed_row]
        bl.enable_extension.return_value = installed_row

        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(tmp_path / "extensions"),
        ):
            payload = service.install_extension_archive(
                filename="vendor-demo.zip",
                content=self._extension_zip_bytes(),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
                ),
            ).dump()

        assert payload["extension"]["extension_key"] == "acme.vendor-demo"
        active_bundle = tmp_path / "extensions" / "acme_vendor_demo"
        assert active_bundle.is_symlink()
        release_bundle = active_bundle.resolve()
        assert release_bundle.parent.parent == tmp_path / "extensions" / ".releases"
        assert (release_bundle / ".fs-extension-install.yaml").is_file()
        assert bl.sync_extension_scan.call_count >= 1
        bl.enable_extension.assert_called_once()

    def test_simple_install_publishes_after_lifecycle_transaction_commits(self, tmp_path):
        from contextlib import contextmanager

        bl = MagicMock()
        bl.conn = None
        installed_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
        }
        bl.list_extensions.return_value = [installed_row]
        bl.enable_extension.return_value = installed_row
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        events: list[str] = []

        @contextmanager
        def lifecycle_transaction(_bl):
            events.append("transaction-open")
            yield None
            events.append("transaction-committed")

        def activate_release(**_kwargs):
            events.append("runtime-published")

        service._context._extension_lifecycle_transaction = lifecycle_transaction
        service._managed_release_service.activate_managed_simple_extension_release = (
            activate_release
        )
        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(tmp_path / "extensions"),
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip",
                content=self._extension_zip_bytes(),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
                ),
            )

        assert events == ["transaction-open", "transaction-committed", "runtime-published"]

    def test_scan_recovers_committed_release_that_was_not_published(self, tmp_path):
        extension_id = "acme.vendor-demo"
        extensions_root = tmp_path / "extensions"
        release_root = extensions_root / ".releases" / "acme_vendor_demo" / "1.0.1"
        release_root.mkdir(parents=True)
        (release_root / "extension.yaml").write_text("extension_id: acme.vendor-demo\n")
        active_root = extensions_root / "acme_vendor_demo"
        stale_release = extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0"
        stale_release.mkdir(parents=True)
        active_root.symlink_to(stale_release, target_is_directory=True)

        bl = MagicMock()
        bl.conn = None
        bl.list_extensions.return_value = [
            {"extension_key": extension_id, "bundle_root": str(release_root)}
        ]
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda _extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
            extension_operation_failure_status=lambda _invocation: 502,
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(extensions_root),
        ):
            service._context._recover_managed_release_publications(bl=bl)

        assert active_root.is_symlink()
        assert active_root.resolve() == release_root

    def test_install_extension_archive_uses_packaged_binary_uncompressed_limit(self, tmp_path):
        from core.infrastructure.security.zip_guard import validate_zip_safe as real_zip_guard

        bl = MagicMock()
        bl.conn = None
        installed_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
        }
        bl.list_extensions.return_value = [installed_row]
        bl.enable_extension.return_value = installed_row
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch(
                "core.infrastructure.security.zip_guard.validate_zip_safe",
                wraps=real_zip_guard,
            ) as guard,
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip",
                content=self._extension_zip_bytes(),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        assert guard.call_args.kwargs["max_size_bytes"] == 150 * 1024 * 1024

    def test_install_extension_archive_rejects_native_payload_without_package_metadata(
        self, tmp_path
    ):
        content = io.BytesIO(self._extension_zip_bytes())
        bundle_dir = extension_folder_name("acme.vendor-demo")
        with zipfile.ZipFile(content, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            binary = zipfile.ZipInfo(f"{bundle_dir}/bin/linux-amd64/toolbox")
            binary.create_system = 3
            binary.external_attr = 0o106755 << 16
            archive.writestr(binary, b"toolbox")

            ordinary = zipfile.ZipInfo(f"{bundle_dir}/README.txt")
            ordinary.create_system = 3
            ordinary.external_attr = 0o107755 << 16
            archive.writestr(ordinary, b"documentation")

        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_5426_1 = content.getvalue()
        _raises_input_5426_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(ServiceValidationError, match="package metadata"),
        ):
            service.install_extension_archive(
                filename="targeted.zip", content=_raises_input_5426_1, scope=_raises_input_5426_2
            )

        assert not (tmp_path / "extensions" / bundle_dir).exists()

    def test_install_extension_archive_materializes_python_requirements_and_records_audit(
        self, tmp_path
    ):
        import yaml

        wheel_name, wheel_content, digest = self._synthetic_wheel_bytes()
        requirement = {"name": "fsdemo", "version": "1.2.3", "hashes": [digest]}
        bl = MagicMock()
        bl.conn = None
        installed_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
        }
        bl.list_extensions.return_value = [installed_row]
        bl.enable_extension.return_value = installed_row

        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(tmp_path / "extensions"),
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip",
                content=self._extension_zip_bytes(
                    python_requirements=[requirement],
                    wheels={wheel_name: wheel_content},
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
                ),
            )

        bundle_root = tmp_path / "extensions" / "acme_vendor_demo"
        assert (bundle_root / ".fs-python" / "1.0.0" / "site" / "fsdemo").is_dir()
        metadata = yaml.safe_load(
            (bundle_root / ".fs-extension-install.yaml").read_text(encoding="utf-8")
        )
        site_dir = metadata["python_dependencies"]["site_dir"]
        assert site_dir == str(bundle_root / ".fs-python" / "1.0.0" / "site")
        assert "install-staged" not in site_dir
        deps = metadata["python_dependencies"]["dependencies"]
        assert deps[0]["name"] == "fsdemo"
        assert deps[0]["hash"] == digest

    def test_install_extension_archive_cleans_bundle_files_when_scan_fails(self, tmp_path):
        bl = MagicMock()
        bl.conn = None
        bl.sync_extension_scan.side_effect = RuntimeError("scan boom")

        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_5513_1 = self._extension_zip_bytes()
        _raises_input_5513_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(RuntimeError, match="scan boom"),
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip", content=_raises_input_5513_1, scope=_raises_input_5513_2
            )

        assert not (tmp_path / "extensions" / "acme_vendor_demo").exists()

    def test_install_extension_archive_rejects_runtime_target_before_publication(self, tmp_path):
        content = io.BytesIO(self._extension_zip_bytes())
        with zipfile.ZipFile(content, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "acme_vendor_demo/.fs-package.yaml",
                "runtime_target: linux-arm64\n",
            )

        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_5549_1 = content.getvalue()
        _raises_input_5549_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch(
                "core.infrastructure.extension_runtime.runtime_target.extension_runtime_target",
                return_value="linux-amd64",
            ),
            pytest.raises(ServiceValidationError) as exc_info,
        ):
            service.install_extension_archive(
                filename="targeted.zip", content=_raises_input_5549_1, scope=_raises_input_5549_2
            )

        assert exc_info.value.payload == {
            "package_runtime_target": "linux-arm64",
            "server_runtime_target": "linux-amd64",
        }
        assert "server target is 'linux-amd64'" in str(exc_info.value)
        assert not (tmp_path / "extensions" / "acme_vendor_demo").exists()

    def test_install_extension_archive_rejects_a_target_package_hiding_a_native_executable(
        self, tmp_path
    ):
        """A target package may declare no native executable only if it ships none."""
        content = io.BytesIO(self._extension_zip_bytes())
        elf64 = bytearray(64)
        elf64[:7] = b"\x7fELF\x02\x01\x01"
        elf64[18:20] = (62).to_bytes(2, "little")
        with zipfile.ZipFile(content, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "acme_vendor_demo/.fs-package.yaml",
                "runtime_target: linux-amd64\n",
            )
            archive.writestr("acme_vendor_demo/bin/linux-amd64/runtime", bytes(elf64))
        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_5597_1 = content.getvalue()
        _raises_input_5597_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch(
                "core.infrastructure.extension_runtime.runtime_target.extension_runtime_target",
                return_value="linux-amd64",
            ),
            pytest.raises(ServiceValidationError, match="declares none"),
        ):
            service.install_extension_archive(
                filename="targeted.zip", content=_raises_input_5597_1, scope=_raises_input_5597_2
            )

        assert not (tmp_path / "extensions" / "acme_vendor_demo").exists()

    def test_install_extension_archive_accepts_valid_native_target_package(self, tmp_path):
        content = io.BytesIO(self._extension_zip_bytes())
        bundle_dir = extension_folder_name("acme.vendor-demo")
        binary = self._elf64(62)
        declared_path = "bin/linux-amd64/runtime"
        with zipfile.ZipFile(content, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            binary_member = zipfile.ZipInfo(f"{bundle_dir}/{declared_path}")
            binary_member.create_system = 3
            binary_member.external_attr = 0o100755 << 16
            archive.writestr(binary_member, binary)
            archive.writestr(
                f"{bundle_dir}/.fs-package.yaml",
                "runtime_target: linux-amd64\n"
                "native_executable:\n"
                f"  path: {declared_path}\n"
                f"  sha256: {hashlib.sha256(binary).hexdigest()}\n",
            )

        bl = MagicMock()
        bl.conn = None
        installed_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_root": str(tmp_path / "extensions" / bundle_dir),
        }
        bl.list_extensions.return_value = [installed_row]
        bl.enable_extension.return_value = installed_row
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch(
                "core.infrastructure.extension_runtime.runtime_target.extension_runtime_target",
                return_value="linux-amd64",
            ),
        ):
            service.install_extension_archive(
                filename="targeted.zip",
                content=content.getvalue(),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        # ADR 0029, decision B: the shared extension tree's executable mode
        installed_binary = tmp_path / "extensions" / bundle_dir / declared_path
        assert installed_binary.stat().st_mode & 0o7777 == 0o775

        installed_binary = tmp_path / "extensions" / bundle_dir / declared_path
        assert installed_binary.read_bytes() == binary
        assert installed_binary.stat().st_mode & 0o7777 == 0o775

    @pytest.mark.parametrize(
        ("binary", "digest", "extra_path", "message"),
        [
            (_elf64.__func__(62), "0" * 64, "", "SHA-256"),
            (_elf64.__func__(183), "valid", "", "ELF machine"),
            (
                _elf64.__func__(62),
                "valid",
                "bin/linux-amd64/undeclared-helper",
                "undeclared target binary",
            ),
            (
                _elf64.__func__(62),
                "valid",
                "bin/linux-arm64/undeclared-runtime",
                "undeclared target binary",
            ),
        ],
    )
    def test_install_extension_archive_rejects_invalid_native_target_package_before_publication(
        self,
        tmp_path,
        binary: bytes,
        digest: str,
        extra_path: str,
        message: str,
    ):
        content = io.BytesIO(self._extension_zip_bytes())
        bundle_dir = extension_folder_name("acme.vendor-demo")
        declared_path = "bin/linux-amd64/runtime"
        declared_digest = hashlib.sha256(binary).hexdigest() if digest == "valid" else digest
        metadata = {
            "runtime_target": "linux-amd64",
            "native_executable": {
                "path": declared_path,
                "sha256": declared_digest,
            },
        }
        with zipfile.ZipFile(content, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{bundle_dir}/{declared_path}", binary)
            if extra_path:
                archive.writestr(f"{bundle_dir}/{extra_path}", self._elf64(62))
            archive.writestr(
                f"{bundle_dir}/.fs-package.yaml",
                "runtime_target: linux-amd64\n"
                "native_executable:\n"
                f"  path: {declared_path}\n"
                f"  sha256: {metadata['native_executable']['sha256']}\n",
            )

        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_5745_1 = content.getvalue()
        _raises_input_5745_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch(
                "core.infrastructure.extension_runtime.runtime_target.extension_runtime_target",
                return_value="linux-amd64",
            ),
            pytest.raises(ServiceValidationError, match=message),
        ):
            service.install_extension_archive(
                filename="targeted.zip", content=_raises_input_5745_1, scope=_raises_input_5745_2
            )

        assert not (tmp_path / "extensions" / bundle_dir).exists()

    @pytest.mark.parametrize("manifest_line", ["", "manifest_version: 1\n"])
    def test_install_extension_archive_rejects_missing_or_pre_v2_manifest_without_state(
        self, tmp_path, manifest_line
    ):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        bl = MagicMock()
        bl.conn = None
        self._persist_scanned_extension_rows(bl)
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "acme_old/extension.yaml",
                manifest_line + "extension_id: acme.old\nkind: tool_provider\nversion: 1.0.0\n",
            )

        _raises_input_5797_1 = content.getvalue()
        _raises_input_5797_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(ServiceValidationError, match="manifest_version: 2") as error,
        ):
            service.install_extension_archive(
                filename="old.zip", content=_raises_input_5797_1, scope=_raises_input_5797_2
            )

        assert "manifest_version" in str(error.value)
        assert install_store.list_for_account(DEFAULT_ACCOUNT_ID) == []
        assert runtime_store.list_releases(account_id=DEFAULT_ACCOUNT_ID) == []
        assert list((tmp_path / "extensions").iterdir()) == []

    def test_install_extension_archive_rejects_descriptor_only_zip_without_state(self, tmp_path):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        bl = MagicMock()
        bl.conn = None
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        _raises_input_5836_1 = self._extension_descriptor_zip_bytes()
        _raises_input_5836_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(ServiceValidationError, match=r"extension.yaml.*manifest_version: 2"),
        ):
            service.install_extension_archive(
                filename="runtime-extension.zip",
                content=_raises_input_5836_1,
                scope=_raises_input_5836_2,
            )

        assert install_store.list_for_account(DEFAULT_ACCOUNT_ID) == []
        assert runtime_store.list_releases(account_id=DEFAULT_ACCOUNT_ID) == []
        assert runtime_store.list_projections(account_id=DEFAULT_ACCOUNT_ID) == []
        assert list((tmp_path / "extensions").iterdir()) == []

    def test_rich_descriptor_update_does_not_unconditionally_delete_previous_release(
        self, tmp_path
    ):
        command = MagicMock()
        command.validate_descriptor.return_value = MagicMock()
        command._descriptor_validation_or_error.return_value = {
            "extension_id": "acme.runtime-extension"
        }
        command.import_descriptor_file.return_value = SimpleNamespace(
            extension_id="acme.runtime-extension",
            release_id="rel_new",
            dump=lambda: {
                "install": {"release_id": "rel_new"},
                "release_version": "2.0.0",
            },
        )
        query = MagicMock()
        query.get_extension.return_value = MagicMock()
        service = ExtensionBindingService(
            get_logger=lambda: MagicMock(),
            get_operation_store=lambda: MagicMock(),
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        service._context._extension_command_service = MagicMock(return_value=command)  # type: ignore[method-assign]
        service._context._extension_query_service = MagicMock(return_value=query)  # type: ignore[method-assign]
        descriptor = tmp_path / "extension-descriptor.yaml"
        descriptor.write_text("extension_id: acme.runtime-extension\n", encoding="utf-8")

        service._archive_new_install_service._install_extension_descriptor_archive(
            descriptor_path=descriptor,
            previous_install=SimpleNamespace(release_id="rel_shared", version="1.0.0"),
        )

        command._cleanup_release_artifacts.assert_not_called()

    def test_install_extension_archive_enriches_scan_install_with_same_install_id(self, tmp_path):
        import yaml

        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        bl = MagicMock()
        bl.conn = None
        installed_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_id": "pi_scan_owned",
            "version_id": "pr_scan_owned",
            "version": "1.0.0",
            "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
        }
        bl.list_extensions.return_value = [installed_row]
        bl.enable_extension.return_value = installed_row

        def _sync_scan(_rows):
            install_store.install(
                extension_id="acme.vendor-demo",
                version="1.0.0",
                account_id=DEFAULT_ACCOUNT_ID,
                install_id="pi_scan_owned",
                release_id="pr_scan_owned",
                descriptor_source="extension_scan",
            )
            runtime_store.create_release(
                {
                    "release_id": "pr_scan_owned",
                    "extension_id": "acme.vendor-demo",
                    "release_version": "1.0.0",
                    "descriptor": {"extension_id": "acme.vendor-demo"},
                    "source_kind": "extension_scan",
                    "source_ref": "acme.vendor-demo",
                    "account_id": DEFAULT_ACCOUNT_ID,
                }
            )

        bl.sync_extension_scan.side_effect = _sync_scan
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(tmp_path / "extensions"),
        ):
            service.install_extension_archive(
                filename="unified.zip",
                content=self._unified_extension_zip_bytes(),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        install = install_store.get(extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID)
        assert install is not None
        assert install.install_id == "pi_scan_owned"
        descriptor_release = next(
            row
            for row in runtime_store.list_releases(
                extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID
            )
            if row.release_id != "pr_scan_owned"
        )
        assert (
            Path(descriptor_release.descriptor["bundle_root"])
            == (tmp_path / "extensions" / "acme_vendor_demo").resolve()
        )
        assert all(
            row.install_id == install.install_id
            for row in runtime_store.list_projections(account_id=DEFAULT_ACCOUNT_ID)
        )
        metadata = yaml.safe_load(
            (tmp_path / "extensions" / "acme_vendor_demo" / ".fs-extension-install.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert metadata["install_id"] == install.install_id

    def test_unified_install_rolls_back_runtime_and_artifacts_when_enable_fails(self, tmp_path):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        bl = MagicMock()
        bl.conn = None
        installed_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": False,
            "status": "valid",
            "bundle_id": "pi_scan_owned",
            "version_id": "pr_scan_owned",
            "version": "1.0.0",
            "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
        }
        bl.list_extensions.return_value = [installed_row]
        bl.enable_extension.side_effect = RuntimeError("enable failed")

        def _sync_scan(_rows):
            if (
                install_store.get(extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID)
                is None
            ):
                install_store.install(
                    extension_id="acme.vendor-demo",
                    version="1.0.0",
                    account_id=DEFAULT_ACCOUNT_ID,
                    install_id="pi_scan_owned",
                    release_id="pr_scan_owned",
                    descriptor_source="extension_scan",
                )
            if runtime_store.get_release("pr_scan_owned") is None:
                runtime_store.create_release(
                    {
                        "release_id": "pr_scan_owned",
                        "extension_id": "acme.vendor-demo",
                        "release_version": "1.0.0",
                        "descriptor": {"extension_id": "acme.vendor-demo"},
                        "source_kind": "extension_scan",
                        "source_ref": "acme.vendor-demo",
                        "account_id": DEFAULT_ACCOUNT_ID,
                    }
                )

        bl.sync_extension_scan.side_effect = _sync_scan
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        _raises_input_6048_1 = self._unified_extension_zip_bytes()
        _raises_input_6048_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(RuntimeError, match="enable failed"),
        ):
            service.install_extension_archive(
                filename="unified.zip", content=_raises_input_6048_1, scope=_raises_input_6048_2
            )

        assert not (tmp_path / "extensions" / "acme_vendor_demo").exists()
        assert (
            install_store.get(extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID)
            is None
        )
        assert (
            runtime_store.list_releases(
                extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID
            )
            == []
        )
        assert runtime_store.list_projections(account_id=DEFAULT_ACCOUNT_ID) == []

    def test_unified_update_restores_previous_release_and_projections_when_enable_fails(
        self, tmp_path
    ):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.vendor-demo",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_existing",
            release_id="pr_existing",
            descriptor_source="extension_scan",
        )
        runtime_store.create_release(
            {
                "release_id": "pr_existing",
                "extension_id": "acme.vendor-demo",
                "release_version": "1.0.0",
                "descriptor": {"extension_id": "acme.vendor-demo"},
                "source_kind": "extension_scan",
                "source_ref": "acme.vendor-demo",
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
        runtime_store.upsert_projection(
            {
                "account_id": DEFAULT_ACCOUNT_ID,
                "install_id": "pi_existing",
                "release_id": "pr_existing",
                "extension_id": "acme.vendor-demo",
                "artifact_kind": "workflow",
                "artifact_id": "wf_previous",
                "projection": {"workflow_id": "wf_previous"},
            }
        )
        extensions_root = tmp_path / "extensions"
        previous_bundle = extensions_root / "acme_vendor_demo"
        previous_release_bundle = (
            extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-pr_existing"
        )
        previous_release_bundle.mkdir(parents=True)
        previous_bundle.symlink_to(previous_release_bundle, target_is_directory=True)
        (previous_bundle / "marker.txt").write_text("previous", encoding="utf-8")

        bl = MagicMock()
        bl.conn = None
        updated_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_id": "pi_existing",
            "version_id": "pr_candidate_scan",
            "version": "2.0.0",
            "bundle_root": str(previous_bundle),
        }
        previous_row = {**updated_row, "version": "1.0.0", "version_id": "pr_existing"}
        bl.list_extensions.return_value = [updated_row]
        bl.get_extension.return_value = previous_row
        bl.enable_extension.side_effect = [RuntimeError("enable failed"), previous_row]
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        _raises_input_6151_1 = self._unified_extension_zip_bytes(version="2.0.0")
        _raises_input_6151_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            pytest.raises(ServiceError, match="previous version was preserved"),
        ):
            service.install_extension_archive(
                filename="unified-v2.zip", content=_raises_input_6151_1, scope=_raises_input_6151_2
            )

        restored = install_store.get(extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID)
        assert restored is not None
        assert restored.version == "1.0.0"
        assert restored.release_id == "pr_existing"
        assert (previous_bundle / "marker.txt").read_text(encoding="utf-8") == "previous"
        projections = runtime_store.list_projections(
            account_id=DEFAULT_ACCOUNT_ID, only_active=False
        )
        assert [(row.release_id, row.artifact_id) for row in projections] == [
            ("pr_existing", "wf_previous")
        ]
        candidate_releases = [
            row
            for row in runtime_store.list_releases(
                extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID
            )
            if row.release_id != "pr_existing"
        ]
        assert candidate_releases == []

    @pytest.mark.parametrize(
        ("failure_source", "expected_compensation_error"),
        [
            ("lifecycle", "restore previous install failed"),
            ("cleanup_cursor", "failed scan cleanup cursor unavailable"),
        ],
    )
    def test_unified_update_reports_incomplete_recovery_when_compensation_fails(
        self,
        tmp_path,
        failure_source,
        expected_compensation_error,
    ):
        extension_id = "acme.vendor-demo"
        extensions_root = tmp_path / "extensions"
        previous_release_dir = extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-previous"
        previous_release_dir.mkdir(parents=True)
        target_dir = extensions_root / "acme_vendor_demo"
        target_dir.symlink_to(previous_release_dir, target_is_directory=True)
        bundle_dir = tmp_path / "candidate"
        bundle_dir.mkdir()
        (bundle_dir / "extension.yaml").write_text(
            "manifest_version: 2\nextension_id: acme.vendor-demo\nversion: 2.0.0\n",
            encoding="utf-8",
        )

        current_install = SimpleNamespace(
            extension_id=extension_id,
            install_id="install-existing",
            release_id="release-existing",
            version="1.0.0",
            state="active",
            config={},
            applied_data_schema_version=1,
        )
        install_store = MagicMock()
        install_store.get.return_value = current_install
        runtime_store = MagicMock()
        runtime_store.get_release.return_value = SimpleNamespace(
            release_id="release-existing",
            extension_id=extension_id,
        )
        runtime_store.get_entitlement.return_value = None
        runtime_store.list_projections.return_value = []

        bl = SimpleNamespace(
            conn=None,
            get_extension=lambda _extension_id: {},
            list_extensions=lambda enabled_only=False: [],
        )
        lifecycle_command = MagicMock()
        lifecycle_command.install_record_payload.return_value = {
            "extension_id": extension_id,
            "install_id": "install-existing",
            "release_id": "release-existing",
            "version": "1.0.0",
        }
        if failure_source == "lifecycle":
            lifecycle_command.rollback_install_release_failure.side_effect = RuntimeError(
                "restore previous install failed"
            )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        service._context._extension_command_service = MagicMock(return_value=lifecycle_command)
        service._state_service.sync_staged_extension_release = MagicMock(
            return_value={"extension_key": extension_id, "version": "2.0.0"}
        )
        candidate_failure = ServiceValidationError(
            "enable candidate failed",
            payload={
                "error": "stale candidate error",
                "message": "stale candidate message",
                "code": "candidate_enable_failed",
                "details": {"phase": "enable"},
            },
        )
        service._state_service.set_enabled_state_locked = MagicMock(
            side_effect=[
                candidate_failure,
                SimpleNamespace(extension={"extension_key": extension_id, "version": "1.0.0"}),
            ]
        )
        service._state_service.scan_extensions = MagicMock()
        if failure_source == "cleanup_cursor":
            service._archive_update_service._cleanup.cleanup_failed_simple_update_state = MagicMock(
                side_effect=RuntimeError("failed scan cleanup cursor unavailable")
            )

        _raises_input_6284_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch(
                "core.infrastructure.extension_runtime.python_dependencies.materialize_python_requirements",
                return_value={},
            ),
            pytest.raises(ServiceError, match="recovery was incomplete") as caught,
        ):
            service._archive_update_service.update_simple_extension_bundle(
                bl=bl,
                install_store=install_store,
                runtime_store=runtime_store,
                bundle_dir=bundle_dir,
                compiled={
                    "extension_key": extension_id,
                    "version": "2.0.0",
                    "checksum": "candidate",
                    "compiled_manifest": {"lifecycle": {"data_schema_version": 1}},
                },
                extensions_root=extensions_root,
                scope=_raises_input_6284_1,
                current_install=current_install,
            )

        assert caught.value.__cause__ is candidate_failure
        failures = caught.value.compensation_failures
        assert isinstance(failures, BaseExceptionGroup)
        assert [str(failure) for failure in failures.exceptions] == [expected_compensation_error]
        assert "previous version was preserved" not in caught.value.message
        assert caught.value.payload["error"] == caught.value.message
        assert caught.value.payload["message"] == caught.value.message
        assert caught.value.payload["code"] == "candidate_enable_failed"
        assert caught.value.payload["details"] == {"phase": "enable"}

        from core.infrastructure.runtime import serve_request_runtime

        response = serve_request_runtime.service_error_response(
            caught.value,
            None,
            error_payload=lambda **kwargs: kwargs,
            request_correlation_id=lambda _request, fallback="": fallback or "generated",
            error_code_for_status=lambda status: f"status-{status}",
            err_request_failed="Request failed",
        )
        serialized = json.loads(response.body)
        assert serialized["message"] == caught.value.message
        assert serialized["code"] == "candidate_enable_failed"
        assert serialized["details"] == {"phase": "enable"}

    def test_unified_update_compensates_cancellation_and_reraises_same_exception(self, tmp_path):
        extension_id = "acme.vendor-demo"
        extensions_root = tmp_path / "extensions"
        previous_release_dir = extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-old"
        previous_release_dir.mkdir(parents=True)
        target_dir = extensions_root / "acme_vendor_demo"
        target_dir.symlink_to(previous_release_dir, target_is_directory=True)
        bundle_dir = tmp_path / "candidate-cancelled"
        bundle_dir.mkdir()
        (bundle_dir / "extension.yaml").write_text(
            "manifest_version: 2\nextension_id: acme.vendor-demo\nversion: 2.0.0\n",
            encoding="utf-8",
        )
        current_install = SimpleNamespace(
            extension_id=extension_id,
            install_id="install-existing",
            release_id="release-existing",
            version="1.0.0",
            state="active",
            config={},
            applied_data_schema_version=1,
        )
        install_store = MagicMock()
        install_store.get.return_value = current_install
        runtime_store = MagicMock()
        runtime_store.get_release.return_value = SimpleNamespace(
            release_id="release-existing",
            extension_id=extension_id,
        )
        runtime_store.get_entitlement.return_value = None
        runtime_store.list_projections.return_value = []
        bl = SimpleNamespace(
            conn=None,
            get_extension=lambda _extension_id: {},
            list_extensions=lambda enabled_only=False: [],
        )
        lifecycle_command = MagicMock()
        lifecycle_command.install_record_payload.return_value = {
            "extension_id": extension_id,
            "install_id": "install-existing",
            "release_id": "release-existing",
            "version": "1.0.0",
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        service._context._extension_command_service = MagicMock(return_value=lifecycle_command)
        service._state_service.scan_extensions = MagicMock()
        service._state_service.set_enabled_state_locked = MagicMock(
            return_value=SimpleNamespace(
                extension={"extension_key": extension_id, "version": "1.0.0"}
            )
        )
        cancellation = asyncio.CancelledError("cancel managed update")

        _raises_input_6402_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch(
                "core.infrastructure.extension_runtime.python_dependencies.materialize_python_requirements",
                side_effect=cancellation,
            ),
            pytest.raises(asyncio.CancelledError) as caught,
        ):
            service._archive_update_service.update_simple_extension_bundle(
                bl=bl,
                install_store=install_store,
                runtime_store=runtime_store,
                bundle_dir=bundle_dir,
                compiled={
                    "extension_key": extension_id,
                    "version": "2.0.0",
                    "checksum": "candidate-cancelled",
                    "compiled_manifest": {"lifecycle": {"data_schema_version": 1}},
                },
                extensions_root=extensions_root,
                scope=_raises_input_6402_1,
                current_install=current_install,
            )

        assert caught.value is cancellation
        service._state_service.scan_extensions.assert_called_once()
        service._state_service.set_enabled_state_locked.assert_called_once()
        lifecycle_command.rollback_install_release_failure.assert_called_once()
        assert list(extensions_root.glob(".acme_vendor_demo.update-staged-*")) == []

    def test_unified_update_defers_previous_release_cleanup(self, tmp_path):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.vendor-demo",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_existing",
            release_id="pr_existing",
            descriptor_source="extension_scan",
        )
        runtime_store.create_release(
            {
                "release_id": "pr_existing",
                "extension_id": "acme.vendor-demo",
                "release_version": "1.0.0",
                "descriptor": {"extension_id": "acme.vendor-demo"},
                "source_kind": "extension_scan",
                "source_ref": "acme.vendor-demo",
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
        extensions_root = tmp_path / "extensions"
        previous_bundle = extensions_root / "acme_vendor_demo"
        previous_release_bundle = (
            extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-pr_existing"
        )
        previous_release_bundle.mkdir(parents=True)
        (previous_release_bundle / "marker.txt").write_text("previous", encoding="utf-8")
        previous_bundle.symlink_to(previous_release_bundle, target_is_directory=True)
        updated_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "bundle_id": "pi_existing",
            "version_id": "pr_candidate_scan",
            "version": "2.0.0",
            "bundle_root": str(previous_bundle),
        }
        previous_row = {**updated_row, "version": "1.0.0", "version_id": "pr_existing"}
        bl = MagicMock()
        bl.conn = None
        bl.list_extensions.return_value = [updated_row]
        bl.get_extension.return_value = previous_row
        bl.enable_extension.return_value = updated_row
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        cleanup_release = MagicMock(
            side_effect=AssertionError("successful repoint must not run release cleanup")
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch(
                "core.application.extensions.command_lifecycle.ExtensionCommandLifecycle._cleanup_release_if_unreferenced",
                cleanup_release,
            ),
        ):
            result = service.install_extension_archive(
                filename="unified-v2.zip",
                content=self._unified_extension_zip_bytes(version="2.0.0"),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        cleanup_release.assert_not_called()
        updated = install_store.get(extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID)
        assert updated is not None
        assert updated.version == "2.0.0"
        assert updated.release_id != "pr_existing"
        assert result.extension["lifecycle_operation"] == "updated"
        assert runtime_store.get_release("pr_existing") is not None

    def test_uninstall_preview_lists_connections_by_install_without_project_scope(self):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.catalog-provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_catalog",
            release_id="pr_catalog",
            descriptor_source="extension_scan",
        )
        runtime_store.create_release(
            {
                "release_id": "pr_catalog",
                "extension_id": "acme.catalog-provider",
                "release_version": "1.0.0",
                "descriptor": {"extension_id": "acme.catalog-provider"},
                "source_kind": "extension_scan",
                "source_ref": "acme.catalog-provider",
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        connection_rows = [
            {
                "connection_id": "conn_a",
                "display_name": "Catalog primary",
                "extension_install_id": "pi_catalog",
                "secret_refs": {"refs": {"token": "must-not-leak"}},
            },
            {
                "connection_id": "conn_b",
                "display_name": "Catalog backup",
                "extension_install_id": "pi_catalog",
            },
        ]

        with patch(
            "core.infrastructure.db.extension_runtime_connection_store.list_extension_runtime_connections",
            return_value=connection_rows,
        ) as list_connections:
            plan = service.preview_extension_uninstall(
                "acme.catalog-provider",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        connection_impact = [row for row in plan["remove"] if row["kind"] == "project_connection"]
        assert connection_impact == [
            {
                "kind": "project_connection",
                "id": "conn_a",
                "label": "Project connection",
                "display_name": "Catalog primary",
                "count": 1,
            },
            {
                "kind": "project_connection",
                "id": "conn_b",
                "label": "Project connection",
                "display_name": "Catalog backup",
                "count": 1,
            },
        ]
        assert "must-not-leak" not in json.dumps(plan)
        list_connections.assert_called_once_with(
            bl,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_catalog",
            enabled_only=False,
        )

    def test_uninstall_preview_lists_project_assignments_owned_by_install(self):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.catalog-provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_catalog",
            release_id="pr_catalog",
            descriptor_source="extension_scan",
        )
        runtime_store.create_release(
            {
                "release_id": "pr_catalog",
                "extension_id": "acme.catalog-provider",
                "release_version": "1.0.0",
                "descriptor": {"extension_id": "acme.catalog-provider"},
                "source_kind": "extension_scan",
                "source_ref": "acme.catalog-provider",
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
        for artifact_kind, artifact_id in (
            ("workflow", "wf_catalog"),
            ("dataset", "dataset_catalog"),
        ):
            runtime_store.upsert_projection(
                {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "install_id": "pi_catalog",
                    "release_id": "pr_catalog",
                    "extension_id": "acme.catalog-provider",
                    "artifact_kind": artifact_kind,
                    "artifact_id": artifact_id,
                    "projection": {
                        "id": artifact_id,
                        "display_name": {
                            "wf_catalog": "Catalog sync",
                            "dataset_catalog": "Catalog items",
                        }[artifact_id],
                    },
                }
            )
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [
                ("extension.pi_catalog.wf_catalog", "Alpha"),
                ("extension.pi_catalog.wf_catalog", "Beta"),
            ],
            [
                ("extension.pi_catalog.dataset_catalog", "Alpha"),
                ("extension.pi_catalog.dataset_catalog", "Beta"),
                ("extension.pi_catalog.dataset_catalog", "Gamma"),
            ],
        ]
        bl = MagicMock()
        bl.cursor.return_value.__enter__.return_value = cursor
        bl._table_exists.side_effect = lambda table: (
            table
            in {
                "project_workflows",
                "project_datasets",
            }
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        with patch(
            "core.infrastructure.db.extension_runtime_connection_store.list_extension_runtime_connections",
            return_value=[],
        ):
            plan = service.preview_extension_uninstall(
                "acme.catalog-provider",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        remove = {row["kind"]: row for row in plan["remove"]}
        assert remove["project_workflow_assignment"]["count"] == 2
        assert remove["project_dataset_assignment"]["count"] == 3
        # Named by what a person recognises: the workflow/dataset and its projects.
        assert remove["project_workflow_assignment"]["label"] == "Workflows in projects"
        assert remove["project_workflow_assignment"]["display_name"] == "Catalog sync (Alpha, Beta)"
        assert remove["project_workflow_assignment"]["project_names"] == ["Alpha", "Beta"]
        assert remove["project_dataset_assignment"]["display_name"] == (
            "Catalog items (Alpha, Beta, Gamma)"
        )
        # The projected artifacts are listed once, by name, not as projection ids.
        assert "projection" not in remove
        assert "pi_catalog" not in json.dumps(
            [{k: v for k, v in row.items() if k not in {"id"}} for row in plan["remove"]]
        )

    def test_uninstall_preview_lists_host_records_and_retained_audits(self):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="fixture.catalog-provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_catalog",
            release_id="pr_catalog",
        )
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [{"id": "cb-1", "callback_id": "events", "project_id": "project-1"}],
            [{"id": "provider-1", "provider_key": "catalog", "display_label": "Catalog LLM"}],
            [
                {
                    "state_id": "state-1",
                    "namespace": "sync",
                    "state_key": "cursor",
                    "project_id": "project-1",
                }
            ],
            # The affected read looks up the provider's keys to find agents and
            # projects that route to them; neither table exists here.
            [{"provider_key": "catalog", "display_label": "Catalog LLM"}],
        ]
        cursor.fetchone.return_value = {"audit_count": 4}
        bl = MagicMock()
        bl.cursor.return_value.__enter__.return_value = cursor
        bl._table_exists.side_effect = lambda table: (
            table
            in {
                "extension_callback_registrations",
                "llm_provider_sources",
                "extension_project_state",
                "external_effects",
            }
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        with patch(
            "core.infrastructure.db.extension_runtime_connection_store.list_extension_runtime_connections",
            return_value=[],
        ):
            plan = service.preview_extension_uninstall(
                "fixture.catalog-provider",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        assert any(
            row["kind"] == "callback_registration" and row["id"] == "cb-1" for row in plan["remove"]
        )
        assert any(
            row["kind"] == "llm_provider_source" and row["id"] == "provider-1"
            for row in plan["remove"]
        )
        assert any(
            row["kind"] == "project_state" and row["id"] == "state-1" for row in plan["remove"]
        )
        assert plan["affected"] == []
        assert plan["retained"] == [
            {
                "kind": "external_effect_audit",
                "id": "pi_catalog",
                "label": "Record of actions sent to other services",
                "display_name": "Kept for audit, with secrets removed",
                "count": 4,
            }
        ]

    @pytest.mark.parametrize(
        ("descriptor_extension_id", "descriptor_version"),
        [("acme.other", "1.0.0"), ("acme.vendor-demo", "2.0.0")],
    )
    def test_install_extension_archive_rejects_manifest_descriptor_identity_mismatch(
        self, tmp_path, descriptor_extension_id, descriptor_version
    ):
        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_6794_1 = self._unified_extension_zip_bytes(
            descriptor_extension_id=descriptor_extension_id, descriptor_version=descriptor_version
        )
        _raises_input_6794_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(ServiceValidationError, match="must match"),
        ):
            service.install_extension_archive(
                filename="mismatch.zip", content=_raises_input_6794_1, scope=_raises_input_6794_2
            )

        assert not (tmp_path / "extensions" / "acme_vendor_demo").exists()

    def test_install_extension_archive_rejects_same_version_update(self, tmp_path):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        bl = MagicMock()
        bl.conn = None
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.runtime-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_existing",
            release_id="pr_existing",
            descriptor_source="file",
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        _raises_input_6843_1 = self._unified_extension_zip_bytes(
            extension_id="acme.runtime-extension", version="1.0.0"
        )
        _raises_input_6843_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(ServiceValidationError, match="already installed"),
        ):
            service.install_extension_archive(
                filename="runtime-extension.zip",
                content=_raises_input_6843_1,
                scope=_raises_input_6843_2,
            )

    def test_install_extension_archive_rejects_downgrade_update(self, tmp_path):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        bl = MagicMock()
        bl.conn = None
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.runtime-extension",
            version="1.0.1",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_existing",
            release_id="pr_existing",
            descriptor_source="file",
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        _raises_input_6890_1 = self._unified_extension_zip_bytes(
            extension_id="acme.runtime-extension", version="1.0.0"
        )
        _raises_input_6890_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(ServiceValidationError, match="Downgrade not supported"),
        ):
            service.install_extension_archive(
                filename="runtime-extension.zip",
                content=_raises_input_6890_1,
                scope=_raises_input_6890_2,
            )

    def test_install_extension_archive_updates_rich_extension_and_preserves_install_id(
        self, tmp_path
    ):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        bl = MagicMock()
        bl.conn = None
        self._persist_scanned_extension_rows(bl)
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        extensions_root = tmp_path / "extensions"
        bl.list_extensions.side_effect = lambda *args, **kwargs: self._managed_scan_rows(
            extensions_root, "acme.runtime-extension"
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(extensions_root),
        ):
            service.install_extension_archive(
                filename="runtime-extension.zip",
                content=self._unified_extension_zip_bytes(
                    extension_id="acme.runtime-extension",
                    version="1.0.0",
                    workflow_id="wf_v1",
                    agent_id="agent_v1",
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )
            initial = install_store.get(
                extension_id="acme.runtime-extension", account_id=DEFAULT_ACCOUNT_ID
            )
            assert initial is not None
            updated = service.install_extension_archive(
                filename="runtime-extension.zip",
                content=self._unified_extension_zip_bytes(
                    extension_id="acme.runtime-extension",
                    version="1.0.1",
                    workflow_id="wf_v2",
                    agent_id="agent_v2",
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            ).dump()

        refreshed = install_store.get(
            extension_id="acme.runtime-extension", account_id=DEFAULT_ACCOUNT_ID
        )
        assert refreshed is not None
        assert refreshed.install_id == initial.install_id
        assert refreshed.version == "1.0.1"
        assert updated["extension"]["lifecycle_operation"] == "updated"
        assert updated["extension"]["from_version"] == "1.0.0"
        assert updated["extension"]["to_version"] == "1.0.1"
        projection_pairs = {
            (row.artifact_kind, row.artifact_id)
            for row in runtime_store.list_projections(
                account_id=DEFAULT_ACCOUNT_ID,
                only_active=False,
            )
        }
        assert projection_pairs == {("workflow", "wf_v2"), ("agent", "agent_v2")}

    def test_install_extension_archive_updates_simple_extension_and_preserves_disabled_state(
        self, tmp_path
    ):
        import yaml

        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        wheel_name, wheel_content, digest = self._synthetic_wheel_bytes()
        requirement = {"name": "fsdemo", "version": "1.2.3", "hashes": [digest]}

        bl = MagicMock()
        bl.conn = None
        bl.get_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "compiled_manifest": {"callbacks": [{"callback_id": "updates"}]},
        }
        bl.list_extension_callback_registrations.return_value = [{"id": "reg_updates"}]
        bl.list_extensions.return_value = [
            {
                "extension_key": "acme.vendor-demo",
                "enabled": False,
                "status": "valid",
                "version": "1.0.1",
                "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
                "compiled_manifest": {"callbacks": []},
            }
        ]
        bl.enable_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "enabled": False,
            "state": "disabled",
            "version": "1.0.1",
            "release_id": "rel_new",
            "compiled_manifest": {"callbacks": []},
        }
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.vendor-demo",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_vendor_demo",
            release_id="rel_old",
            descriptor_source="extension_scan",
            config={"account_id": DEFAULT_ACCOUNT_ID},
        )
        install_store.set_state(
            extension_id="acme.vendor-demo",
            account_id=DEFAULT_ACCOUNT_ID,
            state="disabled",
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(tmp_path / "extensions"),
        ):
            payload = service.install_extension_archive(
                filename="vendor-demo.zip",
                content=self._extension_zip_bytes(
                    version="1.0.1",
                    python_requirements=[requirement],
                    wheels={wheel_name: wheel_content},
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            ).dump()

        assert payload["extension"]["lifecycle_operation"] == "updated"
        assert payload["extension"]["to_version"] == "1.0.1"
        active_bundle = tmp_path / "extensions" / "acme_vendor_demo"
        assert active_bundle.is_symlink()
        assert active_bundle.resolve().parent.parent == tmp_path / "extensions" / ".releases"
        bl.enable_extension.assert_called_once()
        assert bl.enable_extension.call_args.kwargs["enabled"] is False
        assert bl.enable_extension.call_args.kwargs["version"] == "1.0.1"
        bl.update_extension_callback_registration_state.assert_called_once()
        metadata = yaml.safe_load(
            (tmp_path / "extensions" / "acme_vendor_demo" / ".fs-extension-install.yaml").read_text(
                encoding="utf-8"
            )
        )
        site_dir = metadata["python_dependencies"]["site_dir"]
        assert site_dir == str(
            tmp_path / "extensions" / "acme_vendor_demo" / ".fs-python" / "1.0.1" / "site"
        )
        assert "update-staged" not in site_dir

    def test_simple_update_keeps_shared_previous_release_on_immutable_bundle_root(self, tmp_path):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        extensions_root = tmp_path / "extensions"
        active_bundle = extensions_root / "acme_vendor_demo"
        old_release_bundle = (
            extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-rel_shared_old"
        )
        old_release_bundle.mkdir(parents=True)
        (old_release_bundle / "old-marker.txt").write_text("release-one", encoding="utf-8")
        (old_release_bundle / "extension.yaml").write_text(
            "manifest_version: 2\nextension_id: acme.vendor-demo\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        active_bundle.symlink_to(old_release_bundle, target_is_directory=True)
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="acme.vendor-demo",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_owner",
            release_id="rel_shared_old",
            descriptor_source="extension_scan",
        )
        install_store.install(
            extension_id="acme.vendor-demo",
            version="1.0.0",
            account_id="acc_consumer",
            install_id="pi_consumer",
            release_id="rel_shared_old",
            descriptor_source="extension_scan",
        )
        runtime_store.create_release(
            {
                "release_id": "rel_shared_old",
                "extension_id": "acme.vendor-demo",
                "release_version": "1.0.0",
                "descriptor": {
                    "extension_id": "acme.vendor-demo",
                    "bundle_root": str(old_release_bundle),
                },
                "source_kind": "extension_scan",
                "source_ref": "acme.vendor-demo",
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
        candidate_row = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "status": "valid",
            "version": "1.0.1",
            "release_id": "rel_new",
            "bundle_root": str(active_bundle),
        }
        previous_row = {**candidate_row, "version": "1.0.0", "release_id": "rel_shared_old"}
        bl = MagicMock()
        bl.conn = None
        bl.get_extension.return_value = previous_row
        bl.list_extensions.return_value = [candidate_row]

        def _enable_candidate(*_args, **_kwargs):
            install_store.install(
                extension_id="acme.vendor-demo",
                version="1.0.1",
                account_id=DEFAULT_ACCOUNT_ID,
                install_id="pi_owner",
                release_id="rel_new",
                descriptor_source="extension_scan",
            )
            return candidate_row

        bl.enable_extension.side_effect = _enable_candidate
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        lifecycle_order: list[str] = []
        original_rebind = runtime_store.update_release_bundle_root
        original_activate = (
            service._managed_release_service.activate_managed_simple_extension_release
        )

        def _record_rebind(*args, **kwargs):
            lifecycle_order.append("rebind_previous_release")
            return original_rebind(*args, **kwargs)

        def _record_activate(*args, **kwargs):
            lifecycle_order.append("activate_candidate")
            return original_activate(*args, **kwargs)

        runtime_store.update_release_bundle_root = _record_rebind  # type: ignore[method-assign]

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch.object(
                service._managed_release_service,
                "activate_managed_simple_extension_release",
                side_effect=_record_activate,
            ),
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip",
                content=self._extension_zip_bytes(version="1.0.1"),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        old_release = runtime_store.get_release("rel_shared_old")
        assert old_release is not None
        old_release_root = Path(old_release.descriptor["bundle_root"])
        assert active_bundle.is_symlink()
        assert old_release_root != active_bundle.resolve()
        assert (old_release_root / "old-marker.txt").read_text(encoding="utf-8") == "release-one"
        assert "version: 1.0.1" in (active_bundle / "extension.yaml").read_text(encoding="utf-8")
        assert lifecycle_order == ["activate_candidate"]

    def test_install_extension_archive_failed_simple_update_preserves_existing_bundle(
        self, tmp_path
    ):
        from core.application.extensions.install import ExtensionInstallStore

        bl = MagicMock()
        bl.conn = None
        bl.get_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "compiled_manifest": {"callbacks": []},
        }
        bl.enable_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "state": "active",
            "version": "1.0.0",
            "release_id": "rel_old",
            "compiled_manifest": {"callbacks": []},
        }
        install_store = ExtensionInstallStore()
        install_store.install(
            extension_id="acme.vendor-demo",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_vendor_demo",
            release_id="rel_old",
            descriptor_source="extension_scan",
            config={"account_id": DEFAULT_ACCOUNT_ID},
        )
        extensions_root = tmp_path / "extensions"
        target_dir = extensions_root / "acme_vendor_demo"
        previous_release_dir = extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-rel_old"
        previous_release_dir.mkdir(parents=True)
        target_dir.symlink_to(previous_release_dir, target_is_directory=True)
        marker = target_dir / "old-marker.txt"
        marker.write_text("old bundle survives", encoding="utf-8")
        (target_dir / "extension.yaml").write_text(
            "extension_id: acme.vendor-demo\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
        )
        service.scan_extensions = MagicMock()

        _raises_input_7273_1 = self._extension_zip_bytes(version="1.0.1")
        _raises_input_7273_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch(
                "core.infrastructure.extension_runtime.python_dependencies.materialize_python_requirements",
                side_effect=ValueError("broken wheel"),
            ),
            pytest.raises(ServiceError, match="previous version was preserved"),
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip", content=_raises_input_7273_1, scope=_raises_input_7273_2
            )

        assert target_dir.is_dir()
        assert marker.read_text(encoding="utf-8") == "old bundle survives"

    def test_install_extension_archive_rereads_current_version_inside_update_lock(self, tmp_path):
        store = MagicMock()
        version_state = {"version": "1.0.0"}

        def _current_install(*, extension_id, account_id):
            return SimpleNamespace(
                extension_id=extension_id,
                install_id="pi_vendor_demo",
                release_id="rel_current",
                version=version_state["version"],
                state="active",
                config={},
                descriptor_source="extension_scan",
            )

        store.get.side_effect = _current_install
        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: store,
        )

        @contextlib.contextmanager
        def _lock(**_kwargs):
            version_state["version"] = "1.0.1"
            yield

        service._context._extension_update_lock = _lock  # type: ignore[method-assign]
        service._archive_update_service._update_simple_extension_bundle = MagicMock()  # type: ignore[method-assign]

        _raises_input_7333_1 = self._extension_zip_bytes(version="1.0.1")
        _raises_input_7333_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            pytest.raises(ServiceValidationError, match="already installed"),
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip", content=_raises_input_7333_1, scope=_raises_input_7333_2
            )

        service._archive_update_service._update_simple_extension_bundle.assert_not_called()  # type: ignore[attr-defined]

    def test_extension_update_lock_pins_one_pooled_postgres_session(self):
        from core.infrastructure.db.db_pool import PooledConnectionProxy

        connection = MagicMock()
        connection.autocommit = True
        connection.closed = 0
        connection.get_transaction_status.return_value = 0
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        connection.cursor.return_value.__enter__.return_value = cursor
        pool = MagicMock()
        pool.getconn.return_value = connection
        bl = MagicMock()
        bl.conn = PooledConnectionProxy(pool)
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with service._context._extension_update_lock(bl=bl, extension_id="acme.vendor-demo"):
            pass

        assert pool.getconn.call_count == 1
        assert any(
            "pg_try_advisory_lock" in str(call.args[0]) for call in cursor.execute.call_args_list
        )
        assert any(
            "pg_advisory_unlock" in str(call.args[0]) for call in cursor.execute.call_args_list
        )
        connection.commit.assert_not_called()

    def _plain_folder_update_fixture(self, tmp_path):
        """An install whose files sit in a plain folder, plus a service to update it.

        A folder an author placed in ``extensions/`` by hand, or one an earlier
        build copied there, is registered by the scan exactly like that.
        """
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        bl = MagicMock()
        bl.conn = None
        bl.get_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "compiled_manifest": {"callbacks": []},
        }
        bl.list_extension_callback_registrations.return_value = []
        bl.list_extensions.return_value = [
            {
                "extension_key": "acme.vendor-demo",
                "enabled": True,
                "status": "valid",
                "version": "1.0.1",
                "bundle_root": str(tmp_path / "extensions" / "acme_vendor_demo"),
                "compiled_manifest": {"callbacks": []},
            }
        ]
        bl.enable_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "enabled": True,
            "state": "active",
            "version": "1.0.1",
            "release_id": "rel_new",
            "compiled_manifest": {"callbacks": []},
        }
        install_store = ExtensionInstallStore()
        install_store.install(
            extension_id="acme.vendor-demo",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_vendor_demo",
            release_id="rel_old",
            descriptor_source="extension_scan",
            config={"account_id": DEFAULT_ACCOUNT_ID},
        )
        # The plain layout: the files themselves at extensions/<folder>.
        target_dir = tmp_path / "extensions" / "acme_vendor_demo"
        with zipfile.ZipFile(io.BytesIO(self._extension_zip_bytes(version="1.0.0"))) as archive:
            archive.extractall(tmp_path / "extensions")
        assert target_dir.is_dir()
        assert not target_dir.is_symlink()
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: ExtensionRuntimeStore(),
        )
        return bl, install_store, service, target_dir

    def test_an_update_adopts_a_plain_folder_install_and_installs(self, tmp_path):
        """A file or Marketplace update works whatever layout the install has.

        The plain folder is moved, unchanged, into the release store, a link
        takes its place, and the update then proceeds as for any managed install.
        """
        bl, install_store, service, target_dir = self._plain_folder_update_fixture(tmp_path)
        legacy_manifest = (target_dir / "extension.yaml").read_text(encoding="utf-8")

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(tmp_path / "extensions"),
        ):
            payload = service.install_extension_archive(
                filename="vendor-demo.zip",
                content=self._extension_zip_bytes(version="1.0.1"),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
                ),
            ).dump()

        assert payload["extension"]["lifecycle_operation"] == "updated"
        assert payload["extension"]["from_version"] == "1.0.0"
        assert payload["extension"]["to_version"] == "1.0.1"
        releases = tmp_path / "extensions" / ".releases" / "acme_vendor_demo"
        assert target_dir.is_symlink()
        assert target_dir.resolve().parent == releases
        assert target_dir.resolve().name.startswith("1.0.1")
        adopted = [path for path in releases.iterdir() if path.name.startswith("1.0.0")]
        assert len(adopted) == 1
        assert (adopted[0] / "extension.yaml").read_text(encoding="utf-8") == legacy_manifest
        refreshed = install_store.get(
            extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID
        )
        assert refreshed is not None
        assert refreshed.install_id == "pi_vendor_demo"
        assert bl.enable_extension.call_args.kwargs["version"] == "1.0.1"

    def test_a_failed_adoption_puts_the_plain_folder_back_and_changes_nothing(self, tmp_path):
        bl, install_store, service, target_dir = self._plain_folder_update_fixture(tmp_path)
        legacy_manifest = (target_dir / "extension.yaml").read_text(encoding="utf-8")
        content = self._extension_zip_bytes(version="1.0.1")
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch(
                "core.application.extensions.managed_release_service.ManagedReleaseService."
                "activate_managed_simple_extension_release",
                side_effect=OSError("link refused"),
            ),
            patch(
                "core.application.extensions.archive_update_service.UpdateRollbackState",
                side_effect=AssertionError("a refused update must not arm rollback"),
            ),
            pytest.raises(ServiceValidationError) as refused,
        ):
            service.install_extension_archive(
                filename="vendor-demo.zip", content=content, scope=scope
            )

        assert "Nothing was changed" in refused.value.message
        assert "recovery" not in refused.value.message
        assert target_dir.is_dir()
        assert not target_dir.is_symlink()
        assert (target_dir / "extension.yaml").read_text(encoding="utf-8") == legacy_manifest
        bl.enable_extension.assert_not_called()
        refreshed = install_store.get(
            extension_id="acme.vendor-demo", account_id=DEFAULT_ACCOUNT_ID
        )
        assert refreshed is not None
        assert refreshed.version == "1.0.0"

    def test_extension_runtime_extension_enable_disable_and_uninstall_delegate_to_extension_lifecycle(
        self, tmp_path
    ):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        bl = MagicMock()
        bl.conn = None
        self._persist_scanned_extension_rows(bl)
        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        extensions_root = tmp_path / "extensions"
        bl.list_extensions.side_effect = lambda *args, **kwargs: self._managed_scan_rows(
            extensions_root, "acme.runtime-extension"
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(extensions_root),
        ):
            service.install_extension_archive(
                filename="runtime-extension.zip",
                content=self._unified_extension_zip_bytes(
                    extension_id="acme.runtime-extension",
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        disabled = service.set_enabled_state(
            "acme.runtime-extension",
            version=None,
            enabled=False,
            config={},
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="admin",
                principal_type="admin",
            ),
        ).dump()
        assert disabled["extension"]["state"] == "disabled"

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(extensions_root),
        ):
            enabled = service.set_enabled_state(
                "acme.runtime-extension",
                version=None,
                enabled=True,
                config={},
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            ).dump()
        assert enabled["extension"]["state"] == "active"

        transaction_conn = MagicMock()
        bl.conn = transaction_conn
        transaction_conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

        @contextlib.contextmanager
        def lifecycle_transaction(_bl):
            yield transaction_conn

        service._context._extension_lifecycle_transaction = lifecycle_transaction
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                return_value=self._confirmed_uninstall_plan("acme.runtime-extension"),
            ),
        ):
            removed = service.uninstall_extension(
                "acme.runtime-extension",
                plan_revision="confirmed-revision",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            ).dump()
        assert removed["state"] == "uninstalled"

    def test_unscanned_runtime_install_uninstall_uses_full_host_cleanup(self):
        from core.application.extensions.install import ExtensionInstallStore

        install_store = ExtensionInstallStore()
        install_store.install(
            extension_id="acme.unscanned-runtime",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="install_descriptor_only",
            release_id="release_descriptor_only",
        )
        runtime_store = MagicMock()
        runtime_store._conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            {
                "connection_id": "conn_descriptor_only",
                "account_id": DEFAULT_ACCOUNT_ID,
                "project_id": "project_descriptor_only",
                "secret_refs": {},
            }
        ]
        cursor.fetchone.return_value = None
        bl = MagicMock()
        bl.conn.cursor.return_value.__enter__.return_value = cursor
        bl.get_extension.return_value = None
        bl._table_exists.side_effect = lambda table: (
            table
            in {
                "extension_installs",
                "project_connections",
                "external_effects",
                "external_effect_replays",
                "extension_project_state",
                "secret_refs",
                "extension_releases",
            }
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        self._confirm_database_advisory_lock(bl)

        with (
            patch.object(
                service._projection_service,
                "_extension_runtime_extension_detail",
                return_value=(
                    {"extension_id": "acme.unscanned-runtime", "origin": "imported"},
                    SimpleNamespace(
                        install_id="install_descriptor_only",
                        release_id="release_descriptor_only",
                        descriptor_source="file",
                    ),
                ),
            ),
            patch(
                "core.application.management.command_services_project_connection.cleanup_project_connection_owned_data"
            ) as cleanup_connection,
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                return_value=self._confirmed_uninstall_plan(
                    "acme.unscanned-runtime",
                    install_id="install_descriptor_only",
                    release_id="release_descriptor_only",
                ),
            ),
        ):
            payload = service.uninstall_extension(
                "acme.unscanned-runtime",
                plan_revision="confirmed-revision",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            ).dump()

        assert payload["state"] == "uninstalled"
        cleanup_connection.assert_called_once_with(
            bl,
            connection=cursor.fetchall.return_value[0],
            cur=cursor,
            oauth_revocations=[],
        )
        sql = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
        assert "UPDATE external_effect_replays AS replay" in sql
        assert "replay_reason = 'extension_uninstalled'" in sql
        assert "UPDATE external_effects" in sql
        assert sql.index("UPDATE external_effect_replays AS replay") < sql.index(
            "UPDATE external_effects"
        )
        assert "DELETE FROM extension_project_state" in sql

    def test_uninstall_extension_ignores_bundle_claimed_system_trust(self, tmp_path):
        extensions_root = tmp_path / "extensions"
        system_bundle = extensions_root / ".releases" / "acme_system_demo" / "1.0.0-fixture"
        system_bundle.mkdir(parents=True)
        (extensions_root / "acme_system_demo").symlink_to(
            system_bundle,
            target_is_directory=True,
        )
        (system_bundle / "extension.yaml").write_text(
            "extension_id: acme.system-demo\n", encoding="utf-8"
        )
        (system_bundle / ".fs-extension-install.yaml").write_text(
            "origin: system\nofficial: true\n",
            encoding="utf-8",
        )

        bl = MagicMock()
        bl.conn = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "acme.system-demo",
            "bundle_root": str(system_bundle),
            "manifest_path": str(system_bundle / "extension.yaml"),
            "status": "valid",
        }
        bl._table_exists = MagicMock(
            side_effect=lambda name: name in {"enabled_extensions", "extension_installs"}
        )
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        bl.conn.cursor.return_value.__enter__.return_value = cursor

        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        self._confirm_database_advisory_lock(bl)

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                return_value=self._confirmed_uninstall_plan("acme.system-demo"),
            ),
        ):
            payload = service.uninstall_extension(
                "acme.system-demo",
                plan_revision="confirmed-revision",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
                ),
            ).dump()

        assert payload["state"] == "uninstalled"
        assert not system_bundle.exists()
        assert not (extensions_root / "acme_system_demo").exists()

    def test_uninstall_extension_removes_third_party_bundle(self, tmp_path):
        extensions_root = tmp_path / "extensions"
        vendor_bundle = extensions_root / "acme_vendor_demo"
        release_bundle = extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-checksum"
        release_bundle.mkdir(parents=True)
        (release_bundle / "extension.yaml").write_text(
            "extension_id: acme.vendor-demo\n", encoding="utf-8"
        )
        (release_bundle / ".fs-extension-install.yaml").write_text(
            "origin: third_party\nofficial: false\n",
            encoding="utf-8",
        )
        vendor_bundle.symlink_to(
            release_bundle.relative_to(extensions_root), target_is_directory=True
        )

        bl = MagicMock()
        bl.conn = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "bundle_root": str(release_bundle),
            "manifest_path": str(release_bundle / "extension.yaml"),
            "bundle_id": "bundle_acme_vendor_demo",
            "install_id": "install_acme_vendor_demo",
            "status": "valid",
        }
        bl._table_exists = MagicMock(return_value=True)
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        bl.conn.cursor.return_value.__enter__.return_value = cursor

        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        self._confirm_database_advisory_lock(bl)

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                return_value={
                    "extension_id": "acme.vendor-demo",
                    "install_id": "install_acme_vendor_demo",
                    "release_id": "release_acme_vendor_demo",
                    "revision": "confirmed-revision",
                    "remove": [],
                    "affected": [],
                },
            ),
        ):
            payload = service.uninstall_extension(
                "acme.vendor-demo",
                plan_revision="confirmed-revision",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
                ),
            ).dump()

        assert payload["state"] == "uninstalled"
        assert not vendor_bundle.exists()
        assert not vendor_bundle.is_symlink()
        assert not release_bundle.exists()
        anchor_delete = next(
            call
            for call in cursor.execute.call_args_list
            if "DELETE FROM registry_workflows" in str(call.args[0])
        )
        assert "source_install_id = %s" in str(anchor_delete.args[0])
        assert "projection_mode = 'derived'" in str(anchor_delete.args[0])
        assert anchor_delete.args[1] == ("install_acme_vendor_demo",)

    def test_uninstall_preview_removes_package_release_and_projections_for_every_extension(self):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        install_store.install(
            extension_id="fixture.system-provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="install_system",
            release_id="release_system",
        )
        runtime_store.create_release(
            {
                "release_id": "release_system",
                "extension_id": "fixture.system-provider",
                "release_version": "1.0.0",
                "descriptor": {"extension_id": "fixture.system-provider"},
                "source_kind": "system",
                "source_ref": "fixture.system-provider",
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        )
        runtime_store.upsert_projection(
            {
                "account_id": DEFAULT_ACCOUNT_ID,
                "install_id": "install_system",
                "release_id": "release_system",
                "extension_id": "fixture.system-provider",
                "artifact_kind": "workflow",
                "artifact_id": "workflow_system",
                "projection": {},
            }
        )
        bl = MagicMock()
        bl.conn = None
        bl.get_extension.return_value = {"extension_key": "fixture.system-provider"}
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )

        plan = service.preview_extension_uninstall(
            "fixture.system-provider",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="admin",
                principal_type="admin",
            ),
        )

        # No extension is uninstalled by disabling it in place: its release and
        # projections are removed like any other extension's.
        remove_kinds = {item["kind"] for item in plan["remove"]}
        retained_kinds = {item["kind"] for item in plan["retained"]}
        assert "enabled_state" not in remove_kinds
        assert {"release", "projection"} & retained_kinds == set()

    def test_uninstall_revision_lock_covers_every_revision_inventory_table(self):
        from core.application.extensions.uninstall_plan_service import (
            ExtensionUninstallPlanService,
        )

        bl = MagicMock()
        bl._table_exists.return_value = True
        cur = MagicMock()

        ExtensionUninstallPlanService._lock_extension_uninstall_inventory(bl, cur)

        statement = str(cur.execute.call_args.args[0])
        for table in (
            "extension_entitlements",
            "extension_usage_events",
            "extension_bundles",
            "extension_versions",
            "enabled_extensions",
            "extension_invocation_logs",
            "registry_datasets",
            "dataset_query_templates",
            "registry_workflows",
            "secret_refs",
        ):
            assert table in statement

    def test_sibling_uninstall_validator_locks_install_before_inventory_and_preview(self):
        from core.application.extensions.uninstall_models import UninstallContext
        from core.application.extensions.uninstall_service import ExtensionUninstallService

        order: list[str] = []
        cursor = MagicMock()
        transaction_conn = MagicMock()
        transaction_conn.cursor.return_value.__enter__.return_value = cursor
        plan = MagicMock()
        plan.lock_extension_install_for_uninstall.side_effect = lambda *_a, **_k: (
            order.append("install") or True
        )
        plan.lock_extension_uninstall_inventory.side_effect = lambda *_a, **_k: order.append(
            "inventory"
        )
        confirmed = {
            "extension_id": "acme.vendor-demo",
            "install_id": "install_acme_vendor_demo",
            "revision": "confirmed-revision",
            "affected": [],
        }
        plan.preview_extension_uninstall.side_effect = lambda *_a, **_k: (
            order.append("preview") or dict(confirmed)
        )
        service = object.__new__(ExtensionUninstallService)
        service._plan = plan
        context = UninstallContext(
            extension_id="acme.vendor-demo",
            scope=None,
            resolved_scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            plan_revision="confirmed-revision",
            account_id=DEFAULT_ACCOUNT_ID,
            bl=MagicMock(),
            runtime_store=MagicMock(),
            confirmed_plan=dict(confirmed),
        )

        service._validate_locked_plan(context, transaction_conn)

        assert order == ["install", "inventory", "preview"]
        plan.lock_extension_install_for_uninstall.assert_called_once_with(
            cursor,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="install_acme_vendor_demo",
        )

    def test_sibling_uninstall_validator_rejects_confirmed_plan_without_install_id(self):
        from core.application.extensions.uninstall_models import UninstallContext
        from core.application.extensions.uninstall_service import ExtensionUninstallService

        plan = MagicMock()
        service = object.__new__(ExtensionUninstallService)
        service._plan = plan
        context = UninstallContext(
            extension_id="acme.vendor-demo",
            scope=None,
            resolved_scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            plan_revision="confirmed-revision",
            account_id=DEFAULT_ACCOUNT_ID,
            bl=MagicMock(),
            runtime_store=MagicMock(),
            confirmed_plan={
                "extension_id": "acme.vendor-demo",
                "revision": "confirmed-revision",
                "affected": [],
            },
        )

        _raises_input_8129_1 = MagicMock()
        with pytest.raises(ServiceValidationError, match="missing a usable install_id"):
            service._validate_locked_plan(context, _raises_input_8129_1)

        plan.lock_extension_install_for_uninstall.assert_not_called()
        plan.lock_extension_uninstall_inventory.assert_not_called()
        plan.preview_extension_uninstall.assert_not_called()

    def test_uninstall_revalidates_confirmation_after_inventory_lock(self, tmp_path):
        from core.application.management.errors import ServiceConflictError

        vendor_bundle = tmp_path / "extensions" / "acme_vendor_demo"
        vendor_bundle.mkdir(parents=True)
        (vendor_bundle / "extension.yaml").write_text(
            "extension_id: acme.vendor-demo\n", encoding="utf-8"
        )
        cursor = MagicMock()
        bl = MagicMock()
        bl.conn.cursor.return_value.__enter__.return_value = cursor
        bl.get_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "bundle_root": str(vendor_bundle),
            "install_id": "install_acme_vendor_demo",
            "release_id": "release_acme_vendor_demo",
            "status": "valid",
        }
        bl._table_exists.side_effect = lambda table: (
            table
            in {
                "extension_installs",
                "project_connections",
            }
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        self._confirm_database_advisory_lock(bl)
        confirmed = {
            "extension_id": "acme.vendor-demo",
            "install_id": "install_acme_vendor_demo",
            "release_id": "release_acme_vendor_demo",
            "revision": "confirmed-revision",
            "remove": [],
            "affected": [],
        }
        changed = {**confirmed, "revision": "changed-revision"}

        _raises_input_8180_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                side_effect=[confirmed, changed],
            ),
            pytest.raises(ServiceConflictError, match="impact changed"),
        ):
            service.uninstall_extension(
                "acme.vendor-demo", plan_revision="confirmed-revision", scope=_raises_input_8180_1
            )

        assert vendor_bundle.is_dir()
        sql = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
        assert "LOCK TABLE extension_installs, project_connections" in sql
        assert "DELETE FROM extension_installs" not in sql

    def test_revisioned_uninstall_preserves_required_workflow_dependent(self, tmp_path):
        from core.application.extensions.extension_runtime import (
            ExtensionRegistryProjectionRecord,
        )
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        target = install_store.install(
            extension_id="acme.provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id="release_provider",
        )
        dependent = install_store.install(
            extension_id="acme.consumer",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id="release_consumer",
        )
        runtime_store.upsert_projection(
            ExtensionRegistryProjectionRecord(
                account_id=DEFAULT_ACCOUNT_ID,
                install_id=dependent.install_id,
                release_id="release_consumer",
                extension_id="acme.consumer",
                artifact_kind="workflow",
                artifact_id="consume_provider_data",
                projection={
                    "workflow_id": "consume_provider_data",
                    "dependencies": {
                        "extensions": [{"extension_id": "acme.provider", "required": True}]
                    },
                },
            )
        )
        extensions_root = tmp_path / "extensions"
        release_bundle = extensions_root / ".releases" / "acme_provider" / "1.0.0-fixture"
        release_bundle.mkdir(parents=True)
        (release_bundle / "extension.yaml").write_text(
            "extension_id: acme.provider\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        (extensions_root / "acme_provider").symlink_to(
            release_bundle,
            target_is_directory=True,
        )
        bl = MagicMock()
        bl.conn = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "acme.provider",
            "bundle_id": target.install_id,
            "bundle_root": str(release_bundle),
            "status": "valid",
        }
        bl._table_exists.return_value = False
        bl.conn.cursor.return_value.__enter__.return_value = MagicMock()
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        self._confirm_database_advisory_lock(bl)

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(extensions_root),
        ):
            plan = service.preview_extension_uninstall(
                "acme.provider",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )
            assert plan["affected"] == [
                {
                    "kind": "workflow",
                    "id": "consume_provider_data",
                    "label": "Workflow",
                    "display_name": "Consume provider data",
                    "owner_install_id": dependent.install_id,
                    "owner_account_id": DEFAULT_ACCOUNT_ID,
                    "action": "preserve_and_disable",
                    "reason": "Missing required extension: acme.provider",
                    "explanation": (
                        "Needs acme.provider; it stops working until that extension is "
                        "installed again."
                    ),
                }
            ]

            service.uninstall_extension(
                "acme.provider",
                plan_revision=plan["revision"],
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        assert (
            install_store.get(extension_id="acme.provider", account_id=DEFAULT_ACCOUNT_ID) is None
        )
        rows = runtime_store.list_projections(
            account_id=DEFAULT_ACCOUNT_ID,
            only_active=False,
        )
        consumer = next(row for row in rows if row.install_id == dependent.install_id)
        assert consumer.projection["extension_dependency_availability"] == {
            "availability_status": "unavailable",
            "availability_reason_code": "required_extension_uninstalled",
            "availability_reason_text": "Missing required extension: acme.provider",
            "missing_extensions": ["acme.provider"],
            "new_runs_blocked": True,
            "visible_in_catalog": True,
        }

    def test_revisioned_uninstall_rejects_descriptor_only_required_dependent(self, tmp_path):
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        install_store = ExtensionInstallStore()
        runtime_store = ExtensionRuntimeStore()
        target = install_store.install(
            extension_id="acme.provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id="release_provider",
        )
        dependent = install_store.install(
            extension_id="acme.consumer",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id="release_consumer",
        )
        runtime_store.create_release(
            ExtensionReleaseRecord(
                release_id="release_consumer",
                extension_id="acme.consumer",
                descriptor={
                    "extension_id": "acme.consumer",
                    "version": "1.0.0",
                    "dependencies": {
                        "extensions": [{"extension_id": "acme.provider", "required": True}]
                    },
                },
            )
        )
        extensions_root = tmp_path / "extensions"
        release_bundle = extensions_root / ".releases" / "acme_provider" / "1.0.0-fixture"
        release_bundle.mkdir(parents=True)
        (release_bundle / "extension.yaml").write_text(
            "extension_id: acme.provider\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        (extensions_root / "acme_provider").symlink_to(
            release_bundle,
            target_is_directory=True,
        )
        bl = MagicMock()
        bl.conn = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "acme.provider",
            "bundle_id": target.install_id,
            "bundle_root": str(release_bundle),
            "status": "valid",
        }
        bl._table_exists.return_value = False
        bl.conn.cursor.return_value.__enter__.return_value = MagicMock()
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            get_extension_install_store=lambda: install_store,
            get_extension_runtime_store=lambda: runtime_store,
        )
        self._confirm_database_advisory_lock(bl)
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin",
            principal_type="admin",
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(extensions_root),
        ):
            plan = service.preview_extension_uninstall("acme.provider", scope=scope)

            with pytest.raises(ServiceValidationError, match="required dependents"):
                service.uninstall_extension(
                    "acme.provider",
                    plan_revision=plan["revision"],
                    scope=scope,
                )

        assert (
            install_store.get(extension_id="acme.provider", account_id=DEFAULT_ACCOUNT_ID)
            is not None
        )
        assert (
            install_store.get(extension_id="acme.consumer", account_id=DEFAULT_ACCOUNT_ID)
            == dependent
        )

    def test_uninstall_dependency_keeps_projection_available_when_another_install_matches_range(
        self,
    ):
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.uninstall_plan_service import (
            _dependency_has_available_alternative_install,
        )

        store = ExtensionInstallStore()
        removed = store.install(
            extension_id="acme.provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id="pi_removed_provider",
        )
        store.install(
            extension_id="acme.provider",
            version="2.1.0",
            account_id="acc_alternate",
            install_id="pi_alternate_provider",
            visibility="global",
        )

        assert _dependency_has_available_alternative_install(
            install_store=store,
            account_id=DEFAULT_ACCOUNT_ID,
            removed_install_id=removed.install_id,
            dependency={
                "extension_id": "acme.provider",
                "required": True,
                "min_version": "2.0.0",
                "max_version_exclusive": "3.0.0",
            },
        )

    def test_confirmed_uninstall_keeps_bundle_until_database_transaction_commits(self, tmp_path):
        from contextlib import contextmanager

        extensions_root = tmp_path / "extensions"
        vendor_bundle = extensions_root / "acme_vendor_demo"
        release_bundle = extensions_root / ".releases" / "acme_vendor_demo" / "1.0.0-checksum"
        release_bundle.mkdir(parents=True)
        (release_bundle / "extension.yaml").write_text(
            "extension_id: acme.vendor-demo\n", encoding="utf-8"
        )
        vendor_bundle.symlink_to(
            release_bundle.relative_to(extensions_root), target_is_directory=True
        )

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, statement, _params=None):
                if "DELETE FROM extension_installs" in str(statement):
                    assert vendor_bundle.is_symlink()
                    assert release_bundle.exists()
                    raise RuntimeError("forced uninstall failure")

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        class _TransactionConnection:
            def cursor(self):
                return _Cursor()

        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "acme.vendor-demo",
            "bundle_root": str(release_bundle),
            "bundle_id": "install-vendor-demo",
            "status": "valid",
        }
        bl._table_exists.side_effect = lambda table: (
            table
            in {
                "extension_callback_registrations",
                "extension_installs",
            }
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        self._confirm_database_advisory_lock(bl)

        @contextmanager
        def transaction(_bl):
            yield _TransactionConnection()

        service._context._extension_lifecycle_transaction = transaction

        _raises_input_8517_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(extensions_root),
            ),
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                return_value=self._confirmed_uninstall_plan("acme.vendor-demo"),
            ),
            pytest.raises(RuntimeError, match="forced uninstall failure"),
        ):
            service.uninstall_extension(
                "acme.vendor-demo", plan_revision="confirmed-revision", scope=_raises_input_8517_1
            )

        assert vendor_bundle.is_dir()
        assert vendor_bundle.is_symlink()
        assert release_bundle.is_dir()
        assert (vendor_bundle / "extension.yaml").is_file()

    @pytest.mark.parametrize("git_checkout", [False, True])
    def test_uninstall_extension_removes_an_orphaned_scan_folder_unless_it_is_a_checkout(
        self, tmp_path, git_checkout
    ):
        """A folder left at the managed root is registered again by the next scan.

        That is what broke Install for Ollama on the demo: uninstall left the
        folder, the scan re-created the record, and Install took the update
        path. The folder goes with the install; a developer's git checkout
        placed in the extensions root is theirs and stays.
        """
        vendor_bundle = tmp_path / "extensions" / "acme_orphan_demo"
        vendor_bundle.mkdir(parents=True)
        (vendor_bundle / "extension.yaml").write_text(
            "extension_id: acme.orphan-demo\n", encoding="utf-8"
        )
        (vendor_bundle / ".fs-extension-install.yaml").write_text(
            "origin: third_party\nofficial: false\n",
            encoding="utf-8",
        )
        if git_checkout:
            (vendor_bundle / ".git").mkdir()

        bl = MagicMock()
        bl.conn = MagicMock()
        bl.get_extension.return_value = None
        bl._table_exists = MagicMock(return_value=True)
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        bl.conn.cursor.return_value.__enter__.return_value = cursor

        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        self._confirm_database_advisory_lock(bl)

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch.object(
                service._projection_service,
                "_extension_runtime_extension_detail",
                return_value=(None, None),
            ),
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                return_value=self._confirmed_uninstall_plan("acme.orphan-demo"),
            ),
        ):
            payload = service.uninstall_extension(
                "acme.orphan-demo",
                plan_revision="confirmed-revision",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
                ),
            ).dump()

        assert payload["state"] == "uninstalled"
        assert vendor_bundle.is_dir() is git_checkout
        assert ("bundle_files" in payload["removed_data"]) is not git_checkout
        calls_sql = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
        assert "DELETE FROM extension_installs" in calls_sql
        assert "DELETE FROM extension_releases" in calls_sql

    def test_orphaned_scan_uninstall_rolls_back_pooled_writes_and_restores_bundle(self, tmp_path):
        from core.infrastructure.db.db_pool import PooledConnectionProxy

        vendor_bundle = tmp_path / "extensions" / "acme_orphan_demo"
        vendor_bundle.mkdir(parents=True)
        (vendor_bundle / "extension.yaml").write_text(
            "extension_id: acme.orphan-demo\n", encoding="utf-8"
        )

        class _Cursor:
            def __init__(self, connection):
                self.connection = connection
                self.statement = ""

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, statement, _params=None):
                sql = str(statement)
                self.statement = sql
                self.connection.statements.append(sql)
                if "DELETE FROM extension_installs" in sql:
                    raise RuntimeError("fault between orphan cleanup writes")

            def fetchone(self):
                if (
                    "pg_try_advisory_lock" in self.statement
                    or "pg_advisory_unlock" in self.statement
                ):
                    return (True,)
                return None

        class _Connection:
            def __init__(self):
                self.autocommit = True
                self.statements = []
                self.commits = 0
                self.rollbacks = 0

            def cursor(self):
                return _Cursor(self)

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

        class _Pool:
            def __init__(self):
                self.connection = _Connection()
                self.borrowed = 0
                self.returned = []

            def getconn(self):
                self.borrowed += 1
                return self.connection

            def putconn(self, connection, close=False):
                self.returned.append((connection, close))

        pool = _Pool()
        bl = MagicMock()
        bl.conn = PooledConnectionProxy(pool)
        bl.get_extension.return_value = None
        bl._table_exists.side_effect = lambda table: (
            table
            in {
                "project_connections",
                "extension_installs",
                "extension_releases",
            }
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_8690_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value=str(tmp_path / "extensions"),
            ),
            patch.object(
                service._projection_service,
                "_extension_runtime_extension_detail",
                return_value=(None, None),
            ),
            patch.object(
                service._uninstall_plan_service,
                "preview_extension_uninstall",
                return_value=self._confirmed_uninstall_plan("acme.orphan-demo"),
            ),
            pytest.raises(RuntimeError, match="fault between orphan cleanup writes"),
        ):
            service.uninstall_extension(
                "acme.orphan-demo", plan_revision="confirmed-revision", scope=_raises_input_8690_1
            )

        assert vendor_bundle.is_dir()
        # Resolving the current install, fencing lifecycle mutations, and running
        # the transaction that is forced to roll back each borrow a session.
        assert pool.borrowed == 3
        assert pool.connection.commits == 0
        assert pool.connection.rollbacks == 1
        assert pool.returned == [
            (pool.connection, True),
            (pool.connection, False),
            (pool.connection, False),
        ]

    def test_install_extension_archive_rejects_non_default_account_scope(self, tmp_path):
        bl = MagicMock()
        bl.conn = None
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        _raises_input_8741_1 = self._extension_zip_bytes()
        _raises_input_8741_2 = ScopeContext(
            account_id="acc_other", actor_id="admin", principal_type="admin"
        )
        with pytest.raises(ServiceValidationError, match="deployment-scoped"):
            service.install_extension_archive(
                filename="vendor-demo.zip", content=_raises_input_8741_1, scope=_raises_input_8741_2
            )

    def test_execute_action_merges_target_and_input(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        extension = {
            "extension_key": "fixture.notification-provider",
            "extension_install_id": "install-fixture-notifications",
            "enabled": True,
            "action_definitions": [],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "platform",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            scope="row",
            item_ids=["route-1"],
            values={"chat_id": "-100"},
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded"},
        ) as run_action:
            payload = service.execute_action("fixture_events", "toggle_route", body).dump()

        assert payload["action"]["status"] == "succeeded"
        kwargs = run_action.call_args.kwargs
        assert kwargs["target"]["scope"] == "row"
        assert kwargs["target"]["item_ids"] == ["route-1"]
        assert kwargs["input_payload"]["chat_id"] == "-100"

    @pytest.mark.parametrize("invocation_status", ["succeeded", "failed"])
    def test_execute_legacy_action_drops_sensitive_audit_input_without_changing_dispatch(
        self, invocation_status
    ):
        bl = MagicMock()
        extension = {
            "extension_key": "fixture.notification-provider",
            "extension_install_id": "install-fixture-notifications",
            "enabled": True,
            "action_definitions": [],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "platform",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            input={
                "message": "hello",
                "apiKey": "legacy-api-key",  # pragma: allowlist secret
                "private_key": "legacy-private-key",  # pragma: allowlist secret
                "dsn": "postgresql://private.example/db",
                "tls": {
                    "client_certificate": "legacy-certificate",
                    "key_material": [
                        {"cert": "legacy-cert"},
                        {"key": "legacy-key"},
                    ],
                },
                "nested": [
                    {"api_key": "nested-api-key"},  # pragma: allowlist secret
                    {"certificate": "nested-certificate"},
                ],
            },
        )
        invocation = {
            "status": invocation_status,
            "detail": "provider_failed" if invocation_status == "failed" else "",
            "response": {
                "debug": {"api_key": "response-api-key"},  # pragma: allowlist secret
                "materials": [
                    {"privateKey": "response-private-key"}  # pragma: allowlist secret
                ],
            },
        }

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=invocation,
        ) as run_action:
            if invocation_status == "failed":
                with pytest.raises(ServiceError, match="Extension action failed"):
                    service.execute_action("fixture_events", "toggle_route", body)
            else:
                service.execute_action("fixture_events", "toggle_route", body)

        assert run_action.call_args.kwargs["input_payload"] == body.input
        assert bl.log_extension_invocation.call_args.kwargs["request"] == {}
        logged_response = bl.log_extension_invocation.call_args.kwargs["response"]
        assert logged_response == {"status": invocation_status}
        assert "legacy-api-key" not in str(bl.log_extension_invocation.call_args)
        assert "response-private-key" not in str(bl.log_extension_invocation.call_args)

    def test_execute_action_hydrates_declared_project_state_like_workflow_runtime(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        extension = {
            "extension_key": "acme.outgoing-email",
            "extension_install_id": "install-email",
            "enabled": True,
            "action_definitions": [],
            "project_state": {
                "reads": [
                    {
                        "namespace": "templates",
                        "state_key_from_input": "template_id",
                        "state_key_prefix": "template:",
                    }
                ]
            },
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 400,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            values={"template_id": "welcome"},
        )
        template = {
            "namespace": "templates",
            "state_key": "template:welcome",
            "value_jsonb": {"subject": "Welcome", "body_text": "Hello"},
            "revision": 3,
            "connection_id": None,
        }

        with (
            patch(
                "core.application.extensions.extension_project_state.get_state",
                return_value=template,
            ) as get_state,
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={"status": "succeeded", "response": {"ok": True}},
            ) as run_action,
        ):
            service.execute_action(
                "acme.outgoing-email",
                "send_email",
                body,
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                    project_id="7",
                ),
            )

        get_state.assert_called_once_with(
            bl,
            account_id=DEFAULT_ACCOUNT_ID,
            project_id="7",
            extension_id="acme.outgoing-email",
            extension_install_id="install-email",
            namespace="templates",
            state_key="template:welcome",
            connection_id=None,
        )
        assert run_action.call_args.kwargs["target"]["project_state"] == {
            "items": [
                {
                    "namespace": "templates",
                    "state_key": "template:welcome",
                    "value": {"subject": "Welcome", "body_text": "Hello"},
                    "revision": 3,
                    "connection_id": None,
                }
            ]
        }

    def test_execute_action_denies_out_of_scope_project(self):
        # FS-DEEP-CAND-040: a restricted principal supplying a project_id outside
        # its allowed_project_ids must be rejected before the project-mutating
        # action is dispatched.
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        extension = {
            "extension_key": "fixture.notification-provider",
            "extension_install_id": "install-fixture-notifications",
            "enabled": True,
            "action_definitions": [],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "platform",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(context={"project_id": "7"}, values={"chat_id": "-100"})

        _raises_input_8956_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="svc-restricted",
            principal_type="service_account",
            project_id="7",
            allowed_project_ids=frozenset({"99"}),
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            ) as run_action,
            pytest.raises(ServiceForbiddenError),
        ):
            service.execute_action(
                "fixture_events", "save_config", body, scope=_raises_input_8956_1
            )
        run_action.assert_not_called()

    def test_execute_action_allows_in_scope_project(self):
        # FS-DEEP-CAND-040: same-account, in-allowlist project dispatches normally.
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        extension = {
            "extension_key": "fixture.notification-provider",
            "extension_install_id": "install-fixture-notifications",
            "enabled": True,
            "action_definitions": [],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "platform",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(context={"project_id": "7"}, values={"chat_id": "-100"})

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action:
            payload = service.execute_action(
                "fixture_events",
                "save_config",
                body,
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="svc-restricted",
                    principal_type="service_account",
                    project_id="7",
                    allowed_project_ids=frozenset({"7"}),
                ),
            ).dump()
        assert payload["action"]["status"] == "succeeded"
        run_action.assert_called_once()

    def test_execute_action_resolves_project_from_the_project_store(self):
        identity_store = MagicMock(spec=["log_extension_invocation"])
        project_getter = MagicMock(return_value={"id": "7", "account_id": DEFAULT_ACCOUNT_ID})
        extension = {
            "extension_key": "fixture.notification-provider",
            "extension_install_id": "install-fixture-notifications",
            "enabled": True,
            "action_definitions": [],
        }
        service = ExtensionBindingService(
            get_logger=lambda: identity_store,
            get_operation_store=lambda: identity_store,
            get_project_by_id=project_getter,
            resolve_extension_runtime_record=lambda extension_id: (extension, identity_store),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "platform",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(context={"project_id": "7"})

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action:
            payload = service.execute_action(
                "fixture_events",
                "save_config",
                body,
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                    project_id="7",
                ),
            ).dump()

        assert payload["action"]["status"] == "succeeded"
        project_getter.assert_called_once_with("7")
        run_action.assert_called_once()

    def test_execute_action_external_effect_resolves_connection_server_side(self, monkeypatch):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_connection.return_value = {
            "connection_id": "conn_external",
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
            "extension_id": "example.host-fixture",
            "extension_install_id": "install-host-fixture",
            "enabled": True,
            "connection_config": {
                "provider": "example_api",
                "client_id": "api-client",
                "endpoint": "https://api.example.com/records",
            },
            "secret_refs": {"refs": {"api_token": "project_connection:conn_external:api_token"}},
        }
        monkeypatch.setattr(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            lambda *args, **kwargs: {"api_token": "resolved-secret"},
        )
        captured_claim: dict[str, Any] = {}

        def _claim_external_effect(*args, **kwargs):
            captured_claim.update(kwargs)
            return {
                "effect": {"effect_id": "eff_action_1", "status": "claimed"},
                "should_spawn": True,
                "suppressed": False,
                "deduped": False,
            }

        extension = {
            "extension_key": "example.host-fixture",
            "extension_install_id": "install-host-fixture",
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
            "operation_manifest": [
                {
                    "operation_id": "publish_record",
                    "operation_kind": "action",
                    "inputs": [
                        {"name": "record_id"},
                        {"name": "title"},
                        {"name": "content"},
                    ],
                    "outputs": [{"name": "ok"}],
                }
            ],
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "test_mode_behavior": "suppress",
                }
            ],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_external"},
            input={"record_id": "record-a", "title": "Hi", "content": "Body"},
        )

        with (
            patch(
                "core.application.extensions.external_effects.cleanup_stale_external_effects",
                return_value={},
            ),
            patch(
                "core.application.extensions.external_effects.claim_external_effect",
                side_effect=_claim_external_effect,
            ),
            patch(
                "core.application.extensions.external_effects.mark_external_effect_running",
                return_value={"effect_id": "eff_action_1", "status": "running"},
            ),
            patch(
                "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
                return_value={
                    "effect_id": "eff_action_1",
                    "status": "succeeded",
                    "__confirmed_replay_invocation": {
                        "status": "succeeded",
                        "response": {"ok": True},
                    },
                },
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={"status": "succeeded", "response": {"ok": True}},
            ) as run_action,
        ):
            payload = service.execute_action(
                "example.host-fixture",
                "publish_record",
                body,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
                idempotency_key="direct-action-intent-1",
            ).dump()

        assert payload["action"]["status"] == "succeeded"
        bl.get_project_connection.assert_called_once_with(
            project_id="7",
            connection_id="conn_external",
            extension_id="example.host-fixture",
            enabled_only=True,
        )
        target = run_action.call_args.kwargs["target"]
        assert target["connection_id"] == "conn_external"
        assert target["connection"]["config"]["endpoint"] == "https://api.example.com/records"
        assert target["connection"]["secrets"] == {"api_token": "resolved-secret"}
        assert "api_token" not in target["connection"]["config"]
        assert captured_claim["connection_id"] == "conn_external"
        assert captured_claim["project_id"] == "7"
        assert captured_claim["idempotency_key_override"]
        assert "direct-action-intent-1" not in repr(captured_claim)
        direct_intent = captured_claim["request_payload"]["__direct_action_intent"]
        assert direct_intent["target"] == {"connection_id": "conn_external"}
        assert direct_intent["input"] == {
            "record_id": "record-a",
            "title": "Hi",
            "content": "Body",
        }
        assert direct_intent["artifact_inputs"] == []
        assert direct_intent["artifact_outputs"] == []
        assert direct_intent["dataset_inputs"] == []
        assert direct_intent["dataset_outputs"] == []
        assert "resolved-secret" not in repr(direct_intent)
        assert "connection_config" not in repr(direct_intent)
        assert run_action.call_args.kwargs["runtime_context"]["connection_id"] == "conn_external"
        assert (
            run_action.call_args.kwargs["runtime_context"]["external_effect_id"] == "eff_action_1"
        )

    def test_execute_action_non_effect_resolves_connection_server_side(self, monkeypatch):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_connection.return_value = {
            "connection_id": "conn_secure_drop",
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
            "extension_id": "example.connection-fixture",
            "extension_install_id": "install-connection-fixture",
            "enabled": True,
            "connection_type": "secure_drop",
            "connection_config": {
                "endpoint": "https://files.example.com",
                "port": 443,
            },
            "secret_refs": {"refs": {"api_token": "project_connection:conn_secure_drop:api_token"}},
        }
        monkeypatch.setattr(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            lambda *args, **kwargs: {"api_token": "resolved-secret"},
        )
        extension = {
            "extension_key": "example.connection-fixture",
            "extension_install_id": "install-connection-fixture",
            "enabled": True,
            "action_definitions": [{"action_id": "validate_connection"}],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_secure_drop"},
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded"},
        ) as run_action:
            payload = service.execute_action(
                "example.connection-fixture",
                "validate_connection",
                body,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
            ).dump()

        assert payload["action"]["status"] == "succeeded"
        target = run_action.call_args.kwargs["target"]
        assert target["connection_id"] == "conn_secure_drop"
        assert target["connection"]["connection_type"] == "secure_drop"
        assert target["connection"]["config"]["endpoint"] == "https://files.example.com"
        assert target["connection"]["secrets"] == {"api_token": "resolved-secret"}
        assert run_action.call_args.kwargs["runtime_context"]["connection_id"] == "conn_secure_drop"

    @pytest.mark.parametrize("invocation_status", ["succeeded", "failed"])
    def test_execute_contract_v2_action_filters_form_payload_and_redacts_connection_audit(
        self,
        monkeypatch,
        invocation_status,
    ):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_connection.return_value = {
            "connection_id": "conn_imap",
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
            "extension_id": "acme.mailbox",
            "extension_install_id": "install-acme-mailbox",
            "enabled": True,
            "connection_type": "imap_mailbox",
            "connection_config": {
                "imap_host": "mail.private.example",
                "username": "private-user@example.test",
            },
            "secret_refs": {  # pragma: allowlist secret
                "refs": {  # pragma: allowlist secret
                    "password": "project_connection:conn_imap:password"  # pragma: allowlist secret
                }
            },
        }
        monkeypatch.setattr(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            lambda *args, **kwargs: {  # pragma: allowlist secret
                "password": "resolved-mail-password"  # pragma: allowlist secret
            },
        )
        extension = {
            "extension_key": "acme.mailbox",
            "extension_install_id": "install-acme-mailbox",
            "enabled": True,
            "action_definitions": [{"action_id": "test_connection"}],
            "compiled_manifest": {
                "extension_contract_v2_normalized": {
                    "operation_manifest": [
                        {
                            "operation_id": "test_connection",
                            "operation_kind": "source",
                            "connection_type_ids": ["imap_mailbox"],
                            "inputs": [
                                {
                                    "name": "connection_ref",
                                    "value_type": "connection_ref",
                                    "required": True,
                                }
                            ],
                        }
                    ]
                }
            },
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_imap"},
            input={"password": "input-password"},  # pragma: allowlist secret
            values={
                "connection_id": "conn_imap",
                "imap_host": "form.private.example",
                "username": "form-user@example.test",
                "password": "form-password",  # pragma: allowlist secret
            },
            data={  # pragma: allowlist secret
                "secret_refs": {  # pragma: allowlist secret
                    "password": "form-secret-ref"  # pragma: allowlist secret
                }
            },
        )
        invocation = {
            "status": invocation_status,
            "detail": "invalid_connection" if invocation_status == "failed" else "",
        }

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=invocation,
        ) as run_action:
            if invocation_status == "failed":
                _raises_input_9347_1 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7")
                with pytest.raises(ServiceError, match="Extension action failed"):
                    service.execute_action(
                        "acme.mailbox", "test_connection", body, scope=_raises_input_9347_1
                    )
            else:
                service.execute_action(
                    "acme.mailbox",
                    "test_connection",
                    body,
                    scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
                )

        assert run_action.call_args.kwargs["input_payload"] == {"connection_ref": "conn_imap"}
        logged_request = bl.log_extension_invocation.call_args.kwargs["request"]
        assert logged_request["input"] == {"connection_ref": "conn_imap"}
        assert "values" not in logged_request
        assert "data" not in logged_request
        serialized_log = json.dumps(logged_request, sort_keys=True)
        for sensitive_value in (
            "resolved-mail-password",
            "project_connection:conn_imap:password",
            "mail.private.example",
            "private-user@example.test",
            "form.private.example",
            "form-user@example.test",
            "input-password",
            "form-password",
            "form-secret-ref",
        ):
            assert sensitive_value not in serialized_log

    def test_execute_contract_v2_action_uses_declared_runtime_action_parameters(
        self,
        monkeypatch,
    ):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_connection.return_value = {
            "connection_id": "conn_imap",
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
            "extension_id": "acme.mailbox",
            "extension_install_id": "install-acme-mailbox",
            "enabled": True,
            "connection_type": "imap_mailbox",
            "connection_config": {
                "provider_preset": "manual",
                "imap_host": "mail.example.test",
                "imap_port": 993,
                "tls_mode": "ssl",
                "username": "saved-user@example.test",
            },
            "secret_refs": {
                "refs": {
                    "password": "project_connection:conn_imap:password"  # pragma: allowlist secret
                }  # pragma: allowlist secret
            },
        }
        monkeypatch.setattr(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            lambda *args, **kwargs: {"password": "resolved-password"},  # pragma: allowlist secret
        )
        extension = {
            "extension_key": "acme.mailbox",
            "extension_install_id": "install-acme-mailbox",
            "manifest_version": 2,
            "enabled": True,
            "action_definitions": [
                {
                    "action_id": "test_connection",
                    "parameters": [
                        {
                            "name": "connection_ref",
                            "type": "connection_ref",
                            "required": True,
                        }
                    ],
                }
            ],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_imap"},
            values={
                "connection_ref": "conn_imap",
                "connection_id": "conn_imap",
                "provider_preset": "manual",
                "imap_host": "form.example.test",
                "imap_port": 993,
                "tls_mode": "ssl",
                "username": "form-user@example.test",
                "password": "form-password",  # pragma: allowlist secret
            },
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action:
            service.execute_action(
                "acme.mailbox",
                "test_connection",
                body,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
            )

        kwargs = run_action.call_args.kwargs
        assert kwargs["input_payload"] == {"connection_ref": "conn_imap"}
        assert kwargs["target"]["connection_id"] == "conn_imap"
        assert kwargs["target"]["connection"]["config"]["username"] == ("saved-user@example.test")
        assert kwargs["runtime_context"]["project_id"] == "7"
        assert kwargs["runtime_context"]["connection_id"] == "conn_imap"
        bl.get_project_connection.assert_called_once_with(
            project_id="7",
            connection_id="conn_imap",
            extension_id="acme.mailbox",
            enabled_only=True,
        )

    # ------------------------------------------------------------------
    # Audit policy is declared by the extension and applied by Core.
    #
    # Core must not know which extension handles sensitive material, nor what
    # that material is called, so these use a synthetic `acme.*` bundle whose
    # manifest declares its own policy. A real bundle's own suite proves that it
    # declares the right one.
    # ------------------------------------------------------------------

    @staticmethod
    def _synthetic_audit_extension(action_id, *, audit):
        return {
            "extension_key": "acme.sample-connector",
            "extension_install_id": "install-acme-sample-connector",
            "enabled": True,
            "action_definitions": [
                {"action_id": action_id, **({"audit": audit} if audit is not None else {})}
            ],
        }

    def _synthetic_audit_service(self, bl, extension):
        return ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

    @staticmethod
    def _synthetic_audit_store(monkeypatch, *, secret="acme-secret"):  # pragma: allowlist secret
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_connection.return_value = {
            "connection_id": "conn_acme",
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
            "extension_id": "acme.sample-connector",
            "extension_install_id": "install-acme-sample-connector",
            "enabled": True,
            "connection_type": "acme_record_source",
            "connection_config": {"acme_host": "records.example.test"},
            "secret_refs": {"refs": {"password": "acme-password-ref"}},  # pragma: allowlist secret
        }
        monkeypatch.setattr(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            lambda *args, **kwargs: {"password": secret},
        )
        return bl

    @pytest.mark.parametrize("action_id", ["search_records", "fetch_record"])
    def test_declared_result_fields_are_kept_out_of_the_audit_record(self, monkeypatch, action_id):
        """The caller still sees the payload; the audit record never does."""
        bl = self._synthetic_audit_store(monkeypatch)
        extension = self._synthetic_audit_extension(
            action_id,
            audit={"redact_result_fields": ["payload", "note", "sender", "recipient", "headers"]},
        )
        service = self._synthetic_audit_service(bl, extension)
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_acme"},
            input={
                "connection_ref": "conn_acme",
                "collection": "DEFAULT",
                "filters": {
                    "note": "AUDIT_REQUEST_NOTE_SENTINEL",
                    "sender": "audit-request-sender@example.test",
                    "payload": "AUDIT_REQUEST_PAYLOAD_SENTINEL",
                },
                "item_id": 41,
            },
        )
        invocation = {
            "status": "succeeded",
            "response": {
                "result": {
                    "record": {
                        "item_id": 41,
                        "note": "AUDIT_RESPONSE_NOTE_SENTINEL",
                        "sender": "audit-response-sender@example.test",
                        "recipient": "audit-response-recipient@example.test",
                        "payload": "AUDIT_RESPONSE_PAYLOAD_SENTINEL",
                        "headers": {"X-Private": "AUDIT_RAW_HEADER_SENTINEL"},
                    }
                }
            },
        }

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=invocation,
        ):
            result = service.execute_action(
                "acme.sample-connector",
                action_id,
                body,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
            ).dump()

        assert result["action"]["response"]["result"]["record"]["note"] == (
            "AUDIT_RESPONSE_NOTE_SENTINEL"
        )
        logged = bl.log_extension_invocation.call_args.kwargs
        assert logged["response"]["status"] == "succeeded"
        assert logged["response"]["response"]["result"]["record"]["item_id"] == 41
        serialized_log = json.dumps(
            {"request": logged["request"], "response": logged["response"]},
            sort_keys=True,
        )
        for sensitive_value in (
            "AUDIT_REQUEST_NOTE_SENTINEL",
            "audit-request-sender@example.test",
            "AUDIT_REQUEST_PAYLOAD_SENTINEL",
            "AUDIT_RESPONSE_NOTE_SENTINEL",
            "audit-response-sender@example.test",
            "audit-response-recipient@example.test",
            "AUDIT_RESPONSE_PAYLOAD_SENTINEL",
            "AUDIT_RAW_HEADER_SENTINEL",
        ):
            assert sensitive_value not in serialized_log

    def test_action_without_declared_policy_uses_host_audit_projection(self, monkeypatch):
        """No action policy adds redaction beyond the closed host audit projection."""
        bl = self._synthetic_audit_store(monkeypatch)
        extension = self._synthetic_audit_extension("fetch_record", audit=None)
        service = self._synthetic_audit_service(bl, extension)
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_acme"},
            input={"connection_ref": "conn_acme"},
        )
        invocation = {
            "status": "succeeded",
            "response": {
                "result": {
                    "record": {
                        "item_id": 41,
                        "note": "UNDECLARED_NOTE_SENTINEL",
                    }
                }
            },
        }

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=invocation,
        ):
            service.execute_action(
                "acme.sample-connector",
                "fetch_record",
                body,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
            )

        logged = bl.log_extension_invocation.call_args.kwargs
        assert logged["response"]["response"]["result"]["record"] == {"item_id": 41}

    @pytest.mark.parametrize("unsafe_response_key", ["raw_stdout", "value"])
    def test_a_declared_failure_projection_keeps_only_safe_structured_fields(
        self,
        monkeypatch,
        unsafe_response_key,
    ):
        """A failed invocation keeps protocol facts plus the declared row identifier."""
        hydrated_password = "HYDRATED_ACME_PASSWORD_SENTINEL"  # pragma: allowlist secret
        bl = self._synthetic_audit_store(monkeypatch, secret=hydrated_password)
        extension = self._synthetic_audit_extension(
            "fetch_record",
            audit={"failure_projection": {"keep_row_fields": ["item_id"]}},
        )
        service = self._synthetic_audit_service(bl, extension)
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_acme"},
            input={"connection_ref": "conn_acme", "collection": "DEFAULT", "item_id": 41},
        )
        invocation = {
            "status": "failed",
            "detail": "AUDIT_FAILURE_DETAIL_SENTINEL",
            "stdout": "AUDIT_FAILURE_BODY_STDOUT_SENTINEL",
            "stderr": "AUDIT_FAILURE_STDERR_SENTINEL",
            "response": {
                "ok": False,
                "error": "AUDIT_FAILURE_RESPONSE_ERROR_SENTINEL",
                "error_code": "authentication_failed",
                unsafe_response_key: f"AUDIT_FAILURE_{unsafe_response_key.upper()}_SENTINEL",
                "message": "AUDIT_FAILURE_RESPONSE_MESSAGE_SENTINEL",
                "errors": [
                    {
                        "item_id": 41,
                        "status": "failed",
                        "code": "authentication_failed",
                        "detail": "AUDIT_FAILURE_DETAIL_SENTINEL",
                    }
                ],
                "result": {
                    "collection": "AUDIT_FAILURE_RESULT_COLLECTION_SENTINEL",
                    "diagnostic": "AUDIT_FAILURE_RESULT_DIAGNOSTIC_SENTINEL",
                    "results": [
                        {
                            "item_id": 41,
                            "status": "failed",
                            "error_code": "authentication_failed",
                            "note": "AUDIT_FAILURE_BODY_MESSAGE_SENTINEL",
                        }
                    ],
                    "external_effect_status": "timeout_unknown",
                    "definitely_no_external_effect": False,
                },
                "external_effect_status": "timeout_unknown",
                "definitely_no_external_effect": False,
            },
            "errors": [
                {
                    "code": "authentication_failed",
                    "message": "AUDIT_FAILURE_BODY_MESSAGE_SENTINEL",
                }
            ],
        }

        _raises_input_9699_1 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7")
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value=invocation,
            ),
            pytest.raises(ServiceError, match="Extension action failed"),
        ):
            service.execute_action(
                "acme.sample-connector", "fetch_record", body, scope=_raises_input_9699_1
            )

        logged = bl.log_extension_invocation.call_args.kwargs
        assert logged["response"] == {
            "status": "failed",
            "response": {
                "ok": False,
                "error_code": "authentication_failed",
                "errors": [{"code": "authentication_failed"}],
                "result": {
                    "results": [
                        {
                            "item_id": 41,
                            "status": "failed",
                            "error_code": "authentication_failed",
                        }
                    ],
                    "definitely_no_external_effect": False,
                },
            },
        }
        serialized_log = json.dumps(logged, default=str, sort_keys=True)
        for sensitive_value in (
            hydrated_password,
            "AUDIT_FAILURE_DETAIL_SENTINEL",
            "AUDIT_FAILURE_BODY_STDOUT_SENTINEL",
            "AUDIT_FAILURE_STDERR_SENTINEL",
            "AUDIT_FAILURE_BODY_MESSAGE_SENTINEL",
            "AUDIT_FAILURE_RESPONSE_ERROR_SENTINEL",
            "AUDIT_FAILURE_RESPONSE_MESSAGE_SENTINEL",
            "AUDIT_FAILURE_RESULT_DIAGNOSTIC_SENTINEL",
            "AUDIT_FAILURE_RESULT_COLLECTION_SENTINEL",
            f"AUDIT_FAILURE_{unsafe_response_key.upper()}_SENTINEL",
        ):
            assert sensitive_value not in serialized_log

    def test_execute_action_passes_direct_artifact_grants(self):
        bl = MagicMock()
        extension = {
            "extension_key": "example.artifact-fixture",
            "enabled": True,
            "action_definitions": [{"action_id": "download_blob"}],
        }
        plane = SimpleNamespace(
            artifact_presigned_url=MagicMock(
                side_effect=lambda artifact_id, *, method: (
                    f"https://signed.example/{artifact_id}?method={method}"
                )
            ),
            get_artifact=MagicMock(
                side_effect=lambda artifact_id: (
                    {
                        "artifact_id": "art_in",
                        "object_key": "artifacts/art_in/blob",
                        "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                        "size_bytes": 4,
                        "sha256": "0" * 64,
                    }
                    if artifact_id == "art_in"
                    else {
                        "artifact_id": artifact_id,
                        "object_key": f"artifacts/{artifact_id}/blob",
                        "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                    }
                )
            ),
            finalize_artifact_upload=MagicMock(
                side_effect=lambda artifact_id, *, max_size_bytes=None: {
                    "artifact_id": artifact_id,
                    "object_key": f"artifacts/{artifact_id}/blob",
                    "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                    "size_bytes": 4,
                    "sha256": "1" * 64,
                }
            ),
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
            artifact_plane=plane,
        )
        body = ExtensionActionRequest(
            input={"object_key": "in.csv"},
            artifact_inputs=["art_in"],
            artifact_outputs=[{"artifact_id": "art_out", "binding_key": "downloaded_blob"}],
        )

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded"},
        ) as run_action:
            payload = service.execute_action(
                "example.artifact-fixture",
                "download_blob",
                body,
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ).dump()

        kwargs = run_action.call_args.kwargs
        assert kwargs["artifact_inputs"][0]["artifact_handle"] == "artifact:art_in"
        assert kwargs["artifact_inputs"][0]["access"]["download_url"].startswith(
            "https://signed.example/art_in"
        )
        assert kwargs["artifact_outputs"][0]["artifact_handle"] == "artifact:art_out"
        assert kwargs["artifact_outputs"][0]["binding_key"] == "downloaded_blob"
        assert kwargs["artifact_outputs"][0]["access"]["upload_url"].startswith(
            "https://signed.example/art_out"
        )
        plane.finalize_artifact_upload.assert_called_once()
        assert payload["action"]["grant_outputs"] == {"downloaded_blob": "artifact:art_out"}
        assert payload["action"]["produced_artifacts"] == []

    def test_execute_action_non_effect_rejects_body_project_without_access(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_connection.return_value = {
            "connection_id": "conn_secure_drop",
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
            "extension_id": "example.connection-fixture",
            "extension_install_id": "install-connection-fixture",
            "enabled": True,
            "connection_config": {"endpoint": "https://files.example.com"},
            "secret_refs": {"refs": {"api_token": "project_connection:conn_secure_drop:api_token"}},
        }
        extension = {
            "extension_key": "example.connection-fixture",
            "extension_install_id": "install-connection-fixture",
            "enabled": True,
            "action_definitions": [{"action_id": "validate_connection"}],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={"connection_id": "conn_secure_drop"},
        )

        _raises_input_9859_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, allowed_project_ids=frozenset({"99"})
        )
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
            ) as run_action,
            pytest.raises(ServiceForbiddenError),
        ):
            service.execute_action(
                "example.connection-fixture",
                "validate_connection",
                body,
                scope=_raises_input_9859_1,
            )

        run_action.assert_not_called()
        bl.get_project_connection.assert_not_called()

    def test_execute_action_external_effect_rejects_inline_connection_config(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        extension = {
            "extension_key": "example.host-fixture",
            "extension_install_id": "install-host-fixture",
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                }
            ],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionActionRequest(
            context={"project_id": "7"},
            target={
                "connection_id": "conn_external",
                "connection": {
                    "connection_id": "conn_external",
                    "config": {"host": "api.attacker.example"},
                    "secrets": {"api_token": "inline"},
                },
            },
            input={"record_id": "record-a", "title": "Hi", "content": "Body"},
        )

        _raises_input_9915_1 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id="7")
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
            ) as run_action,
            pytest.raises(ServiceValidationError, match=r"only target\.connection_id"),
        ):
            service.execute_action(
                "example.host-fixture", "publish_record", body, scope=_raises_input_9915_1
            )

        run_action.assert_not_called()

    def test_set_enabled_state_returns_validation_error_for_broken_extension(self):
        bl = MagicMock()
        bl.conn = None
        bl.enable_extension.side_effect = ValueError(
            "Extension 'broken_demo' version '1.0.0' has compile/validation errors"
        )
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with pytest.raises(ServiceValidationError):
            service.set_enabled_state("broken_demo", version=None, enabled=True, config={})

    def test_set_enabled_state_rejects_enabling_missing_broken_extension(self):
        bl = MagicMock()
        bl.conn = None
        bl.get_extension.return_value = {
            "extension_key": "broken_demo",
            "enabled": False,
            "bundle_root": "/definitely/missing/bundle",
            "manifest_path": "/definitely/missing/bundle/extension.yaml",
            "status": "valid",
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with pytest.raises(ServiceValidationError, match="Extension bundle is missing"):
            service.set_enabled_state("broken_demo", version=None, enabled=True, config={})

        bl.enable_extension.assert_not_called()

    def test_set_enabled_state_rejects_enabling_extension_that_becomes_broken_after_rescan(self):
        bl = MagicMock()
        bl.conn = None
        stale_row = {
            "extension_key": "broken_demo",
            "enabled": False,
            "bundle_root": "/tmp/present-before-rescan",
            "manifest_path": "/tmp/present-before-rescan/extension.yaml",
            "status": "valid",
        }
        refreshed_row = {
            "extension_key": "broken_demo",
            "enabled": False,
            "bundle_root": "/definitely/missing/bundle",
            "manifest_path": "/definitely/missing/bundle/extension.yaml",
            "status": "valid",
        }
        bl.get_extension.side_effect = [stale_row, refreshed_row]
        bl.list_extensions.return_value = [refreshed_row]
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value="/tmp/extensions",
            ),
            pytest.raises(ServiceValidationError, match="Extension bundle is missing"),
        ):
            service.set_enabled_state("broken_demo", version=None, enabled=True, config={})

        bl.enable_extension.assert_not_called()

    def test_set_enabled_state_fails_closed_when_declared_health_is_not_ready(self, tmp_path):
        bundle = tmp_path / "acme_broken_health"
        bundle.mkdir()
        (bundle / "extension.yaml").write_text("manifest_version: 2\n", encoding="utf-8")
        descriptor = {
            "extension_id": "acme.broken-health",
            "extension_key": "acme.broken-health",
            "bundle_root": str(bundle),
            "release_stage": "active",
            "healthcheck": {"command": ["python3", "health.py"]},
            "dependencies": {},
        }
        bl = MagicMock()
        bl.conn = None
        bl.get_extension.return_value = {
            "extension_key": "acme.broken-health",
            "enabled": False,
            "bundle_root": str(bundle),
            "manifest_path": str(bundle / "extension.yaml"),
            "compiled_manifest": descriptor,
            "status": "valid",
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (None, None),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "extension",
            extension_operation_failure_status=lambda invocation: 502,
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_health",
                return_value={
                    "status": "failed",
                    "detail": "dependency_unavailable: paramiko",
                },
            ),
            pytest.raises(ServiceValidationError) as raised,
        ):
            service.set_enabled_state(
                "acme.broken-health",
                version=None,
                enabled=True,
                config={},
                _skip_rescan=True,
            )

        assert raised.value.payload["reason_code"] == "extension_health_failed"
        bl.sync_enabled_extension_record.assert_not_called()

    def test_execute_query_allows_platform_handler_when_disabled(self):
        bl = MagicMock()
        extension = {
            "extension_key": "fixture.notification-provider",
            "enabled": False,
            "query_definitions": [{"query_id": "get_config", "handler": {"mode": "platform"}}],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda extension_id: (extension, bl),
            extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *args, **kwargs: "platform",
            extension_operation_failure_status=lambda invocation: 502,
        )
        body = ExtensionQueryRequest(context={"project_id": "7"})

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_query",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_query:
            payload = service.execute_query("fixture_events", "get_config", body).dump()

        assert payload["query"]["status"] == "succeeded"
        assert run_query.call_args.kwargs["context"] == {"project_id": "7"}
        bl.log_extension_invocation.assert_called_once()

    def test_invoke_extension_uses_a_store_that_can_resolve_extensions(self):
        """The job store cannot resolve extensions; a dedicated store must be used.

        The API wires this service with the job store, which has no
        get_extension, so POST /api/extensions/{id}/invoke answered 503
        "Extension discovery is not available" on every deployment. The existing
        tests here missed it because a bare MagicMock auto-creates get_extension
        and therefore cannot represent a store that lacks it.
        """

        class _JobStoreWithoutExtensions:
            """Shaped like the real job store: no get_extension at all."""

        extension_store = MagicMock()
        extension_store.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": False,
        }
        service = ToolExecutionService(
            get_logger=_JobStoreWithoutExtensions,
            get_extension_store=lambda: extension_store,
        )

        # Reaching the disabled check proves the lookup happened at all.
        _raises_input_10114_1 = ExtensionInvokeRequest(request={"action": "run"})
        with pytest.raises(ServiceConflictError):
            service.invoke_extension("demo", _raises_input_10114_1)
        extension_store.get_extension.assert_called_once()

    def test_invoke_extension_reports_unavailable_when_no_store_can_resolve(self):
        """Fail closed, but only when nothing can actually resolve extensions."""

        class _StoreWithoutExtensions:
            pass

        service = ToolExecutionService(get_logger=_StoreWithoutExtensions)

        _raises_input_10126_1 = ExtensionInvokeRequest(request={"action": "run"})
        with pytest.raises(ServiceUnavailableError) as exc:
            service.invoke_extension("demo", _raises_input_10126_1)
        assert "discovery is not available" in str(exc.value).lower()

    def test_invoke_extension_rejects_disabled_extension(self):
        bl = MagicMock()
        bl.get_extension.return_value = {"extension_key": "demo", "enabled": False}
        service = ToolExecutionService(get_logger=lambda: bl)

        _raises_input_10135_1 = ExtensionInvokeRequest(request={"action": "run"})
        with pytest.raises(ServiceConflictError) as exc:
            service.invoke_extension("demo", _raises_input_10135_1)

        assert "disabled" in str(exc.value).lower()

    def test_invoke_extension_requires_declared_invoke_scope(self):
        # Fail-closed scope envelope: an enabled extension that does not declare the
        # extension:invoke scope must not be directly invocable.
        bl = MagicMock()
        bl.get_extension.return_value = {"extension_key": "demo", "enabled": True}
        service = ToolExecutionService(get_logger=lambda: bl)

        _raises_input_10147_1 = ExtensionInvokeRequest(request={"action": "run"})
        with pytest.raises(ServiceForbiddenError, match="extension:invoke"):
            service.invoke_extension("demo", _raises_input_10147_1)

    def test_invoke_extension_blocks_direct_external_effect_bypass(self):
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
            "compiled_manifest": {
                "external_effects": [
                    {
                        "operation_id": "publish_record",
                        "effect_kind": "message_delivery",
                        "channel": "api",
                        "idempotency": "required",
                        "test_mode_behavior": "suppress",
                    }
                ]
            },
        }
        service = ToolExecutionService(get_logger=lambda: bl)

        requests = [
            {"action": "publish_record"},
            {"action": {"action_id": "publish_record"}},
            {"action_payload": {"action_id": "publish_record"}},
            {"action_payload": {"action": {"action_id": "publish_record"}}},
        ]
        for request in requests:
            _raises_input_10177_1 = ExtensionInvokeRequest(request=request)
            _raises_input_10177_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
            with (
                patch(
                    "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation"
                ) as run,
                pytest.raises(
                    ServiceForbiddenError, match="cannot bypass declared external_effects"
                ),
            ):
                service.invoke_extension("demo", _raises_input_10177_1, scope=_raises_input_10177_2)

            run.assert_not_called()

    @staticmethod
    def _artifact_plane_with(meta_by_id: dict):
        def artifact_presigned_url(artifact_id: str, *, method: str) -> str:
            meta = meta_by_id.get(str(artifact_id)) or {}
            key = str(meta.get("object_key") or artifact_id).strip()
            return f"https://signed.example/{key}?method={method}"

        return SimpleNamespace(
            artifact_presigned_url=MagicMock(side_effect=artifact_presigned_url),
            get_artifact=lambda aid: meta_by_id.get(str(aid)),
            create_artifact=MagicMock(),
        )

    def test_invoke_extension_denies_foreign_read_artifact(self):
        # FS-DEEP-CAND-023: caller-supplied READ artifact id owned by another
        # account must be rejected before a presigned URL is minted.
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with(
            {
                "art_foreign": {
                    "object_key": "artifacts/art_foreign/blob",
                    "owner_scope": {"account_id": "acc_other"},
                }
            }
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10225_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=["art_foreign"]
        )
        _raises_input_10225_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceForbiddenError, match="Artifact access denied"):
            service.invoke_extension("demo", _raises_input_10225_1, scope=_raises_input_10225_2)
        plane.artifact_presigned_url.assert_not_called()

    def test_invoke_extension_denies_foreign_write_artifact(self):
        # FS-DEEP-CAND-024: caller-supplied WRITE artifact id owned by another
        # account must be rejected before a write presigned URL is minted.
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with(
            {
                "art_foreign": {
                    "object_key": "artifacts/art_foreign/blob",
                    "owner_scope": {"account_id": "acc_other"},
                }
            }
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10252_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_outputs=["art_foreign"]
        )
        _raises_input_10252_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceForbiddenError, match="Artifact access denied"):
            service.invoke_extension("demo", _raises_input_10252_1, scope=_raises_input_10252_2)
        plane.artifact_presigned_url.assert_not_called()

    def test_invoke_extension_denies_finalized_write_artifact_before_presign(self):
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with(
            {
                "art_finalized": {
                    "object_key": "artifacts/art_finalized/blob",
                    "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                    "size_bytes": 8,
                    "sha256": hashlib.sha256(b"old-body").hexdigest(),
                }
            }
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10279_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_outputs=["art_finalized"]
        )
        _raises_input_10279_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceValidationError, match="empty unfinalized artifact"):
            service.invoke_extension("demo", _raises_input_10279_1, scope=_raises_input_10279_2)
        plane.artifact_presigned_url.assert_not_called()

    def test_invoke_extension_denies_write_artifact_with_existing_unfinalized_body(self):
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with(
            {
                "art_pending": {
                    "object_key": "artifacts/art_pending/blob",
                    "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                    "size_bytes": 0,
                    "sha256": "",
                }
            }
        )
        plane.artifact_upload_stat = MagicMock(
            return_value={"key": "artifacts/art_pending/blob", "size_bytes": 8}
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10312_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_outputs=["art_pending"]
        )
        _raises_input_10312_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceValidationError, match="empty unfinalized artifact"):
            service.invoke_extension("demo", _raises_input_10312_1, scope=_raises_input_10312_2)
        plane.artifact_upload_stat.assert_called_once_with("art_pending")
        plane.artifact_presigned_url.assert_not_called()

    def test_invoke_extension_denies_unknown_artifact_id(self):
        # FS-DEEP-CAND-023/024: an unresolvable artifact id must be rejected
        # (fail closed) rather than minting a grant for it.
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with({})
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10336_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=["art_missing"]
        )
        _raises_input_10336_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceForbiddenError, match="Artifact access denied"):
            service.invoke_extension("demo", _raises_input_10336_1, scope=_raises_input_10336_2)
        plane.artifact_presigned_url.assert_not_called()

    def test_invoke_extension_allows_owned_artifacts(self):
        # FS-DEEP-CAND-023/024: caller-owned read and write artifact ids mint
        # presigned URLs and the invocation proceeds.
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with(
            {
                "art_in": {
                    "object_key": "artifacts/art_in/blob",
                    "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                    "size_bytes": 5,
                    "sha256": hashlib.sha256(b"owned").hexdigest(),
                },
                "art_out": {
                    "object_key": "artifacts/art_out/blob",
                    "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                    "size_bytes": 0,
                    "sha256": "",
                },
            }
        )
        plane.artifact_upload_stat = MagicMock(return_value=None)
        plane.finalize_artifact_upload = MagicMock(
            return_value={
                "object_key": "artifacts/art_out/blob",
                "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                "size_bytes": 5,
                "sha256": hashlib.sha256(b"owned").hexdigest(),
            }
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_invocation:
            payload = service.invoke_extension(
                "demo",
                ExtensionInvokeRequest(
                    request={"action": "run"},
                    artifact_inputs=["art_in"],
                    artifact_outputs=["art_out"],
                ),
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ).dump()
        assert payload["invocation"]["status"] == "succeeded"
        assert plane.artifact_presigned_url.call_count == 2
        kwargs = run_invocation.call_args.kwargs
        assert kwargs["artifact_inputs"][0]["object_key"] == "artifacts/art_in/blob"
        assert kwargs["artifact_outputs"][0]["object_key"] == "artifacts/art_out/blob"
        assert kwargs["artifact_outputs"][0]["access"]["max_size_bytes"] == 100 * 1024 * 1024

    def test_finalize_artifact_uploads_does_not_retry_internal_type_error(self):
        from core.application.management.command_services_tool_execution import (
            _finalize_artifact_uploads,
        )

        plane = SimpleNamespace(
            finalize_artifact_upload=MagicMock(side_effect=TypeError("internal finalize bug")),
            get_artifact=MagicMock(return_value=None),
        )

        with pytest.raises(TypeError, match="internal finalize bug"):
            _finalize_artifact_uploads(
                plane,
                ["art_out"],
                max_size_by_id={"art_out": 4},
            )

        plane.finalize_artifact_upload.assert_called_once_with(
            "art_out",
            max_size_bytes=4,
        )
        plane.get_artifact.assert_not_called()

    def test_invoke_extension_rejects_artifact_id_as_both_input_and_output(self):
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with(
            {
                "art_same": {
                    "object_key": "artifacts/art_same/blob",
                    "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                    "size_bytes": 5,
                    "sha256": hashlib.sha256(b"hello").hexdigest(),
                }
            }
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10442_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=["art_same"], artifact_outputs=["art_same"]
        )
        _raises_input_10442_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with pytest.raises(ServiceValidationError, match="Output artifact_id must differ"):
            service.invoke_extension("demo", _raises_input_10442_1, scope=_raises_input_10442_2)
        plane.artifact_presigned_url.assert_not_called()

    def test_invoke_extension_blocks_local_fs_artifact_grants_without_trusted_override(
        self,
        tmp_path,
        monkeypatch,
    ):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

        monkeypatch.delenv(TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, raising=False)
        storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
        plane = ArtifactDatasetPlane(storage=storage)
        handle, meta = plane.create_artifact(
            owner_scope={"account_id": DEFAULT_ACCOUNT_ID},
            run_id="run_bridge",
            step_id="artifact_bridge",
            filename="input.txt",
            content_type="text/plain",
            body=b"original",
        )
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10483_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=[handle.artifact_id]
        )
        _raises_input_10483_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        # AC17-R123: this used to assert the dead end -- a local filesystem grant was
        # refused and the step could not run at all. What always mattered is that a URL
        # the extension cannot fetch never reaches it, and that is now true because the
        # platform substitutes one it serves itself. Without an address to serve from it
        # still refuses, and says which setting is missing instead of describing the
        # symptom.
        monkeypatch.delenv("FS_APP_INTERNAL_URL", raising=False)
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation"
            ) as run,
            pytest.raises(ServiceUnavailableError, match="FS_APP_INTERNAL_URL"),
        ):
            service.invoke_extension("demo", _raises_input_10483_1, scope=_raises_input_10483_2)

        run.assert_not_called()
        assert storage.get(str(meta["object_key"])) == b"original"

    def test_invoke_extension_hands_a_servable_url_when_storage_has_none(
        self,
        tmp_path,
        monkeypatch,
    ):
        """AC17-R123, the behaviour the four refusals above used to stand in for.

        A local filesystem backend has no URL to give an extension. The step no longer
        dies there: the platform substitutes one it serves itself, so what reaches the
        subprocess is an address it can fetch and never the path on disk.
        """
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

        monkeypatch.delenv(TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, raising=False)
        monkeypatch.setenv("FS_APP_INTERNAL_URL", "http://fs-app:8099")
        monkeypatch.setenv("FS_SECRET_KEY", "a-deployment-secret-for-tests")
        storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
        plane = ArtifactDatasetPlane(storage=storage)
        handle, _meta = plane.create_artifact(
            owner_scope={"account_id": DEFAULT_ACCOUNT_ID},
            run_id="run_bridge",
            step_id="artifact_bridge",
            filename="input.txt",
            content_type="text/plain",
            body=b"original",
        )
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        # What the subprocess was handed is the whole question here, so whatever the
        # stubbed invocation returns afterwards is not this test's business.
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation"
            ) as run,
            contextlib.suppress(Exception),
        ):
            service.invoke_extension(
                "demo",
                ExtensionInvokeRequest(
                    request={"action": "run"}, artifact_inputs=[handle.artifact_id]
                ),
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            )

        run.assert_called_once()
        descriptors = run.call_args.kwargs["artifact_inputs"]
        access = dict(descriptors[0]["access"])
        assert access["download_url"].startswith(
            "http://fs-app:8099/api/extension-artifacts/artifact/"
        )
        assert "file://" not in access["download_url"]
        # Signed, and carrying the header that lets the platform accept the request.
        assert access["platform_grant"]["version"] == 1
        assert "x-platform-artifact-access" in {
            str(name).lower() for name in access["required_headers"]
        }

    def test_invoke_extension_rejects_file_url_grant_from_custom_plane_without_override(
        self,
        monkeypatch,
    ):
        monkeypatch.delenv(TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, raising=False)
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = SimpleNamespace(
            get_artifact=lambda _aid: {
                "object_key": "artifacts/art_file/blob",
                "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"original").hexdigest(),
            },
            artifact_presigned_url=MagicMock(
                return_value="file:///tmp/artifacts/art_file/blob?method=GET"
            ),
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10525_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=["art_file"]
        )
        _raises_input_10525_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation"
            ) as run,
            pytest.raises(ServiceUnavailableError, match="FS_APP_INTERNAL_URL"),
        ):
            service.invoke_extension("demo", _raises_input_10525_1, scope=_raises_input_10525_2)

        run.assert_not_called()
        plane.artifact_presigned_url.assert_called_once_with("art_file", method="GET")

    def test_invoke_extension_blocks_memory_artifact_grants_before_subprocess(self):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

        plane = ArtifactDatasetPlane()
        handle, _meta = plane.create_artifact(
            owner_scope={"account_id": DEFAULT_ACCOUNT_ID},
            run_id="run_bridge",
            step_id="artifact_bridge",
            filename="input.txt",
            content_type="text/plain",
            body=b"original",
        )
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10563_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=[handle.artifact_id]
        )
        _raises_input_10563_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation"
            ) as run,
            pytest.raises(ServiceUnavailableError, match="FS_APP_INTERNAL_URL"),
        ):
            service.invoke_extension("demo", _raises_input_10563_1, scope=_raises_input_10563_2)

        run.assert_not_called()

    def test_invoke_extension_rejects_unsupported_artifact_grant_scheme_from_custom_plane(self):
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = SimpleNamespace(
            get_artifact=lambda _aid: {
                "object_key": "artifacts/art_memory/blob",
                "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID},
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"original").hexdigest(),
            },
            artifact_presigned_url=MagicMock(
                return_value="inmemory://artifacts/art_memory/blob?method=GET"
            ),
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10600_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=["art_memory"]
        )
        _raises_input_10600_2 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation"
            ) as run,
            pytest.raises(ServiceUnavailableError, match="FS_APP_INTERNAL_URL"),
        ):
            service.invoke_extension("demo", _raises_input_10600_1, scope=_raises_input_10600_2)

        run.assert_not_called()
        plane.artifact_presigned_url.assert_called_once_with("art_memory", method="GET")

    @staticmethod
    def _compile_subprocess_extension(tmp_path, *, extension_id: str, main_source: str) -> dict:
        import yaml

        from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

        bundle = tmp_path / "extensions" / extension_folder_name(extension_id)
        bundle.mkdir(parents=True)
        manifest = {
            "manifest_version": 2,
            "runtime": {"contract_version": "extension_host_v2"},
            "extension_id": extension_id,
            "kind": "tool_provider",
            "version": "1.0.0",
            "display_name": "Artifact Contract",
            "description": "Artifact contract test",
            "vendor": {"name": "Acme Test"},
            "features": ["tool"],
            "required_scopes": ["extension:invoke"],
            "entrypoint": {"mode": "subprocess", "command": [sys.executable, "main.py"]},
            "health": {"command": [sys.executable, "health.py"], "timeout_seconds": 5},
            "ui_manifest": "ui/ui_manifest.yaml",
            "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        }
        (bundle / "extension.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False),
            encoding="utf-8",
        )
        (bundle / "ui").mkdir()
        (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n")
        (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
        (bundle / "main.py").write_text(main_source, encoding="utf-8")
        compiled = compile_extension_bundle(bundle)
        assert compiled["status"] == "valid"
        return compiled

    def test_invoke_extension_artifact_bridge_round_trips_via_public_sdk(
        self,
        tmp_path,
        monkeypatch,
    ):
        import yaml

        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
        from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

        monkeypatch.setenv(TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
        bundle = tmp_path / "extensions" / "acme_artifact_bridge"
        bundle.mkdir(parents=True)
        manifest = {
            "manifest_version": 2,
            "runtime": {"contract_version": "extension_host_v2"},
            "extension_id": "acme.artifact-bridge",
            "kind": "tool_provider",
            "version": "1.0.0",
            "display_name": "Artifact Bridge",
            "description": "Artifact bridge test",
            "vendor": {"name": "Acme Test"},
            "features": ["tool"],
            "required_scopes": ["extension:invoke"],
            "entrypoint": {"mode": "subprocess", "command": [sys.executable, "main.py"]},
            "health": {"command": [sys.executable, "health.py"], "timeout_seconds": 5},
            "ui_manifest": "ui/ui_manifest.yaml",
            "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        }
        (bundle / "extension.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False),
            encoding="utf-8",
        )
        (bundle / "ui").mkdir()
        (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n")
        (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
        (bundle / "main.py").write_text(
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from flowsteward_extension_sdk import read_artifact_bytes, write_artifact_bytes",
                    "",
                    "payload = json.loads(sys.stdin.read() or '{}')",
                    "body = read_artifact_bytes(payload)",
                    "written = write_artifact_bytes(payload, body.upper(), content_type='text/plain')",
                    "max_size = payload['artifacts']['outputs'][0]['access']['max_size_bytes']",
                    "print(json.dumps({'ok': True, 'result': {'input_text': body.decode('utf-8'), 'written': written, 'max_size_bytes': max_size}}))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        source = (bundle / "main.py").read_text(encoding="utf-8")
        assert "core." not in source
        compiled = compile_extension_bundle(bundle)
        assert compiled["status"] == "valid"

        storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
        plane = ArtifactDatasetPlane(storage=storage)
        input_handle, _input_meta = plane.create_artifact(
            owner_scope={"account_id": DEFAULT_ACCOUNT_ID},
            run_id="run_bridge",
            step_id="artifact_bridge",
            filename="input.txt",
            content_type="text/plain",
            body=b"hello",
        )
        bl = MagicMock()
        extension = dict(compiled)
        extension["enabled"] = True
        extension["required_scopes"] = ["extension:invoke"]
        bl.get_extension.return_value = extension
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        payload = service.invoke_extension(
            "acme.artifact-bridge",
            ExtensionInvokeRequest(
                request={"action": "bridge"},
                artifact_inputs=[input_handle.artifact_id],
                artifact_output_specs=[
                    {
                        "run_id": "run_bridge",
                        "step_id": "tabular",
                        "filename": "output.txt",
                        "content_type": "text/plain",
                        "max_size_bytes": 16,
                    }
                ],
                timeout_seconds=10,
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="tester",
                principal_type="admin",
            ),
        ).dump()

        assert payload["invocation"]["status"] == "succeeded"
        assert payload["invocation"]["response"]["result"]["input_text"] == "hello"
        assert payload["invocation"]["response"]["result"]["max_size_bytes"] == 16
        produced = payload["produced_artifacts"][0]
        output_id = produced["artifact_id"]
        assert produced["size_bytes"] == 5
        assert produced["sha256"] == hashlib.sha256(b"HELLO").hexdigest()
        output_meta = plane.get_artifact(output_id)
        assert output_meta is not None
        assert output_meta["filename"] == "output.txt"
        assert output_meta["size_bytes"] == 5
        assert output_meta["sha256"] == hashlib.sha256(b"HELLO").hexdigest()
        assert storage.get(str(output_meta["object_key"])) == b"HELLO"

    def test_invoke_extension_dataset_bridge_round_trips_via_public_sdk(
        self,
        tmp_path,
        monkeypatch,
    ):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

        monkeypatch.setenv(TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
        compiled = self._compile_subprocess_extension(
            tmp_path,
            extension_id="acme.dataset-bridge",
            main_source=(
                "import json\n"
                "import sys\n"
                "from flowsteward_extension_sdk import read_dataset_bytes, write_dataset_bytes\n"
                "payload = json.loads(sys.stdin.read() or '{}')\n"
                "rows = json.loads(read_dataset_bytes(payload, binding_key='input').decode('utf-8'))\n"
                "output_rows = [{**row, 'name': str(row.get('name', '')).upper()} for row in rows]\n"
                "body = json.dumps(output_rows, separators=(',', ':')).encode('utf-8')\n"
                "written = write_dataset_bytes(payload, body, binding_key='output')\n"
                "max_size = payload['datasets']['outputs'][0]['access']['max_size_bytes']\n"
                "print(json.dumps({'ok': True, 'result': {'row_count': len(rows), 'written': written, 'max_size_bytes': max_size}}))\n"
            ),
        )
        storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
        plane = ArtifactDatasetPlane(storage=storage)
        input_handle, _input_meta = plane.create_dataset(
            owner_scope={"account_id": DEFAULT_ACCOUNT_ID},
            run_id="run_bridge",
            step_id="supplier_feed",
            schema={"columns": [{"name": "name", "type": "text"}]},
            rows=[{"name": "alpha"}, {"name": "beta"}],
        )
        bl = MagicMock()
        extension = dict(compiled)
        extension["enabled"] = True
        extension["required_scopes"] = ["extension:invoke"]
        bl.get_extension.return_value = extension
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        payload = service.invoke_extension(
            "acme.dataset-bridge",
            ExtensionInvokeRequest(
                request={"action": "bridge"},
                dataset_inputs=[{"dataset_id": input_handle.dataset_id, "binding_key": "input"}],
                dataset_output_specs=[
                    {
                        "run_id": "run_bridge",
                        "step_id": "supplier_feed",
                        "binding_key": "output",
                        "schema": {"columns": [{"name": "name", "type": "text"}]},
                        "max_size_bytes": 128,
                    }
                ],
                timeout_seconds=10,
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="tester",
                principal_type="admin",
            ),
        ).dump()

        assert payload["invocation"]["status"] == "succeeded"
        assert payload["invocation"]["response"]["result"]["row_count"] == 2
        assert payload["invocation"]["response"]["result"]["max_size_bytes"] == 128
        produced = payload["produced_datasets"][0]
        output_id = produced["dataset_id"]
        output_meta = plane.get_dataset(output_id)
        assert output_meta is not None
        assert output_meta["row_count"] == 2
        assert output_meta["preview_rows"] == [{"name": "ALPHA"}, {"name": "BETA"}]
        assert (
            output_meta["sha256"]
            == hashlib.sha256(b'[{"name":"ALPHA"},{"name":"BETA"}]').hexdigest()
        )
        assert storage.get(str(output_meta["object_key"])) == b'[{"name":"ALPHA"},{"name":"BETA"}]'

    def test_invoke_extension_fails_when_declared_output_is_not_uploaded(
        self,
        tmp_path,
        monkeypatch,
    ):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

        monkeypatch.setenv(TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
        compiled = self._compile_subprocess_extension(
            tmp_path,
            extension_id="acme.artifact-no-upload",
            main_source="import json\nprint(json.dumps({'ok': True}))\n",
        )
        storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
        plane = ArtifactDatasetPlane(storage=storage)
        bl = MagicMock()
        extension = dict(compiled)
        extension["enabled"] = True
        extension["required_scopes"] = ["extension:invoke"]
        bl.get_extension.return_value = extension
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10876_1 = ExtensionInvokeRequest(
            request={"action": "bridge"},
            artifact_output_specs=[
                {
                    "run_id": "run_bridge",
                    "step_id": "tabular",
                    "filename": "output.txt",
                    "content_type": "text/plain",
                }
            ],
            timeout_seconds=10,
        )
        _raises_input_10876_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="tester", principal_type="admin"
        )
        with pytest.raises(ServiceError) as exc:
            service.invoke_extension(
                "acme.artifact-no-upload", _raises_input_10876_1, scope=_raises_input_10876_2
            )

        invocation = exc.value.payload["invocation"]
        assert invocation["status"] == "failed"
        assert invocation["errors"][0]["code"] == "artifact_upload_missing"
        output_id = invocation["errors"][0]["artifact_id"]
        assert plane.get_artifact(output_id) is None

    def test_invoke_extension_fails_when_output_upload_exceeds_grant_cap(
        self,
        tmp_path,
        monkeypatch,
    ):
        from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
        from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter

        monkeypatch.setenv(TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
        read_body_forbidden = {"enabled": True}

        class NoBodyReadStorage(LocalFilesystemStorageAdapter):
            def get(self, key: str) -> bytes | None:
                if read_body_forbidden["enabled"] and str(key).endswith("/output.txt"):
                    raise AssertionError("oversized output body should not be read")
                return super().get(key)

        compiled = self._compile_subprocess_extension(
            tmp_path,
            extension_id="acme.artifact-too-large",
            main_source=(
                "import json\n"
                "import pathlib\n"
                "import sys\n"
                "import urllib.parse\n"
                "payload = json.loads(sys.stdin.read() or '{}')\n"
                "url = payload['artifacts']['outputs'][0]['access']['upload_url']\n"
                "path = pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))\n"
                "path.parent.mkdir(parents=True, exist_ok=True)\n"
                "path.write_bytes(b'0123456789')\n"
                "print(json.dumps({'ok': True}))\n"
            ),
        )
        storage = NoBodyReadStorage(root_dir=str(tmp_path / "artifact_store"))
        plane = ArtifactDatasetPlane(storage=storage)
        bl = MagicMock()
        extension = dict(compiled)
        extension["enabled"] = True
        extension["required_scopes"] = ["extension:invoke"]
        bl.get_extension.return_value = extension
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_10948_1 = ExtensionInvokeRequest(
            request={"action": "bridge"},
            artifact_output_specs=[
                {
                    "run_id": "run_bridge",
                    "step_id": "tabular",
                    "filename": "output.txt",
                    "content_type": "text/plain",
                    "max_size_bytes": 4,
                }
            ],
            timeout_seconds=10,
        )
        _raises_input_10948_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="tester", principal_type="admin"
        )
        with pytest.raises(ServiceError) as exc:
            service.invoke_extension(
                "acme.artifact-too-large", _raises_input_10948_1, scope=_raises_input_10948_2
            )

        invocation = exc.value.payload["invocation"]
        assert invocation["status"] == "failed"
        assert invocation["errors"][0]["code"] == "artifact_size_limit_exceeded"
        output_id = invocation["errors"][0]["artifact_id"]
        assert plane.get_artifact(output_id) is None
        read_body_forbidden["enabled"] = False
        _raises_input_10977_1 = ExtensionInvokeRequest(
            request={"action": "read rejected"}, artifact_inputs=[output_id], timeout_seconds=10
        )
        _raises_input_10977_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="tester", principal_type="admin"
        )
        with pytest.raises(ServiceForbiddenError, match="Artifact access denied"):
            service.invoke_extension(
                "acme.artifact-too-large", _raises_input_10977_1, scope=_raises_input_10977_2
            )

    def test_invoke_extension_denies_read_artifact_outside_project_allowlist(self):
        # FS-DEEP-CAND-023: same account but project outside the caller's
        # allowed_project_ids is rejected.
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        plane = self._artifact_plane_with(
            {
                "art_p9": {
                    "object_key": "artifacts/art_p9/blob",
                    "owner_scope": {"account_id": DEFAULT_ACCOUNT_ID, "project_id": "9"},
                }
            }
        )
        service = ToolExecutionService(get_logger=lambda: bl, artifact_plane=plane)

        _raises_input_11011_1 = ExtensionInvokeRequest(
            request={"action": "run"}, artifact_inputs=["art_p9"]
        )
        _raises_input_11011_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            principal_type="service_account",
            allowed_project_ids=frozenset({"1"}),
        )
        with pytest.raises(ServiceForbiddenError, match="Artifact access denied"):
            service.invoke_extension("demo", _raises_input_11011_1, scope=_raises_input_11011_2)
        plane.artifact_presigned_url.assert_not_called()

    def test_execute_tool_dispatches_internal_and_mcp_through_same_surface(self):
        from core.application.registry.tool_manifest import ToolManifest

        internal_manifest = ToolManifest.from_db_row(
            {
                "tool_id": "search_tool",
                "display_name": "Search Tool",
                "enabled": True,
                "interface": {
                    "function_name": "search_code",
                    "parameters": [{"name": "query", "type": "string", "required": True}],
                    "returns": {"name": "results", "type": "array", "required": True},
                },
                "implementation": {
                    "kind": "internal",
                    "module": "core.application.orchestration.tools.search_tools",
                },
            }
        )
        mcp_manifest = ToolManifest.from_db_row(
            {
                "tool_id": "context_lookup",
                "display_name": "Context Lookup",
                "enabled": True,
                "interface": {
                    "function_name": "lookup_docs",
                    "parameters": [{"name": "topic", "type": "string", "required": True}],
                    "returns": {"name": "documents", "type": "array", "required": True},
                },
                "implementation": {
                    "kind": "mcp",
                    "server": "docs_lookup",
                    "connector_id": "builtin_docs_lookup_mcp",
                },
            }
        )
        registry_manager = MagicMock()
        manifests = {
            "search_tool": internal_manifest,
            "context_lookup": mcp_manifest,
        }
        registry_manager.tools.get_tool.side_effect = lambda tool_id: manifests.get(tool_id)
        registry_manager.tools.get_catalog_entry.side_effect = lambda tool_id: (
            manifests[tool_id].catalog_entry() if tool_id in manifests else None
        )

        service = ToolExecutionService(
            get_logger=lambda: None,
            get_registry_manager=lambda: registry_manager,
        )

        with patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            side_effect=[
                {"items": ["hit-1"]},
                {"source": "mcp"},
            ],
        ) as invoke_tool:
            internal_payload = service.execute_tool(
                "search_tool",
                ToolInvocationInput(arguments={"query": "auth middleware"}),
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ).dump()
            mcp_payload = service.execute_tool(
                "context_lookup",
                ToolInvocationInput(arguments={"topic": "fastapi auth"}),
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ).dump()

        assert internal_payload["tool_id"] == "search_tool"
        assert internal_payload["implementation_kind"] == "internal"
        assert internal_payload["function_name"] == "search_code"
        assert internal_payload["result"] == {"items": ["hit-1"]}
        assert internal_payload["catalog_entry"]["implementation_kind"] == "internal"
        assert invoke_tool.call_args_list[0].kwargs["function_name"] == "search_code"
        assert invoke_tool.call_args_list[0].kwargs["arguments"] == {"query": "auth middleware"}

        assert mcp_payload["tool_id"] == "context_lookup"
        assert mcp_payload["implementation_kind"] == "mcp"
        assert mcp_payload["function_name"] == "lookup_docs"
        assert mcp_payload["result"] == {"source": "mcp"}
        assert mcp_payload["catalog_entry"]["implementation_kind"] == "mcp"
        assert invoke_tool.call_args_list[1].kwargs["function_name"] == "lookup_docs"
        assert invoke_tool.call_args_list[1].kwargs["arguments"] == {"topic": "fastapi auth"}

    def test_execute_projected_mcp_tool_uses_trusted_scope_tenant_for_lookup_and_rpc(self):
        from core.application.auth import mcp_connector_oauth
        from core.application.management.mcp_connector_services import McpConnectorStore
        from core.application.registry.tool_manifest import ToolManifest

        tool_id = "mcp.catalog.records.list"
        manifest = ToolManifest.from_db_row(
            {
                "tool_id": tool_id,
                "display_name": "Catalog records",
                "enabled": True,
                "scope_kind": "project",
                "project_id": "project-alpha",
                "interface": {
                    "function_name": "records.list",
                    "parameters": [{"name": "tenant_id", "type": "string", "required": False}],
                    "returns": {"name": "records", "type": "array", "required": True},
                },
                "implementation": {
                    "kind": "mcp",
                    "connector_id": "mcp-catalog",
                    "remote_tool_name": "records.list",
                    "owner_account_id": "account-alpha",
                    "owner_project_id": "project-alpha",
                    "owner_tenant_id": "tenant-alpha",
                },
                "config": {
                    "connector_id": "mcp-catalog",
                    "remote_tool_name": "records.list",
                    "owner_account_id": "account-alpha",
                    "owner_project_id": "project-alpha",
                    "owner_tenant_id": "tenant-alpha",
                },
            }
        )
        registry_manager = MagicMock()
        registry_manager.tools.get_tool.return_value = manifest
        registry_manager.tools.get_catalog_entry.return_value = manifest.catalog_entry()
        runtime_store = SimpleNamespace(cursor=object())
        captured: dict[str, Any] = {}

        def get_connector_row(_self, conn, connector_id, **lineage):
            captured["lookup"] = {
                "conn": conn,
                "connector_id": connector_id,
                **lineage,
            }
            return {
                "connector_id": "mcp-catalog",
                "account_id": "account-alpha",
                "project_id": "project-alpha",
                "tenant_id": "tenant-alpha",
                "enabled": True,
                "allowlisted_tools": [tool_id],
                "discovered_tools_jsonb": [],
            }

        def rpc_post(conn, **kwargs):
            captured["rpc"] = {"conn": conn, **kwargs}
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"structuredContent": {"records": ["record-1"]}},
            }

        service = ToolExecutionService(
            get_logger=lambda: runtime_store,
            get_registry_manager=lambda: registry_manager,
        )
        with (
            patch.object(McpConnectorStore, "get_connector_row", get_connector_row),
            patch.object(mcp_connector_oauth, "mcp_connector_rpc_post", rpc_post),
        ):
            payload = service.execute_tool(
                tool_id,
                ToolInvocationInput(arguments={"tenant_id": "tenant-attacker"}),
                scope=ScopeContext(
                    account_id="account-alpha",
                    project_id="project-alpha",
                    tenant_id="tenant-alpha",
                ),
            ).dump()

        assert payload["result"] == {"records": ["record-1"]}
        assert captured["lookup"] == {
            "conn": runtime_store,
            "connector_id": "mcp-catalog",
            "account_id": "account-alpha",
            "project_id": "project-alpha",
            "tenant_id": "tenant-alpha",
        }
        assert captured["rpc"]["runtime_context"]["tenant_id"] == "tenant-alpha"
        assert captured["rpc"]["payload"]["params"]["arguments"] == {"tenant_id": "tenant-attacker"}

    @pytest.mark.parametrize(
        "scope",
        [
            ScopeContext(
                account_id="account-alpha",
                project_id="project-alpha",
                tenant_id="tenant-other",
            ),
            ScopeContext(
                account_id="account-alpha",
                project_id="project-alpha",
                tenant_id=None,
            ),
        ],
        ids=["cross-tenant", "missing-tenant"],
    )
    def test_execute_projected_mcp_tool_rejects_untrusted_tenant_before_lookup(self, scope):
        from core.application.management.mcp_connector_services import McpConnectorStore
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.from_db_row(
            {
                "tool_id": "mcp.catalog.records.list",
                "display_name": "Catalog records",
                "enabled": True,
                "scope_kind": "project",
                "project_id": "project-alpha",
                "interface": {"function_name": "records.list", "parameters": []},
                "implementation": {
                    "kind": "mcp",
                    "connector_id": "mcp-catalog",
                    "remote_tool_name": "records.list",
                    "owner_account_id": "account-alpha",
                    "owner_project_id": "project-alpha",
                    "owner_tenant_id": "tenant-alpha",
                },
                "config": {
                    "connector_id": "mcp-catalog",
                    "remote_tool_name": "records.list",
                    "owner_account_id": "account-alpha",
                    "owner_project_id": "project-alpha",
                    "owner_tenant_id": "tenant-alpha",
                },
            }
        )
        registry_manager = MagicMock()
        registry_manager.tools.get_tool.return_value = manifest
        registry_manager.tools.get_catalog_entry.return_value = manifest.catalog_entry()
        service = ToolExecutionService(
            get_logger=lambda: SimpleNamespace(cursor=object()),
            get_registry_manager=lambda: registry_manager,
        )

        _raises_input_11255_1 = ToolInvocationInput(arguments={})
        with (
            patch.object(
                McpConnectorStore,
                "get_connector_row",
                side_effect=AssertionError("invalid tenant must fail before connector lookup"),
            ),
            pytest.raises(ServiceUnavailableError, match=r"tenant|trusted account/project/tenant"),
        ):
            service.execute_tool("mcp.catalog.records.list", _raises_input_11255_1, scope=scope)

    def test_execute_tool_dispatches_extension_through_same_surface(self):
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.from_db_row(
            {
                "tool_id": "provider.echo_text",
                "display_name": "Echo Text",
                "enabled": True,
                "interface": {
                    "function_name": "echo_text",
                    "parameters": [{"name": "arguments_json", "type": "string", "required": False}],
                    "returns": {"name": "result", "type": "string", "required": False},
                },
                "implementation": {
                    "kind": "extension",
                    "extension_key": "example_tool_provider",
                    "tool_name": "echo_text",
                    "action": "invoke_tool",
                    "provider_key": "example_tool_provider",
                },
                "config": {"static_arguments": {"prefix": "hello"}},
            }
        )
        registry_manager = MagicMock()
        registry_manager.tools.get_tool.return_value = manifest
        registry_manager.tools.get_catalog_entry.return_value = manifest.catalog_entry()

        service = ToolExecutionService(
            get_logger=lambda: None,
            get_registry_manager=lambda: registry_manager,
        )
        resolved_bl = MagicMock()

        with (
            patch(
                "core.application.orchestration.tools.tool_resolver._resolve_extension_record",
                return_value=(
                    {
                        "extension_key": "example_tool_provider",
                        "enabled": True,
                        "kind": "tool_provider",
                    },
                    resolved_bl,
                ),
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
                return_value={
                    "status": "succeeded",
                    "response": {"ok": True, "result": {"text": "hello world"}},
                },
            ) as run_invocation,
        ):
            payload = service.execute_tool(
                "provider.echo_text",
                ToolInvocationInput(arguments={"arguments_json": json.dumps({"text": "world"})}),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="7",
                    workflow_id="wf.tools",
                    actor_id="jane",
                    principal_type="user",
                ),
            ).dump()

        assert payload["tool_id"] == "provider.echo_text"
        assert payload["implementation_kind"] == "extension"
        assert payload["function_name"] == "echo_text"
        assert payload["result"] == {"text": "hello world"}
        request_payload = run_invocation.call_args.kwargs["request_payload"]
        runtime_context = run_invocation.call_args.kwargs["runtime_context"]
        assert request_payload["tool_name"] == "echo_text"
        assert request_payload["provider"]["provider_key"] == "example_tool_provider"
        assert request_payload["arguments"] == {"prefix": "hello", "text": "world"}
        assert runtime_context["account_id"] == DEFAULT_ACCOUNT_ID
        assert runtime_context["project_id"] == "7"
        assert runtime_context["workflow_id"] == "wf.tools"
        assert runtime_context["actor_id"] == "jane"
        assert runtime_context["actor_type"] == "user"
        assert runtime_context["source"] == "tool_execution_service.execute_tool"
        resolved_bl.log_extension_invocation.assert_called_once()
        log_kwargs = resolved_bl.log_extension_invocation.call_args.kwargs
        assert log_kwargs["request_context"]["source"] == "tool_execution_service.execute_tool"
        assert log_kwargs["request_context"]["tool_id"] == "provider.echo_text"
        assert log_kwargs["request_context"]["function_name"] == "echo_text"

    def test_execute_tool_extension_resolution_reuses_service_logger(self):
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.from_db_row(
            {
                "tool_id": "provider.echo_text",
                "display_name": "Echo Text",
                "enabled": True,
                "interface": {
                    "function_name": "echo_text",
                    "parameters": [{"name": "arguments_json", "type": "string", "required": False}],
                    "returns": {"name": "result", "type": "string", "required": False},
                },
                "implementation": {
                    "kind": "extension",
                    "extension_key": "example_tool_provider",
                    "tool_name": "echo_text",
                    "action": "invoke_tool",
                    "provider_key": "example_tool_provider",
                },
            }
        )
        registry_manager = MagicMock()
        registry_manager.tools.get_tool.return_value = manifest
        registry_manager.tools.get_catalog_entry.return_value = manifest.catalog_entry()
        runtime_logger = MagicMock()
        runtime_logger.get_extension.return_value = {
            "extension_key": "example_tool_provider",
            "enabled": True,
            "kind": "tool_provider",
        }

        service = ToolExecutionService(
            get_logger=lambda: runtime_logger,
            get_registry_manager=lambda: registry_manager,
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.normalize_extension_row_for_runtime",
                side_effect=lambda row: dict(row),
            ),
            patch("core.infrastructure.db.platform_stores.PlatformIdentityStore") as logger_cls,
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
                return_value={
                    "status": "succeeded",
                    "response": {"ok": True, "result": {"text": "hello world"}},
                },
            ),
        ):
            payload = service.execute_tool(
                "provider.echo_text",
                ToolInvocationInput(arguments={"arguments_json": json.dumps({"text": "world"})}),
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            ).dump()

        assert payload["result"] == {"text": "hello world"}
        # FS-DEEP-CAND-060: extension resolution must be account-scoped to the caller.
        runtime_logger.get_extension.assert_called_once_with(
            "example_tool_provider", account_id=DEFAULT_ACCOUNT_ID
        )
        logger_cls.assert_not_called()

    def test_execute_tool_denies_cross_account_extension_resolution(self):
        # FS-DEEP-CAND-060: when the tool-provider extension belongs to another
        # account, the account-scoped lookup returns None and resolution fails
        # rather than executing the foreign account's extension.
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.from_db_row(
            {
                "tool_id": "provider.echo_text",
                "display_name": "Echo Text",
                "enabled": True,
                "interface": {
                    "function_name": "echo_text",
                    "parameters": [{"name": "arguments_json", "type": "string", "required": False}],
                    "returns": {"name": "result", "type": "string", "required": False},
                },
                "implementation": {
                    "kind": "extension",
                    "extension_key": "example_tool_provider",
                    "tool_name": "echo_text",
                    "action": "invoke_tool",
                    "provider_key": "example_tool_provider",
                },
            }
        )
        registry_manager = MagicMock()
        registry_manager.tools.get_tool.return_value = manifest
        registry_manager.tools.get_catalog_entry.return_value = manifest.catalog_entry()
        runtime_logger = MagicMock()
        # Account-scoped lookup returns nothing for the caller's account.
        runtime_logger.get_extension.return_value = None

        service = ToolExecutionService(
            get_logger=lambda: runtime_logger,
            get_registry_manager=lambda: registry_manager,
        )

        _raises_input_11456_1 = ToolInvocationInput(
            arguments={"arguments_json": json.dumps({"text": "world"})}
        )
        _raises_input_11456_2 = ScopeContext(account_id="acc_caller")
        with pytest.raises(ServiceUnavailableError):
            service.execute_tool(
                "provider.echo_text", _raises_input_11456_1, scope=_raises_input_11456_2
            )
        runtime_logger.get_extension.assert_called_once_with(
            "example_tool_provider", account_id="acc_caller"
        )

    def test_execute_tool_requires_function_name_for_multi_function_tool(self):
        from core.application.registry.tool_manifest import ToolManifest

        manifest = ToolManifest.from_db_row(
            {
                "tool_id": "connector_tool",
                "display_name": "Connector Tool",
                "enabled": True,
                "interface": {
                    "functions": [
                        {"name": "sync_record", "parameters": []},
                        {"name": "delete_record", "parameters": []},
                    ]
                },
                "implementation": {
                    "kind": "internal",
                    "module": "core.application.orchestration.tools.connector_tools",
                },
            }
        )
        registry_manager = MagicMock()
        registry_manager.tools.get_tool.return_value = manifest
        registry_manager.tool_resolver.resolve.return_value = [
            SimpleNamespace(name="sync_record", invoke=lambda payload: {"ok": True}),
            SimpleNamespace(name="delete_record", invoke=lambda payload: {"ok": True}),
        ]

        service = ToolExecutionService(
            get_logger=lambda: None,
            get_registry_manager=lambda: registry_manager,
        )

        _raises_input_11498_1 = ToolInvocationInput(arguments={})
        with pytest.raises(ServiceValidationError, match="function_name is required"):
            service.execute_tool("connector_tool", _raises_input_11498_1)


class TestProjectCommandService:
    def test_enqueue_manual_task_hides_workflow_contract_exception_details(self):
        secret = "postgresql://registry:super-secret@db.internal/workflows"
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=MagicMock(),
        )

        _raises_input_11527_1 = ProjectTaskEnqueueInput(
            project_id="7",
            task_id="task-77",
            task_summary="Fix the flow",
            workflow_id="wf-1",
            inputs={"title": "Invoice INV-42"},
        )
        with (
            patch(
                "core.application.registry.workflow_contract_service."
                "WorkflowInterfaceService.interface_for_workflow_id",
                side_effect=RuntimeError(secret),
            ),
            pytest.raises(
                ServiceUnavailableError,
                match=r"^Workflow contract service is unavailable; retry later$",
            ) as captured,
        ):
            service.enqueue_manual_task(_raises_input_11527_1)

        assert secret not in str(captured.value)
        assert captured.value.status_code == 503
        assert captured.value.payload["code"] == "workflow_contract_unavailable"

    def test_enqueue_manual_task_requires_caller_task_id_before_side_effects(self):
        bl = MagicMock()
        enqueue_task = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
        )

        _raises_input_11561_1 = ProjectTaskEnqueueInput(
            project_id="7", task_id="   ", task_summary="Fix the flow"
        )
        with pytest.raises(ServiceValidationError, match=r"^task_id is required$") as captured:
            service.enqueue_manual_task(_raises_input_11561_1)

        assert captured.value.payload["code"] == "task_id_required"
        bl.get_project.assert_not_called()
        enqueue_task.assert_not_called()

    def test_project_task_enqueue_command_declares_task_id_as_required(self):
        schema = ProjectTaskEnqueueInput.model_json_schema()

        assert "task_id" in schema["required"]
        assert schema["properties"]["task_id"]["minLength"] == 1

    def test_create_project_keeps_agents_config_sparse_without_overrides(self):
        bl = MagicMock()
        bl.add_project.return_value = 21
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        payload = service.create_project(
            ProjectCreateInput(
                name="Demo Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
            )
        ).dump()

        assert payload["status"] == "created"
        agents_config = bl.add_project.call_args.kwargs["agents_config"]
        assert agents_config == {}
        assert "default_provider" not in agents_config
        assert "default_model" not in agents_config
        assert "agents" not in agents_config

    def test_create_project_honors_explicit_empty_workflow_selection(self):
        bl = MagicMock()
        bl.add_project.return_value = 22
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [
                {"workflow_id": "wf.default"},
                {"workflow_id": "wf.release"},
            ],
        )

        service.create_project(
            ProjectCreateInput(
                name="No Workflow Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=[],
                form_data={"workflow_ids": []},
            )
        )

        assert bl.add_project.call_args.kwargs["project_workflow_ids"] == []

    def test_create_project_uses_create_only_persistence(self):
        bl = MagicMock()
        bl.add_project.return_value = 22
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        service.create_project(
            ProjectCreateInput(
                name="No Silent Overwrite",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=[],
                form_data={"workflow_ids": []},
            )
        )

        assert bl.add_project.call_args.kwargs["create_only"] is True

    def test_create_project_persists_extension_workflow_anchor_before_binding(self):
        workflow_id = "extension.install-demo.wf_extension_demo"
        bl = MagicMock()
        bl.add_project.return_value = 23
        bl.db_store.get_workflow.return_value = None
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [
                {
                    "workflow_id": workflow_id,
                    "display_name": "Extension Demo Workflow",
                    "enabled": True,
                }
            ],
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                selected,
                "explicit",
            ),
        )

        with patch.object(service, "_extension_workflow_ids", return_value={workflow_id}):
            result = service.create_project(
                ProjectCreateInput(
                    name="Extension Workflow Project",
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    workflow_ids=[workflow_id],
                    operator_default_workflow_id=workflow_id,
                    form_data={"workflow_ids": [workflow_id]},
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        assert result.project_id == "23"
        bl.db_store.save_workflow.assert_called_once_with(
            {
                "workflow_id": workflow_id,
                "display_name": "Extension Demo Workflow",
                "enabled": True,
                "source_install_id": "install-demo",
                "projection_mode": "derived",
            },
            author="admin",
            create_only=True,
        )
        call_names = [item[0] for item in bl.method_calls]
        assert call_names.index("db_store.save_workflow") < call_names.index("add_project")

    def test_project_binding_repairs_technical_extension_anchor_display_name(self):
        workflow_id = "extension.install-demo.wf_extension_demo"
        bl = MagicMock()
        bl.db_store.get_workflow.return_value = {
            "workflow_id": workflow_id,
            "revision": 7,
            "workflow_key": workflow_id,
            "display_name": workflow_id,
            "version": "1.0.0",
            "description": "",
            "enabled": True,
            "trigger": {},
            "settings": {},
            "tags": [],
            "source_install_id": "install-demo",
            "projection_mode": "derived",
            "steps": [{"step_id": "must_not_be_materialized"}],
        }
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [
                {
                    "workflow_id": workflow_id,
                    "display_name": "Extension Demo Workflow",
                    "enabled": True,
                }
            ],
        )

        service._ensure_extension_workflow_anchors(
            bl=bl,
            workflow_ids=[workflow_id],
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin",
            transaction_conn=None,
        )

        payload = bl.db_store.save_workflow.call_args.args[0]
        assert bl.db_store.save_workflow.call_args.kwargs["expected_revision"] == 7
        assert payload["display_name"] == "Extension Demo Workflow"
        assert payload["workflow_id"] == workflow_id
        assert "steps" not in payload
        assert "groups" not in payload

    def test_create_project_rejects_disabled_extension_workflow_binding(self):
        workflow_id = "extension.install-demo.wf_extension_demo"
        bl = MagicMock()
        bl.get_workflow = None
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **_kwargs: [
                {
                    "workflow_id": workflow_id,
                    "enabled": True,
                    "source_install_id": "install-demo",
                }
            ],
        )

        _raises_input_11752_1 = ProjectCreateInput(
            name="Disabled Extension Workflow Project",
            workspace_id=DEFAULT_WORKSPACE_ID,
            workflow_ids=[workflow_id],
            form_data={"workflow_ids": [workflow_id]},
        )
        with (
            patch.object(service, "_extension_workflow_ids", return_value=set()),
            pytest.raises(ServiceValidationError, match="Unknown workflow_id"),
        ):
            service.create_project(_raises_input_11752_1)

        bl.add_project.assert_not_called()

    def test_create_project_rolls_back_extension_anchor_with_project_failure(self):
        from core.infrastructure.db.db_pool import PooledConnectionProxy

        workflow_id = "extension.install-demo.wf_extension_demo"

        class _Connection:
            autocommit = True
            closed = 0

            def __init__(self):
                self.rollbacks = 0
                self.commits = 0

            def get_transaction_status(self):
                return 0

            def rollback(self):
                self.rollbacks += 1

            def commit(self):
                self.commits += 1

        class _Pool:
            def __init__(self):
                self.connection = _Connection()

            def getconn(self):
                return self.connection

            def putconn(self, *_args, **_kwargs):
                return None

        pool = _Pool()
        registry_store = MagicMock()
        registry_store.get_workflow.return_value = None
        bl = MagicMock()
        bl.dsn = "postgresql://fixture"
        bl.conn = PooledConnectionProxy(pool)
        bl.db_store = registry_store
        bl.add_project.side_effect = RuntimeError("project insert failure")
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                selected,
                "explicit",
            ),
        )

        _raises_input_11816_1 = ProjectCreateInput(
            name="Atomic Extension Project",
            workspace_id=DEFAULT_WORKSPACE_ID,
            workflow_ids=[workflow_id],
            operator_default_workflow_id=workflow_id,
            form_data={"workflow_ids": [workflow_id]},
        )
        _raises_input_11816_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch.object(service, "_extension_workflow_ids", return_value={workflow_id}),
            patch.object(service, "_require_workspace_in_scope"),
            pytest.raises(ServiceError, match="Failed to create project"),
        ):
            service.create_project(_raises_input_11816_1, scope=_raises_input_11816_2)

        registry_store._set_tx_conn.assert_has_calls([call(pool.connection), call(None)])
        registry_store.save_workflow.assert_called_once()
        assert pool.connection.rollbacks == 1
        assert pool.connection.commits == 0

    def test_create_project_reports_duplicate_as_conflict(self):
        from core.infrastructure.db.project_registration_store import PROJECT_ALREADY_EXISTS

        bl = MagicMock()
        bl.add_project.side_effect = ValueError(PROJECT_ALREADY_EXISTS)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        _raises_input_11851_1 = ProjectCreateInput(
            name="Existing Project",
            workspace_id=DEFAULT_WORKSPACE_ID,
            workflow_ids=[],
            form_data={"workflow_ids": []},
        )
        with pytest.raises(ServiceConflictError, match="already exists"):
            service.create_project(_raises_input_11851_1)

    @pytest.mark.parametrize("row_kind", ["dict", "tuple"])
    def test_workspace_admission_returns_scoped_active_cursor_row(self, row_kind):
        workspace = {
            "id": DEFAULT_WORKSPACE_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "workspace_key": "default",
            "display_name": "Default",
            "status": "active",
            "deleted_at": None,
        }
        bl = MagicMock()
        cursor = bl.conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (
            workspace if row_kind == "dict" else tuple(workspace.values())
        )

        admitted = ProjectCommandService._require_workspace_in_scope(
            bl,
            DEFAULT_WORKSPACE_ID,
            account_id=DEFAULT_ACCOUNT_ID,
            tenant_id=workspace["tenant_id"],
            allowed_workspace_ids=frozenset({DEFAULT_WORKSPACE_ID}),
        )

        assert admitted == workspace

    def test_create_project_rejects_workspace_outside_allowlist_before_lookup(self):
        bl = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **_kwargs: [],
        )
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            tenant_id="00000000-0000-0000-0000-000000000001",
            allowed_workspace_ids=frozenset({"11111111-1111-1111-1111-111111111111"}),
        )
        command = ProjectCreateInput(name="Denied create", workspace_id=DEFAULT_WORKSPACE_ID)

        with pytest.raises(
            ServiceForbiddenError, match=r"^Workspace is outside allowed scope$"
        ) as refused:
            service.create_project(command, scope=scope)

        assert refused.value.status_code == 403
        assert refused.value.payload == {"error": "Workspace is outside allowed scope"}
        bl.conn.cursor.assert_not_called()
        bl.add_project.assert_not_called()

    def test_create_project_rejects_unknown_workspace_before_persistence(self):
        bl = MagicMock()
        bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        _raises_input_11869_1 = ProjectCreateInput(
            name="Unknown Workspace",
            workspace_id="11111111-1111-1111-1111-111111111111",
            workflow_ids=[],
            form_data={"workflow_ids": []},
        )
        with pytest.raises(ServiceNotFoundError, match="Workspace not found"):
            service.create_project(_raises_input_11869_1)

        bl.add_project.assert_not_called()

    def test_create_project_rejects_foreign_account_workspace_before_persistence(self):
        foreign_workspace_id = "22222222-2222-2222-2222-222222222222"
        default_tenant_id = "00000000-0000-0000-0000-000000000001"
        bl = MagicMock()
        cursor = bl.conn.cursor.return_value.__enter__.return_value
        # The workspace exists globally, but the account-qualified lookup must not see it.
        cursor.fetchone.return_value = None
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        _raises_input_11893_1 = ProjectCreateInput(
            name="Foreign Workspace Project",
            workspace_id=foreign_workspace_id,
            workflow_ids=[],
            form_data={"workflow_ids": []},
        )
        _raises_input_11893_2 = command_services_module.normalize_scope(
            {"account_id": DEFAULT_ACCOUNT_ID, "tenant_id": default_tenant_id}
        )
        with pytest.raises(ServiceNotFoundError, match="Workspace not found"):
            service.create_project(_raises_input_11893_1, scope=_raises_input_11893_2)

        workspace_lookup = cursor.execute.call_args
        assert "AND account_id = %s" in workspace_lookup.args[0]
        assert "COALESCE(tenant_id::text, '') = %s" in workspace_lookup.args[0]
        assert workspace_lookup.args[1] == (
            foreign_workspace_id,
            DEFAULT_ACCOUNT_ID,
            default_tenant_id,
        )
        bl.add_project.assert_not_called()

    def test_create_project_ignores_untyped_legacy_extra_tools_without_materializing_config(self):
        assert "extra_tools_json" not in ProjectCreateInput.model_fields
        legacy_json = json.dumps(
            [
                {
                    "name": "retired_http",
                    "description": "Inert legacy definition",
                    "type": "http",
                    "url": "https://retired.example.invalid/hook",
                    "method": "POST",
                }
            ]
        )
        bl = MagicMock()
        bl.add_project.return_value = 23
        bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
            "id": DEFAULT_WORKSPACE_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        # Simulate an old internal caller carrying an untyped legacy value.
        # This does not require a blanket change to ViewModel extra handling.
        command = ProjectCreateInput(
            name="Legacy Input Project",
            workspace_id=DEFAULT_WORKSPACE_ID,
            description="Keep ordinary description",
            project_rules="Keep ordinary rules",
            workflow_ids=[],
            form_data={"workflow_ids": [], "extra_tools_json": legacy_json},
        ).model_copy(update={"extra_tools_json": legacy_json})
        assert command.extra_tools_json == legacy_json

        result = service.create_project(command).dump()

        assert result["status"] == "created"
        assert result["project_id"] == "23"
        bl.add_project.assert_called_once()
        persisted = bl.add_project.call_args.kwargs
        assert persisted["agents_config"] == {}
        assert "extra_tools" not in persisted["agents_config"]
        assert persisted["name"] == "Legacy Input Project"
        assert persisted["workspace_id"] == DEFAULT_WORKSPACE_ID
        assert persisted["description"] == "Keep ordinary description"
        assert persisted["project_rules"] == "Keep ordinary rules"

    def test_create_project_does_not_synthesize_provider_credentials_or_binding(self):
        bl = MagicMock()
        bl.add_project.return_value = 17
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        payload = service.create_project(
            ProjectCreateInput(
                name="Demo Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
            )
        ).dump()

        assert payload["project_id"] == "17"
        bl.save_provider_secret.assert_not_called()
        bl.save_project_provider_binding.assert_not_called()

    def test_create_project_persists_assigned_data_connections(self):
        bl = MagicMock()
        bl.add_project.return_value = 18
        bl.get_project_run_config.return_value = {
            "project_name": "Secret DSN Project",
        }
        bl.set_project_data_connections.return_value = [
            {"connection_id": "warehouse-primary", "display_name": "Warehouse Primary"}
        ]
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        service.create_project(
            ProjectCreateInput(
                name="Secret DSN Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
                data_connection_ids=["warehouse-primary"],
            )
        )

        bl.set_project_data_connections.assert_called_once_with(
            "18",
            ["warehouse-primary"],
        )

    def test_update_project_returns_updated_status_for_selected_workflow(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "General Demo",
            "agents_config": {},
        }
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "wf.default"}],
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                selected or (project_workflows[0]["workflow_id"] if project_workflows else ""),
                "explicit",
            ),
        )

        payload = service.update_project(
            7,
            ProjectUpdateInput(
                name="General Demo",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=["wf.default"],
            ),
        ).dump()

        assert payload["status"] == "updated"
        assert bl.update_project.call_args.kwargs["name"] == "General Demo"

    def test_update_project_accepts_create_valid_empty_workflow_set_without_sync_work(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "General Demo",
            "agents_config": {},
            "workspace_id": DEFAULT_WORKSPACE_ID,
        }
        bl.get_project_run_config.side_effect = [
            {
                "project_name": "General Demo",
            },
            {
                "project_name": "General Demo",
            },
        ]
        bl.list_project_data_connections.return_value = []
        write_project_profile_to_kb = MagicMock(
            side_effect=AssertionError("project save must not synchronously write KB storage")
        )
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
            write_project_profile_to_kb=write_project_profile_to_kb,
        )

        payload = service.update_project(
            7,
            ProjectUpdateInput(
                name="General Demo",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=[],
                form_data={
                    "name": "General Demo",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "workflow_ids": [],
                },
            ),
        ).dump()

        assert payload["status"] == "updated"
        assert "project_workflow_ids" not in bl.update_project.call_args.kwargs
        bl.save_project_workflows.assert_called_once_with("7", [], operator_default_workflow_id="")
        write_project_profile_to_kb.assert_not_called()

    def test_full_project_update_persists_ten_extension_workflow_bindings(self):
        workflow_ids = [f"extension.install-demo.demo_{index}" for index in range(10)]
        bl = MagicMock()
        bl.db_store.get_workflow.return_value = None
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Extension Demo",
            "description": "Prior description",
            "agents_config": {},
            "workspace_id": DEFAULT_WORKSPACE_ID,
        }
        bl.get_project_run_config.return_value = {
            "project_name": "Extension Demo",
            "description": "Prior description",
        }
        bl.list_project_data_connections.return_value = []
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                selected,
                "explicit",
            ),
            list_workflow_choices=lambda **kwargs: [],
        )

        with patch.object(service, "_extension_workflow_ids", return_value=set(workflow_ids)):
            result = service.update_project(
                7,
                ProjectUpdateInput(
                    name="Extension Demo",
                    description="Preserved description",
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    workflow_ids=workflow_ids,
                    operator_default_workflow_id=workflow_ids[4],
                    form_data={
                        "name": "Extension Demo",
                        "description": "Preserved description",
                        "workspace_id": DEFAULT_WORKSPACE_ID,
                        "workflow_ids": workflow_ids,
                    },
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        assert result.status == "updated"
        assert bl.update_project.call_args.kwargs["description"] == "Preserved description"
        assert [
            item.args[0]["workflow_id"] for item in bl.db_store.save_workflow.call_args_list
        ] == workflow_ids
        bl.save_project_workflows.assert_called_once_with(
            "7",
            workflow_ids,
            operator_default_workflow_id=workflow_ids[4],
        )

    def test_full_project_update_rolls_back_fields_anchors_and_bindings_together(self):
        from core.infrastructure.db.db_pool import PooledConnectionProxy

        workflow_ids = [f"extension.install-demo.demo_{index}" for index in range(10)]
        prior_state = {
            "project": {"name": "Before", "description": "Before description"},
            "anchors": {"wf.before"},
            "bindings": ["wf.before"],
        }

        class _Cursor:
            def execute(self, statement, parameters):
                assert "FROM workspaces" in statement
                assert parameters[0] == DEFAULT_WORKSPACE_ID
                assert parameters[1] == DEFAULT_ACCOUNT_ID
                self.workspace = {
                    "id": DEFAULT_WORKSPACE_ID,
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "tenant_id": parameters[2],
                    "workspace_key": "default",
                    "display_name": "Default",
                    "status": "active",
                    "deleted_at": None,
                }

            def fetchone(self):
                return self.workspace

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class _Connection:
            autocommit = True
            closed = 0

            def __init__(self):
                self.durable = {
                    "project": dict(prior_state["project"]),
                    "anchors": set(prior_state["anchors"]),
                    "bindings": list(prior_state["bindings"]),
                }
                self.working = self._copy(self.durable)

            @staticmethod
            def _copy(state):
                return {
                    "project": dict(state["project"]),
                    "anchors": set(state["anchors"]),
                    "bindings": list(state["bindings"]),
                }

            def get_transaction_status(self):
                return 0

            def cursor(self):
                return _Cursor()

            def rollback(self):
                self.working = self._copy(self.durable)

            def commit(self):
                self.durable = self._copy(self.working)

        class _Pool:
            def __init__(self):
                self.connection = _Connection()

            def getconn(self):
                return self.connection

            def putconn(self, *_args, **_kwargs):
                return None

        pool = _Pool()
        bl = MagicMock()
        bl.dsn = "postgresql://fixture"
        bl.conn = PooledConnectionProxy(pool)
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Before",
            "description": "Before description",
            "agents_config": {},
            "workspace_id": DEFAULT_WORKSPACE_ID,
        }
        bl.get_project_run_config.return_value = {
            "project_name": "Before",
            "description": "Before description",
        }
        bl.list_project_data_connections.return_value = []
        registry_store = MagicMock()
        registry_store.get_workflow.return_value = None
        bl.db_store = registry_store

        def update_project(*_args, **kwargs):
            pool.connection.working["project"] = {
                "name": kwargs["name"],
                "description": kwargs["description"],
            }

        def save_anchor(payload, **_kwargs):
            pool.connection.working["anchors"].add(payload["workflow_id"])

        def fail_on_fifth_binding(_project_id, selected_ids, **_kwargs):
            for index, workflow_id in enumerate(selected_ids):
                if index == 4:
                    raise RuntimeError("fifth binding write failed")
                pool.connection.working["bindings"].append(workflow_id)

        bl.update_project.side_effect = update_project
        registry_store.save_workflow.side_effect = save_anchor
        bl.save_project_workflows.side_effect = fail_on_fifth_binding
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                selected,
                "explicit",
            ),
            list_workflow_choices=lambda **kwargs: [],
        )

        _raises_input_12365_1 = ProjectUpdateInput(
            name="After",
            description="After description",
            workspace_id=DEFAULT_WORKSPACE_ID,
            workflow_ids=workflow_ids,
            operator_default_workflow_id=workflow_ids[4],
            form_data={
                "name": "After",
                "description": "After description",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "workflow_ids": workflow_ids,
            },
        )
        _raises_input_12365_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch.object(service, "_extension_workflow_ids", return_value=set(workflow_ids)),
            pytest.raises(ServiceError, match="Failed to update project"),
        ):
            service.update_project(7, _raises_input_12365_1, scope=_raises_input_12365_2)

        assert pool.connection.durable == prior_state
        assert pool.connection.working == prior_state
        registry_store._set_tx_conn.assert_has_calls([call(pool.connection), call(None)])

    def test_project_profile_payload_serializes_connection_timestamps_consistently(self):
        service = ProjectCommandService(get_logger=lambda: MagicMock())

        payload = service._project_profile_payload(
            project_id="project-1",
            account_id=DEFAULT_ACCOUNT_ID,
            name="Demo",
            agents_config={},
            project_rules="",
            assigned_data_connections=[
                {
                    "connection_id": "warehouse",
                    "updated_at": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
                }
            ],
        )

        assert (
            json.loads(json.dumps(payload))["assigned_data_connections"][0]["updated_at"]
            == "2026-08-03T12:00:00+00:00"
        )

    def test_create_project_profile_sync_includes_project_scope(self):
        bl = MagicMock()
        bl.add_project.return_value = 27
        write_project_profile_to_kb = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
            write_project_profile_to_kb=write_project_profile_to_kb,
        )

        async def _run() -> None:
            service.create_project(
                ProjectCreateInput(
                    name="Scoped Project",
                    workspace_id=DEFAULT_WORKSPACE_ID,
                )
            )
            for _ in range(20):
                if write_project_profile_to_kb.called:
                    break
                await asyncio.sleep(0.01)

        _run_async(_run())

        write_project_profile_to_kb.assert_called_once()
        payload = write_project_profile_to_kb.call_args.args[0]
        assert payload["id"] == "27"
        assert payload["account_id"] == DEFAULT_ACCOUNT_ID

    def test_create_project_schedules_profile_storage_off_request_path(self):
        bl = MagicMock()
        bl.add_project.return_value = 28
        started = threading.Event()
        release = threading.Event()

        def write_project_profile_to_kb(_payload):
            started.set()
            assert release.wait(timeout=2)

        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
            write_project_profile_to_kb=write_project_profile_to_kb,
        )

        async def _run() -> None:
            loop = asyncio.get_running_loop()
            before = loop.time()
            payload = service.create_project(
                ProjectCreateInput(
                    name="Non-blocking Project",
                    workspace_id=DEFAULT_WORKSPACE_ID,
                )
            ).dump()
            elapsed = loop.time() - before
            assert payload["status"] == "created"
            assert elapsed < 0.5
            assert await asyncio.to_thread(started.wait, 1)
            release.set()
            await asyncio.sleep(0.05)

        _run_async(_run())

    def test_move_project_to_workspace_requires_target_workspace_in_scope(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": "7",
            "name": "General Demo",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        service = ProjectCommandService(get_logger=lambda: bl)

        _raises_input_12619_1 = command_services_module.normalize_scope(
            {"account_id": DEFAULT_ACCOUNT_ID, "tenant_id": "00000000-0000-0000-0000-000000000001"}
        )
        with pytest.raises(ServiceNotFoundError, match="Workspace not found"):
            service.move_project_to_workspace("7", "workspace-other", scope=_raises_input_12619_1)

    def test_move_project_to_workspace_updates_parent_only_after_scope_check(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": "7",
            "name": "General Demo",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {
                "id": "workspace-other",
                "account_id": DEFAULT_ACCOUNT_ID,
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "workspace_key": "other",
                "display_name": "Other",
                "status": "active",
                "deleted_at": None,
            },
            {"id": "7"},
        ]
        service = ProjectCommandService(get_logger=lambda: bl)

        payload = service.move_project_to_workspace(
            "7",
            "workspace-other",
            scope=command_services_module.normalize_scope(
                {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                }
            ),
        ).dump()

        assert payload["status"] == "moved"
        assert "SET workspace_id = %s" in cur.execute.call_args_list[-1].args[0]

    def test_move_project_to_workspace_skips_updated_at_when_column_missing(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": "7",
            "name": "General Demo",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl._column_exists.return_value = False
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {
                "id": "workspace-other",
                "account_id": DEFAULT_ACCOUNT_ID,
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "workspace_key": "other",
                "display_name": "Other",
                "status": "active",
                "deleted_at": None,
            },
            {"id": "7"},
        ]
        service = ProjectCommandService(get_logger=lambda: bl)

        payload = service.move_project_to_workspace(
            "7",
            "workspace-other",
            scope=command_services_module.normalize_scope(
                {
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                }
            ),
        ).dump()

        assert payload["status"] == "moved"
        sql = cur.execute.call_args_list[-1].args[0]
        assert "SET workspace_id = %s" in sql
        assert "updated_at = now()" not in sql

    def test_create_extension_project_is_refused_outside_extensions_surface(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": 7,
            "name": "Parent Project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        }
        bl.get_projects.return_value = [
            {"id": 7, "name": "Parent Project", "account_id": DEFAULT_ACCOUNT_ID}
        ]
        bl.add_project.return_value = 23
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        _raises_input_12748_1 = ExtensionProjectCreateInput(
            parent_project_id="7",
            name="Parent Project API Extension",
            request_summary="Build a custom connector for the API",
        )
        _raises_input_12748_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with pytest.raises(
            ServiceValidationError,
            match="Extensions are managed through the Extensions surface",
        ):
            service.create_extension_project(_raises_input_12748_1, scope=_raises_input_12748_2)

        bl.add_project.assert_not_called()

    def test_create_extension_project_requires_admin_scope(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": 7,
            "name": "Parent Project",
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        }
        bl.get_projects.return_value = [
            {"id": 7, "name": "Parent Project", "account_id": DEFAULT_ACCOUNT_ID}
        ]
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [],
        )

        _raises_input_12784_1 = ExtensionProjectCreateInput(
            parent_project_id="7",
            name="Parent Project API Extension",
            request_summary="Build a custom connector for the API",
        )
        _raises_input_12784_2 = ScopeContext(
            account_id="acc-1", actor_id="ops", principal_type="operator"
        )
        with pytest.raises(
            ServiceValidationError,
            match="Extensions are managed through the Extensions surface",
        ):
            service.create_extension_project(_raises_input_12784_1, scope=_raises_input_12784_2)

    def test_delete_project_returns_deleted_payload(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 3, "name": "demo"}
        mock_cur = MagicMock()
        bl.conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        bl.conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        service = ProjectCommandService(
            get_logger=lambda: bl, resolve_workflow_selection=lambda *args, **kwargs: ("", "")
        )

        payload = service.delete_project(3).dump()

        assert payload["status"] == "deleted"
        assert payload["project_id"] == "3"
        # Verify soft-delete SQL was executed (UPDATE ... SET deleted_at)
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "deleted_at" in sql
        assert "UPDATE projects" in sql

    def test_update_project_workflows_persists_unique_ids_and_default(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 7, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_workflows.return_value = [
            {"workflow_id": "wf-1", "is_default": True},
            {"workflow_id": "wf-2", "is_default": False},
        ]
        bl.list_workflow_choices.return_value = [
            {"workflow_id": "wf-1"},
            {"workflow_id": "wf-2"},
        ]
        bl.get_workflow.side_effect = lambda workflow_id: {"workflow_id": workflow_id}
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                "wf-1",
                "explicit",
            ),
        )
        service._list_workflow_choices = MagicMock(  # type: ignore[method-assign]
            return_value=[{"workflow_id": "wf-1"}, {"workflow_id": "wf-2"}]
        )

        payload = service.update_project_workflows(
            7,
            ProjectWorkflowBindingsInput(
                workflow_ids=["wf-1", "wf-2", "wf-1"], operator_default_workflow_id="wf-1"
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="admin",
                principal_type="admin",
            ),
        ).dump()

        assert payload["operator_default_workflow_id"] == "wf-1"
        assert bl.save_project_workflows.call_args.args[1] == ["wf-1", "wf-2"]

    def test_update_project_workflows_allows_restoring_an_empty_binding_set(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 7, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        )

        payload = service.update_project_workflows(
            7,
            ProjectWorkflowBindingsInput(workflow_ids=[], operator_default_workflow_id=""),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="admin",
                principal_type="admin",
            ),
        ).dump()

        assert payload["project_workflows"] == []
        assert payload["operator_default_workflow_id"] == ""
        bl.save_project_workflows.assert_called_once_with("7", [], operator_default_workflow_id="")

    def test_update_project_workflows_requires_admin_scope(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 7, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                "wf-1",
                "explicit",
            ),
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "wf-1"}],
        )

        _raises_input_12912_1 = ProjectWorkflowBindingsInput(
            workflow_ids=["wf-1"], operator_default_workflow_id="wf-1"
        )
        _raises_input_12912_2 = ScopeContext(
            account_id="acc-1", actor_id="ops", principal_type="operator"
        )
        with pytest.raises(ServiceForbiddenError, match="Admin role required"):
            service.update_project_workflows(7, _raises_input_12912_1, scope=_raises_input_12912_2)

    def test_update_project_workflows_rejects_foreign_account_global_catalog_access(self):
        foreign_account_id = "11111111-1111-1111-1111-111111111111"
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": 7,
            "name": "dormant tenant project",
            "account_id": foreign_account_id,
        }
        list_workflow_choices = MagicMock(return_value=[{"workflow_id": "global-workflow"}])
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("global-workflow", "explicit"),
            list_workflow_choices=list_workflow_choices,
        )

        _raises_input_12937_1 = ProjectWorkflowBindingsInput(
            workflow_ids=["global-workflow"], operator_default_workflow_id="global-workflow"
        )
        _raises_input_12937_2 = ScopeContext(
            account_id=foreign_account_id, actor_id="foreign-admin", principal_type="admin"
        )
        with pytest.raises(ServiceForbiddenError, match="default account"):
            service.update_project_workflows(7, _raises_input_12937_1, scope=_raises_input_12937_2)

        list_workflow_choices.assert_not_called()
        bl.save_project_workflows.assert_not_called()

    def test_update_project_workflows_rejects_unknown_workflow_ids(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "demo"}
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                "wf-1",
                "explicit",
            ),
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "wf-1"}],
        )

        _raises_input_12966_1 = ProjectWorkflowBindingsInput(
            workflow_ids=["wf-unknown"], operator_default_workflow_id="wf-unknown"
        )
        _raises_input_12966_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with pytest.raises(ServiceValidationError, match="Unknown workflow_id"):
            service.update_project_workflows(7, _raises_input_12966_1, scope=_raises_input_12966_2)

        bl.save_project_workflows.assert_not_called()

    def test_update_project_workflows_accepts_extension_workflow_ids(self):
        bl = MagicMock()
        bl.db_store = MagicMock()
        bl.db_store.get_workflow.return_value = None
        bl.get_project.return_value = {"id": 7, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_workflows.return_value = []
        bl.get_workflow.return_value = None
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                "extension.install-demo.wf_extension_demo",
                "explicit",
            ),
            list_workflow_choices=lambda **kwargs: [],
        )

        with patch.object(
            service,
            "_extension_workflow_ids",
            return_value={"extension.install-demo.wf_extension_demo"},
        ):
            service.update_project_workflows(
                7,
                ProjectWorkflowBindingsInput(
                    workflow_ids=["extension.install-demo.wf_extension_demo"],
                    operator_default_workflow_id="extension.install-demo.wf_extension_demo",
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="admin",
                    principal_type="admin",
                ),
            )

        bl.save_project_workflows.assert_called_once_with(
            "7",
            ["extension.install-demo.wf_extension_demo"],
            operator_default_workflow_id="extension.install-demo.wf_extension_demo",
        )
        assert bl.db_store.save_workflow.call_args.kwargs["author"] == "admin"

    def test_update_project_workflows_rolls_back_extension_anchor_with_binding_failure(self):
        from core.infrastructure.db.db_pool import PooledConnectionProxy

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class _Connection:
            autocommit = True
            closed = 0

            def __init__(self):
                self.rollbacks = 0
                self.commits = 0

            def get_transaction_status(self):
                return 0

            def cursor(self):
                return _Cursor()

            def rollback(self):
                self.rollbacks += 1

            def commit(self):
                self.commits += 1

        class _Pool:
            def __init__(self):
                self.connection = _Connection()

            def getconn(self):
                return self.connection

            def putconn(self, *_args, **_kwargs):
                return None

        pool = _Pool()
        registry_store = MagicMock()
        registry_store.get_workflow.return_value = None
        bl = MagicMock()
        bl.dsn = "postgresql://fixture"
        bl.conn = PooledConnectionProxy(pool)
        bl.db_store = registry_store
        bl.get_project.return_value = {"id": 7, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_workflows.return_value = []
        bl.get_workflow.return_value = None
        bl.save_project_workflows.side_effect = RuntimeError("binding failure")
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda selected, project_workflows=None: (
                selected[0],
                "explicit",
            ),
            list_workflow_choices=lambda **kwargs: [],
        )

        _raises_input_13082_1 = ProjectWorkflowBindingsInput(
            workflow_ids=["extension.install-demo.wf_extension_demo"],
            operator_default_workflow_id="extension.install-demo.wf_extension_demo",
        )
        _raises_input_13082_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin"
        )
        with (
            patch.object(
                service,
                "_extension_workflow_ids",
                return_value={"extension.install-demo.wf_extension_demo"},
            ),
            pytest.raises(ServiceError, match="Failed to update project workflows"),
        ):
            service.update_project_workflows(7, _raises_input_13082_1, scope=_raises_input_13082_2)

        registry_store._set_tx_conn.assert_has_calls([call(pool.connection), call(None)])
        assert pool.connection.rollbacks == 1
        assert pool.connection.commits == 0

    def test_update_project_policy_returns_readback_payload(self):
        bl = MagicMock()
        bl.get_project.return_value = {"id": 9, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_run_config.return_value = {"project_policy": {}}
        bl.get_project_policy.return_value = {"autonomy_mode": "auto"}
        service = ProjectCommandService(
            get_logger=lambda: bl, resolve_workflow_selection=lambda *args, **kwargs: ("", "")
        )

        payload = service.update_project_policy(
            9,
            ProjectPolicyUpdateInput(autonomy_mode="auto"),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="admin",
                principal_type="admin",
            ),
        ).dump()

        assert payload["policy"]["autonomy_mode"] == "auto"
        assert payload["project_id"] == "9"
        bl.save_project_policy.assert_called_once_with("9", {"autonomy_mode": "auto"})

    def test_partial_policy_update_leaves_unmentioned_fields_alone(self):
        """AC17-R58: PUT {"concurrency_cap": 2} used to reset the whole policy document.

        The write is an unconditional UPSERT of every policy column, and the request model
        carried class defaults, so every field the caller did not mention arrived at the
        database as a default. A project configured for auto autonomy and permissive tools
        was quietly downgraded by a request that only meant to change concurrency.
        """
        bl = MagicMock()
        bl.get_project.return_value = {"id": 9, "name": "demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_project_run_config.return_value = {
            "project_policy": {
                "autonomy_mode": "auto",
                "autonomy_confidence_threshold": 0.55,
                "retry_strategy": "failure_class",
                "concurrency_cap": 1,
                "tool_policy_mode": "permissive",
            }
        }
        bl.get_project_policy.return_value = {"autonomy_mode": "auto"}
        service = ProjectCommandService(
            get_logger=lambda: bl, resolve_workflow_selection=lambda *args, **kwargs: ("", "")
        )

        service.update_project_policy(
            9,
            ProjectPolicyUpdateInput(concurrency_cap=2),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="admin",
                principal_type="admin",
            ),
        )

        saved = bl.save_project_policy.call_args[0][1]
        assert saved["concurrency_cap"] == 2
        assert saved["autonomy_mode"] == "auto"
        assert saved["autonomy_confidence_threshold"] == 0.55
        assert saved["retry_strategy"] == "failure_class"
        assert saved["tool_policy_mode"] == "permissive"

    def test_update_project_policy_requires_admin_scope(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 9, "name": "demo"}
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        )

        _raises_input_13202_1 = ProjectPolicyUpdateInput(autonomy_mode="supervised", checks=[])
        _raises_input_13202_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="ops", principal_type="operator"
        )
        with pytest.raises(ServiceForbiddenError, match="Admin role required"):
            service.update_project_policy(9, _raises_input_13202_1, scope=_raises_input_13202_2)

    def test_restore_project_settings_revision_uses_author(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 5, "name": "demo"}
        bl.list_project_data_connections.return_value = []
        bl.restore_project_settings_revision.return_value = {
            "status": "ok",
            "project_id": "5",
            "revision_id": "11111111-1111-1111-1111-111111111111",
            "new_revision_id": "22222222-2222-2222-2222-222222222222",
        }
        service = ProjectCommandService(
            get_logger=lambda: bl, resolve_workflow_selection=lambda *args, **kwargs: ("", "")
        )

        payload = service.restore_project_settings_revision(
            5,
            "11111111-1111-1111-1111-111111111111",
            ProjectSettingsRestoreInput(author="alice"),
        ).dump()

        assert payload["status"] == "ok"
        assert payload["revision_id"] == "11111111-1111-1111-1111-111111111111"
        assert payload["new_revision_id"] == "22222222-2222-2222-2222-222222222222"
        assert bl.restore_project_settings_revision.call_args.kwargs["author"] == "alice"

    def test_enqueue_manual_task_resolves_workflow_and_sets_meta(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=91)
        validate_override = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            validate_explicit_workflow_override=validate_override,
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: str(value or "").strip().lower() == "smoke",
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )

        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["ui"],
                            "input_mode": "passthrough",
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            payload = service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-77",
                    task_summary="  Fix the flow  ",
                    workflow_id="wf-1",
                    test_mode="smoke",
                    task_source="web",
                    inputs={"title": "Invoice INV-42", "invoice_id": "INV-42"},
                    extra_state={"origin": "ui"},
                    job_meta={"trace_id": "abc"},
                )
            ).dump()

        assert payload["job_id"] == "91"
        assert payload["queue"] == "platform.project.7"
        validate_override.assert_called_once()
        assert enqueue_task.call_args.kwargs["task_summary"] == "Fix the flow"
        assert enqueue_task.call_args.kwargs["extra_state"]["workflow_id"] == "wf-1"
        assert enqueue_task.call_args.kwargs["meta"]["selected_workflow_id"] == "wf-1"
        assert enqueue_task.call_args.kwargs["meta"]["requested_workflow_id"] == "wf-1"
        assert enqueue_task.call_args.kwargs["meta"]["entry_path"] == "manual_ui"
        assert enqueue_task.call_args.kwargs["meta"]["input_contract"] == "task_envelope"
        assert (
            enqueue_task.call_args.kwargs["extra_state"]["task_envelope"]["entry_path"]
            == "manual_ui"
        )
        assert enqueue_task.call_args.kwargs["extra_state"]["inputs"] == {
            "title": "Invoice INV-42",
            "invoice_id": "INV-42",
        }
        assert enqueue_task.call_args.kwargs["extra_state"]["inputs"] == {
            "title": "Invoice INV-42",
            "invoice_id": "INV-42",
        }
        assert enqueue_task.call_args.kwargs["test_mode"] is True
        # RS5-027: `origin` is one of the four public annotations the submission
        # allowlist advertises, but unlike description, priority and task_type it
        # fed nothing downstream, so it was accepted, validated and then dropped
        # before the job was written. It must reach the persisted job meta.
        assert enqueue_task.call_args.kwargs["meta"]["origin"] == "ui"

    def test_enqueue_manual_task_strips_reserved_runtime_scope_from_extra_state(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=91)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )

        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["ui"],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-77",
                    task_summary="Fix the flow",
                    workflow_id="wf-1",
                    task_source="web",
                    extra_state={
                        "origin": "ui",
                        "account_id": "victim_account",
                        "project_id": "victim_project",
                        "job_id": "victim_job",
                        "workflow_id": "victim_workflow",
                        "project_config": {
                            "account_id": "victim_account",
                            "project_id": "victim_project",
                        },
                        "_trusted_runtime_context": {
                            "account_id": "victim_account",
                            "project_id": "victim_project",
                        },
                    },
                )
            )

        extra_state = enqueue_task.call_args.kwargs["extra_state"]
        assert extra_state["origin"] == "ui"
        assert extra_state["workflow_id"] == "wf-1"
        assert "account_id" not in extra_state
        assert "project_id" not in extra_state
        assert "job_id" not in extra_state
        assert "project_config" not in extra_state
        assert "_trusted_runtime_context" not in extra_state
        assert (
            extra_state["task_envelope"]["project_id"]
            == str(enqueue_task.call_args.kwargs["project_id"])
            == "7"
        )

    def test_enqueue_manual_task_adopts_terminal_job_after_stale_claim(self, monkeypatch):
        from core.application.idempotency.models import IdempotencyClaim

        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=999)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested or "wf-1",
                "explicit_request" if requested else "project_default",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            idempotency_result_retention_days=45,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["api"],
                            "input_mode": "passthrough",
                            "output_mode": "none",
                        }
                    }
                }
            )
        )

        class _Repository:
            def __init__(self, _conn):
                pass

            def find_job_by_request_identity(self, **_kwargs):
                return {
                    "id": "job-existing",
                    "task_id": "source-77",
                    "status": "cancelled",
                    "meta": {
                        "idempotency": {
                            "request_fingerprint": claim.request_fingerprint,
                            "accepted_response": {
                                "project_id": "7",
                                "task_id": "source-77",
                                "status": "pending",
                                "queue": "platform.project.7",
                                "file_count": 0,
                                "project_created": False,
                                "deduplicated": False,
                            },
                        },
                        "queue_delivery": {
                            "state": "delivery_succeeded",
                            "delivery_succeeded": True,
                        },
                    },
                }

        claim = IdempotencyClaim(
            outcome="claimed",
            scope_key="request:v1:scope",
            key_digest="sha256:key",
            request_fingerprint="placeholder",
            request_id="request-2",
            recovered=True,
        )

        accepted_calls: list[dict[str, Any]] = []

        class _IdempotencyService:
            def __init__(self, _repository, **_kwargs):
                pass

            def claim(self, *, request_fingerprint, **_kwargs):
                object.__setattr__(claim, "request_fingerprint", request_fingerprint)
                return claim

            def renew(self, **_kwargs):
                return True

            def accept(self, **_kwargs):
                accepted_calls.append(_kwargs)
                return

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyRepository",
                _Repository,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyService",
                _IdempotencyService,
            ),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(MagicMock()),
            ),
        ):
            result = service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="source-77",
                    task_summary="Process invoice",
                    workflow_id="wf-1",
                    inputs={"invoice_id": "INV-77"},
                    idempotency_key="launch-77",
                )
            )

        assert result.job_id == "job-existing"
        assert result.deduplicated is True
        enqueue_task.assert_not_called()
        assert accepted_calls[0]["job_id"] == "job-existing"
        assert accepted_calls[0]["result_expires_at"] >= datetime.now(UTC) + timedelta(days=44)

    def test_enqueue_manual_task_redelivers_same_pending_job_after_stale_claim(self, monkeypatch):
        """An incomplete pre-execution dispatch may only re-publish the durable job."""
        from core.application.idempotency.models import IdempotencyClaim

        class _OutboxRepositoryDouble:
            def __init__(self, logger):
                self.logger = logger

            def get(self, *, job_id):
                return self.logger.get_job_dispatch_outbox(job_id)

            def mark(self, *, job_id, status, last_error=None):
                return self.logger.mark_job_dispatch_outbox(
                    job_id, status=status, last_error=last_error
                )

            def settle_after_execution_advanced(self, *, job_id):
                return self.logger.settle_job_dispatch_outbox_after_execution_advanced(job_id)

        monkeypatch.setattr(
            "core.application.jobs.task_dispatch_common._dispatch_outbox_repository",
            lambda logger: _OutboxRepositoryDouble(logger),
        )

        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        bl.settle_job_dispatch_outbox_after_execution_advanced.return_value = False
        enqueue_task = MagicMock(return_value=999)
        redeliver = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested or "wf-1",
                "explicit_request" if requested else "project_default",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            enqueue_task_in_queue=redeliver,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["api"],
                            "input_mode": "passthrough",
                            "output_mode": "none",
                        }
                    }
                }
            )
        )

        durable_job = {
            "id": "job-existing",
            "project_id": "7",
            "task_id": "source-77",
            "task_summary": "Process invoice",
            "task_source": "manual",
            "thread_id": "thread-existing",
            "mode": "",
            "status": "pending",
            "worker_id": None,
            "meta": {
                "idempotency": {
                    "scope_key": "task_launch:7",
                    "key_digest": "digest-launch-77",
                    "request_fingerprint": "placeholder",
                    "accepted_response": {
                        "project_id": "7",
                        "task_id": "source-77",
                        "status": "pending",
                        "queue": "platform.project.7",
                        "file_count": 0,
                        "project_created": False,
                        "deduplicated": False,
                    },
                },
                "queue_delivery": {
                    "state": "retrying_delivery",
                    "dispatch_state": "not_started",
                    "delivery_succeeded": False,
                },
                "project_config_snapshot": {
                    "project_name": "snapshot-demo",
                    "project_policy": {"retry_max_attempts": 1},
                },
                "task_envelope": {
                    "title": "Process invoice",
                    "inputs": {"invoice_id": "INV-77"},
                    "payload_complete": True,
                    "files": [
                        {
                            "artifact_id": "artifact-invoice-77",
                            "filename": "invoice.txt",
                            "content_type": "text/plain",
                            "size_bytes": 7,
                            "sha256": "a" * 64,
                        }
                    ],
                },
            },
        }
        bl.get_job.return_value = durable_job
        from core.application.jobs.task_enqueue_runtime import _encrypt_dispatch_envelope

        bl.get_job_dispatch_outbox.return_value = {
            "status": "pending",
            "encrypted_envelope": _encrypt_dispatch_envelope(
                {
                    "project_name": "snapshot-demo",
                    "project_config": {
                        "project_name": "snapshot-demo",
                        "secret_token": "original-secret",
                    },
                    "extra_state": {
                        "inputs": {"invoice_id": "INV-77"},
                        "task_envelope": durable_job["meta"]["task_envelope"],
                    },
                }
            ),
        }

        class _Repository:
            def __init__(self, _conn):
                pass

            def find_job_by_request_identity(self, **_kwargs):
                durable_job["meta"]["idempotency"]["request_fingerprint"] = (
                    claim.request_fingerprint
                )
                return durable_job

        claim = IdempotencyClaim(
            outcome="claimed",
            scope_key="request:v1:scope",
            key_digest="sha256:key",
            request_fingerprint="placeholder",
            request_id="request-2",
            recovered=True,
        )
        accepted_calls: list[dict[str, Any]] = []
        failed_calls: list[dict[str, Any]] = []

        class _IdempotencyService:
            def __init__(self, _repository, **_kwargs):
                pass

            def claim(self, *, request_fingerprint, **_kwargs):
                object.__setattr__(claim, "request_fingerprint", request_fingerprint)
                return claim

            def renew(self, **_kwargs):
                return True

            def accept(self, **_kwargs):
                accepted_calls.append(_kwargs)
                return

            def fail(self, **_kwargs):
                failed_calls.append(_kwargs)
                return

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyRepository",
                _Repository,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyService",
                _IdempotencyService,
            ),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(MagicMock()),
            ),
        ):
            result = service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="source-77",
                    task_summary="Process invoice",
                    workflow_id="wf-1",
                    inputs={"invoice_id": "INV-77"},
                    idempotency_key="launch-77",
                )
            )

        assert result.job_id == "job-existing"
        assert result.deduplicated is True
        enqueue_task.assert_not_called()
        redeliver.assert_called_once()
        assert redeliver.call_args.kwargs["job_id"] == "job-existing"
        assert redeliver.call_args.kwargs["thread_id"] == "thread-existing"
        assert "project_root" not in redeliver.call_args.kwargs
        assert redeliver.call_args.kwargs["project_config"]["project_name"] == "snapshot-demo"
        assert redeliver.call_args.kwargs["project_config"]["secret_token"] == "original-secret"
        assert "project_policy" not in redeliver.call_args.kwargs["project_config"]
        assert redeliver.call_args.kwargs["extra_state"]["task_envelope"]["files"] == [
            {
                "artifact_id": "artifact-invoice-77",
                "filename": "invoice.txt",
                "content_type": "text/plain",
                "size_bytes": 7,
                "sha256": "a" * 64,
            }
        ]
        assert accepted_calls[0]["job_id"] == "job-existing"

        durable_job["meta"]["task_envelope"]["payload_complete"] = False
        bl.get_job.return_value = durable_job
        redeliver.reset_mock()
        recovery_transaction_events: list[str] = []

        @contextlib.contextmanager
        def recovery_transaction():
            try:
                yield MagicMock()
            except Exception:
                recovery_transaction_events.append("rollback")
                raise
            else:
                recovery_transaction_events.append("commit")

        _raises_input_13999_1 = ProjectTaskEnqueueInput(
            project_id="7",
            task_id="source-77",
            task_summary="Process invoice",
            workflow_id="wf-1",
            inputs={"invoice_id": "INV-77"},
            idempotency_key="launch-77",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyRepository",
                _Repository,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyService",
                _IdempotencyService,
            ),
            patch(
                "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure"
            ) as complete_failure,
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=recovery_transaction(),
            ),
            pytest.raises(ServiceConflictError) as incomplete_error,
        ):
            service.enqueue_manual_task(_raises_input_13999_1)

        assert incomplete_error.value.payload["code"] == "idempotency_payload_incomplete_retryable"
        assert recovery_transaction_events == ["commit"]
        assert failed_calls[-1]["error_code"] == "idempotency_payload_incomplete"
        complete_failure.assert_called_once()
        assert complete_failure.call_args.kwargs["require_pending_unowned"] is True
        assert complete_failure.call_args.kwargs["meta_patch"]["queue_delivery"]["state"] == (
            "dead_lettered"
        )
        abandoned_meta = bl.update_job.call_args.kwargs["meta"]
        assert "idempotency" not in abandoned_meta
        assert abandoned_meta["abandoned_idempotency"]["reason"] == (
            "payload_incomplete_before_dispatch"
        )
        redeliver.assert_not_called()

        # A pre-publish persistence exception may already have marked this same
        # incomplete job failed. Recovery must retire it directly instead of
        # retrying a pending-only SQL transition forever.
        durable_job["status"] = "failed"
        bl.get_job.return_value = durable_job
        _raises_input_14052_1 = ProjectTaskEnqueueInput(
            project_id="7",
            task_id="source-77",
            task_summary="Process invoice",
            workflow_id="wf-1",
            inputs={"invoice_id": "INV-77"},
            idempotency_key="launch-77",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch("core.application.idempotency.RequestIdempotencyRepository", _Repository),
            patch("core.application.idempotency.RequestIdempotencyService", _IdempotencyService),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(MagicMock()),
            ),
            pytest.raises(ServiceConflictError) as failed_incomplete_error,
        ):
            service.enqueue_manual_task(_raises_input_14052_1)
        assert failed_incomplete_error.value.payload["code"] == (
            "idempotency_payload_incomplete_retryable"
        )

        durable_job["meta"]["task_envelope"]["payload_complete"] = True
        durable_job["status"] = "in_progress"
        durable_job["worker_id"] = "worker-77"
        durable_job["meta"]["queue_delivery"] = {
            "state": "queued",
            "dispatch_state": "attempted",
            "delivery_succeeded": False,
        }
        redeliver.reset_mock()
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyRepository",
                _Repository,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyService",
                _IdempotencyService,
            ),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(MagicMock()),
            ),
        ):
            claimed_worker_result = service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="source-77",
                    task_summary="Process invoice",
                    workflow_id="wf-1",
                    inputs={"invoice_id": "INV-77"},
                    idempotency_key="launch-77",
                )
            )

        assert claimed_worker_result.job_id == "job-existing"
        assert claimed_worker_result.deduplicated is True
        redeliver.assert_not_called()

        durable_job["status"] = "pending"
        durable_job["worker_id"] = None
        durable_job["meta"]["queue_delivery"] = {
            "state": "retrying_delivery",
            "dispatch_state": "not_started",
            "delivery_succeeded": False,
        }
        bl.get_job.return_value = durable_job
        redeliver.reset_mock(side_effect=True)
        redeliver.side_effect = RuntimeError("broker nack")

        _raises_input_14132_1 = ProjectTaskEnqueueInput(
            project_id="7",
            task_id="source-77",
            task_summary="Process invoice",
            workflow_id="wf-1",
            inputs={"invoice_id": "INV-77"},
            idempotency_key="launch-77",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyRepository",
                _Repository,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyService",
                _IdempotencyService,
            ),
            patch(
                "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure"
            ),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(MagicMock()),
            ),
            pytest.raises(ServiceUnavailableError, match="RabbitMQ task queue is unavailable"),
        ):
            service.enqueue_manual_task(_raises_input_14132_1)

        assert failed_calls[-1]["error_code"] == "queue_publish_failed"
        # An unclassified broker failure is ambiguous after the attempted
        # fence and must fail closed instead of becoming re-publishable.
        assert bl.mark_job_dispatch_outbox.call_args.kwargs["status"] == "failed"
        assert bl.mark_job_dispatch_outbox.call_args.kwargs["last_error"] == "broker nack"

    def test_enqueue_manual_task_adopts_publisher_confirmed_job_after_stale_claim(self):
        """A durable broker confirmation is enough to replay the same job without publishing it."""
        from core.application.idempotency.models import IdempotencyClaim

        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock()
        redeliver = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested or "wf-1",
                "explicit_request" if requested else "project_default",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            enqueue_task_in_queue=redeliver,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["api"],
                            "input_mode": "passthrough",
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        claim = IdempotencyClaim(
            outcome="claimed",
            scope_key="request:v1:scope",
            key_digest="sha256:key",
            request_fingerprint="placeholder",
            request_id="request-2",
            recovered=True,
        )
        accepted_calls: list[dict[str, Any]] = []

        class _Repository:
            def __init__(self, _conn):
                pass

            def find_job_by_request_identity(self, **_kwargs):
                return {
                    "id": "job-confirmed",
                    "project_id": "7",
                    "task_id": "source-77",
                    "task_summary": "Process invoice",
                    "task_source": "manual",
                    "thread_id": "thread-confirmed",
                    "mode": "",
                    "status": "pending",
                    "worker_id": None,
                    "meta": {
                        "idempotency": {
                            "request_fingerprint": claim.request_fingerprint,
                            "accepted_response": {
                                "project_id": "7",
                                "task_id": "source-77",
                                "status": "pending",
                                "queue": "platform.project.7",
                                "file_count": 0,
                                "project_created": False,
                                "deduplicated": False,
                            },
                        },
                        "queue_delivery": {
                            "state": "queued",
                            "dispatch_state": "publisher_confirmed",
                            "delivery_succeeded": False,
                        },
                    },
                }

        class _IdempotencyService:
            def __init__(self, _repository, **_kwargs):
                pass

            def claim(self, *, request_fingerprint, **_kwargs):
                object.__setattr__(claim, "request_fingerprint", request_fingerprint)
                return claim

            def renew(self, **_kwargs):
                return True

            def accept(self, **kwargs):
                accepted_calls.append(kwargs)

        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch("core.application.idempotency.RequestIdempotencyRepository", _Repository),
            patch("core.application.idempotency.RequestIdempotencyService", _IdempotencyService),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(MagicMock()),
            ),
        ):
            result = service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="source-77",
                    task_summary="Process invoice",
                    workflow_id="wf-1",
                    inputs={"invoice_id": "INV-77"},
                    idempotency_key="launch-77",
                )
            )

        assert result.job_id == "job-confirmed"
        assert result.deduplicated is True
        enqueue_task.assert_not_called()
        redeliver.assert_not_called()
        assert accepted_calls[0]["job_id"] == "job-confirmed"

    def test_enqueue_manual_task_rebuilds_public_task_envelope(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=91)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        rich_envelope = {
            "entry_path": "mcp",
            "source_system": "mcp",
            "title": "Strict runtime repair",
            "description": "Keep exact task contract.",
            "verification_commands": ["python -m pytest tests/unit/test_hub_node.py -q"],
            "files": [{"path": "core/application/orchestration/hub_node.py"}],
            "inputs": {
                "requirements": ["Preserve caller envelope"],
                "verification_commands": ["python -m pytest tests/unit/test_hub_node.py -q"],
            },
            "metadata": {"custom": "kept"},
            "custom_generic_field": {"any": "value"},
        }
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["mcp"],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-77",
                    task_summary="Strict runtime repair",
                    workflow_id="wf-1",
                    task_source="mcp_workflow",
                    entry_path="mcp",
                    extra_state={
                        "task_envelope": rich_envelope,
                        "invoke_child_auto_approve": True,
                    },
                    job_meta={
                        "invoke_child_auto_approve": True,
                        "project_config_snapshot": {"project_name": "attacker"},
                    },
                )
            )

        extra_state = enqueue_task.call_args.kwargs["extra_state"]
        assert "invoke_child_auto_approve" not in extra_state
        envelope = extra_state["task_envelope"]
        assert "verification_commands" not in envelope
        assert envelope["files"] == []
        assert envelope["inputs"]["requirements"] == ["Preserve caller envelope"]
        assert envelope["metadata"] == {"task_source": "mcp_workflow"}
        assert "custom_generic_field" not in envelope
        meta = enqueue_task.call_args.kwargs["meta"]
        assert "invoke_child_auto_approve" not in meta
        assert "project_config_snapshot" not in meta
        assert enqueue_task.call_args.kwargs["meta"]["input_contract"] == "task_envelope"

        enqueue_task.reset_mock()
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-78",
                    task_summary="Keep trusted context",
                    workflow_id="wf-1",
                    task_source="mcp_workflow",
                    entry_path="mcp",
                    extra_state={
                        "client_context": {"session": "mcp-1"},
                        "prepared_brief_markdown": "# Brief",
                    },
                    job_meta={"client_context": {"session": "mcp-1"}},
                ),
                trusted_internal=True,
            )

        trusted_extra_state = enqueue_task.call_args.kwargs["extra_state"]
        trusted_meta = enqueue_task.call_args.kwargs["meta"]
        assert trusted_extra_state["client_context"] == {"session": "mcp-1"}
        assert trusted_extra_state["prepared_brief_markdown"] == "# Brief"
        assert "client_context" not in trusted_meta

        enqueue_task.reset_mock()
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-79",
                    task_summary="Keep bounded caller context",
                    workflow_id="wf-1",
                    task_source="mcp_workflow",
                    entry_path="mcp",
                    client_context={"session": "mcp-2"},
                )
            )

        public_extra_state = enqueue_task.call_args.kwargs["extra_state"]
        public_meta = enqueue_task.call_args.kwargs["meta"]
        assert public_extra_state["client_context"] == {"session": "mcp-2"}
        assert "client_context" not in public_meta

    def test_enqueue_manual_task_uses_envelope_inputs_for_callable_contract(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=91)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        rich_envelope = {
            "title": "Strict runtime repair",
            "description": "Keep exact task contract.",
            "inputs": {
                "request": "Fix the runtime bug",
                "requirements": ["Preserve rich task envelope"],
            },
        }
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["mcp"],
                            "input_mode": "fields",
                            "strict_inputs": True,
                            "expected_inputs": [
                                {"name": "request", "type": "paragraph", "required": True}
                            ],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-77",
                    task_summary="Strict runtime repair",
                    workflow_id="wf-1",
                    task_source="mcp_workflow",
                    entry_path="mcp",
                    extra_state={"task_envelope": rich_envelope},
                )
            )

        extra_state = enqueue_task.call_args.kwargs["extra_state"]
        assert extra_state["inputs"]["request"] == "Fix the runtime bug"
        assert extra_state["inputs"]["request"] == "Fix the runtime bug"
        assert "requirements" not in extra_state["inputs"]
        assert "requirements" not in extra_state["inputs"]
        assert "requirements" not in extra_state["task_envelope"]["inputs"]
        assert extra_state["task_envelope"]["metadata"]["original_task_envelope_inputs"][
            "requirements"
        ] == ["Preserve rich task envelope"]

    def test_enqueue_manual_task_preserves_extra_envelope_inputs_for_non_strict_fields(
        self,
    ):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=91)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        rich_envelope = {
            "title": "Flexible runtime repair",
            "description": "Keep non-strict context.",
            "inputs": {
                "request": "Fix the runtime bug",
                "extra_context": {"auditor": "keep this prompt-facing"},
            },
        }
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["mcp"],
                            "input_mode": "fields",
                            "strict_inputs": False,
                            "expected_inputs": [
                                {"name": "request", "type": "paragraph", "required": True}
                            ],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-77",
                    task_summary="Flexible runtime repair",
                    workflow_id="wf-1",
                    task_source="mcp_workflow",
                    entry_path="mcp",
                    extra_state={"task_envelope": rich_envelope},
                )
            )

        extra_state = enqueue_task.call_args.kwargs["extra_state"]
        assert extra_state["inputs"]["request"] == "Fix the runtime bug"
        assert extra_state["inputs"]["extra_context"] == {"auditor": "keep this prompt-facing"}
        assert extra_state["task_envelope"]["inputs"]["extra_context"] == {
            "auditor": "keep this prompt-facing"
        }
        assert "original_task_envelope_inputs" not in extra_state["task_envelope"].get(
            "metadata", {}
        )

    def test_enqueue_manual_task_validates_json_schema_envelope_inputs(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=91)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["mcp"],
                            "input_mode": "json_schema",
                            "strict_inputs": True,
                            "input_schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"request": {"type": "string"}},
                                "required": ["request"],
                            },
                            "output_mode": "none",
                        }
                    }
                }
            )
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="7",
                    task_id="task-77",
                    task_summary="Strict runtime repair",
                    workflow_id="wf-1",
                    task_source="mcp_workflow",
                    entry_path="mcp",
                    extra_state={
                        "task_envelope": {
                            "title": "Strict runtime repair",
                            "inputs": {"request": "Fix the runtime bug"},
                        }
                    },
                )
            )
            _raises_input_14695_1 = ProjectTaskEnqueueInput(
                project_id="7",
                task_id="task-78",
                task_summary="Strict runtime repair",
                workflow_id="wf-1",
                task_source="mcp_workflow",
                entry_path="mcp",
                extra_state={
                    "task_envelope": {
                        "title": "Strict runtime repair",
                        "inputs": {
                            "request": "Fix the runtime bug",
                            "undeclared": "must fail schema validation",
                        },
                    }
                },
            )
            with pytest.raises(ServiceValidationError):
                service.enqueue_manual_task(_raises_input_14695_1)

        extra_state = enqueue_task.call_args.kwargs["extra_state"]
        assert extra_state["inputs"] == {"request": "Fix the runtime bug"}
        assert extra_state["task_envelope"]["inputs"] == {"request": "Fix the runtime bug"}
        assert enqueue_task.call_count == 1

    def test_enqueue_manual_task_saves_attachments_and_merges_meta(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        bl.get_job.return_value = {
            "workflow_definition_snapshot_id": "snapshot-upload",
            "meta": {
                "existing": "yes",
                "workflow_definition_ref": {"revision": 4, "digest": "a" * 64},
                "attachment_persistence_artifacts": [
                    {"artifact_id": "art-old", "object_key": "old/blob"}
                ],
            },
        }
        plane = SimpleNamespace(
            create_artifact=MagicMock(
                side_effect=lambda **kwargs: (
                    SimpleNamespace(artifact_id=kwargs["artifact_id"]),
                    {
                        "filename": "notes.txt",
                        "content_type": "text/plain",
                        "size_bytes": 4,
                        "sha256": "def456",
                        "object_key": "manual/notes.txt",
                        "retention_class": "run_window",
                    },
                )
            )
        )

        prepared_patch = {}

        def _enqueue_task(**kwargs):
            prepared_patch.update(kwargs["pre_publish_fn"](92, bl))
            return 92

        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=_enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
            artifact_plane=plane,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": False,
                            "trigger_channels": [],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            payload = service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="8",
                    task_id="task-88",
                    task_summary="Upload docs",
                    workflow_id="wf-attachments",
                    files=[
                        ProjectTaskFileInput(
                            filename="notes.txt",
                            mime_type="text/plain",
                            content=b"data",
                        )
                    ],
                )
            ).dump()

        assert payload["file_count"] == 1
        assert plane.create_artifact.call_count == 1
        assert plane.create_artifact.call_args.kwargs["filename"] == "notes.txt"
        assert plane.create_artifact.call_args.kwargs["owner_scope"]["job_id"] == "92"
        assert plane.create_artifact.call_args.kwargs["run_id"] == "job:92"
        assert prepared_patch["files"][0]["filename"] == "notes.txt"
        assert (
            prepared_patch["files"][0]["artifact_id"]
            == (plane.create_artifact.call_args.kwargs["artifact_id"])
        )
        assert prepared_patch["existing"] == "yes"
        assert prepared_patch["workflow_definition_ref"] == {"revision": 4, "digest": "a" * 64}
        assert prepared_patch["attachment_persistence_artifacts"][0] == {
            "artifact_id": "art-old",
            "object_key": "old/blob",
        }

    def test_enqueue_manual_task_rolls_back_staged_attachments_when_publish_fails(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        bl.get_job.return_value = {"meta": {}}
        plane = SimpleNamespace(
            create_artifact=MagicMock(
                side_effect=lambda **kwargs: (
                    SimpleNamespace(artifact_id=kwargs["artifact_id"]),
                    {
                        "filename": "notes.txt",
                        "content_type": "text/plain",
                        "size_bytes": 4,
                        "sha256": "def456",
                        "object_key": "manual/notes.txt",
                        "retention_class": "run_window",
                    },
                )
            ),
            delete_artifact=MagicMock(),
        )

        def _enqueue_task(**kwargs):
            kwargs["pre_publish_fn"](92, bl)
            raise RuntimeError("queue publish failed")

        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=_enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
            artifact_plane=plane,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {"callable": {"enabled": False, "trigger_channels": []}}
                }
            )
        )

        _raises_input_14859_1 = ProjectTaskEnqueueInput(
            project_id="8",
            task_id="task-rollback-1",
            task_summary="Upload docs",
            workflow_id="wf-attachments",
            files=[
                ProjectTaskFileInput(filename="notes.txt", mime_type="text/plain", content=b"data")
            ],
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.jobs.terminal_persistence.TerminalPersistenceService",
            ),
            pytest.raises(ServiceError, match="Failed to enqueue task"),
        ):
            service.enqueue_manual_task(_raises_input_14859_1)

        plane.delete_artifact.assert_called_once_with(
            plane.create_artifact.call_args.kwargs["artifact_id"]
        )

    def test_enqueue_manual_task_reports_an_unrunnable_definition_as_a_conflict(self):
        from core.application.jobs.workflow_definition_snapshot_repository import (
            WorkflowDefinitionPinError,
        )

        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {"project_name": "demo"}
        bl.get_job.return_value = {"meta": {}}
        issue = {
            "code": "workflow.extension_unavailable",
            "message": "The extension that ships this workflow is not active",
            "path": "workflow_id",
        }

        def _enqueue_task(**_kwargs):
            raise WorkflowDefinitionPinError("Workflow 'wf' cannot run", [issue])

        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=_enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {"callable": {"enabled": False, "trigger_channels": []}}
                }
            )
        )
        enqueue_input = ProjectTaskEnqueueInput(
            project_id="8",
            task_id="task-unrunnable-1",
            task_summary="Run",
            workflow_id="wf",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch("core.application.jobs.terminal_persistence.TerminalPersistenceService"),
            pytest.raises(ServiceError) as raised,
        ):
            service.enqueue_manual_task(enqueue_input)

        assert raised.value.status_code == 409
        assert raised.value.payload["code"] == "workflow_not_runnable"
        assert raised.value.payload["issues"] == [issue]

    def test_enqueue_manual_task_rolls_back_artifacts_for_failed_dispatch(self):
        from core.application.idempotency.models import IdempotencyClaim
        from core.application.jobs.task_enqueue_runtime import QueueTransportUnavailableError

        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        bl.get_job.return_value = {"project_id": "8", "status": "pending", "meta": {}}
        plane = SimpleNamespace(
            create_artifact=MagicMock(
                side_effect=lambda **kwargs: (
                    SimpleNamespace(artifact_id=kwargs["artifact_id"]),
                    {
                        "filename": "notes.txt",
                        "content_type": "text/plain",
                        "size_bytes": 4,
                        "sha256": "def456",
                        "object_key": "manual/notes.txt",
                        "retention_class": "run_window",
                    },
                )
            ),
            delete_artifact=MagicMock(),
        )
        claim = IdempotencyClaim(
            outcome="claimed",
            scope_key="request:v1:scope",
            key_digest="sha256:key",
            request_fingerprint="placeholder",
            request_id="request-1",
        )
        failed_claims: list[dict[str, Any]] = []

        class _Repository:
            def __init__(self, _conn):
                pass

        class _IdempotencyService:
            def __init__(self, _repository, **_kwargs):
                pass

            def claim(self, *, request_fingerprint, **_kwargs):
                object.__setattr__(claim, "request_fingerprint", request_fingerprint)
                return claim

            def renew(self, **_kwargs):
                return True

            def fail(self, **kwargs):
                failed_claims.append(kwargs)
                raise RuntimeError("idempotency claim is no longer owned")

        def _enqueue_task(**kwargs):
            kwargs["pre_publish_fn"]("job-recover-1", bl)
            bl.get_job.return_value = {
                "project_id": "8",
                "status": "triage_needed",
                "meta": {
                    "idempotency": {
                        "scope_key": claim.scope_key,
                        "key_digest": claim.key_digest,
                        "request_fingerprint": claim.request_fingerprint,
                    },
                    "queue_delivery": {
                        "state": "dead_lettered",
                        "dispatch_state": "attempted",
                        "last_failure_code": "queue_publish_ambiguous",
                        "delivery_succeeded": False,
                    },
                },
            }
            raise QueueTransportUnavailableError("RabbitMQ task queue is unavailable")

        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=_enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
            artifact_plane=plane,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {"callable": {"enabled": False, "trigger_channels": []}}
                }
            )
        )

        _raises_input_14986_1 = ProjectTaskEnqueueInput(
            project_id="8",
            task_id="task-recover-1",
            task_summary="Upload docs",
            workflow_id="wf-attachments",
            idempotency_key="recover-1",
            files=[
                ProjectTaskFileInput(filename="notes.txt", mime_type="text/plain", content=b"data")
            ],
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyRepository",
                _Repository,
            ),
            patch(
                "core.application.idempotency.RequestIdempotencyService",
                _IdempotencyService,
            ),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(MagicMock()),
            ),
            pytest.raises(ServiceUnavailableError, match="RabbitMQ task queue is unavailable"),
        ):
            service.enqueue_manual_task(_raises_input_14986_1)

        plane.delete_artifact.assert_called_once_with(
            plane.create_artifact.call_args.kwargs["artifact_id"]
        )
        assert failed_claims[0]["error_code"] == "task_enqueue_failed"

    @pytest.mark.parametrize("task_source", ["manual", "mcp_workflow"])
    def test_enqueue_manual_task_backfills_invocation_file_visibility(self, task_source):
        # The invocation snapshot is built pre-persist with empty files; after
        # attachments persist, job detail (invocation.request_context_summary/files/
        # task_envelope) must reflect the uploaded file (post-dev-final RF-1).
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        bl.get_job.return_value = {
            "workflow_definition_snapshot_id": "snapshot-upload",
            "meta": {
                "workflow_definition_ref": {"revision": 4, "digest": "a" * 64},
                "attachment_persistence_artifacts": [
                    {"artifact_id": "art-old", "object_key": "old/blob"}
                ],
                "invocation": {
                    "files": [],
                    "request_context_summary": {"file_count": 0, "file_names": []},
                    "task_envelope": {"files": []},
                },
                "task_envelope": {"files": []},
            },
        }
        plane = SimpleNamespace(
            create_artifact=MagicMock(
                side_effect=lambda **kwargs: (
                    SimpleNamespace(artifact_id=kwargs["artifact_id"]),
                    {
                        "filename": "notes.txt",
                        "content_type": "text/plain",
                        "size_bytes": 4,
                        "sha256": hashlib.sha256(b"data").hexdigest(),
                        "object_key": "manual/notes.txt",
                        "retention_class": "run_window",
                    },
                )
            )
        )

        prepared_patch = {}

        def _enqueue_task(**kwargs):
            prepared_patch.update(kwargs["pre_publish_fn"](92, bl))
            return 92

        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=_enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
            artifact_plane=plane,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": task_source == "mcp_workflow",
                            "trigger_channels": ["mcp"],
                            "input_mode": "fields",
                            "expected_inputs": [
                                {"name": "source_file", "type": "file", "required": True}
                            ],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="8",
                    task_id="task-88",
                    task_summary="Upload docs",
                    workflow_id="wf-attachments",
                    task_source=task_source,
                    entry_path="mcp" if task_source == "mcp_workflow" else "manual",
                    files=[
                        ProjectTaskFileInput(
                            filename="notes.txt",
                            mime_type="text/plain",
                            content=b"data",
                            input_name="source_file" if task_source == "mcp_workflow" else "",
                        )
                    ],
                )
            )

        merged_meta = prepared_patch
        invocation = merged_meta["invocation"]
        assert invocation["request_context_summary"]["file_count"] == 1
        assert invocation["request_context_summary"]["file_names"] == ["notes.txt"]
        assert invocation["files"]
        assert invocation["files"][0]["filename"] == "notes.txt"
        assert invocation["task_envelope"]["files"][0]["filename"] == "notes.txt"
        assert merged_meta["task_envelope"]["files"][0]["filename"] == "notes.txt"

        assert merged_meta["workflow_definition_ref"] == {"revision": 4, "digest": "a" * 64}
        assert merged_meta["attachment_persistence_artifacts"][0] == {
            "artifact_id": "art-old",
            "object_key": "old/blob",
        }
        if task_source == "mcp_workflow":
            stored = merged_meta["files"][0]
            assert merged_meta["inputs"]["source_file"] == stored
            assert merged_meta["task_envelope"]["inputs"]["source_file"] == stored
            assert stored["artifact_id"] == plane.create_artifact.call_args.kwargs["artifact_id"]

    def test_enqueue_manual_task_forwards_project_run_config(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": "general-8",
            "name": "mail-operations",
        }
        bl.get_project_run_config.return_value = {
            "project_id": "general-8",
            "project_name": "mail-operations",
            "project_workflows": [{"workflow_id": "wf-email", "enabled": True}],
        }
        enqueue_task = MagicMock(return_value=93)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": False,
                            "trigger_channels": [],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            result = service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="general-8",
                    task_id="task-general-email",
                    task_summary="Send the digest",
                    workflow_id="wf-email",
                )
            )

        assert result.job_id == "93"
        assert "project_root" not in enqueue_task.call_args.kwargs
        assert enqueue_task.call_args.kwargs["project_config"]["project_name"] == "mail-operations"

    def test_enqueue_manual_task_uses_strict_pre_publish_for_attachments(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        plane = SimpleNamespace(
            create_artifact=MagicMock(
                side_effect=lambda **kwargs: (
                    SimpleNamespace(artifact_id=kwargs["artifact_id"]),
                    {
                        "filename": "notes.txt",
                        "content_type": "text/plain",
                        "size_bytes": 4,
                        "sha256": "fff111",
                        "object_key": "manual/notes-2.txt",
                        "retention_class": "run_window",
                    },
                )
            )
        )
        enqueue_task = MagicMock(return_value=92)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
            artifact_plane=plane,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": False,
                            "trigger_channels": [],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.enqueue_manual_task(
                ProjectTaskEnqueueInput(
                    project_id="8",
                    task_id="task-89",
                    task_summary="Upload docs",
                    workflow_id="wf-attachments",
                    files=[
                        ProjectTaskFileInput(
                            filename="notes.txt",
                            mime_type="text/plain",
                            content=b"data",
                        )
                    ],
                )
            )

        assert callable(enqueue_task.call_args.kwargs["pre_publish_fn"])
        assert enqueue_task.call_args.kwargs["fail_on_pre_publish_error"] is True

    def test_enqueue_manual_task_maps_queue_transport_outage_to_service_unavailable(self):
        from core.application.jobs.task_enqueue_runtime import QueueTransportUnavailableError

        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        enqueue_task = MagicMock(
            side_effect=QueueTransportUnavailableError("RabbitMQ task queue is unavailable")
        )
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": False,
                            "trigger_channels": [],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        _raises_input_15412_1 = ProjectTaskEnqueueInput(
            project_id="8",
            task_id="task-queue-down",
            task_summary="Queue outage",
            workflow_id="wf-attachments",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            pytest.raises(ServiceUnavailableError, match="RabbitMQ task queue is unavailable"),
        ):
            service.enqueue_manual_task(_raises_input_15412_1)

    def test_enqueue_manual_task_names_a_misconfigured_artifact_backend(self, monkeypatch):
        """A storage backend that cannot work must not surface as a bare internal_error."""

        class NoCredentialsError(Exception):
            """Stands in for botocore.exceptions.NoCredentialsError."""

        monkeypatch.setenv("ARTIFACT_STORAGE_BACKEND", "s3")
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        enqueue_task = MagicMock(side_effect=NoCredentialsError("Unable to locate credentials"))
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "settings": {
                        "callable": {
                            "enabled": False,
                            "trigger_channels": [],
                            "output_mode": "none",
                        }
                    }
                }
            )
        )
        request = ProjectTaskEnqueueInput(
            project_id="8",
            task_id="task-storage-misconfigured",
            task_summary="Storage misconfigured",
            workflow_id="wf-attachments",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            pytest.raises(ServiceUnavailableError) as raised,
        ):
            service.enqueue_manual_task(request)

        message = str(raised.value)
        assert "ARTIFACT_STORAGE_BACKEND=s3" in message
        assert "--file-storage local_fs" in message
        assert "config apply" in message
        assert raised.value.status_code == 503
        assert "Failed to enqueue task" not in message

    def test_enqueue_manual_task_preserves_idempotency_recovery_conflict(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 8, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
        }
        recovery_conflict = ServiceConflictError(
            "The durable idempotency reservation has no safely recoverable dispatch",
            payload={"code": "idempotency_recovery_not_safe"},
        )
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=MagicMock(side_effect=recovery_conflict),
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {"settings": {"callable": {"enabled": False}}}
            )
        )

        _raises_input_15457_1 = ProjectTaskEnqueueInput(
            project_id="8",
            task_id="task-idempotency-conflict",
            task_summary="Recovery conflict",
            workflow_id="wf-idempotency",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            pytest.raises(ServiceConflictError) as exc_info,
        ):
            service.enqueue_manual_task(_raises_input_15457_1)

        assert exc_info.value.payload["code"] == "idempotency_recovery_not_safe"

    def test_enqueue_manual_task_rejects_missing_project_even_when_implicit_allowed(self):
        bl = MagicMock()
        enqueue_task = MagicMock(return_value=44)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: ("", "auto"),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )

        _raises_input_15488_1 = ProjectTaskEnqueueInput(
            task_id="task-retired",
            task_summary="Retired submit",
            project_name="Retired Project",
        )
        with pytest.raises(ServiceValidationError, match="project_id is required"):
            service.enqueue_manual_task(_raises_input_15488_1, allow_implicit_project=True)

        bl.add_project.assert_not_called()
        enqueue_task.assert_not_called()

    def test_enqueue_manual_task_rejects_unsupported_attachment_type(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 12, "name": "demo"}
        bl.get_project_run_config.return_value = {"project_name": "demo"}
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: ("", "auto"),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=MagicMock(return_value=45),
            parse_test_mode=lambda value, default: False,
            allowed_attachment_mimes={"text/plain"},
            allowed_attachment_extensions={".txt"},
        )

        _raises_input_15516_1 = ProjectTaskEnqueueInput(
            project_id="12",
            task_id="task-unsupported",
            task_summary="Retired submit",
            files=[
                ProjectTaskFileInput(
                    filename="payload.exe", mime_type="application/octet-stream", content=b"MZ"
                )
            ],
        )
        with pytest.raises(ServiceValidationError) as exc:
            service.enqueue_manual_task(_raises_input_15516_1)

        assert "unsupported file type" in str(exc.value).lower()

    def test_create_scheduled_job_returns_created_payload(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 15, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        bl.create_scheduled_job.return_value = {
            "id": 5,
            "next_run_at": datetime(2026, 3, 22, 10, 0, tzinfo=UTC),
        }
        validate_override = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                "wf-1",
                "explicit_request",
            ),
            validate_explicit_workflow_override=validate_override,
            sanitize_external_input=lambda value: str(value).strip(),
            normalize_cron_expr=lambda value: str(value).strip(),
            cron_next_run_at=lambda expr, ref, **kwargs: datetime(2026, 3, 22, 10, 0, tzinfo=UTC),
            form_flag=lambda value: str(value or "").strip().lower() in {"1", "true", "on"},
        )

        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "revision": 7,
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["schedule"],
                            "output_mode": "none",
                        }
                    },
                }
            )
        )
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            payload = service.create_scheduled_job(
                15,
                ProjectScheduledJobCreateInput(
                    workflow_id="wf-1",
                    cron_expr="0 10 * * *",
                    enabled="1",
                    created_by="alice",
                ),
            ).dump()

        assert payload["status"] == "created"
        assert payload["scheduled_job_id"] == "5"
        assert payload["enabled"] is True
        validate_override.assert_called_once()
        assert bl.create_scheduled_job.call_args.kwargs["created_by"] == "alice"
        assert bl.create_scheduled_job.call_args.kwargs["inputs"] == {}
        assert bl.create_scheduled_job.call_args.kwargs["admission_allowed"] is True
        assert bl.create_scheduled_job.call_args.kwargs["admission_reason"] == "validated"
        assert bl.create_scheduled_job.call_args.kwargs["admission_workflow_revision"] == 7

    def test_enable_scheduled_job_refreshes_admission_at_current_workflow_revision(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 16,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {"project_name": "demo"}
        bl.get_scheduled_job.return_value = {
            "id": 9,
            "project_id": "16",
            "workflow_id": "wf-1",
            "enabled": False,
            "cron_expr": "0 9 * * *",
        }
        validate_override = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
            validate_explicit_workflow_override=validate_override,
            cron_next_run_at=lambda expr, ref, **kwargs: datetime(2026, 3, 22, 9, 0, tzinfo=UTC),
            form_flag=lambda value: str(value or "").strip().lower() in {"1", "true", "on"},
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(get_workflow=lambda _workflow_id: {"revision": 11})
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            service.toggle_scheduled_job(
                16,
                9,
                ProjectScheduledJobToggleInput(enabled="1"),
            )

        validate_override.assert_called_once()
        update = bl.update_scheduled_job.call_args.kwargs
        assert update["admission_allowed"] is True
        assert update["admission_reason"] == "validated"
        assert update["admission_workflow_revision"] == 11

    def test_disable_scheduled_job_revokes_stored_admission(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 16, "name": "demo"}
        bl.get_scheduled_job.return_value = {
            "id": 9,
            "project_id": "16",
            "workflow_id": "wf-1",
            "enabled": True,
            "cron_expr": "0 9 * * *",
            "admission_workflow_revision": 7,
        }
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
            cron_next_run_at=lambda expr, ref, **kwargs: datetime(2026, 3, 22, 9, 0, tzinfo=UTC),
            form_flag=lambda value: str(value or "").strip().lower() in {"1", "true", "on"},
        )

        payload = service.toggle_scheduled_job(
            16,
            9,
            ProjectScheduledJobToggleInput(enabled="0"),
        ).dump()

        assert payload["status"] == "updated"
        assert payload["enabled"] is False
        bl.update_scheduled_job.assert_called_once()
        update = bl.update_scheduled_job.call_args.kwargs
        assert update["admission_allowed"] is False
        assert update["admission_reason"] == "disabled"

    def test_delete_scheduled_job_returns_deleted_payload(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 17, "name": "demo"}
        bl.delete_scheduled_job.return_value = True
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
        )

        payload = service.delete_scheduled_job(17, 4).dump()

        assert payload["status"] == "deleted"
        assert payload["scheduled_job_id"] == "4"

    def test_run_scheduled_job_now_returns_enqueued_job(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 18, "name": "demo"}
        bl.get_project_run_config.return_value = {"project_name": "demo"}
        bl.get_scheduled_job.return_value = {
            "id": 7,
            "project_id": "18",
            "workflow_id": "wf-1",
            "inputs": {},
        }
        bl.update_scheduled_job.return_value = {"id": 7}
        run_scheduled_job_once = MagicMock(return_value={"status": "enqueued", "job_id": 55})
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
            validate_explicit_workflow_override=MagicMock(),
            run_scheduled_job_once=run_scheduled_job_once,
        )
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "revision": 13,
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["schedule"],
                            "output_mode": "none",
                        }
                    },
                }
            )
        )

        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            payload = service.run_scheduled_job_now(18, 7).dump()

        assert payload["status"] == "enqueued"
        assert payload["job_id"] == "55"
        assert bl.update_scheduled_job.call_args.kwargs["admission_workflow_revision"] == 13
        run_scheduled_job_once.assert_called_once()

    def _scheduled_job_service(self, bl: Any) -> ProjectCommandService:
        return ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                "wf-1",
                "explicit_request",
            ),
            validate_explicit_workflow_override=MagicMock(),
            sanitize_external_input=lambda value: str(value).strip(),
            normalize_cron_expr=lambda value: str(value).strip(),
            cron_next_run_at=lambda expr, ref, **kwargs: datetime(2026, 3, 22, 10, 0, tzinfo=UTC),
            form_flag=lambda value: str(value or "").strip().lower() in {"1", "true", "on"},
        )

    def test_create_scheduled_job_rejects_workflow_outside_allowlist(self):
        # FS-DEEP-CAND-068: workflow-restricted principal must not be able to
        # persist a scheduled job for a workflow outside its allowed_workflow_ids.
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 15, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        service = self._scheduled_job_service(bl)
        _raises_input_15853_1 = ProjectScheduledJobCreateInput(
            workflow_id="wf-1",
            cron_expr="0 10 * * *",
            enabled="1",
            created_by="alice",
        )
        _raises_input_15853_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"wf-other"})
        )
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.create_scheduled_job(15, _raises_input_15853_1, scope=_raises_input_15853_2)
        bl.create_scheduled_job.assert_not_called()

    def test_create_scheduled_job_allows_workflow_in_allowlist(self):
        # FS-DEEP-CAND-068 positive path: same-scope caller stays green.
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 15, "name": "demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        bl.create_scheduled_job.return_value = {
            "id": 5,
            "next_run_at": datetime(2026, 3, 22, 10, 0, tzinfo=UTC),
        }
        service = self._scheduled_job_service(bl)
        registry = SimpleNamespace(
            db_store=SimpleNamespace(
                get_workflow=lambda _workflow_id: {
                    "revision": 9,
                    "settings": {
                        "callable": {
                            "enabled": True,
                            "trigger_channels": ["schedule"],
                            "output_mode": "none",
                        }
                    },
                }
            )
        )
        with patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ):
            payload = service.create_scheduled_job(
                15,
                ProjectScheduledJobCreateInput(
                    workflow_id="wf-1",
                    cron_expr="0 10 * * *",
                    enabled="1",
                    created_by="alice",
                ),
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    allowed_workflow_ids=frozenset({"wf-1"}),
                ),
            ).dump()
        assert payload["status"] == "created"
        assert payload["scheduled_job_id"] == "5"

    def test_create_scheduled_job_maps_workflow_registry_outage_to_retryable_503(self):
        secret = "postgresql://registry:super-secret@db.internal/workflows"
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 15,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        service = self._scheduled_job_service(bl)
        registry = SimpleNamespace(
            db_store=SimpleNamespace(get_workflow=lambda _workflow_id: {"revision": 3})
        )

        _raises_input_15937_1 = ProjectScheduledJobCreateInput(
            workflow_id="wf-1",
            cron_expr="0 10 * * *",
            created_by="alice",
        )
        with (
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
            patch(
                "core.application.registry.workflow_contract_service."
                "WorkflowInterfaceService.interface_for_workflow_id",
                side_effect=RuntimeError(secret),
            ),
            pytest.raises(
                ServiceUnavailableError,
                match=r"^Workflow contract service is unavailable; retry later$",
            ) as captured,
        ):
            service.create_scheduled_job(15, _raises_input_15937_1)

        assert captured.value.status_code == 503
        assert captured.value.payload["code"] == "workflow_contract_unavailable"
        assert secret not in str(captured.value)
        bl.create_scheduled_job.assert_not_called()

    def test_run_scheduled_job_now_rejects_workflow_outside_allowlist(self):
        # FS-DEEP-CAND-068: run-now must re-check the stored workflow_id against
        # the caller's allowed_workflow_ids before dispatching.
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 18, "name": "demo"}
        bl.get_scheduled_job.return_value = {"id": 7, "project_id": "18", "workflow_id": "wf-1"}
        run_scheduled_job_once = MagicMock(return_value={"status": "enqueued", "job_id": 55})
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda *args, **kwargs: ("", ""),
            run_scheduled_job_once=run_scheduled_job_once,
        )
        _raises_input_15979_1 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"wf-other"})
        )
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.run_scheduled_job_now(18, 7, scope=_raises_input_15979_1)
        run_scheduled_job_once.assert_not_called()

    def test_enqueue_manual_task_rejects_workflow_outside_allowlist(self):
        # FS-DEEP-CAND-031: workflow-scoped MCP/PAT caller must not enqueue a
        # project workflow outside its allowed_workflow_ids via manual task tools.
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "demo",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True, "is_default": True}],
        }
        enqueue_task = MagicMock(return_value=91)
        service = ProjectCommandService(
            get_logger=lambda: bl,
            resolve_workflow_selection=lambda requested, project_workflows=None: (
                requested,
                "explicit_request",
            ),
            validate_explicit_workflow_override=MagicMock(),
            sanitize_external_input=lambda value: str(value).strip(),
            enqueue_task=enqueue_task,
            parse_test_mode=lambda value, default: False,
        )
        _raises_input_16017_1 = ProjectTaskEnqueueInput(
            project_id="7",
            task_id="task-77",
            task_summary="Fix the flow",
            workflow_id="wf-1",
            task_source="web",
        )
        _raises_input_16017_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"wf-other"})
        )
        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.enqueue_manual_task(_raises_input_16017_1, scope=_raises_input_16017_2)
        enqueue_task.assert_not_called()

    def test_update_project_preserves_unknown_provider_owned_agent_config(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 1,
            "name": "Demo Project",
            "agents_config": {"fixture_events": {"destination_id": "ops"}},
        }
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "weekly_report"}],
            parse_agent_model_overrides=lambda form_data: {},
            build_project_policy_from_form=lambda form_data: {},
            to_json_obj=lambda value: value,
        )

        service.update_project(
            1,
            ProjectUpdateInput(
                name="Demo Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=["weekly_report"],
            ),
        )

        assert bl.update_project.call_args.kwargs["agents_config"] == {
            "fixture_events": {"destination_id": "ops"},
        }
        bl.save_provider_secret.assert_not_called()
        bl.save_project_provider_binding.assert_not_called()

    def test_update_project_persists_assigned_data_connections(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 1,
            "name": "Demo Project",
            "agents_config": {},
            "workspace_id": DEFAULT_WORKSPACE_ID,
        }
        bl.get_project_run_config.return_value = {
            "project_name": "Demo Project",
        }
        bl.list_project_data_connections.return_value = []
        bl.set_project_data_connections.return_value = [
            {"connection_id": "warehouse-primary", "display_name": "Warehouse Primary"}
        ]
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "weekly_report"}],
            parse_agent_model_overrides=lambda form_data: {},
            build_project_policy_from_form=lambda form_data: {},
            to_json_obj=lambda value: value,
        )

        service.update_project(
            1,
            ProjectUpdateInput(
                name="Demo Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=["weekly_report"],
                data_connection_ids=["warehouse-primary"],
                form_data={"data_connection_ids": ["warehouse-primary"]},
            ),
        )
        bl.set_project_data_connections.assert_called_once_with("1", ["warehouse-primary"])

    @pytest.mark.parametrize(
        ("field", "value"),
        [("policy_concurrency_cap", "0"), ("policy_autonomy_confidence_threshold", "2")],
    )
    def test_update_project_rejects_invalid_form_policy_before_persistence(self, field, value):
        from core.application.workflows.serve_workflow_helpers import build_project_policy_from_form

        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 1,
            "name": "Demo Project",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "agents_config": {},
        }
        bl.list_project_data_connections.return_value = []
        service = ProjectCommandService(
            get_logger=lambda: bl,
            parse_agent_model_overrides=lambda form_data: {},
            build_project_policy_from_form=build_project_policy_from_form,
        )
        command = ProjectUpdateInput(
            name="Keep project unchanged",
            workspace_id=DEFAULT_WORKSPACE_ID,
            form_data={field: value},
        )
        with pytest.raises(ServiceValidationError, match=field):
            service.update_project(1, command)
        bl.update_project.assert_not_called()
        bl.save_project_policy.assert_not_called()

    def test_update_project_preserves_existing_policy_when_policy_fields_omitted(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 1,
            "name": "Demo Project",
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "agents_config": {},
        }
        existing_policy = {
            "autonomy_mode": "auto",
            "retry_strategy": "failure_class",
        }
        bl.get_project_run_config.return_value = {
            "project_name": "Demo Project",
            "project_policy": existing_policy,
        }
        bl.list_project_data_connections.return_value = []
        build_project_policy_from_form = MagicMock(return_value={"autonomy_mode": "manual"})
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "weekly_report"}],
            parse_agent_model_overrides=lambda form_data: {},
            build_project_policy_from_form=build_project_policy_from_form,
            to_json_obj=lambda value: value,
        )

        service.update_project(
            1,
            ProjectUpdateInput(
                name="Renamed Demo",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=["weekly_report"],
                form_data={
                    "name": "Renamed Demo",
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                    "workflow_ids": ["weekly_report"],
                },
            ),
        )

        update_kwargs = bl.update_project.call_args.kwargs
        assert update_kwargs["project_policy"] is None
        build_project_policy_from_form.assert_not_called()

    def test_update_project_does_not_sync_profile_storage_in_request_path(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": 1,
            "name": "Demo Project",
            "agents_config": {},
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        write_project_profile_to_kb = MagicMock()
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "weekly_report"}],
            parse_agent_model_overrides=lambda form_data: {},
            build_project_policy_from_form=lambda form_data: {},
            to_json_obj=lambda value: value,
            write_project_profile_to_kb=write_project_profile_to_kb,
        )

        service.update_project(
            1,
            ProjectUpdateInput(
                name="Demo Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
                workflow_ids=["weekly_report"],
            ),
        )

        write_project_profile_to_kb.assert_not_called()

    def test_update_project_schedules_profile_storage_off_request_path(self):
        import threading

        bl = MagicMock()
        bl.get_project.return_value = {
            "id": 1,
            "name": "Demo Project",
            "agents_config": {},
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        started = threading.Event()
        release = threading.Event()

        def write_project_profile_to_kb(_payload):
            started.set()
            assert release.wait(timeout=2)

        service = ProjectCommandService(
            get_logger=lambda: bl,
            parse_agent_model_overrides=lambda form_data: {},
            build_project_policy_from_form=lambda form_data: {},
            to_json_obj=lambda value: value,
            write_project_profile_to_kb=write_project_profile_to_kb,
        )

        async def _run() -> None:
            payload = service.update_project(
                1,
                ProjectUpdateInput(
                    name="Demo Project",
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    workflow_ids=[],
                    form_data={"workflow_ids": []},
                ),
            ).dump()
            assert payload["status"] == "updated"
            assert await asyncio.to_thread(started.wait, 1)
            release.set()
            await asyncio.sleep(0.05)

        _run_async(_run())

    def test_update_project_forwards_description_to_persistence(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 1,
            "name": "Demo Project",
            "agents_config": {},
        }
        service = ProjectCommandService(
            get_logger=lambda: bl,
            list_workflow_choices=lambda **kwargs: [{"workflow_id": "weekly_report"}],
            parse_agent_model_overrides=lambda form_data: {},
            build_project_policy_from_form=lambda form_data: {},
            to_json_obj=lambda value: value,
        )

        service.update_project(
            1,
            ProjectUpdateInput(
                name="Demo Project",
                workspace_id=DEFAULT_WORKSPACE_ID,
                description="Project description from UI",
                workflow_ids=["weekly_report"],
            ),
        )

        assert bl.update_project.call_args.kwargs["description"] == "Project description from UI"


class _FakeRestoreValidationError(ValueError):
    def __init__(self, message: str, *, validation: dict | None = None):
        super().__init__(message)
        self.validation = validation or {"valid": False, "errors": [], "warnings": []}


class TestToolCommandService:
    @pytest.mark.parametrize("model", [ToolSaveInput, ToolPatchInput])
    def test_tool_command_models_reject_nested_noncanonical_interface(self, model):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            model(
                tool_id="fixture_nested_interface",
                display_name="Disabled draft",
                interface={"parameters": {"type": "object", "properties": {}}},
            )
        assert exc.value.errors()[0]["loc"] == ("interface", "parameters")

    @pytest.mark.parametrize("operation", ["replace", "patch", "clone_default"])
    def test_tool_service_rejects_noncanonical_persistence_key(self, operation):
        db = MagicMock()
        db.get_tool.side_effect = lambda key: (
            {
                "tool_id": "fixture invalid",
                "account_id": DEFAULT_ACCOUNT_ID,
                "display_name": "Bad source",
                "enabled": False,
                "implementation": {"type": "internal"},
            }
            if key == "fixture invalid"
            else None
        )
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=lambda: None,
            get_tool_validation=lambda *_args: {"valid": True},
            format_validation_error_detail=lambda **kwargs: kwargs,
        )
        if operation == "replace":
            action = service.update_tool
            payload = ToolSaveInput(tool_id="fixture_valid", display_name="Draft")
        elif operation == "patch":
            action = service.patch_tool
            payload = ToolPatchInput(description="Changed")
        else:
            action = service.clone_tool
            payload = ToolCloneInput()
        with pytest.raises(ServiceValidationError):
            action("fixture invalid", payload)
        db.save_tool.assert_not_called()

    @pytest.mark.parametrize(
        "identifier",
        [" ", "", "fixture invalid/id", "fixture/tool", "fixture\\tool", "fixture\x00tool", ".."],
        ids=["blank", "empty", "space-slash", "slash", "backslash", "control", "dot-segment"],
    )
    @pytest.mark.parametrize("model", [ToolSaveInput, ToolCloneInput])
    def test_tool_command_models_reject_noncanonical_ids(self, identifier, model):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            model(tool_id=identifier, display_name="Disabled draft")
        assert exc.value.errors()[0]["loc"] == ("tool_id",)

    @pytest.mark.parametrize(
        "identifier",
        ["fixture_tool", "std.numbers", "extension.acme.stock-loader", "projected.OperationV2"],
    )
    @pytest.mark.parametrize("model", [ToolSaveInput, ToolCloneInput])
    def test_tool_command_models_preserve_addressable_owner_ids(self, identifier, model):
        assert model(tool_id=identifier, display_name="Draft").tool_id == identifier

    def test_create_tool_returns_validation_payload(self):
        db = MagicMock()
        db.get_tool.return_value = None
        db._transaction.return_value.__enter__.return_value = None
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        payload = service.create_tool(
            ToolSaveInput(
                tool_id="search_tool",
                display_name="Search Tool",
                enabled=False,
                interface={"function_name": "search"},
                implementation={"type": "internal"},
            )
        ).dump()

        assert payload["status"] == "created"
        assert payload["tool_id"] == "search_tool"
        db.save_tool.assert_called_once()

    def test_create_tool_persists_request_account_scope(self):
        db = MagicMock()
        db.get_tool.return_value = None
        db._transaction.return_value.__enter__.return_value = None
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        service.create_tool(
            ToolSaveInput(
                tool_id="account_tool",
                display_name="Account Tool",
                enabled=False,
                interface={"function_name": "account_tool"},
                implementation={"type": "internal"},
            ),
            scope=ScopeContext(
                account_id="acc_beta",
                actor_id="admin-beta",
                principal_type="admin",
            ),
        )

        assert db.save_tool.call_args.args[0]["account_id"] == "acc_beta"

    def test_update_and_revision_restore_preserve_non_default_account_scope(self):
        existing = {
            "tool_id": "account_tool",
            "account_id": "acc_beta",
            "display_name": "Account Tool",
            "enabled": False,
            "implementation": {"type": "internal"},
        }
        db = MagicMock()
        db.get_tool.return_value = existing
        db._transaction.return_value.__enter__.return_value = None
        db.restore_entity_revision.return_value = {
            "restored": dict(existing),
            "validation": {"valid": True, "errors": [], "warnings": []},
        }
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda *_args: {"valid": True, "errors": [], "warnings": []},
            format_validation_error_detail=MagicMock(),
        )
        scope = ScopeContext(
            account_id="acc_beta",
            actor_id="admin-beta",
            principal_type="admin",
        )

        service.update_tool(
            "account_tool",
            ToolSaveInput(
                tool_id="account_tool",
                display_name="Updated account tool",
                enabled=False,
                implementation={"type": "internal"},
            ),
            scope=scope,
        )
        service.restore_tool_revision(
            "account_tool",
            ToolRestoreRevisionInput(entry_id=42),
            scope=scope,
        )

        assert db.save_tool.call_args.args[0]["account_id"] == "acc_beta"
        assert db.restore_entity_revision.call_args.kwargs["account_id"] == "acc_beta"

    def test_public_create_cannot_claim_mcp_projection_ownership(self):
        db = MagicMock()
        db.get_tool.return_value = None
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=MagicMock(),
            format_validation_error_detail=MagicMock(),
        )

        _raises_input_17225_1 = ToolSaveInput(
            tool_id="forged_projection",
            display_name="Forged projection",
            enabled=False,
            interface={"function_name": "forged_projection"},
            implementation={
                "type": "mcp",
                "connector_id": "foreign-connector",
                "owner_account_id": DEFAULT_ACCOUNT_ID,
                "owner_project_id": "project-a",
                "owner_tenant_id": "tenant-a",
            },
            config={
                "owner_account_id": DEFAULT_ACCOUNT_ID,
                "owner_project_id": "project-a",
                "owner_tenant_id": "tenant-a",
            },
        )
        _raises_input_17225_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            tenant_id="tenant-a",
            project_id="project-b",
            allowed_project_ids=frozenset({"project-b"}),
            principal_type="admin",
        )
        with pytest.raises(ServiceValidationError, match="owning connector sync"):
            service.create_tool(_raises_input_17225_1, scope=_raises_input_17225_2)

        db.save_tool.assert_not_called()

    @pytest.mark.parametrize("action", ["toggle", "clone", "restore"])
    def test_public_mutation_parity_rejects_mcp_projected_tools(self, action):
        projected = {
            "tool_id": "projected.read",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-a",
            "enabled": False,
            "implementation": {
                "type": "mcp",
                "connector_id": "mcp-catalog",
                "owner_account_id": DEFAULT_ACCOUNT_ID,
                "owner_project_id": "project-a",
                "owner_tenant_id": "tenant-a",
            },
        }
        db = MagicMock()
        db.get_tool.return_value = projected
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=MagicMock(),
            format_validation_error_detail=MagicMock(),
        )
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            tenant_id="tenant-a",
            project_id="project-a",
            allowed_project_ids=frozenset({"project-a"}),
            principal_type="admin",
        )

        def _raises_operation_16436():
            if action == "toggle":
                service.toggle_tool("projected.read", scope=scope)
            elif action == "clone":
                service.clone_tool(
                    "projected.read",
                    ToolCloneInput(tool_id="projected.copy"),
                    scope=scope,
                )
            else:
                service.restore_tool_revision(
                    "projected.read",
                    ToolRestoreRevisionInput(entry_id=1),
                    scope=scope,
                )

        with pytest.raises(ServiceValidationError, match="owning connector sync"):
            _raises_operation_16436()

        db.toggle_tool.assert_not_called()
        db.save_tool.assert_not_called()
        db.restore_entity_revision.assert_not_called()

    def test_create_tool_suffixes_duplicate_id(self):
        db = MagicMock()
        db.get_tool.side_effect = lambda tool_id: (
            {"tool_id": tool_id} if tool_id in {"search_tool", "search_tool_1"} else None
        )
        db._transaction.return_value.__enter__.return_value = None
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        payload = service.create_tool(
            ToolSaveInput(
                tool_id="search_tool",
                display_name="Search Tool",
                enabled=False,
                interface={"function_name": "search"},
                implementation={"type": "internal"},
            )
        ).dump()

        assert payload["tool_id"] == "search_tool_2"
        assert db.save_tool.call_args.args[0]["tool_id"] == "search_tool_2"

    def test_patch_tool_returns_no_change_without_updates(self):
        db = MagicMock()
        db.get_tool.return_value = {"tool_id": "search_tool", "enabled": False}
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        payload = service.patch_tool("search_tool", ToolPatchInput()).dump()

        assert payload["status"] == "no_change"
        db.save_tool.assert_not_called()

    def test_toggle_tool_blocks_invalid_enable(self):
        db = MagicMock()
        db.get_tool.return_value = {"tool_id": "broken_tool", "enabled": False}
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": False,
                "errors": [{"message": "broken"}],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        with pytest.raises(ServiceValidationError) as exc:
            service.toggle_tool("broken_tool")

        assert exc.value.payload["error"] == "Contract validation failed"
        db.toggle_tool.assert_not_called()

    def test_toggle_tool_blocks_unavailable_enable_with_reason(self):
        db = MagicMock()
        db.get_tool.return_value = {
            "tool_id": "orphaned_tool",
            "enabled": False,
            "availability_status": "unavailable",
            "availability_reason_code": "source_connector_removed",
            "availability_reason_text": "Source MCP connector mcp_removed was deleted.",
        }
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: kwargs,
        )

        with pytest.raises(ServiceConflictError, match="source_connector_removed") as exc:
            service.toggle_tool("orphaned_tool")

        assert "Source MCP connector mcp_removed was deleted." in str(exc.value)
        db.toggle_tool.assert_not_called()

    def test_update_tool_blocks_unavailable_enable_before_save(self):
        db = MagicMock()
        db.get_tool.return_value = {
            "tool_id": "orphaned_tool",
            "enabled": False,
            "availability_status": "unavailable",
            "availability_reason_code": "source_connector_removed",
            "availability_reason_text": "Source MCP connector mcp_removed was deleted.",
        }
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: kwargs,
        )

        _raises_input_17431_1 = ToolSaveInput(
            tool_id="orphaned_tool",
            display_name="Orphaned Tool",
            enabled=True,
            interface={"function_name": "invoke"},
            implementation={"type": "internal"},
        )
        with pytest.raises(ServiceConflictError, match="source_connector_removed"):
            service.update_tool("orphaned_tool", _raises_input_17431_1)

        db.save_tool.assert_not_called()

    def test_patch_tool_preserves_unavailable_lifecycle_metadata(self):
        db = MagicMock()
        db.get_tool.return_value = {
            "tool_id": "orphaned_tool",
            "display_name": "Orphaned Tool",
            "description": "Old description",
            "enabled": False,
            "availability_status": "unavailable",
            "availability_reason_code": "source_connector_removed",
            "availability_reason_text": "Source MCP connector mcp_removed was deleted.",
            "interface": {"function_name": "invoke"},
            "implementation": {"type": "internal"},
        }
        db._transaction.return_value.__enter__.return_value = None
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: kwargs,
        )

        service.patch_tool(
            "orphaned_tool",
            ToolPatchInput(description="Updated description"),
        )

        saved = db.save_tool.call_args.args[0]
        assert saved["enabled"] is False
        assert saved["availability_status"] == "unavailable"
        assert saved["availability_reason_code"] == "source_connector_removed"
        assert saved["availability_reason_text"] == (
            "Source MCP connector mcp_removed was deleted."
        )

    def test_toggle_tool_blocks_disable_for_agent_and_direct_workflow_dependencies(self):
        db = MagicMock()
        db.get_tool.return_value = {"tool_id": "search_web", "enabled": True}
        db.get_agents_using_tool.return_value = ["writer"]
        db.list_workflows.return_value = [
            {
                "workflow_id": "research",
                "enabled": True,
                "settings": {},
                "steps": [{"step_kind": "connector", "connector_id": "connector.search_web"}],
            }
        ]
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: kwargs,
        )

        with pytest.raises(ServiceConflictError, match="research"):
            service.toggle_tool("search_web")

        db.toggle_tool.assert_not_called()

    def test_patch_tool_cannot_bypass_enabled_workflow_disable_guard(self):
        db = MagicMock()
        db.get_tool.return_value = {"tool_id": "search_web", "enabled": True}
        db.get_agents_using_tool.return_value = []
        db.list_workflows.return_value = [
            {
                "workflow_id": "research",
                "enabled": True,
                "settings": {},
                "steps": [{"step_kind": "connector", "connector_id": "connector.search_web"}],
            }
        ]
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: kwargs,
        )

        _raises_input_17538_1 = ToolPatchInput(enabled=False)
        with pytest.raises(ServiceConflictError, match="research"):
            service.patch_tool("search_web", _raises_input_17538_1)

        db.save_tool.assert_not_called()

    def test_delete_tool_maps_foreign_key_violation_to_conflict(self):
        db = MagicMock()
        db.delete_tool.side_effect = RuntimeError("violates foreign key")
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        with pytest.raises(ServiceConflictError) as exc:
            service.delete_tool("search_tool")

        assert "remove associations first" in str(exc.value).lower()

    @pytest.mark.parametrize(
        "implementation",
        [
            {"type": "python", "module": "extensions.catalog.tool"},
            {"type": "mcp", "owner_tenant_id": "tenant-a"},
        ],
        ids=["python", "mcp"],
    )
    def test_clone_tool_refuses_projection_with_install_owned_runtime(self, implementation):
        from core.application.management import command_services_tool as tool_commands

        source = {
            "tool_id": "extension.install-1.catalog_tool",
            "account_id": DEFAULT_ACCOUNT_ID,
            "source_install_id": "install-1",
            "display_name": "Catalog Tool",
            "implementation": implementation,
        }
        db = MagicMock()
        db.get_tool.return_value = None
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda *_args: {"valid": True, "errors": [], "warnings": []},
            format_validation_error_detail=lambda **kwargs: kwargs,
        )

        with (
            patch.object(
                tool_commands, "projection_aware_catalog_row", create=True, return_value=source
            ),
            pytest.raises(ServiceConflictError) as exc,
        ):
            service.clone_tool(
                "extension.install-1.catalog_tool",
                ToolCloneInput(tool_id="catalog_tool_copy"),
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, tenant_id="tenant-a"),
            )

        assert exc.value.payload["code"] == "tool_clone_runtime_dependency_unavailable"
        db.save_tool.assert_not_called()

    def test_clone_tool_creates_disabled_copy(self):
        db = MagicMock()
        db.get_tool.side_effect = [
            {
                "tool_id": "search_tool",
                "display_name": "Search Tool",
                "enabled": True,
                "implementation": {"type": "internal"},
                "interface": {"function_name": "search"},
                "config": {"k": "v"},
                "tags": ["search"],
            },
            None,
        ]
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        payload = service.clone_tool(
            "search_tool", ToolCloneInput(tool_id="search_tool_copy")
        ).dump()

        assert payload["status"] == "cloned"
        assert payload["tool_id"] == "search_tool_copy"
        assert payload["source_tool_id"] == "search_tool"
        assert db.save_tool.call_args.args[0]["enabled"] is False

    def test_clone_tool_persists_request_account_scope(self):
        db = MagicMock()
        db.get_tool.side_effect = [
            {
                "tool_id": "search_tool",
                "display_name": "Search Tool",
                "account_id": "acc_beta",
                "enabled": False,
                "implementation": {"type": "internal"},
                "interface": {"function_name": "search"},
            },
            None,
        ]
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        service.clone_tool(
            "search_tool",
            ToolCloneInput(tool_id="search_tool_copy"),
            scope=ScopeContext(
                account_id="acc_beta",
                actor_id="admin-beta",
                principal_type="admin",
            ),
        )

        assert db.save_tool.call_args.args[0]["account_id"] == "acc_beta"

    def test_clone_tool_suffixes_duplicate_target_id(self):
        db = MagicMock()
        db.get_tool.side_effect = lambda tool_id: (
            {
                "tool_id": "search_tool",
                "display_name": "Search Tool",
                "enabled": True,
                "implementation": {"type": "internal"},
                "interface": {"function_name": "search"},
            }
            if tool_id == "search_tool"
            else {"tool_id": tool_id}
            if tool_id == "search_tool_copy"
            else None
        )
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        payload = service.clone_tool("search_tool", ToolCloneInput()).dump()

        assert payload["status"] == "cloned"
        assert payload["tool_id"] == "search_tool_copy_1"
        assert db.save_tool.call_args.args[0]["tool_id"] == "search_tool_copy_1"

    def test_restore_tool_revision_returns_snapshot_and_validation(self):
        db = MagicMock()
        db.restore_entity_revision.return_value = {
            "restored": {"tool_id": "search_tool", "display_name": "Search Tool"},
            "validation": {"valid": True, "errors": [], "warnings": []},
        }
        service = ToolCommandService(
            get_db=lambda: db,
            reload_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
            format_validation_error_detail=lambda **kwargs: {
                "error": "Contract validation failed",
                **kwargs,
            },
        )

        payload = service.restore_tool_revision(
            "search_tool",
            ToolRestoreRevisionInput(entry_id=12),
            restore_validation_error_cls=_FakeRestoreValidationError,
        ).dump()

        assert payload["status"] == "restored"
        assert payload["restored_from_entry_id"] == "12"
        assert payload["snapshot"]["tool_id"] == "search_tool"


# ---------------------------------------------------------------------------
# E12-S3 — Audit context threading through service callers
# ---------------------------------------------------------------------------


class TestExtensionAuditContextThreading:
    """Verify that execute_action/execute_query/invoke_extension thread
    scope-derived audit fields into runtime_context and log_extension_invocation."""

    def _make_scope(self):
        return ScopeContext(
            account_id="acct_42",
            project_id="7",
            workflow_id="onboarding",
            actor_id="jane",
            principal_type="user",
        )

    def test_execute_action_threads_audit_fields(self):
        bl = MagicMock()
        # FS-DEEP-CAND-040: execute_action validates the scope project against
        # the caller's account before dispatch — provide a same-account project.
        bl.get_project.return_value = {"id": "7", "account_id": "acct_42"}
        extension = {
            "extension_key": "fixture.notification-provider",
            "extension_install_id": "install-fixture-notifications",
            "enabled": True,
            "action_definitions": [],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda eid: (extension, bl),
            extension_identifier=lambda p: str((p or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *a, **kw: "extension",
            extension_operation_failure_status=lambda inv: 502,
        )
        body = ExtensionActionRequest(context={"project_id": "7"})
        scope = self._make_scope()

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action:
            service.execute_action("fixture_events", "save_config", body, scope=scope)

        ctx = run_action.call_args.kwargs["runtime_context"]
        assert ctx["account_id"] == "acct_42"
        assert ctx["workflow_id"] == "onboarding"
        assert ctx["actor_id"] == "jane"

        log_kwargs = bl.log_extension_invocation.call_args.kwargs
        assert log_kwargs["account_id"] == "acct_42"
        assert log_kwargs["actor_type"] == "user"
        assert log_kwargs["actor_id"] == "jane"
        assert log_kwargs["workflow_id"] == "onboarding"
        assert log_kwargs["action_id"] == "save_config"
        assert log_kwargs["request_context"]["source"] == "extension_binding_service.execute_action"
        assert log_kwargs["request_context"]["project_id"] == "7"

    def test_execute_query_threads_audit_fields(self):
        bl = MagicMock()
        extension = {
            "extension_key": "fixture.notification-provider",
            "extension_install_id": "install-fixture-notifications",
            "enabled": True,
            "query_definitions": [],
        }
        service = ExtensionBindingService(
            get_logger=lambda: bl,
            get_operation_store=lambda: bl,
            resolve_extension_runtime_record=lambda eid: (extension, bl),
            extension_identifier=lambda p: str((p or {}).get("extension_key") or ""),
            extension_operation_handler_mode=lambda *a, **kw: "extension",
            extension_operation_failure_status=lambda inv: 502,
        )
        body = ExtensionQueryRequest(context={"project_id": "7"})
        scope = self._make_scope()

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_query",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_query:
            service.execute_query("fixture_events", "get_config", body, scope=scope)

        ctx = run_query.call_args.kwargs["runtime_context"]
        assert ctx["account_id"] == "acct_42"
        assert ctx["workflow_id"] == "onboarding"

        log_kwargs = bl.log_extension_invocation.call_args.kwargs
        assert log_kwargs["account_id"] == "acct_42"
        assert log_kwargs["workflow_id"] == "onboarding"
        assert log_kwargs["query_id"] == "get_config"
        assert log_kwargs["request_context"]["source"] == "extension_binding_service.execute_query"
        assert log_kwargs["request_context"]["project_id"] == "7"

    def test_invoke_extension_threads_audit_fields(self):
        bl = MagicMock()
        bl.get_extension.return_value = {
            "extension_key": "demo",
            "enabled": True,
            "required_scopes": ["extension:invoke"],
        }
        service = ToolExecutionService(get_logger=lambda: bl)
        body = ExtensionInvokeRequest(request={"action": "test"})
        scope = self._make_scope()

        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_invocation",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_invoke:
            service.invoke_extension("demo", body, scope=scope)

        ctx = run_invoke.call_args.kwargs["runtime_context"]
        assert ctx["account_id"] == "acct_42"
        assert ctx["workflow_id"] == "onboarding"
        assert ctx["actor_id"] == "jane"

        log_kwargs = bl.log_extension_invocation.call_args.kwargs
        assert log_kwargs["account_id"] == "acct_42"
        assert log_kwargs["actor_type"] == "user"
        assert log_kwargs["workflow_id"] == "onboarding"
        assert log_kwargs["request_context"]["source"] == "tool_execution_service.invoke_extension"
        assert log_kwargs["request_context"]["project_id"] == "7"
        bl.get_extension.assert_called_once_with("demo", account_id="acct_42")

    def test_invoke_extension_uses_account_scoped_lookup(self):
        bl = MagicMock()
        bl.get_extension.return_value = None
        service = ToolExecutionService(get_logger=lambda: bl)

        _raises_input_17838_1 = ExtensionInvokeRequest(request={"action": "test"})
        _raises_input_17838_2 = self._make_scope()
        with pytest.raises(ServiceNotFoundError):
            service.invoke_extension("demo", _raises_input_17838_1, scope=_raises_input_17838_2)

        bl.get_extension.assert_called_once_with("demo", account_id="acct_42")


class TestCommandDispatchExtensionAudit:
    class _Store:
        def __init__(self) -> None:
            self.jobs: dict[str, dict[str, Any]] = {}
            self.audits: dict[str, dict[str, Any]] = {}
            self.get_extension = MagicMock(return_value=None)
            self.log_extension_invocation = MagicMock(return_value={})
            self.update_job = MagicMock(side_effect=self.update_job)

        def get_job(self, job_id: str) -> dict[str, Any] | None:
            row = self.jobs.get(job_id)
            return dict(row) if row else None

        @staticmethod
        def get_project(project_id: str) -> dict[str, Any]:
            return {"id": project_id, "account_id": DEFAULT_ACCOUNT_ID}

        def update_job(self, job_id: str, *, status: str) -> dict[str, Any]:
            self.jobs[job_id]["status"] = status
            return dict(self.jobs[job_id])

        def log_command_execution(self, **fields: Any) -> dict[str, Any]:
            assert "source" not in fields
            assert "correlation_id" not in fields
            self.audits[fields["command_id"]] = dict(fields)
            return dict(fields)

        def update_command_execution(
            self, *, command_id: str, status: str, result: dict[str, Any], error_text: str | None
        ) -> dict[str, Any]:
            row = self.audits[command_id]
            row.update(status=status, result=result, error_text=error_text)
            return dict(row)

    class _ProjectCommands:
        def __init__(self) -> None:
            self.requests: list[tuple[Any, dict[str, Any]]] = []

        def enqueue_manual_task(self, command: Any, **options: Any) -> Any:
            self.requests.append((command, options))
            return SimpleNamespace(job_id="33333333-3333-3333-3333-333333333333", status="pending")

    def test_pause_job_dispatch_accepts_uuid_job_id(self):
        job_id = "33333333-3333-3333-3333-333333333333"
        bl = self._Store()
        bl.jobs[job_id] = {"id": job_id, "account_id": DEFAULT_ACCOUNT_ID, "status": "in_progress"}
        service = CommandDispatchService(get_logger=lambda: bl)

        payload = service.dispatch(
            CommandDispatchInput(
                command_type="pause_job",
                target={"job_id": job_id},
                payload={},
            ),
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, principal_type="service_account"),
        ).dump()

        assert payload["status"] == "executed"
        assert payload["result"]["job_id"] == job_id
        bl.update_job.assert_called_once_with(job_id, status="paused")
        assert bl.jobs[job_id]["status"] == "paused"
        assert next(iter(bl.audits.values()))["status"] == "executed"

    def test_start_workflow_dispatch_validates_uuid_project_availability(self):
        project_id = "22222222-2222-2222-2222-222222222222"
        bl = self._Store()
        projects = self._ProjectCommands()
        service = CommandDispatchService(
            get_logger=lambda: bl, project_command_service_factory=lambda: projects
        )

        with patch.object(service, "_assert_can_start_workflow") as assert_can_start:
            service.dispatch(
                CommandDispatchInput(
                    command_type="start_workflow",
                    target={"workflow_id": "wf.release", "project_id": project_id},
                    payload={"task_summary": "run"},
                ),
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, project_id=project_id),
            )

        assert assert_can_start.call_args.kwargs["project_id"] == project_id
        assert len(projects.requests) == 1
        assert projects.requests[0][0].project_id == project_id
        assert projects.requests[0][1]["scope"].project_id == project_id
        assert next(iter(bl.audits.values()))["status"] == "executed"

    def test_update_external_status_requires_registered_extension(self):
        bl = self._Store()
        bl.get_extension.return_value = None
        service = CommandDispatchService(get_logger=lambda: bl)

        with pytest.raises(ServiceNotFoundError, match="Extension 'demo_status' not found"):
            service._route_command(
                "update_external_status",
                {
                    "extension_id": "demo_status",
                    "external_id": "ext-123",
                },
                {"status": "done"},
                scope=None,
                bl=bl,
            )

    def test_update_external_status_logs_extension_invocation_before_failure(self):
        bl = self._Store()
        bl.get_extension.return_value = {
            "extension_key": "demo_status",
            "enabled": True,
            "account_id": "acct_42",
        }
        service = CommandDispatchService(get_logger=lambda: bl)
        scope = ScopeContext(
            account_id="acct_42",
            project_id="7",
            workflow_id="wf.sync",
            actor_id="jane",
            principal_type="user",
        )

        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={"status": "failed", "detail": "permission denied", "response": {}},
            ),
            pytest.raises(ServiceError),
        ):
            service._route_command(
                "update_external_status",
                {
                    "extension_id": "demo_status",
                    "external_id": "ext-123",
                },
                {"status": "done"},
                scope=scope,
                bl=bl,
            )

        log_kwargs = bl.log_extension_invocation.call_args.kwargs
        assert log_kwargs["invocation_type"] == "action:sync_completion_status"
        assert log_kwargs["action_id"] == "sync_completion_status"
        assert log_kwargs["command_id"] == "update_external_status"
        assert log_kwargs["request"] == {}
        assert log_kwargs["request_context"]["source"] == "command_dispatch.update_external_status"
        assert log_kwargs["request_context"]["command_type"] == "update_external_status"

    def test_invoke_extension_action_failure_preserves_structured_errors(self):
        bl = self._Store()
        bl.get_extension.return_value = {
            "extension_key": "demo_action",
            "enabled": True,
            "account_id": DEFAULT_ACCOUNT_ID,
            "action_definitions": [{"action_id": "commit_snapshot"}],
        }
        service = CommandDispatchService(get_logger=lambda: bl)
        invocation = {
            "status": "failed",
            "detail": "project_state_revision_conflict",
            "response": {"ok": True},
            "errors": [
                {
                    "code": "project_state_revision_conflict",
                    "message": "revision changed",
                }
            ],
        }

        _raises_input_17967_1 = ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
        with (
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value=invocation,
            ),
            pytest.raises(ServiceError) as exc_info,
        ):
            service._route_command(
                "invoke_extension_action",
                {"extension_id": "demo_action", "action_id": "commit_snapshot"},
                {"snapshot_id": "snap_1"},
                scope=_raises_input_17967_1,
                bl=bl,
            )

        assert exc_info.value.payload["action"]["detail"] == "project_state_revision_conflict"
        assert exc_info.value.payload["errors"] == invocation["errors"]


class TestModelProviderCommandService:
    @pytest.fixture(autouse=True)
    def _stable_public_provider_dns(self, monkeypatch):
        """Keep endpoint-policy unit tests independent of external DNS availability."""
        monkeypatch.setattr(
            "core.application.extensions.url_safety._resolve_hostname_ips",
            lambda _hostname, *, port: ["93.184.216.34"],
        )

    def _service(self, bl):
        return ModelProviderCommandService(get_logger=lambda: bl)

    def _cursor(self):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        return cur

    def test_reverify_rate_limit_blocks_paid_probe_before_upstream_work(self):
        bl = MagicMock()
        service = self._service(bl)
        scope = ScopeContext(account_id="acc_alpha", actor_id="admin-1", principal_type="admin")

        with (
            patch(
                "core.application.management.command_services_model_provider."
                "model_provider_store.get_model_provider",
                return_value={"id": "provider-1", "account_id": "acc_alpha"},
            ),
            patch(
                "core.application.management.command_services_model_provider."
                "consume_model_provider_verification_limit",
                return_value=(False, 37),
            ) as consume_limit,
            patch.object(service, "_verify_selected_model") as verify,
            pytest.raises(ServiceError) as exc_info,
        ):
            service.reverify_model_provider("provider-1", scope=scope)

        assert exc_info.value.status_code == 429
        assert exc_info.value.payload["code"] == "rate_limited"
        assert exc_info.value.payload["retry_after"] == 37
        consume_limit.assert_called_once_with(
            bl.conn,
            account_id="acc_alpha",
            provider_id="provider-1",
        )
        verify.assert_not_called()

    def test_list_model_providers_is_available_when_not_self_hosted(self):
        bl = MagicMock()
        cur = self._cursor()
        cur.fetchall.return_value = []
        bl.conn.cursor.return_value = cur
        service = self._service(bl)

        with patch.dict(os.environ, {"SELF_HOSTED_MODE": "0"}):
            rows = service.list_model_providers()

        assert rows == []
        bl.conn.cursor.assert_called_once()

    def test_list_model_providers_falls_back_without_psycopg_runtime_extras(self, monkeypatch):
        bl = MagicMock()
        cur = self._cursor()
        cur.fetchall.return_value = [
            {
                "id": 1,
                "name": "demo",
                "provider_type": "openai_compatible",
                "api_key_ref": "secret_ref",
                "enabled": True,
            },
        ]
        bl.conn.cursor.return_value = cur
        service = self._service(bl)

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "psycopg_runtime.extras":
                raise ImportError("psycopg_runtime.extras unavailable")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}):
            rows = service.list_model_providers()

        assert bl.conn.cursor.call_args_list[0].kwargs == {}
        assert rows == [
            {"id": "1", "name": "demo", "provider_type": "openai_compatible", "enabled": True}
        ]

    def test_list_model_providers_maps_tuple_rows_without_realdictcursor(self, monkeypatch):
        bl = MagicMock()
        cur = self._cursor()
        cur.description = [("id",), ("name",), ("provider_type",), ("api_key_ref",), ("enabled",)]
        cur.fetchall.return_value = [
            (7, "byok", "openai_compatible", "secret_ref", True),
        ]
        bl.conn.cursor.return_value = cur
        service = self._service(bl)

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "psycopg_runtime.extras":
                raise ImportError("psycopg_runtime.extras unavailable")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}):
            rows = service.list_model_providers()

        assert rows == [
            {"id": "7", "name": "byok", "provider_type": "openai_compatible", "enabled": True}
        ]

    def test_create_model_provider_uses_scope_account_over_command_account_id(self):
        bl = MagicMock()
        service = self._service(bl)
        command = {
            "account_id": "acc_beta",
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "api_key": "secret-value",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.auth.secrets.encrypt_secret",
                return_value="encrypted-secret",
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.upsert_secret_ref",
            ) as upsert_secret,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.insert_model_provider",
                return_value="provider-1",
            ) as insert_provider,
            patch.object(
                service,
                "_verify_selected_model",
                return_value={"verification_status": "passed", "verified_capabilities": ["chat"]},
            ),
        ):
            result = service.create_model_provider(command, scope=scope)

        assert result == {
            "id": "provider-1",
            "name": "Demo",
            "provider_type": "openai_compatible",
            "status": "created",
            "verification_status": "passed",
            "verified_capabilities": ["chat"],
        }
        assert upsert_secret.call_args.kwargs["account_id"] == "acc_alpha"
        assert insert_provider.call_args.kwargs["account_id"] == "acc_alpha"

    def test_create_model_provider_rejects_unsafe_base_url_before_storing_secret(self):
        bl = MagicMock()
        service = self._service(bl)
        command = {
            "name": "Localhost",
            "provider_type": "openai_compatible",
            "base_url": "http://localhost:11434/v1",
            "api_key": "secret-value",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.upsert_secret_ref",
            ) as upsert_secret,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.insert_model_provider",
            ) as insert_provider,
            pytest.raises(ServiceValidationError, match="base_url is not allowed"),
        ):
            service.create_model_provider(command, scope=scope)

        upsert_secret.assert_not_called()
        insert_provider.assert_not_called()

    def test_create_model_provider_rejects_public_plain_http_base_url(self):
        service = self._service(MagicMock())
        command = {
            "name": "Plain HTTP",
            "provider_type": "openai_compatible",
            "base_url": "http://provider.example/v1",
            "api_key": "secret-value",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.extensions.url_safety._resolve_hostname_ips",
                return_value=["93.184.216.34"],
            ),
            pytest.raises(ServiceValidationError, match="must use https URL"),
        ):
            service.create_model_provider(command, scope=scope)

    @pytest.mark.parametrize("provider_type", ["openai", "anthropic", "google"])
    def test_create_fixed_endpoint_provider_rejects_custom_base_url(self, provider_type):
        service = self._service(MagicMock())
        command = {
            "name": "Official provider",
            "provider_type": provider_type,
            "base_url": "https://gateway.example/v1",
            "api_key": "secret-value",
            "default_model": "model-a",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.insert_model_provider",
            ) as insert_provider,
            pytest.raises(ServiceValidationError, match="does not accept base_url"),
        ):
            service.create_model_provider(command, scope=scope)

        insert_provider.assert_not_called()

    def test_create_official_openai_provider_uses_fixed_endpoint_without_custom_url(self):
        service = self._service(MagicMock())
        command = {
            "name": "OpenAI",
            "provider_type": "openai",
            "api_key": "secret-value",
            "default_model": "gpt-5.6-terra",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.insert_model_provider",
                return_value="provider-openai",
            ) as insert_provider,
            patch.object(
                service, "_verify_selected_model", return_value={"verification_status": "passed"}
            ),
        ):
            service.create_model_provider(command, scope=scope)

        assert insert_provider.call_args.kwargs["provider_type"] == "openai"
        assert insert_provider.call_args.kwargs["base_url"] is None

    def test_create_openai_compatible_provider_requires_account_endpoint(self):
        service = self._service(MagicMock())
        command = {
            "name": "Gateway",
            "provider_type": "openai_compatible",
            "api_key": "secret-value",
            "default_model": "model-a",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.insert_model_provider",
            ) as insert_provider,
            pytest.raises(ServiceValidationError, match="base_url is required"),
        ):
            service.create_model_provider(command, scope=scope)

        insert_provider.assert_not_called()

    @pytest.mark.parametrize(
        ("missing_field", "message"),
        [
            ("base_url", "base_url is required"),
            ("api_version", "api_version is required"),
            ("deployment_name", "deployment_name is required"),
        ],
    )
    def test_create_azure_provider_requires_complete_account_endpoint_contract(
        self, missing_field, message
    ):
        service = self._service(MagicMock())
        command = {
            "name": "Azure",
            "provider_type": "azure_openai",
            "base_url": "https://resource.openai.azure.com",
            "api_version": "2024-06-01",
            "deployment_name": "deployment-a",
            "api_key": "secret-value",
            "default_model": "model-a",
            "config": {},
        }
        command[missing_field] = ""
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.insert_model_provider",
            ) as insert_provider,
            pytest.raises(ServiceValidationError, match=message),
        ):
            service.create_model_provider(command, scope=scope)

        insert_provider.assert_not_called()

    def test_create_model_provider_rejects_plaintext_secret_in_config(self):
        service = self._service(MagicMock())
        command = {
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "config": {"api_key": "must-not-be-stored"},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            pytest.raises(ServiceValidationError, match="secret"),
        ):
            service.create_model_provider(command, scope=scope)

    @pytest.mark.parametrize(
        "config",
        [
            {"nested": {"private_key": "must-not-be-stored"}},
            {"headers": {"cookie": "session=must-not-be-stored"}},
            {"auth": {"credentials": "must-not-be-stored"}},
            {"headers": [["Private-Key", "must-not-be-stored"]]},
            {"secretKey": "must-not-be-stored"},
            {"nested": {"accessKey": "must-not-be-stored"}},
        ],
    )
    def test_create_model_provider_reuses_canonical_recursive_secret_classifier(self, config):
        service = self._service(MagicMock())
        command = {
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "config": config,
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            pytest.raises(ServiceValidationError, match="secret"),
        ):
            service.create_model_provider(command, scope=scope)

    def test_create_model_provider_rejects_arbitrary_untyped_config(self):
        service = self._service(MagicMock())
        command = {
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "config": {"timeout": 30},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            pytest.raises(ServiceValidationError, match="config"),
        ):
            service.create_model_provider(command, scope=scope)

    def test_create_model_provider_persists_row_and_secret_in_one_transaction(self):
        bl = MagicMock()
        transaction_conn = MagicMock(name="transaction_conn")
        service = self._service(bl)
        events = []
        command = {
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "api_key": "secret-value",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        @contextlib.contextmanager
        def transaction():
            events.append(("begin", transaction_conn))
            yield transaction_conn
            events.append(("commit", transaction_conn))

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch("core.application.auth.secrets.encrypt_secret", return_value="encrypted"),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=transaction(),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.insert_model_provider",
                side_effect=lambda conn, **_kwargs: (
                    events.append(("insert_provider", conn)) or "provider-1"
                ),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.upsert_secret_ref",
                side_effect=lambda conn, **_kwargs: events.append(("insert_secret", conn)),
            ),
            patch.object(
                service,
                "_verify_selected_model",
                side_effect=lambda **_kwargs: (
                    events.append(("verify", bl.conn)) or {"verification_status": "passed"}
                ),
            ),
        ):
            service.create_model_provider(command, scope=scope)

        assert [event[0] for event in events] == [
            "begin",
            "insert_provider",
            "insert_secret",
            "commit",
            "verify",
        ]
        assert events[1][1] is transaction_conn
        assert events[2][1] is transaction_conn

    def test_custom_provider_verification_failure_is_persisted_fail_closed(self):
        service = self._service(MagicMock())
        with (
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_model_provider",
                return_value={"id": "provider-1", "provider_type": "openai_compatible"},
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.claim_model_provider_verification",
                return_value={"verification_generation": "gen-1"},
            ),
            patch(
                "core.application.orchestration.model_provider.get_chat_model",
                side_effect=TimeoutError("upstream timed out"),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.set_model_provider_verification",
                return_value=True,
            ) as save_verification,
        ):
            result = service._verify_selected_model(
                conn=MagicMock(), account_id="acc_alpha", provider_id="provider-1"
            )

        assert result == {"verification_status": "failed", "verification_error_safe": "timeout"}
        assert save_verification.call_args.kwargs["status"] == "failed"

    def test_custom_provider_decrypt_failure_publishes_failed_verification(self):
        service = self._service(MagicMock())
        snapshot = {
            "id": "provider-1",
            "provider_type": "openai_compatible",
            "api_key_ref": "provider-key",
            "default_model": "model-a",
        }
        conn = MagicMock()
        with (
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                side_effect=lambda value: contextlib.nullcontext(value),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_model_provider",
                return_value=snapshot,
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_secret_ref_ciphertext",
                return_value="malformed-ciphertext",
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.claim_model_provider_verification",
                return_value={"verification_generation": "gen-1"},
            ),
            patch(
                "core.application.auth.secrets.decrypt_secret",
                side_effect=ValueError("malformed ciphertext"),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.set_model_provider_verification",
                return_value=True,
            ) as save_verification,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.cancel_model_provider_verification",
                return_value=True,
            ) as cancel_verification,
            patch(
                "core.application.orchestration.model_provider.get_chat_model",
            ) as get_model,
        ):
            result = service._verify_selected_model(
                conn=conn, account_id="acc_alpha", provider_id="provider-1"
            )

        assert result == {
            "verification_status": "failed",
            "verification_error_safe": "invalid_response",
        }
        assert save_verification.call_args.kwargs["status"] == "failed"
        assert save_verification.call_args.kwargs["safe_error"] == "invalid_response"
        cancel_verification.assert_not_called()
        get_model.assert_not_called()

    def test_custom_provider_verification_cancellation_releases_owned_generation(self):
        service = self._service(MagicMock())
        conn = MagicMock()
        with (
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                side_effect=lambda value: contextlib.nullcontext(value),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_model_provider",
                return_value={
                    "id": "provider-1",
                    "provider_type": "openai_compatible",
                    "api_key_ref": "provider-key",
                },
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_secret_ref_ciphertext",
                return_value="ciphertext",
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.claim_model_provider_verification",
                return_value={"verification_generation": "gen-1"},
            ),
            patch(
                "core.application.auth.secrets.decrypt_secret",
                side_effect=KeyboardInterrupt,
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.set_model_provider_verification",
            ) as save_verification,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.cancel_model_provider_verification",
                return_value=True,
            ) as cancel_verification,
            pytest.raises(KeyboardInterrupt),
        ):
            service._verify_selected_model(
                conn=conn, account_id="acc_alpha", provider_id="provider-1"
            )

        save_verification.assert_not_called()
        assert cancel_verification.call_args.kwargs["generation"]

    def test_custom_provider_verification_returns_stale_after_concurrent_save(self):
        service = self._service(MagicMock())
        with (
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_model_provider",
                return_value={"id": "provider-1", "provider_type": "openai_compatible"},
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.claim_model_provider_verification",
                return_value={"verification_generation": "gen-1"},
            ),
            patch(
                "core.application.orchestration.model_provider.get_chat_model",
                return_value=MagicMock(),
            ),
            patch(
                "core.application.settings.llm_capability_verifier.run_capability_suite",
                return_value={"chat", "tools", "structured_output"},
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.set_model_provider_verification",
                return_value=False,
            ) as save_verification,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.cancel_model_provider_verification",
                return_value=True,
            ) as cancel_verification,
        ):
            result = service._verify_selected_model(
                conn=MagicMock(), account_id="acc_alpha", provider_id="provider-1"
            )

        assert result == {"verification_status": "stale"}
        assert save_verification.call_args.kwargs["generation"]
        assert cancel_verification.call_args.kwargs["generation"]

    def test_custom_provider_verification_claim_prevents_duplicate_probe(self):
        service = self._service(MagicMock())
        snapshot = {
            "id": "provider-1",
            "provider_type": "openai_compatible",
            "default_model": "model-a",
        }
        with (
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                side_effect=lambda conn: contextlib.nullcontext(conn),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_model_provider",
                return_value=snapshot,
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.claim_model_provider_verification",
                side_effect=[{"verification_generation": "gen-1"}, None],
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.set_model_provider_verification",
                return_value=True,
            ),
            patch(
                "core.application.orchestration.model_provider.get_chat_model",
                return_value=MagicMock(),
            ),
            patch(
                "core.application.settings.llm_capability_verifier.run_capability_suite",
                return_value={"chat", "tools", "structured_output"},
            ) as run_suite,
            patch("uuid.uuid4", side_effect=[MagicMock(hex="gen-1"), MagicMock(hex="gen-2")]),
        ):
            first = service._verify_selected_model(
                conn=MagicMock(), account_id="acc_alpha", provider_id="provider-1"
            )
            second = service._verify_selected_model(
                conn=MagicMock(), account_id="acc_alpha", provider_id="provider-1"
            )

        assert first["verification_status"] == "passed"
        assert second == {"verification_status": "stale"}
        run_suite.assert_called_once()

    def test_custom_provider_verification_uses_claimed_snapshot_with_bounded_probe(self):
        service = self._service(MagicMock())
        snapshot = {
            "id": "provider-1",
            "provider_type": "openai_compatible",
            "base_url": "https://provider.example/v1",
            "api_key_ref": "provider-key",
            "default_model": "model-a",
            "secret_ciphertext": "ciphertext-a",
        }
        with (
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                side_effect=lambda conn: contextlib.nullcontext(conn),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_model_provider",
                return_value=dict(snapshot),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_secret_ref_ciphertext",
                return_value="ciphertext-a",
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.claim_model_provider_verification",
                return_value={"verification_generation": "gen-1"},
            ),
            patch("core.application.auth.secrets.decrypt_secret", return_value="api-key-a"),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.set_model_provider_verification",
                return_value=True,
            ),
            patch(
                "core.application.orchestration.model_provider.get_chat_model",
                return_value=MagicMock(),
            ) as get_model,
            patch(
                "core.application.settings.llm_capability_verifier.run_capability_suite",
                return_value={"chat", "tools", "structured_output"},
            ) as run_suite,
        ):
            result = service._verify_selected_model(
                conn=MagicMock(), account_id="acc_alpha", provider_id="provider-1"
            )

        assert result["verification_status"] == "passed"
        assert get_model.call_args.kwargs["timeout_seconds"] == 60
        assert run_suite.call_args.kwargs["timeout_seconds"] == 60
        claimed = get_model.call_args.kwargs["claimed_custom_provider"]
        assert claimed["id"] == "provider-1"
        assert claimed["_decrypted_api_key"] == "api-key-a"
        assert "secret_ciphertext" not in claimed
        assert get_model.call_args.args[0]["agents_config"]["max_retries"] == 0

    def test_update_model_provider_uses_scope_account(self):
        bl = MagicMock()
        service = self._service(bl)
        command = {
            "account_id": "acc_alpha",
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(bl.conn),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
                return_value={
                    "id": "provider-1",
                    "account_id": "acc_alpha",
                    "provider_type": "openai_compatible",
                    "base_url": "https://example.com/v1",
                    "config_jsonb": {},
                },
            ) as lock_provider,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.update_model_provider",
                return_value={"id": "provider-1", "account_id": "acc_alpha"},
            ) as update_provider,
            patch.object(
                service, "_verify_selected_model", return_value={"verification_status": "passed"}
            ),
        ):
            result = service.update_model_provider("provider-1", command, scope=scope)

        assert result["status"] == "updated"
        lock_provider.assert_called_once_with(
            bl.conn,
            provider_id="provider-1",
            account_id="acc_alpha",
        )
        assert update_provider.call_args.kwargs["account_id"] == "acc_alpha"

    def test_display_only_provider_update_preserves_passed_verification(self):
        bl = MagicMock()
        transaction_conn = MagicMock(name="transaction_conn")
        service = self._service(bl)
        existing = {
            "id": "provider-1",
            "account_id": "acc_alpha",
            "name": "Old name",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "api_key_ref": "provider_old_key",
            "api_version": None,
            "deployment_name": None,
            "default_model": "model-a",
            "config_jsonb": {},
            "verification_status": "passed",
            "verified_capabilities_jsonb": ["chat", "structured_output", "tools"],
        }
        command = {
            "name": "New name",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "default_model": "model-a",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(transaction_conn),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
                return_value=existing,
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.update_model_provider",
                return_value={**existing, "name": "New name"},
            ) as update_provider,
            patch.object(service, "_verify_selected_model") as verify,
        ):
            result = service.update_model_provider("provider-1", command, scope=scope)

        assert update_provider.call_args.kwargs["invalidate_verification"] is False
        verify.assert_not_called()
        assert result["verification_status"] == "passed"
        assert result["verified_capabilities"] == ["chat", "structured_output", "tools"]

    def test_endpoint_change_invalidates_verification_and_reprobes_saved_snapshot(self):
        bl = MagicMock()
        service = self._service(bl)
        existing = {
            "id": "provider-1",
            "account_id": "acc_alpha",
            "provider_type": "openai_compatible",
            "base_url": "https://gateway.example/v1",
            "api_key_ref": "provider_key",
            "api_version": None,
            "deployment_name": None,
            "default_model": "model-a",
            "config_jsonb": {},
            "verification_status": "passed",
        }
        command = {
            "name": "Gateway",
            "provider_type": "openai_compatible",
            "base_url": "https://new-gateway.example/v1",
            "default_model": "model-a",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch.object(service, "_validated_base_url", side_effect=lambda value: value),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(bl.conn),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
                return_value=existing,
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.update_model_provider",
                return_value={**existing, "base_url": command["base_url"]},
            ) as update_provider,
            patch.object(
                service,
                "_verify_selected_model",
                return_value={"verification_status": "passed"},
            ) as verify,
        ):
            service.update_model_provider("provider-1", command, scope=scope)

        assert update_provider.call_args.kwargs["invalidate_verification"] is True
        verify.assert_called_once_with(
            conn=bl.conn,
            account_id="acc_alpha",
            provider_id="provider-1",
        )

    def test_provider_key_rotation_is_atomic_and_deletes_displaced_secret(self):
        bl = MagicMock()
        transaction_conn = MagicMock(name="transaction_conn")
        service = self._service(bl)
        events = []
        existing = {
            "id": "provider-1",
            "account_id": "acc_alpha",
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "api_key_ref": "provider_old_key",
            "api_version": None,
            "deployment_name": None,
            "default_model": "model-a",
            "config_jsonb": {},
            "verification_status": "passed",
        }
        command = {
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "api_key": "replacement-secret",
            "default_model": "model-a",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        @contextlib.contextmanager
        def transaction():
            events.append(("begin", transaction_conn))
            yield transaction_conn
            events.append(("commit", transaction_conn))

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch("core.application.auth.secrets.encrypt_secret", return_value="encrypted-new"),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=transaction(),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
                side_effect=lambda conn, **_kwargs: events.append(("lock", conn)) or existing,
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.upsert_secret_ref",
                side_effect=lambda conn, **_kwargs: events.append(("insert_secret", conn)),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.update_model_provider",
                side_effect=lambda conn, **kwargs: (
                    events.append(("update_provider", conn, kwargs["invalidate_verification"]))
                    or {"id": "provider-1"}
                ),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.delete_secret_ref",
                side_effect=lambda conn, **_kwargs: events.append(("delete_old_secret", conn)),
            ),
            patch.object(
                service,
                "_verify_selected_model",
                side_effect=lambda **_kwargs: (
                    events.append(("verify", bl.conn)) or {"verification_status": "passed"}
                ),
            ),
        ):
            service.update_model_provider("provider-1", command, scope=scope)

        assert [event[0] for event in events] == [
            "begin",
            "lock",
            "insert_secret",
            "update_provider",
            "delete_old_secret",
            "commit",
            "verify",
        ]
        assert all(event[1] is transaction_conn for event in events[1:5])
        assert events[3][2] is True

    def test_update_model_provider_rejects_unsafe_base_url_before_storing_secret(self):
        bl = MagicMock()
        service = self._service(bl)
        command = {
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "http://169.254.169.254/latest/meta-data",
            "api_key": "replacement-secret",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.get_model_provider",
                return_value={"id": "provider-1", "account_id": "acc_alpha"},
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.upsert_secret_ref",
            ) as upsert_secret,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.update_model_provider",
            ) as update_provider,
            pytest.raises(ServiceValidationError, match="base_url is not allowed"),
        ):
            service.update_model_provider("provider-1", command, scope=scope)

        upsert_secret.assert_not_called()
        update_provider.assert_not_called()

    @pytest.mark.parametrize(
        "command",
        [
            {
                "name": "Anthropic",
                "provider_type": "anthropic",
                "base_url": "https://gateway.example/v1",
                "default_model": "claude-model",
                "config": {},
            },
            {
                "name": "Azure",
                "provider_type": "azure_openai",
                "base_url": "https://resource.openai.azure.com",
                "api_version": "",
                "deployment_name": "deployment-a",
                "default_model": "model-a",
                "config": {},
            },
        ],
    )
    def test_update_model_provider_enforces_endpoint_contract_before_lock(self, command):
        bl = MagicMock()
        service = self._service(bl)
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
            ) as lock_provider,
            pytest.raises(ServiceValidationError),
        ):
            service.update_model_provider("provider-1", command, scope=scope)

        lock_provider.assert_not_called()

    def test_update_model_provider_ignores_command_account_id_mismatch(self):
        bl = MagicMock()
        service = self._service(bl)
        command = {
            "account_id": "acc_beta",
            "name": "Demo",
            "provider_type": "openai_compatible",
            "base_url": "https://example.com/v1",
            "config": {},
        }
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=contextlib.nullcontext(bl.conn),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
                return_value={
                    "id": "provider-1",
                    "account_id": "acc_alpha",
                    "provider_type": "openai_compatible",
                    "base_url": "https://example.com/v1",
                    "config_jsonb": {},
                },
            ) as lock_provider,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.update_model_provider",
                return_value={"id": "provider-1", "account_id": "acc_alpha"},
            ) as update_provider,
            patch.object(
                service, "_verify_selected_model", return_value={"verification_status": "passed"}
            ),
        ):
            result = service.update_model_provider("provider-1", command, scope=scope)

        assert result["status"] == "updated"
        lock_provider.assert_called_once_with(
            bl.conn,
            provider_id="provider-1",
            account_id="acc_alpha",
        )
        assert update_provider.call_args.kwargs["account_id"] == "acc_alpha"

    def test_delete_model_provider_uses_scope_account_for_provider_and_secret(self):
        bl = MagicMock()
        service = self._service(bl)
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
                return_value={
                    "id": "provider-1",
                    "account_id": "acc_alpha",
                    "api_key_ref": "provider_demo_key",
                },
            ) as lock_provider,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.delete_secret_ref",
            ) as delete_secret,
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.delete_model_provider",
            ) as delete_provider,
        ):
            result = service.delete_model_provider("provider-1", scope=scope)

        assert result == {"id": "provider-1", "status": "deleted"}
        transaction_conn = lock_provider.call_args.args[0]
        lock_provider.assert_called_once_with(
            transaction_conn,
            provider_id="provider-1",
            account_id="acc_alpha",
        )
        delete_secret.assert_called_once_with(
            transaction_conn,
            ref_key="provider_demo_key",
            account_id="acc_alpha",
        )
        delete_provider.assert_called_once_with(
            transaction_conn,
            provider_id="provider-1",
            account_id="acc_alpha",
        )

    def test_delete_model_provider_removes_row_and_secret_in_one_transaction(self):
        bl = MagicMock()
        transaction_conn = MagicMock(name="transaction_conn")
        service = self._service(bl)
        events = []
        scope = ScopeContext(account_id="acc_alpha", actor_id="ops", principal_type="admin")

        @contextlib.contextmanager
        def transaction():
            events.append(("begin", transaction_conn))
            yield transaction_conn
            events.append(("commit", transaction_conn))

        with (
            patch.dict(os.environ, {"SELF_HOSTED_MODE": "1"}),
            patch(
                "core.infrastructure.db.db_pool.transaction_connection",
                return_value=transaction(),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.lock_model_provider_for_update",
                side_effect=lambda conn, **_kwargs: (
                    events.append(("lock", conn))
                    or {"id": "provider-1", "api_key_ref": "provider_old_key"}
                ),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.delete_model_provider",
                side_effect=lambda conn, **_kwargs: events.append(("delete_provider", conn)),
            ),
            patch(
                "core.application.management.command_services_model_provider.model_provider_store.delete_secret_ref",
                side_effect=lambda conn, **_kwargs: events.append(("delete_secret", conn)),
            ),
        ):
            service.delete_model_provider("provider-1", scope=scope)

        assert [event[0] for event in events] == [
            "begin",
            "lock",
            "delete_provider",
            "delete_secret",
            "commit",
        ]
        assert all(event[1] is transaction_conn for event in events[1:4])


def test_connection_cleanup_uses_caller_transaction_and_removes_owned_state():
    from core.application.management.command_services_project_connection import (
        cleanup_project_connection_owned_data,
    )

    bl = MagicMock()
    cur = MagicMock()
    connection = {
        "connection_id": "conn_fixture",
        "account_id": DEFAULT_ACCOUNT_ID,
        "project_id": "project_fixture",
    }

    revocations = []
    prepared = {"endpoint": "https://oauth.example/revoke", "data": {"token": "canary"}}
    with (
        patch(
            "core.application.auth.oauth_client.prepare_connection_oauth_revocation",
            return_value=prepared,
        ) as prepare_revoke,
        patch("core.application.auth.oauth_client.revoke_connection_oauth_tokens") as revoke,
    ):
        cleanup_project_connection_owned_data(
            bl,
            connection=connection,
            cur=cur,
            oauth_revocations=revocations,
        )

    revoke.assert_not_called()
    prepare_revoke.assert_called_once_with(bl, connection=connection)
    assert revocations == [prepared]
    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert "SELECT connection_id" in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "DELETE FROM project_connection_oauth_states" in statements[1]
    assert any("DELETE FROM secret_refs" in statement for statement in statements)
    assert any(
        "DELETE FROM project_connection_oauth_states" in statement for statement in statements
    )
    assert any("DELETE FROM project_connection_sessions" in statement for statement in statements)
    assert any("DELETE FROM extension_project_state" in statement for statement in statements)
    assert any("DELETE FROM project_connections" in statement for statement in statements)
    assert not bl.cursor.called


def test_project_unrelated_update_preserves_opaque_legacy_extra_tools_and_readback():
    from copy import deepcopy

    legacy_tools = [
        {
            "name": "retired_http",
            "description": "Legacy definition retained for recovery",
            "type": "http",
            "url": "https://retired.example.invalid/hook",
            "method": "POST",
        }
    ]
    original_config = {
        "extra_tools": deepcopy(legacy_tools),
        "default_provider": "",
        "default_model": "",
        "agents": {"config": {"composer": {"timeout_seconds": 17}}},
        "unrelated_setting": {"nested": ["keep", "these"]},
    }
    stored = {
        "id": "7",
        "account_id": DEFAULT_ACCOUNT_ID,
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "name": "Before",
        "description": "Keep description",
        "project_rules": "Keep project rules",
        "agents_config": deepcopy(original_config),
    }
    bl = MagicMock()
    bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "id": DEFAULT_WORKSPACE_ID,
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    bl.get_project.side_effect = lambda *_args, **_kwargs: deepcopy(stored)
    bl.get_project_run_config.side_effect = lambda *_args, **_kwargs: {
        "project_name": stored["name"],
    }
    bl.list_project_data_connections.return_value = []
    bl.set_project_data_connections.return_value = []

    def persist_project(_project_id, **fields):
        stored.update(deepcopy(fields))

    bl.update_project.side_effect = persist_project
    service = ProjectCommandService(
        get_logger=lambda: bl,
        list_workflow_choices=lambda **_kwargs: [],
    )
    result = service.update_project(
        "7",
        ProjectUpdateInput(
            name="After",
            workspace_id=DEFAULT_WORKSPACE_ID,
            workflow_ids=[],
            form_data={
                "name": "After",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "workflow_ids": [],
            },
        ),
    ).dump()

    assert result["status"] == "updated"
    bl.update_project.assert_called_once()
    assert bl.update_project.call_args.kwargs["agents_config"] == original_config
    readback = bl.get_project("7")
    assert readback["name"] == "After"
    assert readback["agents_config"] == original_config
    assert readback["agents_config"]["extra_tools"] == legacy_tools
    assert readback["description"] == "Keep description"
    assert readback["project_rules"] == "Keep project rules"


@pytest.fixture(autouse=True)
def _unit_schedule_owner_fence(monkeypatch):
    # These in-memory command stores model a live workflow, not SQL locking.
    from contextlib import nullcontext

    monkeypatch.setattr(
        "core.application.management.command_services_project_run_requests_mixin.workflow_trigger_transaction",
        lambda *_args, **_kwargs: nullcontext(),
    )


class TestAgentCommandServiceClone:
    @staticmethod
    def _commands():
        assert hasattr(command_services_module, "AgentCommandService"), (
            "Agent clone policy service is missing"
        )
        from core.application.management import command_services_agent

        return command_services_agent

    def test_clone_projected_skill_prompts_use_exact_install_reference(self):
        agent_commands = self._commands()
        install_id = "install-1"
        skill_id = "extension.install-1.summarize_support_ticket"
        source = {
            "source_install_id": install_id,
            "prompts": {
                "skill_prompts": {
                    "summarize_support_ticket": "Authored local-ID prompt",
                    "support_ticket": "Do not match by suffix",
                }
            },
            "skills": [
                {"skill_id": skill_id, "skill_prompt": ""},
                {
                    "skill_id": "extension.install-1.other_skill",
                    "skill_prompt": "Explicit override",
                },
            ],
        }

        prompts = agent_commands.AgentCommandService._skill_prompts(
            source,
            [skill_id, "extension.install-1.other_skill"],
        )

        assert prompts["skill_prompts"] == {
            skill_id: "Authored local-ID prompt",
            "extension.install-1.other_skill": "Explicit override",
            "support_ticket": "Do not match by suffix",
        }

    def test_clone_keeps_canonical_prompt_when_projected_assignment_is_empty(self):
        agent_commands = self._commands()
        skill_id = "extension.install-1.canonical_skill"
        source = {
            "source_install_id": "install-1",
            "prompts": {"skill_prompts": {skill_id: "Canonical authored prompt"}},
            "skills": [{"skill_id": skill_id, "skill_prompt": ""}],
        }

        prompts = agent_commands.AgentCommandService._skill_prompts(source, [skill_id])

        assert prompts["skill_prompts"] == {skill_id: "Canonical authored prompt"}

    def test_clone_prefers_nonempty_assignment_over_projected_local_alias(self):
        agent_commands = self._commands()
        skill_id = "extension.install-1.local_skill"
        source = {
            "source_install_id": "install-1",
            "prompts": {"skill_prompts": {"local_skill": "Authored alias prompt"}},
            "skills": [{"skill_id": skill_id, "skill_prompt": "Explicit assignment prompt"}],
        }

        prompts = agent_commands.AgentCommandService._skill_prompts(source, [skill_id])

        assert prompts["skill_prompts"] == {skill_id: "Explicit assignment prompt"}

    def test_clone_projection_preserves_owned_content_and_relationship_snapshot(self):
        agent_commands = self._commands()

        source = {
            "agent_id": "extension.install-1.catalog_agent",
            "account_id": DEFAULT_ACCOUNT_ID,
            "display_name": "Catalog Agent",
            "description": "Full source description",
            "prompts": {"system": "System", "context_template": "Context"},
            "skills": [
                {"skill_id": "base_skill", "skill_prompt": "Base prompt", "sort_order": 0},
                {"skill_id": "review_skill", "skill_prompt": "Review prompt", "sort_order": 1},
            ],
            "tools": [
                {"tool_id": "lookup_tool", "sort_order": 0},
                {"tool_id": "format_tool", "sort_order": 1},
            ],
            "inputs": {"required": [{"name": "topic", "type": "text"}], "optional": []},
            "outputs": ["summary"],
            "_inputs": [
                {"name": "topic", "type": "text", "required": True, "source": "own"},
                {
                    "name": "skill_context",
                    "type": "object",
                    "required": False,
                    "source": "skill:base_skill",
                },
            ],
            "_outputs": [
                {"name": "summary", "type": "text", "required": True, "source": "own"},
                {
                    "name": "audit",
                    "type": "object",
                    "required": False,
                    "source": "skill:review_skill",
                },
            ],
            "depends_on": {"soft": ["prior_agent"], "hard": []},
            "config": {"model": "fixture-model"},
            "quality": {"determinism": "high"},
            "builder_metadata_jsonb": {"purpose": "review"},
            "category": "marketing",
            "version": "2.3.0",
            "tags": ["catalog", "review"],
            "enabled": True,
            "source_install_id": "install-1",
        }
        db = MagicMock()
        db.get_agent.side_effect = lambda key: (
            {"agent_id": key, "account_id": DEFAULT_ACCOUNT_ID} if key == "prior_agent" else None
        )
        db.get_skill.side_effect = lambda key: {
            "skill_id": key,
            "account_id": None if key == "base_skill" else DEFAULT_ACCOUNT_ID,
        }
        db.get_tool.side_effect = lambda key: {
            "tool_id": key,
            "account_id": None if key == "lookup_tool" else DEFAULT_ACCOUNT_ID,
        }
        reload_registry = MagicMock()
        service = agent_commands.AgentCommandService(
            get_db=lambda: db, reload_registry=reload_registry
        )

        with patch.object(
            agent_commands,
            "projection_aware_catalog_row",
            side_effect=_agent_clone_projection_lookup(source),
        ):
            result = service.clone_agent(
                source["agent_id"],
                target_agent_id="catalog_agent_copy",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="operator"),
            )

        payload = db.save_agent.call_args.args[0]
        assert result["agent_id"] == "catalog_agent_copy"
        assert payload == {
            "agent_id": "catalog_agent_copy",
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope_kind": "account",
            "display_name": "Catalog Agent (Copy)",
            "description": "Full source description",
            "prompts": {
                "system": "System",
                "context_template": "Context",
                "skill_prompts": {"base_skill": "Base prompt", "review_skill": "Review prompt"},
            },
            "skills": ["base_skill", "review_skill"],
            "tools": ["lookup_tool", "format_tool"],
            "inputs": {"required": [{"name": "topic", "type": "text"}], "optional": []},
            "outputs": ["summary"],
            "structured_inputs": [
                {"name": "topic", "type": "text", "required": True, "source": "own"}
            ],
            "structured_outputs": [
                {"name": "summary", "type": "text", "required": True, "source": "own"}
            ],
            "depends_on": {"soft": ["prior_agent"], "hard": []},
            "config": {"model": "fixture-model"},
            "quality": {"determinism": "high"},
            "builder_metadata_jsonb": {"purpose": "review"},
            "category": "marketing",
            "enabled": False,
            "version": "2.3.0",
            "tags": ["catalog", "review"],
        }
        assert "source_install_id" not in payload
        reload_registry.assert_called_once()

    @pytest.mark.parametrize(
        ("projection_inputs", "projection_outputs", "anchor_inputs", "anchor_outputs"),
        [
            (
                [{"name": "current_input", "type": "json", "required": True}],
                [{"name": "current_output", "type": "text", "required": True}],
                [],
                [],
            ),
            (
                [],
                [],
                [{"name": "stale_input", "type": "text"}],
                [{"name": "stale_output", "type": "text"}],
            ),
        ],
        ids=["virtual-projection", "explicit-empty-over-stale-anchor"],
    )
    def test_clone_uses_projected_owned_io_over_durable_anchor(
        self, projection_inputs, projection_outputs, anchor_inputs, anchor_outputs
    ):
        agent_commands = self._commands()
        source = {
            "agent_id": "extension.install-1.projected_agent",
            "account_id": DEFAULT_ACCOUNT_ID,
            "source_install_id": "install-1",
            "display_name": "Projected agent",
            "structured_inputs": projection_inputs,
            "structured_outputs": projection_outputs,
            "skills": [],
            "tools": [],
        }
        db = MagicMock()
        db.get_agent.return_value = None
        db.get_agent_own_inputs.return_value = anchor_inputs
        db.get_agent_own_outputs.return_value = anchor_outputs
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())

        with patch.object(
            agent_commands,
            "projection_aware_catalog_row",
            side_effect=_agent_clone_projection_lookup(source),
        ):
            service.clone_agent(
                source["agent_id"],
                target_agent_id="manual_projection_copy",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            )

        payload = db.save_agent.call_args.args[0]
        assert payload["structured_inputs"] == projection_inputs
        assert payload["structured_outputs"] == projection_outputs

    @pytest.mark.parametrize(
        "override",
        ["researcher", "00000000-0000-0000-0000-000000000001"],
        ids=["catalog-key", "catalog-id"],
    )
    @pytest.mark.parametrize(
        "dependency_account", [None, DEFAULT_ACCOUNT_ID], ids=["global", "account"]
    )
    def test_clone_references_durable_installed_dependencies_without_transferring_ownership(
        self, dependency_account
    ):
        from copy import deepcopy

        agent_commands = self._commands()

        source = {
            "agent_id": "extension.install-1.source",
            "account_id": DEFAULT_ACCOUNT_ID,
            "source_install_id": "install-1",
            "skills": [{"skill_id": "installed_skill", "skill_prompt": "Keep prompt"}],
            "tools": [{"tool_id": "installed_tool"}],
            "depends_on": {"soft": ["installed_soft"], "hard": ["installed_hard"]},
        }
        owner = {
            "account_id": dependency_account,
            "scope_kind": "account" if dependency_account else "system",
            "source_install_id": "install-1",
            "enabled": True,
        }
        skill = {**owner, "skill_id": "installed_skill"}
        tool = {
            **owner,
            "tool_id": "installed_tool",
            "implementation": {"type": "mcp", "owner_tenant_id": "tenant-a"},
        }
        agents = {key: {**owner, "agent_id": key} for key in ("installed_soft", "installed_hard")}
        original = deepcopy([source, skill, tool, agents])
        db = MagicMock()
        db.get_agent.side_effect = agents.get
        db.get_skill.return_value = skill
        db.get_tool.return_value = tool
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())

        with patch.object(
            agent_commands,
            "projection_aware_catalog_row",
            side_effect=_agent_clone_projection_lookup(source),
        ):
            result = service.clone_agent(
                source["agent_id"],
                target_agent_id="manual_copy",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, tenant_id="tenant-a"),
            )

        assert result["agent_id"] == "manual_copy"
        payload = db.save_agent.call_args.args[0]
        assert payload["skills"] == ["installed_skill"]
        assert payload["tools"] == ["installed_tool"]
        assert payload["depends_on"] == source["depends_on"]
        assert payload["prompts"]["skill_prompts"] == {"installed_skill": "Keep prompt"}
        assert payload["account_id"] == DEFAULT_ACCOUNT_ID
        assert payload["enabled"] is False
        assert "source_install_id" not in payload
        assert [source, skill, tool, agents] == original
        db.save_skill.assert_not_called()
        db.save_tool.assert_not_called()
        db.save_agent.assert_called_once()

    def test_clone_projection_refuses_foreign_or_virtual_dependencies_before_save(self):
        agent_commands = self._commands()

        source = {
            "agent_id": "extension.install-1.catalog_agent",
            "account_id": DEFAULT_ACCOUNT_ID,
            "skills": [{"skill_id": "foreign_skill"}],
            "tools": [{"tool_id": "virtual_tool"}],
            "depends_on": {"soft": [], "hard": ["virtual_agent_dependency"]},
        }
        db = MagicMock()
        db.get_agent.return_value = None
        db.get_skill.return_value = {"skill_id": "foreign_skill", "account_id": "account-other"}
        db.get_tool.return_value = None
        reload_registry = MagicMock()
        service = agent_commands.AgentCommandService(
            get_db=lambda: db, reload_registry=reload_registry
        )

        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceConflictError) as exc,
        ):
            service.clone_agent(
                source["agent_id"],
                target_agent_id="catalog_agent_copy",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            )

        assert exc.value.payload["code"] == "agent_clone_dependencies_unavailable"
        assert exc.value.payload["skill_ids"] == ["foreign_skill"]
        assert exc.value.payload["tool_ids"] == ["virtual_tool"]
        assert exc.value.payload["agent_ids"] == ["virtual_agent_dependency"]
        db.save_agent.assert_not_called()
        reload_registry.assert_not_called()

    def test_clone_projection_hides_foreign_agent_source(self):
        agent_commands = self._commands()
        from core.application.management.errors import ServiceNotFoundError

        source = {"agent_id": "extension.install-1.foreign", "account_id": "account-other"}
        db = MagicMock()
        db.get_agent.return_value = None
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())

        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceNotFoundError),
        ):
            service.clone_agent(
                source["agent_id"],
                target_agent_id="foreign_copy",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            )

        db.save_agent.assert_not_called()

    def test_clone_does_not_reload_registry_when_store_transaction_fails(self):
        agent_commands = self._commands()

        source = {
            "agent_id": "extension.install-1.catalog_agent",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        db = MagicMock()
        db.get_agent.return_value = None
        db.save_agent.side_effect = RuntimeError("injected store failure")
        reload_registry = MagicMock()
        service = agent_commands.AgentCommandService(
            get_db=lambda: db, reload_registry=reload_registry
        )

        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceError, match="Failed to clone agent"),
        ):
            service.clone_agent(
                source["agent_id"],
                target_agent_id="catalog_agent_copy",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
            )

        reload_registry.assert_not_called()

    def test_clone_hides_source_from_disallowed_project(self):
        agent_commands = self._commands()
        from core.application.management.errors import ServiceNotFoundError

        source = {
            "agent_id": "project_source",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-b",
            "scope_kind": "project",
        }
        db = MagicMock()
        db.get_agent.return_value = None
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())

        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceNotFoundError),
        ):
            service.clone_agent(
                "project_source",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    allowed_project_ids=frozenset({"project-a"}),
                ),
            )

        db.save_agent.assert_not_called()

    def test_clone_refuses_visible_project_source_without_project_target_contract(self):
        agent_commands = self._commands()

        source = {
            "agent_id": "project_source",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-a",
            "scope_kind": "project",
        }
        db = MagicMock()
        db.get_agent.return_value = None
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())

        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceConflictError) as exc,
        ):
            service.clone_agent(
                "project_source",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    project_id="project-a",
                    allowed_project_ids=frozenset({"project-a"}),
                ),
            )

        assert exc.value.payload["code"] == "agent_clone_project_scope_unsupported"
        db.save_agent.assert_not_called()

    @pytest.mark.parametrize(
        "allowed_project",
        ["project-a", "project-b"],
        ids=["dependency-outside-allow-list", "visible-dependency-cannot-widen"],
    )
    def test_clone_refuses_same_account_dependency_from_another_project(self, allowed_project):
        agent_commands = self._commands()

        source = {
            "agent_id": "account_source",
            "account_id": DEFAULT_ACCOUNT_ID,
            "skills": [],
            "tools": [{"tool_id": "project_tool"}],
            "depends_on": {"soft": [], "hard": []},
        }
        project_tool = {
            "tool_id": "project_tool",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "project-b",
            "scope_kind": "project",
            "implementation": {"type": "internal"},
        }
        db = MagicMock()
        db.get_agent.return_value = None
        db.get_tool.return_value = project_tool
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())

        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceConflictError) as exc,
        ):
            service.clone_agent(
                "account_source",
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    allowed_project_ids=frozenset({allowed_project}),
                ),
            )

        assert exc.value.payload["code"] == "agent_clone_dependencies_unavailable"
        assert exc.value.payload["tool_ids"] == ["project_tool"]
        db.save_agent.assert_not_called()

    def test_clone_refuses_mcp_dependency_owned_by_another_tenant(self):
        agent_commands = self._commands()

        source = {
            "agent_id": "account_source",
            "account_id": DEFAULT_ACCOUNT_ID,
            "skills": [],
            "tools": [{"tool_id": "foreign_tenant_tool"}],
            "depends_on": {"soft": [], "hard": []},
        }
        foreign_tool = {
            "tool_id": "foreign_tenant_tool",
            "account_id": DEFAULT_ACCOUNT_ID,
            "implementation": {"type": "mcp", "owner_tenant_id": "tenant-b"},
        }
        db = MagicMock()
        db.get_agent.return_value = None
        db.get_tool.return_value = foreign_tool
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())

        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceConflictError) as exc,
        ):
            service.clone_agent(
                "account_source",
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, tenant_id="tenant-a"),
            )

        assert exc.value.payload["code"] == "agent_clone_dependencies_unavailable"
        assert exc.value.payload["tool_ids"] == ["foreign_tenant_tool"]
        db.save_agent.assert_not_called()

    def test_clone_refuses_connector_only_standard_tool_dependency(self):
        agent_commands = self._commands()
        source = {
            "agent_id": "source",
            "account_id": DEFAULT_ACCOUNT_ID,
            "tools": [{"tool_id": "std.numbers"}],
        }
        db = MagicMock()
        db.get_agent.return_value = None
        db.get_tool.return_value = {"tool_id": "std.numbers", "account_id": None, "enabled": True}
        service = agent_commands.AgentCommandService(get_db=lambda: db, reload_registry=MagicMock())
        with (
            patch.object(
                agent_commands,
                "projection_aware_catalog_row",
                side_effect=_agent_clone_projection_lookup(source),
            ),
            pytest.raises(ServiceConflictError) as exc,
        ):
            service.clone_agent("source", scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID))
        assert exc.value.payload["code"] == "agent_clone_dependencies_unavailable"
        assert exc.value.payload["tool_ids"] == ["std.numbers"]
        db.save_agent.assert_not_called()
