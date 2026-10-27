"""Public status and doctor command specifications."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from core.application.management.command_specs.contracts import (
    CommandSpec,
    CompactSupport,
    InteractivePolicy,
    OperationType,
)
from core.application.readiness import ReadinessReport


class ReadinessCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


STATUS_SPEC = CommandSpec(
    command_id="status",
    compact_support=CompactSupport.PLATFORM,
    path=("status",),
    summary="Show concise installation and core readiness.",
    description="Summarize core blockers and optional feature warnings without mutation.",
    examples=("flow-steward status", "flow-steward status --json"),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(),
    input_model=ReadinessCommandInput,
    result_model=ReadinessReport,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref="readiness.status",
    result_renderer_ref="status",
)

DOCTOR_SPEC = CommandSpec(
    command_id="doctor",
    compact_support=CompactSupport.PLATFORM,
    path=("doctor",),
    summary="Run detailed core-readiness checks.",
    description="Group findings into core readiness, optional features, and recommendations.",
    examples=("flow-steward doctor", "flow-steward doctor --json"),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(),
    input_model=ReadinessCommandInput,
    result_model=ReadinessReport,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref="readiness.doctor",
    result_renderer_ref="doctor",
)

READINESS_COMMAND_SPECS = (STATUS_SPEC, DOCTOR_SPEC)
