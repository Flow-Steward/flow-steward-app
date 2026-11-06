"""A child workflow's report reaches the parent only from its validated final contract."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.orchestration.invoke_workflow_step_node_results import (
    _collect_child_outputs,
)
from tests.unit.test_invoke_workflow_step_node import (  # noqa: F401  (autouse fixture)
    _application_submission_repositories,
)

REPORT = {"kind": "fs.report", "schema_version": 1, "title": "Child report"}


class _Logger:
    def __init__(self, contract: dict[str, Any]) -> None:
        self._contract = contract

    def get_job(self, _job_id: str) -> dict[str, Any]:
        return {"meta": {"workflow_outputs": self._contract}}

    def get_agent_messages(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "envelope": {
                    "artifacts": {
                        "sales_report": {"forged": True},
                        "note": "from a message",
                    }
                }
            }
        ]


def _contract(*, ok: bool) -> dict[str, Any]:
    return {
        "ok": ok,
        "outputs": {"sales_report": REPORT, "note": "final"},
        "interface": {"enabled": True, "report_output": "sales_report"},
    }


def test_a_validated_child_forwards_its_report() -> None:
    resolved = _collect_child_outputs(_Logger(_contract(ok=True)), "7", ["sales_report", "note"])
    assert resolved == {"sales_report": REPORT, "note": "final"}


def test_fallbacks_never_supply_the_child_report() -> None:
    resolved = _collect_child_outputs(_Logger(_contract(ok=False)), "7", ["sales_report", "note"])
    assert "sales_report" not in resolved
    assert resolved["note"] == "from a message"


MARKER = "ROW-MARKER-c51d"


def _marked_report() -> dict[str, Any]:
    import json
    from pathlib import Path

    from core.application.workflow_documents import compile_report

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "workflow_reports"
    definition = json.loads((fixtures / "sales_report_definition.json").read_text("utf-8"))
    inputs = json.loads((fixtures / "sales_report_inputs.json").read_text("utf-8"))
    values = inputs["values"]
    values["regions"] = [
        {"region": f"{MARKER}-{i}", "orders": i, "revenue": 1.5, "margin": 0.1, "manager": "m"}
        for i in range(200)
    ]
    return compile_report(definition, inputs["declared_inputs"], values)


@pytest.mark.parametrize("target", ["state.outputs.child_report", "child_report"])
def test_the_parent_message_carries_a_preview_and_state_keeps_the_report(monkeypatch, target):
    import json

    from core.application.orchestration import invoke_workflow_step_node as node
    from tests.unit.test_invoke_workflow_step_node import (
        _base_state,
        _capture_append_messages,
        _FakeLogger,
        _install_runtime_stubs,
    )

    report = _marked_report()
    _install_runtime_stubs(
        monkeypatch,
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_outputs": [
                            {"name": "sales_report", "type": "object", "required": True}
                        ],
                        "report_output": "sales_report",
                    }
                },
            }
        },
    )
    captured = _capture_append_messages(monkeypatch, node)
    final_contract = {
        "ok": True,
        "outputs": {"sales_report": report},
        "interface": {"enabled": True, "report_output": "sales_report"},
    }

    class _FinishedChildLogger(_FakeLogger):
        def get_job(self, job_id: int):
            row = super().get_job(job_id)
            if int(job_id) == self.child_job_id:
                row["meta"] = {**row["meta"], "workflow_outputs": final_contract}
            return row

    fake_logger = _FinishedChildLogger(child_job_id=981, child_messages=[])
    step = {
        "agent": "reporter",
        "skill": "report",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"task_summary": "state.task_summary"},
                "output_mapping": {"sales_report": target},
            }
        },
    }
    monkeypatch.setattr(
        node, "_resolve_invoke_input", lambda state, selector, key: (state.get("task_summary"), "")
    )
    monkeypatch.setattr(node, "_get_logger", lambda: fake_logger)
    monkeypatch.setattr(
        node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )

    result = node.invoke_workflow_step_node(_base_state(step))

    message = captured[-1]
    assert message["status"] == "pass", (message.get("summary"), message.get("errors"))
    assert MARKER not in json.dumps(message, default=str)
    if target.startswith("state."):
        assert result["outputs"]["child_report"] == report
    previews = [
        value
        for value in message["artifacts"].values()
        if isinstance(value, dict) and value.get("kind") == "fs.report"
    ]
    assert previews
    assert all(preview["datasets"][0]["row_count"] == 200 for preview in previews)
