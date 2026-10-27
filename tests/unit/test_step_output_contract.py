"""Selection parity and immutable declaration resolution, independent of result keys."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from core.application.registry.io_resolver import IOResolver
from core.application.registry.step_output_contract import (
    OutputContractResolutionError,
    resolve_step_output_contract,
)


class ContractStore:
    def __init__(self):
        self.skill = {
            "skill_id": "selected",
            "display_name": "Selected",
            "enabled": True,
            "_outputs": [{"name": "summary", "type": "text", "required": True}],
            "output_schema": {},
        }
        self.own = []
        self.legacy = []
        self.blocks = []

    def list_skills(self):
        return [self.skill]

    def get_skill(self, _):
        return self.skill

    def get_skill_outputs(self, _):
        return self.skill["_outputs"]

    def get_agent(self, _):
        return {"agent_id": "writer", "outputs": self.legacy, "_skills": [{"skill_id": "selected"}]}

    def get_agent_own_outputs(self, _):
        return self.own

    def get_agent_skills(self, _):
        return [{"skill_id": "selected"}]

    def list_published_block_contracts(self, *, enabled_only):
        return [row for row in self.blocks if not enabled_only or row.get("enabled", True)]

    def get_published_block_contract(self, key, *, account_id=None):
        return next(
            (
                row
                for row in self.blocks
                if row["block_key"] == key
                and (not row.get("account_id") or row["account_id"] == account_id)
            ),
            None,
        )


def resolve(store, **kwargs):
    return resolve_step_output_contract(
        store, agent_id="writer", skill_id="selected", account_id="account-a", **kwargs
    )


def block(key="public", source="writer::selected", account="account-a", outputs=None):
    return {
        "block_key": key,
        "source_kind": "agent",
        "source_ref": source,
        "enabled": True,
        "account_id": account,
        "outputs": [] if outputs is None else outputs,
    }


def test_same_exposed_selection_is_used_by_readiness_and_runtime():
    store = ContractStore()
    store.blocks = [
        block("foreign", account="account-b", outputs=[{"field_key": "private"}]),
        block(
            "global",
            source="selected",
            account=None,
            outputs=[
                {"field_key": "published", "value_type": "number", "exposed_by_default": True},
                {"field_key": "hidden", "exposed_by_default": False},
            ],
        ),
    ]
    captured = resolve(store)
    declared = IOResolver(store).get_step_declared_outputs(
        {"step_id": "write", "step_kind": "agent", "agent_id": "writer", "skill_id": "selected"},
        workflow_account_id="account-a",
    )
    assert [row["name"] for row in captured.rows] == ["published"]
    assert [(row["name"], row["type"]) for row in declared] == [("published", "number")]
    assert captured.validate({"published": 42}) == []
    assert captured.validate({"published": "wrong"}) == [
        "Invalid type or value for output 'published'"
    ]


@pytest.mark.parametrize("explicit", [False, True])
def test_valid_empty_exposure_does_not_fall_back_to_skill(explicit):
    store = ContractStore()
    store.blocks = [block()]
    assert resolve(store, block_key="public" if explicit else "").rows == []


@pytest.mark.parametrize("variant", ["missing", "foreign", "disabled", "unrelated"])
def test_explicit_unavailable_block_fails_resolution(variant):
    store = ContractStore()
    if variant != "missing":
        store.blocks = [block(account="other" if variant == "foreign" else "account-a")]
        store.blocks[0]["enabled"] = variant != "disabled"
        if variant == "unrelated":
            store.blocks[0]["source_ref"] = "other::skill"
    with pytest.raises(OutputContractResolutionError, match="unavailable"):
        resolve(store, block_key="public")


def test_schema_only_selected_skill_and_true_own_declarations():
    store = ContractStore()
    store.skill["_outputs"] = []
    store.skill["output_schema"] = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    store.own = [
        {"name": "own", "type": "boolean", "source": "own"},
        {"name": "tool", "type": "text", "source": "tool"},
        {"name": "secondary", "source": "skill"},
    ]
    store.legacy = ["legacy"]
    contract = resolve(store)
    assert [row["name"] for row in contract.rows] == ["count", "legacy", "own"]
    assert contract.validate({"count": 0, "own": False, "legacy": "own output"}) == []
    assert contract.validate({"count": "zero"}) == ["Invalid type or value for output 'count'"]


def test_capture_detaches_both_schema_and_returned_rows():
    store = ContractStore()
    first = resolve(store)
    original = copy.deepcopy(first.rows)
    store.skill["_outputs"][0]["name"] = "changed"
    first.rows[0]["name"] = "also changed"
    assert first.rows == original
    assert first.digest != resolve(store).digest
    assert first.validate({"summary": "original"}) == []


def test_conflicting_types_and_lookup_errors_are_not_valid_empty_contracts():
    store = ContractStore()
    store.own = [{"name": "summary", "type": "number"}]
    with pytest.raises(OutputContractResolutionError, match="Conflicting"):
        resolve(store)
    store.own = []
    store.get_skill = lambda _: (_ for _ in ()).throw(RuntimeError("DB unavailable"))
    with pytest.raises(OutputContractResolutionError, match="Could not resolve"):
        resolve(store)


def test_source_erased_materialized_manifest_outputs_are_not_own_declarations():
    store = ContractStore()
    manifest = SimpleNamespace(outputs=[], structured_outputs=[{"name": "tool", "type": "text"}])
    assert [row["name"] for row in resolve(store, agent_manifest=manifest).rows] == ["summary"]


def test_readiness_lookup_failure_is_not_swallowed_by_preview_fallback():
    store = ContractStore()
    calls = []

    def lookup(**_kwargs):
        calls.append("lookup")
        raise RuntimeError("unavailable")

    store.list_published_block_contracts = lookup
    with pytest.raises(OutputContractResolutionError, match="Could not resolve"):
        IOResolver(store).get_step_declared_outputs(
            {
                "step_id": "write",
                "step_kind": "agent",
                "agent_id": "writer",
                "skill_id": "selected",
            },
            workflow_account_id="account-a",
        )
    assert calls == ["lookup"]


def test_legacy_preview_scope_does_not_become_output_authority():
    from core.application.registry.published_block_selection import select_agent_block_key

    rows = [block()]
    assert (
        select_agent_block_key(rows, agent_id="writer", skill_id="selected", account_id=None)
        == "public"
    )
    assert select_agent_block_key(rows, agent_id="writer", skill_id="selected", account_id="") == ""


@pytest.mark.parametrize(
    ("field_type", "example"),
    [("json", {}), ("list", []), ("integer", 0), ("boolean", False), ("null", None)],
)
def test_captured_prompt_examples_follow_normalized_contract_types(field_type, example):
    import json

    from core.application.orchestration.agents.prompt_builder import PromptBuilder

    store = ContractStore()
    store.skill["_outputs"] = [{"name": "result", "type": field_type, "required": False}]
    contract = resolve(store)
    text = PromptBuilder()._build_phase_result_v2_requirements("selected", output_contract=contract)
    envelope = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    assert envelope["artifacts"]["result"] == example
    assert type(envelope["artifacts"]["result"]) is type(example)
    assert f"`result` ({contract.rows[0]['type']}, optional)" in text


@pytest.mark.parametrize("precise", [False, True])
def test_generic_producer_json_and_authored_object_keep_distinct_readiness_types(precise):
    from core.application.orchestration.models import build_phase_result_json_schema

    store = ContractStore()
    store.skill["_outputs"] = [{"name": "result", "type": "json", "required": True}]
    if precise:
        store.skill["output_schema"] = {
            "type": "object",
            "properties": {"result": {"type": "object"}},
            "required": ["result"],
        }
    contract = resolve(store)
    assert contract.rows[0]["type"] == ("object" if precise else "json")
    schema = build_phase_result_json_schema(contract.rows)
    assert schema["properties"]["artifacts"]["properties"]["result"]["type"] == "object"
    assert contract.validate({"result": {}}) == []
    assert contract.validate({"result": "scalar"}) == (
        ["Invalid type or value for output 'result'"] if precise else []
    )
    outputs = IOResolver(store).get_step_declared_outputs(
        {"step_id": "produce", "agent_id": "writer", "skill_id": "selected"},
        workflow_account_id="account-a",
    )
    assert outputs[0]["type"] == ("object" if precise else "json")


@pytest.mark.parametrize("kind", ["json", "list"])
def test_captured_response_defaults_equal_legacy_without_becoming_validation_constraints(kind):
    from core.application.orchestration.models import build_phase_result_json_schema
    from core.application.registry.step_output_contract import StepOutputContract

    store = ContractStore()
    rows = [{"name": "result", "type": kind, "required": False}]
    store.skill["_outputs"] = rows
    contract = resolve(store)
    assert build_phase_result_json_schema(contract.rows) == build_phase_result_json_schema(rows)
    for value in (
        [False, 0, None, [], {}, "", "scalar"] if kind == "json" else [[], [False, 0, {"a": 1}]]
    ):
        assert contract.validate({"result": value}) == []
    for key in ("contract_schema", "response_schema"):
        changed = contract.rows
        changed[0][key]["description"] = "mutated copy"
        assert "description" not in contract.rows[0][key]
        with pytest.raises(ValueError, match="digest mismatch"):
            StepOutputContract.from_receipt(changed, contract.digest)
    restored = StepOutputContract.from_receipt(contract.rows, contract.digest)
    assert restored.rows == contract.rows


@pytest.mark.parametrize("schema_only", [False, True])
def test_authored_precise_schema_views_are_exact_and_reject_wrong_shape(schema_only):
    from core.application.orchestration.models import build_phase_result_json_schema

    store = ContractStore()
    store.skill["_outputs"] = (
        [] if schema_only else [{"name": "result", "type": "json", "required": True}]
    )
    authored = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
        "additionalProperties": False,
    }
    store.skill["output_schema"] = {
        "type": "object",
        "properties": {"result": authored},
        "required": ["result"],
    }
    contract = resolve(store)
    assert contract.rows[0]["contract_schema"] == authored
    assert contract.rows[0]["response_schema"] == authored
    assert (
        build_phase_result_json_schema(contract.rows)["properties"]["artifacts"]["properties"][
            "result"
        ]
        == authored
    )
    assert contract.validate({"result": {"n": 0}}) == []
    for wrong in ["scalar", {"n": "zero"}, {}, {"n": 0, "extra": True}]:
        assert contract.validate({"result": wrong}) == ["Invalid type or value for output 'result'"]
    authored["properties"]["n"]["type"] = "string"
    assert contract.validate({"result": {"n": 0}}) == []


@pytest.mark.parametrize(
    ("mode", "origin", "reference", "schema", "generic"),
    [
        (
            "derived",
            "field_shape",
            "result",
            {"type": "object", "additionalProperties": True},
            True,
        ),
        (
            "manual",
            "field_shape",
            "result",
            {"type": "object", "additionalProperties": True},
            False,
        ),
        ("derived", "extension", "result", {"type": "object", "additionalProperties": True}, False),
        (
            "derived",
            "field_shape",
            "different",
            {"type": "object", "additionalProperties": True},
            False,
        ),
        ("derived", "field_shape", "result", {"type": "object", "required": ["n"]}, False),
    ],
)
def test_only_proven_generated_linked_field_defaults_remain_generic(
    mode, origin, reference, schema, generic
):
    store = ContractStore()
    store.blocks = [
        block(outputs=[{"field_key": "result", "value_type": "json", "data_contract_id": "shape"}])
    ]
    store.get_data_contract = lambda _: {
        "projection_mode": mode,
        "derived_from_kind": origin,
        "derived_from_ref": reference,
        "kind": "json",
        "schema_json": schema,
        "enabled": True,
    }
    contract = resolve(store)
    assert contract.rows[0]["type"] == ("json" if generic else "object")
    assert bool(contract.validate({"result": "scalar"})) is not generic
    assert contract.rows[0]["response_schema"] == schema


@pytest.mark.parametrize("invalid", ["missing", "disabled", "foreign", "invalid_schema"])
def test_linked_contract_availability_and_authored_schema_fail_closed(invalid):
    store = ContractStore()
    store.blocks = [
        block(outputs=[{"field_key": "result", "value_type": "json", "data_contract_id": "shape"}])
    ]
    row = {"projection_mode": "manual", "schema_json": {"type": "object"}, "enabled": True}
    if invalid == "disabled":
        row["enabled"] = False
    elif invalid == "foreign":
        row["account_id"] = "account-b"
    elif invalid == "invalid_schema":
        row["schema_json"] = {"type": "json"}
    store.get_data_contract = lambda _: None if invalid == "missing" else row
    with pytest.raises(OutputContractResolutionError):
        resolve(store)


@pytest.mark.parametrize(
    ("kind", "value"), [("text", "text"), ("json", 0), ("list", [1, False, {}])]
)
def test_no_block_legacy_skill_metadata_is_normalized_at_the_actual_generator_boundary(kind, value):
    from core.application.orchestration.models import build_phase_result_json_schema
    from core.application.registry.db_store import RegistryDBStore
    from core.application.registry.skill_manifest import SkillManifest

    store = ContractStore()
    store._build_contract_schema = RegistryDBStore._build_contract_schema
    rows = [{"name": "result", "type": kind, "required": True}]
    store.skill["_outputs"] = rows
    store.skill["output_schema"] = store._build_contract_schema(rows)
    selected = SkillManifest.from_db_row(store.skill)
    runtime = resolve(store, skill_manifest=selected)
    readiness = resolve(store)
    assert runtime.rows == readiness.rows
    assert runtime.validate({"result": value}) == []
    assert build_phase_result_json_schema(runtime.rows) == build_phase_result_json_schema(rows)


SCHEMA_ONLY_ALTERNATIVES = [
    {"type": ["string", "number"]},
    {"type": ["number", "string"]},
    {"anyOf": [{"type": "string", "minLength": 2}, {"type": "number", "minimum": 0}]},
]


@pytest.mark.parametrize(
    "schema", SCHEMA_ONLY_ALTERNATIVES, ids=["union", "reversed", "composition"]
)
@pytest.mark.parametrize("value", ["authored", 0, False], ids=["string", "number", "invalid"])
def test_schema_only_alternatives_preserve_exact_authority(schema, value):
    store = ContractStore()
    store.skill["_outputs"] = []
    store.skill["output_schema"] = {
        "type": "object",
        "properties": {"result": schema},
        "required": ["result"],
    }
    contract = resolve(store)
    assert contract.rows[0]["type"] == "json"
    assert contract.rows[0]["contract_schema"] == schema
    assert contract.rows[0]["response_schema"] == schema
    assert bool(contract.validate({"result": value})) is (value is False)


@pytest.mark.parametrize(
    "target",
    [
        "summary",
        "state.summary",
        "outputs.summary",
        "state.outputs.summary",
        "state.workflow_data.summary",
        "state.workflow_data.outputs.summary",
    ],
)
def test_editor_mapping_aliases_keep_text_without_mutating_captured_schema(target):
    store = ContractStore()
    store.skill["_outputs"] = [{"name": "result", "type": "text", "required": True}]
    contract = resolve(store)
    rows = IOResolver(store).get_step_declared_outputs(
        {
            "step_id": "write",
            "step_kind": "agent",
            "agent_id": "writer",
            "skill_id": "selected",
            "output_mapping_jsonb": {"result": target},
        },
        workflow_account_id="account-a",
    )
    by_name = {row["name"]: row for row in rows}
    assert by_name[target.removeprefix("state.")]["type"] == "text"
    assert by_name["result"]["type"] == "text"
    assert contract.rows[0]["type"] == "string"
    assert contract.rows[0]["contract_schema"] == {"type": "string"}


@pytest.mark.parametrize("kind", ["object", "array", "integer", "json"])
def test_editor_projection_keeps_precise_and_generic_non_text_classifications(kind):
    store = ContractStore()
    store.skill["_outputs"] = [{"name": "result", "type": kind}]
    rows = IOResolver(store).get_step_declared_outputs(
        {
            "step_id": "write",
            "step_kind": "agent",
            "agent_id": "writer",
            "skill_id": "selected",
        },
        workflow_account_id="account-a",
    )
    assert rows[0]["type"] == kind


@pytest.mark.parametrize(
    "source",
    ["skill", "agent", "schema_only", "block_notes", "block_field", "block_name", "legacy"],
)
def test_authored_descriptions_keep_their_source_and_are_detached(source):
    from core.application.orchestration.models import build_phase_result_json_schema
    from core.application.registry.step_output_contract import StepOutputContract

    store = ContractStore()
    store.skill["_outputs"] = []
    authored = "State output written by set_variable"
    row = {"name": "result", "type": "text", "description": authored, "required": True}
    if source == "skill":
        store.skill["_outputs"] = [row]
    elif source == "agent":
        store.own = [row]
    elif source == "schema_only":
        store.skill["output_schema"] = {
            "type": "object",
            "properties": {"result": {"type": "string", "description": authored}},
            "required": ["result"],
        }
    elif source == "legacy":
        store.legacy = ["result"]
        authored = ""
    else:
        fields = {
            "block_notes": {
                "notes": authored,
                "field_description": "ignored",
                "field_display_name": "ignored",
            },
            "block_field": {
                "notes": "  ",
                "field_description": authored,
                "field_display_name": "ignored",
            },
            "block_name": {"notes": "", "field_description": " ", "field_display_name": authored},
        }
        store.blocks = [
            block(
                outputs=[
                    {
                        "field_key": "result",
                        "value_type": "text",
                        "required_by_default": True,
                        **fields[source],
                    }
                ]
            )
        ]
    captured = resolve(store)
    description = captured.rows[0]["description"]
    assert description == authored
    public = IOResolver(store).get_step_declared_outputs(
        {"step_id": "write", "agent_id": "writer", "skill_id": "selected"},
        workflow_account_id="account-a",
    )[0]
    assert public["description"] == authored
    assert "description_key" not in public and "description_params" not in public
    request_field = build_phase_result_json_schema(captured.rows)["properties"]["artifacts"][
        "properties"
    ]["result"]
    if source != "legacy":
        assert request_field == {"type": "string", "description": authored}
    row["description"] = "mutated"
    assert captured.rows[0]["description"] == description
    assert StepOutputContract.from_receipt(captured.rows, captured.digest).rows == captured.rows


@pytest.mark.parametrize("first", ["Selected Skill text", ""])
def test_duplicate_declarations_preserve_first_nonempty_authored_description(first):
    store = ContractStore()
    store.skill["_outputs"] = [{"name": "summary", "type": "text", "description": first}]
    store.own = [{"name": "summary", "type": "text", "description": "Agent own text"}]
    row = resolve(store).rows[0]
    expected = first or "Agent own text"
    assert row["description"] == expected
    assert row["response_schema"] == {"type": "string", "description": expected}
    assert row["contract_schema"] == {"type": "string"}


def test_description_annotations_never_replace_authored_format_constraints_or_schema_description():
    store = ContractStore()
    schema = {"type": "array", "items": {"type": "number"}, "description": "Authored schema text"}
    store.skill["_outputs"] = [
        {
            "name": "result",
            "type": "list",
            "description": "Selected Skill text",
            "contract_schema": schema,
        }
    ]
    store.own = [{"name": "result", "type": "list", "description": "Agent own text"}]
    captured = resolve(store)
    row = captured.rows[0]
    assert row["description"] == "Selected Skill text"
    assert row["response_schema"] == schema
    assert captured.validate({"result": [1, 0]}) == []
    assert captured.validate({"result": ["wrong"]})


def test_description_default_response_matches_existing_format_helper():
    from core.application.orchestration.models import build_phase_result_json_schema

    store = ContractStore()
    rows = [
        {"name": "result", "type": "json", "description": "Authored instruction", "required": True}
    ]
    store.skill["_outputs"] = rows
    captured = resolve(store)
    assert captured.rows[0]["contract_schema"] == {}
    assert build_phase_result_json_schema(captured.rows) == build_phase_result_json_schema(rows)


@pytest.mark.parametrize("authored_first", [False, True], ids=["generic-first", "authored-first"])
def test_equal_shape_merge_keeps_authored_schema_description_and_editor_precedence(authored_first):
    from jsonschema import Draft202012Validator

    from core.application.registry.output_contract_fields import add_output_declaration

    generic = {"name": "result", "type": "text", "description": "A"}
    authored = {"name": "result", "type": "text", "description": "Authored row"}
    schema = {"type": "string", "description": "B"}
    declarations = [(generic, None), (authored, schema)]
    if authored_first:
        declarations.reverse()
    fields = {}
    for index, (row, contract_schema) in enumerate(declarations):
        add_output_declaration(fields, row, "normalization-control", str(index), contract_schema)
    merged = fields["result"]
    assert merged["description"] == declarations[0][0]["description"]
    assert merged["response_schema"] == schema
    assert schema in merged["contract_schema"]["allOf"]
    for view in (merged["contract_schema"], merged["response_schema"]):
        validator = Draft202012Validator(view)
        assert validator.is_valid("retained")
        assert not validator.is_valid(42)
    assert schema == {"type": "string", "description": "B"}


def test_unannotated_equal_shape_declaration_keeps_existing_row_description():
    from core.application.registry.output_contract_fields import add_output_declaration

    fields = {}
    add_output_declaration(
        fields, {"name": "result", "type": "text", "description": "A"}, "control", "first"
    )
    add_output_declaration(fields, {"name": "result", "type": "text"}, "control", "second")
    assert fields["result"]["description"] == "A"
    assert fields["result"]["response_schema"] == {"type": "string", "description": "A"}
    assert fields["result"]["contract_schema"] == {"type": "string"}
