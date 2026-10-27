from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService


def test_workflow_interface_normalizes_legacy_types_and_derives_output_mode():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "brief", "type": "text", "required": True}],
                "expected_outputs": [{"name": "payload", "type": "json", "required": True}],
            }
        }
    )

    assert interface.enabled is True
    assert interface.expected_inputs[0].type == "paragraph"
    assert interface.expected_outputs[0].type == "object"
    assert interface.output_mode == "variables"


def test_workflow_interface_flags_reserved_input_names():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "task_summary", "type": "text"}],
                "output_mode": "none",
            }
        }
    )

    codes = {item["code"] for item in interface.definition_issues()}
    assert "workflow.interface.name_reserved" in codes


def test_workflow_interface_service_validates_and_coerces_inputs():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["ui", "api"],
                "strict_inputs": True,
                "expected_inputs": [
                    {"name": "count", "type": "number", "required": True},
                    {"name": "enabled", "type": "boolean", "required": False},
                ],
                "output_mode": "none",
            }
        }
    )
    service = WorkflowInterfaceService()

    result = service.validate_inputs(
        interface,
        {"count": "3", "enabled": "true", "unknown": "x", "task_summary": "runtime"},
        channel="api",
    )

    assert result.ok is False
    assert result.inputs == {"count": 3, "enabled": True, "task_summary": "runtime"}
    assert any(item["code"] == "workflow.input.unknown" for item in result.errors)


def test_workflow_interface_service_rejects_undeclared_trigger_channel():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [{"name": "brief", "type": "string", "required": True}],
                "output_mode": "none",
            }
        }
    )

    result = WorkflowInterfaceService().validate_inputs(
        interface,
        {"brief": "Ship it"},
        channel="api",
    )

    assert result.ok is False
    assert any(item["code"] == "workflow.input.channel_forbidden" for item in result.errors)


def test_workflow_interface_service_resolves_projected_extension_workflow():
    db = SimpleNamespace(get_workflow=lambda _workflow_id: None)
    projected = {
        "workflow_id": "extension.install-1.demo",
        "settings": {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "request", "type": "string", "required": True}],
                "output_mode": "none",
            }
        },
    }

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=SimpleNamespace(db_store=db),
        ),
        patch(
            "core.application.registry.projected_artifact_rows.projected_artifact_rows",
            return_value=[projected],
        ),
    ):
        interface = WorkflowInterfaceService().interface_for_workflow_id(projected["workflow_id"])

    assert interface.expected_inputs[0].name == "request"


def test_workflow_interface_service_validates_json_schema_payload():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "trigger_channels": ["api"],
                "input_mode": "json_schema",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["email"],
                    "properties": {
                        "email": {"type": "string"},
                        "count": {"type": "integer"},
                        "profile": {
                            "type": "object",
                            "required": ["role"],
                            "properties": {
                                "role": {"type": "string", "enum": ["admin", "operator"]},
                                "code": {"type": "string", "minLength": 3},
                            },
                        },
                    },
                },
                "output_mode": "none",
            }
        }
    )

    result = WorkflowInterfaceService().validate_inputs(
        interface,
        {
            "email": "a@example.com",
            "count": "3",
            "profile": {"role": "viewer", "code": "xy"},
            "extra": True,
        },
        channel="api",
    )

    assert result.ok is False
    codes = {item["code"] for item in result.errors}
    assert "workflow.input.invalid_type" in codes
    assert "workflow.input.unknown" in codes
    assert "workflow.input.schema_validation_failed" in codes

    ok = WorkflowInterfaceService().validate_inputs(
        interface,
        {"email": "a@example.com", "count": 3, "profile": {"role": "operator", "code": "xyz"}},
        channel="api",
    )
    assert ok.ok is True


def test_output_mode_none_does_not_require_outputs():
    interface = WorkflowInterface.from_settings(
        {"callable": {"enabled": True, "output_mode": "none"}}
    )

    result = WorkflowInterfaceService().validate_outputs(interface, {})

    assert result.ok is True
    assert result.outputs == {}
    assert result.artifacts == {}


def _returning_step(step_id: str, key: str) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "file_from_base64",
        "override": {
            "file_from_base64": {"source": "state.inputs.body", "result_target": f"state.{key}"},
            "returned_files": [{"result": "file", "key": key}],
        },
    }


def test_output_contract_never_exposes_undeclared_runtime_values():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_outputs": [{"name": "ticket", "type": "text"}],
            }
        },
        steps=[_returning_step("render", "report")],
    )

    result = WorkflowInterfaceService().validate_outputs(
        interface,
        {
            "outputs": {"ticket": "T-1", "internal_token": "secret"},
            "artifacts": {
                "report": {"artifact_id": "artifact-1"},
                "debug_dump": {"artifact_id": "artifact-secret"},
            },
        },
    )

    assert result.outputs == {"ticket": "T-1"}
    assert result.artifacts == {"report": {"artifact_id": "artifact-1"}}


def test_output_mode_none_discards_runtime_outputs_and_artifacts():
    interface = WorkflowInterface.from_settings(
        {"callable": {"enabled": True, "output_mode": "none"}}
    )

    result = WorkflowInterfaceService().validate_outputs(
        interface,
        {
            "outputs": {"internal_token": "secret"},
            "artifacts": {"debug_dump": {"artifact_id": "artifact-secret"}},
        },
    )

    assert result.outputs == {}
    assert result.artifacts == {}


def test_variables_output_mode_requires_declared_runtime_output():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "output_mode": "variables",
                "expected_outputs": [{"name": "report", "type": "text", "required": True}],
            }
        }
    )

    missing = WorkflowInterfaceService().validate_outputs(interface, {})
    present = WorkflowInterfaceService().validate_outputs(
        interface,
        {"outputs": {"report": "done"}},
    )

    assert missing.ok is False
    assert any(item["code"] == "workflow.output.required" for item in missing.errors)
    assert present.ok is True
    assert present.outputs["report"] == "done"


def test_variables_output_mode_reads_declared_outputs_from_workflow_data_root():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "output_mode": "variables",
                "expected_outputs": [{"name": "report", "type": "text", "required": True}],
            }
        }
    )

    result = WorkflowInterfaceService().validate_outputs(
        interface,
        {"workflow_data": {"report": "done"}},
    )

    assert result.ok is True
    assert result.outputs["report"] == "done"


def test_artifact_output_mode_reads_state_artifacts():
    step = _returning_step("render", "pdf")
    step["override"]["returned_files"][0]["required"] = True
    interface = WorkflowInterface.from_settings({"callable": {"enabled": True}}, steps=[step])
    assert interface.output_mode == "artifacts"

    result = WorkflowInterfaceService().validate_outputs(
        interface,
        {"artifacts": {"pdf": {"artifact_id": "art_123", "filename": "report.pdf"}}},
    )

    assert result.ok is True
    assert result.artifacts["pdf"]["artifact_id"] == "art_123"


def test_required_structured_outputs_allow_empty_collections():
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "output_mode": "variables",
                "expected_outputs": [
                    {"name": "blocking_issues", "type": "array", "required": True},
                    {"name": "missing_criteria", "type": "object", "required": True},
                ],
            }
        }
    )

    result = WorkflowInterfaceService().validate_outputs(
        interface,
        {
            "outputs": {
                "blocking_issues": [],
                "missing_criteria": {},
            }
        },
    )

    assert result.ok is True
    assert result.outputs["blocking_issues"] == []
    assert result.outputs["missing_criteria"] == {}
