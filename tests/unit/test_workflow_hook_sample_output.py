"""A hook check with a sample shows what a run would publish, by the runtime's own rules."""

from __future__ import annotations

import pytest

from core.application.management.command_specs.workflows import WorkflowHookPreviewCommandInput
from core.application.workflows.workflow_hook_binding_service import sample_hook_output
from core.application.workflows.workflow_hook_contract import (
    HookContractError,
    parse_hook_declaration,
)
from tests.unit.test_workflow_hook_contract import declaration

HOOK = parse_hook_declaration(declaration())
INPUTS = {"invoice_id": 7, "company": "Example UAB"}


def test_the_child_result_fills_the_mapped_field_and_the_rest_passes_through() -> None:
    output = sample_hook_output(
        HOOK,
        {"company_name": "company"},
        "actions",
        {"inputs": INPUTS, "child_outputs": {"company_name": "Acme", "crm_id": "c-1"}},
    )

    # crm_id is a declared child output nobody mapped: unused, not an error.
    assert output == {"invoice_id": 7, "company": "Acme", "skip_request": False}


def test_an_empty_hook_publishes_its_inputs_and_defaults() -> None:
    assert sample_hook_output(HOOK, {}, None, {"inputs": INPUTS}) == {
        **INPUTS,
        "skip_request": False,
    }


def test_a_result_the_steps_did_not_set_keeps_the_hook_input() -> None:
    output = sample_hook_output(
        HOOK, {"company_name": "company"}, "actions", {"inputs": INPUTS, "child_outputs": {}}
    )

    assert output == {**INPUTS, "skip_request": False}


@pytest.mark.parametrize(
    ("sample", "code"),
    [
        (
            {
                "inputs": {"invoice_id": "seven", "company": "x"},
                "child_outputs": {"company_name": "A"},
            },
            "workflow.hook.input_invalid",
        ),
        (
            {"inputs": INPUTS, "child_outputs": {"company_name": 5}},
            "workflow.hook.output_invalid",
        ),
    ],
)
def test_a_sample_that_would_fail_a_run_reports_why(sample, code) -> None:
    with pytest.raises(HookContractError) as raised:
        sample_hook_output(HOOK, {"company_name": "company"}, "actions", sample)
    assert raised.value.code == code


def test_the_cli_takes_the_sample_as_json_text_and_refuses_another_shape() -> None:
    base = {
        "project_id": "p-1",
        "workflow_id": "invoice_flow",
        "hook_id": "before_invoice",
        "expected_revision": 0,
        "target_workflow_id": "actions",
    }
    parsed = WorkflowHookPreviewCommandInput.model_validate(
        {**base, "sample": '{"inputs": {"invoice_id": 7}, "child_outputs": {}}'}
    )
    assert parsed.sample == {"inputs": {"invoice_id": 7}, "child_outputs": {}}
    assert WorkflowHookPreviewCommandInput.model_validate({**base, "sample": ""}).sample is None
    with pytest.raises(ValueError, match="sample is a JSON object"):
        WorkflowHookPreviewCommandInput.model_validate({**base, "sample": {"outputs": {}}})
