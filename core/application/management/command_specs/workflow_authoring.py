"""Workflow authoring commands: thin CLI adapters over the authoring services.

Each command mirrors one HTTP route (and its MCP tool) and calls the same
application service through ``WorkflowOperatorService`` or the project command
service. Object inputs are JSON text on the command line, or real JSON values
through ``--input-json``; a YAML bundle is read from a file.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.management.command_models import WorkflowRunAcceptedView
from core.application.management.command_specs.contracts import (
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.management.command_specs.workflow_shared import (
    PROJECT_ID_FIELD,
    WORKFLOW_ID_FIELD,
    WorkflowTargetCommandInput,
)

_TOKEN_SCHEMA = "workflows.authoring-schema"
_TOKEN_DRAFT_VALIDATE = "workflows.draft-validate"
_TOKEN_DRAFT_SAVE = "workflows.draft-save"
_TOKEN_DEFINITION = "workflows.definition"
_TOKEN_APPLY_CHANGES = "workflows.apply-changes"
_TOKEN_EXPORT = "workflows.export"
_TOKEN_IMPORT = "workflows.import"
_TOKEN_RUN = "workflows.run"
_TOKEN_STATS = "workflows.stats"


def _json_value(value: Any, *, kind: type, empty: Any) -> Any:
    """A JSON object or list, given as the value itself or as its JSON text (CLI)."""
    if isinstance(value, str):
        text = value.strip()
        value = json.loads(text) if text else empty
    if value is None:
        return empty
    if not isinstance(value, kind):
        raise ValueError(f"Expected a JSON {'object' if kind is dict else 'list'}")
    return value


def _resolutions(value: Any) -> list[dict[str, Any]]:
    rows = _json_value(value, kind=list, empty=[])
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("resolutions must be a JSON list of resolution objects")
    return rows


class WorkflowAuthoringSchemaCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_kind: str = ""


class WorkflowDraftCommandInput(BaseModel):
    """A YAML bundle file plus the resolutions ``WorkflowDraftInput`` accepts."""

    model_config = ConfigDict(extra="forbid")
    project_id: str
    bundle_file: str
    resolutions: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("resolutions", mode="before")
    @classmethod
    def parse_resolutions(cls, value: Any) -> list[dict[str, Any]]:
        return _resolutions(value)


class WorkflowApplyChangesCommandInput(WorkflowTargetCommandInput):
    """The project-linked target plus the ``WorkflowChangeSet`` body."""

    expected_revision: int = Field(ge=1)
    operations: list[dict[str, Any]] = Field(min_length=1)
    dry_run: bool = False

    @field_validator("operations", mode="before")
    @classmethod
    def parse_operations(cls, value: Any) -> list[dict[str, Any]]:
        return _json_value(value, kind=list, empty=[])


class WorkflowExportCommandInput(WorkflowTargetCommandInput):
    include_children: bool = False


class WorkflowImportCommandInput(BaseModel):
    """The import project, a YAML bundle file and ``WorkflowImportRequest`` options."""

    model_config = ConfigDict(extra="forbid")
    project_id: str
    bundle_file: str
    target_workflow_id: str = ""
    resolutions: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("resolutions", mode="before")
    @classmethod
    def parse_resolutions(cls, value: Any) -> list[dict[str, Any]]:
        return _resolutions(value)


class WorkflowRunCommandInput(WorkflowTargetCommandInput):
    """The ``WorkflowRunInput`` body; files are an HTTP/MCP upload, not a CLI input."""

    inputs: dict[str, Any] = Field(default_factory=dict)
    client_context: dict[str, Any] = Field(default_factory=dict)
    test_mode: bool = False
    idempotency_key: str = ""

    @field_validator("inputs", "client_context", mode="before")
    @classmethod
    def parse_object(cls, value: Any) -> dict[str, Any]:
        return _json_value(value, kind=dict, empty={})


class _OpenResult(BaseModel):
    model_config = ConfigDict(extra="allow")


class WorkflowAuthoringSchemaResult(_OpenResult):
    format: str
    kind: str
    step_kinds: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowDraftResult(_OpenResult):
    workflow_id: str = ""
    can_save: bool = False
    issues: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowDefinitionResult(_OpenResult):
    workflow_id: str
    revision: int | None = None


class WorkflowApplyChangesResult(_OpenResult):
    workflow_id: str
    revision: int | None = None
    valid: bool
    applied: bool = True
    issues: list[dict[str, Any]] = Field(default_factory=list)
    blocking_issues: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowExportResult(_OpenResult):
    workflow_id: str
    filename: str
    bundle_yaml: str


class WorkflowImportResult(_OpenResult):
    pass


class WorkflowStatsResult(_OpenResult):
    workflow_id: str
    total_runs: int = 0


def _project_field(description: str) -> FieldSpec:
    return PROJECT_ID_FIELD.model_copy(update={"description": description})


_BUNDLE_FILE_FIELD = FieldSpec(
    key="bundle_file",
    label="Bundle file",
    type=FieldType.PATH,
    required=True,
    description="Path of the workflow YAML bundle, as workflows export writes it.",
    examples=("drafts/invoice_flow.workflow.yaml",),
    option_name="--bundle-file",
)

_RESOLUTIONS_FIELD = FieldSpec(
    key="resolutions",
    label="Resolutions",
    type=FieldType.TEXT,
    default="[]",
    description="JSON list of dependency resolutions chosen for the bundle.",
    examples=('[{"dependency_key": "agent:writer", "action": "map_existing"}]',),
    option_name="--resolutions",
)

_DRAFT_FIELDS = (
    _project_field("Project the draft is checked in and, when saved, linked to."),
    _BUNDLE_FILE_FIELD,
    _RESOLUTIONS_FIELD,
)

_TARGET_FIELDS = (PROJECT_ID_FIELD, WORKFLOW_ID_FIELD)


def _spec(
    token: str,
    *,
    summary: str,
    description: str,
    example: str,
    fields: tuple[FieldSpec, ...],
    input_model: type[BaseModel],
    result_model: type[BaseModel],
    change: bool = False,
    accepts_input_json: bool = False,
) -> CommandSpec:
    return CommandSpec(
        command_id=token,
        path=tuple(token.split(".")),
        summary=summary,
        description=description,
        examples=(example,),
        operation_type=OperationType.CHANGE if change else OperationType.READ,
        interactive_policy=(
            InteractivePolicy.ALWAYS_REVIEW if change else InteractivePolicy.IMMEDIATE
        ),
        fields=fields,
        input_model=input_model,
        result_model=result_model,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=token,
        result_renderer_ref=token,
        accepts_input_json=accepts_input_json,
    )


WORKFLOW_AUTHORING_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    _spec(
        _TOKEN_SCHEMA,
        summary="Show the workflow authoring schema.",
        description="Return the bundle schema and step kinds that drafts and change sets use.",
        example="flow-steward workflows authoring-schema --step-kind agent",
        fields=(
            FieldSpec(
                key="step_kind",
                label="Step kind",
                type=FieldType.TEXT,
                default="",
                description="Limit the schema to one step kind; every kind when empty.",
                examples=("agent",),
                option_name="--step-kind",
            ),
        ),
        input_model=WorkflowAuthoringSchemaCommandInput,
        result_model=WorkflowAuthoringSchemaResult,
    ),
    _spec(
        _TOKEN_DRAFT_VALIDATE,
        summary="Check a workflow draft without saving it.",
        description="Run every draft check (schema, scope, references, readiness) on a bundle.",
        example=(
            "flow-steward workflows draft-validate --project-id proj_123 "
            "--bundle-file drafts/invoice_flow.workflow.yaml"
        ),
        fields=_DRAFT_FIELDS,
        input_model=WorkflowDraftCommandInput,
        result_model=WorkflowDraftResult,
    ),
    _spec(
        _TOKEN_DRAFT_SAVE,
        summary="Save a workflow draft as a disabled workflow.",
        description="Create the checked draft disabled and link it to the project, or save nothing.",
        example=(
            "flow-steward workflows draft-save --project-id proj_123 "
            "--bundle-file drafts/invoice_flow.workflow.yaml"
        ),
        fields=_DRAFT_FIELDS,
        input_model=WorkflowDraftCommandInput,
        result_model=WorkflowDraftResult,
        change=True,
    ),
    _spec(
        _TOKEN_DEFINITION,
        summary="Show a workflow definition.",
        description="Return the saved steps, groups, graph and revision of one linked workflow.",
        example="flow-steward workflows definition --project-id proj_123 invoice_flow",
        fields=_TARGET_FIELDS,
        input_model=WorkflowTargetCommandInput,
        result_model=WorkflowDefinitionResult,
    ),
    _spec(
        _TOKEN_APPLY_CHANGES,
        summary="Apply one atomic change set to a workflow.",
        description="Judge the operations as one set; save one new revision or nothing.",
        example=(
            "flow-steward workflows apply-changes --project-id proj_123 invoice_flow "
            "--expected-revision 3 --operations "
            '\'[{"op": "remove_step", "step_id": "draft_note"}]\''
        ),
        fields=(
            *_TARGET_FIELDS,
            FieldSpec(
                key="expected_revision",
                label="Workflow revision",
                type=FieldType.INTEGER,
                required=True,
                description="Saved revision the change set is based on, from workflows definition.",
                examples=("3",),
                option_name="--expected-revision",
            ),
            FieldSpec(
                key="operations",
                label="Operations",
                type=FieldType.TEXT,
                required=True,
                description="JSON list of change operations, as the changes API takes them.",
                examples=('[{"op": "remove_step", "step_id": "draft_note"}]',),
                option_name="--operations",
            ),
            FieldSpec(
                key="dry_run",
                label="Dry run",
                type=FieldType.BOOLEAN,
                default=False,
                description="Judge the set exactly as apply would and save nothing.",
                examples=("true",),
                option_name="--dry-run",
            ),
        ),
        input_model=WorkflowApplyChangesCommandInput,
        result_model=WorkflowApplyChangesResult,
        change=True,
        accepts_input_json=True,
    ),
    _spec(
        _TOKEN_EXPORT,
        summary="Export a workflow as a YAML bundle.",
        description="Return the portable YAML bundle of one linked workflow.",
        example="flow-steward workflows export --project-id proj_123 invoice_flow",
        fields=(
            *_TARGET_FIELDS,
            FieldSpec(
                key="include_children",
                label="Include children",
                type=FieldType.BOOLEAN,
                default=False,
                description="Add every workflow it invokes so the bundle is a complete template.",
                examples=("true",),
                option_name="--include-children",
            ),
        ),
        input_model=WorkflowExportCommandInput,
        result_model=WorkflowExportResult,
    ),
    _spec(
        _TOKEN_IMPORT,
        summary="Import a YAML bundle as disabled draft workflows.",
        description="Preflight and apply a workflow bundle; imported workflows start disabled.",
        example=(
            "flow-steward workflows import --project-id proj_123 "
            "--bundle-file exports/invoice_flow.workflow.yaml"
        ),
        fields=(
            _project_field("Project the imported workflows are associated with."),
            _BUNDLE_FILE_FIELD,
            FieldSpec(
                key="target_workflow_id",
                label="Target workflow id",
                type=FieldType.TEXT,
                default="",
                description="Import the bundle's workflow under this new id instead.",
                examples=("invoice_flow_copy",),
                option_name="--target-workflow-id",
            ),
            _RESOLUTIONS_FIELD,
        ),
        input_model=WorkflowImportCommandInput,
        result_model=WorkflowImportResult,
        change=True,
    ),
    _spec(
        _TOKEN_RUN,
        summary="Start a workflow run.",
        description="Start a linked workflow with its declared inputs; follow it with jobs status.",
        example=(
            "flow-steward workflows run --project-id proj_123 invoice_flow "
            '--inputs \'{"invoice_id": "INV-42"}\''
        ),
        fields=(
            _project_field("Project the run belongs to; the workflow must be linked to it."),
            WORKFLOW_ID_FIELD,
            FieldSpec(
                key="inputs",
                label="Inputs",
                type=FieldType.TEXT,
                default="{}",
                description="JSON object of the workflow's declared inputs.",
                examples=('{"invoice_id": "INV-42"}',),
                option_name="--inputs",
            ),
            FieldSpec(
                key="client_context",
                label="Client context",
                type=FieldType.TEXT,
                default="{}",
                description="JSON object of bounded execution-only context for the run.",
                examples=('{"source": "release-script"}',),
                option_name="--client-context",
                advanced=True,
            ),
            FieldSpec(
                key="test_mode",
                label="Test mode",
                type=FieldType.BOOLEAN,
                default=False,
                description="Run with test-mode safeguards enabled.",
                examples=("true",),
                option_name="--test-mode",
            ),
            FieldSpec(
                key="idempotency_key",
                label="Idempotency key",
                type=FieldType.TEXT,
                default="",
                description="Retry key: the same key and body return the same job.",
                examples=("invoice-INV-42",),
                option_name="--idempotency-key",
            ),
        ),
        input_model=WorkflowRunCommandInput,
        result_model=WorkflowRunAcceptedView,
        change=True,
        accepts_input_json=True,
    ),
    _spec(
        _TOKEN_STATS,
        summary="Show workflow run statistics.",
        description="Aggregate run counts, success rate, duration and cost of one workflow.",
        example="flow-steward workflows stats --project-id proj_123 invoice_flow",
        fields=_TARGET_FIELDS,
        input_model=WorkflowTargetCommandInput,
        result_model=WorkflowStatsResult,
    ),
)


__all__ = [
    "WORKFLOW_AUTHORING_COMMAND_SPECS",
    "WorkflowApplyChangesCommandInput",
    "WorkflowAuthoringSchemaCommandInput",
    "WorkflowDraftCommandInput",
    "WorkflowExportCommandInput",
    "WorkflowImportCommandInput",
    "WorkflowRunCommandInput",
]
