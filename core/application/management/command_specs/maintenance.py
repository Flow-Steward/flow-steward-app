"""Public maintenance mode command specifications."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from core.application.maintenance.contracts import (
    MaintenanceChangeResult,
    MaintenanceStatusResult,
)
from core.application.management.command_specs.contracts import (
    CommandSpec,
    CompactSupport,
    InteractivePolicy,
    OperationType,
)

_TOKEN_MAINTENANCE_STATUS = "maintenance.status"
_TOKEN_MAINTENANCE_ENABLE = "maintenance.enable"
_TOKEN_MAINTENANCE_DISABLE = "maintenance.disable"


class MaintenanceCommandInput(BaseModel):
    """Closed empty input shared by the exact three maintenance commands."""

    model_config = ConfigDict(extra="forbid")


MAINTENANCE_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_MAINTENANCE_STATUS,
        compact_support=CompactSupport.PLATFORM,
        path=("maintenance", "status"),
        summary="Show platform maintenance status.",
        description="Read the durable maintenance phase and owner kind without changing it.",
        examples=("flow-steward maintenance status",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=MaintenanceCommandInput,
        result_model=MaintenanceStatusResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_MAINTENANCE_STATUS,
        result_renderer_ref=_TOKEN_MAINTENANCE_STATUS,
    ),
    CommandSpec(
        command_id=_TOKEN_MAINTENANCE_ENABLE,
        compact_support=CompactSupport.PLATFORM,
        path=("maintenance", "enable"),
        summary="Enable platform maintenance mode.",
        description="Stop new work, drain admitted work, and keep maintenance enabled.",
        examples=("flow-steward maintenance enable --confirm",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(),
        input_model=MaintenanceCommandInput,
        result_model=MaintenanceChangeResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_MAINTENANCE_ENABLE,
        result_renderer_ref=_TOKEN_MAINTENANCE_ENABLE,
    ),
    CommandSpec(
        command_id=_TOKEN_MAINTENANCE_DISABLE,
        compact_support=CompactSupport.PLATFORM,
        path=("maintenance", "disable"),
        summary="Disable platform maintenance mode.",
        description="Reopen mutations only after health and lifecycle safety checks pass.",
        examples=("flow-steward maintenance disable --confirm",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(),
        input_model=MaintenanceCommandInput,
        result_model=MaintenanceChangeResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_MAINTENANCE_DISABLE,
        result_renderer_ref=_TOKEN_MAINTENANCE_DISABLE,
    ),
)


__all__ = ["MAINTENANCE_COMMAND_SPECS", "MaintenanceCommandInput"]
