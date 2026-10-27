from core.application.workflows.workflow_entrypoint_audit_service import (
    WorkflowEntrypointAuditPolicy,
    WorkflowEntrypointAuditRejection,
    WorkflowEntrypointPreparationAuditFacts,
)
from core.application.workflows.workflow_entrypoint_auth import (
    WorkflowEntrypointAuthOutcome,
    WorkflowEntrypointAuthRejectionReason,
)


def _policy() -> WorkflowEntrypointAuditPolicy:
    return WorkflowEntrypointAuditPolicy(entry_key="invoice", now_iso=lambda: "now")


def test_audit_policy_owns_transport_auth_failure() -> None:
    record = _policy().rejection_outcome(
        WorkflowEntrypointAuditRejection(
            project_id="project-1",
            workflow_id="workflow-1",
            task_id="webhook:invoice:auth_failed",
            reason="auth_failed",
            error="Invalid bearer token",
            account_id=None,
        )
    )

    assert record is not None
    assert record.status == "rejected"


def test_audit_policy_owns_preparation_failure_reason_and_task_identity() -> None:
    record = _policy().preparation_rejection(
        WorkflowEntrypointPreparationAuditFacts(
            code="entrypoint_disabled",
            error="Workflow entrypoint is disabled",
            project_id="project-1",
            workflow_id="workflow-1",
            account_id="account-1",
        )
    )

    assert record is not None
    assert record.task_id == "webhook:invoice:disabled"
    assert record.payload["reason"] == "entrypoint_disabled"
    assert record.account_id == "account-1"


def test_audit_policy_does_not_persist_storage_preparation_failures() -> None:
    record = _policy().preparation_rejection(
        WorkflowEntrypointPreparationAuditFacts(
            code="entrypoint_storage_unavailable",
            error="Workflow entrypoint storage is unavailable",
            project_id=None,
            workflow_id="",
            account_id=None,
        )
    )

    assert record is None


def test_auth_outcome_exposes_semantic_reason_without_http_status() -> None:
    outcome = WorkflowEntrypointAuthOutcome.rejected(
        error="Webhook credential is not configured",
        reason=WorkflowEntrypointAuthRejectionReason.CREDENTIAL_NOT_CONFIGURED,
    )

    assert outcome.reason is WorkflowEntrypointAuthRejectionReason.CREDENTIAL_NOT_CONFIGURED
    assert not hasattr(outcome, "status_code")
