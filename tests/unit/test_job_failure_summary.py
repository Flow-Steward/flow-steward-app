from __future__ import annotations

from core.application.jobs.failure_summary import build_job_failure_summary


def test_failure_summary_classifies_provider_rate_limit_and_preserves_safe_ids() -> None:
    summary = build_job_failure_summary(
        job={
            "id": 42,
            "project_id": 7,
            "status": "failed",
            "task_summary": "Run model call",
            "meta": {"trace_id": "job:42"},
        },
        runtime={
            "active_phase": "implementation",
            "active_agent": "writer",
            "last_structured_error": {
                "id": 9,
                "source": "provider",
                "category": "provider",
                "provider_name": "openai",
                "http_status": 429,
                "provider_error_code": "rate_limit",
                "provider_error_type": "rate_limit_error",
                "request_id": "req_abc-123",
                "retryable": True,
                "error_message": "Rate limit exceeded for request req_abc-123",
            },
            "last_message": {"correlation_id": "corr-safe-1"},
            "selected_workflow_id": "wf.obs",
        },
    )

    assert summary["present"] is True
    assert summary["kind"] == "provider_rate_limit"
    assert summary["retryable"] is True
    assert summary["root_cause"]
    assert "provider" in summary["next_action"].lower()
    assert summary["remediation"]["steps"]
    assert "runtime.last_structured_error.http_status" in summary["evidence_refs"]
    assert summary["raw_diagnostics_available"] is True
    assert {"workflow": "wf.obs", "source": "runtime"} in summary["related_workflows"]
    assert summary["diagnostic_ids"]["request_id"] == "req_abc-123"
    assert summary["diagnostic_ids"]["correlation_id"] == "corr-safe-1"
    assert summary["diagnostic_ids"]["trace_id"] == "job:42"
    assert any(
        row["label"] == "HTTP status" and row["value"] == "429" for row in summary["evidence"]
    )


def test_failure_summary_redacts_secret_like_diagnostic_ids_and_messages() -> None:
    summary = build_job_failure_summary(
        job={
            "id": 43,
            "project_id": 7,
            "status": "failed",
            "last_error": "db=postgres://admin:secret@db/app failed",
        },
        runtime={
            "last_structured_error": {
                "source": "system",
                "category": "unknown",
                "request_id": "postgres://admin:secret@db/app",
                "provider_error_code": "api_key=abcdefghijklmnopqrstuvwxyz",
                "error_message": "db=postgres://admin:secret@db/app failed",
            },
        },
    )

    assert "postgres://" not in summary["detail"]
    assert "postgres://" not in summary["diagnostic_ids"]["request_id"]
    assert summary["diagnostic_ids"]["provider_error_code"] != "api_key=abcdefghijklmnopqrstuvwxyz"


def test_failure_summary_is_absent_without_failure_signal() -> None:
    summary = build_job_failure_summary(
        job={"id": 44, "status": "in_progress", "task_summary": "Still running"},
        runtime={"active_phase": "implementation"},
    )

    assert summary["present"] is False
    assert summary["kind"] == "none"
    assert summary["next_action"] == ""
    assert summary["raw_diagnostics_available"] is False


def test_failure_summary_ignores_stale_gate_reason_after_completion() -> None:
    summary = build_job_failure_summary(
        job={"id": 450, "status": "completed", "task_summary": "Approved release gate"},
        runtime={
            "active_phase": "end",
            "last_failure_class": "unknown",
            "last_retry_reason": "Phase requires human approval",
            "last_blocking_reason": "Phase requires human approval",
        },
    )

    assert summary["present"] is False
    assert summary["kind"] == "none"
    assert summary["status"] == "completed"
    assert summary["detail"] == ""
    assert summary["next_action"] == ""


def test_failure_summary_uses_blocking_reason_while_awaiting_approval() -> None:
    summary = build_job_failure_summary(
        job={"id": 45, "status": "awaiting_approval", "task_summary": "Review runtime fix"},
        runtime={
            "active_phase": "quality",
            "active_agent": "checker",
            "last_failure_class": "quality",
            "last_blocking_reason": "Quality gate blocked this workflow.",
        },
    )

    assert summary["present"] is True
    assert summary["title"] != "No failure recorded"
    assert summary["kind"] == "workflow_guardrail"
    assert "Quality gate blocked" in summary["detail"]
    assert "runtime.last_blocking_reason" in summary["evidence_refs"]


def test_failure_summary_uses_blocked_check_message_while_awaiting_approval() -> None:
    summary = build_job_failure_summary(
        job={"id": 49, "status": "awaiting_approval", "task_summary": "Review validation gate"},
        runtime={
            "active_phase": "quality",
            "active_agent": "checker",
            "last_message": {
                "from_agent": "checker",
                "status": "blocked",
                "summary": "Validation could not start.",
                "artifacts": {
                    "verdict": "blocked",
                    "checks_run": [
                        {
                            "check": "required_validation",
                            "label": "Required validation",
                            "passed": False,
                            "status": "runtime_infrastructure_failed",
                            "stderr": "bwrap: No permissions to create a new namespace",
                        }
                    ],
                },
            },
        },
    )

    assert summary["present"] is True
    assert summary["kind"] == "platform_blocker"
    assert summary["title"] == "Flow Steward blocker"
    assert "bwrap" in summary["detail"]


def test_failure_summary_reads_blocked_checks_from_mapping() -> None:
    summary = build_job_failure_summary(
        job={"id": 50, "status": "awaiting_approval", "task_summary": "Review validation gate"},
        runtime={
            "active_phase": "quality",
            "active_agent": "checker",
            "last_message": {
                "from_agent": "checker",
                "status": "blocked",
                "summary": "Validation could not start.",
                "artifacts": {
                    "verdict": "blocked",
                    "checks_run": {
                        "required_validation": {
                            "passed": False,
                            "status": "runtime_infrastructure_failed",
                            "stderr": "bwrap: No permissions to create a new namespace",
                        }
                    },
                },
            },
        },
    )

    assert summary["present"] is True
    assert summary["kind"] == "platform_blocker"
    assert "bwrap" in summary["detail"]


def test_failure_summary_classifies_approval_rejected_feedback() -> None:
    summary = build_job_failure_summary(
        job={"id": 46, "status": "awaiting_approval", "task_summary": "Revise output"},
        runtime={
            "active_phase": "development",
            "active_agent": "human_approval_gate",
            "last_failure_class": "approval_rejected",
            "last_retry_reason": "Please address the review feedback before publishing.",
        },
    )

    assert summary["present"] is True
    assert summary["kind"] == "approval_rejected"
    assert summary["title"] == "Approval rejected"
    assert "human reviewer rejected" in summary["root_cause"].lower()
    assert "terminal failure" not in summary["root_cause"].lower()


def test_approval_rejected_classification_wins_over_feedback_keywords() -> None:
    for feedback in (
        "Please fix missing MCP tool evidence before approval.",
        "Tool policy regression must be addressed before approval.",
    ):
        summary = build_job_failure_summary(
            job={"id": 47, "status": "awaiting_approval", "task_summary": "Revise output"},
            runtime={
                "active_phase": "development",
                "active_agent": "human_approval_gate",
                "last_failure_class": "approval_rejected",
                "last_retry_reason": feedback,
            },
        )

        assert summary["kind"] == "approval_rejected"
        assert summary["title"] == "Approval rejected"


def test_failure_summary_classifies_platform_blocker() -> None:
    summary = build_job_failure_summary(
        job={"id": 48, "status": "awaiting_approval", "task_summary": "Review blocked validation"},
        runtime={
            "active_phase": "quality",
            "active_agent": "checker",
            "last_failure_class": "platform_blocker",
            "last_retry_reason": "Sandbox blocked verification before delivery.",
        },
    )

    assert summary["present"] is True
    assert summary["kind"] == "platform_blocker"
    assert summary["title"] == "Flow Steward blocker"
    assert "not automatically a task-output defect" in summary["root_cause"]


def test_unavailable_model_is_not_reported_as_a_guardrail_failure() -> None:
    """AC17-R70: the job page blamed a guardrail for a missing model.

    A step that cannot obtain a model produces no valid output, so the validation node
    records `last_failure_class="contract"` and the guardrail branch claimed the failure.
    The page then showed "Workflow guardrail failure" with an empty evidence table — there is
    no guardrail and no evidence — and offered "review the guardrail evidence and update the
    workflow inputs", which cannot be carried out and would not help if it could. The
    downstream symptom outranked the upstream cause.
    """
    summary = build_job_failure_summary(
        job={"id": 77, "project_id": 3, "status": "failed", "task_summary": "Run agent"},
        runtime={
            "active_phase": "analysis",
            "active_agent": "researcher",
            "last_failure_class": "contract",
            "last_structured_error": {
                "error_message": (
                    "model_unavailable: saved Assistant route 'openai/gpt-x' is unavailable"
                ),
            },
        },
    )

    assert summary["kind"] == "model_unavailable"
    assert summary["title"] == "No model available for this agent"
    assert "guardrail" not in summary["next_action"].lower()
    # the remediation must name the thing the operator can actually change
    action = summary["next_action"].lower()
    assert "model" in action or "route" in action
    assert summary["remediation"]["steps"]


def test_a_real_contract_failure_is_still_a_guardrail_failure() -> None:
    """The new branch must not swallow the case it sits in front of."""
    summary = build_job_failure_summary(
        job={"id": 78, "project_id": 3, "status": "failed", "task_summary": "Run agent"},
        runtime={
            "active_phase": "analysis",
            "active_agent": "researcher",
            "last_failure_class": "contract",
            "last_structured_error": {
                "category": "guardrail",
                "error_message": "required output 'summary_doc' was missing",
            },
        },
    )

    assert summary["kind"] == "workflow_guardrail"
    assert summary["title"] == "Workflow guardrail failure"


def test_a_designed_stop_reads_as_the_workflow_ending_the_run() -> None:
    """A stop_fail step read as "Workflow guardrail failure" without its message."""
    summary = build_job_failure_summary(
        job={
            "id": 79,
            "project_id": 3,
            "status": "failed",
            "error": "Stopped by the workflow: Orders over 1000 need manual review",
        },
        runtime={"last_failure_class": "contract"},
    )

    assert summary["kind"] == "workflow_stopped"
    assert summary["title"] == "Stopped by the workflow"
    assert "Orders over 1000 need manual review" in summary["detail"]
