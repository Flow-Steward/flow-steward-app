# ruff: noqa: F405, I001, RUF100
from __future__ import annotations

from typing import Literal

from .query_models_common import *  # noqa: F403,F401  # NOSONAR
from .query_models_admin import *  # noqa: F403,F401  # NOSONAR
from .query_models_jobs import *  # noqa: F403,F401  # NOSONAR
from .query_models_workflows import *  # noqa: F403,F401  # NOSONAR

_MALFORMED_ENTRY_INPUTS_DESCRIPTION = "Original malformed boundary entry rows, retained for repair."


class WorkflowEditorStepView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Step",
        json_schema_extra={
            "description": "One authored workflow step as exposed by the internal workflow editor API.",
            "example": {
                "id": "step_row_42",
                "step_id": "draft_release_notes",
                "display_name": "Draft release notes",
                "step_kind": "agent",
                "agent_id": "release_writer",
                "skill_id": "draft_release_notes",
                "input_mapping_jsonb": {"release_version": "state.inputs.release_version"},
                "output_mapping_jsonb": {"draft_notes": "state.draft_notes"},
                "optional": False,
            },
        },
    )

    id: Any | None = None
    step_id: Any = ""
    group_id: str | None = None
    for_each_id: str | None = None
    lane_id: str | None = None
    block_key: str = ""
    display_name: str = ""
    step_kind: str = ""
    workflow_id: str | None = None
    participant_key: str = ""
    agent_id: str = ""
    skill_id: str | None = None
    connector_id: str | None = None
    connector_install_id: str | None = None
    input_mapping_jsonb: dict[str, Any] = Field(default_factory=dict)
    output_mapping_jsonb: dict[str, Any] = Field(default_factory=dict)
    connection_binding_jsonb: dict[str, Any] = Field(default_factory=dict)
    error_policy: dict[str, Any] = Field(default_factory=dict)
    boundary_contract: dict[str, Any] | None = None
    raw_expected_inputs: Any | None = Field(
        default=None, description=_MALFORMED_ENTRY_INPUTS_DESCRIPTION
    )
    expected_input_issues: list[WorkflowValidationIssueView] = Field(default_factory=list)
    optional: bool = False
    disabled: bool = Field(
        default=False, description="A disabled step is skipped by a run, which continues on Next."
    )
    context_inject: dict[str, Any] | None = None
    override: dict[str, Any] | None = None


class WorkflowEditorGroupView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Group",
        json_schema_extra={
            "description": "One Group of a workflow definition: its body runs once.",
            "example": {
                "group_id": "release_review",
                "display_name": "Release review",
                "body": {
                    "kind": "sequence",
                    "entry": {"kind": "step", "id": "draft_release_notes"},
                },
            },
        },
    )

    id: Any | None = None
    group_id: str = ""
    display_name: str = ""
    description: str = ""
    body: dict[str, Any] = Field(default_factory=dict)
    error_policy: dict[str, Any] = Field(default_factory=dict)
    boundary_contract: dict[str, Any] | None = None
    raw_expected_inputs: Any | None = Field(
        default=None, description=_MALFORMED_ENTRY_INPUTS_DESCRIPTION
    )
    expected_input_issues: list[WorkflowValidationIssueView] = Field(default_factory=list)
    sort_order: int | None = None


class WorkflowEditorForEachView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor For Each",
        json_schema_extra={
            "description": "One For each container: its body runs once per item of `over`.",
            "example": {
                "for_each_id": "each_release",
                "group_id": None,
                "display_name": "Each release",
                "over": "state.releases",
                "item_name": "release",
                "index_name": "release_index",
                "body": {
                    "kind": "sequence",
                    "entry": {"kind": "step", "id": "draft_release_notes"},
                },
            },
        },
    )

    id: Any | None = None
    for_each_id: str = ""
    group_id: str | None = None
    display_name: str = ""
    description: str = ""
    over: str = ""
    item_name: str = ""
    index_name: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)
    error_policy: dict[str, Any] = Field(default_factory=dict)
    boundary_contract: dict[str, Any] | None = None
    sort_order: int | None = None


class WorkflowDefinitionChangeResultView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Definition Change Result",
        json_schema_extra={
            "description": (
                "Result of one atomic change set: the new revision and the issues of the "
                "resulting definition (a disabled workflow may keep issues)."
            ),
            "example": {
                "workflow_id": "release-report",
                "revision": 7,
                "valid": True,
                "issues": [],
                "workflow": {"workflow_id": "release-report", "revision": 7},
            },
        },
    )

    workflow_id: str = ""
    revision: int | None = None
    valid: bool = False
    issues: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Issues exactly as the validator reported them (code, message, path, severity).",
    )
    workflow: dict[str, Any] = Field(default_factory=dict)
    applied: bool = Field(
        default=True,
        description="False for a dry run: nothing was saved and the revision is unchanged.",
    )
    blocking_issues: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Dry run only: the issues that would refuse the set on Apply.",
    )


class WorkflowEditorOutputView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Output",
        json_schema_extra={
            "description": "One output artifact or field visible to the workflow editor.",
            "example": {
                "name": "draft_notes",
                "type": "text",
                "description": "Draft release notes prepared by an earlier step.",
                "source": "analysis",
                "source_kind": "skill",
            },
        },
    )

    name: str = ""
    type: str = "text"
    description: str = ""
    source: str = ""
    source_kind: str = ""


class WorkflowEditorIOIssueView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor I/O Issue",
        json_schema_extra={
            "description": "One validation issue reported for a step's boundary inputs or I/O wiring.",
            "example": {
                "input_name": "release_version",
                "step_id": "draft_release_notes",
                "issue_type": "expected_input_source_unavailable",
                "severity": "error",
                "message": "Expected input 'release_version' source 'message.step.intake.release_version' not available from upstream steps or state",
                "path": "boundary_contract.entry[0].source",
            },
        },
    )

    input_name: str = ""
    agent_id: str = ""
    skill_id: str = ""
    step_id: str = ""
    group_id: str | None = None
    issue_type: str = ""
    source: str = ""
    source_step_id: str = ""
    source_name: str = ""
    path: str = ""
    severity: str = ""
    message: str = ""
    expected_type: str | None = None
    actual_type: str | None = None


class WorkflowEditorVariableView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Variable",
        json_schema_extra={
            "description": "One variable available to workflow authoring and mapping UI.",
            "example": {
                "name": "release_version",
                "type": "text",
                "description": "Version being evaluated.",
                "source": MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE,
            },
        },
    )

    name: str = ""
    type: str = "text"
    description: str = ""
    source: str = ""
    group: str | None = None
    blocked: bool | None = None


class WorkflowEditorAvailableVariablesView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Available Variables",
        json_schema_extra={
            "description": "Grouped variable sources available while editing one workflow step.",
            "example": {
                "state": [
                    {
                        "name": "task_summary",
                        "type": "text",
                        "description": "Task title / description",
                    }
                ],
                "dotted_path": [
                    {
                        "name": "project_config.project_name",
                        "type": "text",
                        "description": "Project name",
                    }
                ],
                "prior_outputs": [
                    {
                        "name": "draft_notes",
                        "type": "text",
                        "description": "Draft notes from an earlier step",
                        "source": "analysis",
                        "source_kind": "skill",
                    }
                ],
            },
        },
    )

    state: list[WorkflowEditorVariableView] = Field(default_factory=list)
    dotted_path: list[WorkflowEditorVariableView] = Field(default_factory=list)
    prior_outputs: list[WorkflowEditorOutputView] = Field(default_factory=list)
    loop: list[WorkflowEditorVariableView] = Field(
        default_factory=list,
        description="Item and index names the For each around the step publishes to its body.",
    )
    position: Literal["before", "after", "unknown"] = Field(
        default="before",
        description=(
            "Where the variables were read: before the step, after it (a step placed right "
            "after it), or unknown (a step not connected yet: every saved step's outputs)."
        ),
    )


class WorkflowEditorInputDefaultView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Input Default",
        json_schema_extra={
            "description": "One suggested or current step input contract row for editor defaults.",
            "example": {
                "name": "release_version",
                "type": "text",
                "source": MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE,
                "required": True,
                "resolved": True,
                "suggested_sources": [MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE],
            },
        },
    )

    name: str = ""
    type: str = "text"
    source: str = ""
    required: bool = False
    resolved: bool | None = None
    read_only: bool | None = None
    source_available: bool | None = None
    source_type: str | None = None
    resolution: str | None = None
    suggested_sources: list[str] = Field(default_factory=list)
    contract_source: str | None = None
    contract_source_id: str | None = None
    description: str | None = None


class WorkflowEditorInputDefaultsView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Input Defaults",
        json_schema_extra={
            "description": "Required, optional, and custom boundary-entry defaults derived from a step's contracts.",
            "example": {
                "workflow_id": "release-report",
                "step_id": "draft_release_notes",
                "required_inputs": [
                    {
                        "name": "release_version",
                        "type": "text",
                        "source": MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE,
                        "required": True,
                        "resolved": True,
                    }
                ],
                "optional_inputs": [],
                "custom_inputs": [],
                "current_expected_inputs": [
                    {
                        "name": "release_version",
                        "type": "text",
                        "source": MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE,
                        "required": True,
                    }
                ],
            },
        },
    )

    workflow_id: str = ""
    step_id: str = ""
    group_id: str | None = None
    required_inputs: list[WorkflowEditorInputDefaultView] = Field(default_factory=list)
    optional_inputs: list[WorkflowEditorInputDefaultView] = Field(default_factory=list)
    custom_inputs: list[WorkflowEditorInputDefaultView] = Field(default_factory=list)
    current_expected_inputs: list[WorkflowEditorInputDefaultView] = Field(default_factory=list)
    raw_expected_inputs: Any | None = Field(
        default=None, description=_MALFORMED_ENTRY_INPUTS_DESCRIPTION
    )
    expected_input_issues: list[WorkflowValidationIssueView] = Field(default_factory=list)


class WorkflowEditorPublishedBlockView(ViewModel):
    block_key: str = ""
    display_name: str = ""
    source_kind: str = ""
    source_ref: str = ""


class WorkflowEditorSkillRefView(ViewModel):
    id: str = ""
    label: str = ""


class WorkflowEditorContractInputView(ViewModel):
    name: str = ""
    type: str = "text"
    description: str = ""
    required: bool = False
    source: str = ""
    source_kind: str | None = None
    resolved: bool = False
    resolution: str = ""
    suggested_sources: list[str] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)
    read_only: bool | None = None
    contract_source: str | None = None
    contract_source_id: str | None = None
    source_available: bool | None = None
    source_type: str | None = None


class WorkflowEditorStepContractPreviewView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Step Contract Preview",
        json_schema_extra={
            "description": "Effective contract preview used by the workflow editor for one step.",
            "example": {
                "workflow_id": "release-report",
                "step_id": "draft_release_notes",
                "group_id": None,
                "step_kind": "agent",
                "agent_id": "release_writer",
                "skill_id": "draft_release_notes",
                "published_block": None,
                "skill": {"id": "draft_release_notes", "label": DRAFT_RELEASE_NOTES_VALUE},
                "locked_required": [
                    {
                        "name": "release_version",
                        "type": "text",
                        "required": True,
                        "source": MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE,
                        "resolved": True,
                        "resolution": "explicit_source",
                        "suggested_sources": [],
                        "read_only": True,
                    }
                ],
                "unresolved_required": [],
                "optional": [],
                "custom": [],
                "outputs": [
                    {
                        "name": "draft_notes",
                        "type": "text",
                        "description": "Draft notes prepared by the step",
                        "source": "draft_release_notes",
                        "source_kind": "skill",
                    }
                ],
                "available_type_map": {MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE: "text"},
            },
        },
    )

    workflow_id: str = ""
    step_id: str = ""
    group_id: str | None = None
    step_kind: str = ""
    agent_id: str = ""
    skill_id: str = ""
    block_key: str = ""
    published_block: WorkflowEditorPublishedBlockView | None = None
    skill: WorkflowEditorSkillRefView = Field(default_factory=WorkflowEditorSkillRefView)
    locked_required: list[WorkflowEditorContractInputView] = Field(default_factory=list)
    unresolved_required: list[WorkflowEditorContractInputView] = Field(default_factory=list)
    optional: list[WorkflowEditorContractInputView] = Field(default_factory=list)
    custom: list[WorkflowEditorContractInputView] = Field(default_factory=list)
    raw_expected_inputs: Any | None = Field(
        default=None, description=_MALFORMED_ENTRY_INPUTS_DESCRIPTION
    )
    expected_input_issues: list[WorkflowValidationIssueView] = Field(default_factory=list)
    outputs: list[WorkflowEditorOutputView] = Field(default_factory=list)
    available_type_map: dict[str, str] = Field(default_factory=dict)


class WorkflowEditorSimulationGateView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Simulation Gate",
        json_schema_extra={
            "description": "Gate configuration preview returned during workflow simulation.",
            "example": {"type": "none"},
        },
    )

    type: str = "none"
    condition: str | None = None
    on_true: str | None = None
    fallback: str | None = None
    on_timeout: str | None = None
    message: str | None = None
    on_demand_only: bool | None = None


class WorkflowEditorSimulationClassificationView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Simulation Classification",
        json_schema_extra={
            "description": "Task classification preview produced before a simulated workflow run.",
            "example": {
                "task_type": "release_analysis",
                "effort_hint": "medium",
                "confidence": 0.92,
                "risk_level": "moderate",
                "automation_recommendation": "auto_run",
                "reasoning": "The request matches the release-report workflow and has explicit inputs.",
            },
        },
    )

    task_type: str = ""
    effort_hint: str = ""
    confidence: float = 0.0
    risk_level: str = ""
    automation_recommendation: str = ""
    reasoning: str = ""


class WorkflowEditorSimulationStepView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Simulation Step",
        json_schema_extra={
            "description": "One step preview returned inside a simulated workflow run.",
            "example": {
                "step_id": "draft_release_notes",
                "step_kind": "agent",
                "participant_key": "writer",
                "agent": RELEASE_WRITER_VALUE,
                "agent_id": "release_writer",
                "skill": DRAFT_RELEASE_NOTES_VALUE,
                "skill_id": "draft_release_notes",
                "workflow_id": "release-report",
                "optional": False,
            },
        },
    )

    step_id: str = ""
    step_kind: str = ""
    group_id: str | None = None
    lane_id: str | None = None
    participant_key: str = ""
    agent: str = ""
    agent_id: str = ""
    skill: str = ""
    skill_id: str = ""
    connector_id: str = ""
    connector_install_id: str = ""
    workflow_id: str = ""
    optional: bool = False
    disabled: bool = False


class WorkflowEditorSimulationResultView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Simulation Result",
        json_schema_extra={
            "description": "Simulation preview returned before running a workflow for real.",
            "example": {
                "workflow_id": "release-report",
                "validation": {"valid": True, "errors": [], "warnings": []},
                "classification": {
                    "task_type": "release_analysis",
                    "effort_hint": "medium",
                    "confidence": 0.92,
                    "risk_level": "moderate",
                    "automation_recommendation": "auto_run",
                    "reasoning": "The request matches the release-report workflow and has explicit inputs.",
                },
                "steps": [
                    {
                        "step_id": "draft_release_notes",
                        "step_kind": "agent",
                        "group_id": None,
                        "lane_id": None,
                        "participant_key": "writer",
                        "agent": RELEASE_WRITER_VALUE,
                        "agent_id": "release_writer",
                        "skill": DRAFT_RELEASE_NOTES_VALUE,
                        "skill_id": "draft_release_notes",
                        "workflow_id": "release-report",
                        "optional": False,
                    }
                ],
            },
        },
    )

    workflow_id: str = ""
    validation: dict[str, Any] = Field(default_factory=dict)
    classification: WorkflowEditorSimulationClassificationView = Field(
        default_factory=WorkflowEditorSimulationClassificationView
    )
    steps: list[WorkflowEditorSimulationStepView] = Field(default_factory=list)


class WorkflowEditorTestRunResultView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Editor Test Run Result",
        json_schema_extra={
            "description": "Ephemeral workflow or step test-run payload returned by the internal workflow editor API.",
            "example": {
                "status": "dry_run",
                "dry_run": True,
                "workflow_id": "release-report",
                "project_id": "proj_42",
                "task_id": "task_123",
                "task_summary": "Validate workflow path",
                "test_mode": True,
            },
        },
    )

    status: str = ""
    dry_run: bool | None = None
    would_execute_ephemerally: bool | None = None
    workflow_id: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    task_id: str | None = None
    task_summary: str | None = None
    task_envelope: dict[str, Any] = Field(default_factory=dict)
    mode: str | None = None
    test_mode: bool | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    job_id: str | None = None
    thread_id: str | None = None
    execution_id: str | None = None
    final_status: str | None = None
    final_report: str | None = None
    error: str | None = None
    needs_user_approval: bool = False
    approval_id: str | None = None
    approval_context: str | None = None
    current_step_id: str | None = None
    current_group_id: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    workflow_outputs: dict[str, Any] = Field(default_factory=dict)
    workflow_terminal: Any | None = None
    state: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None
    step_execution_id: str | None = None
    step_kind: str | None = None
    summary: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    state_patch: dict[str, Any] = Field(default_factory=dict)
