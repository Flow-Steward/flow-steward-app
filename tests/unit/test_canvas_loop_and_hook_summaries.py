"""Break, Continue and hook cards say what they do in the viewer's language."""

from __future__ import annotations

import pytest

from core.application.workflows.canvas_graph_common import (
    _step_config_summary,
    _step_config_summary_metadata,
)
from tests.unit.test_workflow_hook_contract import declaration


@pytest.mark.parametrize(
    ("step", "raw", "key", "params"),
    [
        ({"step_kind": "break"}, "Leave the For each", "react.workflows.break_summary", None),
        ({"step_kind": "continue"}, "Next item", "react.workflows.continue_summary", None),
        (
            {
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"hook": declaration()}},
            },
            "Hook: Before creating an invoice",
            "react.workflows.invoke_hook_summary",
            {"hook": "Before creating an invoice"},
        ),
    ],
)
def test_the_card_summary_is_a_catalog_key(step, raw, key, params) -> None:
    metadata = _step_config_summary_metadata(step, workflow_id="invoice_flow")

    assert _step_config_summary(step, workflow_id="invoice_flow") == raw
    assert metadata.get("config_summary_key") == key
    assert metadata.get("config_summary_params") == params


def test_a_hook_card_never_names_its_owner_as_the_workflow_it_runs() -> None:
    step = {
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"hook": declaration()}},
    }

    assert "invoice_flow" not in _step_config_summary(step, workflow_id="invoice_flow")
