from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.jobs.task_dispatch_publish import _release_scheduled_lock


def test_scheduled_lock_release_requires_mandatory_store_capability() -> None:
    _raises_input_12_1 = SimpleNamespace()
    _raises_input_12_2 = MagicMock()
    with pytest.raises(RuntimeError, match="release capability is unavailable"):
        _release_scheduled_lock(
            _raises_input_12_1,
            {"id": "lock-1", "scheduled_job_id": "schedule-1", "account_id": "acc-1"},
            job_id="job-1",
            project_id="project-1",
            reason="publish_failed",
            logger=_raises_input_12_2,
            log_prefix="scheduled",
        )


def test_scheduled_lock_release_records_complete_safe_context() -> None:
    store = MagicMock()
    store.release_scheduled_job_execution_lock_by_id.return_value = {
        "id": "lock-1",
        "status": "released",
    }

    _release_scheduled_lock(
        store,
        {
            "id": "lock-1",
            "scheduled_job_id": "schedule-1",
            "account_id": "acc-1",
            "project_id": "project-1",
        },
        job_id="job-1",
        project_id="project-1",
        reason="publish_failed",
        logger=MagicMock(),
        log_prefix="scheduled",
    )

    store.release_scheduled_job_execution_lock_by_id.assert_called_once_with(
        lock_id="lock-1",
        release_reason="publish_failed",
    )
    event = store.log_event.call_args.kwargs
    assert event["job_id"] == "job-1"
    assert event["event_type"] == "scheduled_execution_lock_released"
    assert event["detail"] == {
        "scheduled_job_id": "schedule-1",
        "job_id": "job-1",
        "account_id": "acc-1",
        "project_id": "project-1",
        "lock_id": "lock-1",
        "reason": "publish_failed",
    }


def test_failed_dispatch_release_allows_the_next_schedule_slot() -> None:
    class SlotStore:
        def __init__(self) -> None:
            self.active_lock = "lock-1"
            self.events: list[dict] = []

        def release_scheduled_job_execution_lock_by_id(self, *, lock_id, release_reason):
            assert release_reason == "publish_failed"
            if self.active_lock != lock_id:
                return None
            self.active_lock = None
            return {"id": lock_id, "status": "released"}

        def acquire_same_slot(self):
            if self.active_lock is not None:
                return None
            self.active_lock = "lock-2"
            return {"id": self.active_lock}

        def log_event(self, **kwargs):
            self.events.append(kwargs)

    store = SlotStore()
    _release_scheduled_lock(
        store,
        {"id": "lock-1", "scheduled_job_id": "schedule-1"},
        job_id="job-1",
        project_id="project-1",
        reason="publish_failed",
        logger=MagicMock(),
        log_prefix="scheduled",
    )

    assert store.acquire_same_slot() == {"id": "lock-2"}
    assert store.events[0]["event_type"] == "scheduled_execution_lock_released"
