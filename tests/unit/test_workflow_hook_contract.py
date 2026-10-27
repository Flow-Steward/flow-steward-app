"""The hook declaration of an ordinary Invoke and its result normalization (spec §9)."""

from __future__ import annotations

import copy

import pytest

from core.application.workflows.workflow_hook_contract import (
    HookContractError,
    hook_semantic_digest,
    invoke_config_issues,
    normalize_hook_output,
    parse_hook_declaration,
    select_hook_targets,
)


def declaration(**changes) -> dict:
    raw = {
        "hook_id": "before_invoice",
        "contract_version": 1,
        "display_name": "Before creating an invoice",
        "description": "Adjust the buyer before the invoice is created.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "integer"},
                "company": {"type": "string"},
                "note": {"type": ["string", "null"]},
            },
            "required": ["invoice_id", "company"],
        },
        "input_mapping": {
            "invoice_id": "state.invoice.id",
            "company": "state.invoice.company",
            "note": "state.invoice.note",
        },
        "mutable_fields": ["company", "note"],
        "additional_outputs": {
            "skip_request": {"schema": {"type": "boolean"}, "default": False},
            "crm_id": {"schema": {"type": "string"}, "optional": True},
        },
        "stage_contract": {
            "summary": "Runs before the invoice is created; nothing is sent yet.",
            "effects_before": [],
            "effects_after": ["invoice.create"],
        },
    }
    raw.update(changes)
    return raw


def code_of(raw: dict) -> str:
    with pytest.raises(HookContractError) as raised:
        parse_hook_declaration(raw)
    return raised.value.code


def test_a_declaration_parses_with_its_derived_output_contract() -> None:
    hook = parse_hook_declaration(declaration())
    assert hook.hook_id == "before_invoice"
    assert hook.mutable_fields == ("company", "note")
    assert set(hook.output_schema["properties"]) == {
        "invoice_id",
        "company",
        "note",
        "skip_request",
        "crm_id",
    }
    # Required: the required inputs and every output-only field that is not optional.
    assert set(hook.output_schema["required"]) == {"invoice_id", "company", "skip_request"}
    assert hook.output_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"hook_id": "Before-Invoice"}, "workflow.hook.id_invalid"),
        ({"contract_version": 0}, "workflow.hook.contract_version_invalid"),
        ({"contract_version": "1"}, "workflow.hook.contract_version_invalid"),
        ({"mutable_fields": ["missing"]}, "workflow.hook.mutable_field_unknown"),
        ({"input_schema": {"type": "array"}}, "workflow.hook.input_schema_invalid"),
        (
            {"input_schema": {"type": "object", "properties": {"x": {"type": "nope"}}}},
            "workflow.hook.input_schema_invalid",
        ),
        ({"input_mapping": {"unknown": "state.x"}}, "workflow.hook.input_mapping_unknown"),
        (
            {"additional_outputs": {"company": {"schema": {"type": "string"}}}},
            "workflow.hook.output_name_conflict",
        ),
        (
            # A required output-only field needs a default for the empty hook.
            {"additional_outputs": {"flag": {"schema": {"type": "boolean"}}}},
            "workflow.hook.output_default_required",
        ),
        (
            {"additional_outputs": {"flag": {"schema": {"type": "boolean"}, "default": "no"}}},
            "workflow.hook.output_default_invalid",
        ),
        # The removed and never-supported fields are refused, not ignored.
        ({"outcomes": ["skip"]}, "workflow.hook.field_unknown"),
        ({"allow_waits": True}, "workflow.hook.field_unknown"),
        ({"max_duration_ms": 1000}, "workflow.hook.field_unknown"),
        ({"stage_contract": {}}, "workflow.hook.stage_contract_invalid"),
    ],
)
def test_an_invalid_declaration_is_a_precise_error(changes, code) -> None:
    assert code_of(declaration(**changes)) == code


def test_a_declaration_is_never_silently_short_of_a_field() -> None:
    raw = declaration()
    del raw["stage_contract"]
    assert code_of(raw) == "workflow.hook.field_missing"


def test_mixed_direct_workflow_and_hook_is_refused() -> None:
    issues = invoke_config_issues({"workflow_id": "child", "hook": declaration()})
    assert [issue["code"] for issue in issues] == ["workflow.hook.direct_target_conflict"]
    assert invoke_config_issues({"hook": declaration()}) == []
    assert invoke_config_issues({"workflow_id": "child"}) == []
    assert [issue["code"] for issue in invoke_config_issues({})] == [
        "workflow.invoke.target_missing"
    ]


def test_the_semantic_digest_ignores_labels_but_not_the_stage() -> None:
    base = hook_semantic_digest(parse_hook_declaration(declaration()))
    relabeled = declaration(display_name="Other label", description="Other text")
    assert hook_semantic_digest(parse_hook_declaration(relabeled)) == base
    moved = declaration(
        stage_contract={
            "summary": "Runs after the invoice is created.",
            "effects_before": ["invoice.create"],
            "effects_after": [],
        }
    )
    assert hook_semantic_digest(parse_hook_declaration(moved)) != base
    narrower = declaration(mutable_fields=["company"])
    assert hook_semantic_digest(parse_hook_declaration(narrower)) != base


# --- normalization (§9.3) ------------------------------------------------------

INPUTS = {"invoice_id": 123, "company": "Example UAB", "note": None}


def normalize(targets: dict | None, inputs: dict | None = None) -> dict:
    return normalize_hook_output(
        parse_hook_declaration(declaration()), copy.deepcopy(inputs or INPUTS), targets
    )


def test_the_empty_hook_passes_inputs_through_and_publishes_defaults() -> None:
    assert normalize(None) == {
        "invoice_id": 123,
        "company": "Example UAB",
        "note": None,
        "skip_request": False,
    }


def test_a_mutable_field_is_replaced_by_presence_with_falsy_values_kept() -> None:
    original = copy.deepcopy(INPUTS)
    output = normalize({"company": "", "skip_request": True}, inputs=original)
    assert output["company"] == ""
    assert output["skip_request"] is True
    assert output["invoice_id"] == 123
    assert original["company"] == "Example UAB"


@pytest.mark.parametrize("value", ["", 0, False, None, [], {}])
def test_falsy_and_null_values_are_values(value) -> None:
    hook = parse_hook_declaration(
        declaration(
            input_schema={"type": "object", "properties": {"v": {}}},
            input_mapping={"v": "state.v"},
            mutable_fields=["v"],
            additional_outputs={},
        )
    )
    assert normalize_hook_output(hook, {"v": "x"}, {"v": value}) == {"v": value}


def test_an_unmapped_absent_input_stays_absent() -> None:
    output = normalize(None, inputs={"invoice_id": 1, "company": "A"})
    assert "note" not in output


def test_an_optional_output_without_default_is_absent_and_published_when_mapped() -> None:
    assert "crm_id" not in normalize(None)
    assert normalize({"crm_id": "c-1"})["crm_id"] == "c-1"


@pytest.mark.parametrize(
    ("targets", "code"),
    [
        ({"invoice_id": 7}, "workflow.hook.target_read_only"),
        ({"unknown": 1}, "workflow.hook.target_unknown"),
        ({"company": 5}, "workflow.hook.output_invalid"),
        ({"skip_request": "yes"}, "workflow.hook.output_invalid"),
    ],
)
def test_read_only_unknown_or_invalid_targets_are_errors(targets, code) -> None:
    with pytest.raises(HookContractError) as raised:
        normalize(targets)
    assert raised.value.code == code


def test_child_outputs_select_hook_targets_and_unused_outputs_are_allowed() -> None:
    selected = select_hook_targets(
        {"company_name": "company"},
        {"company_name": "New UAB", "crm_id": "unused", "delivery_status": "ok"},
    )
    assert selected == {"company": "New UAB"}


def test_a_result_no_step_set_falls_back_to_the_input_of_that_name() -> None:
    """R2-11: an empty or partial hook keeps its inputs; no Build/Return scaffolding."""
    targets = select_hook_targets(
        {"company": "company", "skip_request": "skip_request", "crm_id": "crm_id"},
        {"crm_id": "c-1"},
    )

    assert targets == {"crm_id": "c-1"}
    # company keeps its input, skip_request its declared default.
    assert normalize(targets) == {
        "invoice_id": 123,
        "company": "Example UAB",
        "note": None,
        "skip_request": False,
        "crm_id": "c-1",
    }


def test_a_result_set_with_the_wrong_type_is_still_refused() -> None:
    targets = select_hook_targets({"company": "company"}, {"company": 5})
    with pytest.raises(HookContractError) as raised:
        normalize(targets)
    assert raised.value.code == "workflow.hook.output_invalid"


def test_child_null_is_a_mapped_value() -> None:
    assert select_hook_targets({"note": "note"}, {"note": None}) == {"note": None}


def test_a_hook_carries_no_child_mappings_of_its_own() -> None:
    issues = invoke_config_issues({"hook": declaration(), "output_mapping": {"a": "b"}})
    assert [issue["code"] for issue in issues] == ["workflow.hook.mapping_conflict"]


def test_a_hook_id_names_one_hook_of_the_workflow() -> None:
    from core.application.workflows.workflow_definition_validation import (
        validate_workflow_definition,
    )
    from tests._workflow_definitions import edge, node

    def hook_step(step_id: str, **changes) -> dict:
        return {
            "step_id": step_id,
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"hook": declaration(**changes)}},
        }

    definition = {
        "steps": [
            hook_step("first"),
            hook_step("second"),
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "for_each": [],
        "execution_graph": {
            "schema_version": 1,
            "entry": node("first"),
            "edges": [edge("first", node("second")), edge("second", node("done"))],
        },
        "settings": {},
    }
    codes = [issue["code"] for issue in validate_workflow_definition(definition)]
    assert codes.count("workflow.hook.id_duplicate") == 1
    definition["steps"][1] = hook_step("second", hook_id="after_invoice")
    assert "workflow.hook.id_duplicate" not in [
        issue["code"] for issue in validate_workflow_definition(definition)
    ]
    definition["steps"][1]["override"]["invoke_workflow"]["workflow_id"] = "child"
    assert "workflow.hook.direct_target_conflict" in [
        issue["code"] for issue in validate_workflow_definition(definition)
    ]
