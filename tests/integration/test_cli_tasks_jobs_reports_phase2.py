"""Phase 2 task/job/report CLI migration regression tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.command_models import (
    ApprovalDecisionInput,
    ApprovalResumeAcceptedView,
    JobAcknowledgeInput,
    ProjectTaskEnqueueInput,
    ProjectTaskEnqueueResultView,
)
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.entrypoints.api.jobs_control import create_jobs_control_router
from core.entrypoints.api.reports import create_api_reports_router
from core.entrypoints.cli.platform.service_registry import ServiceRegistry

PROJECT_ALLOWED = "00000000-0000-0000-0000-000000000001"
PROJECT_BLOCKED = "00000000-0000-0000-0000-000000000002"


@dataclass
class _ProjectCommand:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    durable_jobs: list[dict[str, Any]] = field(default_factory=list)
    enforce_scope_membership: bool = False

    def enqueue_manual_task(
        self,
        command: ProjectTaskEnqueueInput,
        *,
        allow_implicit_project: bool = False,
        scope: Any = None,
    ) -> dict[str, Any]:
        if self.enforce_scope_membership:
            allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset()) or frozenset()
            project_id = str(command.project_id)
            if allowed_project_ids and project_id not in allowed_project_ids:
                raise ServiceForbiddenError(
                    "project access denied",
                    payload={
                        "error": "project_access_denied",
                        "project_id": project_id,
                        "allowed_project_ids": sorted(allowed_project_ids),
                    },
                )
        self.calls.append(
            (
                "enqueue_manual_task",
                {
                    "command": command.model_dump(mode="json"),
                    "allow_implicit_project": allow_implicit_project,
                    "scope": scope,
                },
            )
        )
        self.durable_jobs.append(
            {
                "project_id": str(command.project_id),
                "task_id": str(command.task_id),
                "workflow_id": str(command.workflow_id or ""),
                "summary": str(command.task_summary),
            }
        )
        return ProjectTaskEnqueueResultView(
            project_id=str(command.project_id),
            task_id=command.task_id,
            job_id="job_1",
            status="pending",
            queue=f"platform.project.{command.project_id}",
            file_count=0,
            project_created=False,
        )


@dataclass
class _ApprovalCommand:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def resume_task(
        self,
        *,
        thread_id: str,
        scope: Any,
        decision: ApprovalDecisionInput,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "resume_task",
                {
                    "thread_id": thread_id,
                    "scope": scope,
                    "decision": decision.model_dump(mode="json"),
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return ApprovalResumeAcceptedView(
            thread_id=thread_id,
            job_id="job_1",
            approval_id=decision.approval_id,
            approved=decision.approved,
            action=decision.action,
            comment=decision.comment,
            override_content=decision.override_content,
            actor="operator",
            status="in_progress",
        )


@dataclass
class _JobCommand:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def acknowledge_job(
        self, job_id: str, command: JobAcknowledgeInput, *, scope: Any = None
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "acknowledge_job",
                {"job_id": job_id, "command": command.model_dump(mode="json"), "scope": scope},
            )
        )
        return {"job_id": job_id, "status": "acknowledged"}

    def unacknowledge_job(self, job_id: str, *, scope: Any = None) -> dict[str, Any]:
        self.calls.append(("unacknowledge_job", {"job_id": job_id, "scope": scope}))
        return {"job_id": job_id, "status": "unacknowledged"}

    def cancel_job(
        self,
        job_id: str,
        *,
        actor: str = "operator",
        comment: str = "Cancelled by user",
        scope: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "cancel_job",
                {"job_id": job_id, "actor": actor, "comment": comment, "scope": scope},
            )
        )
        return {"job_id": job_id, "status": "cancelled", "cancelled": True}

    def restart_job(self, job_id: str, *, scope: Any = None) -> dict[str, Any]:
        self.calls.append(("restart_job", {"job_id": job_id, "scope": scope}))
        return {"job_id": job_id, "status": "pending", "new_job_id": "job_2", "enqueued": True}


@dataclass
class _JobQuery:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def list_jobs(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_jobs", kwargs))
        return {"jobs": [], "total": 0, "page": 1, "per_page": 25, "total_pages": 1}

    def get_job_detail_contract(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_job_detail_contract", {"job_id": job_id, **kwargs}))
        return {"id": job_id, "status": "awaiting_approval", "thread_id": "thread_1"}

    async def get_job_status(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_job_status", {"job_id": job_id, **kwargs}))
        return {"id": job_id, "status": "in_progress", "task_summary": "Run task"}

    def list_logs(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_logs", {"job_id": job_id, **kwargs}))
        return {"logs": [], "status": "in_progress"}

    def list_messages(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_messages", {"job_id": job_id, **kwargs}))
        return {"messages": [], "last_id": "", "has_more": False}

    def list_errors(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_errors", {"job_id": job_id, **kwargs}))
        return {"job_id": job_id, "errors": []}


@dataclass
class _ReportingService:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def report_generate(
        self,
        report_type: str,
        params: dict[str, Any],
        *,
        scope: Any,
        persist: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "report_generate",
                {
                    "report_type": report_type,
                    "params": params,
                    "scope": scope,
                    "persist": persist,
                    "session_id": session_id,
                },
            )
        )
        return {
            "id": "report_1" if persist else None,
            "report_type": report_type,
            "status": "generated",
            "params": params,
            "summary_text": "ok",
            "payload": {},
            "chart_payload": {},
            "directives": [],
            "created_at": "2026-07-17T00:00:00+00:00",
        }

    def report_get(self, report_id: str, *, scope: Any) -> dict[str, Any]:
        self.calls.append(("report_get", {"report_id": report_id, "scope": scope}))
        return {
            "id": report_id,
            "report_type": "activity_daily",
            "status": "generated",
            "params": {},
            "summary_text": "saved",
            "payload": {},
            "chart_payload": {},
            "directives": [],
            "created_at": "2026-07-17T00:00:00+00:00",
        }


@dataclass
class _HealthSnapshots:
    def collect(self, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "service": {"ok": True}, "database": {"ok": True}}


@dataclass
class _Runtime:
    project_command_service: _ProjectCommand = field(default_factory=_ProjectCommand)
    approval_command_service: _ApprovalCommand = field(default_factory=_ApprovalCommand)
    job_command_service: _JobCommand = field(default_factory=_JobCommand)
    job_query_service: _JobQuery = field(default_factory=_JobQuery)
    reporting_service: _ReportingService = field(default_factory=_ReportingService)
    ops_health_snapshot_service: _HealthSnapshots = field(default_factory=_HealthSnapshots)
    project_query_service: object = object()
    approval_query_service: object = object()
    workflow_operator_service: object = object()
    user_query_service: object = object()
    user_command_service: object = object()
    extension_query_service: object = object()
    extension_binding_service: object = object()
    extension_scaffold_service: object = object()


def _cli_app(runtime: _Runtime) -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(ServiceRegistry(runtime), SimpleNamespace())


def _scope(_request: Any = None, **kwargs: Any) -> ScopeContext:
    return ScopeContext(
        account_id="acc_default",
        allowed_project_ids=frozenset({str(kwargs.get("project_id") or "proj_1")}),
    )


def _limited_scope(_request: Any = None, **kwargs: Any) -> ScopeContext:
    return ScopeContext(
        account_id="acc_default",
        project_id=kwargs.get("project_id"),
        workflow_id=kwargs.get("workflow_id"),
        actor_id="limited-operator",
        principal_type="operator",
        allowed_project_ids=frozenset({PROJECT_ALLOWED}),
    )


def _assert_cli_scope(
    value: object, *, project_id: str | None = None, workflow_id: str | None = None
) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "system_actor"
    assert value.project_id == project_id
    assert value.workflow_id == workflow_id
    if project_id:
        assert value.allowed_project_ids == frozenset({project_id})
    if workflow_id:
        assert value.allowed_workflow_ids == frozenset({workflow_id})
    return value


def _service_error_response(exc: Exception, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=int(getattr(exc, "status_code", 500)))


def _api_error_response(exc: Exception, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


def test_tasks_jobs_reports_phase2_tree_contains_only_approved_leaves() -> None:
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    tree: dict[str, list[str]] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        if len(spec.path) == 2:
            tree.setdefault(spec.path[0], []).append(spec.path[1])

    assert "tasks" not in tree
    assert tree["jobs"] == [
        "list",
        "show",
        "status",
        "logs",
        "messages",
        "errors",
        "approve",
        "reject",
        "ack",
        "unack",
        "cancel",
        "restart",
        "health-snapshot",
    ]
    assert tree["reports"] == ["generate", "get", "diagnostics"]


def test_legacy_task_job_report_aliases_are_not_public_routes() -> None:
    runtime = _Runtime()
    app = _cli_app(runtime)

    for args in (
        ["tooling", "tasks", "submit"],
        ["tooling", "jobs", "list"],
        ["tooling", "reports", "generate"],
        ["test", "reports", "get"],
    ):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
    assert runtime.project_command_service.calls == []
    assert runtime.job_command_service.calls == []
    assert runtime.reporting_service.calls == []


def test_cli_and_api_approval_delegate_to_same_in_process_service() -> None:
    runtime = _Runtime()
    cli = CliRunner().invoke(
        _cli_app(runtime),
        [
            "jobs",
            "approve",
            "thread_1",
            "--approval-id",
            "approval_1",
            "--json",
            "--comment",
            "ok",
            "--confirm",
        ],
        input="y\n",
    )
    assert cli.exit_code == 0, cli.output

    app = FastAPI()
    app.include_router(
        create_jobs_control_router(
            project_command_service=lambda: runtime.project_command_service,
            approval_command_service=lambda: runtime.approval_command_service,
            job_command_service=lambda: runtime.job_command_service,
            request_scope=_scope,
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
            logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
        )
    )
    api = TestClient(app).post(
        "/api/approvals/thread_1",
        json={"approved": True, "comment": "ok", "approval_id": "approval_1"},
        headers={"Idempotency-Key": "approval-transport-parity"},
    )
    assert api.status_code == 202, api.text

    assert [name for name, _payload in runtime.approval_command_service.calls] == [
        "resume_task",
        "resume_task",
    ]
    cli_call, api_call = [payload for _name, payload in runtime.approval_command_service.calls]
    assert cli_call["thread_id"] == api_call["thread_id"] == "thread_1"
    assert cli_call["decision"]["approved"] is True
    assert cli_call["decision"]["approval_id"] == api_call["decision"]["approval_id"]
    _assert_cli_scope(cli_call["scope"])
    assert isinstance(api_call["scope"], ScopeContext)


def test_cli_and_api_report_generation_delegate_to_same_reporting_service() -> None:
    runtime = _Runtime()
    cli = CliRunner().invoke(
        _cli_app(runtime),
        [
            "reports",
            "generate",
            "activity_daily",
            "--json",
            "--params",
            '{"project_id": "proj_1"}',
            "--no-persist",
            "--confirm",
        ],
        input="y\n",
    )
    assert cli.exit_code == 0, cli.output

    app = FastAPI()
    app.include_router(
        create_api_reports_router(
            reporting_service=lambda: runtime.reporting_service,
            request_scope=_scope,
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
        )
    )
    api = TestClient(app).post(
        "/api/reports/generate",
        json={
            "report_type": "activity_daily",
            "params": {"project_id": "proj_1"},
            "persist": False,
        },
    )
    assert api.status_code == 200, api.text

    assert [name for name, _payload in runtime.reporting_service.calls] == [
        "report_generate",
        "report_generate",
    ]
    cli_call, api_call = [payload for _name, payload in runtime.reporting_service.calls]
    assert cli_call["params"] == api_call["params"] == {"project_id": "proj_1"}
    _assert_cli_scope(cli_call["scope"])
    assert isinstance(api_call["scope"], ScopeContext)
