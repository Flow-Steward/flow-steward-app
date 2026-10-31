"""Operator answers through the job services, HTTP routes and MCP adapters (task 1C).

Each test pins one defect of the operational job tools: status groups in the
filter, grouped attention that pages, project jobs that honour status and
workflow restrictions, result flags, restart of a live run, the cancel reason
and acknowledging actor, static validation, and a bounded approvals queue.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.management.command_models import JobAcknowledgeInput
from core.application.management.command_services import JobCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.query_services import JobQueryService
from core.application.management.scope import ScopeContext
from core.application.management.workflow_operator_service import WorkflowOperatorService
from core.application.reports.job_result_bundle import build_job_result_bundle
from core.entrypoints.api import jobs_control
from core.entrypoints.api.jobs import create_api_jobs_router
from core.entrypoints.mcp import server_api_tool

# ---------------------------------------------------------------------------
# helpers


def _query_service(bl) -> JobQueryService:
    return JobQueryService(
        get_logger=lambda: bl,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: row,
        job_attention_group=lambda status, acknowledged_at=None: (
            "needs_attention" if status in {"failed", "orphaned"} else "running"
        ),
        build_runtime_payload_for_api=AsyncMock(return_value={}),
    )


def _error_response(exc) -> JSONResponse:
    return JSONResponse(exc.payload, status_code=exc.status_code)


def _jobs_control_client(job_command_service) -> TestClient:
    app = FastAPI()
    app.include_router(
        jobs_control.create_jobs_control_router(
            project_command_service=lambda: SimpleNamespace(),
            approval_command_service=lambda: SimpleNamespace(),
            job_command_service=lambda: job_command_service,
            request_scope=lambda *_args, **_kwargs: ScopeContext(account_id="account-42"),
            service_error_response=_error_response,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
            logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
        )
    )
    return TestClient(app)


def _jobs_client(job_query_service) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_api_jobs_router(
            resolve_user_role=lambda _request: "operator",
            job_query_service=lambda: job_query_service,
            job_command_service=lambda: SimpleNamespace(),
            approval_query_service=lambda: SimpleNamespace(),
            request_scope=lambda *_args, **_kwargs: ScopeContext(account_id="account-42"),
            resolve_actor_username=lambda *_args, **_kwargs: "operator",
            optional_actor_username=lambda *_args, **_kwargs: "operator",
            service_error_response=_error_response,
            api_error_response=lambda exc, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            get_logger_only=lambda: None,
            watchdog_scan=lambda *_args, **_kwargs: {},
            parse_worker_pid=lambda *_args, **_kwargs: None,
            terminate_worker_pid=lambda *_args, **_kwargs: {},
            append_runtime_journal=lambda *_args, **_kwargs: {},
            build_rerun_workflow_selection=lambda *_args, **_kwargs: ({}, {}),
            load_replay_snapshot=lambda *_args, **_kwargs: {},
            enqueue_task=lambda *_args, **_kwargs: 1,
            retryable_interrupted_step_status="retryable_interrupted_step",
            err_database_not_configured="Database unavailable",
        )
    )
    return TestClient(app)


class _RecordingClient:
    responses: ClassVar[list[object]] = []
    requests: ClassVar[list[dict[str, object]]] = []

    def request(self, method: str, path: str, **kwargs: object) -> object:
        self.requests.append({"method": method, "path": path, **kwargs})
        return self.responses.pop(0)


@pytest.fixture
def mcp_client(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingClient]:
    monkeypatch.setattr(_RecordingClient, "responses", [])
    monkeypatch.setattr(_RecordingClient, "requests", [])
    monkeypatch.setattr(server_api_tool, "AppApiClient", _RecordingClient)
    monkeypatch.setattr(server_api_tool, "_caller_bearer", lambda: "caller-token")
    return _RecordingClient


# ---------------------------------------------------------------------------
# 26: a status group key selects its whole group


@pytest.mark.parametrize(
    ("requested", "stored"),
    [
        ("stuck", ("stuck", "orphaned")),
        ("failed", ("failed", "max_iterations", "triage_needed")),
        ("failed,stuck", ("failed", "max_iterations", "triage_needed", "stuck", "orphaned")),
        ("waiting", ("awaiting_timer", "awaiting_child", "external_wait")),
        ("acknowledged,pending", ("acknowledged", "pending")),
    ],
)
@pytest.mark.parametrize("group_by", ["", "attention"])
def test_status_group_key_filters_its_whole_group_before_paging(group_by, requested, stored):
    bl = MagicMock()
    bl.list_jobs_for_attention.return_value = []
    bl.count_jobs_for_attention.return_value = 0

    payload = _query_service(bl).list_jobs(
        group_by=group_by, status=requested, scope=ScopeContext(account_id="acc")
    )

    assert bl.list_jobs_for_attention.call_args.kwargs["status"] == stored
    assert bl.count_jobs_for_attention.call_args.kwargs["status"] == stored
    if not group_by:
        assert payload.status == requested


# ---------------------------------------------------------------------------
# 25: grouped attention pages and says whether the answer is complete


def _attention_bl(total: int) -> MagicMock:
    rows = [
        {"id": str(index), "project_id": "p", "status": "orphaned" if index % 2 else "in_progress"}
        for index in range(total)
    ]
    bl = MagicMock()
    bl.list_jobs_for_attention.side_effect = lambda **kwargs: rows[
        kwargs["offset"] : kwargs["offset"] + kwargs["limit"]
    ]
    bl.count_jobs_for_attention.return_value = total
    return bl


def test_attention_groups_report_total_and_more_pages_instead_of_dropping_old_runs():
    bl = _attention_bl(30)
    service = _query_service(bl)

    first = service.list_jobs(
        group_by="attention", page=1, per_page=25, scope=ScopeContext(account_id="acc")
    ).dump()
    second = service.list_jobs(
        group_by="attention", page=2, per_page=25, scope=ScopeContext(account_id="acc")
    ).dump()

    assert (first["total"], first["page"], first["per_page"], first["total_pages"]) == (
        30,
        1,
        25,
        2,
    )
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert [call.kwargs["offset"] for call in bl.list_jobs_for_attention.call_args_list] == [0, 25]
    listed = [
        row["id"]
        for payload in (first, second)
        for group in payload["groups"].values()
        for row in group
    ]
    assert sorted(listed, key=int) == [str(index) for index in range(30)]


def test_attention_groups_keep_limit_as_the_page_size_for_existing_callers():
    bl = _attention_bl(3)

    payload = _query_service(bl).list_jobs(
        group_by="attention", limit=100, scope=ScopeContext(account_id="acc")
    )

    assert bl.list_jobs_for_attention.call_args.kwargs["limit"] == 100
    assert (payload.total, payload.per_page, payload.has_more) == (3, 100, False)


def test_mcp_job_list_forwards_attention_paging(mcp_client):
    from core.entrypoints.mcp.server_job_tools import job_list

    mcp_client.responses.append({"groups": {}, "total": 30, "has_more": True})
    result = json.loads(job_list(group_by="attention", page=2, per_page=10))

    assert result["data"]["has_more"] is True
    assert mcp_client.requests[0]["params"]["page"] == 2
    assert mcp_client.requests[0]["params"]["per_page"] == 10


# ---------------------------------------------------------------------------
# 27: project jobs honour status and the caller's workflow restrictions


def _project_bl() -> MagicMock:
    bl = MagicMock()
    bl.get_project.return_value = {"id": "p", "account_id": DEFAULT_ACCOUNT_ID}
    bl.get_jobs.return_value = []
    bl.count_jobs.return_value = 0
    return bl


def test_project_jobs_filter_status_and_workflow_restriction_in_store_before_paging():
    bl = _project_bl()
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"release", "import"})
    )

    _query_service(bl).list_project_jobs("p", page=3, per_page=10, status="stuck", scope=scope)

    filters = {
        "status": ("stuck", "orphaned"),
        "workflow_ids": ["import", "release"],
    }
    bl.get_jobs.assert_called_once_with("p", limit=10, offset=20, **filters)
    bl.count_jobs.assert_called_once_with("p", **filters)


def test_project_jobs_for_a_single_workflow_token_list_only_that_workflow():
    bl = _project_bl()

    _query_service(bl).list_project_jobs(
        "p", scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, workflow_id="release")
    )

    assert bl.get_jobs.call_args.kwargs["workflow_id"] == "release"
    assert bl.count_jobs.call_args.kwargs["workflow_id"] == "release"


def test_unrestricted_project_jobs_call_the_store_as_before():
    bl = _project_bl()

    _query_service(bl).list_project_jobs("p", scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID))

    bl.get_jobs.assert_called_once_with("p", limit=25, offset=0)
    bl.count_jobs.assert_called_once_with("p")


def test_mcp_project_jobs_pages_and_filters(mcp_client, monkeypatch):
    from core.entrypoints.mcp.server_project_workflow_tools import project_jobs

    monkeypatch.setattr(server_api_tool, "_project_id_from_name", lambda *_a, **_k: "p-1")
    mcp_client.responses.append({"jobs": [], "total": 0})
    project_jobs(project_name="Shop", limit=500, page=3, status="failed")

    request = mcp_client.requests[0]
    assert request["path"] == "/api/projects/p-1/jobs"
    assert request["params"] == {"page": 3, "per_page": 50, "status": "failed"}


# ---------------------------------------------------------------------------
# 28: job_result flags say how the run ended and what was cut


class _ResultSource:
    def __init__(self, status: str, *, more: bool = False, events: int = 0) -> None:
        self.status = status
        self.more = more
        self.events = events
        self.calls: dict[str, dict] = {}

    async def get_job_status(self, job_id, *, scope, include_workflow_result=False):
        return {"id": job_id, "status": self.status}

    def list_messages(self, job_id, **kwargs):
        self.calls["messages"] = kwargs
        return {"messages": [{"summary": "last"}], "has_more": self.more}

    def list_logs(self, job_id, **kwargs):
        self.calls["logs"] = kwargs
        return {"logs": [], "has_more": self.more}

    def list_events(self, job_id, **kwargs):
        self.calls["events"] = kwargs
        return {"events": [{"id": str(index)} for index in range(self.events)]}


def _result(source: _ResultSource) -> dict:
    return asyncio.run(
        build_job_result_bundle(job_query_service=source, job_id="job-1", scope=object())
    )


@pytest.mark.parametrize(
    ("status", "completed", "terminal", "outcome"),
    [
        ("completed", True, True, "completed"),
        ("failed", False, True, "failed"),
        ("cancelled", False, True, "cancelled"),
        ("max_iterations", False, True, "failed"),
        ("triage_needed", False, True, "failed"),
        ("awaiting_timer", False, False, "waiting"),
        ("awaiting_child", False, False, "waiting"),
        ("awaiting_approval", False, False, "awaiting_approval"),
        ("in_progress", False, False, "running"),
    ],
)
def test_job_result_completed_means_success_and_terminal_is_separate(
    status, completed, terminal, outcome
):
    result = _result(_ResultSource(status))

    assert (result["completed"], result["terminal"], result["outcome"]) == (
        completed,
        terminal,
        outcome,
    )


def test_job_result_reads_the_newest_window_and_flags_what_was_cut():
    source = _ResultSource("completed", more=True, events=201)

    result = _result(source)

    assert source.calls["messages"]["newest"] is True
    assert source.calls["logs"]["newest"] is True
    assert source.calls["events"]["limit"] == 201
    assert result["truncated"] == {"messages": True, "logs": True, "events": True}
    assert len(result["raw"]["events"]) == 200


def test_job_result_complete_lists_are_not_flagged():
    result = _result(_ResultSource("completed", events=200))

    assert result["truncated"] == {"messages": False, "logs": False, "events": False}


# ---------------------------------------------------------------------------
# 29: restart refuses a run that is still active or waiting


def _restart_service(status: str) -> tuple[JobCommandService, MagicMock]:
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": 15,
        "status": status,
        "task_id": "task-15",
        "task_summary": "Import stock",
        "project_id": "3",
        "meta": {"replay_snapshot": {"artifact_id": "artifact-a"}},
    }
    bl.get_project.return_value = {"id": 3, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
    enqueue_task = MagicMock(return_value=88)
    service = JobCommandService(
        get_logger=lambda: bl,
        build_rerun_workflow_selection=MagicMock(return_value=({}, {})),
        load_replay_snapshot=MagicMock(return_value={"workflow_id": "wf"}),
        enqueue_task=enqueue_task,
        get_project_run_config=MagicMock(return_value={"project_name": "alpha"}),
    )
    return service, enqueue_task


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "queued",
        "resume_pending",
        "in_progress",
        "awaiting_timer",
        "awaiting_child",
        "awaiting_approval",
    ],
)
def test_restart_refuses_an_active_or_waiting_run(status):
    service, enqueue_task = _restart_service(status)

    with pytest.raises(ServiceConflictError) as raised:
        service.restart_job(15)

    assert raised.value.payload["code"] == "job_restart_active"
    assert raised.value.payload["status"] == status
    enqueue_task.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "max_iterations"])
def test_restart_of_a_finished_run_enqueues_a_new_run(status):
    service, enqueue_task = _restart_service(status)

    assert service.restart_job(15).new_job_id == "88"
    enqueue_task.assert_called_once()


def test_restart_route_answers_a_live_run_with_a_conflict():
    service, _enqueue = _restart_service("awaiting_child")

    response = _jobs_control_client(service).post("/api/jobs/15/restart")

    assert response.status_code == 409
    assert response.json()["code"] == "job_restart_active"


# ---------------------------------------------------------------------------
# 30: the cancel reason and the acknowledging actor reach the audit trail


class _RecordingJobCommands:
    def __init__(self) -> None:
        self.cancelled: list[dict] = []
        self.acknowledged: list[JobAcknowledgeInput] = []

    def cancel_job(self, job_id, *, actor, comment, scope):
        self.cancelled.append({"job_id": job_id, "actor": actor, "comment": comment})
        return SimpleNamespace(cancelled=True)

    def acknowledge_job(self, job_id, command, *, scope):
        self.acknowledged.append(command)
        return SimpleNamespace(status="acknowledged", job_id=job_id)


def test_cancel_route_passes_the_body_comment_and_keeps_the_default_without_one():
    commands = _RecordingJobCommands()
    client = _jobs_control_client(commands)

    assert client.post("/api/jobs/7/cancel", json={"comment": "Supplier feed is wrong"}).json() == {
        "cancelled": True,
        "job_id": "7",
    }
    client.post("/api/jobs/7/cancel")
    client.post("/api/jobs/7/cancel", json={})

    assert [row["comment"] for row in commands.cancelled] == [
        "Supplier feed is wrong",
        "Cancelled by user",
        "Cancelled by user",
    ]


def test_cancel_route_bounds_the_comment():
    response = _jobs_control_client(_RecordingJobCommands()).post(
        "/api/jobs/7/cancel", json={"comment": "x" * 2001}
    )

    assert response.status_code == 422


def test_acknowledge_route_keeps_the_authenticated_actor_and_reports_the_named_one():
    commands = _RecordingJobCommands()
    client = _jobs_control_client(commands)

    assert client.post("/api/jobs/7/acknowledge", json={"actor": "ops-agent"}).status_code == 200
    assert client.post("/api/jobs/7/acknowledge").status_code == 200

    assert [(row.actor, row.reported_actor) for row in commands.acknowledged] == [
        ("system", "ops-agent"),
        ("system", ""),
    ]


def _lifecycle_bl(status: str = "failed") -> MagicMock:
    bl = MagicMock()
    bl.get_job.return_value = {"id": 41, "project_id": "1", "status": status}
    bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
    bl.acknowledge_job.return_value = True
    return bl


def test_acknowledge_journals_the_authenticated_and_the_reported_actor():
    bl = _lifecycle_bl()
    journal = MagicMock()
    service = JobCommandService(get_logger=lambda: bl, append_runtime_journal=journal)

    service.acknowledge_job(41, JobAcknowledgeInput(actor="alice", reported_actor="ops-agent"))

    bl.acknowledge_job.assert_called_once_with("41", "alice")
    journal.assert_called_once_with(
        bl,
        "41",
        event="manual_acknowledge",
        reason="Acknowledged by operator",
        detail={"acknowledged_by": "alice", "reported_actor": "ops-agent"},
    )


def test_cancel_journals_who_cancelled_and_why():
    bl = _lifecycle_bl("in_progress")
    journal = MagicMock()
    service = JobCommandService(get_logger=lambda: bl, append_runtime_journal=journal)

    with (
        patch(
            "core.infrastructure.runtime.subprocess_lifecycle.terminalize_running_worker_subprocesses",
            return_value=0,
        ),
        patch("core.application.jobs.terminal_persistence.TerminalPersistenceService") as terminal,
    ):
        service.cancel_job(41, actor="alice", comment="Supplier feed is wrong")

    assert terminal.return_value.complete_failure.call_args.kwargs["error_message"] == (
        "Supplier feed is wrong"
    )
    journal.assert_called_once_with(
        bl,
        "41",
        event="manual_cancel",
        reason="Supplier feed is wrong",
        detail={
            "cancelled_by": "alice",
            "comment": "Supplier feed is wrong",
            "previous_status": "in_progress",
        },
    )


def test_a_journal_failure_does_not_undo_the_acknowledgement():
    bl = _lifecycle_bl()
    service = JobCommandService(
        get_logger=lambda: bl,
        append_runtime_journal=MagicMock(side_effect=RuntimeError("journal down")),
    )

    assert service.acknowledge_job(41, JobAcknowledgeInput(actor="alice")).status == (
        "acknowledged"
    )


def test_mcp_cancel_and_acknowledge_send_the_reason_and_actor(mcp_client):
    from core.entrypoints.mcp.server_job_tools import job_acknowledge, job_cancel

    mcp_client.responses.extend([{"cancelled": True}, {"status": "acknowledged"}, {}])
    job_cancel(job_id="7", comment="Supplier feed is wrong")
    job_acknowledge(job_id="7", actor="ops-agent")
    job_acknowledge(job_id="8")

    assert [request["json_body"] for request in mcp_client.requests] == [
        {"comment": "Supplier feed is wrong"},
        {"actor": "ops-agent"},
        None,
    ]


# ---------------------------------------------------------------------------
# 31a: static validation states that execution was not verified


def _validation_service(workflow_id: str) -> WorkflowOperatorService:
    workflow = {"workflow_id": workflow_id, "display_name": "Draft", "enabled": False}
    db = SimpleNamespace(
        get_workflow=lambda requested: workflow if requested == workflow_id else None
    )
    return WorkflowOperatorService(
        get_logger=lambda: db,
        project_query_service=None,
        registry_manager_provider=lambda: SimpleNamespace(
            db_store=db, workflows=SimpleNamespace(get_workflow=lambda _workflow_id: None)
        ),
        workflow_availability_service=SimpleNamespace(
            get_workflow_availability=lambda _workflow_id, account_id, project_id=None: {
                "availability_status": "available",
                "new_runs_blocked": False,
            }
        ),
    )


def test_workflow_validate_states_that_execution_was_not_verified():
    from core.application.workflows.authoring.drafts import UNPERFORMED_CHECKS

    payload = (
        _validation_service("draft").workflow_validate("draft", require_available=False).dump()
    )

    assert payload["execution_verified"] is False
    assert payload["unperformed_checks"] == list(UNPERFORMED_CHECKS)


# ---------------------------------------------------------------------------
# 31b: the approvals queue is bounded and paged


def _approvals_bl(count: int) -> MagicMock:
    bl = MagicMock()
    bl.get_awaiting_approval_jobs.return_value = [
        {"id": str(index), "account_id": "account-42", "tenant_id": DEFAULT_TENANT_ID}
        for index in range(count)
    ]
    return bl


def test_approvals_page_counts_the_whole_scoped_queue():
    service = _query_service(_approvals_bl(30))
    scope = ScopeContext(account_id="account-42")

    first = service.page_awaiting_approval_jobs(page=1, per_page=25, scope=scope)
    second = service.page_awaiting_approval_jobs(page=2, per_page=25, scope=scope)

    assert (first.total, first.total_pages, first.has_more, len(first.jobs)) == (30, 2, True, 25)
    assert (second.has_more, [job.id for job in second.jobs]) == (
        False,
        [str(index) for index in range(25, 30)],
    )


def test_approvals_page_size_is_bounded():
    page = _query_service(_approvals_bl(3)).page_awaiting_approval_jobs(
        per_page=10_000, scope=ScopeContext(account_id="account-42")
    )

    assert page.per_page == 200


def test_approvals_route_pages_on_request_and_keeps_the_plain_list_otherwise():
    client = _jobs_client(_query_service(_approvals_bl(3)))

    plain = client.get("/api/jobs/awaiting-approval")
    paged = client.get("/api/jobs/awaiting-approval", params={"per_page": 2})

    assert [row["id"] for row in plain.json()] == ["0", "1", "2"]
    assert paged.json()["total"] == 3
    assert paged.json()["has_more"] is True
    assert [row["id"] for row in paged.json()["jobs"]] == ["0", "1"]
    assert client.get("/api/jobs/awaiting-approval", params={"per_page": 201}).status_code == 422


def test_mcp_pending_approvals_asks_for_one_page(mcp_client):
    from core.entrypoints.mcp.server_job_tools import job_approvals_pending

    mcp_client.responses.append({"jobs": [], "total": 0, "has_more": False})
    job_approvals_pending(page=2, per_page=50)

    assert mcp_client.requests[0]["path"] == "/api/jobs/awaiting-approval"
    assert mcp_client.requests[0]["params"] == {"page": 2, "per_page": 50}
