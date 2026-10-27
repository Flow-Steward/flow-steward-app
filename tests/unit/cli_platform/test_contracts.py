"""Contract tests for the schema-driven CLI platform."""

from __future__ import annotations

from enum import Enum

import pytest
from pydantic import BaseModel, ValidationError

from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CLIErrorDetail,
    CLIFailureEnvelope,
    CLISuccessEnvelope,
    CLIWarning,
    CommandSpec,
    ExecutionResult,
    ExitCode,
    FieldIssue,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationProgressEvent,
    OperationStep,
    OperationStepStatus,
    OperationType,
)


class _InputModel(BaseModel):
    name: str


class _ResultModel(BaseModel):
    name: str


def _command_spec(**overrides: object) -> CommandSpec:
    values: dict[str, object] = {
        "command_id": "users.list",
        "path": ("users", "list"),
        "summary": "List users.",
        "description": "Lists users.",
        "examples": (),
        "operation_type": OperationType.READ,
        "interactive_policy": InteractivePolicy.IMMEDIATE,
        "fields": (),
        "input_model": _InputModel,
        "result_model": _ResultModel,
        "plan_builder_ref": None,
        "confirmation_policy_ref": None,
        "service_ref": "users.list",
        "result_renderer_ref": "table",
    }
    values.update(overrides)
    return CommandSpec(**values)


def test_all_contract_models_reject_unknown_fields() -> None:
    models_and_values = (
        (ChoiceItem, {"value": "admin", "label": "Admin"}),
        (FieldSpec, {"key": "name", "label": "Name", "type": FieldType.TEXT}),
        (CommandSpec, _command_spec().model_dump()),
        (FieldIssue, {"field": "email", "message": "Invalid email."}),
        (CLIWarning, {"code": "deprecated", "message": "Use the replacement."}),
        (CLIErrorDetail, {"code": "validation", "message": "Invalid input."}),
        (OperationStep, {"step_id": "validate", "status": "running", "label": "Validating"}),
        (
            OperationProgressEvent,
            {"step_id": "validate", "status": "running", "label": "Validating"},
        ),
        (ExecutionResult, {"result": _ResultModel(name="Ada")}),
        (CLISuccessEnvelope, {"command": "users.list", "result": {}}),
        (
            CLIFailureEnvelope,
            {
                "command": "users.list",
                "error": {"code": "validation", "message": "Invalid input."},
            },
        ),
    )

    for model, values in models_and_values:
        with pytest.raises(ValidationError):
            model(**values, unknown=True)


def test_command_and_field_specifications_are_immutable() -> None:
    field = FieldSpec(key="name", label="Name", type=FieldType.TEXT)
    command = _command_spec()

    with pytest.raises(ValidationError):
        field.label = "Different"
    with pytest.raises(ValidationError):
        command.summary = "Different"


def test_field_specification_metadata_is_deeply_immutable() -> None:
    choice = ChoiceItem(value="viewer", label="Viewer")
    field = FieldSpec(
        key="roles",
        label="Roles",
        type=FieldType.MULTI_SELECT,
        choices=(choice,),
        default=[{"roles": ["viewer"]}],
        visible_if={"mode": {"values": ["create"]}},
    )

    with pytest.raises(ValidationError):
        choice.label = "Different"
    with pytest.raises(TypeError):
        field.visible_if["mode"] = "update"
    with pytest.raises(TypeError):
        field.visible_if["mode"]["values"] = ("update",)
    with pytest.raises((AttributeError, TypeError)):
        field.visible_if["mode"]["values"].append("update")
    with pytest.raises(TypeError):
        list.append(field.default, {"roles": ["admin"]})
    with pytest.raises(TypeError):
        list.__setitem__(field.default[0]["roles"], 0, "admin")

    assert list(field.default) == [{"roles": ("viewer",)}]
    assert field.visible_if["mode"]["values"] == ("create",)


def test_closed_enum_values_match_the_normative_contract() -> None:
    assert {item.name: item.value for item in OperationType} == {
        "READ": "read",
        "CHANGE": "change",
        "DESTRUCTIVE": "destructive",
    }
    assert {item.name: item.value for item in InteractivePolicy} == {
        "IMMEDIATE": "immediate",
        "WIZARD_WHEN_INCOMPLETE": "wizard_when_incomplete",
        "ALWAYS_REVIEW": "always_review",
    }
    assert {item.name: item.value for item in FieldType} == {
        "TEXT": "text",
        "SECRET": "secret",
        "BOOLEAN": "boolean",
        "SINGLE_SELECT": "single_select",
        "MULTI_SELECT": "multi_select",
        "INTEGER": "integer",
        "DECIMAL": "decimal",
        "PATH": "path",
        "URL": "url",
        "EMAIL": "email",
        "DURATION": "duration",
        "EXTENSION_ID": "extension_id",
        "RUNTIME_IDENTIFIER": "runtime_identifier",
    }
    assert all(issubclass(type(item), Enum) for item in FieldType)
    assert {item.name: item.value for item in OperationStepStatus} == {
        "PENDING": "pending",
        "RUNNING": "running",
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
        "SKIPPED": "skipped",
        "CANCELLED": "cancelled",
    }


def test_secret_field_must_be_secret_type_and_cannot_be_positional() -> None:
    with pytest.raises(ValidationError):
        FieldSpec(key="password", label="Password", type=FieldType.TEXT, secret=True)
    with pytest.raises(ValidationError):
        FieldSpec(
            key="password",
            label="Password",
            type=FieldType.SECRET,
            required=True,
            secret=True,
            positional=True,
        )


def test_field_cannot_mix_static_choices_and_dynamic_choice_provider() -> None:
    _raises_input_178_1 = (ChoiceItem(value="admin", label="Admin"),)
    with pytest.raises(ValidationError):
        FieldSpec(
            key="role",
            label="Role",
            type=FieldType.SINGLE_SELECT,
            choices=_raises_input_178_1,
            choice_provider="roles.available",
        )


def test_required_field_cannot_declare_a_default() -> None:
    with pytest.raises(ValidationError, match="required fields cannot declare defaults"):
        FieldSpec(
            key="roles",
            label="Roles",
            type=FieldType.MULTI_SELECT,
            required=True,
            default=["viewer"],
        )


@pytest.mark.parametrize("field_type", (FieldType.TEXT, FieldType.INTEGER, FieldType.SECRET))
def test_dynamic_choice_provider_requires_a_select_field(field_type: FieldType) -> None:
    with pytest.raises(ValidationError):
        FieldSpec(
            key="value",
            label="Value",
            type=field_type,
            choice_provider="values.available",
        )


def test_command_path_must_be_canonical_and_command_id_must_match() -> None:
    for path in ((), ("users", "", "list"), ("users", "list", "all"), ("Users", "list")):
        with pytest.raises(ValidationError):
            _command_spec(path=path)
    with pytest.raises(ValidationError):
        _command_spec(command_id="users.other")

    group = _command_spec(command_id="users", path=("users",))
    assert group.path == ("users",)


def test_cli_envelopes_serialize_with_the_fixed_schema() -> None:
    success = CLISuccessEnvelope(command="users.list", result={"users": []})
    failure = CLIFailureEnvelope(
        command="users.list",
        error=CLIErrorDetail(code="validation", message="Input is invalid."),
    )

    assert success.model_dump(mode="json")["schema"] == "flow-steward.cli.v1"
    assert failure.model_dump(mode="json")["schema"] == "flow-steward.cli.v1"


def test_execution_result_preserves_concrete_result_fields_when_serialized() -> None:
    result = ExecutionResult(result=_ResultModel(name="Ada"))

    assert result.model_dump(mode="json")["result"] == {"name": "Ada"}


def test_failure_details_support_aggregate_safe_field_issues() -> None:
    failure = CLIFailureEnvelope(
        command="users.create",
        error=CLIErrorDetail(
            code="validation",
            message="Input is invalid.",
            field_issues=(
                FieldIssue(field="email", message="Not a valid email.", code="value_error"),
                FieldIssue(field="roles", message="Select at least one role.", code="missing"),
            ),
        ),
    )

    payload = failure.model_dump(mode="json")
    assert payload["error"]["field_issues"] == [
        {"field": "email", "message": "Not a valid email.", "code": "value_error"},
        {"field": "roles", "message": "Select at least one role.", "code": "missing"},
    ]
    assert "input" not in payload["error"]
    assert all("input" not in issue for issue in payload["error"]["field_issues"])


def test_exit_code_values_match_the_normative_contract() -> None:
    assert {item.name: item.value for item in ExitCode} == {
        "SUCCESS": 0,
        "SYNTAX": 2,
        "VALIDATION": 3,
        "PRECONDITION": 4,
        "PERMISSION": 5,
        "CONFLICT": 6,
        "DEPENDENCY": 7,
        "STABLE_FAILURE": 8,
        "RECOVERY_REQUIRED": 9,
        "INTERRUPTED": 10,
        "INTERNAL": 70,
    }
