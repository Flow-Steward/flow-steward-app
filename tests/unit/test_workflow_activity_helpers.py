from __future__ import annotations

import pytest

from tests.e2e.workflow_activity_helpers import wait_for_status_with_activity


def _row(
    *,
    job_id: int,
    status: str,
    task_id: str,
    project_id: int = 69,
    current_phase: str = "",
    current_agent: str = "",
    wait_reason: str = "",
    heartbeat_at: str = "",
) -> dict:
    runtime = {
        "active_phase": current_phase,
        "wait_reason": wait_reason,
        "heartbeat_at": heartbeat_at,
    }
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


def test_wait_for_status_with_activity_tracks_descendant_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    def _fake_job_row(_job_id: int) -> dict:
        return next(parent_rows)

    def _fake_project_jobs(_project_id: str) -> list[dict]:
        try:
            return next(child_snapshots)
        except StopIteration:
            return []

    def _fake_monotonic() -> float:
        return now["value"]

    def _fake_sleep(seconds: int | float) -> None:
        now["value"] += float(seconds)

    monkeypatch.setattr("tests.e2e.workflow_activity_helpers.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("tests.e2e.workflow_activity_helpers.time.sleep", _fake_sleep)

    result = wait_for_status_with_activity(
        101,
        get_job=_fake_job_row,
        project_jobs=_fake_project_jobs,
        target_statuses=["completed"],
        timeout=2,
        interval=1,
        activity_timeout=5,
    )

    assert result["status"] == "completed"


def test_wait_for_status_with_activity_treats_fresh_heartbeat_as_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_task_id = "parent-task"
    parent_rows = iter(
        [
            _row(
                job_id=301,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
                heartbeat_at="2026-04-18T10:00:00+00:00",
            ),
            _row(
                job_id=301,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
                heartbeat_at="2026-04-18T10:00:05+00:00",
            ),
            _row(
                job_id=301,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
                heartbeat_at="2026-04-18T10:00:10+00:00",
            ),
            _row(
                job_id=301,
                status="completed",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Parent invoke completed",
                heartbeat_at="2026-04-18T10:00:15+00:00",
            ),
        ]
    )
    now = {"value": 0.0}

    def _fake_job_row(_job_id: int) -> dict:
        return next(parent_rows)

    def _fake_project_jobs(_project_id: str) -> list[dict]:
        return []

    def _fake_monotonic() -> float:
        return now["value"]

    def _fake_sleep(seconds: int | float) -> None:
        now["value"] += float(seconds)

    monkeypatch.setattr("tests.e2e.workflow_activity_helpers.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("tests.e2e.workflow_activity_helpers.time.sleep", _fake_sleep)

    result = wait_for_status_with_activity(
        301,
        get_job=_fake_job_row,
        project_jobs=_fake_project_jobs,
        target_statuses=["completed"],
        timeout=2,
        interval=1,
        activity_timeout=5,
    )

    assert result["status"] == "completed"


def test_wait_for_status_with_activity_allows_long_running_descendant_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_task_id = "parent-task"
    parent_rows = iter(
        [
            _row(
                job_id=401,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
            ),
            _row(
                job_id=401,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
            ),
            _row(
                job_id=401,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
            ),
            _row(
                job_id=401,
                status="in_progress",
                task_id=parent_task_id,
                current_phase="story_loop",
                current_agent="invoke_workflow",
                wait_reason="Agent 'invoke_workflow' is executing",
            ),
            _row(
                job_id=401,
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
                    job_id=402,
                    status="in_progress",
                    task_id=f"{parent_task_id}:invoke:review_cycle:story_loop:STORY-001",
                    current_phase="development",
                    current_agent="composer",
                )
            ],
            [
                _row(
                    job_id=402,
                    status="in_progress",
                    task_id=f"{parent_task_id}:invoke:review_cycle:story_loop:STORY-001",
                    current_phase="quality",
                    current_agent="proofreader",
                )
            ],
            [
                _row(
                    job_id=402,
                    status="in_progress",
                    task_id=f"{parent_task_id}:invoke:review_cycle:story_loop:STORY-001",
                    current_phase="review",
                    current_agent="critic",
                )
            ],
            [
                _row(
                    job_id=402,
                    status="in_progress",
                    task_id=f"{parent_task_id}:invoke:review_cycle:story_loop:STORY-001",
                    current_phase="rework",
                    current_agent="reviser",
                )
            ],
            [],
        ]
    )
    now = {"value": 0.0}

    def _fake_job_row(_job_id: int) -> dict:
        return next(parent_rows)

    def _fake_project_jobs(_project_id: str) -> list[dict]:
        try:
            return next(child_snapshots)
        except StopIteration:
            return []

    def _fake_monotonic() -> float:
        return now["value"]

    def _fake_sleep(seconds: int | float) -> None:
        now["value"] += float(seconds)

    monkeypatch.setattr("tests.e2e.workflow_activity_helpers.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("tests.e2e.workflow_activity_helpers.time.sleep", _fake_sleep)

    result = wait_for_status_with_activity(
        401,
        get_job=_fake_job_row,
        project_jobs=_fake_project_jobs,
        target_statuses=["completed"],
        timeout=2,
        interval=2,
        activity_timeout=3,
    )

    assert result["status"] == "completed"
