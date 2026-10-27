from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests._step_run_state import step_run_state


def _latest_message(update):
    payload = _command_update(update)
    return payload["agent_messages"][-1]


def _command_update(update):
    # The node returns a plain state update; its static edge leads to the hub.
    assert isinstance(update, dict)
    return update


@pytest.mark.parametrize(
    ("step_kind", "config"),
    [
        ("if", {"condition": "state.rows[abc].sku"}),
        (
            "if",
            {
                "condition": {"in": {"left": "state.rows[abc].sku", "right": ["A1"]}},
            },
        ),
        (
            "if",
            {
                "condition": {"not_in": {"left": "state.rows[abc].sku", "right": ["A1"]}},
            },
        ),
        (
            "switch",
            {"selector": "state.rows[abc].sku", "cases": [{"case_id": "a", "when": "A1"}]},
        ),
        (
            "set_variable",
            {"target": "state.out", "value": {"source": "state.rows[abc].sku"}},
        ),
        (
            "batch",
            {
                "source_path": "state.rows[abc]",
                "result_target": "state.batches",
                "batch_size": 10,
            },
        ),
    ],
)
def test_malformed_state_selector_is_typed_before_control_flow_mutation(
    step_kind: str, config: dict
) -> None:
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "malformed",
            "step_kind": step_kind,
            "override": {step_kind: config},
        },
        rows=[{"sku": "A1"}],
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        command = control_flow_step_node(state)

    payload = _command_update(command)
    message = payload["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "invalid_input_selector"
    assert (
        message["artifacts"]["selector"] == "state.rows[abc].sku"
        or message["artifacts"]["selector"] == "state.rows[abc]"
    )
    assert "out" not in payload
    assert "batches" not in payload
    assert "iterations" not in payload


@pytest.mark.parametrize(("ready", "outcome"), [(True, "true"), (False, "false")])
def test_if_reports_its_outcome_and_leaves_routing_to_the_hub(ready: bool, outcome: str):
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "choose",
            "step_kind": "if",
            "override": {"if": {"condition": {"selector": "state.ready", "operator": "truthy"}}},
        },
        {"step_id": "next", "step_kind": "transform"},
        ready=ready,
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    assert isinstance(update, dict)
    assert set(update) == {"agent_messages"}
    message = update["agent_messages"][-1]
    assert message["status"] == "pass"
    assert message["artifacts"]["_outcome"] == {"name": outcome}
    assert message["artifacts"]["result"] is ready
    assert "target_phase" not in message["artifacts"]
    cursor = state["run_cursor"]
    assert (message["step_id"], message["step_execution_id"], message["attempt"]) == (
        "choose",
        cursor["step_execution_id"],
        cursor["attempt"],
    )


def test_set_variable_persists_dynamic_value_in_workflow_data():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "target": "state.weather_query",
                    "value": {"city": "Berlin", "latitude": 52.52},
                }
            },
        },
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    payload = _command_update(update)
    assert isinstance(update, dict)
    assert payload["weather_query"]["city"] == "Berlin"
    assert payload["workflow_data"]["weather_query"]["latitude"] == 52.52


def test_set_variable_keeps_selector_copied_bulk_value_out_of_message():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    rows = [{"sku": f"SKU-{index:05d}", "qty": index} for index in range(5000)]
    state = step_run_state(
        {
            "step_id": "copy-rows",
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "target": "state.copied_rows",
                    "value": {"source": "state.source_rows"},
                }
            },
        },
        source_rows=rows,
        workflow_data={"prior": rows},
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = _command_update(control_flow_step_node(state))

    assert update["copied_rows"] == rows
    assert update["workflow_data"]["copied_rows"] == rows
    message = update["agent_messages"][-1]
    assert len(json.dumps(message, separators=(",", ":"))) <= 4096
    assert message["artifacts"]["result_targets"] == ["state.copied_rows"]
    assert "_state_patch" not in message["artifacts"]
    assert "copied_rows" not in message["artifacts"]
    assert "workflow_data" not in message["artifacts"]


def test_set_variable_rejects_reserved_runtime_scope_targets():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "assignments": {
                        "state.account_id": "acct-victim",
                        "state.test_mode": True,
                        "state.safe_value": "ok",
                    }
                }
            },
        },
        account_id="acct-caller",
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    payload = _command_update(update)
    message = payload["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "set_variable_reserved_target"
    assert "account_id" not in payload
    assert "test_mode" not in payload
    assert "safe_value" not in payload


def test_set_variable_rejects_host_owned_test_mode_target():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "target": "state.test_mode",
                    "value": True,
                }
            },
        },
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        payload = _command_update(control_flow_step_node(state))

    message = payload["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "set_variable_reserved_target"
    assert message["artifacts"]["blocked_targets"] == ["state.test_mode"]
    assert "test_mode" not in payload


def test_set_variable_copies_source_value_from_state():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "assignments": {"state.target_value": {"source": "state.source_value"}}
                }
            },
        },
        workflow_data={"source_value": {"city": "Vilnius", "score": 7}},
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    payload = _command_update(update)
    assert payload["target_value"] == {"city": "Vilnius", "score": 7}
    assert payload["workflow_data"]["target_value"] == {"city": "Vilnius", "score": 7}


def test_set_variable_preserves_present_null_over_runtime_public_output():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "assignments": {"state.approved_subject": {"source": "state.approved_subject"}}
                }
            },
        },
        approved_subject=None,
        agent_messages=[
            {
                "step_id": "primary_execution",
                "from_agent": "sample_launch_copywriter",
                "status": "pass",
                "artifacts": {
                    "approved_subject": "Launch the Flow Steward demo workflow extension"
                },
            }
        ],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    payload = _command_update(update)
    assert payload["approved_subject"] is None
    assert payload["workflow_data"]["approved_subject"] is None


def test_set_variable_keeps_non_source_objects_literal():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    literal_value = {"source": "state.source_value", "mode": "literal"}
    state = step_run_state(
        {
            "step_kind": "set_variable",
            "override": {"set_variable": {"assignments": {"state.literal_value": literal_value}}},
        },
        workflow_data={"source_value": "copied"},
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    payload = _command_update(update)
    assert payload["literal_value"] == literal_value
    assert payload["workflow_data"]["literal_value"] == literal_value


def test_http_request_template_reads_workflow_data_fallback():
    from core.application.orchestration.http_request_step_runtime import _render_value

    rendered = _render_value(
        {
            "latitude": "{{ state.weather_query.latitude }}",
            "longitude": "{{ state.weather_query.longitude }}",
        },
        {
            "workflow_data": {
                "weather_query": {
                    "latitude": 52.52,
                    "longitude": 13.41,
                }
            }
        },
    )

    assert rendered == {"latitude": "52.52", "longitude": "13.41"}


def test_legacy_if_condition_selector_operator_value_evaluates_supported_operators():
    from core.application.orchestration.control_flow_step_node import _evaluate_condition

    state = {
        "inputs": {"city": "Berlin", "score": 5, "tags": ["weather", "demo"]},
        "workflow_data": {"ready": True},
    }

    assert _evaluate_condition(
        {"selector": "state.inputs.city", "operator": "equals", "value": "Berlin"}, state
    )
    assert not _evaluate_condition(
        {"selector": "state.inputs.city", "operator": "not_equals", "value": "Berlin"}, state
    )
    assert not _evaluate_condition(
        {"selector": "state.inputs.tags", "operator": "contains", "value": "missing"}, state
    )
    assert _evaluate_condition(
        {"selector": "state.inputs.city", "operator": "in", "value": ["Berlin", "Paris"]}, state
    )
    assert _evaluate_condition(
        {"selector": "state.inputs.city", "operator": "not_in", "value": ["Paris", "Rome"]},
        state,
    )
    assert not _evaluate_condition(
        {"selector": "state.inputs.score", "operator": "gt", "value": 10}, state
    )
    assert _evaluate_condition(
        {"selector": "state.workflow_data.ready", "operator": "truthy"}, state
    )
    assert _evaluate_condition({"operator": "always_true"}, state)
    assert not _evaluate_condition({"operator": "always_false"}, state)


SWITCH_STEP = {
    "step_id": "route_channel",
    "step_kind": "switch",
    "override": {
        "switch": {
            "selector": "state.inputs.channel",
            "cases": [
                {"case_id": "email", "when": "email", "label": "Email"},
                {"case_id": "social", "when": "social"},
            ],
        }
    },
}


@pytest.mark.parametrize(
    ("channel", "outcome"),
    [
        ("email", {"name": "case", "case_id": "email"}),
        ("social", {"name": "case", "case_id": "social"}),
        ("blog", {"name": "default"}),
    ],
)
def test_switch_reports_the_matched_case_or_default(channel: str, outcome: dict):
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(SWITCH_STEP, inputs={"channel": channel})

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    assert isinstance(update, dict)
    assert set(update) == {"agent_messages"}
    msg = _latest_message(update)
    assert msg["status"] == "pass"
    assert msg["artifacts"]["_outcome"] == outcome
    assert msg["artifacts"]["selector"] == channel
    expected_output = dict(outcome)
    if channel == "email":
        expected_output["label"] = "Email"
    assert msg["artifacts"]["output_payload"] == expected_output


@pytest.mark.parametrize("matched", [False, True], ids=["default", "matched"])
@pytest.mark.parametrize(
    ("inputs", "selector_value", "when", "reason"),
    [
        ({}, None, "", "has no value"),
        ({"channel": None}, None, "", "has no value"),
        ({"channel": False}, False, "False", "resolved to false"),
        ({"channel": 0}, 0, "0", "resolved to 0"),
        ({"channel": ""}, "", None, 'resolved to ""'),
        ({"channel": "email"}, "email", "email", 'resolved to "email"'),
        ({"channel": "social"}, "social", "social", 'resolved to "social"'),
        ({"channel": "unknown"}, "unknown", "unknown", 'resolved to "unknown"'),
        (
            {"channel": {"ready": False, "count": 0}},
            {"ready": False, "count": 0},
            {"ready": False, "count": 0},
            'resolved to {"ready": false, "count": 0}',
        ),
    ],
    ids=["missing", "null", "false", "zero", "empty", "email", "social", "unmatched", "object"],
)
def test_switch_reason_preserves_typed_artifacts_and_case_default_outcome(
    inputs, selector_value, when, reason, matched
):
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    cases = [{"case_id": "chosen", "when": when, "label": "Owner branch"}] if matched else []
    step = {
        "step_id": "route_channel",
        "step_kind": "switch",
        "override": {"switch": {"selector": "state.inputs.channel", "cases": cases}},
    }
    state = step_run_state(step, inputs=inputs)
    cursor = dict(state["run_cursor"])
    expected_outcome = {"name": "case", "case_id": "chosen"} if matched else {"name": "default"}
    expected_output = {**expected_outcome, "label": "Owner branch"} if matched else expected_outcome
    taken = "case 'chosen'" if matched else "the default case"

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message", return_value=True
    ):
        update = control_flow_step_node(state)

    message = _latest_message(update)
    assert message["summary"] == f"switch selector {reason}: {taken}"
    assert set(update) == {"agent_messages"}
    assert message["status"] == "pass"
    artifacts = message["artifacts"]
    assert artifacts["selector"] == selector_value
    assert type(artifacts["selector"]) is type(selector_value)
    assert artifacts["input_payload"] == {
        "selector": "state.inputs.channel",
        "value": selector_value,
    }
    assert type(artifacts["input_payload"]["value"]) is type(selector_value)
    assert artifacts["_outcome"] == expected_outcome
    assert artifacts["output_payload"] == expected_output
    assert set(artifacts) == {
        "step_kind",
        "step_id",
        "selector",
        "_outcome",
        "input_payload",
        "output_payload",
        "summary_key",
        "summary_params",
    }
    assert state["run_cursor"] == cursor
    assert state["inputs"] == inputs


def test_switch_case_order_decides_between_equal_when_values():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    step = {
        "step_id": "route",
        "step_kind": "switch",
        "override": {
            "switch": {
                "selector": "state.count",
                "cases": [{"case_id": "first", "when": 3}, {"case_id": "second", "when": "3"}],
            }
        },
    }
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        msg = _latest_message(control_flow_step_node(step_run_state(step, count=3)))

    assert msg["artifacts"]["_outcome"] == {"name": "case", "case_id": "first"}


def test_stop_success_reports_its_result_and_leaves_the_terminal_to_the_hub():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "done",
            "step_kind": "stop_success",
            "override": {"stop_success": {"message": "done"}},
        }
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    assert isinstance(update, dict)
    assert set(update) == {"agent_messages"}
    msg = _latest_message(update)
    assert msg["status"] == "pass"
    assert msg["step_id"] == "done"
    assert msg["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert msg["artifacts"]["terminal"] == "pass"
    assert msg["artifacts"]["reason"] == "done"
    assert "workflow_terminal" not in msg["artifacts"]
    assert "_state_patch" not in msg["artifacts"]


def test_stop_success_refuses_to_overwrite_unresolved_step_failure():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    unresolved_failure = {
        "status": "fail",
        "step_id": "load_demo_dataset",
        "correlation_id": "corr-dataset-fail",
        "error_code": "dataset_query_failed",
        "message": "Dataset Query failed: Dataset is not assigned to this project",
        "occurrence": 1,
    }
    state = step_run_state(
        {
            "step_id": "workflow_success",
            "step_kind": "stop_success",
            "override": {"stop_success": {"message": "done"}},
        },
        unresolved_step_failure=unresolved_failure,
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    assert isinstance(update, dict)
    assert set(update) == {"agent_messages"}
    message = _latest_message(update)
    assert message["status"] == "fail"
    assert message["summary"] == unresolved_failure["message"]
    assert message["artifacts"]["error"] == "unresolved_step_failure"


def test_set_variable_records_the_state_values_it_read_and_its_name() -> None:
    """Job Details showed "No step inputs were recorded" for a copy from state."""
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "init",
            "display_name": "Init",
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "assignments": {
                        "state.requested_city": {"source": "state.inputs.city"},
                        "state.mode": "fast",
                    }
                }
            },
        },
        inputs={"city": "Vilnius"},
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        message = _latest_message(control_flow_step_node(state))

    assert message["status"] == "pass"
    assert message["artifacts"]["step_display_name"] == "Init"
    assert message["artifacts"]["input_payload"] == {"state.inputs.city": "Vilnius"}
    assert message["artifacts"]["output_payload"]["state.requested_city"] == "Vilnius"


def test_set_variable_with_a_literal_records_no_inputs() -> None:
    """The target it writes is not an input: it showed as "state.verdict: —"."""
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "mark",
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": "state.verdict", "value": "capital"}},
        },
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        message = _latest_message(control_flow_step_node(state))

    assert "input_payload" not in message["artifacts"]
    assert message["artifacts"]["output_payload"] == {"state.verdict": "capital"}


def test_a_stop_step_reports_how_it_ended_the_run() -> None:
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "done",
            "step_kind": "stop_success",
            "override": {"stop_success": {"message": "All cities checked"}},
        },
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        message = _latest_message(control_flow_step_node(state))

    assert message["artifacts"]["output_payload"] == {
        "workflow": "completed",
        "message": "All cities checked",
    }


def test_batch_records_what_it_took_and_produced() -> None:
    """It showed "No step inputs/outputs were recorded"."""
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    rows = [{"sku": f"SKU-{index:03d}", "name": "x" * 40} for index in range(25)]
    config = {"source_path": "state.rows", "result_target": "state.batches", "batch_size": 10}
    state = step_run_state(
        {"step_id": "s", "step_kind": "batch", "override": {"batch": config}},
        rows=rows,
        agent_messages=[],
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        message = _latest_message(control_flow_step_node(state))

    artifacts = message["artifacts"]
    assert message["status"] == "pass", artifacts
    assert artifacts["input_payload"]["items"] == 25
    assert artifacts["output_payload"]["batches"] == 3
    assert artifacts["output_payload"]["batch_sizes"] == [10, 10, 5]
    assert len(json.dumps(message, separators=(",", ":"))) <= 4096


def test_a_stop_fail_step_reports_the_author_message_as_its_reason() -> None:
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "reject_order",
            "step_kind": "stop_fail",
            "override": {"stop_fail": {"reason": "too_large", "message": "Needs manual review"}},
        }
    )

    with patch(
        "core.application.orchestration.agents.common.publish_agent_message",
        return_value=True,
    ):
        update = control_flow_step_node(state)

    assert set(update) == {"agent_messages"}
    message = _latest_message(update)
    assert message["status"] == "pass"
    assert message["artifacts"]["terminal"] == "fail"
    assert message["artifacts"]["reason"] == "Needs manual review"
    assert message["artifacts"]["output_payload"] == {
        "workflow": "failed",
        "message": "Needs manual review",
        "reason": "too_large",
    }
