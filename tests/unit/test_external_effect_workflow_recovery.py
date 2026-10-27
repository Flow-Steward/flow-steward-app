"""The existing Approval surface carries only a protected, host-owned effect ref."""

from __future__ import annotations

import pytest

from core.application.orchestration.graph.resume_checkpoint import (
    _build_state_based_approval_update,
)
from core.application.orchestration.state import (
    OrchestratorState,
    OrchestratorStateModel,
    build_initial_orchestrator_state,
)
from core.application.orchestration.state_path_guards import is_reserved_state_target
from tests.unit.test_approval_gate import call_gate, make_state

KIND = "external_effect_unknown"
EFFECT = {"effect_id": "11111111-1111-1111-1111-111111111111", "claim_version": 3}
REF = {
    "effect_id": EFFECT["effect_id"],
    "fenced_version": 4,
    "command_id": "receipt",
    "approval_id": "gate",
}


def _resume_through_bridge(bridge, state, response):
    if bridge == "live":
        return call_gate(state, response)
    return _build_state_based_approval_update(
        approved=False,
        action="reject",
        comment=response["comment"],
        resume_value=response,
        state_values=state,
    )


def test_resolution_ref_is_a_declared_protected_channel_and_extra_state_cannot_forge_it():
    assert "external_effect_resolution_ref" in OrchestratorState.__annotations__
    assert "external_effect_resolution_ref" in OrchestratorStateModel.model_fields
    assert is_reserved_state_target("state.external_effect_resolution_ref")
    assert is_reserved_state_target("state.external_effect_resolution_ref.effect_id")
    state = build_initial_orchestrator_state(
        task_id="task",
        task_summary="publish",
        task_source="manual",
        project_name="project",
        extra_state={"external_effect_resolution_ref": REF},
    )
    assert not state.get("external_effect_resolution_ref")


@pytest.mark.parametrize("bridge", ["live", "state"])
def test_both_existing_bridges_reject_retired_factual_workflow_ref(bridge):
    state = make_state(
        step_id="draft", pending={"kind": KIND, "detail": {"external_effect": EFFECT}}
    )
    response = {
        "approved": False,
        "action": "reject",
        "comment": "Verified no effect.",
        "external_effect_resolution_ref": REF,
        "arbitrary_channel": "must not be copied",
    }
    # A factual direct-action receipt is not an authored workflow retry grant.
    with pytest.raises(ValueError, match="external_effect_resolution_reference_invalid"):
        _resume_through_bridge(bridge, state, response)


@pytest.mark.parametrize("bridge", ["live", "state"])
def test_bridges_reject_a_ref_for_another_effect_before_it_can_reach_dispatch(bridge):
    state = make_state(
        step_id="draft", pending={"kind": KIND, "detail": {"external_effect": EFFECT}}
    )
    response = {
        "approved": False,
        "action": "reject",
        "comment": "No effect.",
        "external_effect_resolution_ref": {
            **REF,
            "effect_id": "22222222-2222-2222-2222-222222222222",
        },
    }
    with pytest.raises(ValueError, match="resolution"):
        _resume_through_bridge(bridge, state, response)


def test_an_authored_gate_does_not_inherit_an_external_effect_retry_ref():
    state = make_state(external_effect_resolution_ref=REF)
    result, interrupted = call_gate(
        state, {"approved": True, "action": "approve", "comment": "Normal authored approval."}
    )
    update = result.update
    assert update.get("external_effect_resolution_ref") is None
    payload = interrupted.call_args.args[0]
    assert payload["gate_kind"] == "approval"
    assert payload["allowed_actions"] == ["approve", "reject"]
    assert "recovery_kind" not in payload
    assert update["agent_messages"][-1]["artifacts"]["_outcome"] == {"name": "approved"}


@pytest.mark.parametrize("unsupported", ["multiple", "lane"])
def test_unsupported_effect_context_is_blocked_before_guessing_a_ledger_row(unsupported):
    from core.application.orchestration.active_step import ActiveStep
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.external_effect_recovery import (
        ExternalEffectRecoveryUnsupported,
        observe_workflow_effect,
    )

    current = ActiveStep(
        step={"step_kind": "connector", "lane_id": "lane" if unsupported == "lane" else None},
        step_id="publish",
        step_execution_id="33333333-3333-4333-8333-333333333333",
        attempt=1,
    )
    observation = {
        **EFFECT,
        "step_execution_id": "33333333-3333-4333-8333-333333333333",
        "attempt": 1,
    }
    message = build_agent_message(
        from_agent="connector_step",
        to_agent="orchestrator",
        skill="connector",
        status="fail",
        correlation_id="host-unknown-result",
        step_id="publish",
        step_execution_id="33333333-3333-4333-8333-333333333333",
        attempt=1,
        published_at="2026-10-18T12:00:00+00:00",
        artifacts={
            "step_kind": "connector",
            "external_effect_observations": [observation] * (2 if unsupported == "multiple" else 1),
        },
        operation_retry={
            "schema_version": "operation_retry_signal_v1",
            "provenance": "deterministic_connector_step",
            "step_id": "publish",
            "signal": {
                "external_effect_status": "timeout_unknown",
                "request_semantics": "mutation",
                "retryable": False,
            },
        },
    )
    with pytest.raises(ExternalEffectRecoveryUnsupported, match=r"multiple|parallel lane"):
        observe_workflow_effect({}, current, message)


def test_connector_output_mapping_cannot_forge_the_host_resolution_ref():
    from core.application.orchestration.connector_mapping import _apply_output_mapping
    from tests.unit.test_connector_output_mapping_visibility import _dispatch, _step

    dispatch = _dispatch({"receipt": REF})
    _apply_output_mapping(
        step_payload=_step({"receipt": "state.external_effect_resolution_ref"}),
        dispatch_result=dispatch,
        state={},
    )
    assert dispatch["ok"] is False
    assert "state_patch" not in dispatch
    assert dispatch["errors"][0]["code"] == "output_mapping_reserved_target"
