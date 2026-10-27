from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


@pytest.mark.parametrize(
    "handle_reference",
    [
        "state.created_dataset.dataset_handle",
        "{{ state.created_dataset.dataset_handle }}",
    ],
)
def test_compiled_graph_resolves_tabular_handle_after_checkpoint(
    monkeypatch, handle_reference: str
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.state import OrchestratorState
    from tests._step_run_state import step_run_state

    plane = ArtifactDatasetPlane()
    monkeypatch.setattr(
        "core.application.orchestration.tabular_feed_step_runtime.plane_from_state_or_env",
        lambda state: plane,
    )

    trusted_scope = {
        "account_id": "acct_runtime",
        "project_id": "proj_runtime",
        "workflow_id": "wf_runtime",
        "job_id": "job_runtime",
    }
    create_step = {
        "step_id": "create-dataset",
        "step_kind": "tabular_feed",
        "override": {
            "tabular_feed": {
                "operation": "create_dataset",
                "rows": [{"sku": "00123", "qty": "2"}],
                "result_target": "state.created_dataset",
            }
        },
    }
    read_step = {
        "step_id": "read-rows",
        "step_kind": "tabular_feed",
        "override": {
            "tabular_feed": {
                "operation": "read_rows",
                "input": {"dataset_handle": handle_reference},
                "columns": ["sku", "qty"],
                "result_target": "state.page",
            }
        },
    }

    # Each node runs with the run cursor on its own step of one pinned
    # definition; only what the step wrote crosses the checkpoint.
    def create_node(state: dict) -> dict:
        return execute_step_node({**state, **step_run_state(create_step, read_step)})

    def read_node(state: dict) -> dict:
        return execute_step_node({**state, **step_run_state(read_step, create_step)})

    builder = StateGraph(OrchestratorState)
    builder.add_node("create", create_node)
    builder.add_node("read", read_node)
    builder.add_edge(START, "create")
    builder.add_edge("create", "read")
    builder.add_edge("read", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    result = graph.invoke(
        {
            "trusted_runtime_context": dict(trusted_scope),
            "agent_messages": [],
            "project_config": dict(trusted_scope),
            "workflow_data": {},
        },
        {"configurable": {"thread_id": f"tabular-checkpoint-{handle_reference}"}},
    )

    assert "created_dataset" not in result
    assert result["workflow_data"]["page"]["rows"] == [{"sku": "00123", "qty": "2"}]
    assert result["agent_messages"][-1]["status"] == "pass"
