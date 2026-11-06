"""The run report read and the model-safe result bundle (ADR 0059 task 3, decision 11).

A model may read job state and the bounded report preview, never snapshot rows;
the viewer and the regular API get the full snapshot.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.application.jobs.workflow_run_models import PublicWorkflowResultV1
from core.application.jobs.workflow_run_report import (
    STATE_NO_REPORT,
    STATE_PENDING,
    STATE_READY,
    workflow_run_report,
)
from core.application.management.query_models import JobStatusView
from core.application.reports.job_result_bundle import build_job_result_bundle
from core.application.workflow_documents import compile_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflow_reports"
MARKER = "ROW-MARKER-9c1d"


def _large_report() -> dict[str, Any]:
    definition = json.loads((FIXTURES / "sales_report_definition.json").read_text("utf-8"))
    inputs = json.loads((FIXTURES / "sales_report_inputs.json").read_text("utf-8"))
    values = inputs["values"]
    values["regions"] = [
        {"region": f"{MARKER}-{i}", "orders": i, "revenue": 1.5, "margin": 0.1, "manager": "m"}
        for i in range(1000)
    ]
    return compile_report(definition, inputs["declared_inputs"], values)


def _status(status: str, result: PublicWorkflowResultV1 | None) -> JobStatusView:
    return JobStatusView(id="job-1", status=status, task_summary="Weekly", workflow_result=result)


def _result(
    report: dict[str, Any], *, report_output: str | None = "weekly"
) -> PublicWorkflowResultV1:
    return PublicWorkflowResultV1(
        outputs={"weekly": report, "note": "done"}, artifacts={}, report_output=report_output
    )


def test_a_finished_run_gives_the_preview_to_the_model_and_the_snapshot_to_the_viewer() -> None:
    report = _large_report()
    answer = workflow_run_report(_status("completed", _result(report)))

    assert answer["snapshot"] == report
    model = answer["model"]
    assert model["state"] == STATE_READY
    assert model["report_output"] == "weekly"
    assert model["job"] == {
        "id": "job-1",
        "status": "completed",
        "outcome": "completed",
        "terminal": True,
    }
    assert model["report"]["title"] == report["title"]
    assert model["report"]["datasets"][0]["row_count"] == 1000
    assert model["text"] == report["summary_text"]
    assert MARKER not in json.dumps(model)


def test_a_running_job_is_pending_without_a_snapshot() -> None:
    answer = workflow_run_report(_status("running", None))

    assert answer["snapshot"] is None
    assert answer["model"]["state"] == STATE_PENDING
    assert answer["model"]["report"] is None
    assert "not ready" in answer["model"]["text"]


@pytest.mark.parametrize(
    "result",
    [
        None,
        PublicWorkflowResultV1(outputs={"note": "done"}, artifacts={}, report_output=None),
        PublicWorkflowResultV1(outputs={"note": "done"}, artifacts={}, report_output="weekly"),
        PublicWorkflowResultV1(outputs={"weekly": "text"}, artifacts={}, report_output="weekly"),
    ],
    ids=["no-result", "no-selection", "selection-absent", "not-a-report"],
)
def test_a_finished_run_without_a_report_says_so(result: PublicWorkflowResultV1 | None) -> None:
    answer = workflow_run_report(_status("failed", result))

    assert answer["snapshot"] is None
    assert answer["model"]["state"] == STATE_NO_REPORT
    assert answer["model"]["report_output"] is None
    assert answer["model"]["job"]["terminal"] is True


class _Jobs:
    """A job whose report sits in the result, the raw status and a message."""

    def __init__(self, report: dict[str, Any]) -> None:
        self._report = report

    async def get_job_status(self, job_id, *, scope, include_workflow_result=False):
        assert include_workflow_result is True
        return _status("completed", _result(self._report))

    def list_messages(self, job_id, *, since_id, before_id, newest, limit, scope):
        message = {"summary": "done", "artifacts": {"report": self._report}}
        return SimpleNamespace(dump=lambda: {"messages": [message]})

    def list_logs(self, job_id, *, since_id, before_id, newest, limit, scope):
        return SimpleNamespace(dump=lambda: {"logs": []})

    def list_events(self, job_id, *, since_id, limit, scope):
        return SimpleNamespace(dump=lambda: {"events": [{"details": {"weekly": self._report}}]})


def _bundle(report: dict[str, Any], *, model_safe: bool) -> dict[str, Any]:
    return asyncio.run(
        build_job_result_bundle(
            job_query_service=_Jobs(report), job_id="job-1", scope=object(), model_safe=model_safe
        )
    )


def test_the_model_view_of_the_result_bundle_carries_no_report_rows() -> None:
    report = _large_report()
    bundle = _bundle(report, model_safe=True)

    assert MARKER not in json.dumps(bundle, default=str)
    outputs = bundle["workflow_result"]["outputs"]
    assert outputs["note"] == "done"
    assert outputs["weekly"]["title"] == report["title"]
    assert outputs["weekly"]["datasets"][0]["row_count"] == 1000
    assert bundle["workflow_result"]["report_output"] == "weekly"


def test_the_regular_result_bundle_keeps_the_full_report() -> None:
    report = _large_report()
    bundle = _bundle(report, model_safe=False)

    assert bundle["workflow_result"]["outputs"]["weekly"] == report
