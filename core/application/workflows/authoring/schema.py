"""Existing portability and persisted step configuration guidance."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

import yaml

from core.application.management.errors import ServiceNotFoundError
from core.application.registry.builtin_step_kind_profiles import builtin_step_kind_profiles
from core.application.workflow_documents.report_examples import TEMPLATE_REPORT_EXAMPLE
from core.application.workflows.execution_graph import OUTCOME_ERROR
from core.application.workflows.workflow_authoring_requests import WorkflowChangeSet
from core.application.workflows.workflow_definition_changes import OPERATION_KINDS
from core.application.workflows.workflow_definition_validation import (
    WAITING_STEP_KINDS,
    step_outcomes,
)
from core.application.workflows.workflow_feature_versions import (
    TYPED_CONTRACT_CORE_MIN,
    TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN,
)
from core.application.workflows.workflow_portability import BUNDLE_KIND, SCHEMA_VERSION
from core.application.workflows.workflow_step_config_contracts import STEP_CONFIG_ALLOWED_KEYS
from core.infrastructure.config.installation_mode import installation_mode

_STEP_REFERENCE_FIELDS = {
    "agent": frozenset({"agent_id", "skill_id"}),
    "connector": frozenset({"connector_id", "connector_install_id"}),
}


class DefinitionModelGuidance(TypedDict):
    persisted_location: str
    description: str


class DefinitionExample(TypedDict):
    id: str
    purpose: str
    prerequisites: str
    limitations: str
    bundle_yaml: str


class OperationExample(TypedDict):
    id: str
    purpose: str
    limitations: str
    precondition_bundle_yaml: str
    expected_revision_guidance: str
    dry_run: bool
    operations: list[dict[str, Any]]


class ChangeSetGuidance(TypedDict):
    schema_scope: str
    request_envelope_schema: dict[str, Any]
    operation_names: list[str]
    partial_examples: list[OperationExample]


class DefinitionGuidance(TypedDict):
    contract_level: str
    validation_guidance: str
    models: dict[str, DefinitionModelGuidance]
    examples: list[DefinitionExample]
    change_set: ChangeSetGuidance


class AuthoringSchemaResult(TypedDict):
    format: str
    kind: str
    installation_mode: str
    minimal_bundle_yaml: str
    step_kinds: list[dict[str, Any]]
    mapping_guidance: str
    definition_guidance: DefinitionGuidance


_HOOK_DEVELOPMENT_ONLY = (
    "Development installation mode is required for a new Hook declaration "
    "(see installation_mode); elsewhere it is refused with "
    "workflow.hook.declaration_requires_development."
)
_GROUP_NOT_CONVERTIBLE = (
    "Its Group body holds only set_variable steps, so workflow_convert_group refuses it "
    "as is (workflow.execution_bindings_missing): a child workflow needs at least one "
    "execution step such as a transform, HTTP request, connector or agent."
)


_APPROVAL_REVIEW_GUIDANCE = (
    "override.approval.review gives the person typed fields to check and correct before "
    "deciding. Each of review.fields is a workflow contract field (name, label, type, "
    "required, options, items, properties, min_items, max_items, format, description) "
    "plus editable (the person may change it) and from (its initial value, the same "
    "selector syntax as an invoke_workflow input mapping: "
    "message.step.<step_id>.artifacts.<name>, state.<path> or literal:<json>). A field "
    "without a type is a paragraph; secret fields are refused, and a required field that "
    "is not editable needs a from. review.original names a file or a list of files shown "
    "beside the fields; review.fields_from_workflow is an editor hint only. On approve the "
    "checked values become the step's outputs message.step.<step_id>.artifacts.<field>, "
    "which later steps map like any other output; message.step.<step_id>.artifacts.comment "
    "carries the reviewer's comment, which is the feedback on the rejected branch, and "
    "artifacts.decision is approved or rejected. Decisions send corrections as values "
    "(job_approval_decide values): editable fields only, typed as listed. A template or "
    "extension bundle that uses review or the contract shape keys declares the first Core "
    "version that has them, or its import is refused: the catalog entry's "
    f"compatible.min {TYPED_CONTRACT_CORE_MIN} or later, the bundle's runtime.compatibility.platform_min "
    f"{TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN} or later."
)


class WorkflowAuthoringSchema:
    """Expose existing schemas with the persisted locations their consumers read."""

    def get(self, *, step_kind: str | None = None) -> AuthoringSchemaResult:
        profiles = [profile for profile in builtin_step_kind_profiles() if profile.get("enabled")]
        if step_kind:
            profiles = [profile for profile in profiles if profile["step_kind_key"] == step_kind]
            if not profiles:
                raise ServiceNotFoundError("Unknown step kind")
        mode = installation_mode()
        return {
            "format": SCHEMA_VERSION,
            "kind": BUNDLE_KIND,
            # New Hook declarations are refused outside Development, so a client
            # needs to know which mode it is authoring against.
            "installation_mode": mode,
            "minimal_bundle_yaml": yaml.safe_dump(self._minimal_bundle(), sort_keys=False),
            "step_kinds": [self._step_profile(profile) for profile in profiles],
            "mapping_guidance": "Use step.input_mapping_jsonb and step.output_mapping_jsonb; runtime configuration belongs in step.override.<step_kind>. Obtain capability contracts and permitted connection references from describe.",
            "definition_guidance": self._definition_guidance(mode),
        }

    @staticmethod
    def _step_profile(profile: dict[str, Any]) -> dict[str, Any]:
        kind = profile["step_kind_key"]
        schema = deepcopy(profile.get("config_schema_jsonb") or {})
        allowed = STEP_CONFIG_ALLOWED_KEYS.get(kind, frozenset())
        properties = schema.get("properties") or {}
        wrapped = properties.get(kind)
        fields = wrapped.get("properties", {}) if isinstance(wrapped, dict) else properties
        locations = {
            field: f"step.{field}"
            if field in _STEP_REFERENCE_FIELDS.get(kind, frozenset())
            and not isinstance(wrapped, dict)
            else f"step.override.{kind}.{field}"
            for field in fields
        }
        result = {
            "step_kind": kind,
            "display_name": profile["display_name"],
            "description": profile["description"],
            "profile_schema": schema,
            "profile_schema_guidance": "This existing profile schema describes fields at the locations below; it is not a complete workflow or override schema.",
            "field_locations": locations,
            "config_path": f"override.{kind}",
            "allowed_config_keys": sorted(allowed),
        }
        if kind in WAITING_STEP_KINDS:
            result["outcomes"] = [outcome.name for outcome in step_outcomes(kind, {})] + [
                OUTCOME_ERROR
            ]
        if kind == "connector":
            result["example_step"] = {
                "step_id": "send",
                "step_kind": kind,
                "connector_id": "installed_tool",
                "connector_install_id": "installed_extension",
                "override": {kind: {"operation_id": "send"}},
            }
        elif kind == "approval":
            result["review_guidance"] = _APPROVAL_REVIEW_GUIDANCE
            result["example_step"] = {
                "step_id": "check_before_accounting",
                "step_kind": kind,
                "override": {
                    kind: {
                        "message": "Compare the recognized document with the original",
                        "approve_label": "Export to accounting",
                        "reject_label": "Recognize again",
                        "review": {
                            "original": "state.inputs.files",
                            "fields_from_workflow": "accounting_export",
                            "fields": [
                                {
                                    "name": "supplier",
                                    "type": "string",
                                    "required": True,
                                    "editable": True,
                                    "from": "message.step.recognize.artifacts.supplier",
                                },
                                {
                                    "name": "total",
                                    "type": "number",
                                    "required": True,
                                    "editable": True,
                                    "from": "message.step.recognize.artifacts.total",
                                },
                                {
                                    "name": "confidence",
                                    "type": "number",
                                    "editable": False,
                                    "from": "message.step.recognize.artifacts.confidence",
                                },
                            ],
                        },
                    }
                },
            }
        elif kind == "agent":
            result["example_step"] = {
                "step_id": "reason",
                "step_kind": kind,
                "agent_id": "installed_agent",
                "skill_id": "installed_skill",
            }
        return result

    @staticmethod
    def _minimal_bundle() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": BUNDLE_KIND,
            "metadata": {},
            "workflows": [
                {
                    "workflow_id": "new_workflow",
                    "display_name": "New workflow",
                    "version": "1.0.0",
                    "enabled": False,
                    "trigger": {},
                    "settings": {},
                    "participants": [],
                    "tags": [],
                    "steps": [
                        {
                            "step_id": "initialize",
                            "step_kind": "set_variable",
                            "override": {
                                "set_variable": {"target": "state.started", "value": True}
                            },
                        },
                        {
                            "step_id": "done",
                            "step_kind": "stop_success",
                            "override": {"stop_success": {"message": "Complete"}},
                        },
                    ],
                    "groups": [],
                    "execution_graph": {
                        "schema_version": 1,
                        "entry": {"kind": "step", "id": "initialize"},
                        "edges": [
                            {
                                "source": {"kind": "step", "id": "initialize"},
                                "outcome": {"name": "success"},
                                "target": {"kind": "step", "id": "done"},
                                "rework": False,
                            }
                        ],
                    },
                }
            ],
            "dependencies": {},
            "import_hints": {"default_enabled": False, "import_state": "draft"},
        }

    @staticmethod
    def _node(kind: str, node_id: str) -> dict[str, str]:
        return {"kind": kind, "id": node_id}

    @classmethod
    def _edge(cls, source_kind: str, source_id: str, target: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": cls._node(source_kind, source_id),
            "outcome": {"name": "success"},
            "target": target,
            "rework": False,
        }

    @staticmethod
    def _work_step(step_id: str, **membership: str) -> dict[str, Any]:
        return {
            "step_id": step_id,
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": f"state.{step_id}", "value": True}},
            **membership,
        }

    @staticmethod
    def _hook_declaration() -> dict[str, Any]:
        return {
            "hook_id": "review_extension",
            "contract_version": 1,
            "display_name": "Review extension",
            "description": "A project may bind its own editable child workflow to this declared Hook. Creating a new Hook declaration requires Development mode.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "input_mapping": {},
            "mutable_fields": [],
            "additional_outputs": {},
            "stage_contract": {
                "summary": "Review the workflow after the preceding step.",
                "effects_before": [],
                "effects_after": [],
            },
        }

    @classmethod
    def _definition_example_bundle(cls, example_id: str) -> dict[str, Any]:
        """Small complete disabled bundles; shared validators remain authoritative."""
        bundle = cls._minimal_bundle()
        workflow = bundle["workflows"][0]
        done = workflow["steps"][1]
        edges: list[dict[str, Any]] = []
        if example_id == "hook_unbound":
            workflow["steps"] = [
                {
                    "step_id": "review",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"hook": cls._hook_declaration()}},
                },
                done,
            ]
            entry = cls._node("step", "review")
            edges.append(cls._edge("step", "review", cls._node("step", "done")))
        else:
            is_group = example_id.startswith("group_")
            is_lanes = example_id.endswith("_lanes")
            owner_id = "review_group" if is_group else "items_loop"
            owner_kind = "group" if is_group else "for_each"
            owner_field = "group_id" if is_group else "for_each_id"
            exit_kind = "group_exit" if is_group else "for_each_exit"
            members = ["left_work", "right_work"] if is_lanes else ["item_work"]
            if is_lanes:
                body: dict[str, Any] = {
                    "kind": "lanes",
                    "lanes": [
                        {"lane_id": lane, "entry": cls._node("step", step_id)}
                        for lane, step_id in zip(("left", "right"), members, strict=True)
                    ],
                }
            else:
                body = {"kind": "sequence", "entry": cls._node("step", members[0])}
            container: dict[str, Any] = {owner_field: owner_id, "body": body}
            if is_group:
                workflow["groups"] = [container]
            else:
                container.update(
                    {"over": "state.items", "item_name": "item", "index_name": "item_index"}
                )
                workflow["for_each"] = [container]
            workflow["steps"] = [
                cls._work_step(
                    step_id,
                    **{owner_field: owner_id},
                    **({"lane_id": lane} if is_lanes else {}),
                )
                for lane, step_id in zip(
                    ("left", "right") if is_lanes else ("",), members, strict=True
                )
            ] + [done]
            entry = cls._node(owner_kind, owner_id)
            for lane, step_id in zip(
                ("left", "right") if is_lanes else (None,), members, strict=True
            ):
                edges.append(
                    cls._edge(
                        "step",
                        step_id,
                        {"kind": exit_kind, owner_field: owner_id, "lane_id": lane},
                    )
                )
            edges.append(cls._edge(owner_kind, owner_id, cls._node("step", "done")))
        workflow["execution_graph"] = {"schema_version": 1, "entry": entry, "edges": edges}
        return bundle

    @classmethod
    def _operation_examples(cls, *, hooks_allowed: bool = True) -> list[OperationExample]:
        def step_ref(node_id: str) -> dict[str, str]:
            return cls._node("step", node_id)

        group_ref = cls._node("group", "review_group")
        loop_ref = cls._node("for_each", "items_loop")
        group_and_loop: list[dict[str, Any]] = [
            {
                "op": "create_group",
                "group": {"group_id": "review_group", "body": {"kind": "sequence"}},
            },
            {
                "op": "create_for_each",
                "for_each": {
                    "for_each_id": "items_loop",
                    "group_id": "review_group",
                    "over": "state.items",
                    "item_name": "item",
                    "index_name": "item_index",
                    "body": {"kind": "sequence"},
                },
            },
            {"op": "create_step", "step": cls._work_step("item_work", for_each_id="items_loop")},
            {"op": "set_entry", "group_id": "review_group", "target": loop_ref},
            {"op": "set_entry", "for_each_id": "items_loop", "target": step_ref("item_work")},
            {
                "op": "connect",
                "edge": cls._edge(
                    "step",
                    "item_work",
                    {"kind": "for_each_exit", "for_each_id": "items_loop", "lane_id": None},
                ),
            },
            {
                "op": "connect",
                "edge": cls._edge(
                    "for_each",
                    "items_loop",
                    {"kind": "group_exit", "group_id": "review_group", "lane_id": None},
                ),
            },
            {"op": "connect", "edge": cls._edge("step", "initialize", group_ref), "replace": True},
            {"op": "connect", "edge": cls._edge("group", "review_group", step_ref("done"))},
        ]
        root_hook: list[dict[str, Any]] = [
            {
                "op": "create_step",
                "step": {
                    "step_id": "review",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"hook": cls._hook_declaration()}},
                },
            },
            {
                "op": "connect",
                "edge": cls._edge("step", "initialize", step_ref("review")),
                "replace": True,
            },
            {"op": "connect", "edge": cls._edge("step", "review", step_ref("done"))},
        ]
        report: list[dict[str, Any]] = [
            {
                "op": "create_step",
                "step": {
                    "step_id": "build_report",
                    "step_kind": "template",
                    "override": {"template": deepcopy(TEMPLATE_REPORT_EXAMPLE)},
                },
            },
            {
                "op": "connect",
                "edge": cls._edge("step", "initialize", step_ref("build_report")),
                "replace": True,
            },
            {"op": "connect", "edge": cls._edge("step", "build_report", step_ref("done"))},
            {
                "op": "update_interface",
                "patch": {
                    "enabled": True,
                    "trigger_channels": ["ui", "api", "mcp"],
                    "expected_outputs": [
                        {"name": "sales_report", "type": "object", "required": True}
                    ],
                    "report_output": "sales_report",
                },
            },
        ]
        precondition = yaml.safe_dump(cls._minimal_bundle(), sort_keys=False)
        revision = "Read the current positive workflow revision; use that exact value as expected_revision."
        examples: list[OperationExample] = [
            {
                "id": "group_for_each_atomic",
                "purpose": "Create a sequence Group containing a For each and connect its body in one change set.",
                "limitations": _GROUP_NOT_CONVERTIBLE
                + " state.items is not written with a declared type here either, so its For each cannot move into a child until a step writes it with output_types.",
                "precondition_bundle_yaml": precondition,
                "expected_revision_guidance": revision,
                "dry_run": True,
                "operations": group_and_loop,
            },
            {
                "id": "root_hook_atomic",
                "purpose": "Declare an unbound root Hook in Development mode and connect its Invoke carrier.",
                "limitations": _HOOK_DEVELOPMENT_ONLY,
                "precondition_bundle_yaml": precondition,
                "expected_revision_guidance": revision,
                "dry_run": True,
                "operations": root_hook,
            },
            {
                "id": "report_output_atomic",
                "purpose": "Build a report with a Template step in format report and publish it as the workflow's report output in one change set.",
                "limitations": "",
                "precondition_bundle_yaml": precondition,
                "expected_revision_guidance": revision,
                "dry_run": True,
                "operations": report,
            },
        ]
        if not hooks_allowed:
            examples = [row for row in examples if row["id"] != "root_hook_atomic"]
        return examples

    @classmethod
    def _definition_guidance(cls, mode: str = "production") -> DefinitionGuidance:
        # Outside Development a new Hook declaration is refused everywhere, so the
        # examples that create one are not offered (MCP probe F12).
        hooks_allowed = mode == "development"
        purposes = {
            "group_sequence": "Run one Group body in sequence.",
            "group_lanes": "Run one Group body with two parallel lanes.",
            "for_each_sequence": "Visit a collection with a sequence For each body.",
            "for_each_lanes": "Visit a collection with two For each lanes.",
        }
        if hooks_allowed:
            purposes["hook_unbound"] = (
                "Declare a root Hook without a direct child target or project binding."
            )
        examples: list[DefinitionExample] = []
        instructional = "Instructional disabled bundle; use workflow_draft_validate before saving or enabling it."
        for example_id, purpose in purposes.items():
            limitations = instructional
            if example_id.startswith("group_"):
                limitations = f"{instructional} {_GROUP_NOT_CONVERTIBLE}"
            examples.append(
                {
                    "id": example_id,
                    "purpose": purpose,
                    "prerequisites": _HOOK_DEVELOPMENT_ONLY
                    if example_id == "hook_unbound"
                    else "Import as a disabled draft and review its collection/state inputs.",
                    "limitations": limitations,
                    "bundle_yaml": yaml.safe_dump(
                        cls._definition_example_bundle(example_id), sort_keys=False
                    ),
                }
            )
        return {
            "contract_level": "partial_guidance_and_examples",
            "validation_guidance": "These examples are partial authoring guidance, not complete write schemas. workflow_draft_validate and workflow_apply_changes with dry_run=true remain authoritative.",
            "models": {
                "group": {
                    "persisted_location": "workflow.groups",
                    "description": "A Group runs its sequence or parallel lanes once; body membership, entry and exit refs are part of the execution graph.",
                },
                "for_each": {
                    "persisted_location": "workflow.for_each",
                    "description": "For each iterates an over collection with item/index names; its sequence or lanes have explicit body entry and exit refs.",
                },
                "interface": {
                    "persisted_location": "workflow.settings.callable",
                    "description": "The public boundary: trigger_channels, expected_inputs and expected_outputs. A field may also declare its shape: items for an array element {type, options?, format?, properties?} (several files: type array with items.type file), properties for an object (sub-fields in the same model, at most 3 levels deep), min_items/max_items for an array, and format (date, date-time, time, email, uri) for string or paragraph; each is checked only when declared. report_output names one required output of type object that holds a report snapshot written by a Template step in format report (ADR 0059). Change it with the update_interface operation (a shallow patch; null removes a key), never with a raw settings write.",
                },
                "hook": {
                    "persisted_location": "steps[].override.invoke_workflow.hook",
                    "description": "A new Hook is a root Invoke declaration in Development mode, with typed inputs/outputs and stage effects. A project binding is separate from the author definition revision; direct workflow_id is mutually exclusive with hook."
                    + (
                        ""
                        if hooks_allowed
                        else f" This installation runs in {mode} mode: a new Hook declaration is refused (workflow.hook.declaration_requires_development), so no Hook example is offered; Hooks that already exist stay editable and bindable."
                    ),
                },
            },
            "examples": examples,
            "change_set": {
                "schema_scope": "envelope_only_operations_are_open_objects",
                "request_envelope_schema": WorkflowChangeSet.model_json_schema(),
                "operation_names": sorted(OPERATION_KINDS),
                "partial_examples": cls._operation_examples(hooks_allowed=hooks_allowed),
            },
        }
