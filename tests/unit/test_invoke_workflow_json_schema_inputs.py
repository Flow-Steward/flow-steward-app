"""Schema-governed child calls must retain valid empty values verbatim."""

import pytest

from tests.unit.test_invoke_workflow_step_node import (
    _application_submission_repositories as _application_submission_repositories,
)
from tests.unit.test_invoke_workflow_step_node import (
    _base_state,
    _capture_append_messages,
    _FakeLogger,
    _install_runtime_stubs,
)


@pytest.mark.parametrize(
    ("value", "schema_type"),
    [([], "array"), ({}, "object"), ("", "string"), ("  text  ", "string"), (None, "null")],
)
@pytest.mark.usefixtures("_application_submission_repositories")
def test_json_schema_child_receives_valid_empty_values(monkeypatch, value, schema_type):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "input_mode": "json_schema",
                        "input_schema": {
                            "type": "object",
                            "properties": {"payload": {"type": schema_type}},
                            "required": ["payload"],
                        },
                        "expected_outputs": [{"name": "plan", "type": "object"}],
                    }
                },
            }
        },
    )
    messages = _capture_append_messages(monkeypatch, node)
    logger = _FakeLogger(child_job_id=932, child_messages=[])
    monkeypatch.setattr(node, "_get_logger", lambda: logger)
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent: False)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda *_args, **_kwargs: ({"id": 932, "status": "completed", "error": ""}, "completed"),
    )
    step = {
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"payload": "state.payload"},
            }
        },
    }
    state = _base_state(step)
    state["payload"] = value
    node.invoke_workflow_step_node(state)

    assert runtime["enqueue_calls"], messages
    inputs = runtime["enqueue_calls"][0]["extra_state"]["inputs"]
    assert "payload" in inputs
    assert inputs["payload"] == value


@pytest.mark.usefixtures("_application_submission_repositories")
def test_json_schema_omits_absent_optional_source_instead_of_synthesizing_null(monkeypatch):
    from core.application.orchestration import invoke_workflow_step_node as node

    runtime = _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "input_mode": "json_schema",
                        "input_schema": {
                            "type": "object",
                            "properties": {"payload": {"type": ["string", "null"]}},
                        },
                        "expected_outputs": [{"name": "plan", "type": "object"}],
                    }
                },
            }
        },
    )
    messages = _capture_append_messages(monkeypatch, node)
    monkeypatch.setattr(
        node, "_get_logger", lambda: _FakeLogger(child_job_id=932, child_messages=[])
    )
    monkeypatch.setattr(node, "_should_execute_child_inline", lambda _parent: False)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda *_args, **_kwargs: ({"id": 932, "status": "completed", "error": ""}, "completed"),
    )
    step = {
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"payload": "state.absent"},
            }
        },
    }
    node.invoke_workflow_step_node(_base_state(step))
    assert runtime["enqueue_calls"], messages
    assert "payload" not in runtime["enqueue_calls"][0]["extra_state"]["inputs"]
