"""Collection steps expose their actual test-run input and output contracts."""

from types import SimpleNamespace

import pytest

from core.application.registry.io_resolver import IOResolver


@pytest.mark.parametrize(
    "kind,config,input_name,source,output_name,output_type",
    [
        (
            "collect",
            {"source": "state.current_item", "target": "state.collected", "mode": "append"},
            "source",
            "state.current_item",
            "collected",
            "array",
        ),
        (
            "batch",
            {"source_path": "state.rows", "result_target": "state.batches", "batch_size": 1},
            "source_path",
            "state.rows",
            "batches",
            "array",
        ),
    ],
)
def test_collection_step_contract_preview(
    monkeypatch, kind, config, input_name, source, output_name, output_type
):
    step = {"step_id": "prepare", "step_kind": kind, "override": {kind: config}}
    io = IOResolver(SimpleNamespace())
    monkeypatch.setattr(io, "_get_workflow_step", lambda *_args: ({"workflow_id": "wf"}, step))
    monkeypatch.setattr(io, "_build_available_type_map", lambda *_args: {})

    contract = io.resolve_step_contract_preview(workflow_id="wf", step_id="prepare")

    inputs = [
        row
        for group in ("locked_required", "unresolved_required", "optional", "custom")
        for row in contract[group]
    ]
    assert [(row["name"], row["source"], row["required"]) for row in inputs] == [
        (input_name, source, True)
    ]
    assert [(row["name"], row["type"]) for row in contract["outputs"]] == [
        (output_name, output_type)
    ]
