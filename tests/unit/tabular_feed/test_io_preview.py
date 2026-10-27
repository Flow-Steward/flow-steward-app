from types import SimpleNamespace

import pytest

from core.application.registry.io_resolver import IOResolver
from tests._workflow_definitions import linear_workflow


@pytest.mark.parametrize("baseline", ["state.inputs.baseline", ""])
def test_diff_summary_exposes_both_datasets_and_excludes_old_read_artifact(baseline):
    workflow = linear_workflow(
        {
            "step_id": "diff-stock",
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "diff",
                    "input": {
                        "dataset_handle": "state.inputs.current",
                        "baseline_dataset_handle": baseline,
                        "artifact_handle": "state.old_read_artifact",
                    },
                    "result_target": "state.diff",
                }
            },
        },
        workflow_id="stock-import",
        settings={
            "callable": {
                "expected_inputs": [
                    {"name": "current", "type": "text"},
                    {"name": "baseline", "type": "text"},
                ]
            }
        },
    )
    resolver = IOResolver(SimpleNamespace(get_workflow=lambda _workflow_id: workflow))
    preview = resolver.resolve_step_contract_preview(
        workflow_id="stock-import", step_id="diff-stock"
    )
    rows = {
        row["name"]: row
        for group in ("locked_required", "unresolved_required", "optional")
        for row in preview[group]
    }
    assert set(rows) == {"input.dataset_handle", "input.baseline_dataset_handle"}
    assert rows["input.dataset_handle"]["source"] == "state.inputs.current"
    assert rows["input.baseline_dataset_handle"]["source"] == baseline
    assert rows["input.baseline_dataset_handle"]["required"] is True
    if not baseline:
        assert rows["input.baseline_dataset_handle"]["resolved"] is False
