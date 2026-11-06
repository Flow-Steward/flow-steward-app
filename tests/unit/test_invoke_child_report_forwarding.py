"""A child workflow's report reaches the parent only from its validated final contract."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from core.application.orchestration.invoke_workflow_step_node_results import (
    _collect_child_outputs,
)
from core.application.registry.workflow_contract import WorkflowInterface
from core.application.registry.workflow_contract_service import collect_runtime_outputs
from core.application.workflow_documents import contains_report, split_report_previews
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


def _run_invoke(monkeypatch, *, child_outputs: dict[str, Any], output_mapping: dict[str, str]):
    from core.application.orchestration import invoke_workflow_step_node as node
    from tests.unit.test_invoke_workflow_step_node import (
        _base_state,
        _capture_append_messages,
        _FakeLogger,
        _install_runtime_stubs,
    )

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
                            {"name": name, "type": "json", "required": True}
                            for name in child_outputs
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
        "outputs": child_outputs,
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
                "output_mapping": output_mapping,
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
    state = _base_state(step)
    before = copy.deepcopy({key: state.get(key) for key in ("outputs", "workflow_data")})
    result = node.invoke_workflow_step_node(state)
    after = {key: state.get(key) for key in ("outputs", "workflow_data")}
    assert after == before, "the parent's canonical state must not be modified in place"
    message = captured[-1]
    assert message["status"] == "pass", (message.get("summary"), message.get("errors"))
    return result, message


@pytest.mark.parametrize("target", ["state.outputs.child_report", "child_report"])
def test_the_parent_message_carries_a_preview_and_state_keeps_the_report(monkeypatch, target):
    report = _marked_report()
    result, message = _run_invoke(
        monkeypatch,
        child_outputs={"sales_report": report},
        output_mapping={"sales_report": target},
    )
    assert MARKER not in json.dumps(message, default=str)
    if target.startswith("state."):
        assert result["outputs"]["child_report"] == report
    artifacts = message["artifacts"]
    assert not contains_report(
        {key: value for key, value in artifacts.items() if key != "report_previews"}
    )
    previews = artifacts["report_previews"]
    assert previews
    for preview in previews.values():
        assert preview["kind"] == "fs.report"
        assert preview["datasets"][0]["row_count"] == 200


def test_a_mixed_list_is_left_out_whole_and_never_published_shortened(monkeypatch):
    report = _marked_report()
    bundle = [report, "kept"]
    result, message = _run_invoke(
        monkeypatch,
        child_outputs={"sales_report": report, "bundle": bundle, "note": "plain"},
        output_mapping={
            "sales_report": "state.outputs.child_report",
            "bundle": "state.outputs.bundle",
            "note": "state.outputs.note",
        },
    )
    # Canonical state receives every value exactly.
    assert result["outputs"]["child_report"] == report
    assert result["outputs"]["bundle"] == [report, "kept"]
    assert result["outputs"]["note"] == "plain"

    artifacts = message["artifacts"]
    assert MARKER not in json.dumps(message, default=str)
    patch = artifacts["_state_patch"]
    assert not contains_report(patch)
    assert patch["outputs"] == {"note": "plain"}
    assert "bundle" not in artifacts
    preview_bundle = artifacts["report_previews"]["bundle"]
    assert len(preview_bundle) == 2
    assert preview_bundle[1] == "kept"

    # Without canonical state the message fallback publishes only exact values:
    # no shortened array and no preview standing in for the report.
    interface = WorkflowInterface.from_settings(
        {
            "callable": {
                "enabled": True,
                "expected_outputs": [
                    {"name": "bundle", "type": "array", "required": False},
                    {"name": "note", "type": "string", "required": False},
                    {"name": "child_report", "type": "object", "required": False},
                ],
            }
        },
        steps=[],
    )
    outputs, _artifacts = collect_runtime_outputs(
        {"outputs": {}, "agent_messages": [message]}, interface=interface
    )
    assert outputs == {"note": "plain"}


def test_contains_report_and_split_previews() -> None:
    report = {"kind": "fs.report", "schema_version": 1, "title": "T", "datasets": []}
    assert contains_report({"a": [1, {"b": report}]})
    assert not contains_report({"a": [1, {"b": {"kind": "other"}}]})
    split = split_report_previews({"keep": [1, 2], "mixed": [report, "x"], "plain": "p"})
    assert split["keep"] == [1, 2]
    assert split["plain"] == "p"
    assert "mixed" not in split
    assert split["report_previews"]["mixed"][1] == "x"
    assert split["report_previews"]["mixed"][0]["kind"] == "fs.report"
