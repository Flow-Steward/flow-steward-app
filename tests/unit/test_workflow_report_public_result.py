"""The public workflow result names its report from the persisted run (ADR 0059 decision 5)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from core.application.jobs.workflow_run_models import PublicWorkflowResultV1
from core.application.jobs.workflow_run_projection import build_public_result
from core.application.security.credential_redaction import redact

SNAPSHOT = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "workflow_reports"
        / "sales_report_snapshot.json"
    ).read_text(encoding="utf-8")
)


def _job(interface: dict[str, Any] | None, *, settings: dict | None = None) -> dict[str, Any]:
    workflow_outputs: dict[str, Any] = {
        "ok": True,
        "outputs": {"sales_report": copy.deepcopy(SNAPSHOT), "raw_totals": {"eur": 1}},
        "artifacts": {},
    }
    if interface is not None:
        workflow_outputs["interface"] = interface
    return {"id": 9, "meta": {"workflow_outputs": workflow_outputs}, "settings": settings or {}}


def _public(job: dict[str, Any], status: str = "completed") -> PublicWorkflowResultV1 | None:
    return build_public_result(job, status)


def test_a_completed_run_names_its_report_and_returns_it_whole() -> None:
    result = _public(_job({"enabled": True, "report_output": "sales_report"}))
    assert result is not None
    assert result.report_output == "sales_report"
    assert result.outputs["sales_report"] == SNAPSHOT
    assert result.outputs["raw_totals"] == {"eur": 1}


def test_the_name_comes_from_the_run_not_from_live_settings() -> None:
    edited = {"callable": {"report_output": "raw_totals"}}
    result = _public(_job({"enabled": True, "report_output": "sales_report"}, settings=edited))
    assert result is not None
    assert result.report_output == "sales_report"


@pytest.mark.parametrize(
    "interface",
    [None, {"enabled": True}, {"enabled": True, "report_output": "missing"}],
    ids=["no_interface", "no_selection", "selection_without_output"],
)
def test_no_report_is_named_without_a_persisted_selection(interface: dict | None) -> None:
    result = _public(_job(interface))
    assert result is not None
    assert result.report_output is None


@pytest.mark.parametrize("status", ["failed", "running", "cancelled"])
def test_only_completed_runs_publish_a_result(status: str) -> None:
    assert _public(_job({"enabled": True, "report_output": "sales_report"}), status) is None


def test_the_api_redaction_keeps_the_report_and_its_name() -> None:
    result = _public(_job({"enabled": True, "report_output": "sales_report"}))
    assert result is not None
    public = PublicWorkflowResultV1.model_validate(redact(result.model_dump(mode="json")))
    assert public.report_output == "sales_report"
    assert public.outputs["sales_report"] == SNAPSHOT
