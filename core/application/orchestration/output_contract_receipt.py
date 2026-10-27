"""Host-owned attempt output authority and verification against a pinned run."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema.exceptions import SchemaError

from core.application.orchestration.run_cursor import FrozenDefinition
from core.application.orchestration.run_definition import (
    definition_ref_from_state,
    load_run_definition,
)
from core.application.registry.step_output_contract import (
    StepOutputContract,
    resolve_step_output_contract,
)

OUTPUT_RECEIPT_VERSION = "agent_output_contract_v1"


@dataclass(frozen=True)
class OutputProducerContext:
    """Verified run context, retained at execution/terminal validation boundaries."""

    definition: FrozenDefinition
    snapshot_id: str
    definition_digest: str
    job_id: str

    @classmethod
    def for_state(
        cls, state: Mapping[str, Any], definition: FrozenDefinition | None = None
    ) -> OutputProducerContext:
        verified = load_run_definition(state) if definition is None else definition
        ref = definition_ref_from_state(state)
        return cls(verified, ref["snapshot_id"], ref["digest"], str(state.get("job_id") or ""))


def capture_agent_output_contract(
    agent: Any, manifest: Any, agent_input: Any
) -> tuple[StepOutputContract, Any]:
    """Authorize the selected skill and capture all output authority before invocation."""
    from core.application.management.errors import ServiceScopeNotFoundError
    from core.application.registry.registry_manager import RegistryManager

    registry = RegistryManager.instance()
    skills = getattr(agent, "skill_registry", None) or registry.skills
    state = {
        **(agent_input.state_ref or {}),
        **agent_input.task_context,
        **agent_input.step_context,
    }
    scope = state.get("scope_context", state)
    if not isinstance(scope, Mapping):
        raise ServiceScopeNotFoundError("Selected skill is unavailable in the current scope.")
    selected = skills.resolve_assigned_skill(
        manifest,
        agent_input.skill,
        scope=scope,
        project_id=str(state.get("project_id") or "").strip() or None,
    )
    step = agent_input.step_context.get("_step") or {}
    override = step.get("override") or {}
    block_key = str(override.get("published_block_key") or override.get("block_key") or "").strip()
    contract = resolve_step_output_contract(
        registry.db_store,
        agent_id=manifest.agent_id,
        skill_id=agent_input.skill,
        account_id=str(scope.get("account_id") or "").strip(),
        block_key=block_key,
        skill_manifest=selected,
        agent_manifest=manifest,
    )
    return contract, copy.deepcopy(selected)


def build_output_receipt(
    contract: StepOutputContract,
    context: OutputProducerContext,
    *,
    identity: dict[str, Any],
    agent_id: str,
    skill_id: str,
) -> dict[str, Any]:
    """Construct authority only from the host's captured contract and execution identity."""
    receipt = {
        "schema_version": OUTPUT_RECEIPT_VERSION,
        "definition_digest": context.definition_digest,
        "snapshot_id": context.snapshot_id,
        "job_id": context.job_id,
        "step_id": identity["step_id"],
        "step_execution_id": identity["step_execution_id"],
        "attempt": identity["attempt"],
        "from_agent": agent_id,
        "skill": skill_id,
        "contract_digest": contract.digest,
        "declarations": contract.rows,
    }
    # Host construction is strict; malformed optional receipts read from history
    # merely lose direct eligibility and never discard explicit state fallbacks.
    probe = {
        **identity,
        "from_agent": agent_id,
        "skill": skill_id,
        "status": "pass",
        "errors": [],
        "output_contract": receipt,
    }
    if verified_output_contract(probe, context) is None:
        raise ValueError("Invalid host output receipt binding")
    return receipt


def verified_output_contract(
    message: dict[str, Any], context: OutputProducerContext | None
) -> StepOutputContract | None:
    """No registry read: match receipt, message and verified pinned producer exactly."""
    if context is None or message.get("status") != "pass" or message.get("errors"):
        return None
    receipt = message.get("output_contract")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != OUTPUT_RECEIPT_VERSION:
        return None
    if (
        receipt.get("definition_digest") != context.definition_digest
        or receipt.get("snapshot_id") != context.snapshot_id
        or receipt.get("job_id") != context.job_id
    ):
        return None
    for key in ("step_id", "step_execution_id", "attempt", "from_agent", "skill"):
        if not message.get(key) or receipt.get(key) != message[key]:
            return None
    if type(receipt.get("attempt")) is not int:
        return None
    step = context.definition.steps.get(str(message["step_id"]))
    if not step or str(step.get("step_kind") or "agent") != "agent":
        return None
    static_agent = str(step.get("agent_id") or step.get("agent") or "").strip()
    if static_agent and static_agent != message["from_agent"]:
        return None
    if not static_agent and not step.get("participant_key"):
        return None
    static_skill = str(step.get("skill_id") or step.get("skill") or "").strip()
    if static_skill and static_skill != message["skill"]:
        return None
    try:
        return StepOutputContract.from_receipt(
            receipt.get("declarations"), receipt.get("contract_digest")
        )
    except (ValueError, TypeError, KeyError, SchemaError):
        return None
