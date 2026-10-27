"""Integration coverage for ``step_kind=batch``."""

from __future__ import annotations

import json

import pytest

from tests._step_run_state import step_run_state

pytestmark = [pytest.mark.integration]


def _command_update(update):
    # The node returns a plain state update; its static edge leads to the hub.
    assert isinstance(update, dict)
    return update


def test_batch_step_chunks_array_into_deterministic_groups():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.inputs.items",
                    "batch_size": 2,
                    "emit_partial_batch": True,
                    "result_target": "state.outputs.batches",
                }
            },
        },
        inputs={"items": [1, 2, 3, 4, 5]},
        agent_messages=[],
    )

    update = _command_update(control_flow_step_node(state))
    assert update["outputs"]["batches"] == [[1, 2], [3, 4], [5]]
    msg = update["agent_messages"][-1]
    assert msg["status"] == "pass"
    assert msg["artifacts"]["batch_count"] == 3


def test_batch_step_keeps_accumulated_workflow_data_out_of_message():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    prior_rows = [{"sku": f"PRIOR-{index:05d}", "qty": index} for index in range(5000)]
    current_rows = [{"sku": f"CURRENT-{index:04d}", "qty": index} for index in range(1000)]
    state = step_run_state(
        {
            "step_id": "map-chunk",
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.inputs.items",
                    "batch_size": 1000,
                    "result_target": "state.map_chunks",
                }
            },
        },
        inputs={"items": current_rows},
        workflow_data={"mapped_rows": prior_rows},
        agent_messages=[],
    )

    update = _command_update(control_flow_step_node(state))

    assert update["map_chunks"] == [current_rows]
    assert update["workflow_data"]["mapped_rows"] == prior_rows
    assert update["workflow_data"]["map_chunks"] == [current_rows]
    message = update["agent_messages"][-1]
    assert len(json.dumps(message, separators=(",", ":"))) <= 4096
    assert message["artifacts"]["result_target"] == "state.map_chunks"
    assert "_state_patch" not in message["artifacts"]
    assert "map_chunks" not in message["artifacts"]
    assert "workflow_data" not in message["artifacts"]


def test_batch_step_can_drop_partial_tail_when_disabled():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.inputs.items",
                    "batch_size": 2,
                    "emit_partial_batch": False,
                    "result_target": "state.outputs.batches",
                }
            },
        },
        inputs={"items": [1, 2, 3, 4, 5]},
        agent_messages=[],
    )

    update = _command_update(control_flow_step_node(state))
    assert update["outputs"]["batches"] == [[1, 2], [3, 4]]
