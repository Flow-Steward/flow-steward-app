"""A step Test Run must not report a failure with nothing in it.

Regression for the workflow E2E ledger findings F2 and F12: a step Test Run
surfaced a failure as `Status: failed`, `Action: —`, `Result: {}`, `Raw inputs {}`,
`Raw result {}` — and `jobs.last_error` was empty in the database too, not just in
the UI. The same step run through the workflow-level Test Run reported the real
message.

Two causes, both here. `_step_result_payload` read its `error` from the step
message's `errors` list, which every non-agent step kind left empty (fixed
separately, in the step failure envelope). And when a step produced no message at
all — rejected before it ran — the payload was assembled from `None` and every
field came out blank, so the operator saw a failure with no cause anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.workflows.platform_test_run_runtime import _step_result_payload

STEP = {"step_id": "read_feed", "step_kind": "tabular_feed"}
IDENTITY = {
    "step_id": "read_feed",
    "step_execution_id": "8a4f3f8e-0f7c-4a59-9a4e-3f1b2c9d7e10",
    "attempt": 1,
}


def _payload(state: dict, update: dict) -> dict:
    return _step_result_payload(
        state=state,
        update=update,
        workflow_id="wf-1",
        step=dict(STEP),
        input_payload={"path": "."},
    )


# ---------------------------------------------------------------------------
# A step that did run reports what it produced
# ---------------------------------------------------------------------------


def test_a_failing_step_reports_its_error():
    message = {
        **IDENTITY,
        "status": "fail",
        "summary": "tabular_feed step failed",
        "errors": ["too_many_rows: 134933 rows above the 100000 maximum"],
        "artifacts": {"step_kind": "tabular_feed", "error": "tabular_feed_failed"},
    }

    payload = _payload({"status": "in_progress"}, {"agent_messages": [message]})

    assert payload["status"] == "fail"
    assert payload["error"] == "too_many_rows: 134933 rows above the 100000 maximum"
    assert payload["summary"] == "tabular_feed step failed"


def test_a_passing_step_is_unchanged():
    message = {
        **IDENTITY,
        "status": "pass",
        "summary": "tabular_feed step executed",
        "artifacts": {"rows": 10},
    }

    payload = _payload({"status": "in_progress"}, {"agent_messages": [message]})

    assert payload["status"] == "pass"
    assert payload["error"] == ""
    assert payload["result"] == {"rows": 10}


# ---------------------------------------------------------------------------
# A step that never ran must say so
# ---------------------------------------------------------------------------


def test_a_step_that_produced_no_message_is_not_a_blank_failure():
    """This is the `Status: failed / Result: {} / no error text` case."""
    state = {"status": "failed", "error": "Invalid Quality policy configuration"}

    payload = _payload(state, {})

    assert payload["status"] == "failed"
    assert payload["error"] == "Invalid Quality policy configuration"
    assert payload["summary"], "a failure with an empty summary is what the operator saw"
    assert "Invalid Quality policy configuration" in payload["summary"]


def test_a_step_that_produced_no_message_and_no_error_still_says_something():
    payload = _payload({"status": "failed"}, {})

    assert payload["status"] == "failed"
    assert payload["summary"], "the drawer must never render an empty failure"
    assert "no result" in payload["summary"].lower()


def test_a_step_still_in_progress_is_not_relabelled():
    payload = _payload({"status": "in_progress"}, {})

    assert payload["status"] == "in_progress"
    assert payload["summary"] == ""
