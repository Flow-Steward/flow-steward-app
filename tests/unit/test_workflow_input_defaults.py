"""Configured defaults are fixed for the workflow revision.

An operator who configures a default is stating what the workflow runs with.
Letting a caller pass the same key would make the configured value advisory —
and silently ignoring the caller's value would be worse: the run would use one
value while the caller believed it had supplied another. So the caller is told.
"""

from __future__ import annotations

import math
from typing import get_args

import pytest

from core.application.registry.workflow_contract import WorkflowFieldType, WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService


def _interface(*fields: dict) -> WorkflowInterface:
    return WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": list(fields),
                "output_mode": "none",
                "trigger_channels": ["api"],
            }
        }
    )


def _codes(result) -> set[str]:
    return {str(item.get("code") or "") for item in (result.errors or [])}


def test_a_configured_default_is_used_when_the_caller_omits_the_field() -> None:
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "region", "type": "string", "default": "eu-west-1"}),
        {},
    )

    assert result.ok, result.errors
    assert result.inputs["region"] == "eu-west-1"


def test_a_caller_may_not_override_a_configured_default() -> None:
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "region", "type": "string", "default": "eu-west-1"}),
        {"region": "us-east-1"},
    )

    assert not result.ok
    assert "workflow.input.override_forbidden" in _codes(result)


def test_supplying_the_same_value_is_still_an_override() -> None:
    """Comparing values would make the rule depend on what the caller guessed."""
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "region", "type": "string", "default": "eu-west-1"}),
        {"region": "eu-west-1"},
    )

    assert not result.ok
    assert "workflow.input.override_forbidden" in _codes(result)


def test_a_default_satisfies_required() -> None:
    """A required field with a default is never missing."""
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "region", "type": "string", "required": True, "default": "eu-west-1"}),
        {},
    )

    assert result.ok, result.errors
    assert result.inputs["region"] == "eu-west-1"


def test_without_a_default_the_caller_still_supplies_the_value() -> None:
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "region", "type": "string"}),
        {"region": "us-east-1"},
    )

    assert result.ok, result.errors
    assert result.inputs["region"] == "us-east-1"


def test_without_a_default_required_still_applies() -> None:
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "region", "type": "string", "required": True}),
        {},
    )

    assert not result.ok
    assert "workflow.input.required" in _codes(result)


def test_a_default_is_coerced_by_the_same_rules_as_a_caller_value() -> None:
    """A default written as text in the editor must arrive as the declared type."""
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "retries", "type": "number", "default": "3"}),
        {},
    )

    assert result.ok, result.errors
    assert result.inputs["retries"] == 3


def test_a_default_that_does_not_fit_its_type_is_a_definition_error() -> None:
    """Accepting it and failing at the step would blame the run for the config."""
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "retries", "type": "number", "default": "not-a-number"}),
        {},
    )

    assert not result.ok
    assert "workflow.interface.default_invalid" in _codes(result)


def test_an_invalid_configured_default_is_a_readiness_definition_error() -> None:
    interface = _interface({"name": "retries", "type": "number", "default": "not-a-number"})

    codes = {item["code"] for item in interface.definition_issues()}

    assert "workflow.interface.default_invalid" in codes


def test_a_select_default_must_be_one_of_the_options() -> None:
    result = WorkflowInterfaceService().validate_inputs(
        _interface(
            {
                "name": "tier",
                "type": "select",
                "options": ["gold", "silver"],
                "default": "bronze",
            }
        ),
        {},
    )

    assert not result.ok
    assert "workflow.interface.default_invalid" in _codes(result)


def test_a_boolean_default_of_false_is_a_configured_default() -> None:
    """The falsy-value trap: `false` is a value, not the absence of one."""
    service = WorkflowInterfaceService()
    interface = _interface({"name": "dry_run", "type": "boolean", "default": False})

    applied = service.validate_inputs(interface, {})
    overridden = service.validate_inputs(interface, {"dry_run": True})

    assert applied.ok
    assert applied.inputs["dry_run"] is False
    assert "workflow.input.override_forbidden" in _codes(overridden)


def test_an_empty_string_default_is_configured_and_round_trips() -> None:
    interface = _interface({"name": "prefix", "type": "string", "default": ""})

    result = WorkflowInterfaceService().validate_inputs(interface, {})

    assert interface.expected_inputs[0].has_default is True
    assert interface.dump()["expected_inputs"][0]["default"] == ""
    assert result.ok, result.errors
    assert result.inputs["prefix"] == ""


def test_outputs_cannot_be_secrets_or_have_configured_defaults() -> None:
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_outputs": [
                    {"name": "credential", "type": "secret"},
                    {"name": "region", "type": "string", "default": "eu-west-1"},
                ],
                "output_mode": "variables",
            }
        }
    )

    codes = {item["code"] for item in interface.definition_issues()}

    assert "workflow.interface.output_secret_forbidden" in codes
    assert "workflow.interface.output_default_forbidden" in codes


def test_public_output_projection_never_exposes_default_controls_or_secret_authority() -> None:
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_outputs": [
                    {
                        "name": "credential",
                        "type": "secret",
                        "default_secret_ref": "workflow-input:v1:owned:4",
                    },
                    {"name": "region", "type": "string", "default": "eu-west-1"},
                ],
                "output_mode": "variables",
            }
        }
    )

    payload = WorkflowInterfaceService().public_contract_payload(interface)

    for row in payload["expected_outputs"]:
        assert "default" not in row
        assert "default_configured" not in row
        assert "default_secret_ref" not in row


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", math.nan, math.inf])
def test_non_finite_number_defaults_are_definition_errors(value: object) -> None:
    interface = _interface({"name": "ratio", "type": "number", "default": value})

    codes = {item["code"] for item in interface.definition_issues()}

    assert "workflow.interface.default_invalid" in codes


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", math.nan, math.inf])
def test_non_finite_caller_numbers_are_rejected(value: object) -> None:
    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "ratio", "type": "number"}), {"ratio": value}
    )

    assert not result.ok
    assert "workflow.input.invalid_type" in _codes(result)


def test_malformed_inline_secret_reference_is_a_bounded_validation_error() -> None:
    marker = {
        "__type__": "flow-steward.secret-ref.v1",
        "name": "login",
        "ciphertext": "malformed-ciphertext-must-not-echo",
    }

    result = WorkflowInterfaceService().validate_inputs(
        _interface({"name": "login", "type": "secret"}),
        {"login": marker},
    )

    assert not result.ok
    assert "workflow.input.invalid_type" in _codes(result)
    assert "malformed-ciphertext-must-not-echo" not in repr(result.errors)


def test_secret_is_part_of_the_declared_workflow_field_type() -> None:
    assert "secret" in get_args(WorkflowFieldType)
