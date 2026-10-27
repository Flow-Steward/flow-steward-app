"""Secret input aliases must be opaque before Test Run durable state is built."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.management.query_models_workflow_editor import WorkflowEditorTestRunResultView
from core.application.orchestration.state import build_initial_orchestrator_state
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.workflows.platform_test_run_runtime import (
    _workflow_test_run_payload_from_state,
)
from core.application.workflows.test_run_input_bindings import apply_test_run_input_bindings
from core.application.workflows.test_run_submission_service import (
    TestRunInputRejected as InputRejected,
)
from core.application.workflows.test_run_submission_service import (
    materialize_test_run_envelope,
)
from core.infrastructure.security import secret_codec
from core.infrastructure.security.secret_reference import SecretReference, is_secret_reference


def _interface(*, default_ref: str = "") -> WorkflowInterface:
    return WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_inputs": [
                    {
                        "name": "login",
                        "type": "secret",
                        "required": True,
                        "default_secret_ref": default_ref,
                    }
                ],
                "trigger_channels": ["ui"],
                "output_mode": "none",
            }
        }
    )


def _envelope(value: Any, *, source: str = "state.inputs.login") -> dict[str, Any]:
    return {
        "inputs": {"header": value},
        "metadata": {"input_bindings": [{"name": "header", "source": source, "type": "text"}]},
    }


def _materialize(envelope: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
    return materialize_test_run_envelope(
        workflow_id="wf-secret",
        project_id="project-1",
        task_envelope=envelope,
        workflow_interface=kwargs.pop("workflow_interface", _interface()),
        step_run=True,
        **kwargs,
    )


@pytest.mark.parametrize("source", ["state.inputs.login", "state.inputs['login']"])
def test_bound_dynamic_secret_stays_opaque_in_durable_state_and_public_result(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "test-run-secret-key"})
    plaintext = "synthetic-bound-value-for-regression"
    envelope, snapshot = _materialize(_envelope(plaintext, source=source))

    assert plaintext not in repr(envelope)
    reference = envelope["metadata"]["input_binding_values"]["header"]
    assert is_secret_reference(reference)
    assert len(snapshot) == 1
    patch = apply_test_run_input_bindings({}, envelope)
    assert patch["inputs"]["login"] == reference
    assert patch["workflow_data"]["inputs"]["login"] == reference
    state = build_initial_orchestrator_state(
        task_id="task-1",
        task_summary="Validate a bound input",
        task_source="step_test_run",
        project_name="Demo",
        project_config={"project_id": "project-1"},
        test_mode=True,
        extra_state={
            **patch,
            "task_envelope": envelope,
            "_workflow_secret_ref_fingerprints": snapshot,
        },
    )
    assert state["inputs"]["login"] == reference
    assert state["workflow_data"]["inputs"]["login"] == reference
    assert plaintext not in repr(state)
    state["agent_messages"] = [
        {
            "step_id": "consume",
            "step_execution_id": "11111111-1111-4111-8111-111111111111",
            "attempt": 1,
            "status": "pass",
            "summary": "Input accepted",
            "from_agent": "template_step",
            "artifacts": {"step_kind": "template", "step_id": "consume"},
        }
    ]
    payload = _workflow_test_run_payload_from_state(
        state,
        workflow_id="wf-secret",
        project_id="project-1",
        thread_id="thread-1",
        execution_id="execution-1",
    )
    public = WorkflowEditorTestRunResultView.model_validate(payload).model_dump()
    assert "header" in public["inputs"]
    assert plaintext not in repr(public)
    assert reference["ciphertext"] not in repr(public)


@pytest.mark.parametrize("inputs", [None, {}, {"header": "ordinary"}])
def test_caller_cannot_supply_a_prefilled_binding_sidecar(inputs: Any) -> None:
    envelope = _envelope("ordinary")
    envelope["inputs"] = inputs
    envelope["metadata"]["input_binding_values"] = {"header": "untrusted-sidecar"}
    with pytest.raises(InputRejected):
        _materialize(envelope)


@pytest.mark.parametrize("nested", [False, True])
def test_bound_alias_rejects_external_secret_references(nested: bool) -> None:
    reference = SecretReference(name="login", ref_key="caller-owned-key").to_wire()
    value = {"nested": [reference]} if nested else reference
    envelope = _envelope(value)
    with pytest.raises(InputRejected):
        _materialize(envelope)


@pytest.mark.parametrize("value", ["configured-value", "", None])
def test_fixed_secret_default_forbids_alias_presence(value: Any) -> None:
    envelope = _envelope(value)
    interface = _interface(default_ref="configured-key")
    with pytest.raises(InputRejected) as exc:
        _materialize(envelope, workflow_interface=interface)
    assert any(error["code"] == "workflow.input.override_forbidden" for error in exc.value.errors)


def test_omitted_alias_uses_the_existing_secret_default() -> None:
    envelope = _envelope("unused")
    envelope["inputs"] = {}
    materialized, snapshot = _materialize(
        envelope, workflow_interface=_interface(default_ref="configured-key")
    )
    reference = SecretReference(name="login", ref_key="configured-key").to_wire()
    assert materialized["inputs"]["login"] == reference
    assert len(snapshot) == 1


@pytest.mark.parametrize("value", ["  keep whitespace  ", "", False, 0, None, {}, []])
def test_plain_bound_values_preserve_the_exact_value(value: Any) -> None:
    envelope = _envelope(value, source="state.outputs.value")
    materialized, snapshot = _materialize(envelope)
    assert materialized["metadata"]["input_binding_values"]["header"] == value
    assert type(materialized["metadata"]["input_binding_values"]["header"]) is type(value)
    assert snapshot == []


def test_bracket_root_binding_remains_ignored() -> None:
    envelope = _envelope("ordinary", source="state['inputs']['login']")
    assert apply_test_run_input_bindings({}, envelope) == {}


@pytest.mark.parametrize("secret_first", [True, False])
def test_any_authoritative_secret_row_materializes_shared_alias(
    monkeypatch, secret_first: bool
) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "test-run-secret-key"})
    secret = {"name": "header", "source": "state.outputs.value", "type": "secret"}
    plain = {"name": "header", "source": "state.outputs.label", "type": "text"}
    rows = [secret, plain] if secret_first else [plain, secret]
    envelope = _envelope("synthetic-preview-input", source="state.outputs.value")
    envelope["metadata"]["input_bindings"].append(plain)
    materialized, snapshot = _materialize(envelope, step_contract_preview={"optional": rows})
    reference = materialized["metadata"]["input_binding_values"]["header"]
    assert is_secret_reference(reference)
    assert "synthetic-preview-input" not in repr(materialized)
    assert len(snapshot) == 1
    patch = apply_test_run_input_bindings({}, materialized)
    assert patch["outputs"]["value"] == reference
    assert patch["outputs"]["label"] == reference


def test_client_secret_type_cannot_create_secret_authority() -> None:
    envelope = _envelope("  ordinary  ", source="state.outputs.value")
    envelope["metadata"]["input_bindings"][0]["type"] = "secret"
    materialized, snapshot = _materialize(envelope)
    assert materialized["metadata"]["input_binding_values"]["header"] == "  ordinary  "
    assert snapshot == []


def test_authoritative_default_cannot_be_bypassed_by_changing_binding_source() -> None:
    envelope = _envelope("synthetic-override", source="state.outputs.other")
    interface = _interface(default_ref="configured-key")
    with pytest.raises(InputRejected) as exc:
        _materialize(
            envelope,
            workflow_interface=interface,
            step_contract_preview={
                "optional": [{"name": "header", "source": "state.inputs.login", "type": "secret"}]
            },
        )
    assert any(error["code"] == "workflow.input.override_forbidden" for error in exc.value.errors)


def test_missing_dynamic_alias_is_not_coerced_as_empty() -> None:
    envelope = _envelope("unused")
    envelope["inputs"] = {}
    materialized, snapshot = _materialize(envelope)
    assert "input_binding_values" not in materialized["metadata"]
    assert snapshot == []


def test_nested_key_named_state_does_not_inherit_workflow_input_secret_policy() -> None:
    envelope = _envelope("  ordinary nested value  ", source="state.state.inputs.login")
    materialized, _snapshot = _materialize(
        envelope, workflow_interface=_interface(default_ref="configured-key")
    )
    patch = apply_test_run_input_bindings({}, materialized)
    assert patch["state"]["inputs"]["login"] == "  ordinary nested value  "
    assert materialized["metadata"]["input_binding_values"]["header"] == "  ordinary nested value  "


def test_secret_binding_authority_covers_renamed_aliases_and_parent_writes(monkeypatch) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "test-run-secret-key"})
    preview = {
        "optional": [{"name": "server_header", "source": "state.outputs.login", "type": "secret"}]
    }
    cases = {
        "renamed_alias": ("state.outputs.login", "synthetic-bound-leaf"),
        "preview_parent": (
            "state.outputs",
            {"login": "synthetic-bound-leaf", "ordinary": [False, 0, None, {}]},
        ),
        "workflow_parent": (
            "state.inputs",
            {"login": "synthetic-bound-leaf", "ordinary": [False, 0, None, {}]},
        ),
        "workflow_mirror": ("state.workflow_data.inputs.login", "synthetic-bound-leaf"),
        "preview_mirror": ("state.workflow_data.outputs.login", "synthetic-bound-leaf"),
    }
    protected = {}
    for case, (source, value) in cases.items():
        materialized, snapshot = _materialize(
            _envelope(value, source=source), step_contract_preview=preview
        )
        protected[case] = "synthetic-bound-leaf" not in repr(materialized) and len(snapshot) == 1
        if isinstance(value, dict):
            assert materialized["metadata"]["input_binding_values"]["header"]["ordinary"] == [
                False,
                0,
                None,
                {},
            ]
    assert protected == dict.fromkeys(cases, True)


@pytest.mark.parametrize("source", ["state.inputs", "state.workflow_data.inputs"])
def test_parent_object_preserves_omitted_fixed_secret_default(source: str) -> None:
    materialized, _snapshot = _materialize(
        _envelope({"ordinary": "  unchanged  "}, source=source),
        workflow_interface=_interface(default_ref="configured-key"),
    )
    value = materialized["metadata"]["input_binding_values"]["header"]
    assert value == {
        "ordinary": "  unchanged  ",
        "login": SecretReference(name="login", ref_key="configured-key").to_wire(),
    }
    patch = apply_test_run_input_bindings({}, materialized)
    assert patch["workflow_data"]["inputs"]["login"] == value["login"]
    state = build_initial_orchestrator_state(
        task_id="task-1",
        task_summary="Validate parent input",
        task_source="step_test_run",
        project_name="Demo",
        project_config={"project_id": "project-1"},
        test_mode=True,
        extra_state={
            **patch,
            "task_envelope": materialized,
            "materialized_inputs": materialized["inputs"],
        },
    )
    assert state["inputs"]["login"] == value["login"]
    assert state["workflow_data"]["inputs"]["login"] == value["login"]


@pytest.mark.parametrize(
    "source,value",
    [
        ("state.inputs", {"login": "synthetic-override"}),
        ("state.workflow_data.inputs", {"login": "synthetic-override"}),
        ("state.workflow_data.inputs.login", "synthetic-override"),
        ("state.inputs.login.name", "synthetic-override"),
    ],
)
def test_overlapping_writes_cannot_override_fixed_secret_default(source: str, value: Any) -> None:
    envelope = _envelope(value, source=source)
    interface = _interface(default_ref="configured-key")
    with pytest.raises(InputRejected) as exc:
        _materialize(envelope, workflow_interface=interface)
    assert any(error["code"] == "workflow.input.override_forbidden" for error in exc.value.errors)


def test_multiple_aliases_to_same_secret_destination_remain_opaque(monkeypatch) -> None:
    monkeypatch.setattr(secret_codec, "runtime_env", {"ENCRYPTION_KEY": "test-run-secret-key"})
    envelope = _envelope("synthetic-first", source="state.outputs.login")
    envelope["inputs"]["other"] = "synthetic-second"
    envelope["metadata"]["input_bindings"].append(
        {"name": "other", "source": "state.outputs['login']", "type": "text"}
    )
    materialized, snapshot = _materialize(
        envelope,
        step_contract_preview={
            "optional": [
                {"name": "server_header", "source": "state.outputs.login", "type": "secret"}
            ]
        },
    )
    values = materialized["metadata"]["input_binding_values"]
    assert all(is_secret_reference(value) for value in values.values())
    assert len(snapshot) == 2
    assert "synthetic-first" not in repr(materialized)
    assert "synthetic-second" not in repr(materialized)


def test_descendant_write_cannot_mutate_a_dynamic_secret_scalar() -> None:
    envelope = _envelope("synthetic-child", source="state.inputs.login.child")
    with pytest.raises(InputRejected) as exc:
        _materialize(envelope)
    assert any(error["code"] == "workflow.input.invalid_type" for error in exc.value.errors)
