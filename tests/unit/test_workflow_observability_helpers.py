from __future__ import annotations

import pytest

from tests.e2e import test_workflow_observability_contracts as observability


def _row(
    *,
    job_id: int,
    status: str,
    task_id: str,
    project_id: int = 69,
    current_phase: str = "",
    current_agent: str = "",
    wait_reason: str = "",
) -> dict:
    runtime = {"active_phase": current_phase, "wait_reason": wait_reason}
    return {
        "id": job_id,
        "project_id": project_id,
        "status": status,
        "task_id": task_id,
        "current_phase": current_phase,
        "current_agent": current_agent,
        "runtime": runtime,
        "thread_id": "thread-1",
    }


def test_drive_job_to_terminal_treats_active_child_workflow_as_progress(
    monkeypatch: pytest.MonkeyPatch,
):
    parent_task_id = "parent-task"
    parent_rows = iter(
        [
            _row(
                job_id=101,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
            ),
            _row(
                job_id=101,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
            ),
            _row(
                job_id=101,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
            ),
            _row(
                job_id=101,
                status="completed",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Parent invoke completed",
            ),
        ]
    )
    child_snapshots = iter(
        [
            [
                _row(
                    job_id=202,
                    status="in_progress",
                    task_id=f"{parent_task_id}:invoke:review_cycle:story_loop:STORY-001",
                    current_phase="development",
                    current_agent="composer",
                )
            ],
            [
                _row(
                    job_id=202,
                    status="in_progress",
                    task_id=f"{parent_task_id}:invoke:review_cycle:story_loop:STORY-001",
                    current_phase="quality",
                    current_agent="proofreader",
                )
            ],
            [
                _row(
                    job_id=202,
                    status="in_progress",
                    task_id=f"{parent_task_id}:invoke:review_cycle:story_loop:STORY-001",
                    current_phase="review",
                    current_agent="critic",
                )
            ],
            [],
        ]
    )
    now = {"value": 0.0}
    cancelled: list[int] = []

    def _fake_job_row(_job_id: int) -> dict:
        return next(parent_rows)

    def _fake_project_jobs(_project_id: str, *, page: int = 1, per_page: int = 100) -> list[dict]:
        _ = page, per_page
        try:
            return next(child_snapshots)
        except StopIteration:
            return []

    def _fake_monotonic() -> float:
        return now["value"]

    def _fake_sleep(seconds: int | float) -> None:
        now["value"] += float(seconds)

    monkeypatch.setattr(observability, "_job_row", _fake_job_row)
    monkeypatch.setattr(observability, "_project_jobs", _fake_project_jobs)
    monkeypatch.setattr(observability, "_approve_job", lambda _thread_id: None)
    monkeypatch.setattr(observability, "_approval_needed", lambda _row: False)
    monkeypatch.setattr(observability, "_cancel_job", lambda job_id: cancelled.append(job_id))
    monkeypatch.setattr(observability.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(observability.time, "sleep", _fake_sleep)
    monkeypatch.setattr(observability, "WORKFLOW_ACTIVITY_TIMEOUT", 5)

    result = observability._drive_job_to_terminal(
        101,
        timeout=2,
        interval=1,
        max_approvals=0,
    )

    assert result["status"] == "completed"
    assert cancelled == []
