"""Public installed-stack lifecycle command specifications."""

from __future__ import annotations

from core.application.lifecycle.stack_service import (
    StackLogsRequest,
    StackLogsResult,
    StackRebuildRequest,
    StackRestartRequest,
    StackResult,
    StackStartRequest,
    StackStatusRequest,
    StackStopRequest,
)
from core.application.management.command_specs.contracts import (
    CommandSpec,
    CompactSupport,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)

_TOKEN_STACK_STATUS = "stack.status"
_TOKEN_STACK_LOGS = "stack.logs"


def _mutation_spec(
    action: str,
    *,
    summary: str,
    description: str,
    input_model: type,
    fields: tuple[FieldSpec, ...] = (),
    examples: tuple[str, ...] | None = None,
) -> CommandSpec:
    return CommandSpec(
        command_id=f"stack.{action}",
        path=("stack", action),
        summary=summary,
        description=description,
        examples=examples or (f"flow-steward stack {action}",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=fields,
        input_model=input_model,
        result_model=StackResult,
        plan_builder_ref="stack.mutation.preview",
        confirmation_policy_ref=None,
        service_ref=f"stack.{action}",
        result_renderer_ref=f"stack.{action}",
        # Every stack mutation goes to the retained fs-cli container, like the two
        # specs written out below. The factory hid them from a first pass that read
        # the files rather than the registry, and the packaging guard caught it.
        compact_support=CompactSupport.RETAINED_CLI,
        lifecycle_interrupts=True,
    )


STACK_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_STACK_STATUS,
        compact_support=CompactSupport.RETAINED_CLI,
        path=("stack", "status"),
        summary="Inspect installed Compose services and volumes.",
        description=(
            "Show detailed container, health, exit, published-port, and durable-volume "
            "state for the selected installed Compose configuration."
        ),
        examples=("flow-steward stack status", "flow-steward stack status --json"),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=StackStatusRequest,
        result_model=StackResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_STACK_STATUS,
        result_renderer_ref=_TOKEN_STACK_STATUS,
    ),
    _mutation_spec(
        "start",
        summary="Start the installed runtime safely.",
        description=(
            "Start selected runtime services and verify health and preservation. A start "
            "keeps the running container image identity unchanged; pass "
            "--adopt-image-pins to declare a redeploy onto the configured image pins."
        ),
        examples=(
            "flow-steward stack start",
            "flow-steward stack start --confirm --adopt-image-pins",
        ),
        input_model=StackStartRequest,
        fields=(
            FieldSpec(
                key="adopt_image_pins",
                label="Adopt configured image pins",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--adopt-image-pins",
                examples=("true",),
                description=(
                    "Declare that the runtime may move onto the image identities the "
                    "installed configuration pins. Data preservation is still verified."
                ),
            ),
        ),
    ),
    _mutation_spec(
        "stop",
        summary="Stop the installed runtime safely.",
        description="Stop selected runtime services without deleting containers or volumes.",
        input_model=StackStopRequest,
    ),
    _mutation_spec(
        "restart",
        summary="Restart the installed runtime safely.",
        description=(
            "Restart selected runtime services without applying configuration changes, "
            "then verify health and preservation."
        ),
        input_model=StackRestartRequest,
    ),
    _mutation_spec(
        "rebuild",
        summary="Build and recreate selected runtime services.",
        description=(
            "Build and force-recreate only the installed rebuildable runtime services "
            "without any volume operation."
        ),
        input_model=StackRebuildRequest,
    ),
    CommandSpec(
        command_id=_TOKEN_STACK_LOGS,
        compact_support=CompactSupport.RETAINED_CLI,
        path=("stack", "logs"),
        summary="Stream installed Compose service logs.",
        description=(
            "Stream redacted logs through the common technical channel. Service choices "
            "come from the selected installed Compose configuration."
        ),
        examples=(
            "flow-steward stack logs",
            "flow-steward stack logs --service fs-app --tail 200",
            "flow-steward stack logs --service fs-worker --follow",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            FieldSpec(
                key="service",
                label="Service",
                type=FieldType.SINGLE_SELECT,
                choice_provider="stack.log-services",
                option_name="--service",
                examples=("fs-app",),
                description="Optional installed Compose service to select.",
            ),
            FieldSpec(
                key="tail",
                label="Tail lines",
                type=FieldType.INTEGER,
                default=200,
                option_name="--tail",
                examples=("200",),
                description="Number of recent lines to stream.",
            ),
            FieldSpec(
                key="follow",
                label="Follow",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--follow",
                examples=("true",),
                description="Continue streaming new log lines.",
            ),
        ),
        input_model=StackLogsRequest,
        result_model=StackLogsResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_STACK_LOGS,
        result_renderer_ref=_TOKEN_STACK_LOGS,
    ),
)
