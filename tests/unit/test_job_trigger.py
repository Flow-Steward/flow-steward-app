from __future__ import annotations

import pytest

from core.application.reports.job_trigger import job_trigger


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (
            {"task_source": "workflow_test_run", "meta": {"actor_id": "ana@example.test"}},
            {
                "kind": "test_run",
                "label": "Test run from the workflow builder",
                "label_key": "react.jobs.workflow_builder_test_run",
            },
        ),
        (
            {"task_source": "step_test_run", "meta": {"step_id": "resize"}},
            {
                "kind": "test_run",
                "label": "Step test run from the workflow builder",
                "detail": "step resize",
            },
        ),
        (
            {
                "task_source": "invoke_workflow",
                "meta": {"invoke_parent_job_id": "job-1", "invoke_parent_workflow_id": "wf-a"},
            },
            {
                "kind": "parent_workflow",
                "label": "Another workflow",
                "detail": "workflow wf-a",
                "link": "/jobs/job-1",
            },
        ),
        (
            {
                "task_source": "api",
                "meta": {
                    "scheduled_job_id": "s-1",
                    "scheduled_run_type": "scheduled",
                    "scheduled_cron_expr": "0 * * * *",
                },
            },
            {
                "kind": "schedule",
                "label": "Schedule",
                "label_key": "project_detail.scheduled.fields.schedule",
                "detail": "cron 0 * * * *",
            },
        ),
        (
            {
                "meta": {
                    "scheduled_job_id": "s-1",
                    "scheduled_run_type": "manual",
                    "scheduled_cron_expr": "0 9 * * 1",
                }
            },
            {
                "kind": "schedule",
                "label": "Schedule, run now",
                "label_key": "react.jobs.schedule_run_now",
                "detail": "cron 0 9 * * 1",
            },
        ),
        (
            {"invocation": {"channel": "webhook", "entry_key": "orders"}},
            {"kind": "webhook", "label": "Webhook"},
        ),
        (
            {"task_source": "job_restart", "meta": {"restart_of_job_id": "job-0"}},
            {"kind": "restart", "label": "Restart", "link": "/jobs/job-0"},
        ),
        (
            {"invocation": {"channel": "manual_ui"}, "meta": {"actor_id": "ana"}},
            {"kind": "manual_ui", "label": "Started in the app"},
        ),
        ({}, {"kind": "api", "label": "API"}),
    ],
)
def test_job_trigger_names_what_started_the_run(job: dict, expected: dict) -> None:
    assert job_trigger(job) == expected


def test_job_detail_contract_exposes_the_trigger() -> None:
    from core.application.reports.job_detail_contract import _TOP_LEVEL_JOB_FIELDS

    assert "trigger" in _TOP_LEVEL_JOB_FIELDS
