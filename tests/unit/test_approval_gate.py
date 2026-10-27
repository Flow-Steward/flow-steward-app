"""Unit tests for the human approval node (``_registry_approval_gate_node``).

The node serves the pauses of the active step execution only:

- a recovery approval the hub opened (``recovery_pending``: failure, exhausted
  rework budget, autonomy policy);
- an authored Approval step, whose decision becomes that execution's step
  message with an ``approved``/``rejected`` outcome;
- test runs, which pause by state and resume from ``user_approved``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from langgraph.types import Command

from core.application.orchestration.active_step import active_step, message_matches_execution
from core.application.orchestration.agent_messages import build_agent_message
from core.application.orchestration.graph.builder_approval_gate import (
    _registry_approval_gate_node,
    approval_id_for_execution,
)
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION, RunCursor, begin_step
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.workflows.workflow_definition_snapshot import definition_digest
from tests._workflow_definitions import edge, node


@pytest.fixture(autouse=True)
def _isolated_definitions():
    clear_run_definition_cache()
    yield
    clear_run_definition_cache()


def _definition(approval: dict | None = None) -> dict:
    return {
        "steps": [
            {"step_id": "draft", "step_kind": "transform", "display_name": "Draft"},
            {
                "step_id": "review",
                "step_kind": "approval",
                "display_name": "Final Delivery Review",
                "override": {"approval": dict(approval or {})},
            },
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "execution_graph": {
            "schema_version": 1,
            "entry": node("draft"),
            "edges": [
                edge("draft", node("review")),
                edge("review", node("done"), "approved"),
                edge("review", node("draft"), "rejected", rework=True),
            ],
        },
        "settings": {},
    }


def _message(step_id: str, *, execution_id: str | None = None, **artifacts) -> dict:
    return build_agent_message(
        from_agent="writer",
        to_agent="orchestrator",
        skill="write",
        status="pass",
        correlation_id=uuid.uuid4().hex,
        artifacts=artifacts,
        summary=f"{step_id} result",
        published_at=datetime.now(UTC).isoformat(),
        step_id=step_id,
        step_execution_id=execution_id or str(uuid.uuid4()),
        attempt=1,
    )


def make_state(
    *,
    step_id: str = "review",
    approval: dict | None = None,
    pending: dict | None = None,
    **overrides,
) -> dict:
    definition = _definition(approval)
    ref = {
        "snapshot_id": f"test:{uuid.uuid4()}",
        "revision": 1,
        "digest": definition_digest(definition),
    }
    prime_run_definition(ref, definition)
    cursor = begin_step(RunCursor(), step_id)
    state = {
        "task_id": "test-001",
        "task_summary": "Fix broken login",
        "status": "in_progress",
        "workflow_id": "wf",
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "run_cursor": cursor.dump(),
        "current_step_id": step_id,
        "needs_user_approval": True,
        "user_approved": None,
        "review_feedback": None,
        "agent_messages": [],
    }
    if pending is not None:
        state["recovery_pending"] = {
            "request_id": "req-1",
            "step_id": step_id,
            "step_execution_id": cursor.step_execution_id,
            "attempt": cursor.attempt,
            "detail": {},
            **pending,
        }
    state.update(overrides)
    return state


def call_gate(state: dict, response):
    with patch("langgraph.types.interrupt", return_value=response) as mock_interrupt:
        result = _registry_approval_gate_node(state)
    return result, mock_interrupt


def _payload(mock_interrupt) -> dict:
    return mock_interrupt.call_args.args[0]


# ---------------------------------------------------------------------------
# Authored Approval step
# ---------------------------------------------------------------------------


class TestAuthoredApprovalPayload:
    def test_interrupt_carries_step_identity_and_no_phase_scope(self):
        state = make_state(approval={"message": "Review output."})
        _result, mock_interrupt = call_gate(state, {"approved": True})

        payload = _payload(mock_interrupt)
        cursor = state["run_cursor"]
        assert payload["step_id"] == "review"
        assert payload["step_execution_id"] == cursor["step_execution_id"]
        assert payload["attempt"] == 1
        assert payload["allowed_actions"] == ["approve", "reject"]
        assert payload["gate_kind"] == "approval"
        assert payload["message"] == "Review output."
        assert payload["stage"] == "Final Delivery Review"
        assert "phase_id" not in payload
        assert payload["approval_id"] == str(
            uuid.uuid5(uuid.NAMESPACE_URL, cursor["step_execution_id"])
        )

    def test_default_message_when_none_is_authored(self):
        _result, mock_interrupt = call_gate(make_state(), {"approved": True})
        assert (
            _payload(mock_interrupt)["message"]
            == "Please review and approve this step before proceeding."
        )

    def test_approval_id_is_stable_per_entry_and_new_for_a_new_entry(self):
        state = make_state()
        _r, first = call_gate(state, {"approved": True})
        _r, again = call_gate(state, {"approved": True})
        assert _payload(first)["approval_id"] == _payload(again)["approval_id"]

        reentered = dict(state)
        reentered["run_cursor"] = begin_step(RunCursor(), "review").dump()
        _r, new_entry = call_gate(reentered, {"approved": True})
        assert _payload(new_entry)["approval_id"] != _payload(first)["approval_id"]
        assert _payload(new_entry)["approval_id"] == approval_id_for_execution(
            reentered["run_cursor"]["step_execution_id"]
        )

    def test_show_outputs_reads_state_value(self):
        summary = "# Summary\nThis is the full Summary content"
        state = make_state(approval={"show_outputs": ["summary_doc"]}, summary_doc=summary)
        _result, mock_interrupt = call_gate(state, {"approved": True})
        payload = _payload(mock_interrupt)
        assert payload["content"] == summary
        assert payload["gate_meta"] == {"show_outputs": ["summary_doc"]}

    @pytest.mark.parametrize(
        "selector",
        [
            "external_effect_resolution_ref",
            "state.external_effect_resolution_ref",
            "state.external_effect_resolution_ref.effect_id",
        ],
    )
    def test_show_outputs_excludes_protected_effect_association(self, selector):
        private = "private-effect-association-sentinel"
        state = make_state(
            approval={"show_outputs": [selector, "summary_doc"]},
            summary_doc="Public business summary",
            external_effect_resolution_ref={
                "phase": "association",
                "effect_id": private,
                "definition_ref": {"snapshot_id": private},
            },
        )
        # Capture the actual public interrupt before any decision is processed.
        with (
            patch(
                "langgraph.types.interrupt",
                side_effect=RuntimeError("captured-public-interrupt"),
            ) as interrupted,
            pytest.raises(RuntimeError, match="captured-public-interrupt"),
        ):
            _registry_approval_gate_node(state)
        payload = interrupted.call_args.args[0]
        assert payload["content"] == "Public business summary"
        assert payload["gate_meta"] == {"show_outputs": ["summary_doc"]}
        assert private not in str(payload)

    def test_show_outputs_reads_message_selector(self):
        state = make_state(
            approval={"show_outputs": ["message.step.draft.artifacts.risk_review"]},
            agent_messages=[_message("draft", risk_review="Review complete.")],
        )
        _result, mock_interrupt = call_gate(state, {"approved": True})
        assert _payload(mock_interrupt)["content"] == "Review complete."

    def test_show_outputs_reads_latest_result_artifact(self):
        state = make_state(
            approval={"show_outputs": ["draft"]},
            agent_messages=[
                _message(
                    "draft",
                    raw_output='{"artifacts": {"DRAFT": "# Summary\\nDetailed requirements"}}',
                )
            ],
        )
        _result, mock_interrupt = call_gate(state, {"approved": True})
        assert _payload(mock_interrupt)["content"] == "# Summary\nDetailed requirements"

    def test_content_falls_back_to_task_summary(self):
        state = make_state(approval={"show_outputs": ["architecture"]}, task_summary="Add OAuth")
        _result, mock_interrupt = call_gate(state, {"approved": True})
        assert _payload(mock_interrupt)["content"] == "Add OAuth"

    def test_gate_without_recovery_or_approval_step_fails_fast(self):
        state = make_state(step_id="draft")
        with pytest.raises(ValueError, match="recovery request or an active Approval step"):
            _registry_approval_gate_node(state)


class TestAuthoredApprovalDecision:
    def test_approved_appends_one_step_message_with_approved_outcome(self):
        state = make_state(agent_messages=[_message("draft")])
        result, _ = call_gate(state, {"approved": True, "comment": "Looks good."})

        assert isinstance(result, Command)
        assert result.goto == "orchestrator"
        update = result.update
        messages = update["agent_messages"]
        assert len(messages) == 2
        decision = messages[-1]
        assert decision["from_agent"] == "human_approval_gate"
        assert decision["status"] == "pass"
        assert decision["artifacts"]["decision"] == "approved"
        assert decision["artifacts"]["feedback"] == "Looks good."
        assert decision["artifacts"]["_outcome"] == {"name": "approved"}
        assert decision["artifacts"]["approval_id"] == approval_id_for_execution(
            state["run_cursor"]["step_execution_id"]
        )
        current = active_step(state)
        assert current is not None
        assert message_matches_execution(decision, current)
        assert update["user_approved"] is None
        assert update["status"] == "in_progress"
        assert update["needs_user_approval"] is False
        assert update["review_feedback"] == "Looks good."
        assert update["reviewer_comments"] == "Looks good."
        assert update["risk_notes"] == "Looks good."

    def test_approved_without_comment_materializes_review_feedback(self):
        result, _ = call_gate(make_state(), {"approved": True, "comment": ""})
        assert result.update["review_feedback"] == "Approved."
        assert result.update["reviewer_comments"] == "Approved."
        assert result.update["risk_notes"] == "Approved without additional risk notes."
        assert result.update["agent_messages"][-1]["artifacts"]["feedback"] == "Approved."

    def test_rejected_reports_rejected_outcome_and_keeps_feedback(self):
        result, _ = call_gate(make_state(), {"approved": False, "comment": "Add user stories"})

        update = result.update
        decision = update["agent_messages"][-1]
        assert result.goto == "orchestrator"
        assert decision["artifacts"]["decision"] == "rejected"
        assert decision["artifacts"]["_outcome"] == {"name": "rejected"}
        assert decision["artifacts"]["feedback"] == "Add user stories"
        assert update["review_feedback"] == "Add user stories"
        assert update["approval_context"] is None
        assert update["last_failure_class"] == "approval_rejected"
        # No user_approved-driven retry router: the graph edge decides.
        assert update["user_approved"] is None

    def test_rejected_without_comment_gets_default_feedback(self):
        result, _ = call_gate(make_state(), {"approved": False, "comment": ""})
        assert result.update["review_feedback"] == "Please revise and resubmit for approval."
        assert (
            result.update["agent_messages"][-1]["artifacts"]["feedback"]
            == "Please revise and resubmit for approval."
        )

    def test_boolean_responses(self):
        approved, _ = call_gate(make_state(), True)
        rejected, _ = call_gate(make_state(), False)
        assert approved.update["agent_messages"][-1]["artifacts"]["decision"] == "approved"
        assert rejected.update["agent_messages"][-1]["artifacts"]["decision"] == "rejected"

    @pytest.mark.parametrize("action", ["skip", "override"])
    def test_skip_and_override_count_as_approval(self, action):
        result, _ = call_gate(make_state(), {"action": action, "comment": "Known issue is fine."})
        assert result.update["agent_messages"][-1]["artifacts"]["decision"] == "approved"
        assert result.update["review_feedback"] == "Known issue is fine."

    def test_abort_cancels_the_run(self):
        result, _ = call_gate(make_state(), {"action": "abort", "comment": "Stop."})
        assert result.goto == "orchestrator"
        assert result.update["status"] == "cancelled"
        assert result.update["review_feedback"] == "Stop."
        assert "agent_messages" not in result.update

    def test_override_writes_declared_targets_only_when_approved(self):
        approval = {"response_mapping": {"override_content_targets": ["architecture", "outline"]}}
        override = "# Operator Architecture\n\nPrefer queue-backed processing."

        approved, _ = call_gate(
            make_state(approval=approval, architecture="# Original"),
            {"action": "override", "override_content": override},
        )
        assert approved.update["architecture"] == override
        assert approved.update["outline"] == override

        rejected, _ = call_gate(
            make_state(approval=approval, architecture="# Original"),
            {"approved": False, "override_content": override},
        )
        assert "architecture" not in rejected.update

    def test_output_mapping_copies_the_decision_to_state(self):
        approval = {
            "output_mapping": {
                "decision": "state.review.decision",
                "feedback": "state.review.comment",
            }
        }
        result, _ = call_gate(
            make_state(approval=approval), {"approved": False, "comment": "Fix the totals."}
        )

        assert result.update["review"] == {"decision": "rejected", "comment": "Fix the totals."}
        decision = result.update["agent_messages"][-1]["artifacts"]
        assert decision["mapped_outputs"]["decision"] == "rejected"
        # The mapping never changes the route: the outcome is the decision.
        assert decision["_outcome"] == {"name": "rejected"}

    def test_without_output_mapping_the_result_stays_on_the_step_message(self):
        result, _ = call_gate(make_state(), {"approved": True})

        decision = result.update["agent_messages"][-1]["artifacts"]
        assert decision["decision"] == "approved"
        assert decision["approved"] is True
        assert "mapped_outputs" not in decision
        assert "review" not in result.update

    def test_output_mapping_cannot_write_host_runtime_state(self):
        approval = {"output_mapping": {"decision": "state.trusted_runtime_context"}}
        result, _ = call_gate(make_state(approval=approval), {"approved": True})

        assert "trusted_runtime_context" not in result.update
        decision = result.update["agent_messages"][-1]["artifacts"]
        assert decision["output_mapping_errors"][0]["code"] == "output_mapping_reserved_target"

    def test_override_without_declared_mapping_writes_no_state(self):
        result, _ = call_gate(
            make_state(summary_doc="# Original"),
            {"action": "override", "override_content": "# Operator version"},
        )
        assert "summary_doc" not in result.update
        assert "initial_requirements" not in result.update

    @pytest.mark.parametrize(
        "reserved_target",
        ["test_mode", "trusted_runtime_context", "_trusted_runtime_context", "project_id"],
    )
    def test_override_content_cannot_replace_host_runtime_state(self, reserved_target):
        state = make_state(
            approval={"response_mapping": {"override_content_targets": [reserved_target]}},
            project_id="project-9",
            test_mode=False,
            trusted_runtime_context={"project_id": "project-9", "test_mode": False},
            _trusted_runtime_context={"project_id": "project-9", "test_mode": False},
        )
        result, _ = call_gate(
            state, {"action": "override", "override_content": "attacker-controlled"}
        )
        assert reserved_target not in result.update

    def test_invoked_authored_approval_requires_a_human_even_with_legacy_auto_flag(self):
        result, mock_interrupt = call_gate(
            make_state(invoke_child_auto_approve=True), {"approved": False}
        )
        assert mock_interrupt.call_count == 1
        assert result.goto == "orchestrator"
        assert result.update["agent_messages"][-1]["artifacts"]["decision"] == "rejected"


class TestApprovalTestRuns:
    @pytest.mark.parametrize("task_source", ["workflow_test_run", "step_test_run"])
    def test_pauses_to_validation_with_authored_message(self, task_source):
        state = make_state(
            task_source=task_source,
            approval={"message": "Review the generated content before continuing."},
        )
        result, mock_interrupt = call_gate(state, {"approved": True})

        assert mock_interrupt.call_count == 0
        assert result.goto == "orchestrator_validation"
        assert result.update["status"] == "awaiting_approval"
        assert result.update["needs_user_approval"] is True
        assert result.update["user_approved"] is None
        assert (
            result.update["approval_context"] == "Review the generated content before continuing."
        )

    def test_reuses_state_based_test_run_approval(self):
        state = make_state(
            task_source="workflow_test_run",
            user_approved=True,
            review_feedback="Looks good.",
            reviewer_comments="Looks good.",
            risk_notes="Looks good.",
        )
        result, mock_interrupt = call_gate(state, None)

        assert mock_interrupt.call_count == 0
        assert result.goto == "orchestrator"
        update = result.update
        assert update["status"] == "in_progress"
        assert update["needs_user_approval"] is False
        assert update["approval_context"] is None
        assert update["review_feedback"] == "Looks good."
        assert update["reviewer_comments"] == "Looks good."
        assert update["risk_notes"] == "Looks good."
        assert update["agent_messages"][-1]["artifacts"]["decision"] == "approved"

    def test_state_based_rejection_reports_rejected_outcome(self):
        state = make_state(task_source="step_test_run", user_approved=False, review_feedback="")
        result, _ = call_gate(state, None)
        decision = result.update["agent_messages"][-1]
        assert decision["artifacts"]["_outcome"] == {"name": "rejected"}
        assert decision["artifacts"]["feedback"] == "Please revise and resubmit for approval."


# ---------------------------------------------------------------------------
# Recovery approvals
# ---------------------------------------------------------------------------


class TestRecoveryApproval:
    def test_failure_recovery_payload_is_scoped_to_the_execution(self):
        state = make_state(
            step_id="draft",
            pending={
                "kind": "failure",
                "reason": "writer returned fail after 2 retries: provider outage",
                "allowed_actions": ["approve", "reject"],
            },
        )
        _result, mock_interrupt = call_gate(state, {"approved": True})

        payload = _payload(mock_interrupt)
        assert payload["request_id"] == "req-1"
        assert payload["step_id"] == "draft"
        assert payload["step_execution_id"] == state["run_cursor"]["step_execution_id"]
        assert payload["allowed_actions"] == ["approve", "reject"]
        assert payload["recovery_kind"] == "failure"
        assert payload["stage"] == "Draft"
        assert payload["content"].startswith("writer returned fail")
        assert "phase_id" not in payload
        assert "approval_id" not in payload

    def test_failure_recovery_decisions_set_user_approved_for_the_hub(self):
        pending = {"kind": "failure", "reason": "boom", "allowed_actions": ["approve", "reject"]}
        approved, _ = call_gate(
            make_state(step_id="draft", pending=pending), {"approved": True, "comment": "Proceed"}
        )
        assert approved.goto == "orchestrator"
        assert approved.update["user_approved"] is True
        assert approved.update["review_feedback"] == "Proceed"
        assert "agent_messages" not in approved.update

        rejected, _ = call_gate(
            make_state(step_id="draft", pending=pending), {"approved": False, "comment": ""}
        )
        assert rejected.goto == "orchestrator"
        assert rejected.update["user_approved"] is False
        assert rejected.update["review_feedback"] == "Please revise and resubmit for approval."
        assert rejected.update["last_failure_class"] == "approval_rejected"

    def test_rework_budget_offers_approve_or_abort_and_ignores_reject(self):
        state = make_state(
            pending={
                "kind": "rework_budget",
                "reason": "Rework budget exhausted (run) for the edge from 'review'",
                "allowed_actions": ["approve", "abort"],
                "detail": {
                    "blocked_edge": ["step", "review", "rejected"],
                    "exhausted": ["run"],
                    "rework": {"used": 50, "allowance": 50, "edges": {}},
                },
            }
        )
        with patch(
            "langgraph.types.interrupt",
            side_effect=[{"approved": False, "action": "reject"}, {"approved": True}],
        ) as mock_interrupt:
            result = _registry_approval_gate_node(state)

        assert mock_interrupt.call_count == 2
        payload = _payload(mock_interrupt)
        assert payload["allowed_actions"] == ["approve", "abort"]
        assert payload["rework_budget_exhausted"] is True
        assert payload["rework_exhausted"] == ["run"]
        assert payload["rework_retry_attempt"] == 50
        assert result.update["user_approved"] is True
        assert "agent_messages" not in result.update

    def test_rework_budget_abort_cancels(self):
        state = make_state(
            pending={
                "kind": "rework_budget",
                "reason": "x",
                "allowed_actions": ["approve", "abort"],
            }
        )
        result, _ = call_gate(state, {"action": "abort"})
        assert result.update["status"] == "cancelled"
        assert result.update["review_feedback"] == "Job aborted by user."

    def test_rework_budget_test_run_reject_is_not_a_decision(self):
        state = make_state(
            task_source="workflow_test_run",
            user_approved=False,
            pending={
                "kind": "rework_budget",
                "reason": "x",
                "allowed_actions": ["approve", "abort"],
            },
        )
        result, _ = call_gate(state, None)
        assert result.goto == "orchestrator_validation"
        assert result.update["status"] == "awaiting_approval"
        assert result.update["user_approved"] is None

    def test_autonomy_recovery_payload_is_user_facing(self):
        reason = "step confidence 0.60 below assisted threshold 0.80"
        state = make_state(
            step_id="draft",
            pending={
                "kind": "autonomy",
                "reason": reason,
                "allowed_actions": ["approve", "reject"],
                "detail": {
                    "mode": "assisted",
                    "confidence": 0.6,
                    "min_confidence": 0.8,
                    "target_node": "researcher",
                },
            },
        )
        _result, mock_interrupt = call_gate(state, {"approved": True})

        payload = _payload(mock_interrupt)
        assert payload["gate_kind"] == "autonomy_gate"
        assert (
            "Flow Steward wants to start the draft step with the researcher agent."
            in (payload["content"])
        )
        assert "estimated confidence at 0.60, below the 0.80 threshold" in payload["content"]
        assert payload["technical_details"] == reason
        assert payload["gate_meta"]["step_execution_id"] == state["run_cursor"]["step_execution_id"]
        assert payload["gate_meta"]["target_agent"] == "researcher"

    def test_autonomy_approval_returns_to_the_hub_without_phase_keys(self):
        state = make_state(
            step_id="draft",
            pending={"kind": "autonomy", "reason": "step policy mode=manual", "detail": {}},
        )
        result, _ = call_gate(state, {"action": "override", "override_content": "x"})
        assert result.goto == "orchestrator"
        assert result.update["user_approved"] is True
        assert "autonomy_gate_approvals" not in result.update
        assert "autonomy_gate_pending" not in result.update

    def test_invoked_autonomy_recovery_requires_a_human(self):
        state = make_state(
            step_id="draft",
            invoke_child_auto_approve=True,
            pending={"kind": "autonomy", "reason": "step policy mode=manual"},
        )
        result, mock_interrupt = call_gate(state, {"approved": False})
        assert mock_interrupt.call_count == 1
        assert result.goto == "orchestrator"
        assert result.update["user_approved"] is False

    def test_invoke_child_auto_approve_does_not_bypass_failure_recovery(self):
        state = make_state(
            step_id="draft",
            invoke_child_auto_approve=True,
            last_failure_class="validation",
            pending={"kind": "failure", "reason": "checker returned fail after 2 retries"},
        )
        result, mock_interrupt = call_gate(state, {"approved": True})
        assert mock_interrupt.call_count == 1
        assert result.goto == "orchestrator"
        assert result.update["status"] == "in_progress"
        assert result.update["user_approved"] is True

    def test_recovery_test_run_pauses_then_resumes_from_state(self):
        pending = {"kind": "failure", "reason": "boom", "allowed_actions": ["approve", "reject"]}
        paused, _ = call_gate(
            make_state(step_id="draft", task_source="workflow_test_run", pending=pending), None
        )
        assert paused.goto == "orchestrator_validation"
        assert paused.update["approval_context"]

        resumed, _ = call_gate(
            make_state(
                step_id="draft",
                task_source="workflow_test_run",
                pending=pending,
                user_approved=True,
                review_feedback="ok",
            ),
            None,
        )
        assert resumed.goto == "orchestrator"
        assert resumed.update["user_approved"] is True
        assert resumed.update["review_feedback"] == "ok"


class TestHubAcceptsTheDecision:
    """The hub follows the authored outcome of the decision the gate recorded."""

    @pytest.fixture(autouse=True)
    def _quiet_events(self, monkeypatch):
        import core.application.orchestration.agents.common as agents_common

        monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)

    @staticmethod
    def _hub_after(state: dict, response) -> tuple[dict, Command]:
        from core.application.orchestration.step_hub import orchestrator_hub_node

        cursor = state["run_cursor"]
        state["dispatched_step_execution"] = {
            "step_id": cursor["step_id"],
            "step_execution_id": cursor["step_execution_id"],
            "attempt": cursor["attempt"],
        }
        result, _ = call_gate(state, response)
        merged = {**state, **result.update}
        return merged, orchestrator_hub_node(merged)

    def test_approved_follows_the_approved_edge(self):
        _state, command = self._hub_after(make_state(), {"approved": True})
        assert command.update["run_cursor"]["step_id"] == "done"
        assert command.update["run_cursor"]["rework"]["used"] == 0

    def test_rejected_follows_the_rework_edge_with_feedback(self):
        _state, command = self._hub_after(make_state(), {"approved": False, "comment": "Redo"})
        assert command.update["run_cursor"]["step_id"] == "draft"
        assert command.update["review_feedback"] == "Redo"
        assert command.update["run_cursor"]["rework"]["used"] == 1


@pytest.mark.parametrize("source", ["workflow_test_run", "step_test_run"])
def test_state_based_approval_projects_actionable_notification_after_timer_resume(source):
    from types import SimpleNamespace

    from core.application.jobs.workflow_run_projection import (
        approval_notification_payload_from_state,
    )

    state = make_state(task_source=source, approval={"message": "Review after timer"})
    from core.application.jobs.workflow_wait_projection import durable_wait_from_state
    from core.application.orchestration.wait_step_runtime import approval_pending_wait

    current = active_step(state)
    pending = approval_pending_wait(
        current, approval_id=approval_id_for_execution(current.step_execution_id), revision=1
    )
    state["pending_wait"] = pending
    paused, _ = call_gate(state, None)
    snapshot = SimpleNamespace(tasks=(), values={**state, **paused.update})
    payload = approval_notification_payload_from_state(snapshot)
    assert payload["wait_id"] == pending["wait_id"]
    assert durable_wait_from_state(snapshot) == pending
    assert payload["step_id"] == "review"
    assert payload["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert payload["message"] == "Review after timer"
    assert payload["approval_id"]
    assert payload["wait_id"]


def test_state_based_recovery_after_timer_has_a_stable_decision_identity():
    from types import SimpleNamespace

    from core.application.jobs.workflow_run_projection import (
        approval_notification_payload_from_state,
    )

    state = make_state(
        step_id="draft",
        task_source="workflow_test_run",
        pending={"kind": "failure", "reason": "boom"},
    )
    paused, _ = call_gate(state, None)
    snapshot = SimpleNamespace(tasks=(), values={**state, **paused.update})
    first = approval_notification_payload_from_state(snapshot)
    assert first["approval_id"]
    assert first["wait_id"]
    assert first == approval_notification_payload_from_state(snapshot)


def _paused_test_run_gate(*, source="workflow_test_run", recovery=False):
    from types import SimpleNamespace

    state = make_state(
        task_source=source,
        step_id="draft" if recovery else "review",
        pending={
            "kind": "rework_budget",
            "reason": "Budget exhausted",
            "allowed_actions": ["approve", "abort"],
        }
        if recovery
        else None,
    )
    paused, _ = call_gate(state, None)
    return SimpleNamespace(tasks=(), values={**state, **paused.update})


def _state_gate_command(snapshot):
    from core.application.management.command_services_approval import ApprovalCommandService

    return ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: None,
        enqueue_workflow_resume=lambda **_: None,
        workflow_resume_repository_factory=lambda _: None,
        get_thread_state_snapshot=lambda _: snapshot,
    )


@pytest.mark.parametrize("source", ["workflow_test_run", "step_test_run"])
@pytest.mark.parametrize("recovery", [False, True])
def test_state_based_gate_query_command_notification_share_identity_and_actions(source, recovery):
    from types import SimpleNamespace

    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.jobs.workflow_run_projection import (
        approval_notification_payload_from_state,
    )
    from core.application.management.command_models_api_inputs import ApprovalDecisionInput
    from core.application.management.errors import ServiceConflictError
    from core.application.management.query_services_approval import ApprovalQueryService

    snapshot = _paused_test_run_gate(source=source, recovery=recovery)
    notification = approval_notification_payload_from_state(snapshot)
    row = {
        "id": "42",
        "thread_id": "thread-42",
        "project_id": "7",
        "status": "awaiting_approval",
        "meta": {"workflow_test_run": True, "state_approval_id": "old-random-state-id"},
    }
    logger = SimpleNamespace(
        get_job=lambda _: row, get_project=lambda _: {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
    )
    query = ApprovalQueryService(
        get_logger=lambda: logger,
        get_thread_state_snapshot=lambda _: snapshot,
        approval_fallback_payload=lambda _: {"message": "fallback"},
    )
    public_gate = query.get_current_gate("42")
    assert public_gate.approval_id == notification["approval_id"]
    assert public_gate.allowed_actions == notification["allowed_actions"]

    command = _state_gate_command(snapshot)
    payload = command._current_interrupt_payload("thread-42", attempts=1)
    assert payload["approval_id"] == public_gate.approval_id
    decision = ApprovalDecisionInput(approved=True, approval_id=public_gate.approval_id)
    assert (
        command._validate_current_gate(
            row=row, payload=payload, decision=decision, token_principal=False
        )
        == public_gate.approval_id
    )
    stale = ApprovalDecisionInput(approved=True, approval_id="old-random-state-id")
    with pytest.raises(ServiceConflictError, match="stale"):
        command._validate_current_gate(
            row=row, payload=payload, decision=stale, token_principal=False
        )
    if recovery:
        rejected = ApprovalDecisionInput(
            approved=False, action="reject", approval_id=public_gate.approval_id
        )
        with pytest.raises(ServiceConflictError, match="not allowed"):
            command._validate_current_gate(
                row=row, payload=payload, decision=rejected, token_principal=False
            )


@pytest.mark.parametrize("source", ["workflow_test_run", "step_test_run"])
def test_initial_test_run_approval_polling_id_is_accepted_by_command(source, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.application.jobs import workflow_pause_persistence
    from core.application.jobs.workflow_run_projection import (
        approval_notification_payload_from_state,
    )
    from core.application.management.command_models_api_inputs import ApprovalDecisionInput
    from core.application.workflows.test_run_query_service import WorkflowTestRunQueryService
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        _persist_awaiting_approval_state,
    )

    snapshot = _paused_test_run_gate(source=source)
    notification = approval_notification_payload_from_state(snapshot)
    row = {
        "id": "42",
        "thread_id": "thread-42",
        "project_id": "7",
        "status": "in_progress",
        "meta": {"workflow_test_run": True},
    }
    logger = MagicMock()
    logger.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        "in_progress",
        "worker-1",
    )
    monkeypatch.setattr(
        workflow_pause_persistence, "transaction_connection", lambda conn: nullcontext(conn)
    )
    logger.get_job.return_value = row
    logger.update_job.side_effect = lambda _, **kwargs: row.update(kwargs)
    logger.merge_job_meta.side_effect = lambda _, patch: row["meta"].update(patch)
    _persist_awaiting_approval_state(
        bl=logger,
        expected_worker_id="worker-1",
        job_id="42",
        project_id="7",
        selected_workflow_id="wf",
        pending_interrupt_state=snapshot,
        result=snapshot.values,
    )
    public = WorkflowTestRunQueryService(
        SimpleNamespace(find_by_thread_id=lambda _: row)
    ).get_public_state("thread-42")
    assert public["approval_id"] == notification["approval_id"]
    command = _state_gate_command(snapshot)
    assert (
        command._validate_current_gate(
            row=row,
            payload=command._current_interrupt_payload("thread-42", attempts=1),
            decision=ApprovalDecisionInput(approved=True, approval_id=public["approval_id"]),
            token_principal=False,
        )
        == public["approval_id"]
    )


@pytest.mark.parametrize("recovery", [False, True])
def test_resumed_state_based_gate_persists_its_new_polling_identity(recovery):
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.application.jobs.workflow_resume_repository import (
        WorkflowResumeRepositoryAdapter,
    )
    from core.application.jobs.workflow_run_projection import (
        approval_notification_payload_from_state,
    )
    from core.application.workflows.test_run_query_service import WorkflowTestRunQueryService

    approval = approval_notification_payload_from_state(_paused_test_run_gate(recovery=recovery))
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.rowcount = 1
    repository = WorkflowResumeRepositoryAdapter(SimpleNamespace(conn=conn))
    assert repository.mark_finalizing(
        job_id="42",
        resume_revision=3,
        lease_token="owner",
        final_status="awaiting_approval",
        result={"status": "awaiting_approval", "approval": approval},
    )
    meta_patch = json.loads(cursor.execute.call_args.args[1][6])
    row = {
        "id": "42",
        "thread_id": "thread-42",
        "status": "awaiting_approval",
        "meta": {
            "workflow_test_run": True,
            "state_approval_id": "old-random-state-id",
            **meta_patch,
        },
    }
    public = WorkflowTestRunQueryService(
        SimpleNamespace(find_by_thread_id=lambda _: row)
    ).get_public_state("thread-42")
    assert public["approval_id"] == approval["approval_id"]


def test_ephemeral_test_run_uses_the_same_pinned_gate_identity():
    from core.application.jobs.workflow_run_projection import (
        approval_notification_payload_from_state,
    )
    from core.application.workflows.platform_test_run_runtime import _approval_id_from_state

    snapshot = _paused_test_run_gate()
    assert (
        _approval_id_from_state(snapshot.values)
        == approval_notification_payload_from_state(snapshot)["approval_id"]
    )


@pytest.mark.parametrize("source", ["workflow_test_run", "step_test_run"])
@pytest.mark.parametrize("recovery", [False, True])
def test_shared_state_payload_preserves_the_runtime_interrupt_contract(source, recovery):
    from core.application.orchestration.approval_payload import state_based_approval_payload

    snapshot = _paused_test_run_gate(source=source, recovery=recovery)
    projected = state_based_approval_payload(snapshot.values)
    interactive_state = {**snapshot.values, "task_source": "manual"}

    class GateCaptured(Exception):
        pass

    # Stop at the real node's interrupt boundary, before applying any decision.
    with (
        patch("langgraph.types.interrupt", side_effect=GateCaptured) as interrupt,
        pytest.raises(GateCaptured),
    ):
        _registry_approval_gate_node(interactive_state)
    expected = dict(interrupt.call_args.args[0])
    if recovery:
        expected["approval_id"] = f"recovery:{expected['request_id']}"
    assert projected == expected
