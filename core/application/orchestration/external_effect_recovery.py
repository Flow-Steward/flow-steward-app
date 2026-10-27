"""Workflow coordination for one persisted ambiguous external effect."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.external_effect_resolution import (
    PENDING_RETRY_REASONS,
    RESOLUTION_COMMAND,
    RETRY_AUTHORIZED,
    effect_intent,
)
from core.application.extensions.external_effect_resolution_repository import (
    ExternalEffectResolutionRepository,
)
from core.application.orchestration.active_step import ActiveStep, active_step
from core.application.orchestration.connector_effects import _source_ref_from_runtime_context
from core.application.orchestration.run_cursor import FrozenDefinition, Transition
from core.application.orchestration.runtime_identity import runtime_identity_from_state
from core.application.orchestration.step_operation_retry import has_unknown_external_effect
from core.infrastructure.db.db_pool import transaction_connection

RECOVERY_KIND = "external_effect_unknown"
CARRIER = "external_effect_resolution_ref"
OBSERVATIONS = "external_effect_observations"


class ExternalEffectRecoveryUnsupported(ValueError):
    """An unknown operation cannot be addressed by the existing workflow runtime."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"external_effect_resolution_unsupported: {reason}")


def _store() -> Any:
    from core.application.orchestration.connector_events import _shared_logger

    store = _shared_logger()
    if store is None or not callable(getattr(store, "cursor", None)):
        raise ExternalEffectRecoveryUnsupported("ledger storage is unavailable")
    return store


def observe_workflow_effect(
    state: Mapping[str, Any], current: ActiveStep, message: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Verify host observations against the exact scoped ledger row before retry policy."""
    if not has_unknown_external_effect(current, message):
        return None
    artifacts = message.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    observations = artifacts.get(OBSERVATIONS)
    observations = observations if isinstance(observations, list) else []
    if len(observations) != 1:
        raise ExternalEffectRecoveryUnsupported(
            "multiple ambiguous effects require separate decisions"
            if observations
            else "no exact ledger identity is available"
        )
    if current.step_kind != "connector" or current.lane_id:
        raise ExternalEffectRecoveryUnsupported(
            "this executor or parallel lane cannot retry one exact effect"
        )
    observation = observations[0]
    if (
        not isinstance(observation, Mapping)
        or observation.get("step_execution_id") != current.step_execution_id
        or observation.get("attempt") != current.attempt
    ):
        raise ExternalEffectRecoveryUnsupported("the observation belongs to another step execution")
    identity = runtime_identity_from_state(state)
    context = identity.as_context()
    source = _source_ref_from_runtime_context(context)
    with transaction_connection(_store()) as conn:
        effect = ExternalEffectResolutionRepository(conn).lock_effect(
            account_id=identity.account_id,
            project_id=identity.project_id or None,
            effect_id=str(observation.get("effect_id") or ""),
        )
    if (
        effect is None
        or effect.get("source_type") != "workflow_step"
        or effect.get("source_ref_jsonb") != source
    ):
        raise ExternalEffectRecoveryUnsupported(
            "the ledger source does not identify this workflow step"
        )
    version = observation.get("claim_version")
    if (
        type(version) is not int
        or effect.get("claim_version") != version
        or effect.get("status") != "timeout_unknown"
    ):
        raise ExternalEffectRecoveryUnsupported(
            "the ledger observation is stale or no longer ambiguous"
        )
    if effect.get("status_reason") in PENDING_RETRY_REASONS:
        raise ExternalEffectRecoveryUnsupported(
            "the recorded retry decision is awaiting its authorized resume"
        )
    return {
        "effect_id": str(effect["effect_id"]),
        "claim_version": version,
        "operation_id": str(effect.get("operation_id") or ""),
        "source_ref": source,
        "intent": effect_intent(effect),
    }


def direct_error_association(
    state: Mapping[str, Any],
    current: ActiveStep,
    message: Mapping[str, Any],
    transition: Transition,
    definition: FrozenDefinition,
) -> dict[str, Any] | None:
    """Bind only the executed host error edge to its immediate authored gate."""
    from core.application.orchestration.run_cursor import TRANSITION_DISPATCH

    if transition.kind != TRANSITION_DISPATCH or not transition.step_id:
        return None
    if definition.step(transition.step_id).get("step_kind") != "approval":
        return None
    try:
        effect = observe_workflow_effect(state, current, message)
    except ExternalEffectRecoveryUnsupported:
        return None  # Ordinary routing is still valid; no retry authority exists.
    if effect is None:
        return None
    return {
        "phase": "association",
        "effect": effect,
        "origin": current.message_identity(),
        "gate": {"step_id": transition.step_id},
        "definition_ref": dict(state.get("workflow_definition_ref") or {}),
    }


def _association_gate(
    state: Mapping[str, Any],
    association: Any,
    definition: FrozenDefinition | None = None,
) -> tuple[ActiveStep, str]:
    from core.application.orchestration.approval_payload import approval_id_for_execution
    from core.application.orchestration.run_cursor import NodeRef, Outcome, RunCursorError
    from core.application.orchestration.run_definition import load_run_definition

    frozen = definition or load_run_definition(state)
    current = active_step(state, definition=frozen)
    if (
        not isinstance(association, Mapping)
        or association.get("phase") != "association"
        or current is None
        or current.step_kind != "approval"
        or current.lane_id
        or association.get("gate") != current.message_identity()
        or association.get("definition_ref") != state.get("workflow_definition_ref")
    ):
        raise ValueError("external_effect_resolution_gate_mismatch")
    origin = association.get("origin") or {}
    effect = association.get("effect") or {}
    try:
        origin_step = (
            frozen.step(str(origin.get("step_id") or "")) if isinstance(origin, Mapping) else {}
        )
    except RunCursorError as exc:
        raise ValueError("external_effect_resolution_origin_invalid") from exc
    if (
        not isinstance(origin, Mapping)
        or type(origin.get("attempt")) is not int
        or origin["attempt"] < 1
        or not origin.get("step_execution_id")
        or origin_step.get("step_kind") != "connector"
    ):
        raise ValueError("external_effect_resolution_origin_invalid")
    edge = frozen.graph.edge_for(NodeRef("step", origin["step_id"]), Outcome("error"))
    if edge is None or edge.target != NodeRef("step", current.step_id):
        raise ValueError("external_effect_resolution_not_direct")
    identity = runtime_identity_from_state(state)
    source = _source_ref_from_runtime_context(
        {**identity.as_context(), "step_id": origin["step_id"]}
    )
    if not isinstance(effect, Mapping) or not isinstance(effect.get("intent"), Mapping):
        raise ValueError("external_effect_resolution_source_mismatch")
    intent = effect["intent"]
    if (
        source != effect.get("source_ref")
        or source != intent.get("source_ref_jsonb")
        or intent.get("source_type") != "workflow_step"
        or intent.get("account_id") != identity.account_id
        or intent.get("project_id") != (identity.project_id or "")
        or origin.get("group_id") != current.group_id
        or origin.get("for_each_id") != current.for_each_id
        or origin.get("item_index") != current.item_index
    ):
        raise ValueError("external_effect_resolution_source_mismatch")
    return current, approval_id_for_execution(current.step_execution_id)


def resolve_authored_approval_retry(
    conn: Any,
    *,
    row: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    approval_id: str,
    approved: bool,
    comment: str,
    actor_id: str,
    actor_type: str,
    account_id: str,
    project_id: str | None,
    current_gate_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Private decision seam: job pin/current gate/actual transition, then fence."""
    from copy import deepcopy

    from core.application.extensions.external_effect_resolution import resolve_external_effect
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.application.orchestration.run_cursor import (
        TRANSITION_DISPATCH,
        NodeRef,
        Outcome,
        RunCursor,
        resolve_after_step,
    )
    from core.application.orchestration.run_definition import RunDefinitionIntegrityError, _verified

    if not isinstance(state, Mapping) or state.get(CARRIER) is None:
        return None
    association = state[CARRIER]
    if (
        str(row.get("account_id") or "") != account_id
        or str(row.get("project_id") or "") != (project_id or "")
        or str(row.get("id") or "") != str(state.get("job_id") or "")
        or not actor_id
    ):
        raise ValueError("external_effect_resolution_scope_mismatch")
    archive = WorkflowDefinitionSnapshotRepository(conn).load_for_job(row["id"])
    pin = dict(state.get("workflow_definition_ref") or {})
    if archive is None:
        raise ValueError("external_effect_resolution_pin_mismatch")
    try:
        frozen = _verified(pin, archive)
    except (RunDefinitionIntegrityError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("external_effect_resolution_pin_mismatch") from exc
    if archive["workflow_id"] != str(state.get("workflow_id") or "") or archive[
        "workflow_id"
    ] != str(row.get("selected_workflow") or row.get("selected_workflow_key") or ""):
        raise ValueError("external_effect_resolution_pin_mismatch")
    if isinstance(association, Mapping) and association.get("phase") == "issued":
        return checked_gate_resolution_ref(
            state,
            association,
            connection=conn,
            definition=frozen,
            current_gate_payload=current_gate_payload,
            current_approval_id=approval_id,
        )
    current, current_id = _association_gate(state, association, frozen)
    if approval_id != current_id:
        raise ValueError("external_effect_resolution_gate_mismatch")
    outcome = Outcome("approved" if approved else "rejected")
    edge = frozen.graph.edge_for(NodeRef("step", current.step_id), outcome)
    original_step = association["origin"]["step_id"]
    if edge is None or edge.target != NodeRef("step", original_step):
        return None
    transition = resolve_after_step(
        frozen, RunCursor.load(deepcopy(state.get("run_cursor"))), outcome
    )
    if transition.kind != TRANSITION_DISPATCH or transition.step_id != original_step:
        raise ValueError("external_effect_resolution_return_not_immediate")
    effect = association["effect"]
    authorization = {
        "association": dict(association),
        "approval_id": approval_id,
        "approved": bool(approved),
        "outcome": outcome.name,
        "target_step_id": original_step,
    }
    return resolve_external_effect(
        conn,
        account_id=account_id,
        project_id=project_id,
        effect_id=effect["effect_id"],
        observed_version=effect["claim_version"],
        action=RETRY_AUTHORIZED,
        comment=comment,
        actor_id=actor_id,
        actor_type=actor_type,
        approval_id=approval_id,
        expected_source_ref=effect["source_ref"],
        authorization=authorization,
    )["resolution_ref"]


def _issued_autonomy_target(
    state: Mapping[str, Any],
    authorization: Mapping[str, Any],
    definition: FrozenDefinition,
    *,
    current_gate_payload: Mapping[str, Any] | None,
    current_approval_id: str,
) -> bool:
    current = active_step(state, definition=definition)
    pending = state.get("recovery_pending")
    if current is None or current.step_kind != "connector":
        return False
    association = authorization.get("association") or {}
    identity = runtime_identity_from_state(state)
    if (
        not isinstance(pending, Mapping)
        or pending.get("kind") != "autonomy"
        or not pending.get("request_id")
        or pending.get("step_id") != current.step_id
        or pending.get("step_execution_id") != current.step_execution_id
        or type(pending.get("attempt")) is not int
        or pending["attempt"] != current.attempt
        or current.lane_id
        or authorization.get("target_step_id") != current.step_id
        or association.get("origin", {}).get("step_id") != current.step_id
        or association.get("definition_ref") != state.get("workflow_definition_ref")
        or association.get("effect", {}).get("source_ref")
        != _source_ref_from_runtime_context(identity.as_context())
    ):
        raise ValueError("external_effect_resolution_autonomy_target_mismatch")
    if current_gate_payload is not None and (
        current_gate_payload.get("request_id") != pending["request_id"]
        or current_gate_payload.get("step_id") != current.step_id
        or current_gate_payload.get("step_execution_id") != current.step_execution_id
        or current_approval_id
        != str(
            current_gate_payload.get("approval_id")
            or current_gate_payload.get("interrupt_id")
            or current_gate_payload.get("id")
            or ""
        )
    ):
        raise ValueError("external_effect_resolution_gate_mismatch")
    return True


def checked_gate_resolution_ref(
    state: Mapping[str, Any],
    value: Any,
    *,
    approved: bool | None = None,
    connection: Any | None = None,
    definition: FrozenDefinition | None = None,
    current_gate_payload: Mapping[str, Any] | None = None,
    current_approval_id: str = "",
) -> dict[str, Any] | None:
    """A protected association is never authority; only the recorded issued ref is."""
    from contextlib import nullcontext
    from copy import deepcopy

    from core.application.orchestration.run_cursor import (
        TRANSITION_DISPATCH,
        NodeRef,
        Outcome,
        RunCursor,
        resolve_after_step,
    )
    from core.application.orchestration.run_definition import load_run_definition

    context = state.get(CARRIER)
    if value is None:
        if isinstance(context, Mapping) and context.get("phase") == "issued":
            raise ValueError("external_effect_resolution_reference_required")
        if isinstance(context, Mapping) and context.get("phase") == "association":
            current, _ = _association_gate(state, context)
            frozen = load_run_definition(state)
            outcome = Outcome("approved" if approved else "rejected")
            edge = frozen.graph.edge_for(NodeRef("step", current.step_id), outcome)
            if edge is not None and edge.target == NodeRef("step", context["origin"]["step_id"]):
                raise ValueError("external_effect_resolution_reference_required")
        return None
    if not isinstance(value, Mapping) or value.get("phase") != "issued":
        raise ValueError("external_effect_resolution_reference_invalid")
    identity = runtime_identity_from_state(state)
    frozen = definition or load_run_definition(state)
    with (
        nullcontext(connection) if connection is not None else transaction_connection(_store())
    ) as conn:
        repository = ExternalEffectResolutionRepository(conn)
        receipt = repository.receipt(
            account_id=identity.account_id, command_id=str(value.get("command_id") or "")
        )
        if (
            receipt is None
            or receipt["command_type"] != RESOLUTION_COMMAND
            or receipt["payload"].get("action") != RETRY_AUTHORIZED
            or receipt["result"].get("resolution_ref") != dict(value)
        ):
            raise ValueError("external_effect_resolution_reference_invalid")
        authorization = receipt["payload"].get("authorization") or {}
        association = authorization.get("association") or {}
        target_pause = _issued_autonomy_target(
            state,
            authorization,
            frozen,
            current_gate_payload=current_gate_payload,
            current_approval_id=current_approval_id,
        )
        if target_pause:
            if context != value:
                raise ValueError("external_effect_resolution_gate_mismatch")
        else:
            _, approval_id = _association_gate(state, association, frozen)
            if (context != association and context != value) or approval_id != value.get(
                "approval_id"
            ):
                raise ValueError("external_effect_resolution_gate_mismatch")
            if approved is not None and authorization.get("approved") != bool(approved):
                raise ValueError("external_effect_resolution_outcome_mismatch")
        effect = repository.lock_effect(
            account_id=identity.account_id,
            project_id=identity.project_id or None,
            effect_id=str(value.get("effect_id") or ""),
        )
        if (
            effect is None
            or effect_intent(effect) != receipt["payload"].get("intent")
            or type(value.get("fenced_version")) is not int
            or value["fenced_version"] != association["effect"]["claim_version"] + 1
            or effect.get("claim_version")
            not in {value["fenced_version"], value["fenced_version"] + 1}
        ):
            raise ValueError("external_effect_resolution_fence_invalid")
    if target_pause:
        return dict(value)
    transition = resolve_after_step(
        frozen,
        RunCursor.load(deepcopy(state.get("run_cursor"))),
        Outcome(authorization["outcome"]),
    )
    if (
        transition.kind != TRANSITION_DISPATCH
        or transition.step_id != authorization["target_step_id"]
    ):
        raise ValueError("external_effect_resolution_return_not_immediate")
    return dict(value)


def approval_return_ref(
    state: Mapping[str, Any],
    current: ActiveStep,
    transition: Transition,
    *,
    approved: bool,
) -> dict[str, Any] | None:
    """The issued ref survives exactly its chosen ordinary Approval return."""
    from core.application.orchestration.run_cursor import TRANSITION_DISPATCH

    value = state.get(CARRIER)
    if value is None or current.step_kind != "approval":
        return None
    if transition.kind != TRANSITION_DISPATCH:
        raise ValueError("external_effect_resolution_return_not_immediate")
    return checked_gate_resolution_ref(state, value, approved=approved)


def dispatch_resolution_ref(
    state: Mapping[str, Any],
    current: ActiveStep,
    update: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Bind new gate execution; default-clear everything except exact target delivery."""
    value = (update or {}).get(CARRIER)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("external_effect_resolution_reference_invalid")
    if value.get("phase") == "association":
        if current.step_kind != "approval" or value.get("gate") != {"step_id": current.step_id}:
            raise ValueError("external_effect_resolution_gate_mismatch")
        bound = {**value, "gate": current.message_identity()}
        _association_gate(
            {
                **state,
                "run_cursor": {
                    "step_id": current.step_id,
                    "step_execution_id": current.step_execution_id,
                    "attempt": current.attempt,
                    "frames": [frame.dump() for frame in current.frames],
                },
            },
            bound,
        )
        return bound
    ref = checked_gate_resolution_ref(state, value)
    if ref is None:
        raise ValueError("external_effect_resolution_reference_invalid")
    identity = runtime_identity_from_state(
        {
            **state,
            "run_cursor": {
                "step_id": current.step_id,
                "step_execution_id": current.step_execution_id,
                "attempt": current.attempt,
                "frames": [frame.dump() for frame in current.frames],
            },
        }
    )
    with transaction_connection(_store()) as conn:
        receipt = ExternalEffectResolutionRepository(conn).receipt(
            account_id=identity.account_id, command_id=ref["command_id"]
        )
    if receipt is None:
        raise ValueError("external_effect_resolution_reference_invalid")
    if receipt["payload"]["authorization"]["target_step_id"] != current.step_id or receipt[
        "payload"
    ]["intent"]["source_ref_jsonb"] != _source_ref_from_runtime_context(identity.as_context()):
        raise ValueError("external_effect_resolution_source_mismatch")
    return ref
