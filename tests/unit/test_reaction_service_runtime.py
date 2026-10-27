from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext
from core.application.workflows.reaction_service_runtime import (
    build_worker_inbound_reaction_evaluator,
    resume_job_via_approval_command_service,
)


@pytest.mark.parametrize(
    ("conflict_code", "outcome_code"),
    [
        ("stale_approval_gate", "stale"),
        ("approval_already_resolved", "already_handled"),
        ("approval_action_not_allowed", "not_allowed"),
    ],
)
def test_worker_approval_adapter_returns_terminal_machine_outcome(
    conflict_code: str, outcome_code: str
) -> None:
    job_store = MagicMock()
    job_store.get_job.return_value = {"id": "job-1", "thread_id": "thread-1"}
    approval_service = MagicMock()
    approval_service.resume_task.side_effect = ServiceConflictError(
        "Approval conflict",
        payload={"code": conflict_code},
    )

    result = resume_job_via_approval_command_service(
        "job-1",
        "",
        approved=True,
        action="approve",
        expected_approval_id="gate-1",
        scope=ScopeContext(account_id="acc-1"),
        idempotency_key="decision-1",
        get_job_store=lambda: job_store,
        approval_command_service_factory=lambda: approval_service,
    )

    assert result == {
        "status": "ignored",
        "reason": conflict_code,
        "outcome_code": outcome_code,
    }


def test_worker_default_abort_uses_canonical_workflow_resume_path() -> None:
    resume = MagicMock(
        return_value={
            "status": "resumed",
            "job_status": "resume_pending",
        }
    )
    evaluator = build_worker_inbound_reaction_evaluator(
        to_json_obj=lambda value: value if isinstance(value, dict) else {},
        resume_job_from_reaction=resume,
        logger=MagicMock(),
    )
    store = MagicMock()
    store.list_workflow_reactions.return_value = []
    scope = ScopeContext(account_id="acc-1", actor_id="chat:alice")

    results = evaluator(
        store,
        {
            "event_type": "notification.interaction_received",
            "job_id": "job-1",
            "workflow_id": "weekly_report",
            "correlation_id": "chat-abort-1",
            "payload": {
                "interaction_kind": "approval_decision",
                "approval_id": "gate-1",
                "approved": False,
                "action": "abort",
                "comment": "Stop this run",
            },
        },
        scope=scope,
    )

    assert results[0]["status"] == "resumed"
    assert results[0]["executed"] is True
    assert resume.call_args.args == ("job-1", "Stop this run")
    assert resume.call_args.kwargs == {
        "approved": False,
        "action": "abort",
        "expected_approval_id": "gate-1",
        "scope": scope,
        "idempotency_key": "chat-abort-1",
    }


def test_worker_approval_adapter_preserves_success_status() -> None:
    job_store = MagicMock()
    job_store.get_job.return_value = {"id": "job-1", "thread_id": "thread-1"}
    approval_service = MagicMock()
    approval_service.resume_task.return_value = SimpleNamespace(status="resume_pending")

    result = resume_job_via_approval_command_service(
        "job-1",
        "Stop this run",
        approved=False,
        action="abort",
        expected_approval_id="gate-1",
        scope=ScopeContext(account_id="acc-1"),
        idempotency_key="decision-abort",
        get_job_store=lambda: job_store,
        approval_command_service_factory=lambda: approval_service,
    )

    assert result["status"] == "resumed"
    assert result["job_status"] == "resume_pending"
    decision = approval_service.resume_task.call_args.kwargs["decision"]
    assert decision.action == "abort"
    assert decision.approved is False
