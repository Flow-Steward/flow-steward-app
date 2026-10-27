from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.application.jobs.scheduled_job_concurrency import resolve_scheduled_job_concurrency_key
from core.application.management.command_models import (
    ProjectScheduledJobCreateInput,
    ProjectScheduledJobUpdateInput,
)
from core.entrypoints.admin.project_schedules import (
    ApiScheduledJobCreateRequest,
    ApiScheduledJobUpdateRequest,
)


@pytest.mark.parametrize(
    "model",
    [
        ApiScheduledJobCreateRequest,
        ApiScheduledJobUpdateRequest,
        ProjectScheduledJobCreateInput,
        ProjectScheduledJobUpdateInput,
    ],
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instructions", "Do more"),
        ("concurrency_scope", "workflow"),
        ("concurrency_key", "shared"),
        ("catchup_policy", "latest_only"),
        ("task_summary", "Do more"),
    ],
)
def test_schedule_input_rejects_retired_fields(model, field, value) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate(
            {
                "workflow_id": "wf",
                "cron_expr": "0 * * * *",
                field: value,
            }
        )
    assert any(
        item["loc"] == (field,) and item["type"] == "extra_forbidden"
        for item in error.value.errors()
    )


@pytest.mark.parametrize(
    "model",
    [
        ApiScheduledJobCreateRequest,
        ApiScheduledJobUpdateRequest,
        ProjectScheduledJobCreateInput,
        ProjectScheduledJobUpdateInput,
    ],
)
def test_schedule_accepts_workflow_identity_without_arbitrary_task_summary(model) -> None:
    schedule = model.model_validate({"workflow_id": "wf", "cron_expr": "0 * * * *"})
    assert schedule.workflow_id == "wf"
    assert "task_summary" not in schedule.model_dump()


def test_automatic_schedule_lock_isolates_account_project_and_schedule() -> None:
    keys = {
        resolve_scheduled_job_concurrency_key(
            account_id=account,
            project_id=project,
            scheduled_job_id=schedule,
        )
        for account, project, schedule in [
            ("a", "p", 1),
            ("b", "p", 1),
            ("a", "q", 1),
            ("a", "p", 2),
        ]
    }
    assert keys == {
        "scheduled_job:a:p:1",
        "scheduled_job:b:p:1",
        "scheduled_job:a:q:1",
        "scheduled_job:a:p:2",
    }
