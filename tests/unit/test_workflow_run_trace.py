from __future__ import annotations

import pytest

from core.application.orchestration.run_cursor import FrozenDefinition, RunCursor
from core.application.orchestration.run_trace import append_run_trace, edge_fact
from core.application.workflows.execution_graph import NodeRef, Outcome
from core.application.workflows.workflow_definition_snapshot import definition_digest
from tests._workflow_definitions import edge, node


def trace_state():
    definition = {
        "steps": [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "b", "step_kind": "stop_success"},
        ],
        "groups": [],
        "for_each": [],
        "settings": {},
        "execution_graph": {
            "schema_version": 1,
            "entry": node("a"),
            "edges": [edge("a", node("b"))],
        },
    }
    return {
        "job_id": "job-1",
        "workflow_id": "workflow-1",
        "workflow_definition_ref": {
            "snapshot_id": "archive-1",
            "revision": 1,
            "digest": definition_digest(definition),
        },
    }, definition


def one_trace():
    state, definition = trace_state()
    state["workflow_run_trace"] = append_run_trace(state, [])
    frozen = FrozenDefinition(definition)
    fact = edge_fact(
        state,
        frozen,
        frozen.graph.edge_for(NodeRef("step", "a"), Outcome("success")),
        RunCursor(step_id="a", step_execution_id="exec-a", attempt=2),
    )
    return append_run_trace(state, [fact])


def test_trace_is_idempotent_bounded_and_old_cursor_coverage_stays_partial(monkeypatch):
    import core.application.orchestration.run_trace as trace_module

    state, definition = trace_state()
    state["run_cursor"] = {"step_id": "a", "step_execution_id": "old-execution"}
    state["workflow_run_trace"] = append_run_trace(state, [])
    assert state["workflow_run_trace"]["covered_from_start"] is False
    monkeypatch.setattr(trace_module, "TRACE_LIMIT", 2)
    frozen = FrozenDefinition(definition)
    facts = [
        edge_fact(
            state,
            frozen,
            frozen.graph.edges[0],
            RunCursor(step_id="a", step_execution_id=f"exec-{i}"),
        )
        for i in range(3)
    ]
    result = append_run_trace(state, facts)
    assert len(result["transitions"]) == 2
    assert result["truncated"] is True
    assert result["last_sequence"] == 3
    assert append_run_trace({**state, "workflow_run_trace": result}, [facts[-1]]) == result
    assert state["workflow_run_trace"]["transitions"] == []


def test_runtime_trace_cannot_be_written_by_author_state_paths_or_external_patch():
    from core.application.orchestration.state_path_guards import (
        is_reserved_state_target,
        strip_reserved_runtime_state,
    )

    assert is_reserved_state_target("state.workflow_run_trace.transitions")
    assert strip_reserved_runtime_state(
        {"workflow_run_trace": {"forged": True}, "domain_value": 0}
    ) == {"domain_value": 0}


@pytest.mark.parametrize("field", ["snapshot_id", "revision", "digest"])
@pytest.mark.parametrize("invalid", ["missing", None, ""])
def test_trace_refuses_incomplete_definition_refs_without_masking_run_failure(field, invalid):
    state, _definition = trace_state()
    if invalid == "missing":
        state["workflow_definition_ref"].pop(field)
    else:
        state["workflow_definition_ref"][field] = invalid

    assert append_run_trace(state, []) is None


@pytest.mark.parametrize(
    "ref_patch", [{"revision": "not-a-number"}, {"revision": 0}, {"digest": ""}]
)
def test_trace_refuses_invalid_complete_definition_refs(ref_patch):
    state, _definition = trace_state()
    state["workflow_definition_ref"].update(ref_patch)

    assert append_run_trace(state, []) is None
