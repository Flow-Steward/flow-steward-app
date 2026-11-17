"""A run failed by its output contract at a successful end says why, everywhere.

The probe's case: a reviewer rejects, the graph takes the ``rejected`` edge to a
Stop Success that never writes the required report output, and final validation
fails the run. The failure stays a failure (that is the contract), but the job
error, the incident, the status, the run report and the authoring readiness
must all explain it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import tests.unit.test_validation_node as validation_tests
from core.application.jobs.execution_contracts import terminal_failure_context
from core.application.jobs.job_polling import build_job_polling_payload
from core.application.jobs.terminal_persistence import record_structured_job_error
from core.application.jobs.workflow_resume_finalization_repository import (
    WorkflowResumeFinalizationRepository,
)
from core.application.jobs.workflow_run_report import workflow_run_report
from core.application.management.query_models import JobStatusView
from core.application.registry.contract_validation import validate_workflow_readiness
from tests._workflow_definitions import edge, node, stop_step

MISSING_REPORT = {
    "code": "workflow.output.required",
    "message": "Workflow output 'sales_report' is required",
    "path": "outputs.sales_report",
}
REASON = (
    "Required output 'sales_report' was not written: the run reached the successful end "
    "'rejected_end' without it, so its output contract failed."
)


def _validate_rejected_run() -> dict[str, Any]:
    from core.application.orchestration.validation_node import orchestrator_validation_node

    state = validation_tests.make_state(
        workflow_terminal={
            "kind": "stop_success",
            "status": "pass",
            "step_id": "rejected_end",
            "step_execution_id": "exec-rejected-1",
        }
    )
    with validation_tests.TestValidationNode()._common_patch(ok=False, errors=[MISSING_REPORT]):
        return orchestrator_validation_node(state)


def test_validation_records_the_missing_output_and_the_end_reached() -> None:
    result = _validate_rejected_run()

    assert result["status"] == "failed"
    assert result["last_failure_class"] == "workflow_output_contract"
    assert result["error"] == REASON
    assert result["output_contract_failure"] == {
        "error_code": "workflow.output.required",
        "path": "outputs.sales_report",
        "end_step_id": "rejected_end",
        "diagnostic_kind": "workflow_output_contract",
        "message": REASON,
    }
    assert terminal_failure_context(result) == {
        "error_code": "workflow.output.required",
        "diagnostic_kind": "workflow_output_contract",
        "path": "outputs.sales_report",
    }


def test_the_worker_persists_it_as_the_jobs_structured_error() -> None:
    from core.application.jobs.execution_contracts import TaskExecutionRequest, TaskGraphOutcome
    from core.infrastructure.runtime.worker import worker_execution_adapters

    result = _validate_rejected_run()
    service = MagicMock()
    service.complete_failure.return_value = SimpleNamespace(status="failed")
    request = TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Weekly report",
        task_source="test",
        project_name="Project",
        project_config={},
        extra_state={},
        workflow_id="weekly_report",
        test_mode=True,
    )
    adapter = worker_execution_adapters.WorkerTerminalEffectsAdapter(
        bl=MagicMock(), worker_id=lambda: "worker-1"
    )
    with patch.object(
        worker_execution_adapters, "_terminal_persistence_service", return_value=service
    ):
        adapter.persist(
            request,
            job_row={},
            project_id="project-1",
            final_status="failed",
            result=result,
            graph_outcome=TaskGraphOutcome.from_result(result),
        )

    kwargs = service.complete_failure.call_args.kwargs
    assert kwargs["error_message"] == REASON
    assert kwargs["error_code"] == "workflow.output.required"
    assert kwargs["diagnostic_kind"] == "workflow_output_contract"
    assert kwargs["diagnostic_path"] == "outputs.sales_report"
    # The Stop Success itself succeeded: no step is marked failed.
    assert kwargs["step_id"] is None


def test_the_structured_error_row_carries_code_and_path() -> None:
    logger = MagicMock()
    record_structured_job_error(
        logger,
        "job-1",
        {
            "error_code": "workflow.output.required",
            "error_message": REASON,
            "category": "output_contract",
            "diagnostic_kind": "workflow_output_contract",
            "path": "outputs.sales_report",
        },
    )

    kwargs = logger.log_job_error.call_args.kwargs
    assert kwargs["category"] == "contract"
    assert kwargs["error_message"] == REASON
    assert kwargs["context_jsonb"] == {
        "error_code": "workflow.output.required",
        "diagnostic_kind": "workflow_output_contract",
        "path": "outputs.sales_report",
    }


class _Cursor:
    rowcount = 1

    def __init__(self, calls: list) -> None:
        self._calls = calls

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self._calls.append(sql)


class _Connection:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.commits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self.calls)

    def commit(self) -> None:
        self.commits += 1


def test_a_resumed_run_after_the_rejection_also_gets_its_error_row() -> None:
    """The approval resume finalizer writes the terminal row itself; it writes the error too."""
    result = _validate_rejected_run()
    bl = SimpleNamespace(conn=_Connection(), log_job_error=MagicMock())

    updated = WorkflowResumeFinalizationRepository(bl).mark_finalizing(
        job_id="job-1",
        resume_revision=1,
        lease_token="lease-1",
        final_status="failed",
        result=result,
        error=result["error"],
    )

    assert updated is True
    kwargs = bl.log_job_error.call_args.kwargs
    assert kwargs["error_message"] == REASON
    assert kwargs["context_jsonb"]["error_code"] == "workflow.output.required"
    assert kwargs["context_jsonb"]["path"] == "outputs.sales_report"
    assert bl.conn.commits == 1


def test_a_resumed_completion_writes_no_error_row() -> None:
    bl = SimpleNamespace(conn=_Connection(), log_job_error=MagicMock())

    WorkflowResumeFinalizationRepository(bl).mark_finalizing(
        job_id="job-1",
        resume_revision=1,
        lease_token="lease-1",
        final_status="completed",
        result={"status": "completed"},
    )

    bl.log_job_error.assert_not_called()


def _failed_status() -> JobStatusView:
    return JobStatusView(
        id="job-1",
        status="failed",
        task_summary="Weekly report",
        failure_summary={
            "present": True,
            "kind": "runtime_error",
            "title": "Runtime failure",
            "detail": REASON,
        },
    )


def test_job_status_and_run_report_say_why() -> None:
    polled = build_job_polling_payload(_failed_status())
    assert polled["error"] == {
        "kind": "runtime_error",
        "title": "Runtime failure",
        "message": REASON,
    }
    assert (
        build_job_polling_payload(JobStatusView(id="j", status="running", task_summary="x"))[
            "error"
        ]
        is None
    )

    model = workflow_run_report(_failed_status())["model"]
    assert model["state"] == "no_report"
    assert model["reason"] == REASON
    assert model["text"] == f"Job job-1 ended (failed) without a report. Reason: {REASON}"


def _report_workflow(*, rejected_writes: bool) -> dict[str, Any]:
    template = {
        "step_kind": "template",
        "group_id": None,
        "for_each_id": None,
        "lane_id": None,
    }
    steps = [
        {"step_id": "review", "step_kind": "approval", "group_id": None, "for_each_id": None},
        {
            **template,
            "step_id": "publish",
            "override": {"template": {"text": "Report", "result_target": "state.sales_report"}},
        },
        {
            **template,
            "step_id": "explain",
            "override": {
                "template": {
                    "text": "Rejected",
                    "result_target": "state.sales_report" if rejected_writes else "state.note",
                }
            },
        },
        {**stop_step("done"), "group_id": None, "for_each_id": None},
        {**stop_step("rejected_end"), "group_id": None, "for_each_id": None},
    ]
    graph = {
        "schema_version": 1,
        "entry": node("review"),
        "edges": [
            edge("review", node("publish"), "approved"),
            edge("publish", node("done")),
            edge("review", node("explain"), "rejected"),
            edge("explain", node("rejected_end")),
        ],
    }
    return {
        "workflow_id": "weekly_report",
        "display_name": "Weekly report",
        "enabled": False,
        "settings": {
            "execution_graph": graph,
            "callable": {
                "enabled": True,
                "allow_empty_inputs": True,
                "trigger_channels": ["ui"],
                "expected_outputs": [{"name": "sales_report", "type": "string", "required": True}],
            },
        },
        "steps": steps,
        "groups": [],
        "for_each": [],
    }


def _success_path_warnings(workflow: dict[str, Any]) -> list[dict]:
    readiness = validate_workflow_readiness(
        SimpleNamespace(get_workflow=lambda _: workflow), workflow["workflow_id"]
    )
    return [
        issue
        for issue in readiness["warnings"]
        if issue["code"] == "workflow.callable.output_missing_on_success_path"
    ]


def test_authoring_warns_when_a_successful_end_skips_a_required_output() -> None:
    (warning,) = _success_path_warnings(_report_workflow(rejected_writes=False))

    assert warning["severity"] == "warning"
    assert warning["path"] == "workflow.callable.expected_outputs[0].name"
    assert warning["message"].startswith(
        "Required output 'sales_report' is not written on every path that reaches a "
        "successful end (e.g. via explain → rejected_end)."
    )


def test_no_warning_when_every_successful_end_has_a_writer_on_its_path() -> None:
    assert _success_path_warnings(_report_workflow(rejected_writes=True)) == []
