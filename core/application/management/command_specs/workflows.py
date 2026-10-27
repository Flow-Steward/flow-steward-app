"""Workflow-domain public command specifications."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.application.management.command_specs.contracts import (
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.management.command_specs.workflow_authoring import (
    WORKFLOW_AUTHORING_COMMAND_SPECS,
)
from core.application.management.command_specs.workflow_shared import (
    PROJECT_ID_FIELD as _PROJECT_ID_FIELD,
)
from core.application.management.command_specs.workflow_shared import (
    WORKFLOW_ID_FIELD as _WORKFLOW_ID_FIELD,
)
from core.application.management.command_specs.workflow_shared import (
    WorkflowTargetCommandInput,
    _mapping_value,
)
from core.application.management.query_models import (
    ProjectWorkflowCatalogView,
    ProjectWorkflowEntrypointsView,
    WorkflowContractResultView,
    WorkflowSuggestionsResultView,
    WorkflowValidationResultView,
)

_TOKEN_WORKFLOWS_CONVERT_GROUP = "workflows.convert-group"
_TOKEN_WORKFLOWS_LIST = "workflows.list"
_TOKEN_WORKFLOWS_ENTRYPOINTS = "workflows.entrypoints"
_TOKEN_WORKFLOWS_VALIDATE = "workflows.validate"
_TOKEN_WORKFLOWS_CONTRACT = "workflows.contract"
_TOKEN_WORKFLOWS_SUGGESTIONS = "workflows.suggestions"
_TOKEN_WORKFLOWS_HOOKS_LIST = "workflows.hooks-list"
_TOKEN_WORKFLOWS_HOOK_GET = "workflows.hook-get"
_TOKEN_WORKFLOWS_HOOK_PREVIEW = "workflows.hook-preview"
_TOKEN_WORKFLOWS_HOOK_BIND = "workflows.hook-bind"
_TOKEN_WORKFLOWS_HOOK_UNBIND = "workflows.hook-unbind"
_TOKEN_WORKFLOWS_HOOK_CREATE_AND_BIND = "workflows.hook-create-and-bind"
_TOKEN_WORKFLOWS_WHERE_USED = "workflows.where-used"


class WorkflowListCommandInput(BaseModel):
    """Validated input for workflow catalog queries."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    include_disabled: bool = False
    include_validation: bool = False


class WorkflowEntrypointsCommandInput(BaseModel):
    """Validated input for workflow entrypoint queries."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    include_disabled: bool = False


class WorkflowConvertGroupCommandInput(WorkflowTargetCommandInput):
    group_id: str
    child_workflow_id: str
    display_name: str
    expected_revision: int = Field(gt=0)
    apply: bool = False
    preview_digest: str = ""

    @model_validator(mode="after")
    def require_preview(self):
        if self.apply and not self.preview_digest:
            raise ValueError("Apply requires --preview-digest from a current preview.")
        return self


class WorkflowConvertGroupResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    can_convert: bool
    child_workflow_id: str
    revision: int
    issues: list[dict] = Field(default_factory=list)
    preview_digest: str


class WorkflowHookCommandInput(WorkflowTargetCommandInput):
    """One hook of a linked workflow."""

    hook_id: str

    @field_validator("hook_id")
    @classmethod
    def require_hook_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("hook_id is required")
        return normalized


class WorkflowHookBindingCommandInput(WorkflowHookCommandInput):
    """A project binding to check or save: child, both mappings, edited revision."""

    expected_revision: int = Field(ge=0)
    target_workflow_id: str
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)
    hook_contract_version: int | None = Field(default=None, gt=0)
    hook_semantic_digest: str = ""
    release_id: str = ""

    @field_validator("input_mapping", "output_mapping", mode="before")
    @classmethod
    def parse_mapping(cls, value: Any) -> dict[str, str]:
        return _mapping_value(value)


class WorkflowHookPreviewCommandInput(WorkflowHookBindingCommandInput):
    """A binding to check and, optionally, a sample to run through it.

    ``sample`` is ``{"inputs": {...}, "child_outputs": {...}}``: hook inputs and
    a result of the child (for example from its Test Run). The check then also
    returns the hook output a run would publish for them.
    """

    sample: dict[str, Any] | None = None

    @field_validator("sample", mode="before")
    @classmethod
    def parse_sample(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else None
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or set(value) - {"inputs", "child_outputs"}
            or not all(isinstance(value.get(key, {}), dict) for key in ("inputs", "child_outputs"))
        ):
            raise ValueError("sample is a JSON object with the objects inputs and child_outputs")
        return dict(value)


class WorkflowHookUnbindCommandInput(WorkflowHookCommandInput):
    expected_revision: int = Field(gt=0)


class WorkflowHookCreateCommandInput(WorkflowHookCommandInput):
    expected_revision: int = Field(ge=0)
    child_workflow_id: str
    display_name: str = ""


class WorkflowHooksResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    project_id: str
    workflow_id: str
    hooks: list[dict] = Field(default_factory=list)


class WorkflowHookResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    hook_id: str
    binding_revision: int = 0


class WorkflowHookPreviewResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    valid: bool
    issues: list[dict] = Field(default_factory=list)


class WorkflowHookBindingResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    hook_id: str
    binding_revision: int
    revision_id: str
    target_workflow_id: str | None = None


class WorkflowWhereUsedCommandInput(WorkflowTargetCommandInput):
    """Optional presentation language never changes workflow authority."""

    locale: str = Field(default="", max_length=32)


class WorkflowConsumerResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["invoke", "hook"]
    caller_workflow_id: str
    caller_display_name: str | None = None
    caller_extension_id: str | None = None
    caller_extension_name: str | None = None
    caller_extension_version: str | None = None
    hook_display_name: str | None = None
    step_display_name: str | None = None
    step_id: str = ""
    caller_enabled: bool = True
    project_id: str | None = None
    hook_id: str | None = None
    source_install_id: str = ""
    local_workflow_id: str = ""
    binding_revision: int | None = None


class WorkflowWhereUsedResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    workflow_id: str
    workflow_display_name: str | None = None
    consumers: list[WorkflowConsumerResult] = Field(default_factory=list)


_HOOK_ID_FIELD = FieldSpec(
    key="hook_id",
    label="Hook id",
    type=FieldType.RUNTIME_IDENTIFIER,
    required=True,
    description="Stable id of the hook the author workflow declares.",
    examples=("before_invoice",),
    option_name="--hook-id",
)

_BINDING_REVISION_FIELD = FieldSpec(
    key="expected_revision",
    label="Binding revision",
    type=FieldType.INTEGER,
    required=True,
    description="Binding revision the change is based on; 0 before the first binding.",
    examples=("0",),
    option_name="--expected-revision",
)

_HOOK_SAMPLE_FIELD = FieldSpec(
    key="sample",
    label="Sample",
    type=FieldType.TEXT,
    default="",
    description=(
        'JSON object {"inputs": {...}, "child_outputs": {...}}: also return the hook output '
        "a run would publish for these hook inputs and this child result."
    ),
    examples=('{"inputs": {"invoice_id": 7}, "child_outputs": {"company_name": "Acme"}}',),
    option_name="--sample",
    advanced=True,
)

_HOOK_BINDING_FIELDS = (
    _PROJECT_ID_FIELD,
    _WORKFLOW_ID_FIELD,
    _HOOK_ID_FIELD,
    _BINDING_REVISION_FIELD,
    FieldSpec(
        key="target_workflow_id",
        label="Connected workflow",
        type=FieldType.RUNTIME_IDENTIFIER,
        required=True,
        description="Callable workflow of this account that the hook runs.",
        examples=("enrich_invoice",),
        option_name="--target-workflow-id",
    ),
    FieldSpec(
        key="input_mapping",
        label="Input mapping",
        type=FieldType.TEXT,
        default="{}",
        description="JSON object: child input name to the hook input it receives.",
        examples=('{"invoice_id": "invoice_id"}',),
        option_name="--input-mapping",
    ),
    FieldSpec(
        key="output_mapping",
        label="Output mapping",
        type=FieldType.TEXT,
        default="{}",
        description="JSON object: child output name to the hook field it fills.",
        examples=('{"company_name": "company"}',),
        option_name="--output-mapping",
    ),
    FieldSpec(
        key="hook_contract_version",
        label="Reviewed contract version",
        type=FieldType.INTEGER,
        description="Hook contract version you reviewed; a changed contract is refused.",
        examples=("1",),
        option_name="--hook-contract-version",
    ),
    FieldSpec(
        key="hook_semantic_digest",
        label="Reviewed contract digest",
        type=FieldType.TEXT,
        default="",
        description="Semantic digest of the hook contract you reviewed.",
        examples=("sha256_digest",),
        option_name="--hook-semantic-digest",
    ),
    FieldSpec(
        key="release_id",
        label="Extension release",
        type=FieldType.TEXT,
        default="",
        description="Prepare the binding for this extension release instead of now.",
        examples=("release_01J8",),
        option_name="--release-id",
        advanced=True,
    ),
)


WORKFLOW_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_CONVERT_GROUP,
        path=("workflows", "convert-group"),
        summary="Preview or apply conversion of a group to a callable workflow.",
        description="Preview typed boundaries first. Apply with the returned digest and revision creates the child and replaces the group atomically.",
        examples=(
            "flow-steward workflows convert-group parent --project-id project --group-id group --child-workflow-id child --display-name Child --expected-revision 1",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _PROJECT_ID_FIELD,
            _WORKFLOW_ID_FIELD,
            FieldSpec(
                key="group_id",
                description="Identifier of the saved Group to extract, with any For each inside it.",
                examples=("process_items",),
                label="Group",
                type=FieldType.RUNTIME_IDENTIFIER,
                required=True,
                option_name="--group-id",
            ),
            FieldSpec(
                key="child_workflow_id",
                description="Unique lowercase identifier for the newly created child workflow.",
                examples=("process_child",),
                label="Child workflow id",
                type=FieldType.RUNTIME_IDENTIFIER,
                required=True,
                option_name="--child-workflow-id",
            ),
            FieldSpec(
                key="display_name",
                description="Human-readable name for the generated callable workflow.",
                examples=("Process child",),
                label="Child name",
                type=FieldType.TEXT,
                required=True,
                option_name="--display-name",
            ),
            FieldSpec(
                key="expected_revision",
                description="Saved parent revision returned by the workflow definition endpoint.",
                examples=("3",),
                label="Parent revision",
                type=FieldType.INTEGER,
                required=True,
                option_name="--expected-revision",
            ),
            FieldSpec(
                key="apply",
                description="Create the reviewed child and atomically replace its parent group.",
                examples=("true",),
                label="Apply reviewed preview",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--apply",
            ),
            FieldSpec(
                key="preview_digest",
                description="Exact digest returned by the latest unchanged preview.",
                examples=("sha256_digest",),
                label="Preview digest",
                type=FieldType.TEXT,
                default="",
                option_name="--preview-digest",
            ),
        ),
        input_model=WorkflowConvertGroupCommandInput,
        result_model=WorkflowConvertGroupResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_CONVERT_GROUP,
        result_renderer_ref=_TOKEN_WORKFLOWS_CONVERT_GROUP,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_LIST,
        path=("workflows", "list"),
        summary="List workflows linked to a project.",
        description="List workflows linked to one project through the shared workflow operator service.",
        examples=("flow-steward workflows list --project-id proj_123",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _PROJECT_ID_FIELD,
            FieldSpec(
                key="include_disabled",
                label="Include disabled",
                type=FieldType.BOOLEAN,
                description="Include workflows that are currently disabled for the project.",
                examples=("true",),
                default=False,
                option_name="--include-disabled",
            ),
            FieldSpec(
                key="include_validation",
                label="Include validation",
                type=FieldType.BOOLEAN,
                description="Attach structured readiness and contract validation to each workflow.",
                examples=("true",),
                default=False,
                option_name="--include-validation",
            ),
        ),
        input_model=WorkflowListCommandInput,
        result_model=ProjectWorkflowCatalogView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_LIST,
        result_renderer_ref=_TOKEN_WORKFLOWS_LIST,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_ENTRYPOINTS,
        path=("workflows", "entrypoints"),
        summary="List workflow entrypoints.",
        description="List callable workflow entrypoints for one project.",
        examples=("flow-steward workflows entrypoints --project-id proj_123",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _PROJECT_ID_FIELD,
            FieldSpec(
                key="include_disabled",
                label="Include disabled",
                type=FieldType.BOOLEAN,
                description="Include callable entrypoints that are currently disabled.",
                examples=("true",),
                default=False,
                option_name="--include-disabled",
            ),
        ),
        input_model=WorkflowEntrypointsCommandInput,
        result_model=ProjectWorkflowEntrypointsView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_ENTRYPOINTS,
        result_renderer_ref=_TOKEN_WORKFLOWS_ENTRYPOINTS,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_VALIDATE,
        path=("workflows", "validate"),
        summary="Validate a workflow.",
        description="Validate one workflow linked to a project using structured workflow validation.",
        examples=("flow-steward workflows validate --project-id proj_123 release-report",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_PROJECT_ID_FIELD, _WORKFLOW_ID_FIELD),
        input_model=WorkflowTargetCommandInput,
        result_model=WorkflowValidationResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_VALIDATE,
        result_renderer_ref=_TOKEN_WORKFLOWS_VALIDATE,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_CONTRACT,
        path=("workflows", "contract"),
        summary="Show a workflow contract.",
        description="Return the machine-readable contract for one linked workflow.",
        examples=("flow-steward workflows contract --project-id proj_123 release-report",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_PROJECT_ID_FIELD, _WORKFLOW_ID_FIELD),
        input_model=WorkflowTargetCommandInput,
        result_model=WorkflowContractResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_CONTRACT,
        result_renderer_ref=_TOKEN_WORKFLOWS_CONTRACT,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_SUGGESTIONS,
        path=("workflows", "suggestions"),
        summary="Show workflow repair suggestions.",
        description="Return structured repair suggestions for one linked workflow.",
        examples=("flow-steward workflows suggestions --project-id proj_123 release-report",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_PROJECT_ID_FIELD, _WORKFLOW_ID_FIELD),
        input_model=WorkflowTargetCommandInput,
        result_model=WorkflowSuggestionsResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_SUGGESTIONS,
        result_renderer_ref=_TOKEN_WORKFLOWS_SUGGESTIONS,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_HOOKS_LIST,
        path=("workflows", "hooks-list"),
        summary="List the hooks of a workflow and their bindings.",
        description="List every hook a linked workflow declares with its binding in the project.",
        examples=("flow-steward workflows hooks-list --project-id proj_123 invoice-flow",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_PROJECT_ID_FIELD, _WORKFLOW_ID_FIELD),
        input_model=WorkflowTargetCommandInput,
        result_model=WorkflowHooksResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_HOOKS_LIST,
        result_renderer_ref=_TOKEN_WORKFLOWS_HOOKS_LIST,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_HOOK_GET,
        path=("workflows", "hook-get"),
        summary="Show one hook and its binding.",
        description="Show one hook's contract and its binding in the project.",
        examples=(
            "flow-steward workflows hook-get --project-id proj_123 invoice-flow --hook-id before_invoice",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_PROJECT_ID_FIELD, _WORKFLOW_ID_FIELD, _HOOK_ID_FIELD),
        input_model=WorkflowHookCommandInput,
        result_model=WorkflowHookResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_HOOK_GET,
        result_renderer_ref=_TOKEN_WORKFLOWS_HOOK_GET,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_HOOK_PREVIEW,
        path=("workflows", "hook-preview"),
        summary="Check a hook binding without saving it.",
        description=(
            "Run every binding check (child, mappings, chain, revision) and report the issues; "
            "with a sample, also return the hook output a run would publish."
        ),
        examples=(
            "flow-steward workflows hook-preview --project-id proj_123 invoice-flow --hook-id before_invoice --expected-revision 0 --target-workflow-id enrich_invoice",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(*_HOOK_BINDING_FIELDS, _HOOK_SAMPLE_FIELD),
        input_model=WorkflowHookPreviewCommandInput,
        result_model=WorkflowHookPreviewResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_HOOK_PREVIEW,
        result_renderer_ref=_TOKEN_WORKFLOWS_HOOK_PREVIEW,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_HOOK_BIND,
        path=("workflows", "hook-bind"),
        summary="Connect a workflow to a hook in a project.",
        description="Save a checked binding as the next revision; new runs of the project use it.",
        examples=(
            "flow-steward workflows hook-bind --project-id proj_123 invoice-flow --hook-id before_invoice --expected-revision 0 --target-workflow-id enrich_invoice",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=_HOOK_BINDING_FIELDS,
        input_model=WorkflowHookBindingCommandInput,
        result_model=WorkflowHookBindingResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_HOOK_BIND,
        result_renderer_ref=_TOKEN_WORKFLOWS_HOOK_BIND,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_HOOK_UNBIND,
        path=("workflows", "hook-unbind"),
        summary="Disconnect the workflow of a hook.",
        description="Clear the binding: new runs of the project pass the hook inputs through.",
        examples=(
            "flow-steward workflows hook-unbind --project-id proj_123 invoice-flow --hook-id before_invoice --expected-revision 1",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _PROJECT_ID_FIELD,
            _WORKFLOW_ID_FIELD,
            _HOOK_ID_FIELD,
            _BINDING_REVISION_FIELD,
        ),
        input_model=WorkflowHookUnbindCommandInput,
        result_model=WorkflowHookBindingResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_HOOK_UNBIND,
        result_renderer_ref=_TOKEN_WORKFLOWS_HOOK_UNBIND,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_HOOK_CREATE_AND_BIND,
        path=("workflows", "hook-create-and-bind"),
        summary="Create a workflow for a hook and connect it.",
        description="Create the empty workflow that holds a hook's steps, then bind it once.",
        examples=(
            "flow-steward workflows hook-create-and-bind --project-id proj_123 invoice-flow --hook-id before_invoice --expected-revision 0 --child-workflow-id invoice_actions",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _PROJECT_ID_FIELD,
            _WORKFLOW_ID_FIELD,
            _HOOK_ID_FIELD,
            _BINDING_REVISION_FIELD,
            FieldSpec(
                key="child_workflow_id",
                label="New workflow id",
                type=FieldType.RUNTIME_IDENTIFIER,
                required=True,
                description="Unique id of the workflow created for the hook.",
                examples=("invoice_actions",),
                option_name="--child-workflow-id",
            ),
            FieldSpec(
                key="display_name",
                label="New workflow name",
                type=FieldType.TEXT,
                default="",
                description="Name of the new workflow; the hook's name when empty.",
                examples=("Invoice actions",),
                option_name="--display-name",
            ),
        ),
        input_model=WorkflowHookCreateCommandInput,
        result_model=WorkflowHookBindingResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_HOOK_CREATE_AND_BIND,
        result_renderer_ref=_TOKEN_WORKFLOWS_HOOK_CREATE_AND_BIND,
    ),
    CommandSpec(
        command_id=_TOKEN_WORKFLOWS_WHERE_USED,
        path=("workflows", "where-used"),
        summary="Show where a workflow is used.",
        description="List the Invoke steps and hook bindings of the account that call the workflow.",
        examples=("flow-steward workflows where-used --project-id proj_123 enrich-invoice",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _PROJECT_ID_FIELD,
            _WORKFLOW_ID_FIELD,
            FieldSpec(
                key="locale",
                label="Language",
                type=FieldType.TEXT,
                default="",
                description="Optional language for human extension names.",
                examples=("en",),
                option_name="--locale",
            ),
        ),
        input_model=WorkflowWhereUsedCommandInput,
        result_model=WorkflowWhereUsedResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_WORKFLOWS_WHERE_USED,
        result_renderer_ref=_TOKEN_WORKFLOWS_WHERE_USED,
    ),
    *WORKFLOW_AUTHORING_COMMAND_SPECS,
)
