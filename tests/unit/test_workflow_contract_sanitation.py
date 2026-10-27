"""Shared workflow contract sanitation at ordinary orchestration boundaries."""

import copy

import pytest

from core.application.workflows.contract_sanitation import safe_contract_rows


def test_safe_contract_rows_normalizes_dataset_field_type():
    rows = safe_contract_rows([{"name": "sales_dataset", "type": "dataset", "required": False}])

    assert rows == [{"name": "sales_dataset", "type": "string", "required": False}]


@pytest.mark.parametrize(
    "name",
    [
        "workflow_id",
        "workflow_id_customer",
        "confirm_change_request",
        "q1_pr_step_id",
        "step_kind_process",
        "branch_condition_ready",
    ],
)
def test_safe_contract_rows_preserves_declared_names(name):
    row = {"name": name, "type": "string", "required": True}

    assert safe_contract_rows([row]) == [row]


def test_safe_contract_rows_parses_rows_and_copies_nested_data():
    rows = [
        {"name": "sales_dataset", "type": "dataset", "metadata": {"labels": ["sales"]}},
        '{"name": "request", "type": "string", "required": true}',
        {"name": "   ", "type": "string"},
        "{invalid json",
        "[]",
        None,
        7,
    ]
    original = copy.deepcopy(rows)

    normalized = safe_contract_rows(rows)

    assert normalized == [
        {"name": "sales_dataset", "type": "string", "metadata": {"labels": ["sales"]}},
        {"name": "request", "type": "string", "required": True},
    ]
    normalized[0]["metadata"]["labels"].append("normalized")
    assert rows == original
    assert safe_contract_rows({"name": "request", "type": "string"}) == []
