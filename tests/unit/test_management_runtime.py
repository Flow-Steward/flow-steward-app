from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.management import management_runtime


def test_cli_job_details_preserve_cancelled_abort_history_without_comment() -> None:
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.entrypoints.cli.platform.service_bindings.jobs import jobs_show

    approval = {
        "approval_id": "approval-1",
        "stage": "check",
        "action": "abort",
        "approved": False,
        "comment": "",
        "question": "The rework budget is exhausted.",
        "actor": "operator",
        "actor_type": "admin",
        "timestamp": "2026-10-15T04:47:41.569341+00:00",
    }
    store = MagicMock(
        spec=[
            "get_job",
            "get_job_cost_summary",
            "get_job_cost_breakdown",
            "get_logs",
            "get_events",
            "get_agent_messages",
            "list_job_errors",
        ]
    )
    store.get_job.return_value = {
        "id": "job-1",
        "status": "cancelled",
        "task_summary": "Validate output",
        "account_id": DEFAULT_ACCOUNT_ID,
        "project_id": None,
        "meta": {"approvals": [approval]},
    }
    store.get_job_cost_summary.return_value = {}
    store.get_job_cost_breakdown.return_value = []
    store.get_logs.return_value = []
    store.get_events.return_value = []
    store.get_agent_messages.return_value = []
    store.list_job_errors.return_value = []
    runtime = SimpleNamespace(
        job_query_service=management_runtime.create_job_query_service(get_logger=lambda: store)
    )

    payload = asyncio.run(jobs_show(runtime, {"job_id": "job-1", "include_details": True})).dump()

    assert payload["status"] == "cancelled"
    assert payload["approval_history"] == [approval]
    decisions = payload["sections"]["previous_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["kind"] == "approval"
    assert decisions[0]["title"] == "check decision"
    assert decisions[0]["status"] == "Abort"


def _diagnostic_store(*, error_reader=True):
    methods = ["get_job", "get_job_cost_summary"]
    if error_reader:
        methods.append("list_job_errors")
    store = MagicMock(spec=methods)
    store.get_job.return_value = {
        "id": "job-1",
        "status": "failed",
        "task_summary": "Validate output",
        "account_id": "00000000-0000-0000-0000-000000000002",
        "project_id": None,
    }
    store.get_job_cost_summary.return_value = {}
    return store


@pytest.mark.parametrize("reader_state", ["missing", "empty", "failed"])
def test_management_status_remains_available_without_persisted_diagnostics(reader_state):
    store = _diagnostic_store(error_reader=reader_state != "missing")
    if reader_state == "empty":
        store.list_job_errors.return_value = []
    elif reader_state == "failed":
        store.list_job_errors.side_effect = RuntimeError("Diagnostic store unavailable")
    service = management_runtime.create_job_query_service(get_logger=lambda: store)

    payload = asyncio.run(service.get_job_status("job-1")).dump()

    assert payload["status"] == "failed"
    assert payload["failure_summary"]["kind"] == "runtime_error"
    assert payload["failure_summary"]["raw_diagnostics_available"] is False


def test_management_status_bounds_and_redacts_persisted_diagnostic_header():
    store = _diagnostic_store()
    token = "sk-" + "Z" * 48
    message = f"Workflow contract failed with api_key={token}. " + "x" * 5000
    store.list_job_errors.return_value = [
        {
            "id": "error-1",
            "source": "system",
            "category": "contract",
            "retryable": False,
            "error_message": message,
            "response_excerpt": message,
            "traceback": "private stack details",
            "context_jsonb": {"private": "raw diagnostic context"},
        }
    ]
    service = management_runtime.create_job_query_service(get_logger=lambda: store)

    payload = asyncio.run(service.get_job_status("job-1")).dump()

    summary = payload["failure_summary"]
    assert summary["kind"] == "workflow_guardrail"
    assert summary["retryable"] is False
    assert token not in str(payload)
    header = payload["runtime"]["last_structured_error"]
    assert header["id"] == "error-1"
    assert len(header["error_message"]) <= 2000
    assert len(header["response_excerpt"]) <= 2000
    assert "traceback" not in header
    assert "context_jsonb" not in header
    store.list_job_errors.assert_called_once_with("job-1", limit=1)


def test_management_status_authorizes_before_reading_diagnostics():
    from core.application.management.errors import ServiceForbiddenError
    from core.application.management.scope import ScopeContext

    store = _diagnostic_store()
    service = management_runtime.create_job_query_service(get_logger=lambda: store)
    foreign_scope = ScopeContext(account_id="11111111-1111-4111-8111-111111111111")

    status = service.get_job_status("job-1", scope=foreign_scope)
    with pytest.raises(ServiceForbiddenError):
        asyncio.run(status)

    store.list_job_errors.assert_not_called()


def test_management_status_preserves_provider_classification_and_diagnostic_id():
    store = _diagnostic_store()
    store.list_job_errors.return_value = [
        {
            "id": "error-1",
            "source": "provider",
            "category": "provider",
            "http_status": 429,
            "request_id": "request-safe-1",
            "retryable": True,
            "error_message": "Provider rate limit exceeded",
        }
    ]
    service = management_runtime.create_job_query_service(get_logger=lambda: store)

    payload = asyncio.run(service.get_job_status("job-1")).dump()

    summary = payload["failure_summary"]
    assert summary["kind"] == "provider_rate_limit"
    assert summary["retryable"] is True
    assert summary["diagnostic_ids"]["request_id"] == "request-safe-1"


def test_management_status_uses_timeout_fallback_without_second_diagnostic_read(monkeypatch):
    from unittest.mock import AsyncMock

    from core.infrastructure.runtime import serve_request_runtime

    store = _diagnostic_store()
    # Model an executor that cannot finish before the established request deadline.
    monkeypatch.setattr(
        serve_request_runtime.asyncio, "to_thread", AsyncMock(side_effect=TimeoutError)
    )
    service = management_runtime.create_job_query_service(get_logger=lambda: store)

    payload = asyncio.run(service.get_job_status("job-1")).dump()

    assert payload["status"] == "failed"
    assert payload["failure_summary"]["raw_diagnostics_available"] is False
    store.list_job_errors.assert_not_called()


def test_job_command_service_uses_store_project_run_config() -> None:
    job_store = MagicMock(spec=[])
    project_store = MagicMock()
    project_store.get_project_run_config.return_value = {
        "project_name": "CLI Matrix Project",
        "project_root": "/app",
    }

    service = management_runtime.create_job_command_service(
        get_logger=lambda: job_store,
        get_project_run_config=project_store.get_project_run_config,
    )

    assert service._project_run_config(job_store, "project-1") == {
        "project_name": "CLI Matrix Project",
        "project_root": "/app",
    }
    project_store.get_project_run_config.assert_called_once_with("project-1")


def test_project_command_service_uses_integration_store_for_notification_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.management.command_models import ProjectNotificationDispatchInput
    from core.application.management.scope import ScopeContext
    from core.application.notifications import dispatch as notification_dispatch

    project_store = MagicMock()
    project_store.get_project.return_value = {
        "id": "project-1",
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    integration_store = MagicMock()
    dispatch = MagicMock(return_value=[])
    monkeypatch.setattr(notification_dispatch, "dispatch_project_notification_event", dispatch)

    service = management_runtime.create_project_command_service(
        get_logger=lambda: project_store,
        get_notification_logger=lambda: integration_store,
    )
    service.dispatch_project_notification(
        "project-1",
        ProjectNotificationDispatchInput(event_type="workflow.completed"),
        scope=ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="cli",
            principal_type="system_actor",
            project_id="project-1",
        ),
    )

    assert dispatch.call_args.args[0] is integration_store
    assert dispatch.call_args.kwargs["project_id"] == "project-1"
