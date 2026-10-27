"""A Test Run that stops at a gate must be decidable.

Remediation epic 2026-07-29, WS-04 — `AUDIT-038` symptoms (b) and (d), root
cause RD-004.

Workflow test runs are durable in the checkpointer but were not durable as
*jobs*, and both halves of the approval lifecycle are job-anchored:

* `get_awaiting_approval_jobs` selects `j.status = 'awaiting_approval'` from
  `jobs`, so a run with no row can never appear in the approvals drawer;
* `ApprovalCommandService.resume_task` resolves the owning project through the
  same row in order to run `require_project_access`, so a run with no row
  answers 404 *"Approval job not found"* when anyone tries to decide it.

Confirmed against the running stack before the fix: 14 distinct `test-run-`
threads existed in `checkpoints` and **zero** rows in `jobs`; a decision POST to
one of them returned `404 Approval job not found`.

The jobless rejection in `resume_task` is a deliberate security property — the
row is what carries the scope — so the fix gives the run an anchor rather than
skipping the check. These tests pin that, and pin that the anchor is created
only when a human decision is actually pending.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from core.application.workflows.platform_test_run_runtime import (
    anchor_test_run_awaiting_approval,
)

AWAITING = {
    "status": "awaiting_approval",
    "needs_user_approval": True,
    "thread_id": "test-run-abc123",
    "execution_id": "exec-1",
    "approval_id": "int-9",
}


def _logger(job_id: str = "job-1") -> Any:
    bl = MagicMock()
    bl.create_job.return_value = job_id
    return bl


class TestARunParkedAtAGateGetsAnAnchor:
    def test_a_job_row_is_created(self):
        bl = _logger()

        job_id = anchor_test_run_awaiting_approval(
            bl,
            AWAITING,
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        assert job_id == "job-1"
        assert bl.create_job.call_count == 1

    def test_the_row_is_bound_to_the_runs_own_thread(self):
        """resume_task looks the run up by thread_id; a different id anchors nothing."""
        bl = _logger()

        anchor_test_run_awaiting_approval(
            bl,
            AWAITING,
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        assert bl.create_job.call_args.kwargs["thread_id"] == "test-run-abc123"

    def test_the_row_is_awaiting_approval_so_the_drawer_can_find_it(self):
        """get_awaiting_approval_jobs filters on exactly this status."""
        bl = _logger()

        anchor_test_run_awaiting_approval(
            bl,
            AWAITING,
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        assert bl.create_job.call_args.kwargs["status"] == "awaiting_approval"

    def test_the_workflow_is_recorded_where_resume_task_reads_it(self):
        """resume_task resolves the workflow for require_workflow_access from
        COALESCE(selected_workflow, selected_workflow_key, meta->>'selected_workflow_id')."""
        bl = _logger()

        anchor_test_run_awaiting_approval(
            bl,
            AWAITING,
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        meta = bl.create_job.call_args.kwargs["meta"]
        assert meta["selected_workflow_id"] == "monthly_review"

    def test_the_row_is_marked_as_a_test_run(self):
        """So a test run is distinguishable from a submitted task downstream."""
        bl = _logger()

        anchor_test_run_awaiting_approval(
            bl,
            AWAITING,
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        assert bl.create_job.call_args.kwargs["meta"]["workflow_test_run"] is True
        assert bl.create_job.call_args.kwargs["task_source"] == "workflow_test_run"

    def test_the_project_carries_the_scope(self):
        """create_job derives tenant/account/workspace from this project row,
        which is what require_project_access later checks."""
        bl = _logger()

        anchor_test_run_awaiting_approval(
            bl,
            AWAITING,
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        assert bl.create_job.call_args.args[0] == "project_alpha"


class TestRunsThatNeedNoDecisionStayEphemeral:
    def test_a_completed_run_creates_no_job(self):
        bl = _logger()

        job_id = anchor_test_run_awaiting_approval(
            bl,
            {"status": "completed", "needs_user_approval": False, "thread_id": "test-run-z"},
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        assert job_id == ""
        bl.create_job.assert_not_called()

    def test_a_run_without_a_thread_creates_no_job(self):
        """An unanchorable run must not produce a row nothing can resolve."""
        bl = _logger()

        job_id = anchor_test_run_awaiting_approval(
            bl,
            {"needs_user_approval": True, "thread_id": ""},
            project_id="project_alpha",
            workflow_id="monthly_review",
            task_id="task-1",
            task_summary="Run the workflow",
        )

        assert job_id == ""
        bl.create_job.assert_not_called()

    def test_no_logger_is_not_an_error(self):
        assert (
            anchor_test_run_awaiting_approval(
                None,
                AWAITING,
                project_id="project_alpha",
                workflow_id="monthly_review",
                task_id="task-1",
                task_summary="Run the workflow",
            )
            == ""
        )


class TestAnchoringNeverBreaksTheRun:
    def test_a_failure_to_anchor_returns_empty_rather_than_raising(self):
        """The reviewer still gets their run result; it is simply not decidable."""
        bl = MagicMock()
        bl.create_job.side_effect = RuntimeError("unique violation")

        assert (
            anchor_test_run_awaiting_approval(
                bl,
                AWAITING,
                project_id="project_alpha",
                workflow_id="monthly_review",
                task_id="task-1",
                task_summary="Run the workflow",
            )
            == ""
        )


class TestTheRunPathPassesTheLoggerThrough:
    def test_run_workflow_test_run_ephemeral_accepts_a_logger(self):
        """Without this parameter the anchor can never be created in production."""
        import inspect

        from core.application.workflows.platform_test_run_runtime import (
            run_workflow_test_run_ephemeral,
        )

        assert "bl" in inspect.signature(run_workflow_test_run_ephemeral).parameters

    def test_the_admin_route_uses_the_durable_submission_owner(self):
        from pathlib import Path

        source = Path("core/entrypoints/admin/workflows_definition_run_routes.py").read_text(
            encoding="utf-8"
        )

        assert "submit_test_run(" in source
        assert "run_workflow_test_run_ephemeral" not in source
