from __future__ import annotations

from typing import Any

import pytest

from core.application.orchestration.run_cursor import seeded_step_cursor
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.workflows.test_run_submission_service import submit_test_run


class _JobStore:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        assert job_id == "41"
        return self.row


class _DisabledInterfaceService:
    def interface_for_workflow_id(self, _workflow_id: str) -> WorkflowInterface:
        return WorkflowInterface(enabled=False, output_mode="none")

    def validate_inputs(self, interface: WorkflowInterface, inputs: object, *, channel: str):
        return WorkflowInterfaceService().validate_inputs(interface, inputs, channel=channel)


def _submit(**overrides: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    captured: dict[str, Any] = {}

    def enqueue(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 41

    arguments: dict[str, Any] = {
        "kind": "workflow",
        "workflow_id": "wf-demo",
        "project_id": "project-1",
        "task_id": "task-1",
        "task_summary": "Validate the workflow",
        "task_envelope": {
            "title": "Validate the workflow",
            "inputs": {"project_id": "project-1", "release": "1.0"},
        },
        "project_name": "Demo",
        "project_config": {"project_id": "project-1", "account_id": "account-1"},
        "mode": "brownfield",
        "caller": {
            "actor_id": "operator-1",
            "principal_type": "user",
            "allowed_project_ids": ["project-1"],
            "allowed_workflow_ids": ["wf-demo"],
            "correlation_id": "request-123",
            "idempotency_key": "idem-123",
        },
        "workflow_interface_service": _DisabledInterfaceService(),
    }
    arguments.update(overrides)
    result = submit_test_run(
        enqueue=enqueue,
        job_store=_JobStore(
            {
                "id": 41,
                "thread_id": "thread-41",
                "status": "pending",
            }
        ),
        **arguments,
    )
    return result, captured


def test_workflow_test_run_is_enqueued_with_durable_identity_and_original_caller() -> None:
    result, captured = _submit()

    assert result == {
        "status": "queued",
        "final_status": "queued",
        "job_id": "41",
        "thread_id": "thread-41",
        "execution_id": captured["meta"]["execution_id"],
        "workflow_id": "wf-demo",
        "project_id": "project-1",
        "project_name": "Demo",
        "task_id": "task-1",
        "task_summary": "Validate the workflow",
        "mode": "brownfield",
        "test_mode": True,
        "needs_user_approval": False,
    }
    assert captured["task_source"] == "workflow_test_run"
    assert captured["test_mode"] is True
    assert captured["extra_state"]["debug_test_run"] is True
    assert captured["extra_state"]["workflow_id"] == "wf-demo"
    assert captured["extra_state"]["inputs"] == {
        "project_id": "project-1",
        "release": "1.0",
    }
    assert captured["meta"]["actor_id"] == "operator-1"
    assert captured["meta"]["principal_type"] == "user"
    assert captured["meta"]["allowed_project_ids"] == ["project-1"]
    assert captured["meta"]["allowed_workflow_ids"] == ["wf-demo"]
    assert captured["meta"]["correlation_id"] == "request-123"
    assert captured["meta"]["idempotency_key"] == "idem-123"


def test_step_test_run_enqueues_only_the_selected_step_for_worker_execution() -> None:
    step = {
        "step_id": "compile",
        "step_kind": "set_variable",
        "display_name": "Compile",
        "override": {"set_variable": {"target": "state.result", "value": "ok"}},
    }

    result, captured = _submit(kind="step", step=step)

    execution_id = captured["meta"]["execution_id"]
    extra_state = captured["extra_state"]
    assert captured["task_source"] == "step_test_run"
    # The run starts at the selected step of the job's pinned definition; the
    # job pins it because the workflow is the job's selected workflow.
    assert extra_state["workflow_id"] == "wf-demo"
    assert extra_state["run_cursor"] == seeded_step_cursor("compile", seed=execution_id).dump()
    assert extra_state["run_cursor"]["attempt"] == 1
    assert extra_state["current_step_id"] == "compile"
    assert extra_state["step_test_run_context"] == {
        "workflow_id": "wf-demo",
        "step_id": "compile",
        "execution_id": execution_id,
    }
    for removed in ("phase_stack", "current_phase", "current_step"):
        assert removed not in extra_state
    assert "phase_id" not in captured["meta"]
    assert captured["meta"]["step_test_run"] is True
    assert captured["meta"]["step_id"] == "compile"
    assert captured["extra_state"]["task_envelope"]["metadata"]["step_id"] == "compile"
    assert "phase_id" not in result
    assert result["step_id"] == "compile"
    assert result["step_kind"] == "set_variable"


def test_step_test_run_requires_a_step_id() -> None:
    with pytest.raises(ValueError, match="require a step"):
        _submit(kind="step", step={"step_kind": "set_variable"})


def test_submission_fails_if_committed_job_identity_cannot_be_read_back() -> None:
    _raises_input_148_1 = _JobStore(None)
    _raises_input_148_2 = _DisabledInterfaceService()
    with pytest.raises(RuntimeError, match="durable thread identity"):
        submit_test_run(
            enqueue=lambda **_kwargs: 41,
            job_store=_raises_input_148_1,
            kind="workflow",
            workflow_id="wf-demo",
            project_id="project-1",
            task_id="task-1",
            task_summary="Validate",
            task_envelope={},
            project_name="Demo",
            project_config={"project_id": "project-1"},
            workflow_interface_service=_raises_input_148_2,
        )


def test_dynamic_secret_is_materialized_before_test_run_persistence_and_hidden_from_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.security import secret_codec
    from core.infrastructure.security.prompt_safety import REDACTED_CREDENTIAL_MARKER
    from core.infrastructure.security.secret_reference import is_secret_reference

    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "test-run-secret-key"})
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [{"name": "login", "type": "secret", "required": True}],
                "trigger_channels": ["ui"],
                "output_mode": "none",
            }
        }
    )

    class _InterfaceService:
        def interface_for_workflow_id(self, workflow_id: str) -> WorkflowInterface:
            assert workflow_id == "wf-demo"
            return interface

        def validate_inputs(self, loaded: WorkflowInterface, inputs: object, *, channel: str):
            return WorkflowInterfaceService().validate_inputs(loaded, inputs, channel=channel)

    plaintext = "dynamic-secret-value"
    result, captured = _submit(
        task_envelope={"inputs": {"project_id": "project-1", "login": plaintext}},
        workflow_interface_service=_InterfaceService(),
    )

    durable = captured["extra_state"]
    assert plaintext not in repr(durable)
    assert is_secret_reference(durable["inputs"]["login"])
    assert "task_envelope" not in captured["meta"]
    assert "task_envelope" not in result
    assert REDACTED_CREDENTIAL_MARKER not in repr(durable)


class _StrictInterfaceService:
    """A workflow whose public interface requires `city` and accepts nothing else."""

    def interface_for_workflow_id(self, _workflow_id: str) -> WorkflowInterface:
        from core.application.registry.workflow_contract import WorkflowContractField

        return WorkflowInterface(
            enabled=True,
            strict_inputs=True,
            trigger_channels=["api"],
            expected_inputs=[WorkflowContractField(name="city", type="string", required=True)],
            output_mode="none",
        )

    def validate_inputs(self, interface: WorkflowInterface, inputs: object, *, channel: str):
        return WorkflowInterfaceService().validate_inputs(interface, inputs, channel=channel)


def _template_step() -> dict[str, Any]:
    return {
        "step_id": "compose",
        "step_kind": "template",
        "override": {"template": {"text": "{{ body }}", "result_target": "state.outputs.text"}},
    }


def test_a_step_run_is_not_judged_by_the_workflows_public_inputs() -> None:
    from core.application.workflows.test_run_input_bindings import apply_test_run_input_bindings

    _result, captured = _submit(
        kind="step",
        step=_template_step(),
        workflow_interface_service=_StrictInterfaceService(),
        task_envelope={
            "inputs": {"project_id": "project-1", "body": "Body text", "footer": "Bye"},
            "metadata": {
                "input_bindings": [
                    {"name": "body", "source": "state.outputs.body", "type": "text"},
                    {"name": "footer", "source": "state.footer", "type": "text"},
                ]
            },
        },
    )

    envelope = captured["extra_state"]["task_envelope"]
    # The step's own values are not workflow inputs...
    assert "body" not in envelope["inputs"]
    assert "body" not in captured["extra_state"]["inputs"]
    # ...they land at the state paths the step reads.
    patch = apply_test_run_input_bindings({}, envelope)
    assert patch["outputs"] == {"body": "Body text"}
    assert patch["footer"] == "Bye"


def test_a_step_run_still_rejects_an_invalid_workflow_input() -> None:
    from core.application.workflows.test_run_submission_service import TestRunInputRejected

    step = _template_step()
    interfaces = _StrictInterfaceService()
    with pytest.raises(TestRunInputRejected) as rejected:
        _submit(
            kind="step",
            step=step,
            workflow_interface_service=interfaces,
            task_envelope={"inputs": {"project_id": "project-1", "unknown": "x"}},
        )

    assert [row["code"] for row in rejected.value.errors] == ["workflow.input.unknown"]


def test_a_workflow_run_still_requires_its_public_inputs() -> None:
    from core.application.workflows.test_run_submission_service import TestRunInputRejected

    interfaces = _StrictInterfaceService()
    with pytest.raises(TestRunInputRejected) as rejected:
        _submit(workflow_interface_service=interfaces)

    assert "workflow.input.required" in [row["code"] for row in rejected.value.errors]


def test_the_revision_the_builder_shows_is_recorded_for_the_atomic_pin_check() -> None:
    _result, captured = _submit(expected_revision=7)
    assert captured["meta"]["expected_workflow_revision"] == 7

    _result, captured = _submit()
    assert "expected_workflow_revision" not in captured["meta"]


def test_the_route_refuses_a_test_run_of_a_revision_it_no_longer_shows() -> None:
    from fastapi import HTTPException

    from core.entrypoints.admin.workflows_definition_run_routes import (
        _assert_expected_revision,
    )

    _assert_expected_revision("wf-demo", {"revision": 4}, 4)
    _assert_expected_revision("wf-demo", {"revision": 4}, None)
    with pytest.raises(HTTPException) as refused:
        _assert_expected_revision("wf-demo", {"revision": 5}, 4)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "workflow_revision_conflict"
    assert (refused.value.detail["expected_revision"], refused.value.detail["revision"]) == (4, 5)


@pytest.mark.parametrize(
    "name,value", [("method", "DELETE"), ("url", "https://example.test/other")]
)
def test_http_step_test_rejects_unbound_configuration_as_workflow_inputs(name, value):
    """API and CLI cannot turn strict interface validation into a config override."""
    from core.application.workflows.test_run_submission_service import TestRunInputRejected

    step = {
        "step_id": "fetch",
        "step_kind": "http_request",
        "override": {"http_request": {"method": "GET", "url": "https://example.test"}},
    }
    interface_service = _StrictInterfaceService()
    with pytest.raises(TestRunInputRejected) as rejected:
        _submit(
            kind="step",
            step=step,
            workflow_interface_service=interface_service,
            task_envelope={"inputs": {"project_id": "project-1", name: value}},
        )
    assert [row["code"] for row in rejected.value.errors] == ["workflow.input.unknown"]
    assert step["override"]["http_request"] == {"method": "GET", "url": "https://example.test"}


def _required_step_issue(name="request", **patch):
    return {
        "code": "workflow.step_io.error",
        "issue_type": "step_required_input_unavailable",
        "step_id": "compile",
        "input_name": name,
        "path": f"workflow.steps.compile.inputs.{name}",
        "message": "Required input is unavailable",
        **patch,
    }


def test_supplied_step_value_cannot_override_configured_workflow_default():
    from core.application.registry.workflow_contract import WorkflowContractField
    from core.application.workflows.test_run_submission_service import TestRunInputRejected

    interface = WorkflowInterface(
        enabled=True,
        trigger_channels=["ui"],
        expected_inputs=[
            WorkflowContractField(
                name="request", type="text", required=True, default="Configured request"
            )
        ],
    )
    required_issue = _required_step_issue()
    with pytest.raises(TestRunInputRejected) as refused:
        _submit(
            kind="step",
            step={"step_id": "compile", "step_kind": "agent"},
            workflow_interface=interface,
            task_envelope={"inputs": {"request": "Caller replacement"}},
            step_contract_preview={
                "unresolved_required": [
                    {"name": "request", "type": "text", "required": True, "source": ""}
                ]
            },
            step_validation={"valid": False, "errors": [required_issue]},
        )
    assert "workflow.input.override_forbidden" in [row["code"] for row in refused.value.errors]


@pytest.mark.parametrize(
    "field_type,value,expected",
    [
        ("number", "0", 0),
        ("boolean", False, False),
        ("array", "[0, false]", [0, False]),
        ("file", {"artifact_id": "file-1"}, {"artifact_id": "file-1"}),
    ],
)
def test_supplied_step_fields_keep_canonical_type_coercion(field_type, value, expected):
    result, captured = _submit(
        kind="step",
        step={"step_id": "compile", "step_kind": "agent"},
        task_envelope={"inputs": {"request": value}},
        step_contract_preview={
            "unresolved_required": [
                {"name": "request", "type": field_type, "required": True, "source": ""}
            ]
        },
        step_validation={"valid": False, "errors": [_required_step_issue()]},
    )
    assert result["status"] == "queued"
    assert captured["extra_state"]["materialized_inputs"]["request"] == expected
    assert type(captured["extra_state"]["materialized_inputs"]["request"]) is type(expected)


@pytest.mark.parametrize(
    "issue",
    [
        _required_step_issue(name="other"),
        _required_step_issue(step_id="another"),
        _required_step_issue(issue_type="expected_input_source_unavailable"),
        {
            "code": "workflow.agent_tool_unavailable",
            "step_id": "compile",
            "message": "Selected dependency is disabled",
        },
        {
            "code": "workflow.step_contract.agent_not_found",
            "path": "workflow.steps.compile",
            "message": "Selected Agent is unavailable",
        },
        {
            "code": "workflow.interface.invalid_type",
            "path": "workflow.settings.callable.expected_inputs[0].type",
            "message": "Invalid declaration",
        },
    ],
)
def test_supplied_step_value_discharges_only_its_matching_structured_issue(issue):
    from core.application.workflows.test_run_submission_service import TestRunReadinessRejected

    required_issue = _required_step_issue()
    with pytest.raises(TestRunReadinessRejected) as refused:
        _submit(
            kind="step",
            step={"step_id": "compile", "step_kind": "agent"},
            task_envelope={"inputs": {"request": "Supplied request"}},
            step_contract_preview={
                "unresolved_required": [
                    {"name": "request", "type": "text", "required": True, "source": ""}
                ]
            },
            step_validation={
                "valid": False,
                "errors": [required_issue, issue],
                "warnings": [],
            },
        )
    assert refused.value.validation["errors"] == [issue]


@pytest.mark.parametrize("validation", [{}, {"valid": False, "errors": [], "warnings": []}])
def test_supplied_step_values_cannot_waive_an_unexplained_invalid_readiness_report(validation):
    from core.application.workflows.test_run_submission_service import TestRunReadinessRejected

    with pytest.raises(TestRunReadinessRejected):
        _submit(
            kind="step",
            step={"step_id": "compile", "step_kind": "agent"},
            task_envelope={"inputs": {"request": "Supplied request"}},
            step_contract_preview={
                "unresolved_required": [
                    {"name": "request", "type": "text", "required": True, "source": ""}
                ]
            },
            step_validation=validation,
        )
