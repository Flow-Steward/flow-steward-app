from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import CommandDispatchInput
from core.application.management.command_services import CommandDispatchService
from core.application.management.errors import ServiceUnavailableError
from core.application.management.scope import normalize_scope

_SCOPE = normalize_scope(
    account_id=DEFAULT_ACCOUNT_ID,
    principal_type="service_account",
)


class _DurableJobStore:
    def __init__(self) -> None:
        self.jobs = {
            "job-1": {
                "id": "job-1",
                "project_id": "project-1",
                "account_id": DEFAULT_ACCOUNT_ID,
                "thread_id": "thread-1",
                "status": "in_progress",
            }
        }
        self.audits: dict[str, dict] = {}
        self.audit_events: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str]] = []

    def get_job(self, job_id: str):
        row = self.jobs.get(str(job_id))
        return dict(row) if row else None

    @staticmethod
    def get_project(project_id: str):
        if str(project_id) != "project-1":
            return None
        return {"id": "project-1", "account_id": DEFAULT_ACCOUNT_ID}

    def update_job(self, job_id: str, *, status: str):
        row = self.jobs.get(str(job_id))
        if row is None:
            return None
        row["status"] = status
        self.transitions.append((str(job_id), status))
        return dict(row)

    def log_command_execution(self, **fields):
        assert "source" not in fields
        assert "correlation_id" not in fields
        self.audit_events.append((fields["command_id"], fields["status"]))
        command_id = str(fields["command_id"])
        self.audits[command_id] = dict(fields)
        return dict(fields)

    def update_command_execution(self, *, command_id: str, status: str, result, error_text):
        row = self.audits.get(str(command_id))
        if row is None:
            return None
        self.audit_events.append((command_id, status))
        row.update(status=status, result=dict(result or {}), error_text=error_text)
        return dict(row)


class _ProjectCommands:
    def __init__(self) -> None:
        self.requests = []
        self.options = []

    def enqueue_manual_task(self, command, *, scope, trusted_internal, allow_implicit_project):
        self.requests.append(command)
        self.options.append(
            {
                "scope": scope,
                "trusted_internal": trusted_internal,
                "allow_implicit_project": allow_implicit_project,
            }
        )
        assert scope.account_id == DEFAULT_ACCOUNT_ID
        assert trusted_internal is True
        return SimpleNamespace(job_id="job-created", status="pending")


class _JobCommands:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.retried: list[str] = []
        self.scopes = []
        self.retry_requests = []

    def cancel_job(self, job_id: str, *, scope):
        self.cancelled.append(job_id)
        self.scopes.append(scope)
        return SimpleNamespace(job_id=job_id, status="cancelled")

    def retry_interrupted_step(self, job_id: str, command, *, scope):
        self.retried.append(job_id)
        self.scopes.append(scope)
        self.retry_requests.append(command)
        return SimpleNamespace(job_id=job_id, status="pending", new_job_id="job-retry")


class _ApprovalCommands:
    def __init__(self) -> None:
        self.decisions = []

    def resume_task(self, *, thread_id: str, scope, decision, idempotency_key: str = ""):
        self.decisions.append((thread_id, decision, idempotency_key))
        return SimpleNamespace(
            thread_id=thread_id,
            job_id="job-1",
            approval_id=decision.approval_id,
            approved=decision.approved,
            status="in_progress",
        )


def _service(
    store: _DurableJobStore,
    *,
    projects: _ProjectCommands | None = None,
    jobs: _JobCommands | None = None,
    approvals: _ApprovalCommands | None = None,
) -> CommandDispatchService:
    return CommandDispatchService(
        get_logger=lambda: store,
        project_command_service_factory=(lambda: projects) if projects is not None else None,
        job_command_service_factory=(lambda: jobs) if jobs is not None else None,
        approval_command_service_factory=(lambda: approvals) if approvals is not None else None,
    )


def test_start_workflow_creates_exactly_one_job_and_finalizes_one_audit() -> None:
    store = _DurableJobStore()
    projects = _ProjectCommands()

    result = _service(store, projects=projects).dispatch(
        CommandDispatchInput(
            command_type="start_workflow",
            correlation_id="start-request-1",
            source="api",
            target={"workflow_id": "weekly_digest", "project_id": "project-1"},
            payload={
                "task_summary": "repair the release",
                "inputs": {"owner": "team"},
                "extra_state": {"ticket": "T-1"},
                "job_meta": {"origin": "release"},
            },
        ),
        scope=_SCOPE,
    )

    assert result.status == "executed"
    assert result.result == {"job_id": "job-created", "action": "started"}
    assert len(projects.requests) == 1
    task = projects.requests[0]
    assert task.project_id == "project-1"
    assert task.workflow_id == "weekly_digest"
    assert task.task_id == "start-request-1"
    assert task.task_summary == "repair the release"
    assert task.task_source == "command_dispatch"
    assert task.entry_path == "manual_api"
    assert task.inputs == {"owner": "team"}
    assert task.extra_state == {"ticket": "T-1"}
    assert task.job_meta == {"origin": "release"}
    assert projects.options == [
        {"scope": _SCOPE, "trusted_internal": True, "allow_implicit_project": False}
    ]
    assert store.audit_events == [(result.command_id, "accepted"), (result.command_id, "executed")]
    assert len(store.audits) == 1
    audit = next(iter(store.audits.values()))
    assert audit["status"] == "executed"
    assert audit["result"]["job_id"] == "job-created"


def test_missing_start_port_fails_closed_and_finalizes_the_accepted_audit() -> None:
    store = _DurableJobStore()
    service = _service(store)

    _raises_input_146_1 = CommandDispatchInput(
        command_type="start_workflow",
        target={"workflow_id": "weekly_digest", "project_id": "project-1"},
    )
    with pytest.raises(ServiceUnavailableError, match=r"[Ww]orkflow start port"):
        service.dispatch(_raises_input_146_1, scope=_SCOPE)

    assert len(store.audits) == 1
    audit = next(iter(store.audits.values()))
    assert audit["status"] == "failed"
    assert "workflow start port" in str(audit["error_text"]).lower()


def test_job_lifecycle_and_approval_commands_use_durable_production_ports() -> None:
    store = _DurableJobStore()
    jobs = _JobCommands()
    approvals = _ApprovalCommands()
    service = _service(store, jobs=jobs, approvals=approvals)

    paused = service.dispatch(
        CommandDispatchInput(command_type="pause_job", target={"job_id": "job-1"}),
        scope=_SCOPE,
    )
    assert paused.result["action"] == "paused"
    assert store.get_job("job-1")["status"] == "paused"

    resumed = service.dispatch(
        CommandDispatchInput(command_type="resume_job", target={"job_id": "job-1"}),
        scope=_SCOPE,
    )
    assert resumed.result["action"] == "resumed"
    assert store.get_job("job-1")["status"] == "in_progress"

    cancelled = service.dispatch(
        CommandDispatchInput(command_type="cancel_job", target={"job_id": "job-1"}),
        scope=_SCOPE,
    )
    assert cancelled.result["action"] == "cancelled"
    assert jobs.cancelled == ["job-1"]

    retried = service.dispatch(
        CommandDispatchInput(
            command_type="retry_step",
            target={"job_id": "job-1", "step_id": "step-1"},
        ),
        scope=_SCOPE,
    )
    assert retried.result["action"] == "retried"
    assert retried.result["new_job_id"] == "job-retry"
    assert jobs.retried == ["job-1"]
    assert jobs.scopes == [_SCOPE, _SCOPE]
    assert jobs.retry_requests[0].requested_by == _SCOPE.actor_name

    approved = service.dispatch(
        CommandDispatchInput(
            command_type="resolve_approval",
            correlation_id="approval-request-1",
            target={"job_id": "job-1", "approval_id": "approval-1"},
            payload={"approved": True, "action": "approve", "comment": "ship it"},
        ),
        scope=_SCOPE,
    )
    assert approved.result["action"] == "approval_resolved"
    assert approved.result["approval_id"] == "approval-1"
    assert approvals.decisions[0][0] == "thread-1"
    decision = approvals.decisions[0][1]
    assert decision.approval_id == "approval-1"
    assert decision.approved is True
    assert decision.action == "approve"
    assert decision.comment == "ship it"
    assert approvals.decisions[0][2] == "approval-request-1"
    assert store.transitions == [("job-1", "paused"), ("job-1", "in_progress")]
    assert len(store.audits) == 5
    assert all(row["status"] == "executed" for row in store.audits.values())


class _OldAuditStore:
    def __init__(self) -> None:
        self.audit_calls = []
        self.mutations = []

    def record_command_audit(self, **fields):
        self.audit_calls.append(fields)
        return fields

    def update_command_audit_status(self, **fields):
        self.audit_calls.append(fields)
        return fields

    def get_job(self, job_id):
        return {"id": job_id, "account_id": DEFAULT_ACCOUNT_ID}

    def update_job_status(self, job_id, status):
        self.mutations.append((job_id, status))


class _OldOperationStore(_DurableJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.old_calls = []

    def update_job_status(self, job_id, status):
        self.old_calls.append(("status", job_id, status))
        self.jobs[job_id]["status"] = status

    def enqueue_manual_job(self, **fields):
        self.old_calls.append(("enqueue", fields))
        return {"id": "old-job"}

    def retry_step(self, **fields):
        self.old_calls.append(("retry", fields))


@pytest.mark.parametrize("store_factory", [_OldAuditStore, MagicMock])
def test_old_or_manufactured_audit_ports_cannot_accept_a_command(store_factory) -> None:
    store = store_factory()
    service = CommandDispatchService(get_logger=lambda: store)
    command = CommandDispatchInput(command_type="pause_job", target={"job_id": "job-1"})
    with pytest.raises(ServiceUnavailableError, match="audit persistence port"):
        service.dispatch(command, scope=_SCOPE)
    if isinstance(store, _OldAuditStore):
        assert store.audit_calls == []
        assert store.mutations == []
    else:
        store.record_command_audit.assert_not_called()
        store.update_job_status.assert_not_called()


@pytest.mark.parametrize(
    "command_type",
    ["start_workflow", "enqueue_followup_job", "retry_step", "cancel_job", "resolve_approval"],
)
def test_missing_services_reject_old_operations_and_finalize_failed_audit(command_type) -> None:
    store = _OldOperationStore()
    before_job = dict(store.jobs["job-1"])
    service = _service(store)
    command = CommandDispatchInput(
        command_type=command_type,
        target={
            "job_id": "job-1",
            "workflow_id": "weekly_digest",
            "project_id": "project-1",
            "parent_job_id": "job-1",
            "step_id": "step-1",
            "approval_id": "approval-1",
        },
        payload={"approved": True},
    )
    with pytest.raises(ServiceUnavailableError) as error:
        service.dispatch(command, scope=_SCOPE)
    assert error.value.status_code == 503
    assert store.old_calls == []
    assert store.transitions == []
    assert store.jobs["job-1"] == before_job
    assert len(store.audits) == 1
    audit = next(iter(store.audits.values()))
    assert audit["status"] == "failed"
    assert audit["result"] == {}
    assert audit["error_text"]
    assert store.audit_events == [
        (audit["command_id"], "accepted"),
        (audit["command_id"], "failed"),
    ]


@pytest.mark.parametrize("command_type", ["pause_job", "resume_job", "request_approval"])
def test_direct_transitions_require_verified_update_job_instead_of_old_status_port(command_type):
    store = _OldOperationStore()
    store.update_job = None
    before_job = dict(store.jobs["job-1"])
    service = _service(store)
    command = CommandDispatchInput(command_type=command_type, target={"job_id": "job-1"})
    with pytest.raises(ServiceUnavailableError, match="Durable job transition port"):
        service.dispatch(command, scope=_SCOPE)
    assert store.old_calls == []
    assert store.jobs["job-1"] == before_job
    assert next(iter(store.audits.values()))["status"] == "failed"


def test_job_transition_that_does_not_persist_finalizes_failed_audit() -> None:
    store = _DurableJobStore()
    store.update_job = lambda job_id, **fields: dict(store.jobs[job_id])
    service = _service(store)
    command = CommandDispatchInput(command_type="pause_job", target={"job_id": "job-1"})
    with pytest.raises(ServiceUnavailableError, match="could not be verified"):
        service.dispatch(command, scope=_SCOPE)
    assert store.jobs["job-1"]["status"] == "in_progress"
    assert next(iter(store.audits.values()))["status"] == "failed"


@pytest.mark.parametrize(
    ("command_type", "factory_name"),
    [
        ("start_workflow", "project_command_service_factory"),
        ("enqueue_followup_job", "project_command_service_factory"),
        ("retry_step", "job_command_service_factory"),
        ("cancel_job", "job_command_service_factory"),
        ("resolve_approval", "approval_command_service_factory"),
    ],
)
@pytest.mark.parametrize("missing_service", [None, object()])
def test_factory_without_required_service_port_fails_closed(
    command_type, factory_name, missing_service
):
    store = _DurableJobStore()
    service = CommandDispatchService(
        get_logger=lambda: store,
        **{factory_name: lambda: missing_service},
    )
    command = CommandDispatchInput(
        command_type=command_type,
        target={
            "job_id": "job-1",
            "project_id": "project-1",
            "workflow_id": "weekly_digest",
            "approval_id": "approval-1",
        },
        payload={"approved": True},
    )
    with pytest.raises(ServiceUnavailableError) as error:
        service.dispatch(command, scope=_SCOPE)
    assert error.value.status_code == 503
    assert store.transitions == []
    assert next(iter(store.audits.values()))["status"] == "failed"


def test_real_api_job_store_exposes_selectable_canonical_audit_ports() -> None:
    from core.infrastructure.db.platform_stores import PlatformJobStore

    store = object.__new__(PlatformJobStore)
    accepted_row = {"command_id": "cmd-real", "status": "accepted"}
    final_row = {"command_id": "cmd-real", "status": "executed"}
    with (
        patch.object(store, "log_command_execution", return_value=accepted_row) as accepted,
        patch.object(store, "update_command_execution", return_value=final_row) as final,
    ):
        accept_port, final_port = CommandDispatchService._audit_ports(store)
        assert (
            accept_port(command_id="cmd-real", source="api", correlation_id="request-1")
            == accepted_row
        )
        assert (
            final_port(command_id="cmd-real", status="executed", result={}, error_text=None)
            == final_row
        )
    accepted.assert_called_once_with(command_id="cmd-real")
    final.assert_called_once_with(
        command_id="cmd-real", status="executed", result={}, error_text=None
    )


@pytest.mark.parametrize("project_id", ["project-1", None])
def test_followup_uses_project_queue_and_preserves_parent_and_correlation(project_id):
    store = _DurableJobStore()
    projects = _ProjectCommands()
    result = _service(store, projects=projects).dispatch(
        CommandDispatchInput(
            command_type="enqueue_followup_job",
            correlation_id="followup-request-1",
            target={
                "workflow_id": "weekly_digest",
                "project_id": project_id,
                "parent_job_id": "job-1",
            },
            payload={"inputs": {"reason": "followup"}, "extra_state": {"attempt": 2}},
        ),
        scope=_SCOPE,
    )
    assert result.result == {"action": "enqueued", "job": {"id": "job-created"}}
    assert len(projects.requests) == 1
    task = projects.requests[0]
    assert task.project_id == project_id
    assert task.task_id == "followup-request-1"
    assert task.workflow_id == "weekly_digest"
    assert task.task_summary == "Followup from job job-1"
    assert task.job_meta == {"parent_job_id": "job-1"}
    assert task.inputs == {"reason": "followup"}
    assert task.extra_state == {"attempt": 2}
    assert task.task_source == "command_dispatch"
    assert task.entry_path == "manual_api"
    assert projects.options == [
        {"scope": _SCOPE, "trusted_internal": True, "allow_implicit_project": project_id is None}
    ]
    assert store.audit_events == [(result.command_id, "accepted"), (result.command_id, "executed")]


@pytest.mark.parametrize("port_name", ["log_command_execution", "update_command_execution"])
def test_incomplete_canonical_audit_pair_prevents_mutation(port_name):
    store = _DurableJobStore()
    setattr(store, port_name, None)
    service = _service(store)
    command = CommandDispatchInput(command_type="pause_job", target={"job_id": "job-1"})
    with pytest.raises(ServiceUnavailableError, match="audit persistence port"):
        service.dispatch(command, scope=_SCOPE)
    assert store.audits == {}
    assert store.transitions == []


@pytest.mark.parametrize("not_persisted", [None, False])
def test_audit_acceptance_must_persist_before_transition(not_persisted):
    store = _DurableJobStore()
    store.log_command_execution = lambda **fields: not_persisted
    service = _service(store)
    command = CommandDispatchInput(command_type="pause_job", target={"job_id": "job-1"})
    with pytest.raises(ServiceUnavailableError, match="acceptance did not persist"):
        service.dispatch(command, scope=_SCOPE)
    assert store.audits == {}
    assert store.transitions == []


@pytest.mark.parametrize("not_persisted", [None, False])
def test_audit_finalization_failure_cannot_report_execution_success(not_persisted):
    store = _DurableJobStore()
    store.update_command_execution = lambda **fields: not_persisted
    service = _service(store)
    command = CommandDispatchInput(command_type="pause_job", target={"job_id": "job-1"})
    with pytest.raises(ServiceUnavailableError, match="finalization did not persist"):
        service.dispatch(command, scope=_SCOPE)
    assert len(store.audits) == 1
    assert next(iter(store.audits.values()))["status"] == "accepted"
    assert store.transitions == [("job-1", "paused")]
