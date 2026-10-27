"""A binding whose mapped types can never fit is refused when it is checked (spec §9.2)."""

from __future__ import annotations

from core.application.workflows.workflow_hook_binding_service import (
    HookBindingRequest,
    _mapping_issues,
)
from core.application.workflows.workflow_hook_contract import parse_hook_declaration
from tests.unit.test_workflow_hook_contract import declaration

HOOK = parse_hook_declaration(declaration())


def child(*, inputs: list[dict] | None = None, input_schema: dict | None = None) -> dict:
    callable_settings: dict = {
        "enabled": True,
        "trigger_channels": ["parent_workflow"],
        "output_mode": "variables",
        "expected_outputs": [
            {"name": "company_name", "type": "string"},
            {"name": "flagged", "type": "boolean"},
            {"name": "score", "type": "number"},
        ],
    }
    if input_schema is not None:
        callable_settings.update(input_mode="json_schema", input_schema=input_schema)
    else:
        callable_settings.update(input_mode="fields", expected_inputs=inputs or [])
    return {"settings": {"callable": callable_settings}}


def issues(target: dict, *, inputs: dict | None = None, outputs: dict | None = None) -> list:
    request = HookBindingRequest(
        owner_workflow_id="invoice_flow",
        project_id="p-1",
        hook_id="before_invoice",
        expected_revision=0,
        target_workflow_id="actions",
        input_mapping=inputs or {},
        output_mapping=outputs or {},
    )
    return [issue["code"] for issue in _mapping_issues(HOOK, target, request)]


def test_a_text_result_cannot_fill_a_yes_no_field() -> None:
    assert issues(child(), outputs={"company_name": "skip_request"}) == [
        "workflow.hook.binding_output_type_mismatch"
    ]


def test_matching_and_unrestricted_types_fit() -> None:
    assert issues(child(), outputs={"company_name": "company", "flagged": "skip_request"}) == []
    # "note" allows a string or null: a text result fits.
    assert issues(child(), outputs={"company_name": "note"}) == []


def test_a_hook_number_cannot_feed_a_yes_no_input_of_the_child() -> None:
    target = child(inputs=[{"name": "invoice", "type": "boolean"}])
    assert issues(target, inputs={"invoice": "invoice_id"}) == [
        "workflow.hook.binding_input_type_mismatch"
    ]
    # A whole number fits a number input, declared as a field or as a JSON Schema.
    assert (
        issues(
            child(inputs=[{"name": "invoice", "type": "number"}]), inputs={"invoice": "invoice_id"}
        )
        == []
    )
    schema = {"type": "object", "properties": {"invoice": {"type": "integer"}}}
    assert issues(child(input_schema=schema), inputs={"invoice": "invoice_id"}) == []
