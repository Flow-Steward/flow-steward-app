"""Names and modes for a workflow diagram, without configuration values or authority."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.application.registry.workflow_contract import (
    WorkflowInputMode,
    WorkflowInterface,
    WorkflowOutputMode,
    WorkflowTriggerChannel,
)

_UNREADABLE_SETTINGS_CODES = frozenset(
    {
        "workflow.interface.invalid_callable_shape",
        "workflow.interface.retired_key_unsupported",
    }
)


class WorkflowSummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool


class WorkflowContractSummary(BaseModel):
    """A deliberately closed projection safe for an authorized archived diagram."""

    model_config = ConfigDict(extra="forbid")

    inputs: list[WorkflowSummaryInput] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    #: Returned files: the keys of step results ticked "Return from workflow".
    artifacts: list[str] = Field(default_factory=list)
    trigger_channels: list[WorkflowTriggerChannel] = Field(default_factory=list)
    input_mode: WorkflowInputMode
    output_mode: WorkflowOutputMode


def contract_summary(interface: WorkflowInterface) -> WorkflowContractSummary:
    """Project the same normalized public interface used by the live builder.

    An array/scalar JSON schema still has a contract, even though it contributes
    no named properties. Its mode survives without exposing the schema itself.
    """
    if interface.input_mode == "json_schema":
        properties = interface.input_schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = interface.input_schema.get("required")
        required = required if isinstance(required, list) else []
        inputs = [
            WorkflowSummaryInput(name=name, required=name in required)
            for name in properties
            if isinstance(name, str)
        ]
    else:
        inputs = [
            WorkflowSummaryInput(name=field.name, required=field.required)
            for field in interface.expected_inputs
        ]
    return WorkflowContractSummary(
        inputs=inputs,
        outputs=[field.name for field in interface.expected_outputs],
        artifacts=[row.name for row in interface.returned_files if row.name],
        trigger_channels=list(interface.trigger_channels),
        input_mode=interface.input_mode,
        output_mode=interface.output_mode,
    )


def archived_contract_summary(definition: Any) -> WorkflowContractSummary | None:
    """Read only the pinned definition; unavailable old data is distinct from empty.

    The caller must first authorize the job and its archived definition. Never
    substitute the live workflow when the archive has no usable contract data.
    """
    if not isinstance(definition, dict) or not isinstance(definition.get("settings"), dict):
        return None
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService

    try:
        interface = WorkflowInterfaceService().interface_from_definition(definition)
    except (TypeError, ValueError):
        return None
    # Unreadable settings make the archive unavailable; a returned file with a
    # setup issue is still a readable contract.
    if any(issue.code in _UNREADABLE_SETTINGS_CODES for issue in interface.settings_issues):
        return None
    return contract_summary(interface)
