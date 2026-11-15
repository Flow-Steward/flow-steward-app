"""What the job page shows for an event Wait and a failed Invoke child (P14, P15)."""

from __future__ import annotations

from core.application.jobs.job_runtime_payload import _public_workflow_wait
from core.application.reports.job_detail_sections import build_execution_steps

_EVENT_WAIT = {
    "kind": "event",
    "wait_id": "w-1",
    "step_id": "wait_for_run",
    "step_execution_id": "e-1",
    "source": "acme.crawler",
    "event_types": ["run_completed", "run_failed"],
    "correlation_key": "run-42",
    "timeout_ms": 259_200_000,
    "started_at": "2026-11-15T08:36:37+00:00",
    "due_at": "2026-11-18T08:36:37+00:00",
    "due_epoch_ms": 1_763_455_000_000,
    "checkpoint": {"thread_id": "t-1"},
}


def test_the_job_projection_says_what_an_event_wait_waits_for() -> None:
    wait = _public_workflow_wait({"status": "awaiting_event"}, {"workflow_wait": _EVENT_WAIT})

    assert wait == {
        "kind": "event",
        "wait_id": "w-1",
        "step_id": "wait_for_run",
        "started_at": "2026-11-15T08:36:37+00:00",
        "due_at": "2026-11-18T08:36:37+00:00",
        "source": "acme.crawler",
        "event_types": ["run_completed", "run_failed"],
        "correlation_key": "run-42",
        "timeout_ms": 259_200_000,
    }


def _durable_row(step_id: str, *, status: str = "completed") -> dict:
    return {
        "step_id": step_id,
        "step_execution_id": "e-1",
        "occurrence": 1,
        "status": status,
        "started_at": "2026-11-15T08:36:37Z",
        "finished_at": "2026-11-15T08:37:38Z",
    }


def test_an_event_woken_wait_shows_the_event_as_its_result() -> None:
    message = {
        "id": "m-1",
        "step_id": "wait_for_run",
        "step_execution_id": "e-1",
        "from_agent": "wait_step",
        "skill": "wait",
        "status": "pass",
        "summary": "Wait step 'wait_for_run' received run_completed (key run-42) at 2026-11-15T08:37:34+00:00",
        "timestamp": "2026-11-15T08:37:38Z",
        "artifacts": {
            "event_type": "run_completed",
            "received_at": "2026-11-15T08:37:34+00:00",
            "correlation_key": "run-42",
            "source": "acme.crawler",
            "payload": {"records": 20, "outcome": "complete"},
            "step_kind": "wait",
            "step_id": "wait_for_run",
            "wait_id": "w-1",
            "summary_key": "react.jobs.step_wait_event_received",
            "summary_params": {
                "event_type": "run_completed",
                "correlation_key": "run-42",
                "received_at": "2026-11-15T08:37:34+00:00",
            },
            "_outcome": {"name": "success"},
        },
        "errors": [],
    }

    steps, _ = build_execution_steps(
        job={"id": "job-1", "project_id": "p-1", "status": "completed"},
        messages=[message],
        workflow_run={"steps": [_durable_row("wait_for_run")]},
    )

    (step,) = steps
    assert step["source"] == "message"
    assert step["status"] == "completed"
    assert step["outputs"] == {
        "event_type": "run_completed",
        "received_at": "2026-11-15T08:37:34+00:00",
        "correlation_key": "run-42",
        "source": "acme.crawler",
        "payload": {"records": 20, "outcome": "complete"},
    }
    assert step["output_field_label_keys"]["received_at"] == "react.jobs.field_received_at"
    assert step["summary_key"] == "react.jobs.step_wait_event_received"
    assert step["step_type_label_key"] == "react.workflows.step_kind_wait"
    assert [card.get("title_key") for card in step["evidence"][:2]] == [
        "react.jobs.field_event_type",
        "react.jobs.field_received_at",
    ]


def test_a_failed_child_reaches_the_parents_invoke_step_as_a_failed_step() -> None:
    reason = "No target page could be sampled: start page not usable (HTTP 404)"
    failure = {
        "child_error_message": reason,
        "child_error_code": "start_page_unusable",
        "child_status": "failed",
        "child_error_step_id": "fetch_samples",
        "child_job_id": "a5c049c4",
    }
    message = {
        "id": "m-2",
        "step_id": "scrape",
        "step_execution_id": "e-1",
        "from_agent": "invoke_workflow",
        "skill": "invoke_workflow",
        "status": "fail",
        "summary": (
            "Child workflow 'scrape_a_site' finished with status 'failed' at step "
            f"'fetch_samples': {reason}"
        ),
        "timestamp": "2026-11-15T08:37:38Z",
        "artifacts": {
            **failure,
            "output_payload": dict(failure),
            "_invoke_workflow_error": "child_failed",
            "step_id": "scrape",
            "step_kind": "invoke_workflow",
        },
        "errors": [f"Child workflow 'scrape_a_site' finished with status 'failed': {reason}"],
    }

    steps, _ = build_execution_steps(
        job={"id": "job-2", "project_id": "p-1", "status": "failed"},
        messages=[message],
        # The node finished, so the durable row says completed; the error route
        # took the failure.
        workflow_run={"steps": [_durable_row("scrape")]},
    )

    (step,) = steps
    assert step["status"] == "failed"
    assert step["status_label"] == "Failed"
    assert reason in step["summary"]
    assert reason in str(step["error"])
    assert step["outputs"] == failure
    assert step["output_field_label_keys"]["child_error_message"] == (
        "react.jobs.field_child_error_message"
    )
    assert step["evidence"][0]["summary"] == reason
    assert step["evidence"][0]["title_key"] == "react.jobs.field_child_error_message"
