from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.approval_decision_service import ApprovalDecisionService
from core.application.management.approval_repository import ApprovalRepository
from core.application.management.command_models_api_inputs import ApprovalDecisionInput
from core.application.management.errors import ServiceConflictError
from core.application.management.query_services_approval import ApprovalQueryService
from core.application.management.scope import ScopeContext


def _approval_service() -> ApprovalDecisionService:
    return ApprovalDecisionService(
        lock_job_row=lambda *_args: None,
        current_interrupt_payload=lambda *_args: {},
        persist_decision=lambda **_kwargs: None,
    )


def test_gate_surface_defaults_actions_when_identity_already_exists() -> None:
    store = MagicMock()
    store.get_job.return_value = {
        "id": "job-1",
        "status": "awaiting_approval",
        "thread_id": "thread-1",
        "task_summary": "Review",
        "project_id": "project-1",
    }
    store.get_project.return_value = {
        "id": "project-1",
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    state = MagicMock()
    state.tasks = [
        MagicMock(
            interrupts=[
                MagicMock(
                    id="interrupt-1",
                    value={"approval_id": "gate-1", "allowed_actions": []},
                )
            ]
        )
    ]
    service = ApprovalQueryService(
        get_logger=lambda: store,
        get_thread_state_snapshot=lambda _thread_id: state,
        approval_fallback_payload=lambda _job: {},
    )

    gate = service.get_current_gate("job-1").dump()

    assert gate["approval_id"] == "gate-1"
    assert gate["allowed_actions"] == ["approve", "reject"]
    assert gate["payload"]["allowed_actions"] == ["approve", "reject"]


@pytest.mark.parametrize("raw_allowed", [None, []])
def test_empty_or_absent_allow_list_rejects_unknown_action(raw_allowed) -> None:
    payload = {"approval_id": "gate-1"}
    if raw_allowed is not None:
        payload["allowed_actions"] = raw_allowed

    _raises_callable_67_1 = _approval_service().validate_current_gate
    _raises_input_67_2 = ApprovalDecisionInput(
        approved=True, action="override", approval_id="gate-1"
    )
    with pytest.raises(ServiceConflictError) as exc_info:
        _raises_callable_67_1(
            row={"status": "awaiting_approval"},
            payload=payload,
            decision=_raises_input_67_2,
            token_principal=False,
        )

    assert exc_info.value.payload == {
        "error": "Approval action is not allowed by the current gate",
        "code": "approval_action_not_allowed",
        "allowed_actions": ["approve", "reject"],
    }


@pytest.mark.parametrize(
    ("approved", "action"),
    [(True, "approve"), (False, "reject")],
)
def test_safe_default_keeps_approve_and_reject_rework_available(approved, action) -> None:
    approval_id = _approval_service().validate_current_gate(
        row={"status": "awaiting_approval"},
        payload={"approval_id": "gate-1", "allowed_actions": []},
        decision=ApprovalDecisionInput(
            approved=approved,
            action=action,
            approval_id="gate-1",
        ),
        token_principal=False,
    )

    assert approval_id == "gate-1"


def test_exhausted_gate_rejects_another_rework_but_allows_abort() -> None:
    service = _approval_service()
    payload = {
        "approval_id": "gate-1",
        "allowed_actions": ["approve", "abort"],
        "rework_budget_exhausted": True,
    }

    _raises_input_113_1 = ApprovalDecisionInput(
        approved=False, action="reject", approval_id="gate-1", comment="one more time"
    )
    with pytest.raises(ServiceConflictError) as exc_info:
        service.validate_current_gate(
            row={"status": "awaiting_approval"},
            payload=payload,
            decision=_raises_input_113_1,
            token_principal=False,
        )

    assert exc_info.value.payload["code"] == "approval_action_not_allowed"
    assert (
        service.validate_current_gate(
            row={"status": "awaiting_approval"},
            payload=payload,
            decision=ApprovalDecisionInput(
                approved=False,
                action="abort",
                approval_id="gate-1",
                comment="cancel at exhaustion",
            ),
            token_principal=False,
        )
        == "gate-1"
    )


def test_rejection_history_records_the_resulting_retry_ordinal() -> None:
    entry = ApprovalRepository._history_entry(
        approval_id="gate-2",
        payload={"stage": "Review", "rework_retry_attempt": 1},
        decision=ApprovalDecisionInput(
            approved=False,
            action="reject",
            approval_id="gate-2",
            comment="revise",
        ),
        scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="operator"),
    )

    assert entry["retry_ordinal"] == 2
