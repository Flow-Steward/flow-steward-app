"""Terminal events and the proof of work never carry report rows (ADR 0059 decision 11).

The validation node used to copy the whole ``workflow_outputs`` into its
``phase_complete`` event details and into ``proof_of_work``, both of which reach
model-visible job diagnostics. The canonical ``workflow_outputs`` handed to
terminal persistence must stay complete.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from core.application.workflow_documents import compile_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflow_reports"
MARKER = "ROW-MARKER-4b8e"


def _large_report() -> dict[str, Any]:
    definition = json.loads((FIXTURES / "sales_report_definition.json").read_text("utf-8"))
    inputs = json.loads((FIXTURES / "sales_report_inputs.json").read_text("utf-8"))
    values = inputs["values"]
    values["regions"] = [
        {"region": f"{MARKER}-{i}", "orders": i, "revenue": 1.5, "margin": 0.1, "manager": "m"}
        for i in range(1000)
    ]
    return compile_report(definition, inputs["declared_inputs"], values)


def _state() -> dict[str, Any]:
    return {
        "task_id": "report-001",
        "task_summary": "Weekly report",
        "initial_requirements": "Weekly report",
        "status": "in_progress",
        "workflow_id": "weekly_report",
        "workflow_terminal": {
            "kind": "stop_success",
            "status": "pass",
            "step_id": "workflow_success",
            "step_execution_id": "exec-success-1",
        },
        "agent_messages": [],
        "platform_events": [],
        "thread_id": "thread-report",
    }


class _Result(SimpleNamespace):
    def dump(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "outputs": self.outputs,
            "artifacts": {},
            "interface": {"enabled": True, "report_output": "sales_report"},
        }


@contextlib.contextmanager
def _patched(result: _Result, events: list[dict[str, Any]]):
    fake_common = ModuleType("core.application.orchestration.agents.common")
    fake_common.emit_event = lambda *args, **kwargs: events.append(  # type: ignore[attr-defined]
        {"args": args, "kwargs": kwargs}
    )

    class _Service:
        @staticmethod
        def validate_outputs_for_workflow_id(*_args: Any, **_kwargs: Any) -> _Result:
            return result

    with (
        patch.dict(sys.modules, {"core.application.orchestration.agents.common": fake_common}),
        patch(
            "core.application.registry.workflow_contract_service.WorkflowInterfaceService",
            return_value=_Service(),
        ),
    ):
        yield


@pytest.mark.parametrize("ok", [True, False])
def test_events_and_proof_carry_only_the_report_preview(ok: bool) -> None:
    from core.application.orchestration.validation_node import orchestrator_validation_node

    report = _large_report()
    errors = [] if ok else [{"code": "workflow.output.required", "message": "x", "path": "y"}]
    events: list[dict[str, Any]] = []
    with _patched(_Result(ok=ok, errors=errors, outputs={"sales_report": report}), events):
        result = orchestrator_validation_node(_state())

    assert result["status"] == ("completed" if ok else "failed")
    assert events
    diagnostics = json.dumps([events, result.get("proof_of_work")], default=str)
    assert MARKER not in diagnostics
    event_outputs = events[-1]["args"][4]["workflow_outputs"]["outputs"]
    assert event_outputs["sales_report"]["datasets"][0]["row_count"] == 1000
    # The canonical outputs for terminal persistence keep the whole snapshot.
    assert result["workflow_outputs"]["outputs"]["sales_report"] == report
    assert result["outputs"]["sales_report"] == report
