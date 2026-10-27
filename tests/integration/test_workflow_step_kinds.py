"""Integration coverage for runtime step-kind convergence."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.infrastructure.db.schema_state import read_canonical_schema_sql
from tests._step_run_state import step_run_state

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import core.application.orchestration.agents.common as agents_common
    from core.application.orchestration.run_definition import clear_run_definition_cache

    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    yield
    clear_run_definition_cache()


SUPPORTED_KIND_STEPS = [
    {
        "step_id": "draft_outline",
        "step_kind": "agent",
        "agent_id": "researcher",
        "skill": "draft_outline",
        "context_inject": {"task": "input"},
        "autonomy": {"mode": "manual"},
        "error_policy": {"on_error": "fail"},
    },
    {
        "step_id": "fetch_keywords",
        "step_kind": "connector",
        "connector_id": "connector.semrush",
        "override": {"connector": {"operation_kind": "source"}},
    },
    {
        "step_id": "child_call",
        "step_kind": "invoke_workflow",
        "workflow_id": "child_workflow",
        "override": {"invoke_workflow": {"workflow_id": "child_workflow"}},
    },
    {"step_id": "check", "step_kind": "if", "override": {"if": {"condition": "True"}}},
]


def test_pinned_definition_keeps_the_authored_step_shape_for_supported_step_kinds():
    """Executors read the authored step from the pinned definition, never a planned copy."""
    from core.application.orchestration.active_step import active_step

    for index, authored in enumerate(SUPPORTED_KIND_STEPS):
        others = [step for position, step in enumerate(SUPPORTED_KIND_STEPS) if position != index]
        current = active_step(step_run_state(authored, *others))

        assert current is not None
        assert current.step_id == authored["step_id"]
        assert current.step_kind == authored["step_kind"]
        assert {key: current.step[key] for key in authored} == authored


@pytest.mark.parametrize(
    ("step", "node"),
    [
        ({"step_kind": "agent", "agent_id": "researcher"}, "researcher"),
        ({"step_kind": "connector", "connector_id": "connector.semrush"}, "execute_step"),
        (
            {
                "step_kind": "dataset_query",
                "override": {"dataset_query": {"query_template_id": "query-1"}},
            },
            "dataset_query_step",
        ),
        (
            {
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"workflow_id": "wf_child"}},
            },
            "invoke_workflow_step",
        ),
        ({"step_kind": "if", "override": {"if": {"condition": "True"}}}, "control_flow_step"),
        ({"step_kind": "approval"}, "human_approval_gate"),
    ],
    ids=["agent", "connector", "dataset_query", "invoke_workflow", "if", "approval"],
)
def test_each_step_kind_has_its_executor_node(step, node):
    from core.application.orchestration.step_hub_support import executor_node_for_step

    assert executor_node_for_step({"step_id": "s", **step}) == node


@pytest.mark.parametrize("step_kind", ["agent", ""])
def test_agent_wrapper_is_not_a_child_runtime_actor(step_kind):
    from core.application.orchestration.step_hub_support import executor_node_for_step

    step = {"agent": "planner", "override": {"invoke_workflow": {"workflow_id": "wf_child"}}}
    if step_kind:
        step["step_kind"] = step_kind
    assert executor_node_for_step(step) == "planner"


def test_a_wait_step_runs_on_the_durable_wait_node():
    from core.application.orchestration.step_hub_support import (
        WAIT_NODE,
        executor_node_for_step,
    )

    assert executor_node_for_step({"step_id": "pause", "step_kind": "wait"}) == WAIT_NODE


def test_hub_dispatches_a_pinned_dataset_query_step_to_the_dataset_actor():
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests._step_run_state import pinned_definition, prime

    definition = pinned_definition(
        {
            "step_id": "load_open_orders",
            "step_kind": "dataset_query",
            "override": {"dataset_query": {"query_template_id": "query-1"}},
        },
        {"step_id": "done", "step_kind": "stop_success"},
    )
    state = {"workflow_id": "wf_dataset_query", "agent_messages": [], **prime(definition)}

    command = orchestrator_hub_node(state)

    assert command.goto == "dataset_query_step"
    assert command.update["current_step_id"] == "load_open_orders"
    assert command.update["dispatched_step_execution"]["step_id"] == "load_open_orders"


def test_graph_routes_dataset_query_actor_back_to_orchestrator():
    graph_source = Path("core/application/orchestration/graph/builder.py").read_text()

    assert 'graph.add_edge("dataset_query_step", "orchestrator")' in graph_source
    assert 'graph.add_edge("tabular_feed_step", "orchestrator")' in graph_source


def test_canonical_schema_allows_dataset_query_step_kind():
    schema_source = read_canonical_schema_sql()

    assert "registry_workflow_steps_step_kind_check" in schema_source
    assert "'dataset_query'::text" in schema_source
    assert "'transform'::text" in schema_source
    assert "'tabular_feed'::text" in schema_source
    assert "'file_from_base64'::text" in schema_source
    assert "'template'::text" in schema_source
    assert "'approval'::text" in schema_source


def test_execute_step_connector_branch_emits_connector_actor_message():
    from core.application.orchestration.graph.nodes import execute_step_node

    state = step_run_state({"step_kind": "connector", "connector_id": "connector.semrush"})

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            side_effect=AssertionError("agent registry must not be used for connector steps"),
        ),
        patch(
            "core.application.orchestration.connector_step_runtime.execute_connector_step",
            return_value={
                "ok": True,
                "runtime": "tool_manifest",
                "tool_id": "semrush",
                "result": {"ok": True},
                "detail": "",
            },
        ),
    ):
        update = execute_step_node(state)

    msg = (update.get("agent_messages") or [])[-1]
    assert msg["from_agent"] == "connector_step"
    assert msg["status"] == "pass"
    assert msg["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert msg["artifacts"]["connector_id"] == "connector.semrush"
    assert msg["artifacts"]["_connector_step"]["execution_mode"] == "tool_manifest"


def test_transform_data_is_not_a_supported_step_kind():
    from core.application.workflows.workflow_step_kinds import (
        SUPPORTED_STEP_KINDS,
        normalize_step_kind,
    )

    assert "transform_data" not in SUPPORTED_STEP_KINDS
    assert normalize_step_kind("transform_data", default="") == ""


def test_tabular_feed_is_a_supported_real_execution_step():
    from core.application.registry.contract_validation_common import (
        REAL_EXECUTION_STEP_KINDS,
    )
    from core.application.workflows.workflow_step_kinds import (
        SUPPORTED_STEP_KINDS,
        normalize_step_kind,
        runtime_actor_for_step,
    )

    assert "tabular_feed" in SUPPORTED_STEP_KINDS
    assert "tabular_feed" in REAL_EXECUTION_STEP_KINDS
    assert normalize_step_kind("tabular_feed", default="") == "tabular_feed"
    assert runtime_actor_for_step({"step_kind": "tabular_feed"}) == "tabular_feed_step"


def test_execute_step_dataset_query_branch_emits_dataset_actor_message():
    from core.application.orchestration.graph.nodes import execute_step_node

    state = step_run_state(
        {
            "step_kind": "dataset_query",
            "override": {"dataset_query": {"query_template_id": "query-1"}},
        }
    )

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            side_effect=AssertionError("agent registry must not be used for dataset_query steps"),
        ),
        patch(
            "core.application.orchestration.dataset_query_step_runtime.execute_dataset_query_step",
            return_value={
                "ok": True,
                "runtime": "dataset_query",
                "detail": "dataset_query step executed",
                "result": {"row_count": 2},
                "state_patch": {"users_result": {"row_count": 2}},
            },
        ),
    ):
        update = execute_step_node(state)

    msg = (update.get("agent_messages") or [])[-1]
    assert msg["from_agent"] == "dataset_query_step"
    assert msg["status"] == "pass"
    assert msg["artifacts"]["dataset_query"]["result"]["row_count"] == 2
    assert update["users_result"]["row_count"] == 2


def test_execute_step_tabular_feed_branch_emits_tabular_actor_message():
    from core.application.orchestration.graph.nodes import execute_step_node

    state = step_run_state(
        {
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "read",
                    "input": {"artifact_handle": "state.feed_artifact"},
                    "result_target": "state.feed_dataset",
                }
            },
        },
        feed_artifact="artifact:feed-1",
    )

    rows = [{"sku": f"SKU-{index:04d}", "qty": index} for index in range(1000)]
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            side_effect=AssertionError("agent registry must not be used for tabular_feed steps"),
        ),
        patch(
            "core.application.orchestration.tabular_feed_step_runtime.execute_tabular_feed_step",
            return_value={
                "ok": True,
                "runtime": "tabular_feed",
                "detail": "tabular_feed step executed",
                "operation": "read",
                "action_id": "read_rows",
                "result": {"dataset_handle": "dataset:feed-1", "rows": rows},
                "errors": [],
                "invocation": {
                    "status": "succeeded",
                    "runtime": "tabular_feed",
                    "response": {
                        "ok": True,
                        "result": {"dataset_handle": "dataset:feed-1", "rows": rows},
                    },
                },
                "state_patch": {
                    "feed_dataset": {"dataset_handle": "dataset:feed-1", "rows": rows},
                    "workflow_data": {
                        "feed_dataset": {"dataset_handle": "dataset:feed-1", "rows": rows}
                    },
                },
            },
        ),
    ):
        update = execute_step_node(state)

    msg = (update.get("agent_messages") or [])[-1]
    assert msg["from_agent"] == "tabular_feed_step"
    assert msg["status"] == "pass"
    assert len(json.dumps(msg, separators=(",", ":"))) <= 4096
    assert msg["artifacts"]["tabular_feed"]["result_target"] == "state.feed_dataset"
    assert "result" not in msg["artifacts"]["tabular_feed"]
    assert "state_patch" not in msg["artifacts"]["tabular_feed"]
    assert "invocation" not in msg["artifacts"]["tabular_feed"]
    assert "_state_patch" not in msg["artifacts"]
    assert update["feed_dataset"]["dataset_handle"] == "dataset:feed-1"
    assert update["feed_dataset"]["rows"] == rows


def test_execute_step_tabular_feed_failure_keeps_diagnostics_without_state_copy():
    from core.application.orchestration.graph.nodes import execute_step_node

    state = step_run_state(
        {
            "step_id": "read-feed",
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "read_rows",
                    "input": {"dataset_handle": "state.missing_handle"},
                    "result_target": "state.feed_dataset",
                }
            },
        }
    )

    with patch(
        "core.application.orchestration.tabular_feed_step_runtime.execute_tabular_feed_step",
        return_value={
            "ok": False,
            "runtime": "tabular_feed",
            "detail": "tabular_feed state reference is missing",
            "operation": "read_rows",
            "action_id": "read_rows",
            "result": {},
            "errors": [{"reason": "state_reference_missing", "selector": "state.missing_handle"}],
            "state_patch": {},
        },
    ):
        update = execute_step_node(state)

    message = update["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "tabular_feed_failed"
    assert message["artifacts"]["tabular_feed"]["errors"] == [
        {"reason": "state_reference_missing", "selector": "state.missing_handle"}
    ]
    assert message["artifacts"]["tabular_feed"]["result_target"] == "state.feed_dataset"
    assert "_state_patch" not in message["artifacts"]
