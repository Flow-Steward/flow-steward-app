"""Test-run redaction and kept outputs leave a report intact or refuse it whole.

ADR 0059 recommendations 3 and 6: a snapshot survives test-run redaction
unchanged, and keeping a report larger than the kept-output cap is refused with
an explicit error, never cut.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.application.workflows.platform_test_run_common import redact_test_run_payload
from core.application.workflows.workflow_kept_test_outputs import (
    KEPT_OUTPUT_MAX_BYTES,
    KeptTestOutputError,
    normalize_kept_outputs,
)

SNAPSHOT = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "workflow_reports"
        / "sales_report_snapshot.json"
    ).read_text(encoding="utf-8")
)


def _with_session_columns() -> dict:
    report = copy.deepcopy(SNAPSHOT)
    report["datasets"][0]["fields"] = [*report["datasets"][0]["fields"], "sessions"]
    for row in report["datasets"][0]["rows"]:
        row.append(42)
    return report


def test_a_snapshot_survives_test_run_redaction_unchanged() -> None:
    payload = {"template": {"ok": True, "output": _with_session_columns()}}
    assert redact_test_run_payload(payload) == payload


def test_a_small_report_is_kept_as_is() -> None:
    kept = normalize_kept_outputs({"state.outputs.sales_report": SNAPSHOT})
    assert kept["state.outputs.sales_report"] == SNAPSHOT


def test_a_report_over_the_kept_cap_is_refused_not_cut() -> None:
    report = copy.deepcopy(SNAPSHOT)
    report["datasets"][0]["rows"] = [["x" * 300, 1, 0.1] for _ in range(1000)]
    assert len(json.dumps(report)) > KEPT_OUTPUT_MAX_BYTES
    with pytest.raises(KeptTestOutputError, match="KB of JSON"):
        normalize_kept_outputs({"state.outputs.sales_report": report})
