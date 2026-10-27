from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from core.application.jobs import job_runtime_payload


def _identity(value):
    return value


def test_build_runtime_payload_merges_runtime_state_and_persisted_rows() -> None:
    now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=UTC)
    job = {
        "id": 77,
        "status": "retryable_interrupted_step",
        "thread_id": "thread-77",
        "meta": {
            "retryable_interrupted_step": {
                "agent_id": "composer",
                "skill": "write_section",
            }
        },
        "selected_workflow": "",
        "workflow_id": "",
        "current_step_id": "implementation",
        "current_group_id": None,
        "current_agent": "composer",
        "last_error": "",
        "last_response_excerpt": "",
        "heartbeat_at": now,
    }
    runtime_state = {
        "run_cursor": {
            "step_id": "write_section",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
            "attempt": 3,
            "frames": [
                {
                    "frame": "group",
                    "group_id": "per_section",
                    "group_execution_id": "4b5c1d2e-0000-4000-8000-000000000001",
                },
                {
                    "frame": "for_each",
                    "for_each_id": "sections",
                    "for_each_execution_id": "4b5c1d2e-0000-4000-8000-000000000002",
                    "item_index": 2,
                    "selected_count": 5,
                    "source_count": 7,
                    "applied_limit": 5,
                    "processed_count": 2,
                    "truncated": True,
                },
            ],
        },
        "current_step_id": "write_section",
        "current_group_id": "per_section",
        "test_mode": True,
        "parallel_lanes": [
            {
                "lane_id": "lane_a",
                "status": "running",
                "completed_steps": 1,
                "total_steps": 3,
                "next_agent": "composer",
                "positions": [0, 2, 4],
                "merge_status": "retrying",
                "merge_id": 42,
            }
        ],
        "parallel_concurrency_cap": 2,
        "parallel_execution_order": [0, 1, 2],
    }
    bl = MagicMock()
    bl.get_latest_log_entry.return_value = None
    bl.get_latest_event.return_value = None
    bl.get_latest_agent_message.return_value = None
    bl.list_job_errors.return_value = []

    payload = job_runtime_payload.build_runtime_payload(
        job,
        bl,
        get_thread_state_values=lambda _thread_id: runtime_state,
        to_json_obj=_identity,
        serialize_job_error_row=_identity,
        is_graph_interrupt_trace=lambda value: "GraphInterrupt" in str(value),
        ws_get_approval_payload=lambda _job: None,
        sanitize_retryable_interrupted_step_snapshot=_identity,
        compute_wait_reason=lambda _job, _runtime_state, _approval_payload: "waiting",
        serialize_event_row=_identity,
        serialize_agent_message_row=_identity,
        now_fn=lambda: now,
        stuck_threshold_seconds=180,
    )

    assert payload["current_step_id"] == "write_section"
    assert payload["current_group_id"] == "per_section"
    assert payload["active_steps"] == [
        {
            "step_id": "write_section",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
            "attempt": 3,
            # The immediate owner only: the For each, not the Group around it.
            "group_id": None,
            "for_each_id": "sections",
            "lane_id": None,
            "iteration": 2,
            # Where the For each is, and its bounded counts; never the items.
            "for_each_progress": {
                "for_each_id": "sections",
                "item_index": 2,
                "selected_count": 5,
                "source_count": 7,
                "applied_limit": 5,
                "processed_count": 2,
                "truncated": True,
            },
            "status": "in_progress",
        }
    ]
    assert payload["retry_count"] == 2
    assert "active_phase" not in payload
    assert "phase_stack" not in payload
    assert payload["parallel_concurrency_cap"] == 2
    assert payload["parallel_execution_order"] == [0, 1, 2]
    assert payload["parallel_lanes"][0]["lane_id"] == "lane_a"
    assert payload["parallel_lanes"][0]["merge_id"] == "42"
    assert payload["test_mode"] is True
    assert payload["can_retry_interrupted_step"] is True
    assert payload["retryable_interrupted_step"]["skill"] == "write_section"
    assert "job_worktrees" not in payload


def test_build_runtime_payload_uses_invocation_selected_workflow_fallback() -> None:
    job = {
        "id": 778,
        "status": "completed",
        "thread_id": "thread-778",
        "meta": {
            "invocation": {
                "selected_workflow_id": "sample_parallel_launch_review",
                "workflow_selection_source": "explicit_request",
            }
        },
        "selected_workflow": "",
        "workflow_id": "",
        "current_step_id": "complete",
        "current_agent": "",
        "last_error": "",
        "last_response_excerpt": "",
        "heartbeat_at": None,
    }
    bl = MagicMock()
    bl.get_latest_log_entry.return_value = None
    bl.get_latest_event.return_value = None
    bl.get_latest_agent_message.return_value = None
    bl.list_job_errors.return_value = []

    payload = job_runtime_payload.build_runtime_payload(
        job,
        bl,
        get_thread_state_values=lambda _thread_id: {},
        to_json_obj=_identity,
        serialize_job_error_row=_identity,
        is_graph_interrupt_trace=lambda value: "GraphInterrupt" in str(value),
        ws_get_approval_payload=lambda _job: None,
        sanitize_retryable_interrupted_step_snapshot=_identity,
        compute_wait_reason=lambda _job, _runtime_state, _approval_payload: "",
        serialize_event_row=_identity,
        serialize_agent_message_row=_identity,
    )

    assert payload["selected_workflow"] == "sample_parallel_launch_review"
    assert payload["selected_workflow_source"] == "meta.invocation.selected_workflow_id"


def test_build_runtime_payload_suppresses_graph_interrupt_errors() -> None:
    graph_interrupt_error = (
        "langgraph.errors.GraphInterrupt: (Interrupt(value={'stage': 'Review'}, id='x'),)"
    )
    job = {
        "id": 555,
        "status": "in_progress",
        "thread_id": "",
        "last_error": graph_interrupt_error,
        "selected_workflow": "",
        "workflow_id": "",
        "meta": {},
        "current_step_id": "",
        "current_agent": "",
        "last_response_excerpt": "",
        "heartbeat_at": None,
    }
    bl = MagicMock()

    def _latest_log_entry(_job_id, *args, **kwargs):
        if kwargs.get("require_error"):
            return {"error": graph_interrupt_error}
        return None

    bl.get_latest_log_entry.side_effect = _latest_log_entry
    bl.get_latest_event.return_value = None
    bl.get_latest_agent_message.return_value = None
    bl.list_job_errors.return_value = []

    payload = job_runtime_payload.build_runtime_payload(
        job,
        bl,
        get_thread_state_values=lambda _thread_id: {},
        to_json_obj=_identity,
        serialize_job_error_row=_identity,
        is_graph_interrupt_trace=lambda value: "GraphInterrupt" in str(value),
        ws_get_approval_payload=lambda _job: None,
        sanitize_retryable_interrupted_step_snapshot=_identity,
        compute_wait_reason=lambda _job, _runtime_state, _approval_payload: "",
        serialize_event_row=_identity,
        serialize_agent_message_row=_identity,
    )

    assert payload["last_stderr_excerpt"] == ""


def test_build_runtime_payload_terminal_job_does_not_restore_active_step_from_state() -> None:
    job = {
        "id": 901,
        "status": "cancelled",
        "thread_id": "thread-901",
        "selected_workflow": "",
        "workflow_id": "",
        "meta": {},
        "current_step_id": "",
        "current_agent": "",
        "last_error": "",
        "last_response_excerpt": "",
        "heartbeat_at": None,
    }
    bl = MagicMock()
    bl.get_latest_log_entry.return_value = None
    bl.get_latest_event.return_value = None
    bl.get_latest_agent_message.return_value = {
        "step_id": "development",
        "from_agent": "composer",
    }
    bl.list_job_errors.return_value = []

    payload = job_runtime_payload.build_runtime_payload(
        job,
        bl,
        get_thread_state_values=lambda _thread_id: {
            "current_step_id": "development",
            "run_cursor": {
                "step_id": "development",
                "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                "attempt": 1,
            },
            "active_agents": ["composer"],
        },
        to_json_obj=_identity,
        serialize_job_error_row=_identity,
        is_graph_interrupt_trace=lambda value: "GraphInterrupt" in str(value),
        ws_get_approval_payload=lambda _job: None,
        sanitize_retryable_interrupted_step_snapshot=_identity,
        compute_wait_reason=lambda _job, _runtime_state, _approval_payload: "",
        serialize_event_row=_identity,
        serialize_agent_message_row=_identity,
    )

    assert payload["current_step_id"] is None
    assert payload["current_group_id"] is None
    assert payload["active_steps"] == []
    assert payload["active_agent"] == ""
    assert payload["last_message"]["step_id"] == "development"


def test_build_runtime_payload_fallback_marks_stuck_jobs() -> None:
    now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=UTC)
    payload = job_runtime_payload.build_runtime_payload_fallback(
        {
            "status": "in_progress",
            "selected_workflow": "wf-1",
            "current_step_id": "implementation",
            "current_group_id": "per_file",
            "current_agent": "composer",
            "last_error": "boom",
            "last_response_excerpt": "partial",
            "heartbeat_at": now - timedelta(seconds=120),
        },
        watchdog_stuck_seconds=60,
        now_fn=lambda: now,
    )

    assert payload["selected_workflow"] == "wf-1"
    assert payload["current_step_id"] == "implementation"
    assert payload["current_group_id"] == "per_file"
    assert payload["active_steps"] == []
    assert payload["active_agent"] == "composer"
    assert payload["is_stuck"] is True
    assert payload["runtime_fallback"] is True


def test_a_parallel_group_lists_its_running_lanes_and_names_no_current_step():
    from core.application.jobs.job_runtime_payload import _active_steps, current_step_scope

    runtime_state = {
        "run_cursor": {
            "step_id": None,
            "frames": [{"frame": "group", "group_id": "fan_out", "group_execution_id": "g-1"}],
        },
        "parallel_lanes": [
            {
                "lane_id": "left",
                "status": "running",
                "step_id": "a",
                "step_execution_id": "e-a",
                "attempt": 1,
            },
            {
                "lane_id": "right",
                "status": "escalated",
                "step_id": "b",
                "step_execution_id": "e-b",
                "attempt": 2,
            },
            {"lane_id": "done", "status": "completed", "step_id": None, "step_execution_id": None},
        ],
    }

    lanes = _active_steps(runtime_state)

    assert [(row["lane_id"], row["status"], row["attempt"]) for row in lanes] == [
        ("left", "in_progress", 1),
        ("right", "escalated", 2),
    ]
    assert all(row["group_id"] == "fan_out" for row in lanes)
    # The job row's last lane step is never reported as the current step.
    assert current_step_scope(runtime_state, {"current_step_id": "b"}) == ("", "fan_out")


def test_a_serial_pause_reports_the_waiting_step_with_the_runs_status():
    from core.application.jobs.job_runtime_payload import _active_steps

    runtime_state = {
        "run_cursor": {"step_id": "hold", "step_execution_id": "e-1", "attempt": 1, "frames": []},
    }

    (row,) = _active_steps(runtime_state, job_status="awaiting_timer")

    assert (row["step_id"], row["status"], row["group_id"]) == ("hold", "awaiting_timer", None)
