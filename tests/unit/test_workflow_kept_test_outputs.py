"""Kept test output: storage rules, the run snapshot and the step hub's stand-in."""

from __future__ import annotations

import pytest

import core.application.orchestration.agents.common as agents_common
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.orchestration.state_path_guards import strip_reserved_runtime_state
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.workflows.workflow_definition_snapshot import definition_digest
from core.application.workflows.workflow_kept_test_outputs import (
    KeptTestOutputError,
    kept_outputs_for_run,
    kept_test_outputs_for_workflow,
    normalize_kept_outputs,
    step_kind_keeps_output,
)
from tests._workflow_definitions import linear_graph, node


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    yield
    clear_run_definition_cache()


DEFINITION = {
    "steps": [
        {"step_id": "a", "step_kind": "transform"},
        {"step_id": "b", "step_kind": "http_request"},
        {"step_id": "done", "step_kind": "stop_success"},
    ],
    "groups": [],
    "for_each": [],
    "execution_graph": {
        "schema_version": 1,
        "entry": node("a"),
        "edges": linear_graph(["a", "b", "done"])["edges"],
    },
    "settings": {},
}


def _run(**state) -> dict:
    ref = {"snapshot_id": "snap", "revision": 1, "digest": definition_digest(DEFINITION)}
    prime_run_definition(ref, DEFINITION)
    return {
        "workflow_id": "wf",
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "agent_messages": [],
        **state,
    }


def _apply(state: dict, command) -> dict:
    merged = dict(state)
    for key, value in (command.update or {}).items():
        if key == "orchestrator_journal":
            merged[key] = [*(merged.get(key) or []), *value]
        else:
            merged[key] = value
    return merged


KEPT = {"a": {"state.shipment": {"tracking_number": "JD014600003SE", "carrier": "DHL"}}}


def test_a_workflow_test_run_returns_the_kept_output_instead_of_running_the_step() -> None:
    state = _run(task_source="workflow_test_run", workflow_test_run_kept_outputs=KEPT)
    first = orchestrator_hub_node(state)
    # Nothing is dispatched to an executor: the hub records the result itself.
    assert first.goto == "orchestrator"
    state = _apply(state, first)
    message = state["agent_messages"][-1]
    assert message["skill"] == "kept_test_output"
    assert message["status"] == "pass"
    assert message["step_id"] == "a"
    assert message["artifacts"]["output_payload"] == KEPT["a"]
    assert state["workflow_data"]["shipment"]["tracking_number"] == "JD014600003SE"

    # The hub accepts the kept result and goes on along the step's success edge.
    second = orchestrator_hub_node(state)
    state = _apply(state, second)
    assert (second.goto, state["current_step_id"]) == ("execute_step", "b")


def test_a_real_run_never_reads_kept_outputs() -> None:
    state = _run(task_source="api", workflow_test_run_kept_outputs=KEPT)
    assert orchestrator_hub_node(state).goto == "execute_step"


def test_a_step_test_run_never_reads_kept_outputs() -> None:
    state = _run(task_source="step_test_run", workflow_test_run_kept_outputs=KEPT)
    command = orchestrator_hub_node(state)
    assert command.goto != "orchestrator" or not any(
        message.get("skill") == "kept_test_output"
        for message in (command.update or {}).get("agent_messages") or []
    )


def test_an_external_state_patch_cannot_supply_kept_outputs() -> None:
    assert "workflow_test_run_kept_outputs" not in strip_reserved_runtime_state(
        {"workflow_test_run_kept_outputs": KEPT, "inputs": {}}
    )


@pytest.mark.parametrize("kind", ["if", "switch", "approval", "wait", "set_variable", "batch"])
def test_decisions_and_control_flow_always_run(kind: str) -> None:
    assert not step_kind_keeps_output(kind)


@pytest.mark.parametrize("kind", ["agent", "connector", "http_request", "transform"])
def test_steps_with_outputs_can_keep_one(kind: str) -> None:
    assert step_kind_keeps_output(kind)


def test_kept_values_are_a_json_object_of_outputs() -> None:
    assert normalize_kept_outputs({" state.order ": {"id": "A-1001"}}) == {
        "state.order": {"id": "A-1001"}
    }
    for invalid in ([], {}, "x", {"": 1}, {"state.x": float("nan")}):
        with pytest.raises(KeptTestOutputError):
            normalize_kept_outputs(invalid)
    with pytest.raises(KeptTestOutputError):
        normalize_kept_outputs({"state.big": "x" * 300_000})


def test_the_run_snapshot_keeps_only_steps_that_exist_and_may_stand_in() -> None:
    rows = [
        {"step_id": "a", "outputs": {"state.x": 1}},
        {"step_id": "gone", "outputs": {"state.y": 2}},
        {"step_id": "branch", "outputs": {"state.z": 3}},
        {"step_id": "empty", "outputs": {}},
    ]
    kinds = {"a": "agent", "branch": "if", "empty": "agent"}
    assert kept_outputs_for_run(rows, kinds) == {"a": {"state.x": 1}}


def test_a_workflow_without_steps_or_storage_starts_without_kept_outputs() -> None:
    assert kept_test_outputs_for_workflow(object(), {"workflow_id": "wf", "steps": []}) == {}
    assert (
        kept_test_outputs_for_workflow(
            object(), {"workflow_id": "wf", "steps": [{"step_id": "a", "step_kind": "agent"}]}
        )
        == {}
    )


def test_a_workflow_test_run_restores_kept_outputs_only_from_trusted_job_meta() -> None:
    from core.application.jobs.task_execution_request import _restore_job_meta

    def restore(task_source: str, meta: dict) -> dict:
        extra: dict = {}
        _restore_job_meta(
            {"task_source": task_source, "meta": meta, "selected_workflow": "wf"},
            extra,
            {},
            True,
            "wf",
            None,
        )
        return extra

    trusted = {"workflow_test_run": True, "kept_test_outputs": KEPT}
    assert restore("workflow_test_run", trusted)["workflow_test_run_kept_outputs"] == KEPT
    assert "workflow_test_run_kept_outputs" not in restore("api", trusted)
    assert "workflow_test_run_kept_outputs" not in restore(
        "workflow_test_run", {"kept_test_outputs": KEPT}
    )
