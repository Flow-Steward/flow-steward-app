"""Schemas describe the persisted format and their sample is an importable bundle."""

from __future__ import annotations

import pytest
import yaml

from core.application.workflows.authoring.schema import WorkflowAuthoringSchema
from core.application.workflows.workflow_authoring_requests import WorkflowChangeSet
from core.application.workflows.workflow_definition_changes import OPERATION_KINDS
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_hook_contract import (
    invoke_config_issues,
    parse_hook_declaration,
)
from core.application.workflows.workflow_portability import (
    BUNDLE_KIND,
    SCHEMA_VERSION,
    WorkflowPortabilityService,
)
from core.application.workflows.workflow_step_config_contracts import (
    STEP_CONFIG_ALLOWED_KEYS,
    validate_workflow_step_config_keys,
)


def test_schema_sample_is_complete_roundtrippable_bundle_with_explicit_disabled_workflow():
    result = WorkflowAuthoringSchema().get()
    bundle = WorkflowPortabilityService(object()).parse_bundle(result["minimal_bundle_yaml"])
    assert len(bundle["workflows"]) == 1
    assert bundle["workflows"][0]["workflow_id"] == "new_workflow"
    assert bundle["workflows"][0]["enabled"] is False
    assert validate_workflow_step_config_keys(bundle["workflows"][0]) == []
    assert yaml.safe_load(yaml.safe_dump(bundle)) == bundle


def test_step_schema_uses_runtime_override_wrapper_and_config_keys():
    result = WorkflowAuthoringSchema().get(step_kind="set_variable")
    step = result["step_kinds"][0]
    assert step["step_kind"] == "set_variable"
    assert step["config_path"] == "override.set_variable"
    assert "target" in step["allowed_config_keys"]
    assert "value" in step["allowed_config_keys"]
    assert "params_schema" not in step


def test_connector_and_agent_profile_reference_fields_live_on_step_not_override():
    schemas = WorkflowAuthoringSchema()
    connector = schemas.get(step_kind="connector")["step_kinds"][0]
    assert connector["field_locations"]["connector_id"] == "step.connector_id"
    assert connector["field_locations"]["connector_install_id"] == "step.connector_install_id"
    assert connector["field_locations"]["operation_id"] == "step.override.connector.operation_id"
    assert "connector_id" not in connector["allowed_config_keys"]
    agent = schemas.get(step_kind="agent")["step_kinds"][0]
    assert agent["field_locations"]["agent_id"] == "step.agent_id"
    assert agent["field_locations"]["skill_id"] == "step.skill_id"
    assert "agent_id" not in agent["allowed_config_keys"]
    # The returned persisted example catches incorrectly placing profile reference fields.
    assert (
        validate_workflow_step_config_keys(
            {"phases": [{"steps": [connector["example_step"], agent["example_step"]]}]}
        )
        == []
    )


def test_invoke_workflow_reference_location_matches_runtime_override():
    schema = WorkflowAuthoringSchema().get(step_kind="invoke_workflow")["step_kinds"][0]
    assert schema["field_locations"]["workflow_id"] == "step.override.invoke_workflow.workflow_id"


@pytest.mark.parametrize(
    "kind, fields, outcomes",
    [
        ("wait", {"duration_ms", "input_mapping"}, {"success", "error"}),
        (
            "approval",
            {"message", "show_outputs", "output_mapping", "response_mapping"},
            {"approved", "rejected", "error"},
        ),
    ],
)
def test_durable_step_schemas_are_discoverable_with_config_and_outcomes(kind, fields, outcomes):
    schemas = WorkflowAuthoringSchema()
    assert kind in {row["step_kind"] for row in schemas.get()["step_kinds"]}
    profile = schemas.get(step_kind=kind)["step_kinds"][0]
    assert set(profile["allowed_config_keys"]) == fields
    assert profile["field_locations"] == {
        field: f"step.override.{kind}.{field}" for field in fields
    }
    assert set(profile["outcomes"]) == outcomes


def test_definition_discovery_is_additive_and_describes_real_collections():
    schemas = WorkflowAuthoringSchema()
    full = schemas.get()
    filtered = schemas.get(step_kind="set_variable")
    guidance = full["definition_guidance"]

    assert filtered["definition_guidance"] == guidance
    assert guidance["contract_level"] == "partial_guidance_and_examples"
    assert set(guidance["models"]) == {"group", "for_each", "hook"}
    assert {name: model["persisted_location"] for name, model in guidance["models"].items()} == {
        "group": "workflow.groups",
        "for_each": "workflow.for_each",
        "hook": "steps[].override.invoke_workflow.hook",
    }
    for model in guidance["models"].values():
        assert isinstance(model["description"], str)
        assert model["description"].strip()

    names = guidance["change_set"]["operation_names"]
    assert set(names) == OPERATION_KINDS
    assert len(names) == len(OPERATION_KINDS)
    assert not set(guidance["models"]).intersection(
        profile["step_kind"] for profile in full["step_kinds"]
    )
    assert full["format"] == SCHEMA_VERSION
    assert full["kind"] == BUNDLE_KIND
    assert full["mapping_guidance"]
    for field in ("format", "kind", "minimal_bundle_yaml", "mapping_guidance"):
        assert filtered[field] == full[field]
    assert filtered["step_kinds"] == [
        profile for profile in full["step_kinds"] if profile["step_kind"] == "set_variable"
    ]
    assert len(filtered["step_kinds"]) == 1
    assert filtered["step_kinds"][0]["config_path"] == "override.set_variable"
    assert (
        set(filtered["step_kinds"][0]["allowed_config_keys"])
        == STEP_CONFIG_ALLOWED_KEYS["set_variable"]
    )


def test_discovery_uses_current_change_envelope_schema_without_claiming_operation_schema():
    result = WorkflowAuthoringSchema().get()
    guidance = result["definition_guidance"]
    change_set = guidance["change_set"]

    assert guidance["contract_level"] == "partial_guidance_and_examples"
    assert change_set["schema_scope"] == "envelope_only_operations_are_open_objects"
    envelope = change_set["request_envelope_schema"]
    assert envelope == WorkflowChangeSet.model_json_schema()
    assert envelope["type"] == "object"
    assert envelope["additionalProperties"] is False
    assert set(envelope["required"]) == {"expected_revision", "operations"}
    properties = envelope["properties"]
    assert properties["expected_revision"]["type"] == "integer"
    assert properties["expected_revision"]["minimum"] == 1
    assert properties["operations"]["type"] == "array"
    assert properties["operations"]["minItems"] == 1
    assert properties["dry_run"]["type"] == "boolean"
    assert properties["dry_run"]["default"] is False
    operation = properties["operations"]["items"]
    assert operation["type"] == "object"
    assert operation.get("additionalProperties", True) is True
    assert not {"properties", "required", "oneOf", "anyOf", "$ref"}.intersection(operation)
    for model in guidance["models"].values():
        assert not {"schema", "json_schema", "$schema", "properties", "required"}.intersection(
            model
        )


@pytest.mark.parametrize(
    "example_id",
    ["group_sequence", "group_lanes", "for_each_sequence", "for_each_lanes", "hook_unbound"],
)
def test_discovery_examples_satisfy_current_shared_definition_contract(example_id):
    examples = WorkflowAuthoringSchema().get()["definition_guidance"]["examples"]
    example = next(row for row in examples if row["id"] == example_id)
    bundle = WorkflowPortabilityService(object()).parse_bundle(example["bundle_yaml"])
    assert len(bundle["workflows"]) == 1
    candidate = bundle["workflows"][0]
    assert candidate["enabled"] is False
    definition = {
        "steps": candidate["steps"],
        "groups": candidate.get("groups", []),
        "for_each": candidate.get("for_each", []),
        "execution_graph": candidate["execution_graph"],
    }
    assert validate_workflow_definition(definition) == []
    if example_id == "hook_unbound":
        hook_steps = [step for step in candidate["steps"] if step["step_kind"] == "invoke_workflow"]
        assert len(hook_steps) == 1
        config = hook_steps[0]["override"]["invoke_workflow"]
        assert "workflow_id" not in config
        assert invoke_config_issues(config) == []
        assert parse_hook_declaration(config["hook"]).hook_id == "review_extension"
