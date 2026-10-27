"""Mapped Wait inputs use the ordinary isolated Step Test Run bindings."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.application.orchestration.wait_step_runtime import resolve_wait_duration
from core.application.orchestration.workflow_state_selector_contract import (
    discover_step_state_selectors,
)
from core.application.registry.io_resolver import IOResolver
from core.application.workflows.test_run_input_bindings import apply_test_run_input_bindings


def _step(config):
    return {"step_id": "pause", "step_kind": "wait", "override": {"wait": config}}


def _preview(monkeypatch, config):
    step = _step(config)
    io = IOResolver(SimpleNamespace())
    monkeypatch.setattr(io, "_get_workflow_step", lambda *_args: ({"workflow_id": "wf"}, step))
    monkeypatch.setattr(
        io, "_build_available_type_map", lambda *_args: {"state.inputs.duration": "number"}
    )
    contract = io.resolve_step_contract_preview(workflow_id="wf", step_id="pause")
    return step, [
        row
        for group in ("locked_required", "unresolved_required", "optional", "custom")
        for row in contract[group]
    ]


@pytest.mark.parametrize("source", ["state.inputs.duration", {"source": "state.inputs.duration"}])
def test_mapped_wait_contract_materializes_a_numeric_step_test_value(monkeypatch, source):
    step, rows = _preview(monkeypatch, {"input_mapping": {"duration_ms": source}})
    saved = deepcopy(step)
    assert len(rows) == 1
    row = rows[0]
    assert {key: row[key] for key in ("name", "type", "required", "source")} == {
        "name": "duration_ms",
        "type": "number",
        "required": True,
        "source": "state.inputs.duration",
    }
    state = apply_test_run_input_bindings(
        {},
        {
            "metadata": {
                "input_bindings": rows,
                "input_binding_values": {"duration_ms": 0},
            }
        },
    )
    assert state["inputs"]["duration"] == 0
    assert resolve_wait_duration(step, state) == (0, "")
    assert step == saved


@pytest.mark.parametrize("source", ["state.inputs.duration", {"source": "state.inputs.duration"}])
def test_wait_dependency_discovery_uses_the_runtime_source_dialect(source):
    references = discover_step_state_selectors(
        _step({"input_mapping": {"duration_ms": source}}), step_path="workflow.steps.pause"
    )
    suffix = ".source" if isinstance(source, dict) else ""
    assert [(reference.selector, reference.path) for reference in references] == [
        (
            "state.inputs.duration",
            f"workflow.steps.pause.override.wait.input_mapping.duration_ms{suffix}",
        )
    ]


@pytest.mark.parametrize("config", [{"duration_ms": 0}, {"input_mapping": {"duration_ms": 0}}])
def test_literal_wait_durations_do_not_become_editable_step_test_inputs(monkeypatch, config):
    step, rows = _preview(monkeypatch, config)
    assert rows == []
    assert discover_step_state_selectors(step) == []
    assert resolve_wait_duration(step, {}) == (0, "")
