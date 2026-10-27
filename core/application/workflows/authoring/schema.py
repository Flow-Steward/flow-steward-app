"""Existing portability and persisted step configuration guidance."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

import yaml

from core.application.management.errors import ServiceNotFoundError
from core.application.registry.builtin_step_kind_profiles import builtin_step_kind_profiles
from core.application.workflows.execution_graph import OUTCOME_ERROR
from core.application.workflows.workflow_authoring_requests import WorkflowChangeSet
from core.application.workflows.workflow_definition_changes import OPERATION_KINDS
from core.application.workflows.workflow_definition_validation import (
    WAITING_STEP_KINDS,
    step_outcomes,
)
from core.application.workflows.workflow_portability import BUNDLE_KIND, SCHEMA_VERSION
from core.application.workflows.workflow_step_config_contracts import STEP_CONFIG_ALLOWED_KEYS

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
    minimal_bundle_yaml: str
    step_kinds: list[dict[str, Any]]
    mapping_guidance: str
    definition_guidance: DefinitionGuidance


class WorkflowAuthoringSchema:
    """Expose existing schemas with the persisted locations their consumers read."""

    def get(self, *, step_kind: str | None = None) -> AuthoringSchemaResult:
        profiles = [profile for profile in builtin_step_kind_profiles() if profile.get("enabled")]
        if step_kind:
            profiles = [profile for profile in profiles if profile["step_kind_key"] == step_kind]
            if not profiles:
                raise ServiceNotFoundError("Unknown step kind")
        return {
            "format": SCHEMA_VERSION,
            "kind": BUNDLE_KIND,
            "minimal_bundle_yaml": yaml.safe_dump(self._minimal_bundle(), sort_keys=False),
            "step_kinds": [self._step_profile(profile) for profile in profiles],
            "mapping_guidance": "Use step.input_mapping_jsonb and step.output_mapping_jsonb; runtime configuration belongs in step.override.<step_kind>. Obtain capability contracts and permitted connection references from describe.",
            "definition_guidance": self._definition_guidance(),
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
    def _operation_examples(cls) -> list[OperationExample]:
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
        precondition = yaml.safe_dump(cls._minimal_bundle(), sort_keys=False)
        revision = "Read the current positive workflow revision; use that exact value as expected_revision."
        return [
            {
                "id": "group_for_each_atomic",
                "purpose": "Create a sequence Group containing a For each and connect its body in one change set.",
                "precondition_bundle_yaml": precondition,
                "expected_revision_guidance": revision,
                "dry_run": True,
                "operations": group_and_loop,
            },
            {
                "id": "root_hook_atomic",
                "purpose": "Declare an unbound root Hook in Development mode and connect its Invoke carrier.",
                "precondition_bundle_yaml": precondition,
                "expected_revision_guidance": revision,
                "dry_run": True,
                "operations": root_hook,
            },
        ]

    @classmethod
    def _definition_guidance(cls) -> DefinitionGuidance:
        purposes = {
            "group_sequence": "Run one Group body in sequence.",
            "group_lanes": "Run one Group body with two parallel lanes.",
            "for_each_sequence": "Visit a collection with a sequence For each body.",
            "for_each_lanes": "Visit a collection with two For each lanes.",
            "hook_unbound": "Declare a root Hook without a direct child target or project binding.",
        }
        examples: list[DefinitionExample] = []
        for example_id, purpose in purposes.items():
            examples.append(
                {
                    "id": example_id,
                    "purpose": purpose,
                    "prerequisites": "Development installation mode is required for a new Hook declaration."
                    if example_id == "hook_unbound"
                    else "Import as a disabled draft and review its collection/state inputs.",
                    "limitations": "Instructional disabled bundle; use workflow_draft_validate before saving or enabling it.",
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
                "hook": {
                    "persisted_location": "steps[].override.invoke_workflow.hook",
                    "description": "A new Hook is a root Invoke declaration in Development mode, with typed inputs/outputs and stage effects. A project binding is separate from the author definition revision; direct workflow_id is mutually exclusive with hook.",
                },
            },
            "examples": examples,
            "change_set": {
                "schema_scope": "envelope_only_operations_are_open_objects",
                "request_envelope_schema": WorkflowChangeSet.model_json_schema(),
                "operation_names": sorted(OPERATION_KINDS),
                "partial_examples": cls._operation_examples(),
            },
        }
