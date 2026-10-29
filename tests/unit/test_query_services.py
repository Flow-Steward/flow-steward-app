from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs.workflow_run_models import (
    PublicWorkflowResultV1,
    WorkflowRunStateV1,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceUnavailableError,
)
from core.application.management.query_models import (
    ProjectJobRowView,
    ProjectJobsListView,
    ProjectWorkflowBindingsView,
    WorkflowValidationResultView,
)
from core.application.management.query_services import (
    ApprovalQueryService,
    EventQueryService,
    JobQueryService,
    ProjectQueryService,
    ToolQueryService,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.mcp.server_api_client import AppApiError

_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
    "jinja2",
    "psycopg_runtime",
    "langgraph",
    "langchain_core",
}

PROJECT_UUID = "11111111-1111-4111-8111-111111111111"
KB_PROJECT_UUID = "77777777-7777-4777-8777-777777777777"


def _mcp_data(text: str) -> dict:
    payload = json.loads(text)
    assert payload["ok"] is True
    return payload["data"]


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


def _assert_frontend_shell_response(resp) -> str:
    assert resp.status_code == 200
    text = resp.text
    assert "<!doctype html>" in text.lower()
    assert '<div id="root"></div>' in text
    assert "/app/assets/index-" in text
    return text


def _fake_request(*, auth_user: dict[str, Any] | None = None) -> Any:
    return types.SimpleNamespace(state=types.SimpleNamespace(auth_user=auth_user))


def _import_mcp_server(monkeypatch):
    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    sys.modules.pop("core.entrypoints.mcp.server", None)
    importlib.import_module("core.entrypoints.mcp.server")
    project_tools = importlib.import_module("core.entrypoints.mcp.server_project_workflow_tools")
    notification_tools = importlib.import_module(
        "core.entrypoints.mcp.server_report_notification_tools"
    )
    return types.SimpleNamespace(
        project_jobs=project_tools.project_jobs,
        project_list=project_tools.project_list,
        project_notification_routes=notification_tools.project_notification_routes,
        workflow_trigger=project_tools.workflow_trigger,
    )


class _TestClientBackedAppApiClient:
    """Route thin MCP requests through the same FastAPI app used by REST assertions."""

    def __init__(self, client, requests: list[dict[str, object]]) -> None:
        self._client = client
        self._requests = requests

    def request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        params: dict[str, Any] | None = None,
        json_body: object | None = None,
        correlation_id: str = "",
        idempotency_key: str = "",
        additional_headers: dict[str, str] | None = None,
    ) -> object:
        headers = {
            "Authorization": f"Bearer {bearer}",
            **dict(additional_headers or {}),
        }
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        self._requests.append(
            {
                "method": method,
                "path": path,
                "bearer": bearer,
                "params": dict(params or {}),
            }
        )
        response = self._client.request(
            method,
            path,
            params=params,
            json=json_body,
            headers=headers,
        )
        payload = response.json()
        if response.is_error:
            raise AppApiError(response.status_code, payload)
        return payload


@pytest.fixture(scope="module")
def client():
    try:
        from fastapi.testclient import TestClient

        with patch(
            "core.infrastructure.runtime.app_factory.validate_runtime_service_ports",
            return_value={},
        ):
            from core.entrypoints.api.app import create_app

            current_app = create_app()
        current_app.state.local_auth_enabled_override = lambda: False
        current_app.state.external_auth_enabled_override = lambda: False
        yield TestClient(current_app, raise_server_exceptions=False)
    except Exception as exc:
        _skip_only_missing_optional_dep(
            exc, context="Could not create TestClient for query service tests"
        )


class TestQueryServices:
    def test_get_workflow_run_reuses_durable_artifact_snapshot_without_new_revision(self):
        now = datetime.now(UTC)
        snapshot = WorkflowRunStateV1(
            job_id="job-1",
            project_id="proj-1",
            workflow_id="workflow-1",
            status="completed",
            terminal=True,
            result=PublicWorkflowResultV1(
                outputs={"ticket_id": "T-1"},
                artifacts={"report": {"artifact_id": "artifact-1", "filename": "report.pdf"}},
            ),
            queued_at=now,
            started_at=now,
            updated_at=now,
            finished_at=now,
        )
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": "job-1",
            "project_id": "proj-1",
            "thread_id": "thread-1",
            "selected_workflow": "workflow-1",
            "status": "completed",
            "finished_at": now,
            "meta": {
                "workflow_outputs": {
                    "outputs": {"ticket_id": "T-1"},
                    "artifacts": {"report": {"artifact_id": "artifact-1"}},
                }
            },
        }
        bl.get_project.return_value = {
            "id": "proj-1",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_public_progress_snapshot.return_value = types.SimpleNamespace(
            snapshot=snapshot,
            marker_id="marker-1",
        )
        resolve_artifact = MagicMock(
            return_value={
                "artifact_id": "artifact-1",
                "filename": "report.pdf",
                "owner_scope": {"project_id": "proj-1", "job_id": "job-1"},
            }
        )
        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "completed",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
            get_thread_state_snapshot=lambda _thread_id: None,
            resolve_artifact=resolve_artifact,
        )

        result = service.get_workflow_run(
            "job-1",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID),
        )

        assert result == snapshot
        bl.record_public_progress_transition.assert_not_called()

    def test_step_output_value_is_read_only_from_a_message_of_that_job(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "proj-42",
            "status": "completed",
            "task_summary": "Write briefs",
        }
        bl.get_project.return_value = {
            "id": "proj-42",
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        brief = "Vilnius is the capital. " * 60
        messages = {
            "m-own": {"id": "m-own", "job_id": "42", "artifacts": {"brief": brief}},
            "m-other": {"id": "m-other", "job_id": "99", "artifacts": {"brief": "other job"}},
        }
        bl.get_agent_message_by_id.side_effect = lambda message_id: messages.get(message_id)
        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "done",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        assert service.get_step_output_value(42, "m-own", "brief") == {
            "field": "brief",
            "value": brief,
        }
        with pytest.raises(ServiceNotFoundError):
            service.get_step_output_value(42, "m-other", "brief")
        with pytest.raises(ServiceNotFoundError):
            service.get_step_output_value(42, "m-own", "missing")

    def test_job_query_service_get_job_status_builds_view_model(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "proj-42",
            "status": "in_progress",
            "thread_id": "thread-42",
            "task_summary": "Investigate retry flow",
            "mode": "greenfield",
            "task_source": "workflow_test_run",
            "started_at": now,
            "finished_at": None,
            "duration_ms": 1500,
        }
        bl.get_job_cost_summary.return_value = {
            "tokens_prompt": 11,
            "tokens_completion": 7,
            "tokens_total": 18,
            "cost_usd": 0.12,
            "llm_calls": 2,
        }
        bl.get_project.return_value = {
            "id": "proj-42",
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        build_runtime_payload = AsyncMock(
            return_value={
                "current_step_id": "planning",
                "runtime_fallback": False,
                "last_structured_error": {
                    "source": "provider",
                    "category": "provider",
                    "provider_name": "openai",
                    "http_status": 429,
                    "provider_error_code": "rate_limit",
                    "request_id": "req-safe-42",
                    "retryable": True,
                    "error_message": "Rate limit exceeded.",
                },
            }
        )

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=build_runtime_payload,
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["id"] == "42"
        assert payload["project_id"] == "proj-42"
        assert payload["mode"] == "greenfield"
        assert payload["runtime"]["current_step_id"] == "planning"
        assert payload["cost"]["tokens_total"] == 18
        assert payload["failure_summary"]["kind"] == "provider_rate_limit"
        assert payload["failure_summary"]["diagnostic_ids"]["request_id"] == "req-safe-42"
        assert payload["trigger"] == {
            "kind": "test_run",
            "label": "Test run from the workflow builder",
            "label_key": "react.jobs.workflow_builder_test_run",
        }
        build_runtime_payload.assert_awaited_once()

    def test_job_query_service_get_job_status_clears_active_step_for_cancelled_job(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "proj-42",
            "status": "cancelled",
            "thread_id": "thread-42",
            "task_summary": "Cancelled run",
            "mode": "greenfield",
            "started_at": now,
            "finished_at": now,
            "duration_ms": 1500,
            "current_step_id": "",
            "current_group_id": "",
            "current_agent": "",
        }
        bl.get_job_cost_summary.return_value = {}
        bl.get_project.return_value = {
            "id": "proj-42",
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        build_runtime_payload = AsyncMock(
            return_value={
                "current_step_id": "development",
                "current_group_id": "drafts",
                "active_steps": [{"step_id": "development"}],
                "active_agent": "composer",
                "runtime_fallback": False,
            }
        )

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "cancelled",
            build_runtime_payload_for_api=build_runtime_payload,
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["current_step_id"] is None
        assert payload["current_group_id"] is None
        assert payload["current_agent"] is None
        assert payload["runtime"]["current_step_id"] is None
        assert payload["runtime"]["current_group_id"] is None
        assert payload["runtime"]["active_steps"] == []
        assert payload["runtime"]["active_agent"] == ""

    def test_job_query_service_preserves_persisted_step_for_failed_test_run(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "proj-42",
            "status": "failed",
            "thread_id": "thread-42",
            "task_summary": "Failed test run",
            "task_source": "workflow_test_run",
            "selected_workflow_key": "extension.install-a.release_audit",
            "started_at": now,
            "finished_at": now,
            "duration_ms": 1500,
            "current_step_id": "sync_records",
            "current_group_id": "per_record",
            "current_agent": "",
        }
        bl.get_job_cost_summary.return_value = {}
        bl.get_project.return_value = {
            "id": "proj-42",
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project_workflows.return_value = []
        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "failed",
            build_runtime_payload_for_api=AsyncMock(
                return_value={
                    "current_step_id": "stale_step",
                    "current_group_id": None,
                    "active_agent": "stale_agent",
                }
            ),
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["selected_workflow"] == "extension.install-a.release_audit"
        assert payload["selected_workflow_source"] == "job.selected_workflow_key"
        assert payload["current_step_id"] == "sync_records"
        assert payload["current_group_id"] == "per_record"
        assert payload["runtime"]["current_step_id"] == "sync_records"
        assert payload["runtime"]["current_group_id"] == "per_record"
        assert payload["current_agent"] is None
        assert payload["runtime"]["active_agent"] == ""

    def test_job_query_service_get_job_status_returns_workflow_metadata(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "status": "in_progress",
            "thread_id": "thread-42",
            "task_summary": "Run the standard feature workflow",
            "selected_workflow": "weekly_report",
            "meta": {
                "invocation": {
                    "channel": "webhook",
                    "trigger_family": "incoming_webhook",
                    "request_id": "req-42",
                    "mapped_inputs": {
                        "email": "[REDACTED]",
                        "credential": {
                            "__type__": "flow-steward.secret-ref.v1",
                            "name": "credential",
                            "ciphertext": "replayable-ciphertext",
                        },
                    },
                }
            },
            "workflow_id": "legacy_workflow",
            "runtime_state": {"workflow_id": "runtime_workflow"},
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_job_cost_summary.return_value = {}
        bl.get_project_workflows.return_value = [
            {
                "workflow_id": "weekly_report",
                "workflow_display_name": "Standard Feature",
                "enabled": True,
            },
            {
                "workflow_id": "runtime_workflow",
                "workflow_display_name": "Runtime Workflow",
                "enabled": True,
            },
        ]

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["selected_workflow"] == "weekly_report"
        assert payload["selected_workflow_source"] == "job.selected_workflow"
        assert payload["workflow_selection_reason"] == "Resolved from job.selected_workflow."
        assert payload["selected_workflow_display_name"] == "Standard Feature"
        assert payload["invocation"]["channel"] == "webhook"
        assert payload["invocation"]["mapped_inputs"]["email"] == "[REDACTED]"
        assert payload["invocation"]["mapped_inputs"]["credential"] == "[REDACTED]"
        assert "replayable-ciphertext" not in json.dumps(payload)
        bl.get_project_workflows.assert_called_once_with("7", enabled_only=False)

    def test_job_query_service_get_job_status_prefers_registry_label_from_job_row(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "status": "completed",
            "thread_id": "thread-42",
            "task_summary": "Import inventory",
            "selected_workflow": "inventory_import",
            "selected_workflow_display_name": "Inventory Import",
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        bl.get_project.return_value = {
            "id": 7,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_job_cost_summary.return_value = {}
        bl.get_project_workflows.return_value = []

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "completed",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["selected_workflow_display_name"] == "Inventory Import"
        bl.get_project_workflows.assert_not_called()

    def test_job_query_service_get_job_status_labels_a_hook_run_as_the_hook(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "status": "completed",
            "thread_id": "thread-42",
            "task_summary": "Hook 'Before finish' from step 'hook_step'",
            "selected_workflow": "hook_storage_wf_1",
            "selected_workflow_display_name": "Before finish",
            "hook_storage": True,
            "hook_author_workflow_display_name": "Order fulfilment",
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_job_cost_summary.return_value = {}
        bl.get_project_workflows.return_value = []

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "completed",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["selected_workflow_display_name"] == "Before finish · Order fulfilment"

    def test_job_query_service_get_job_status_uses_runtime_state_workflow_fallback(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "status": "in_progress",
            "thread_id": "thread-42",
            "task_summary": "Run a workflow",
            "runtime_state": {"workflow_id": "runtime_workflow"},
            "workflow_id": "legacy_workflow",
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_job_cost_summary.return_value = {}
        bl.get_project_workflows.return_value = []

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["selected_workflow"] == "runtime_workflow"
        assert payload["selected_workflow_source"] == "runtime_state.workflow_id"

    def test_job_query_service_get_job_status_fails_closed_when_project_lookup_missing(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "status": "in_progress",
            "thread_id": "thread-42",
            "task_summary": "Investigate retry flow",
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        bl.get_project.return_value = None

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        # The project row is simply gone, which is an orphaned job rather than an
        # outage — reporting "Project lookup unavailable" (503) hid the job from
        # the operator who has to clear it. Authorization still fails closed:
        # this job carries no account_id, so account scoping refuses it.
        with pytest.raises(ServiceForbiddenError):
            asyncio.run(service.get_job_status(42))

    def test_job_query_service_serves_an_orphaned_job_to_its_own_account(self):
        """A job whose project was deleted must stay visible to its own account.

        It is exactly the job an operator has to acknowledge or cancel to clear a
        red readiness check, and a 503 made that impossible.
        """
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "account_id": DEFAULT_ACCOUNT_ID,
            "status": "triage_needed",
            "thread_id": "thread-42",
            "task_summary": "Investigate retry flow",
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        bl.get_project.return_value = None

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID, actor_id="alice", principal_type="admin"
        )
        _bl, job, _scope = service._job_context("42", scope=scope)

        assert job["id"] == 42

    def test_job_query_service_uses_separate_project_lookup_port(self):
        job_store = MagicMock()
        job_store.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "status": "in_progress",
            "thread_id": "thread-42",
            "task_summary": "Inspect a projected workflow",
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
        }
        job_store.get_job_cost_summary.return_value = {}
        job_store.get_project_workflows.return_value = []
        get_project = MagicMock(
            return_value={"id": "7", "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        )
        service = JobQueryService(
            get_logger=lambda: job_store,
            get_project_by_id=get_project,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = asyncio.run(service.get_job_status(42)).dump()

        assert payload["status"] == "in_progress"
        get_project.assert_called_once_with("7")

    def test_job_query_service_get_trace_builds_view_model(self):
        job_store = MagicMock()
        trace_store = MagicMock()
        job_store.get_job.return_value = {
            "id": 42,
            "project_id": "7",
            "status": "in_progress",
        }
        job_store.get_project.return_value = {
            "id": 7,
            "name": "demo",
            "account_id": "acc_alpha",
        }
        trace_store.build_job_trace.return_value = {
            "trace_id": "job:42",
            "job_id": 42,
            "project_id": "7",
            "account_id": "acc_alpha",
            "status": "in_progress",
            "root_span_id": "job:42:root",
            "root_run_id": "run-42",
            "linkage": {"correlation_ids": ["corr-1"], "run_ids": ["run-42"]},
            "spans": [
                {
                    "span_id": "job:42:root",
                    "trace_id": "job:42",
                    "kind": "run",
                    "layer": "job",
                    "name": "job",
                }
            ],
        }

        service = JobQueryService(
            get_logger=lambda: job_store,
            get_trace_store=lambda: trace_store,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = service.get_trace(
            42,
            scope=ScopeContext(account_id="acc_alpha", actor_id="alice", principal_type="user"),
            limit=750,
        ).dump()

        assert payload["trace_id"] == "job:42"
        assert payload["root_run_id"] == "run-42"
        assert payload["spans"][0]["layer"] == "job"
        trace_store.build_job_trace.assert_called_once_with(
            "42",
            account_id="acc_alpha",
            limit=750,
            job=job_store.get_job.return_value,
        )

    def test_job_query_service_list_project_jobs_builds_stable_payload(self):
        job_id = uuid.UUID("7c09d799-d212-4562-a555-c931133e56ba")
        project_id = uuid.UUID(PROJECT_UUID)
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_project.return_value = {
            "id": str(project_id),
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_jobs.return_value = [
            {
                "id": job_id,
                "project_id": project_id,
                "task_id": "task-201",
                "task_summary": "Investigate auth",
                "status": "queued",
                "started_at": now,
                "duration_ms": 65000,
            }
        ]
        bl.count_jobs.return_value = 51

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
            build_display_summary=lambda value: f"summary:{value}",
        )

        payload = service.list_project_jobs(str(project_id), page=2, per_page=25).dump()

        assert payload["page"] == 2
        assert payload["per_page"] == 25
        assert payload["total"] == 51
        assert payload["total_pages"] == 3
        assert payload["jobs"][0]["id"] == str(job_id)
        assert payload["jobs"][0]["project_id"] == str(project_id)
        assert payload["jobs"][0]["display_summary"] == "summary:Investigate auth"
        assert payload["jobs"][0]["started_at"] == now.isoformat()
        bl.get_jobs.assert_called_once_with(str(project_id), limit=25, offset=25)

    def test_project_job_row_view_coerces_uuid_fields_to_strings(self):
        job_id = uuid.UUID("7c09d799-d212-4562-a555-c931133e56ba")
        project_id = uuid.UUID(PROJECT_UUID)

        payload = ProjectJobRowView.model_validate(
            {
                "id": job_id,
                "project_id": project_id,
                "task_summary": "Investigate auth",
                "status": "queued",
            }
        ).dump()

        assert payload["id"] == str(job_id)
        assert payload["project_id"] == str(project_id)

    def test_project_job_row_view_drops_raw_job_metadata_from_public_lists(self):
        payload = ProjectJobRowView.model_validate(
            {
                "id": "job-1",
                "project_id": "project-1",
                "task_summary": "Run workflow",
                "status": "queued",
                "meta": {
                    "execution_envelope": {
                        "inputs": {
                            "credential": {
                                "__type__": "flow-steward.secret-ref.v1",
                                "name": "credential",
                                "ciphertext": "replayable-ciphertext",
                            }
                        }
                    }
                },
            }
        ).dump()

        assert "meta" not in payload
        assert "replayable-ciphertext" not in json.dumps(payload)

    def test_job_query_service_get_runtime_journal_reads_meta_json(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 56,
            "project_id": "7",
            "meta": '{"runtime_journal":[{"event":"watchdog_mark","reason":"stuck"}]}',
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = service.get_runtime_journal(56).dump()

        assert payload["job_id"] == "56"
        assert payload["runtime_journal"][0]["event"] == "watchdog_mark"

    def test_job_query_service_get_runtime_journal_merges_runtime_events_and_messages(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 56,
            "project_id": "7",
            "meta": '{"runtime_journal":[{"event":"queue_delivery_claimed","reason":"claimed"}]}',
            "runtime_state": {
                "orchestrator_journal": [
                    {
                        "event": "step_completed",
                        "step_id": "quality",
                        "reason": "verifier passed",
                        "detail": {"action": "continue"},
                    }
                ],
            },
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        event_rows = [
            {
                "id": f"evt-noise-{index}",
                "timestamp": f"2026-06-03T00:00:{index % 60:02d}Z",
                "phase": "development",
                "agent": "composer",
                "event_type": "worker_heartbeat",
                "severity": "info",
                "title": "heartbeat",
                "detail": {},
            }
            for index in range(500)
        ]
        event_rows.append(
            {
                "id": "evt-late-routing",
                "timestamp": "2026-06-03T00:30:00Z",
                "phase": "quality",
                "agent": "orchestrator",
                "event_type": "routing_decision",
                "severity": "info",
                "title": "Phase quality next_phase",
                "detail": {"reason": "checker pass"},
            }
        )

        def _get_events(job_id: str, since_id=0, limit=500):
            assert job_id == "56"
            start = 0
            if since_id:
                ids = [str(row.get("id")) for row in event_rows]
                start = ids.index(str(since_id)) + 1
            return event_rows[start : start + limit]

        bl.get_events.side_effect = _get_events
        bl.get_agent_messages.return_value = [
            {
                "id": "msg-proofreader",
                "from_agent": "proofreader",
                "step_id": "quality",
                "skill": "review_summary",
                "status": "blocked",
                "summary": "Review blocked by sandbox.",
                "artifacts": {
                    "raw_output": "x" * 5000,
                    "task_envelope": {"description": "large task context"},
                    "blocked_reason": "Sandbox unavailable",
                    "checks_run": [
                        {
                            "check": "ruff",
                            "passed": False,
                            "blocking_reason": "Ruff failed",
                            "stdout": "y" * 5000,
                        }
                    ],
                },
                "errors": ["bwrap unavailable"],
                "timestamps": {"published_at": "2026-06-03T00:01:00Z"},
            },
            {
                "id": "msg-verifier",
                "from_agent": "checker",
                "step_id": "task_verification",
                "skill": "check_summary",
                "status": "pass",
                "summary": "Task verifier passed.",
                "artifacts": {"task_verdict": "pass"},
                "errors": [],
                "timestamps": {"published_at": "2026-06-03T00:02:00Z"},
            },
        ]

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = service.get_runtime_journal(56).dump()

        events = [item["event"] for item in payload["runtime_journal"]]
        assert "queue_delivery_claimed" in events
        assert "step_completed" in events
        quality_entry = next(
            item for item in payload["runtime_journal"] if item["event"] == "step_completed"
        )
        assert quality_entry["step_id"] == "quality"
        assert "routing_decision" in events
        assert "agent_message_blocked" in events
        assert "agent_message_pass" in events
        assert "worker_heartbeat" not in events
        assert bl.get_events.call_args_list[0].kwargs == {"since_id": 0, "limit": 500}
        assert bl.get_events.call_args_list[1].kwargs == {
            "since_id": "evt-noise-499",
            "limit": 500,
        }
        bl.get_agent_messages.assert_called_once_with("56", newest=True, limit=1000)
        review_entry = next(
            item for item in payload["runtime_journal"] if item["event"] == "agent_message_blocked"
        )
        review_detail = review_entry["detail"]
        assert "artifacts" not in review_detail
        assert review_detail["message_detail_ref"] == {
            "kind": "agent_message",
            "id": "msg-proofreader",
        }
        assert review_detail["artifact_summary"]["blocked_reason"] == "Sandbox unavailable"
        assert review_detail["artifact_summary"]["checks_run"][0]["check"] == "ruff"
        assert "raw_output" not in str(review_detail)
        assert "large task context" not in str(review_detail)

    def test_job_query_service_list_messages_applies_filters_and_pagination(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_job.return_value = {"id": 99, "project_id": "7"}
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_agent_messages.return_value = [
            {"id": 201, "timestamp": now, "job_id": 99, "summary": "old"},
            {"id": 202, "timestamp": now, "job_id": 99, "summary": "new"},
            {"id": 203, "timestamp": now, "job_id": 99, "summary": "extra"},
        ]

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
            serialize_agent_message_row=lambda row: {
                "id": row["id"],
                "summary": row["summary"],
            },
        )

        payload = service.list_messages(
            99,
            since_id=100,
            before_id=500,
            newest=True,
            step_id="proofreader",
            producer="reviewer",
            skill="check",
            status="pass",
            limit=2,
        ).dump()

        assert payload["has_more"] is True
        assert payload["last_id"] == "203"
        assert [row["id"] for row in payload["messages"]] == ["202", "203"]
        bl.get_agent_messages.assert_called_once_with(
            "99",
            since_id="100",
            before_id="500",
            newest=True,
            limit=3,
            step_id="proofreader",
            from_agent="reviewer",
            skill="check",
            status="pass",
        )

    def test_job_query_service_get_replay_step_builds_trace_payload(self):
        bl = MagicMock()
        bl.get_job.return_value = {"id": 42, "project_id": "7"}
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
            resolve_replay_message_row=lambda _bl, **kwargs: (
                {"id": kwargs["message_id"], "job_id": kwargs["job_id"]},
                None,
                200,
            ),
            collect_replay_step_trace=lambda _bl, job_id, row: {
                "message": {"id": row["id"]},
                "routing_decisions": [{"job_id": job_id}],
                "related_messages": [],
            },
        )

        payload = service.get_replay_step(42, message_id=900).dump()

        assert payload["message"]["id"] == "900"
        assert payload["routing_decisions"][0]["job_id"] == "42"

    def test_job_query_service_get_logs_page_builds_context(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 786,
            "project_id": "1",
            "status": "completed",
            "task_summary": "Some task",
            "thread_id": "thread-786",
            "meta": {},
        }
        bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_logs.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]
        bl.get_events.return_value = [{"id": 4}]
        bl.get_agent_messages.return_value = [{"id": 5}, {"id": 6}, {"id": 7}]
        bl.list_job_decision_overrides.return_value = [{"id": 8, "job_id": 786}]
        bl.list_job_errors.return_value = [{"id": 9, "job_id": 786}]

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
            serialize_decision_override_row=lambda row: {"id": row["id"]},
            serialize_job_error_row=lambda row: {"id": row["id"]},
            build_runtime_payload=lambda job, current_bl: {
                "current_step_id": "delivery",
                "job_id": job["id"],
                "same_bl": current_bl is bl,
            },
            resolve_job_project_name=lambda _bl, _job: "test-project",
        )

        payload = service.get_logs_page(786, initial_limit=2)

        assert payload.project_name == "test-project"
        assert payload.log_has_more is True
        assert payload.message_has_more is True
        assert payload.runtime["current_step_id"] == "delivery"
        assert payload.decision_overrides[0]["id"] == 8
        assert payload.job_errors[0]["id"] == 9
        bl.get_logs.assert_called_once_with("786", limit=3, newest=True)
        bl.get_events.assert_called_once_with("786", limit=25)
        bl.get_agent_messages.assert_called_once_with("786", limit=3, newest=True)

    def test_terminal_job_default_logs_include_latest_failure_page(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 786,
            "project_id": "1",
            "status": "failed",
        }
        bl.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_logs.return_value = [
            {"id": "older"},
            {"id": "terminal", "error": "Agent output contract failed"},
        ]
        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = service.list_logs(786).dump()

        assert payload["logs"][-1]["id"] == "terminal"
        bl.get_logs.assert_called_once_with(
            "786",
            since_id="",
            before_id=None,
            newest=True,
            limit=26,
        )

    def test_job_query_service_get_logs_page_fails_closed_on_project_lookup_failure(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 787,
            "project_id": "1",
            "status": "completed",
            "task_summary": "Some task",
            "thread_id": "thread-787",
            "meta": {},
        }
        bl.get_project.side_effect = RuntimeError("temporary lookup failure")
        bl.get_project_run_config.return_value = {"project_name": "test-project"}
        bl.get_logs.return_value = []
        bl.get_events.return_value = []
        bl.get_agent_messages.return_value = []
        bl.list_job_decision_overrides.return_value = []
        bl.list_job_errors.return_value = []

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
            build_runtime_payload=lambda _job, _bl: {},
            resolve_job_project_name=lambda current_bl, job: str(
                (current_bl.get_project_run_config(int(job.get("project_id") or 0)) or {}).get(
                    "project_name"
                )
                or ""
            ).strip(),
        )

        with pytest.raises(ServiceUnavailableError, match="Project lookup unavailable"):
            service.get_logs_page(787)

    def test_job_query_service_list_awaiting_approval_respects_allowed_project_ids(self):
        bl = MagicMock()
        bl.get_awaiting_approval_jobs.return_value = [
            {
                "id": 1,
                "project_id": "10",
                "status": "awaiting_approval",
                "account_id": "acc_alpha",
                "tenant_id": "00000000-0000-0000-0000-000000000001",
            },
            {
                "id": 2,
                "project_id": "20",
                "status": "awaiting_approval",
                "account_id": "acc_alpha",
                "tenant_id": "00000000-0000-0000-0000-000000000001",
            },
        ]
        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        rows = service.list_awaiting_approval_jobs(
            scope=ScopeContext(account_id="acc_alpha", allowed_project_ids=frozenset({"10"}))
        )

        assert [row.id for row in rows] == ["1"]

    def test_job_query_service_list_reactions_returns_rows(self):
        job_store = MagicMock()
        event_store = MagicMock()
        job_store.get_job.return_value = {"id": 55, "project_id": "1"}
        job_store.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        event_store.list_job_reaction_events.return_value = [
            {"id": 1, "event_type": "external.ci.failed", "source": "gitlab"}
        ]

        service = JobQueryService(
            get_logger=lambda: job_store,
            get_event_store=lambda: event_store,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = service.list_reactions(55).dump()

        assert payload["job_id"] == "55"
        assert payload["reactions"][0]["event_type"] == "external.ci.failed"
        event_store.list_job_reaction_events.assert_called_once_with("55")

    def test_job_query_service_list_reactions_fails_when_projection_is_unavailable(self):
        job_store = MagicMock()
        job_store.get_job.return_value = {"id": 55, "project_id": "1"}
        job_store.get_project.return_value = {
            "id": 1,
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        service = JobQueryService(
            get_logger=lambda: job_store,
            get_event_store=lambda: object(),
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        with pytest.raises(ServiceUnavailableError, match="Reaction projection is not available"):
            service.list_reactions(55)

    def test_project_query_service_get_project_edit_form_returns_page_context(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
            "agents_config": {"default_provider": "openai", "default_model": "gpt-4.1-mini"},
        }
        bl.get_project_workflows.return_value = [
            {"workflow_id": "wf-1", "enabled": True, "is_default": True},
            {"workflow_id": "wf-2", "enabled": False, "is_default": False},
        ]
        bl.get_project_policy.return_value = {"autonomy_mode": "supervised"}
        bl.list_project_settings_revisions.return_value = [{"id": 11, "created_at": now}]

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
            list_workflow_choices=lambda **kwargs: [
                {"workflow_id": "wf-1", "display_name": "Workflow One"},
                {"workflow_id": "wf-2", "display_name": "Workflow Two"},
            ],
        )

        payload = service.get_project_edit_form(7)

        assert payload.project["operator_default_workflow_id"] == "wf-1"
        assert payload.project["default_provider"] == "openai"
        assert payload.project["default_model"] == "gpt-4.1-mini"
        assert "index_status" not in payload.project
        assert not any(key.startswith("stack_") for key in payload.project)
        assert payload.project_workflow_ids == ["wf-1"]
        assert [row["workflow_id"] for row in payload.project_workflows] == ["wf-1", "wf-2"]
        assert payload.project_policy["autonomy_mode"] == "supervised"
        assert payload.project_settings_revisions[0]["id"] == 11

    def test_list_projects_forwards_filters_and_sort_to_the_query(self):
        bl = MagicMock()
        bl.list_projects.return_value = [
            {"id": 1, "name": "Alpha", "job_count": 4, "account_id": DEFAULT_ACCOUNT_ID}
        ]
        bl.count_projects.return_value = 1

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "job_count": int(row.get("job_count") or 0),
            },
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
            to_json_obj=lambda value: value,
        )

        payload = service.list_projects(has_jobs="YES", sort="Job_Count", direction="DESC")

        assert payload.total == 1
        list_kwargs = bl.list_projects.call_args.kwargs
        # Normalized once in the service, so the query layer never re-parses them.
        assert "project_type" not in list_kwargs
        assert list_kwargs["has_jobs"] == "yes"
        assert list_kwargs["sort"] == "job_count"
        assert list_kwargs["direction"] == "desc"
        count_kwargs = bl.count_projects.call_args.kwargs
        assert count_kwargs["has_jobs"] == "yes"

    def test_project_query_service_get_runtime_config_returns_run_config(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.get_project_run_config.return_value = {"project_name": "Demo"}

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_runtime_config(7).dump()

        assert payload["project_id"] == "7"
        assert payload["project_name"] == "Demo"
        assert "db_type" not in payload["run_config"]

    def test_tool_query_service_list_tools_filters_category(self):
        db = MagicMock()
        db.list_tools.return_value = [
            {"tool_id": "search_tool", "category": "search"},
            {"tool_id": "git_status", "category": "version_control"},
        ]

        service = ToolQueryService(
            get_db=lambda: db,
            get_registry=MagicMock(),
            get_tool_validation=lambda _db, _tool_id: {"valid": True, "errors": [], "warnings": []},
        )

        payload = service.list_tools(category="search").dump()

        assert [row["tool_id"] for row in payload["tools"]] == ["search_tool"]
        db.list_tools.assert_called_once_with(enabled_only=False)

    def test_tool_query_marks_virtual_projection_unassignable(self):
        from core.application.management import query_services_tool

        projection = {
            "tool_id": "extension.install-1.catalog_tool",
            "account_id": DEFAULT_ACCOUNT_ID,
            "source_install_id": "install-1",
            "display_name": "Catalog Tool",
            "enabled": True,
        }
        db = MagicMock()
        db.list_tools.return_value = [
            {"tool_id": "std.numbers", "account_id": None, "enabled": True},
            {"tool_id": "shared_manual_tool", "account_id": None, "enabled": True},
            {"tool_id": "account_manual_tool", "account_id": DEFAULT_ACCOUNT_ID, "enabled": True},
            {
                "tool_id": "installed_durable_tool",
                "account_id": DEFAULT_ACCOUNT_ID,
                "source_install_id": "install-1",
                "enabled": True,
            },
            {"tool_id": "foreign_manual_tool", "account_id": "account-other", "enabled": True},
            {
                "tool_id": "same_tenant_tool",
                "account_id": DEFAULT_ACCOUNT_ID,
                "implementation": {"type": "mcp", "owner_tenant_id": "tenant-a"},
                "enabled": True,
            },
            {
                "tool_id": "foreign_tenant_tool",
                "account_id": DEFAULT_ACCOUNT_ID,
                "implementation": {"type": "mcp", "owner_tenant_id": "tenant-other"},
                "enabled": True,
            },
        ]
        service = ToolQueryService(
            get_db=lambda: db,
            get_registry=MagicMock(),
            get_tool_validation=lambda *_args: {"valid": True},
        )

        with patch.object(
            query_services_tool,
            "projection_aware_catalog_rows",
            side_effect=lambda _db, _kind, _key, durable, **_kwargs: [*durable, projection],
        ):
            rows = service.list_tools(
                scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, tenant_id="tenant-a")
            ).dump()["tools"]

        row = next(row for row in rows if row["tool_id"] == projection["tool_id"])
        assert row["agent_relationship_assignable"] is False
        assert row["agent_relationship_reason_code"] == "tool_not_assignable_in_current_scope"
        assert "unavailable for assignment" in row["agent_relationship_reason_text"]
        assert (
            next(row for row in rows if row["tool_id"] == "shared_manual_tool")[
                "agent_relationship_assignable"
            ]
            is True
        )
        assert (
            next(row for row in rows if row["tool_id"] == "account_manual_tool")[
                "agent_relationship_assignable"
            ]
            is True
        )
        assert (
            next(row for row in rows if row["tool_id"] == "installed_durable_tool")[
                "agent_relationship_assignable"
            ]
            is True
        )
        assert (
            next(row for row in rows if row["tool_id"] == "std.numbers")[
                "agent_relationship_assignable"
            ]
            is False
        )
        connector_only = next(row for row in rows if row["tool_id"] == "std.numbers")
        assert connector_only["agent_relationship_reason_code"] == "tool_connector_only"
        assert connector_only["agent_relationship_reason_text"] == (
            "Standard Tool Actions are available only through connector workflow steps"
        )
        assert not any(row["tool_id"] == "foreign_manual_tool" for row in rows)
        assert (
            next(row for row in rows if row["tool_id"] == "same_tenant_tool")[
                "agent_relationship_assignable"
            ]
            is True
        )
        assert not any(row["tool_id"] == "foreign_tenant_tool" for row in rows)

    def test_tool_query_service_contract_validation_returns_payload(self):
        db = MagicMock()
        db.get_tool.return_value = {
            "tool_id": "search_tool",
            "account_id": DEFAULT_ACCOUNT_ID,
        }

        service = ToolQueryService(
            get_db=lambda: db,
            get_registry=MagicMock(),
            get_tool_validation=lambda current_db, tool_id: {
                "valid": True,
                "tool_id": tool_id,
                "db_is_same": current_db is db,
            },
        )

        payload = service.get_contract_validation("search_tool").dump()

        assert payload["tool_id"] == "search_tool"
        assert payload["validation"]["valid"] is True
        assert payload["validation"]["db_is_same"] is True

    def test_tool_query_service_get_diff_maps_missing_history_to_not_found(self):
        from core.application.management.errors import ServiceNotFoundError

        db = MagicMock()
        db.get_tool.return_value = {
            "tool_id": "search_tool",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        db.diff_entity_history.side_effect = KeyError("missing")

        service = ToolQueryService(
            get_db=lambda: db,
            get_registry=MagicMock(),
            get_tool_validation=lambda _db, _tool_id: {"valid": True},
        )

        with pytest.raises(ServiceNotFoundError):
            service.get_diff("search_tool")

    def test_project_query_service_queue_serializes_pending_jobs_and_lease(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.conn = MagicMock()
        bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (1, 0, 0, 0)
        bl._column_exists.return_value = False
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_pending_jobs.return_value = [
            {
                "id": 501,
                "task_id": "cu-501",
                "task_summary": "Fix queue card",
                "task_source": "provider",
                "queued_at": now,
            }
        ]
        bl.get_project_policy.return_value = {"concurrency_cap": 2}
        bl.list_project_execution_leases.return_value = [
            {
                "project_id": "7",
                "worker_instance_id": "worker-7",
                "lease_token": "lease-7",
                "heartbeat_at": now,
                "expires_at": now,
                "acquired_at": now,
                "active_job_id": 501,
                "drain_status": "draining",
            }
        ]
        bl.get_project_execution_lease.return_value = {
            "project_id": "7",
            "worker_instance_id": "worker-7",
            "lease_token": "lease-7",
            "heartbeat_at": now,
            "expires_at": now,
            "acquired_at": now,
            "active_job_id": 501,
            "drain_status": "draining",
        }

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 3,
            get_ready_queue_depth=lambda: 11,
        )

        payload = service.get_project_queue(7).dump()

        assert payload["project_id"] == "7"
        assert payload["project_backlog"] == 1
        assert payload["ready_queue_depth"] == 11
        assert payload["project_lease"]["active_job_id"] == "501"
        assert payload["ready_state"]["is_ready"] is True
        assert payload["ready_state"]["reason"] == "capacity_available"
        assert payload["pending_jobs"][0]["task_id"] == "cu-501"
        assert payload["dead_letter_queue_name"] == "platform.project.7.dead_letter"
        assert payload["broker_available"] is True
        assert payload["queue_transport_state"] == "broker_available"
        assert payload["queue_health"]["state"] == "queued_pending"
        assert payload["pending_jobs"][0]["queue_delivery_state"] == "queued"

    def test_project_query_service_queue_keeps_live_state_idle_when_only_historical_dead_letter_exists(
        self,
    ):
        bl = MagicMock()
        bl.conn = MagicMock()
        bl.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (0, 0, 1, 0)
        bl._column_exists.return_value = False
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        bl.get_pending_jobs.return_value = []
        bl.get_project_policy.return_value = {"concurrency_cap": 1}
        bl.list_project_execution_leases.return_value = []
        bl.get_project_execution_lease.return_value = None

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_project_queue(7).dump()

        assert payload["queue_health"]["state"] == "idle"
        assert payload["queue_health"]["attention_state"] == "dead_lettered"
        assert payload["queue_health"]["requires_intervention"] is True

    def test_project_query_service_get_workflow_bindings_returns_default_and_rows(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.get_project_workflows.return_value = [
            {"workflow_id": "wf-1", "enabled": True, "is_default": True},
            {"workflow_id": "wf-2", "enabled": False, "is_default": False},
        ]

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_workflow_bindings(7).dump()

        assert payload["project_id"] == "7"
        assert payload["operator_default_workflow_id"] == "wf-1"
        assert [row["workflow_id"] for row in payload["project_workflows"]] == ["wf-1", "wf-2"]
        assert payload["workflow_ids"] == ["wf-1"]
        assert payload["workflow_ids"] == [
            row["workflow_id"]
            for row in payload["project_workflows"]
            if row.get("enabled") is not False
        ]

    def test_project_workflow_bindings_never_list_a_hook_storage_workflow(self):
        """R2-11: the project's hook steps are kept in a workflow nobody browses or links."""
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.get_project_workflows.return_value = [
            {"workflow_id": "order_flow", "enabled": True, "is_default": True},
            {
                "workflow_id": "order_flow_adjust_order",
                "enabled": True,
                "is_default": False,
                "hook_storage": True,
            },
        ]
        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_workflow_bindings(7).dump()

        assert payload["workflow_ids"] == ["order_flow"]
        assert [row["workflow_id"] for row in payload["project_workflows"]] == ["order_flow"]

    def test_project_query_service_disabled_binding_is_metadata_not_selected_or_default(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
        }
        bl.get_project_workflows.return_value = [
            {"workflow_id": "wf-disabled", "enabled": False, "is_default": True},
            {"workflow_id": "wf-active", "enabled": True, "is_default": False},
        ]
        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_workflow_bindings(7).dump()

        assert payload["workflow_ids"] == ["wf-active"]
        assert payload["operator_default_workflow_id"] == ""
        assert [row["workflow_id"] for row in payload["project_workflows"]] == [
            "wf-disabled",
            "wf-active",
        ]

    def test_project_query_service_filters_workflow_bindings_to_token_allowlist(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
        }
        bl.get_project_workflows.return_value = [
            {"workflow_id": "wf-allowed", "enabled": True, "is_default": False},
            {"workflow_id": "wf-unrelated", "enabled": True, "is_default": True},
        ]
        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_workflow_bindings(
            7,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="machine-1",
                principal_type="service_account",
                allowed_workflow_ids=frozenset({"wf-allowed"}),
            ),
        ).dump()

        assert payload["operator_default_workflow_id"] == ""
        assert payload["workflow_ids"] == ["wf-allowed"]
        assert [row["workflow_id"] for row in payload["project_workflows"]] == ["wf-allowed"]

    def test_project_query_service_empty_workflow_allowlist_keeps_all_bindings(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
        }
        bl.get_project_workflows.return_value = [
            {"workflow_id": "wf-1", "enabled": True, "is_default": True},
            {"workflow_id": "wf-2", "enabled": True, "is_default": False},
        ]
        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_workflow_bindings(
            7,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="operator-1",
                principal_type="operator",
                allowed_workflow_ids=frozenset(),
            ),
        ).dump()

        assert payload["operator_default_workflow_id"] == "wf-1"
        assert [row["workflow_id"] for row in payload["project_workflows"]] == [
            "wf-1",
            "wf-2",
        ]

    def test_project_query_service_hides_global_workflow_bindings_from_foreign_account(self):
        foreign_account_id = "11111111-1111-1111-1111-111111111111"
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": foreign_account_id,
            "id": 7,
            "name": "Dormant tenant project",
        }
        bl.get_project_workflows.return_value = [
            {"workflow_id": "global-workflow", "enabled": True, "is_default": True}
        ]
        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_workflow_bindings(
            7,
            scope=ScopeContext(
                account_id=foreign_account_id,
                actor_id="foreign-admin",
                principal_type="admin",
            ),
        ).dump()

        assert payload["operator_default_workflow_id"] == ""
        assert payload["workflow_ids"] == []
        assert payload["project_workflows"] == []
        bl.get_project_workflows.assert_not_called()

    def test_project_query_service_get_policy_returns_stable_payload(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.get_project_policy.return_value = {"autonomy_mode": "supervised"}

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.get_policy(7).dump()

        assert payload["project_id"] == "7"
        assert payload["policy"]["autonomy_mode"] == "supervised"
        for removed_key in ("checks", "quality_check_types", "resolved_quality_checks"):
            assert removed_key not in payload

    def test_project_query_service_list_history_returns_revisions(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.list_project_settings_revisions.return_value = [
            {
                "id": 11,
                "project_id": "7",
                "author": "alice",
                "action": "save",
                "source": "ui",
                "created_at": now,
            },
            {
                "id": 10,
                "project_id": "7",
                "author": "bob",
                "action": "create",
                "source": "api",
                "created_at": now,
            },
        ]

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.list_history(7, limit=25).dump()

        assert payload["project_id"] == "7"
        assert [row["id"] for row in payload["revisions"]] == [11, 10]

    def test_project_query_service_get_history_diff_builds_payload(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.get_project_settings_revision.return_value = {
            "id": 11,
            "project_id": "7",
            "author": "alice",
            "action": "save",
            "source": "ui",
            "created_at": now,
            "snapshot_jsonb": {"project": {"description": "Before"}},
        }
        bl.get_project_settings_snapshot.return_value = {"project": {"description": "After"}}
        diff_helper = MagicMock(
            return_value={
                "summary": {"total": 1, "added": 0, "removed": 0, "changed": 1},
                "changes": [
                    {
                        "path": "project.description",
                        "change": "changed",
                        "from": "Before",
                        "to": "After",
                    }
                ],
            }
        )

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
            to_json_obj=lambda value: value,
            project_settings_diff=diff_helper,
        )

        payload = service.get_history_diff(7, 11).dump()

        assert payload["project_id"] == "7"
        assert payload["selected_revision"]["id"] == "11"
        assert payload["summary"]["total"] == 1
        assert payload["changes"][0]["path"] == "project.description"
        diff_helper.assert_called_once_with(
            {"project": {"description": "Before"}}, {"project": {"description": "After"}}
        )

    def test_project_query_service_list_scheduled_jobs_returns_jobs_and_runs(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
            "schedule_timezone": "Europe/Vilnius",
        }
        bl.list_scheduled_jobs.return_value = [
            {
                "id": 1,
                "project_id": "7",
                "workflow_id": "wf-1",
                "next_run_at": datetime(2026, 3, 26, 0, 0, tzinfo=UTC),
                "last_run_at": datetime(2026, 3, 25, 0, 0, tzinfo=UTC),
            }
        ]
        bl.list_scheduled_job_runs.return_value = [
            {
                "id": 10,
                "scheduled_job_id": 1,
                "status": "ok",
                "created_at": datetime(2026, 3, 25, 12, 0, tzinfo=UTC),
            }
        ]

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
        )

        payload = service.list_scheduled_jobs(7, limit_runs=20).dump()

        assert payload["project_id"] == "7"
        assert payload["timezone_context"]["project_schedule_timezone"] == "Europe/Vilnius"
        assert payload["timezone_context"]["display_timezone"] == "UTC"
        assert payload["jobs"][0]["workflow_id"] == "wf-1"
        assert payload["jobs"][0]["next_run_at_utc"] == "2026-03-26T00:00:00Z"
        assert payload["jobs"][0]["next_run_at_project_local"].endswith("EET")
        assert payload["runs"][0]["scheduled_job_id"] == 1
        assert payload["runs"][0]["created_at_utc"] == "2026-03-25T12:00:00Z"

    def test_project_query_service_get_workflow_preview_returns_step_plan(self):
        bl = MagicMock()
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "operator_default_workflow_id": "weekly_report",
            "project_workflows": [
                {"workflow_id": "weekly_report", "enabled": True, "is_default": True}
            ],
        }
        captured: dict[str, Any] = {}

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
            sanitize_external_input=lambda value: str(value).strip(),
            resolve_workflow_selection=lambda requested, _selected="", project_workflows=None: (
                requested,
                "explicit_request",
            ),
            validate_explicit_workflow_override=lambda **kwargs: None,
            build_task_envelope_for_preview=lambda task_summary: {
                "title": task_summary,
                "description": task_summary,
            },
            normalize_orchestrator_state=lambda state: dict(
                state, mode="brownfield", complexity=None, workflow_path=[]
            ),
            plan_workflow_preview=lambda envelope, state: (
                captured.update({"state": dict(state)}),
                {
                    "workflow_id": "weekly_report",
                    "classification": {"confidence": 0.9, "reasoning": "explicit"},
                    "routing": {"reason": "explicit preview", "signals": {}},
                    "steps": [
                        {
                            "step_id": "collect",
                            "step_kind": "agent",
                            "display_name": "Collect findings",
                            "agent": "scout",
                            "skill": "collect_findings",
                            "group_id": None,
                        }
                    ],
                },
            )[1],
        )

        payload = service.get_workflow_preview(
            7,
            task_summary="Fix typo",
            workflow_id="weekly_report",
        ).dump()

        assert payload["project_id"] == "7"
        assert payload["requested_workflow_id"] == "weekly_report"
        assert payload["selected_workflow_id"] == "weekly_report"
        assert payload["selection_source"] == "explicit_request"
        assert payload["step_count"] == 1
        assert payload["step_plan"][0]["step_id"] == "collect"
        assert payload["step_plan"][0]["agent"] == "scout"
        assert payload["selection_reason"] == "explicit preview"
        assert payload["classification"]["confidence"] == 0.9
        state = captured.get("state") or {}
        assert state["project_name"] == "Demo"
        assert "container_root" not in state
        assert "project_root" not in state
        assert state["mode"] == "brownfield"
        assert state["workflow_path"] == []
        assert state["workflow_id"] == "weekly_report"

    def test_project_query_service_get_project_detail_builds_page_context(self):
        bl = MagicMock()
        bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
            "agents_config": {},
        }
        bl.count_jobs.return_value = 51
        bl.get_jobs.return_value = [{"id": 201, "task_summary": "Fix auth"}]
        bl.get_project_workflows.return_value = [
            {"workflow_id": "wf-1", "enabled": True, "is_default": True}
        ]
        bl.get_project_run_config.return_value = {
            "project_name": "Demo",
            "project_policy": {"autonomy_mode": "supervised"},
        }
        bl.list_scheduled_jobs.return_value = [{"id": 1, "project_id": "7", "workflow_id": "wf-1"}]
        bl.list_scheduled_job_runs.return_value = [
            {"id": 10, "scheduled_job_id": 1, "status": "ok"}
        ]

        service = ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda row: row,
            get_queue_depth=lambda _project_id: 0,
            get_ready_queue_depth=lambda: 0,
            list_workflow_choices=lambda **kwargs: [
                {"workflow_id": "wf-1", "display_name": "Standard Feature"}
            ],
        )
        payload = service.get_project_detail(7, page=2, per_page=25).dump()

        assert payload["project"]["operator_default_workflow_id"] == "wf-1"
        assert payload["default_workflow"]["display_name"] == "Standard Feature"
        assert payload["page"] == 2
        assert payload["per_page"] == 25
        assert payload["total_jobs"] == 51
        assert payload["total_pages"] == 3
        assert payload["scheduled_jobs"][0]["id"] == 1
        assert payload["scheduled_runs"][0]["scheduled_job_id"] == 1
        assert "index_status" not in payload
        bl.get_jobs.assert_called_once_with("7", limit=25, offset=25)

    def test_approval_query_service_get_current_gate_returns_interrupt_payload(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        state = MagicMock()
        state.tasks = [
            MagicMock(interrupts=[MagicMock(value={"stage": "review", "question": "Approve?"})])
        ]

        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: state,
            approval_fallback_payload=lambda job: {"stage": "fallback", "task_id": job.get("id")},
            to_json_obj=lambda value: value,
        )

        payload = service.get_current_gate(42).dump()

        assert payload["job_id"] == "42"
        assert payload["thread_id"] == "thread-42"
        assert payload["project_id"] == "7"
        assert payload["payload"]["stage"] == "review"

    def test_approval_query_service_uses_separate_project_lookup_port(self):
        job_store = MagicMock()
        job_store.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
        }
        get_project = MagicMock(
            return_value={"id": "7", "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        )
        state = MagicMock()
        state.tasks = [MagicMock(interrupts=[MagicMock(value={"stage": "review"})])]
        service = ApprovalQueryService(
            get_logger=lambda: job_store,
            get_project_by_id=get_project,
            get_thread_state_snapshot=lambda _thread_id: state,
            approval_fallback_payload=lambda _job: {},
            to_json_obj=lambda value: value,
        )

        payload = service.get_current_gate(42).dump()

        assert payload["project_id"] == "7"
        get_project.assert_called_once_with("7")

    def test_approval_query_service_redacts_interrupt_payload_for_token_scope(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        state = MagicMock()
        state.tasks = [
            MagicMock(
                interrupts=[
                    MagicMock(
                        value={
                            "approval_id": "gate-42",
                            "message": "Approve release",
                            "allowed_actions": ["approve", "reject"],
                            "content": {"summary": "Safe summary", "secret": "do-not-leak"},
                            "checkpoint": {"raw": "do-not-leak"},
                        }
                    )
                ]
            )
        ]
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: state,
            approval_fallback_payload=lambda _job: {},
            to_json_obj=lambda value: value,
        )

        payload = service.get_current_gate(
            42,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="machine-1",
                principal_type="service_account",
                auth_source="machine",
            ),
        ).dump()

        assert payload["payload"] == {
            "approval_id": "gate-42",
            "message": "Approve release",
            "allowed_actions": ["approve", "reject"],
            "content": {"summary": "Safe summary"},
        }

    def test_token_approval_query_bounds_every_public_string(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        state = MagicMock()
        state.tasks = [
            MagicMock(
                interrupts=[
                    MagicMock(
                        value={
                            "approval_id": "gate-42",
                            "message": "m" * 5000,
                            "stage": "s" * 500,
                            "step_id": "x" * 500,
                            "group_id": "g" * 500,
                            "content": {"summary": "c" * 5000},
                        }
                    )
                ]
            )
        ]
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: state,
            approval_fallback_payload=lambda _job: {},
            to_json_obj=lambda value: value,
        )

        payload = service.get_current_gate(
            42,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="machine-1",
                principal_type="service_account",
                auth_source="machine",
            ),
        ).dump()["payload"]

        assert len(payload["message"]) == 1000
        assert len(payload["stage"]) == 128
        assert len(payload["step_id"]) == 255
        assert len(payload["group_id"]) == 255
        assert len(payload["content"]["summary"]) == 1000

    def test_token_approval_query_fails_when_checkpoint_cannot_supply_approval_id(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: None,
            approval_fallback_payload=lambda _job: {"message": "legacy fallback"},
            to_json_obj=lambda value: value,
        )

        with pytest.raises(ServiceUnavailableError, match="Current approval gate is unavailable"):
            service.get_current_gate(
                42,
                scope=ScopeContext(
                    account_id=DEFAULT_ACCOUNT_ID,
                    actor_id="machine-1",
                    principal_type="service_account",
                    auth_source="machine",
                ),
            )

    def test_approval_query_service_list_history_returns_meta_entries(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "completed",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
            "meta": {"approvals": [{"stage": "review", "approved": True}]},
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}

        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: None,
            approval_fallback_payload=lambda _job: {},
        )

        payload = service.list_history(42).dump()

        assert payload["job_id"] == "42"
        assert payload["approvals"] == [{"stage": "review", "approved": True}]

    @pytest.mark.parametrize(
        ("principal_type", "auth_source"),
        [("user", "session"), ("service_account", "machine")],
        ids=["browser", "token"],
    )
    def test_approval_history_excludes_private_retry_context_preserving_public_decision(
        self, principal_type, auth_source
    ):
        public_entry = {
            "approval_id": "gate-42",
            "stage": "Release review",
            "question": "Retry this publication?",
            "content_preview": "The publication response was lost.",
            "approved": False,
            "action": "reject",
            "comment": "Retry the same publication after checking the destination.",
            "override_content_preview": "",
            "actor": "release-operator",
            "actor_type": "user",
            "timestamp": "2026-10-18T19:00:00+00:00",
            "retry_ordinal": 1,
            "external_actor": {"id": "reviewer-7", "display_name": "Release reviewer"},
        }
        private_context = {
            "external_effect_resolution_ref": {
                "command_id": "private-command",
                "effect_id": "private-effect",
                "fenced_version": 4,
                "approval_id": "gate-42",
            },
            "external_effect": {"effect_id": "private-effect", "claim_version": 3},
            "effect_id": "private-effect",
            "source_ref": {"job_id": "42", "step_execution_id": "private-execution"},
            "source_ref_jsonb": {"idempotency_key": "private-original-intent"},
            "claim_version": 3,
            "fenced_version": 4,
            "pinned_definition": {"revision": 7, "digest": "private-definition-pin"},
            "approval_context": {"external_effect_resolution_ref": "private-carrier"},
        }
        stored_entry = {**public_entry, **private_context}
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "completed",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
            "meta": {"approvals": [stored_entry]},
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: None,
            approval_fallback_payload=lambda _job: {},
        )

        payload = service.list_history(
            42,
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="reviewer-7",
                principal_type=principal_type,
                auth_source=auth_source,
            ),
        ).dump()

        assert payload["job_id"] == "42"
        assert payload["approvals"] == [public_entry]
        # Projection must not erase the private durable context needed for resume.
        assert stored_entry == {**public_entry, **private_context}

    def test_approval_query_service_denies_workflow_outside_token_allowlist(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
            "selected_workflow": "wf-denied",
            "meta": {"approvals": [{"stage": "review", "approved": True}]},
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: None,
            approval_fallback_payload=lambda _job: {},
        )
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="machine-1",
            principal_type="service_account",
            auth_source="machine",
            allowed_workflow_ids=frozenset({"wf-allowed"}),
        )

        with pytest.raises(ServiceForbiddenError):
            service.get_current_gate(42, scope=scope)
        with pytest.raises(ServiceForbiddenError):
            service.list_history(42, scope=scope)

    def test_approval_query_service_denies_restricted_scope_without_workflow_identity(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: None,
            approval_fallback_payload=lambda _job: {},
        )
        scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="machine-1",
            principal_type="service_account",
            auth_source="machine",
            allowed_workflow_ids=frozenset({"wf-allowed"}),
        )

        with pytest.raises(ServiceForbiddenError):
            service.get_current_gate(42, scope=scope)
        with pytest.raises(ServiceForbiddenError):
            service.list_history(42, scope=scope)

    def test_approval_query_service_rejects_multiple_simultaneous_interrupts(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "awaiting_approval",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
        }
        bl.get_project.return_value = {"id": 7, "name": "Demo", "account_id": DEFAULT_ACCOUNT_ID}
        state = MagicMock()
        state.tasks = [
            MagicMock(interrupts=[MagicMock(value={"id": "gate-a"})]),
            MagicMock(interrupts=[MagicMock(value={"id": "gate-b"})]),
        ]
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: state,
            approval_fallback_payload=lambda _job: {},
        )

        with pytest.raises(ServiceConflictError) as exc_info:
            service.get_current_gate(42)

        assert exc_info.value.payload["code"] == "multiple_approval_gates_unsupported"
        assert exc_info.value.payload["error"] == (
            "Multiple simultaneous approval gates are not supported"
        )

    def test_approval_query_service_identifies_job_without_active_gate(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "status": "completed",
            "project_id": None,
        }
        service = ApprovalQueryService(
            get_logger=lambda: bl,
            get_thread_state_snapshot=lambda _thread_id: None,
            approval_fallback_payload=lambda _job: {},
        )

        with pytest.raises(ServiceConflictError) as exc_info:
            service.get_current_gate(42)

        assert exc_info.value.payload["code"] == "approval_not_awaiting"

    def test_event_query_service_detail_includes_dispatches_and_commands(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_domain_event.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 9,
            "event_id": "evt-9",
            "event_type": "workflow.failed",
            "severity": "error",
            "status": "recorded",
            "summary": "Workflow failed",
            "payload": {"detail": "stack"},
            "actor": {"username": "system"},
            "urls": {"job": "/jobs/9"},
            "envelope": {"source": "test"},
            "source": "workflow",
            "aggregate_type": "job",
            "aggregate_id": "9",
            "correlation_id": "corr-9",
            "project_id": "7",
            "job_id": 9,
            "occurred_at": now,
            "updated_at": now,
        }
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.list_event_dispatches.return_value = [
            {
                "id": 1,
                "dispatch_id": "dispatch-1",
                "target_type": "notification_route",
                "target_id": "route-1",
                "status": "failed",
                "reason_code": "retryable",
                "attempt_count": 1,
                "max_attempts": 3,
                "created_at": now,
            }
        ]
        bl.list_event_delivery_attempts_by_dispatch_ids.return_value = {
            "dispatch-1": [
                {
                    "id": 2,
                    "dispatch_id": "dispatch-1",
                    "status": "failed",
                    "handler": "fixture_notifier",
                    "error_text": "timeout",
                    "attempt_no": 1,
                    "created_at": now,
                    "started_at": now,
                }
            ]
        }
        bl.list_command_executions.return_value = [
            {
                "command_id": "cmd-1",
                "command_type": "resolve_approval",
                "status": "accepted",
            }
        ]

        service = EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda _bl, _project_id, _cache: "Demo",
            resolve_domain_event_by_ref=lambda current_bl, event_ref: current_bl.get_domain_event(
                event_ref
            ),
            serialize_domain_event_summary=lambda row, project_name="": {
                "id": row.get("id"),
                "event_id": row.get("event_id"),
                "event_type": row.get("event_type"),
                "severity": row.get("severity"),
                "status": row.get("status"),
                "title": row.get("summary"),
                "summary": row.get("summary"),
                "detail": (row.get("payload") or {}).get("detail", ""),
                "project_id": row.get("project_id"),
                "project_name": project_name,
                "workflow_id": row.get("workflow_id"),
                "job_id": row.get("job_id"),
                "group_id": row.get("group_id"),
                "step_id": row.get("step_id"),
                "created_at": row.get("occurred_at").isoformat(),
                "updated_at": row.get("updated_at").isoformat(),
            },
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            to_json_obj=lambda value: value,
        )

        payload = service.get_event_detail("evt-9").dump()

        assert payload["event_id"] == "evt-9"
        assert payload["dispatches"][0]["dispatch_id"] == "dispatch-1"
        assert payload["dispatches"][0]["attempts"][0]["handler"] == "fixture_notifier"
        assert payload["commands"][0]["command_id"] == "cmd-1"
        assert payload["commands"][0]["command_type_label"] == "Resolve Approval"
        assert payload["commands"][0]["status_label"] == "Accepted"
        assert bl.list_event_dispatches.call_args.kwargs["account_id"] == DEFAULT_ACCOUNT_ID
        assert bl.list_event_delivery_attempts_by_dispatch_ids.call_args.kwargs == {
            "dispatch_ids": ["dispatch-1"],
            "limit_per_dispatch": 200,
        }
        bl.list_event_delivery_attempts.assert_not_called()

    def test_event_query_service_detail_fails_closed_when_dispatch_lookup_errors(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.get_domain_event.return_value = {
            "id": 9,
            "event_id": "evt-9",
            "event_type": "workflow.failed",
            "severity": "error",
            "status": "failed",
            "summary": "Workflow failed",
            "payload": {"detail": "stack"},
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "7",
            "job_id": 9,
            "occurred_at": now,
            "updated_at": now,
        }
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.list_event_dispatches.side_effect = RuntimeError("db timeout")

        service = EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda _bl, _project_id, _cache: "Demo",
            resolve_domain_event_by_ref=lambda current_bl, event_ref: current_bl.get_domain_event(
                event_ref
            ),
            serialize_domain_event_summary=lambda row, project_name="": {
                "id": row.get("id"),
                "event_id": row.get("event_id"),
                "event_type": row.get("event_type"),
                "severity": row.get("severity"),
                "status": row.get("status"),
                "title": row.get("summary"),
                "summary": row.get("summary"),
                "detail": (row.get("payload") or {}).get("detail", ""),
                "project_id": row.get("project_id"),
                "project_name": project_name,
                "workflow_id": row.get("workflow_id"),
                "job_id": row.get("job_id"),
                "group_id": row.get("group_id"),
                "step_id": row.get("step_id"),
                "created_at": row.get("occurred_at").isoformat(),
                "updated_at": row.get("updated_at").isoformat(),
            },
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            to_json_obj=lambda value: value,
        )

        with pytest.raises(ServiceUnavailableError, match="dispatch lookup failed"):
            service.get_event_detail("evt-9")

    def test_event_query_service_detail_requires_attempt_storage(self):
        now = datetime.now(UTC)
        bl = MagicMock(spec=["get_domain_event", "get_project", "list_event_dispatches"])
        bl.get_domain_event.return_value = {
            "id": 9,
            "event_id": "evt-9",
            "event_type": "workflow.failed",
            "severity": "error",
            "status": "failed",
            "summary": "Workflow failed",
            "payload": {"detail": "stack"},
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "7",
            "job_id": 9,
            "occurred_at": now,
            "updated_at": now,
        }
        bl.get_project.return_value = {"account_id": DEFAULT_ACCOUNT_ID, "id": 7, "name": "Demo"}
        bl.list_event_dispatches.return_value = [
            {
                "id": 1,
                "dispatch_id": "dispatch-1",
                "status": "failed",
                "project_id": "7",
                "account_id": DEFAULT_ACCOUNT_ID,
                "created_at": now,
            }
        ]

        service = EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda _bl, _project_id, _cache: "Demo",
            resolve_domain_event_by_ref=lambda current_bl, event_ref: current_bl.get_domain_event(
                event_ref
            ),
            serialize_domain_event_summary=lambda row, project_name="": {
                "id": row.get("id"),
                "event_id": row.get("event_id"),
                "event_type": row.get("event_type"),
                "severity": row.get("severity"),
                "status": row.get("status"),
                "title": row.get("summary"),
                "summary": row.get("summary"),
                "detail": (row.get("payload") or {}).get("detail", ""),
                "project_id": row.get("project_id"),
                "project_name": project_name,
                "workflow_id": row.get("workflow_id"),
                "job_id": row.get("job_id"),
                "group_id": row.get("group_id"),
                "step_id": row.get("step_id"),
                "created_at": row.get("occurred_at").isoformat(),
                "updated_at": row.get("updated_at").isoformat(),
            },
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            to_json_obj=lambda value: value,
        )

        with pytest.raises(ServiceUnavailableError, match="attempt storage is unavailable"):
            service.get_event_detail("evt-9")

    def test_event_query_service_list_events_scopes_by_account_and_project_membership(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.list_domain_events.side_effect = [
            [
                {
                    "id": 1,
                    "event_id": "evt-1",
                    "event_type": "job.failed",
                    "severity": "error",
                    "status": "recorded",
                    "summary": "Failed in allowed project",
                    "payload": {},
                    "account_id": "acc_alpha",
                    "project_id": "7",
                    "workflow_id": "wf-1",
                    "job_id": 10,
                    "occurred_at": now,
                    "updated_at": now,
                },
                {
                    "id": 2,
                    "event_id": "evt-2",
                    "event_type": "job.failed",
                    "severity": "error",
                    "status": "recorded",
                    "summary": "Failed in blocked project",
                    "payload": {},
                    "account_id": "acc_alpha",
                    "project_id": "9",
                    "workflow_id": "wf-1",
                    "job_id": 11,
                    "occurred_at": now,
                    "updated_at": now,
                },
            ],
            [],
        ]

        service = EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda _bl, _project_id, _cache: "Demo",
            resolve_domain_event_by_ref=lambda current_bl, event_ref: current_bl.get_domain_event(
                event_ref
            ),
            serialize_domain_event_summary=lambda row, project_name="": {
                "id": row.get("id"),
                "event_id": row.get("event_id"),
                "event_type": row.get("event_type"),
                "severity": row.get("severity"),
                "status": row.get("status"),
                "title": row.get("summary"),
                "summary": row.get("summary"),
                "detail": "",
                "project_id": row.get("project_id"),
                "project_name": project_name,
                "workflow_id": row.get("workflow_id"),
                "job_id": row.get("job_id"),
                "group_id": row.get("group_id"),
                "step_id": row.get("step_id"),
                "created_at": row.get("occurred_at").isoformat(),
                "updated_at": row.get("updated_at").isoformat(),
            },
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            to_json_obj=lambda value: value,
        )

        payload = service.list_events(
            scope=ScopeContext(
                account_id="acc_alpha",
                allowed_project_ids=frozenset({"7"}),
            )
        ).dump()

        assert payload["total"] == 1
        assert payload["events"][0]["event_id"] == "evt-1"
        first_call = bl.list_domain_events.call_args_list[0]
        assert first_call.kwargs["account_id"] == "acc_alpha"
        assert first_call.kwargs["project_id"] == "7"

    def test_event_query_service_detail_rejects_cross_account_event(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        event_row = {
            "id": 9,
            "event_id": "evt-9",
            "event_type": "workflow.failed",
            "severity": "error",
            "status": "recorded",
            "summary": "Workflow failed",
            "payload": {"detail": "stack"},
            "account_id": "acc_beta",
            "project_id": "7",
            "workflow_id": "wf-1",
            "job_id": 9,
            "occurred_at": now,
            "updated_at": now,
        }

        def _resolver(_bl, _event_ref, **kwargs):
            assert kwargs["account_id"] == "acc_alpha"
            return event_row

        service = EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda _bl, _project_id, _cache: "Demo",
            resolve_domain_event_by_ref=_resolver,
            serialize_domain_event_summary=lambda row, project_name="": row,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            to_json_obj=lambda value: value,
        )

        with pytest.raises(ServiceForbiddenError, match="Access denied"):
            service.get_event_detail("evt-9", scope=ScopeContext(account_id="acc_alpha"))

    def test_event_query_service_list_workflow_reactions_filters_scope(self):
        bl = MagicMock()
        bl.list_workflow_reactions.return_value = [
            {"reaction_key": "workflow_rule", "event_type": "job.completed", "enabled": True},
            {
                "reaction_key": "scope::group::design::group_rule",
                "event_type": "job.group_completed",
                "enabled": True,
            },
            {
                "reaction_key": "scope::group::analysis::group_rule",
                "event_type": "job.group_completed",
                "enabled": True,
            },
        ]

        service = EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda _bl, _project_id, _cache: "",
            resolve_domain_event_by_ref=lambda current_bl, event_ref: None,
            serialize_domain_event_summary=lambda row, project_name="": row,
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            to_json_obj=lambda value: value,
            workflow_exists=lambda workflow_id: workflow_id == "weekly_report",
        )

        payload = service.list_workflow_reactions(
            "weekly_report",
            scope_kind="group",
            scope_id="design",
            scope=ScopeContext(account_id=DEFAULT_ACCOUNT_ID, workflow_id="weekly_report"),
        ).dump()

        assert payload["workflow_id"] == "weekly_report"
        assert payload["scope_kind"] == "group"
        assert payload["scope_id"] == "design"
        assert len(payload["reactions"]) == 1
        assert payload["reactions"][0]["reaction_key"] == "group_rule"
        assert payload["reactions"][0]["scope_kind"] == "group"
        assert payload["reactions"][0]["scope_id"] == "design"


class TestEventQueryEndpoints:
    def test_project_job_list_v1_and_v2_drop_raw_metadata(self, client):
        payload = ProjectJobsListView.model_validate(
            {
                "jobs": [
                    {
                        "id": "job-1",
                        "project_id": "1",
                        "task_summary": "Run workflow",
                        "status": "queued",
                        "meta": {
                            "execution_envelope": {
                                "inputs": {"credential": "replayable-ciphertext"}
                            }
                        },
                    }
                ],
                "total": 1,
                "page": 1,
                "per_page": 25,
                "total_pages": 1,
            }
        )
        mock_service = MagicMock()
        mock_service.list_project_jobs.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            responses = [
                client.get("/api/projects/1/jobs"),
                client.get("/api/v2/projects/1/jobs"),
            ]

        assert all(response.status_code == 200 for response in responses)
        assert all("replayable-ciphertext" not in response.text for response in responses)
        assert "meta" not in responses[0].json()["jobs"][0]
        assert "meta" not in responses[1].json()["data"]["jobs"][0]

    def test_job_status_endpoint_redacts_secret_reference_invocation(self, client):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": 42,
            "project_id": "1",
            "status": "in_progress",
            "task_summary": "Run workflow",
            "meta": {
                "invocation": {
                    "mapped_inputs": {
                        "credential": {
                            "__type__": "flow-steward.secret-ref.v1",
                            "name": "credential",
                            "ciphertext": "replayable-ciphertext",
                        }
                    }
                }
            },
            "started_at": None,
            "finished_at": None,
        }
        bl.get_project.return_value = {
            "id": "1",
            "name": "Demo",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_job_cost_summary.return_value = {}
        bl.get_project_workflows.return_value = []
        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: service},
        ):
            response = client.get("/api/jobs/42")

        assert response.status_code == 200
        assert response.json()["invocation"]["mapped_inputs"]["credential"] == "[REDACTED]"
        assert "replayable-ciphertext" not in response.text

    def test_api_project_workflow_validate_uses_shared_project_scoped_service_contract(
        self, client
    ):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow operator validate contract test"
            )

        mock_service = MagicMock()
        mock_service.get_workflow_bindings.return_value = ProjectWorkflowBindingsView(
            project_id="1",
            workflow_ids=["wf.weekly_digest"],
            project_workflows=[{"workflow_id": "wf.weekly_digest"}],
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: mock_service},
            ),
            patch(
                "core.application.management.workflow_operator_service.WorkflowOperatorService.workflow_validate",
                return_value=WorkflowValidationResultView(
                    workflow_id="wf.weekly_digest",
                    validation={"valid": False},
                    issues=[],
                ),
            ) as validate_call,
        ):
            resp = client.get("/api/projects/1/workflows/wf.weekly_digest/validate")

        assert resp.status_code == 200
        assert resp.json()["workflow_id"] == "wf.weekly_digest"
        assert resp.json()["validation"]["valid"] is False
        call = mock_service.get_workflow_bindings.call_args
        assert call.args == ("1",)
        assert call.kwargs["scope"].project_id == "1"
        validate_call.assert_called_once_with("wf.weekly_digest", require_available=False)

    def test_api_project_workflow_validate_returns_404_for_unlinked_workflow(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow operator 404 contract test"
            )

        mock_service = MagicMock()
        mock_service.get_workflow_bindings.return_value = ProjectWorkflowBindingsView(
            project_id="1",
            workflow_ids=["wf.other"],
            project_workflows=[{"workflow_id": "wf.other"}],
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: mock_service},
            ),
            patch(
                "core.application.management.workflow_operator_service.WorkflowOperatorService.workflow_validate"
            ) as validate_call,
        ):
            resp = client.get("/api/projects/1/workflows/wf.weekly_digest/validate")

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert body["message"] == "Workflow not linked to project"
        validate_call.assert_not_called()

    def test_api_project_workflow_validate_returns_404_for_stale_registry_link(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow operator stale-link contract test"
            )

        mock_service = MagicMock()
        mock_service.get_workflow_bindings.return_value = ProjectWorkflowBindingsView(
            project_id="1",
            workflow_ids=["wf.weekly_digest"],
            project_workflows=[{"workflow_id": "wf.weekly_digest"}],
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: mock_service},
            ),
            patch(
                "core.application.management.workflow_operator_service.WorkflowOperatorService.workflow_validate",
                side_effect=ServiceNotFoundError("Workflow 'wf.weekly_digest' not found"),
            ) as validate_call,
        ):
            resp = client.get("/api/projects/1/workflows/wf.weekly_digest/validate")

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert body["message"] == "Workflow 'wf.weekly_digest' not found"
        validate_call.assert_called_once_with("wf.weekly_digest", require_available=False)

    def test_api_project_workflow_contract_returns_403_for_forbidden_scope(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow operator 403 contract test"
            )

        mock_service = MagicMock()
        mock_service.get_workflow_bindings.side_effect = ServiceForbiddenError("Access denied")

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/projects/1/workflows/wf.weekly_digest/contract")

        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == "forbidden"
        assert body["message"] == "Access denied"

    def test_api_project_workflows_uses_project_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project workflows service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "project_id": "1",
            "operator_default_workflow_id": "weekly_report",
            "project_workflows": [{"workflow_id": "weekly_report", "is_default": True}],
        }
        mock_service = MagicMock()
        mock_service.get_workflow_bindings.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/projects/1/workflows")

        assert resp.status_code == 200
        assert resp.json()["operator_default_workflow_id"] == "weekly_report"
        call = mock_service.get_workflow_bindings.call_args
        assert call.args == ("1",)
        assert call.kwargs["scope"].project_id == "1"

    def test_api_project_policy_uses_project_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project policy service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "project_id": "1",
            "policy": {"autonomy_mode": "supervised"},
        }
        mock_service = MagicMock()
        mock_service.get_policy.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/projects/1/policy")

        assert resp.status_code == 200
        assert resp.json()["policy"]["autonomy_mode"] == "supervised"
        call = mock_service.get_policy.call_args
        assert call.args == ("1",)
        assert call.kwargs["scope"].project_id == "1"

    def test_api_project_queue_uses_project_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project queue service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "project_id": "1",
            "queue_depth": 3,
            "ready_queue_depth": 1,
            "pending_jobs": [],
            "lease": None,
            "scheduler": {},
        }
        mock_service = MagicMock()
        mock_service.get_project_queue.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/projects/1/queue")

        assert resp.status_code == 200
        assert resp.json()["queue_depth"] == 3
        call = mock_service.get_project_queue.call_args
        assert call.args == ("1",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "1"

    def test_api_project_queue_unexpected_error_uses_api_error_response(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project queue error routing test"
            )

        mock_service = MagicMock()
        mock_service.get_project_queue.side_effect = RuntimeError("queue boom")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.route_registration._api_error_response",
                return_value=api_runtime.JSONResponse({"handled": "api"}, status_code=500),
            ) as api_handler,
        ):
            resp = client.get("/api/projects/1/queue")

        assert resp.status_code == 500
        assert resp.json() == {"handled": "api"}
        api_handler.assert_called_once()

    def test_api_project_jobs_list_service_error_uses_service_error_response(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project jobs list error routing test"
            )

        mock_service = MagicMock()
        mock_service.list_project_jobs.side_effect = ServiceError("jobs unavailable")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.route_registration._service_error_response",
                return_value=api_runtime.JSONResponse({"handled": "service"}, status_code=503),
            ) as service_handler,
        ):
            resp = client.get("/api/projects/1/jobs")

        assert resp.status_code == 503
        assert resp.json() == {"handled": "service"}
        service_handler.assert_called_once()

    def test_api_project_workflow_preview_uses_project_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow preview service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "project_id": "1",
            "selected_workflow_id": "weekly_digest",
            "requested_workflow_id": "weekly_report",
            "selection_source": "explicit_request",
            "step_plan": [{"step_id": "analysis"}],
            "step_count": 1,
        }
        mock_service = MagicMock()
        mock_service.get_workflow_preview.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_query_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/projects/1/workflow-preview",
                json={"task_summary": "Fix typo", "workflow_id": "weekly_report"},
            )

        assert resp.status_code == 200
        assert resp.json()["selected_workflow_id"] == "weekly_digest"
        call = mock_service.get_workflow_preview.call_args
        assert call.args == ("1",)
        assert call.kwargs["task_summary"] == "Fix typo"
        assert call.kwargs["workflow_id"] == "weekly_report"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "1"

    def test_api_extension_health_uses_extension_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension health service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "extension_id": "demo_task_provider",
            "health": {"status": "healthy", "detail": "ok"},
        }
        mock_service = MagicMock()
        mock_service.get_health.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/extensions/demo_task_provider/health")

        assert resp.status_code == 200
        assert resp.json()["health"]["status"] == "healthy"
        call = mock_service.get_health.call_args
        assert call.args == ("demo_task_provider",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_extension_query_uses_extension_binding_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension query service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "extension_id": "fixture.notification-provider",
            "query_id": "get_config",
            "query": {"status": "succeeded", "response": {"ok": True}},
        }
        mock_service = MagicMock()
        mock_service.execute_query.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/extensions/fixture.notification-provider/query/get_config",
                json={"context": {"project_id": "7"}},
            )

        assert resp.status_code == 200
        assert resp.json()["query"]["status"] == "succeeded"
        call = mock_service.execute_query.call_args
        assert call.args[0] == "fixture.notification-provider"
        assert call.args[1] == "get_config"
        assert call.args[2].context == {"project_id": "7"}
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "7"

    def test_api_extensions_scan_uses_extension_binding_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension scan service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "root": "/tmp/extensions",
            "extensions": [],
            "scanned_count": 0,
        }
        mock_service = MagicMock()
        mock_service.scan_extensions.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            resp = client.post("/api/extensions/scan", json={"root": "/tmp/extensions"})

        assert resp.status_code == 200
        call = mock_service.scan_extensions.call_args
        assert call.kwargs["root"] == "/tmp/extensions"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_extensions_list_forwards_state_filter(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension list state filter contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {"extensions": []}
        mock_service = MagicMock()
        mock_service.list_extensions.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/extensions?state=disabled")

        assert resp.status_code == 200
        call = mock_service.list_extensions.call_args
        assert call.kwargs["filters"]["state"] == ["disabled"]
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_extension_install_forwards_upload_to_binding_service(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension install contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "extension": {"extension_key": "vendor_demo", "enabled": True},
            "installed_path": "/tmp/extensions/vendor_demo",
        }
        mock_service = MagicMock()
        mock_service.install_extension_archive.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/extensions/install",
                files={"file": ("vendor_demo.zip", b"PK\x03\x04demo", "application/zip")},
            )

        assert resp.status_code == 200
        call = mock_service.install_extension_archive.call_args
        assert call.kwargs["filename"] == "vendor_demo.zip"
        assert call.kwargs["content"].startswith(b"PK")
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_extension_archive_capability_uses_binding_service(self, client):
        payload = MagicMock()
        payload.dump.return_value = {
            "available": False,
            "reason_code": "managed_root_read_only",
            "message": "Extension archive lifecycle is unavailable in this deployment.",
            "remediation": {
                "install_update": ["./flow-steward extensions install --archive <path>"],
                "uninstall": ["./flow-steward extensions uninstall <extension-id>"],
            },
        }
        mock_service = MagicMock()
        mock_service.archive_lifecycle_availability.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            response = client.get("/api/extensions/capabilities/archive-lifecycle")

        assert response.status_code == 200
        assert response.json() == payload.dump.return_value
        mock_service.archive_lifecycle_availability.assert_called_once_with()

    def test_api_extension_install_checks_capability_before_reading_upload(self, client):
        mock_service = MagicMock()
        read_upload = AsyncMock()
        mock_service.require_archive_lifecycle_available.side_effect = ServicePreconditionError(
            "Extension archive installation and updates are unavailable in this deployment.",
            payload={
                "code": "extension_archive_lifecycle_unavailable",
                "message": (
                    "Extension archive installation and updates are unavailable in this deployment."
                ),
                "details": {"remediation": ["./flow-steward extensions install --archive <path>"]},
            },
        )

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"extension_binding_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.reactions_extensions._read_extension_upload_with_limit",
                read_upload,
            ),
        ):
            response = client.post(
                "/api/extensions/install",
                files={"file": ("vendor_demo.zip", b"PK\x03\x04demo", "application/zip")},
            )

        assert response.status_code == 412
        body = response.json()
        message = "Extension archive installation and updates are unavailable in this deployment."
        assert set(body) == {"status", "code", "message", "details", "correlation_id", "error"}
        assert body["status"] == "error"
        assert body["code"] == "extension_archive_lifecycle_unavailable"
        assert body["message"] == message
        assert body["error"] == message
        assert body["details"] == {
            "remediation": ["./flow-steward extensions install --archive <path>"]
        }
        assert body["correlation_id"]
        mock_service.require_archive_lifecycle_available.assert_called_once_with(
            operation="install_update"
        )
        read_upload.assert_not_awaited()
        mock_service.install_extension_archive.assert_not_called()

    def test_api_extension_enable_uses_lifecycle_command_service(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension enable service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "extension": {"extension_key": "fixture.notification-provider", "enabled": True},
        }
        mock_service = MagicMock()
        mock_service.enable_extension.return_value = payload

        registry = MagicMock()
        with (
            patch(
                "core.application.extensions.command_service.ExtensionCommandService",
                return_value=mock_service,
            ),
            patch(
                "core.application.registry.registry_manager.RegistryManager.instance",
                return_value=registry,
            ),
        ):
            resp = client.post("/api/extensions/fixture.notification-provider/enable")

        assert resp.status_code == 200
        call = mock_service.enable_extension.call_args
        assert call.args == ("fixture.notification-provider",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        registry.reload_all.assert_called_once_with()

    def test_api_extension_action_uses_extension_binding_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension action service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "extension_id": "fixture.notification-provider",
            "action_id": "toggle_route",
            "action": {"status": "succeeded", "response": {"ok": True}},
        }
        mock_service = MagicMock()
        mock_service.execute_action.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/extensions/fixture.notification-provider/action/toggle_route",
                headers={"Idempotency-Key": "direct-action-intent-1"},
                json={
                    "context": {"project_id": "7"},
                    "scope": "row",
                    "item_ids": ["route-1"],
                    "values": {"enabled": True},
                },
            )

        assert resp.status_code == 200
        call = mock_service.execute_action.call_args
        assert call.args[0] == "fixture.notification-provider"
        assert call.args[1] == "toggle_route"
        assert call.args[2].context == {"project_id": "7"}
        assert call.kwargs["idempotency_key"] == "direct-action-intent-1"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "7"

    def test_api_extension_action_surfaces_idempotency_conflict_as_409(self, client):
        from core.application.management import ServiceError

        mock_service = MagicMock()
        mock_service.execute_action.side_effect = ServiceError(
            "external_effect_idempotency_conflict",
            status_code=409,
            payload={"code": "external_effect_idempotency_conflict"},
        )

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            response = client.post(
                "/api/extensions/fixture.notification-provider/action/deliver_notification",
                headers={"Idempotency-Key": "reused-intent"},
                json={"context": {"project_id": "7"}},
            )

        assert response.status_code == 409
        call = mock_service.execute_action.call_args
        assert call.kwargs["idempotency_key"] == "reused-intent"

    def test_api_extension_invoke_uses_tool_execution_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for extension invoke service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "extension_id": "fixture.notification-provider",
            "invocation": {"status": "succeeded", "response": {"ok": True}},
        }
        mock_service = MagicMock()
        mock_service.invoke_extension.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"tool_execution_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/extensions/fixture.notification-provider/invoke",
                json={"request": {"action": "sync", "context": {"project_id": "7"}}},
            )

        assert resp.status_code == 200
        call = mock_service.invoke_extension.call_args
        assert call.args[0] == "fixture.notification-provider"
        assert call.args[1].request == {"action": "sync", "context": {"project_id": "7"}}
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "7"

    def test_api_projects_list_uses_project_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for projects list api service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "projects": [
                {
                    "id": "7",
                    "name": "Demo Project",
                    "job_count": 3,
                }
            ],
            "total": 31,
            "page": 2,
            "per_page": 25,
            "total_pages": 2,
        }
        mock_service = MagicMock()
        mock_service.list_projects.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/projects?page=2&per_page=25&search=demo")

        assert resp.status_code == 200
        assert resp.json()["total"] == 31
        call = mock_service.list_projects.call_args
        assert call.kwargs["page"] == 2
        assert call.kwargs["per_page"] == 25
        assert call.kwargs["search"] == "demo"
        assert "index_filter" not in call.kwargs
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_projects_list_page_uses_project_query_service_contract(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for projects list page service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "projects": [
                {
                    "id": 7,
                    "name": "Demo Project",
                    "job_count": 3,
                }
            ],
            "total": 1,
            "page": 1,
            "per_page": 200,
            "total_pages": 1,
        }
        mock_service = MagicMock()
        mock_service.list_projects.return_value = payload

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: mock_service},
            ),
            patch.object(api_runtime, "_list_workflow_choices", return_value=[]),
        ):
            resp = client.get("/projects")

        _assert_frontend_shell_response(resp)
        mock_service.list_projects.assert_not_called()

    def test_api_job_approval_uses_approval_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for job approval service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "job_id": 42,
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
            "payload": {"stage": "review"},
        }
        mock_service = MagicMock()
        mock_service.get_current_gate.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"approval_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/42/approval")

        assert resp.status_code == 200
        assert resp.json()["payload"]["stage"] == "review"
        call = mock_service.get_current_gate.call_args
        assert call.args == ("42",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_token_approval_gate_omits_legacy_payload_and_flattens_safe_fields(self):
        from core.entrypoints.api.jobs import _token_approval_gate_response

        assert _token_approval_gate_response(
            {
                "job_id": "42",
                "thread_id": "thread-42",
                "task_summary": "Ship it",
                "project_id": "7",
                "approval_id": "approval-42",
                "allowed_actions": ["approve"],
                "expires_at": "2030-01-01T00:00:00Z",
                "payload": {
                    "approval_id": "stale-gate",
                    "allowed_actions": ["reject"],
                    "expires_at": "1999-01-01T00:00:00Z",
                    "message": "Approve this release",
                    "content": {"summary": "Safe summary"},
                    "technical_details": "must never be exposed",
                },
            }
        ) == {
            "job_id": "42",
            "thread_id": "thread-42",
            "task_summary": "Ship it",
            "project_id": "7",
            "approval_id": "approval-42",
            "allowed_actions": ["approve"],
            "expires_at": "2030-01-01T00:00:00Z",
            "message": "Approve this release",
            "content": {"summary": "Safe summary"},
        }

    def test_api_job_approvals_uses_approval_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for approval history service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "job_id": 42,
            "approvals": [{"stage": "review", "approved": True}],
        }
        mock_service = MagicMock()
        mock_service.list_history.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"approval_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/42/approvals")

        assert resp.status_code == 200
        assert resp.json()["approvals"][0]["stage"] == "review"
        call = mock_service.list_history.call_args
        assert call.args == ("42",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_job_runtime_journal_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for runtime journal service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "job_id": 56,
            "runtime_journal": [{"event": "watchdog_mark"}],
        }
        mock_service = MagicMock()
        mock_service.get_runtime_journal.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/56/runtime-journal")

        assert resp.status_code == 200
        assert resp.json()["runtime_journal"][0]["event"] == "watchdog_mark"
        call = mock_service.get_runtime_journal.call_args
        assert call.args == ("56",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_jobs_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for jobs list service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "jobs": [{"id": 201, "status": "queued"}],
            "total": 12,
            "page": 2,
            "per_page": 25,
            "total_pages": 1,
        }
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get(
                "/api/jobs?page=2&per_page=25&project_id=7&status=queued&status=failed,stuck"
                "&hide_acknowledged=true"
            )

        assert resp.status_code == 200
        assert resp.json()["total"] == 12
        call = mock_service.list_jobs.call_args
        assert call.kwargs["page"] == 2
        assert call.kwargs["per_page"] == 25
        assert call.kwargs["project_id"] == "7"
        assert call.kwargs["status"] == ["queued", "failed,stuck"]
        assert call.kwargs["hide_acknowledged"] is True
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "7"

    def test_api_jobs_awaiting_approval_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for awaiting approval jobs service contract test"
            )

        payload = [MagicMock(dump=lambda: {"id": 42, "status": "awaiting_approval"})]
        mock_service = MagicMock()
        mock_service.list_awaiting_approval_jobs.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/awaiting-approval")

        assert resp.status_code == 200
        assert resp.json()[0]["status"] == "awaiting_approval"
        call = mock_service.list_awaiting_approval_jobs.call_args
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_job_status_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for job status service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {"id": 42, "status": "in_progress", "thread_id": "thread-42"}
        mock_service = MagicMock()
        mock_service.get_job_status = AsyncMock(return_value=payload)

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/42")

        assert resp.status_code == 200
        assert resp.json()["thread_id"] == "thread-42"
        call = mock_service.get_job_status.call_args
        assert call.args == ("42",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_job_trace_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for job trace service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "trace_id": "job:42",
            "job_id": 42,
            "account_id": DEFAULT_ACCOUNT_ID,
            "root_span_id": "job:42:root",
            "spans": [],
        }
        mock_service = MagicMock()
        mock_service.get_trace.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/42/trace?limit=321")

        assert resp.status_code == 200
        assert resp.json()["trace_id"] == "job:42"
        call = mock_service.get_trace.call_args
        assert call.args == ("42",)
        assert call.kwargs["limit"] == 321
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_job_trace_excludes_extension_rows_from_another_job(self, client):
        from core.infrastructure.db.logger_trace_mixin import PlatformLoggerTraceMixin

        class _TraceStore(PlatformLoggerTraceMixin):
            def get_project(self, _project_id):
                return {
                    "id": "project-current",
                    "name": "Current project",
                    "account_id": DEFAULT_ACCOUNT_ID,
                }

            def get_logs(self, *_args, **_kwargs):
                return []

            def get_events(self, *_args, **_kwargs):
                return []

            def get_agent_messages(self, *_args, **_kwargs):
                return []

            def list_extension_invocations(self, *_args, **_kwargs):
                return [
                    {
                        "id": "inv-current",
                        "job_id": "job-current",
                        "invocation_type": "action:current",
                        "status": "ok",
                        "request_context_jsonb": {},
                    },
                    {
                        "id": "inv-foreign",
                        "job_id": "job-foreign",
                        "invocation_type": "action:foreign",
                        "status": "ok",
                        "request_context_jsonb": {},
                    },
                ]

            def list_domain_events(self, *_args, **_kwargs):
                return []

            def list_event_dispatches(self, *_args, **_kwargs):
                return []

            def list_job_errors(self, *_args, **_kwargs):
                return []

        job_store = MagicMock()
        job_store.get_job.return_value = {
            "id": "job-current",
            "project_id": "project-current",
            "account_id": DEFAULT_ACCOUNT_ID,
            "task_id": "trace-current",
            "status": "completed",
        }
        job_store.get_project.return_value = {
            "id": "project-current",
            "name": "Current project",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        service = JobQueryService(
            get_logger=lambda: job_store,
            get_trace_store=_TraceStore,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: service},
        ):
            response = client.get("/api/jobs/job-current/trace")

        assert response.status_code == 200
        extension_spans = [
            span for span in response.json()["spans"] if span["layer"] == "extension"
        ]
        assert [span["attributes"]["invocation_id"] for span in extension_spans] == ["inv-current"]

    def test_api_job_messages_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for job messages service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "messages": [{"id": 201, "summary": "ok"}],
            "last_id": 201,
            "has_more": False,
        }
        mock_service = MagicMock()
        mock_service.list_messages.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get(
                "/api/jobs/99/messages",
                params={
                    "since_id": 100,
                    "before_id": 500,
                    "newest": 1,
                    "step_id": "proofreader",
                    "producer": "reviewer",
                    "skill": "check",
                    "status": "pass",
                    "limit": 2,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["messages"][0]["id"] == 201
        call = mock_service.list_messages.call_args
        assert call.args == ("99",)
        assert call.kwargs["since_id"] == "100"
        assert call.kwargs["before_id"] == "500"
        assert call.kwargs["newest"] is True
        assert call.kwargs["step_id"] == "proofreader"
        assert call.kwargs["producer"] == "reviewer"
        assert call.kwargs["skill"] == "check"
        assert call.kwargs["status"] == "pass"
        assert call.kwargs["limit"] == 2
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_message_detail_service_error_uses_service_error_response(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for message detail error routing test"
            )

        mock_service = MagicMock()
        mock_service.get_message_detail.side_effect = ServiceError("message not available")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.route_registration._service_error_response",
                return_value=api_runtime.JSONResponse({"handled": "service"}, status_code=503),
            ) as service_handler,
        ):
            resp = client.get("/api/messages/501")

        assert resp.status_code == 503
        assert resp.json() == {"handled": "service"}
        service_handler.assert_called_once()

    def test_api_message_detail_unexpected_error_uses_api_error_response(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for message detail api error routing test"
            )

        mock_service = MagicMock()
        mock_service.get_message_detail.side_effect = RuntimeError("message boom")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.route_registration._api_error_response",
                return_value=api_runtime.JSONResponse({"handled": "api"}, status_code=500),
            ) as api_handler,
        ):
            resp = client.get("/api/messages/501")

        assert resp.status_code == 500
        assert resp.json() == {"handled": "api"}
        api_handler.assert_called_once()

    def test_api_job_replay_step_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for replay step service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "message": {"id": 900},
            "routing_decisions": [{"id": 701}],
            "related_messages": [],
        }
        mock_service = MagicMock()
        mock_service.get_replay_step.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/42/replay-step", params={"message_id": 900})

        assert resp.status_code == 200
        assert resp.json()["message"]["id"] == 900
        call = mock_service.get_replay_step.call_args
        assert call.kwargs["job_id"] == "42"
        assert call.kwargs["message_id"] == "900"
        assert call.kwargs["correlation_id"] == ""
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_create_job_decision_override_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc,
                context="api_runtime import for decision override command service contract test",
            )

        payload = MagicMock()
        payload.job_id = 42
        payload.override = {"id": 7, "domain": "workflow_selection"}
        mock_service = MagicMock()
        mock_service.create_decision_override.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/jobs/42/decision-overrides",
                json={
                    "domain": "workflow_selection",
                    "override_value": {"workflow_id": "wf-1"},
                    "reason": "Use alternate workflow",
                    "workflow_id": "wf-1",
                },
            )

        assert resp.status_code == 200
        call = mock_service.create_decision_override.call_args
        assert call.args[0] == "42"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].workflow_id == "wf-1"

    def test_api_job_delete_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for job delete service contract test"
            )

        payload = MagicMock()
        payload.status = "deleted"
        payload.job_id = 42
        mock_service = MagicMock()
        mock_service.delete_job.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.delete("/api/jobs/42")

        assert resp.status_code == 200
        call = mock_service.delete_job.call_args
        assert call.args == ("42",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_project_jobs_bulk_delete_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project jobs bulk delete service contract test"
            )

        payload = MagicMock()
        payload.status = "ok"
        payload.requested = [10, 11]
        payload.deleted = [10]
        payload.deleted_count = 1
        payload.skipped = [{"job_id": 11, "reason": "wrong_project"}]
        mock_service = MagicMock()
        mock_service.bulk_delete_project_jobs.return_value = payload
        session_user = {
            "id": 18,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._csrf_request_valid",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=session_user,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"db": lambda: MagicMock()},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_command_service": lambda: mock_service},
            ),
        ):
            resp = client.post(
                "/api/projects/7/jobs/bulk-delete",
                headers={"Authorization": "Bearer token-123"},
                json={"ids": [10, 11], "confirm": "DELETE"},
            )

        assert resp.status_code == 200
        call = mock_service.bulk_delete_project_jobs.call_args
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].project_id == "7"

    def test_api_project_jobs_bulk_delete_unexpected_error_uses_api_error_response(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc,
                context="api_runtime import for project jobs bulk delete api error routing test",
            )

        mock_service = MagicMock()
        mock_service.bulk_delete_project_jobs.side_effect = RuntimeError("bulk delete boom")
        session_user = {
            "id": 18,
            "username": "admin@example.com",
            "display_name": "Admin",
            "is_active": True,
            "roles": ["admin"],
        }

        with (
            patch.object(client.app.state, "local_auth_enabled_override", return_value=True),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._csrf_request_valid",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=session_user,
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"db": lambda: MagicMock()},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_command_service": lambda: mock_service},
            ),
            patch(
                "core.entrypoints.api.route_registration._api_error_response",
                return_value=api_runtime.JSONResponse({"handled": "api"}, status_code=500),
            ) as api_handler,
        ):
            resp = client.post(
                "/api/projects/7/jobs/bulk-delete",
                headers={"Authorization": "Bearer token-123"},
                json={"ids": [10], "confirm": "DELETE"},
            )

        assert resp.status_code == 500
        assert resp.json() == {"handled": "api"}
        api_handler.assert_called_once()

    def test_api_job_replay_step_execute_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for replay execute service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {"job_id": 42, "message_id": 900, "replay_id": 5}
        mock_service = MagicMock()
        mock_service.execute_step_replay.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/jobs/42/replay-step/execute",
                json={"message_id": 900, "requested_by": "alice", "replay_mode": "step_reexecute"},
            )

        assert resp.status_code == 200
        call = mock_service.execute_step_replay.call_args
        assert call.args[0] == "42"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_job_retry_step_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for retry step service contract test"
            )

        payload = MagicMock()
        payload.job_id = 42
        payload.status = "pending"
        payload.enqueued = True
        payload.thread_id = "thread-42"
        payload.queue_name = "platform.project.1"
        mock_service = MagicMock()
        mock_service.retry_interrupted_step.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.post("/api/jobs/42/retry-step", json={"requested_by": "alice"})

        assert resp.status_code == 200
        call = mock_service.retry_interrupted_step.call_args
        assert call.args[0] == "42"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_job_acknowledge_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for acknowledge service contract test"
            )

        payload = MagicMock()
        payload.status = "acknowledged"
        payload.job_id = 42
        mock_service = MagicMock()
        mock_service.acknowledge_job.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.post("/api/jobs/42/acknowledge")

        assert resp.status_code == 200
        call = mock_service.acknowledge_job.call_args
        assert call.args[0] == "42"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_job_unacknowledge_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for unacknowledge service contract test"
            )

        payload = MagicMock()
        payload.status = "unacknowledged"
        payload.job_id = 42
        mock_service = MagicMock()
        mock_service.unacknowledge_job.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.post("/api/jobs/42/unacknowledge")

        assert resp.status_code == 200
        call = mock_service.unacknowledge_job.call_args
        assert call.args == ("42",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_job_cancel_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for cancel service contract test"
            )

        payload = MagicMock()
        payload.project_id = 7
        mock_service = MagicMock()
        mock_service.cancel_job.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.post("/api/jobs/42/cancel", headers={"Accept": "application/json"})

        assert resp.status_code == 200
        call = mock_service.cancel_job.call_args
        assert call.args[0] == "42"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_job_restart_uses_job_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for restart service contract test"
            )

        payload = MagicMock()
        payload.new_job_id = 88
        mock_service = MagicMock()
        mock_service.restart_job.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/jobs/42/restart",
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )

        assert resp.status_code == 200
        assert resp.json() == {"job_id": "42", "new_job_id": 88}
        call = mock_service.restart_job.call_args
        assert call.args == ("42",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_tool_routes_list_tools_uses_tool_query_service_contract(self):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="tool routes import for list_tools service contract test"
            )

        payload = MagicMock()
        payload.tools = [
            MagicMock(dump=MagicMock(return_value={"tool_id": "search_tool", "category": "search"}))
        ]
        mock_service = MagicMock()
        mock_service.list_tools.return_value = payload

        with patch.object(tool_routes, "_tool_query_service", return_value=mock_service):
            result = tool_routes.list_tools(
                request=_fake_request(),
                enabled_only=True,
                category="search",
                search=None,
            )

        assert result == [{"tool_id": "search_tool", "category": "search"}]
        call = mock_service.list_tools.call_args
        assert call.kwargs["enabled_only"] is True
        assert call.kwargs["category"] == "search"
        assert call.kwargs["search"] == ""
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_tool_routes_contract_validation_uses_tool_query_service_contract(self):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="tool routes import for validation service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "tool_id": "search_tool",
            "validation": {"valid": True, "errors": [], "warnings": []},
        }
        mock_service = MagicMock()
        mock_service.get_contract_validation.return_value = payload

        with patch.object(tool_routes, "_tool_query_service", return_value=mock_service):
            result = tool_routes.get_tool_contract_validation(
                "search_tool", request=_fake_request()
            )

        assert result["tool_id"] == "search_tool"
        call = mock_service.get_contract_validation.call_args
        assert call.args == ("search_tool",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_tool_routes_diff_uses_tool_query_service_contract(self):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="tool routes import for diff service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "entity_type": "tool",
            "entity_id": "search_tool",
            "changes": [],
            "summary": {"total": 0, "added": 0, "removed": 0, "changed": 0},
        }
        mock_service = MagicMock()
        mock_service.get_diff.return_value = payload

        with patch.object(tool_routes, "_tool_query_service", return_value=mock_service):
            result = tool_routes.get_tool_diff(
                "search_tool",
                request=_fake_request(),
                from_entry_id=7,
                to_entry_id=11,
            )

        assert result["entity_id"] == "search_tool"
        call = mock_service.get_diff.call_args
        assert call.args == ("search_tool",)
        assert call.kwargs["from_entry_id"] == 7
        assert call.kwargs["to_entry_id"] == 11
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_tool_routes_create_tool_uses_tool_command_service_contract(self):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="tool routes import for create service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {"status": "created", "tool_id": "search_tool"}
        mock_service = MagicMock()
        mock_service.create_tool.return_value = payload

        with patch.object(tool_routes, "_tool_command_service", return_value=mock_service):
            result = tool_routes.create_tool(
                tool_routes.ToolSave(tool_id="search_tool", display_name="Search Tool"),
                request=_fake_request(),
            )

        assert result["tool_id"] == "search_tool"
        call = mock_service.create_tool.call_args
        assert call.args[0].tool_id == "search_tool"
        assert call.kwargs["author"] == "admin"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_tool_routes_toggle_tool_uses_tool_command_service_contract(self):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="tool routes import for toggle service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {"status": "toggled", "tool_id": "search_tool", "enabled": True}
        mock_service = MagicMock()
        mock_service.toggle_tool.return_value = payload

        with patch.object(tool_routes, "_tool_command_service", return_value=mock_service):
            result = tool_routes.toggle_tool("search_tool", request=_fake_request())

        assert result["status"] == "toggled"
        call = mock_service.toggle_tool.call_args
        assert call.args == ("search_tool",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_tool_routes_restore_revision_uses_tool_command_service_contract(self):
        try:
            import core.entrypoints.admin.tools as tool_routes
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="tool routes import for restore service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "status": "restored",
            "tool_id": "search_tool",
            "restored_from_entry_id": 12,
        }
        mock_service = MagicMock()
        mock_service.restore_tool_revision.return_value = payload

        with patch.object(tool_routes, "_tool_command_service", return_value=mock_service):
            result = tool_routes.restore_tool_revision(
                "search_tool",
                tool_routes.RestoreRevisionRequest(entry_id=12),
                request=_fake_request(),
            )

        assert result["status"] == "restored"
        call = mock_service.restore_tool_revision.call_args
        assert call.args[0] == "search_tool"
        assert call.args[1].entry_id == "12"
        assert call.kwargs["author"] == "admin_restore"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].actor_id == "admin"

    def test_job_logs_page_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for job logs page service contract test"
            )

        payload = type(
            "Payload",
            (),
            {
                "job": {
                    "id": 786,
                    "project_id": "1",
                    "status": "completed",
                    "task_source": "manual",
                    "task_id": "task-786",
                    "task_summary": "Some task",
                    "thread_id": "thread-786",
                    "meta": {},
                    "queue_name": None,
                    "started_at": None,
                    "finished_at": None,
                    "duration_ms": None,
                    "mode": None,
                    "error": None,
                },
                "logs": [],
                "events": [],
                "messages": [],
                "log_has_more": False,
                "message_has_more": False,
                "runtime": {},
                "decision_overrides": [],
                "job_errors": [],
                "job_failure_reason": "",
                "project_name": "test-project",
            },
        )()
        mock_service = MagicMock()
        mock_service.get_logs_page.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/jobs/786")

        _assert_frontend_shell_response(resp)
        mock_service.get_logs_page.assert_not_called()

    def test_api_job_reactions_uses_job_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for job reactions service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "job_id": 55,
            "reactions": [{"id": 1, "event_type": "external.ci.failed"}],
        }
        mock_service = MagicMock()
        mock_service.list_reactions.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"job_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/jobs/55/reactions")

        assert resp.status_code == 200
        assert resp.json()["reactions"][0]["event_type"] == "external.ci.failed"
        call = mock_service.list_reactions.call_args
        assert call.args == ("55",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_workflow_reactions_uses_event_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow reactions service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "workflow_id": "weekly_report",
            "scope_kind": "group",
            "scope_id": "design",
            "reactions": [{"reaction_key": "group_rule", "event_type": "job.group_completed"}],
        }
        mock_service = MagicMock()
        mock_service.list_workflow_reactions.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"event_query_service": lambda: mock_service},
        ):
            resp = client.get(
                "/api/workflows/weekly_report/reactions?scope_kind=group&scope_id=design"
            )

        assert resp.status_code == 200
        assert resp.json()["reactions"][0]["reaction_key"] == "group_rule"
        call = mock_service.list_workflow_reactions.call_args
        assert call.args == ("weekly_report",)
        assert call.kwargs["scope_kind"] == "group"
        assert call.kwargs["scope_id"] == "design"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].workflow_id == "weekly_report"

    def test_api_update_workflow_reactions_uses_event_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow reaction update service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "workflow_id": "weekly_report",
            "scope_kind": "group",
            "scope_id": "design",
            "reactions": [
                {"reaction_key": "group_approval_wait", "event_type": "job.approval_waiting"}
            ],
        }
        mock_service = MagicMock()
        mock_service.update_workflow_reactions.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"event_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/workflows/weekly_report/reactions?scope_kind=group&scope_id=design",
                json={
                    "reactions": [
                        {
                            "reaction_key": "group_approval_wait",
                            "display_name": "Notify on approval wait",
                            "event_type": "job.approval_waiting",
                            "conditions": {},
                            "action": {"type": "notify"},
                            "debounce_seconds": 0,
                            "enabled": True,
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        assert resp.json()["reactions"][0]["reaction_key"] == "group_approval_wait"
        call = mock_service.update_workflow_reactions.call_args
        assert call.args[0] == "weekly_report"
        assert call.args[1].reactions[0].reaction_key == "group_approval_wait"
        assert call.kwargs["scope_kind"] == "group"
        assert call.kwargs["scope_id"] == "design"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID
        assert call.kwargs["scope"].workflow_id == "weekly_report"

    def test_api_update_workflow_reactions_passes_project_context_into_scope(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow reaction project scope contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "workflow_id": "weekly_report",
            "scope_kind": "workflow",
            "scope_id": "",
            "project_id": "7",
            "reactions": [],
        }
        mock_service = MagicMock()
        mock_service.update_workflow_reactions.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"event_command_service": lambda: mock_service},
        ):
            resp = client.post(
                "/api/workflows/weekly_report/reactions?project_id=7",
                json={"reactions": []},
            )

        assert resp.status_code == 200
        call = mock_service.update_workflow_reactions.call_args
        assert call.kwargs["scope"].project_id == "7"

    def test_project_edit_form_uses_project_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project edit service contract test"
            )

        payload = type(
            "Payload",
            (),
            {
                "project": {
                    "id": 1,
                    "name": "Demo Project",
                    "operator_default_workflow_id": "wf-1",
                    "agents_config": {},
                },
                "workflows": [{"workflow_id": "wf-1", "display_name": "Workflow One"}],
                "project_workflows": [{"workflow_id": "wf-1", "is_default": True}],
                "project_workflow_ids": ["wf-1"],
                "project_policy": {"autonomy_mode": "supervised"},
                "project_settings_revisions": [],
            },
        )()
        mock_service = MagicMock()
        mock_service.get_project_edit_form.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"project_query_service": lambda: mock_service},
        ):
            resp = client.get("/projects/1")

        _assert_frontend_shell_response(resp)
        mock_service.get_project_edit_form.assert_not_called()

    def test_project_history_list_html_and_api_share_revision_ids(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project history list parity test"
            )

        now = datetime(2026, 3, 18, 8, 0, tzinfo=UTC)
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": KB_PROJECT_UUID,
            "name": "Demo Project",
        }
        mock_bl.list_project_settings_revisions.return_value = [
            {
                "id": 11,
                "project_id": "1",
                "author": "alice",
                "action": "save",
                "source": "ui",
                "created_at": now,
            },
            {
                "id": 10,
                "project_id": "1",
                "author": "bob",
                "action": "create",
                "source": "api",
                "created_at": now,
            },
        ]
        mock_bl.get_project_settings_revision.return_value = None

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            api_resp = client.get("/api/projects/1/history")
            html_resp = client.get("/projects/1?tab=history")

        assert api_resp.status_code == 200
        assert html_resp.status_code == 200
        assert [row["id"] for row in api_resp.json()["revisions"]] == [11, 10]
        _assert_frontend_shell_response(html_resp)

    def test_project_history_diff_html_and_api_share_changed_paths(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project history diff parity test"
            )

        now = datetime(2026, 3, 18, 8, 0, tzinfo=UTC)
        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 1,
            "name": "Demo Project",
        }
        mock_bl.list_project_settings_revisions.return_value = [
            {
                "id": 7,
                "project_id": "1",
                "author": "alice",
                "action": "save",
                "source": "ui",
                "created_at": now,
            },
        ]
        mock_bl.get_project_settings_revision.return_value = {
            "id": 7,
            "project_id": "1",
            "author": "alice",
            "action": "save",
            "source": "ui",
            "created_at": now,
            "snapshot_jsonb": {
                "project": {"description": "Before"},
                "operator_default_workflow_id": "weekly_report",
            },
        }
        mock_bl.get_project_settings_snapshot.return_value = {
            "project": {"description": "After"},
            "operator_default_workflow_id": "weekly_digest",
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            api_resp = client.get("/api/projects/1/history/7/diff")
            html_resp = client.get("/projects/1?tab=history&selected_revision_id=7")

        assert api_resp.status_code == 200
        assert html_resp.status_code == 200
        body = api_resp.json()
        changed_paths = {str(item.get("path") or "") for item in (body.get("changes") or [])}
        assert "project.description" in changed_paths
        assert "operator_default_workflow_id" in changed_paths
        _assert_frontend_shell_response(html_resp)

    def test_dashboard_summary_endpoint_returns_range_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for dashboard summary contract test"
            )

        grouped_payload = MagicMock()
        grouped_payload.dump.return_value = {
            "groups": {
                "needs_attention": [],
                "acknowledged": [
                    {
                        "job_id": 18,
                        "project_name": "Demo Project",
                        "task_summary": "Investigate flaky webhook retry",
                        "display_summary": "Investigate flaky webhook retry",
                        "status": "failed",
                        "acknowledged_at": "2026-03-20T10:06:00+00:00",
                    }
                ],
                "running": [],
                "blocked": [],
                "done": [
                    {
                        "job_id": 21,
                        "project_name": "Demo Project",
                        "task_summary": "Run smoke tests",
                        "display_summary": "Run smoke tests",
                        "status": "completed",
                        "finished_at": "2026-03-20T10:05:00+00:00",
                    }
                ],
            }
        }

        query_service = MagicMock()

        def _list_jobs(*, group_by="", **_kwargs):
            # The status-group counters: a status-filtered count query.
            if _kwargs.get("status"):
                return {"jobs": [], "total": 0}
            assert group_by == "attention"
            return grouped_payload

        query_service.list_jobs.side_effect = _list_jobs

        mock_bl = MagicMock()
        mock_bl.list_jobs_for_attention.return_value = [
            {
                "job_id": 21,
                "project_name": "Demo Project",
                "task_summary": "Run smoke tests",
                "status": "completed",
                "selected_workflow": "wf-main",
                "current_agent": "proofreader",
                "started_at": "2026-03-20T10:00:00+00:00",
                "finished_at": "2026-03-20T10:05:00+00:00",
            }
        ]
        mock_bl.get_jobs_cost_summary.return_value = {21: {"cost_usd": 1.25}}

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: query_service},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"db": lambda: mock_bl},
            ),
        ):
            resp = client.get("/api/dashboard/summary?range=7d")

        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "7d"
        assert "hero_kpis" in body
        assert "trends" in body
        assert "groups" in body
        assert "triage_groups" in body
        assert "acknowledged" in body["triage_groups"]
        assert "done" in body["triage_groups"]
        assert body["groups"]["acknowledged"][0]["attention_group"] == "acknowledged"
        assert body["hero_kpis"]["completed"]["label"] == "Completed"
        assert body["groups"]["done"][0]["status_label"] == "Completed successfully"
        assert body.get("metrics_truncated") is False
        assert "/api/dashboard/summary" not in str(body.get("error") or "")
        assert mock_bl.list_jobs_for_attention.call_count == 2
        for call in mock_bl.list_jobs_for_attention.call_args_list:
            kwargs = call.kwargs
            assert kwargs["account_id"] == DEFAULT_ACCOUNT_ID
            assert kwargs["offset"] == 0
            assert kwargs["limit"] >= 1
            assert kwargs["finished_after"] < kwargs["finished_before"]

    def test_dashboard_summary_prefers_dominant_agent_when_current_agent_missing(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for dashboard dominant-agent test"
            )

        now_utc = datetime.now(UTC)
        started_at = (now_utc.replace(microsecond=0) - timedelta(minutes=7)).isoformat()
        finished_at = now_utc.replace(microsecond=0).isoformat()

        grouped_payload = MagicMock()
        grouped_payload.dump.return_value = {
            "groups": {
                "needs_attention": [],
                "running": [],
                "blocked": [],
                "done": [],
            }
        }

        query_service = MagicMock()

        def _list_jobs(*, group_by="", **_kwargs):
            # The status-group counters: a status-filtered count query.
            if _kwargs.get("status"):
                return {"jobs": [], "total": 0}
            assert group_by == "attention"
            return grouped_payload

        query_service.list_jobs.side_effect = _list_jobs

        mock_bl = MagicMock()
        mock_bl.list_jobs_for_attention.return_value = [
            {
                "job_id": 77,
                "project_name": "Demo Project",
                "task_summary": "Implement feature",
                "status": "completed",
                "selected_workflow": "wf-main",
                "current_agent": "",
                "started_at": started_at,
                "finished_at": finished_at,
            }
        ]
        mock_bl.get_jobs_cost_summary.return_value = {
            77: {"cost_usd": 3.5, "dominant_agent": "composer"}
        }

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: query_service},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.get("/api/dashboard/summary?range=7d")

        assert resp.status_code == 200
        body = resp.json()
        agent_rows = ((body.get("trends") or {}).get("spend_breakdown") or {}).get("agent") or []
        assert agent_rows, "dashboard should emit agent spend rows for finished jobs in-range"
        assert agent_rows[0]["name"] == "composer"

    def test_dashboard_fetch_cost_by_job_prefers_bulk_summary(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for dashboard cost bulk test"
            )

        mock_bl = MagicMock()
        mock_bl.get_jobs_cost_summary.return_value = {
            11: {"cost_usd": 1.5, "dominant_agent": "proofreader"},
            "12": {"cost_usd": 2.0, "dominant_agent": "composer"},
        }

        payload = api_runtime._dashboard_fetch_cost_by_job(
            bl=mock_bl,
            rows=[{"job_id": 11}, {"id": 12}, {"job_id": 11}],
        )

        mock_bl.get_jobs_cost_summary.assert_called_once_with(["11", "12"])
        assert payload["11"]["cost_usd"] == 1.5
        assert payload["11"]["dominant_agent"] == "proofreader"
        assert payload["12"]["cost_usd"] == 2.0
        assert payload["12"]["dominant_agent"] == "composer"

    def test_dashboard_fetch_cost_by_job_falls_back_to_single_summary(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for dashboard cost fallback test"
            )

        mock_bl = MagicMock()
        mock_bl.get_jobs_cost_summary.side_effect = RuntimeError("bulk unavailable")
        mock_bl.get_job_cost_summary.side_effect = [
            {"cost_usd": 3.25, "dominant_agent": "designer"},
            RuntimeError("single failed"),
        ]

        payload = api_runtime._dashboard_fetch_cost_by_job(
            bl=mock_bl,
            rows=[{"job_id": 21}, {"job_id": 22}],
        )

        assert payload["21"]["cost_usd"] == 3.25
        assert payload["21"]["dominant_agent"] == "designer"
        assert payload["22"]["cost_usd"] is None
        assert payload["22"]["dominant_agent"] is None

    def test_dashboard_summary_reads_historical_metrics_from_rollup_facts(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for dashboard rollup-facts test"
            )

        now_utc = datetime.now(UTC).replace(microsecond=0)
        started_at = (now_utc - timedelta(days=2, minutes=8)).isoformat()
        finished_at = (now_utc - timedelta(days=2)).isoformat()

        grouped_payload = MagicMock()
        grouped_payload.dump.return_value = {
            "groups": {
                "needs_attention": [],
                "running": [],
                "blocked": [],
                "done": [],
            }
        }

        query_service = MagicMock()

        def _list_jobs(*, group_by="", **_kwargs):
            # The status-group counters: a status-filtered count query.
            if _kwargs.get("status"):
                return {"jobs": [], "total": 0}
            assert group_by == "attention"
            return grouped_payload

        query_service.list_jobs.side_effect = _list_jobs

        mock_bl = MagicMock()
        mock_bl.list_dashboard_job_facts.return_value = [
            {
                "job_id": 101,
                "id": 101,
                "project_id": "1",
                "project_name": "Rollup Project",
                "selected_workflow": "wf-rollup",
                "current_agent": "composer",
                "status": "completed",
                "result": "completed",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": 480.0,
                "cost_usd": 4.25,
            }
        ]
        mock_bl.list_dashboard_daily_rollups.return_value = []
        mock_bl.list_jobs_for_attention.return_value = []

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: query_service},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
        ):
            resp = client.get("/api/dashboard/summary?range=7d")

        assert resp.status_code == 200
        body = resp.json()
        assert body["historical_metrics_source"] == "rollup_facts"
        spend_kpi = (body.get("hero_kpis") or {}).get("spend") or {}
        assert float(spend_kpi.get("value_usd") or 0.0) >= 4.25
        agent_rows = ((body.get("trends") or {}).get("spend_breakdown") or {}).get("agent") or []
        assert agent_rows
        assert agent_rows[0]["name"] == "composer"

    def test_project_detail_and_api_scheduled_jobs_share_job_rows(self, client):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for scheduled jobs parity test"
            )

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 1,
            "name": "Demo Project",
            "agents_config": {},
        }
        mock_bl.count_jobs.return_value = 0
        mock_bl.get_jobs.return_value = []
        mock_bl.get_project_workflows.return_value = []
        mock_bl.get_project_run_config.return_value = {
            "project_name": "Demo Project",
        }
        mock_bl.list_scheduled_jobs.return_value = [
            {
                "id": 7,
                "project_id": "1",
                "workflow_id": "wf-1",
                "task_summary": "Nightly sync",
                "cron_expr": "0 3 * * *",
                "enabled": True,
                "status": "active",
                "next_run_at": datetime(2026, 3, 23, 3, 0, tzinfo=UTC),
            }
        ]
        mock_bl.list_scheduled_job_runs.return_value = [
            {"id": 10, "scheduled_job_id": 7, "status": "ok"}
        ]

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
            ),
            patch.object(api_runtime, "_list_workflow_choices", return_value=[]),
        ):
            api_resp = client.get("/api/projects/1/scheduled-jobs")
            html_resp = client.get("/projects/1")

        assert api_resp.status_code == 200
        assert html_resp.status_code == 200
        body = api_resp.json()
        assert body["jobs"][0]["id"] == 7
        assert body["jobs"][0]["task_summary"] == "Nightly sync"
        assert body["runs"][0]["scheduled_job_id"] == 7
        _assert_frontend_shell_response(html_resp)

    def test_project_list_rest_and_mcp_share_project_rows(self, client, monkeypatch):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project list MCP parity test"
            )
        server = _import_mcp_server(monkeypatch)

        rest_payload = MagicMock()
        rest_payload.dump.return_value = {
            "projects": [
                {
                    "id": "11",
                    "name": "Demo Project",
                    "job_count": 7,
                }
            ],
            "total": 1,
            "page": 1,
            "per_page": 25,
            "total_pages": 1,
        }
        rest_service = MagicMock()
        rest_service.list_projects.return_value = rest_payload
        restricted_scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="mcp-project-reader",
            principal_type="service_account",
            allowed_project_ids=frozenset({"11"}),
        )
        delegated_requests: list[dict[str, object]] = []
        api_tool = importlib.import_module("core.entrypoints.mcp.server_api_tool")
        route_registration = importlib.import_module("core.entrypoints.api.route_registration")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: rest_service},
            ),
            patch.object(route_registration, "_request_scope", return_value=restricted_scope),
            patch.object(
                api_tool,
                "AppApiClient",
                side_effect=lambda: _TestClientBackedAppApiClient(client, delegated_requests),
            ),
            patch.object(api_tool, "_caller_bearer", return_value="restricted-project-token"),
        ):
            api_resp = client.get(
                "/api/projects?page=1&per_page=25",
                headers={"Authorization": "Bearer restricted-project-token"},
            )
            mcp_body = _mcp_data(server.project_list())

        assert api_resp.status_code == 200
        assert rest_service.list_projects.call_count == 2
        for service_call in rest_service.list_projects.call_args_list:
            assert service_call.kwargs["scope"] == restricted_scope
        assert delegated_requests == [
            {
                "method": "GET",
                "path": "/api/projects",
                "bearer": "restricted-project-token",
                "params": {"page": 1, "per_page": 200},
            }
        ]
        body = api_resp.json()
        assert mcp_body == body
        assert body["projects"] == [
            {
                "id": "11",
                "name": "Demo Project",
                "job_count": 7,
            }
        ]

    def test_project_jobs_rest_and_mcp_share_job_rows(self, client, monkeypatch):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for project jobs MCP parity test"
            )
        server = _import_mcp_server(monkeypatch)

        rest_payload = MagicMock()
        rest_payload.dump.return_value = {
            "jobs": [
                {
                    "id": 7,
                    "status": "queued",
                    "duration_ms": 65000,
                    "started_at": "2026-03-22T10:15:00+00:00",
                    "display_summary": "Fix auth flow",
                    "task_summary": "Fix auth flow",
                }
            ],
            "total": 1,
            "page": 1,
            "per_page": 5,
            "total_pages": 1,
        }
        rest_service = MagicMock()
        rest_service.list_project_jobs.return_value = rest_payload
        project_payload = MagicMock()
        project_payload.dump.return_value = {
            "projects": [{"id": "11", "name": "Demo Project"}],
            "total": 1,
            "page": 1,
            "per_page": 200,
            "total_pages": 1,
        }
        project_service = MagicMock()
        project_service.list_projects.return_value = project_payload
        delegated_requests: list[dict[str, object]] = []
        api_tool = importlib.import_module("core.entrypoints.mcp.server_api_tool")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"job_query_service": lambda: rest_service},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: project_service},
            ),
            patch.object(
                api_tool,
                "AppApiClient",
                side_effect=lambda: _TestClientBackedAppApiClient(client, delegated_requests),
            ),
            patch.object(api_tool, "_caller_bearer", return_value="caller-token"),
        ):
            api_resp = client.get("/api/projects/11/jobs?page=1&per_page=5")
            mcp_body = _mcp_data(server.project_jobs("Demo Project", limit=5))

        assert api_resp.status_code == 200
        assert rest_service.list_project_jobs.call_count == 2
        body = api_resp.json()
        assert mcp_body == body
        assert body["jobs"][0]["id"] == 7
        assert delegated_requests[0]["path"] == "/api/projects"
        assert delegated_requests[1]["path"] == "/api/projects/11/jobs"

    def test_workflow_trigger_rest_and_mcp_share_launch_payload(self, client, monkeypatch):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for workflow launch MCP parity test"
            )
        server = _import_mcp_server(monkeypatch)

        rest_payload = MagicMock()
        rest_payload.dump.return_value = {
            "job_id": "77",
            "task_id": "task-77",
            "status": "queued",
            "project_id": PROJECT_UUID,
        }
        rest_command_service = MagicMock()
        rest_command_service.start_mcp_workflow_run.return_value = rest_payload
        project_list_payload = MagicMock()
        project_list_payload.dump.return_value = {
            "projects": [{"id": PROJECT_UUID, "name": "Demo Project"}],
            "total": 1,
            "page": 1,
            "per_page": 200,
            "total_pages": 1,
        }
        project_query_service = MagicMock()
        project_query_service.list_projects.return_value = project_list_payload
        delegated_requests: list[dict[str, object]] = []
        api_tool = importlib.import_module("core.entrypoints.mcp.server_api_tool")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_command_service": lambda: rest_command_service},
            ),
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: project_query_service},
            ),
            patch.object(
                api_tool,
                "AppApiClient",
                side_effect=lambda: _TestClientBackedAppApiClient(client, delegated_requests),
            ),
            patch.object(api_tool, "_caller_bearer", return_value="caller-token"),
        ):
            api_resp = client.post(
                "/api/workflow-runs",
                json={
                    "project_id": PROJECT_UUID,
                    "workflow_id": "stock-import",
                },
            )
            mcp_body = _mcp_data(
                server.workflow_trigger(
                    project_name="Demo Project",
                    workflow_id="stock-import",
                )
            )

        assert api_resp.status_code == 200
        assert rest_command_service.start_mcp_workflow_run.call_count == 2
        api_body = api_resp.json()
        assert mcp_body == api_body
        assert api_body["job_id"] == "77"
        assert delegated_requests[0]["path"] == "/api/projects"
        assert delegated_requests[1]["path"] == "/api/workflow-runs"

    def test_project_notification_routes_rest_and_mcp_share_payload(self, client, monkeypatch):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for notification routes MCP parity test"
            )
        server = _import_mcp_server(monkeypatch)

        rest_payload = MagicMock()
        rest_payload.dump.return_value = {
            "project_id": "11",
            "notification_routes": [
                {
                    "route_key": "failed",
                    "provider_key": "fixture_notifier",
                    "event_type": "job.failed",
                    "severity": "error",
                    "enabled": True,
                }
            ],
        }
        rest_query_service = MagicMock()
        rest_query_service.list_notification_routes.return_value = rest_payload
        project_list_payload = MagicMock()
        project_list_payload.dump.return_value = {
            "projects": [{"id": "11", "name": "Demo Project"}],
            "total": 1,
            "page": 1,
            "per_page": 200,
            "total_pages": 1,
        }
        rest_query_service.list_projects.return_value = project_list_payload
        delegated_requests: list[dict[str, object]] = []
        api_tool = importlib.import_module("core.entrypoints.mcp.server_api_tool")

        with (
            patch.dict(
                client.app.state.runtime_container.service_factories,
                {"project_query_service": lambda: rest_query_service},
            ),
            patch.object(
                api_tool,
                "AppApiClient",
                side_effect=lambda: _TestClientBackedAppApiClient(client, delegated_requests),
            ),
            patch.object(api_tool, "_caller_bearer", return_value="caller-token"),
        ):
            api_resp = client.get("/api/projects/11/notification-routes")
            mcp_body = _mcp_data(server.project_notification_routes("Demo Project"))

        assert api_resp.status_code == 200
        assert rest_query_service.list_notification_routes.call_count == 2
        api_body = api_resp.json()
        assert mcp_body == api_body
        assert api_body["project_id"] == "11"
        assert delegated_requests[0]["path"] == "/api/projects"
        assert delegated_requests[1]["path"] == "/api/projects/11/notification-routes"

    def test_api_events_list_returns_service_backed_payload(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for api_events_list test"
            )

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.list_domain_events.return_value = [
            {
                "account_id": DEFAULT_ACCOUNT_ID,
                "id": 9,
                "event_id": "evt-9",
                "event_type": "workflow.failed",
                "severity": "error",
                "status": "recorded",
                "summary": "",
                "payload": {"detail": "stack"},
                "project_id": "7",
                "job_id": 9,
                "occurred_at": now,
                "updated_at": now,
            }
        ]
        mock_bl.count_domain_events.return_value = 1
        mock_bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
        }

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/events?page=1&per_page=25")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["events"][0]["event_id"] == "evt-9"
        assert body["events"][0]["project_name"] == "Demo"
        assert body["events"][0]["title"] == "Workflow Failed"
        assert body["events"][0]["event_type_label"] == "Workflow Failed"
        assert body["events"][0]["status_label"] == "Recorded"

    def test_api_events_list_uses_event_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for event list service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "events": [{"id": 9, "event_id": "evt-9"}],
            "total": 1,
            "page": 2,
            "per_page": 25,
            "total_pages": 1,
        }
        mock_service = MagicMock()
        mock_service.list_events.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"event_query_service": lambda: mock_service},
        ):
            resp = client.get(
                "/api/events?page=2&per_page=25&status=failed&workflow_id=wf-1&event_family=workflow&event_category=ingress"
            )

        assert resp.status_code == 200
        assert resp.json()["events"][0]["event_id"] == "evt-9"
        call = mock_service.list_events.call_args
        assert call.kwargs["page"] == 2
        assert call.kwargs["per_page"] == 25
        assert call.kwargs["status"] == "failed"
        assert call.kwargs["workflow_id"] == "wf-1"
        assert call.kwargs["event_family"] == "workflow"
        assert call.kwargs["event_category"] == "ingress"
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_events_list_validates_date_range(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for api_events date validation test"
            )

        mock_bl = MagicMock()
        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/events?from_date=2026-03-21&to_date=2026-03-20")

        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "invalid_request"
        assert body["message"] == body["error"]
        assert body["correlation_id"]
        assert "from_date" in body["error"]

    def test_api_event_detail_returns_dispatches_and_commands(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for api_event_detail test"
            )

        now = datetime.now(UTC)
        mock_bl = MagicMock()
        mock_bl.get_domain_event.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 9,
            "event_id": "evt-9",
            "event_type": "workflow.failed",
            "severity": "error",
            "status": "recorded",
            "summary": "Workflow failed",
            "payload": {"detail": "stack"},
            "actor": {"username": "system"},
            "urls": {"job": "/jobs/9"},
            "envelope": {"source": "test"},
            "source": "workflow",
            "aggregate_type": "job",
            "aggregate_id": "9",
            "correlation_id": "corr-9",
            "project_id": "7",
            "job_id": 9,
            "occurred_at": now,
            "updated_at": now,
        }
        mock_bl.get_project.return_value = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "id": 7,
            "name": "Demo",
        }
        mock_bl.list_event_dispatches.return_value = [
            {
                "id": 1,
                "dispatch_id": "dispatch-1",
                "target_type": "notification_route",
                "target_id": "route-1",
                "status": "failed",
                "reason_code": "retryable",
                "attempt_count": 1,
                "max_attempts": 3,
                "created_at": now,
            }
        ]
        mock_bl.list_event_delivery_attempts.return_value = [
            {
                "id": 2,
                "dispatch_id": "dispatch-1",
                "status": "failed",
                "handler": "fixture_notifier",
                "error_text": "timeout",
                "attempt_no": 1,
                "created_at": now,
                "started_at": now,
            }
        ]
        mock_bl.list_command_executions.return_value = [
            {
                "command_id": "cmd-1",
                "command_type": "resolve_approval",
                "status": "accepted",
            }
        ]

        with patch.dict(
            client.app.state.runtime_container.service_factories, {"db": lambda: mock_bl}
        ):
            resp = client.get("/api/events/evt-9")

        assert resp.status_code == 200
        body = resp.json()
        assert body["event_id"] == "evt-9"
        assert body["dispatches"][0]["dispatch_id"] == "dispatch-1"
        assert body["dispatches"][0]["attempts"][0]["handler"] == "fixture_notifier"
        assert body["commands"][0]["command_id"] == "cmd-1"
        assert body["commands"][0]["command_type_label"] == "Resolve Approval"
        assert body["commands"][0]["status_label"] == "Accepted"

    def test_api_event_detail_uses_event_query_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for event detail service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {"event_id": "evt-9", "dispatches": [], "commands": []}
        mock_service = MagicMock()
        mock_service.get_event_detail.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"event_query_service": lambda: mock_service},
        ):
            resp = client.get("/api/events/evt-9")

        assert resp.status_code == 200
        assert resp.json()["event_id"] == "evt-9"
        call = mock_service.get_event_detail.call_args
        assert call.args == ("evt-9",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID

    def test_api_event_redrive_uses_event_command_service_contract(self, client):
        try:
            pass
        except Exception as exc:
            _skip_only_missing_optional_dep(
                exc, context="api_runtime import for event redrive service contract test"
            )

        payload = MagicMock()
        payload.dump.return_value = {
            "status": "accepted",
            "event_id": "evt-9",
            "redriven_dispatches": ["dispatch-1"],
        }
        mock_service = MagicMock()
        mock_service.redrive_event.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"event_command_service": lambda: mock_service},
        ):
            resp = client.post("/api/events/evt-9/redrive")

        assert resp.status_code == 200
        assert resp.json()["event_id"] == "evt-9"
        call = mock_service.redrive_event.call_args
        assert call.args == ("evt-9",)
        assert call.kwargs["scope"].account_id == DEFAULT_ACCOUNT_ID


# ---------------------------------------------------------------------------
# UI projection layer tests
# ---------------------------------------------------------------------------


class TestUIProjection:
    """Tests for core.application.reports.ui_projection — semantic dashboard and job detail projections."""

    def test_project_job_summary_derives_next_action(self):
        from core.application.reports.ui_projection import project_job_summary

        job = {
            "status": "awaiting_approval",
            "task_summary": "Fix login bug",
            "duration_ms": 192000,
        }
        summary = project_job_summary(job, {}, "")
        assert "approve" in summary["next_action"].lower()
        assert summary["duration_display"] == "3m 12s"

    def test_a_durable_wait_is_cancellable_and_never_asks_for_approval(self):
        from core.application.reports.ui_projection import _build_job_actions, project_job_summary

        for status in ("awaiting_timer", "awaiting_child"):
            job = {"id": "job-1", "status": status, "task_summary": "Wait"}
            next_action = project_job_summary(job, {}, "")["next_action"].lower()
            assert next_action
            assert "approv" not in next_action
            actions = _build_job_actions(job=job, user_role="operator")["page_actions"]
            assert [(action["kind"], action["confirm"]) for action in actions] == [
                ("cancel_job", "Cancel this job?")
            ]

    def test_project_job_summary_failed_status(self):
        from core.application.reports.ui_projection import project_job_summary

        job = {"status": "failed", "task_summary": "Deploy", "duration_ms": 5000}
        summary = project_job_summary(job, {}, "OOM killed")
        assert summary["failure_reason"] == "OOM killed"
        assert (
            "restart" in summary["next_action"].lower()
            or "review" in summary["next_action"].lower()
        )

    def test_user_role_from_auth_admin(self):
        from core.application.reports.ui_projection import user_role_from_auth

        assert user_role_from_auth(None) == "viewer"
        assert user_role_from_auth("broken") == "viewer"
        assert user_role_from_auth({"roles": ["admin"]}) == "admin"
        assert user_role_from_auth({"roles": ["operator"]}) == "operator"
        assert user_role_from_auth({"roles": ["viewer"]}) == "viewer"
        assert user_role_from_auth({"roles": []}) == "viewer"
        assert user_role_from_auth({}) == "viewer"

    def test_project_job_detail_waiting_for_approval_builds_review_workspace(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": "11111111-1111-4111-8111-111111111111",
                "project_id": "7",
                "status": "awaiting_approval",
                "task_summary": "Review the architecture proposal",
                "task_source": "manual",
                "thread_id": "thread-91",
                "started_at": "2026-03-24T09:12:00+00:00",
                "duration_ms": 702000,
            },
            runtime={
                "current_step_id": "architecture",
                "wait_reason": "Architecture: Please review and approve the Architecture document.",
                "selected_workflow": "weekly_report",
            },
            approval_gate={
                "thread_id": "thread-91",
                "stage": "Architecture",
                "message": "Please review and approve the Architecture document.",
                "content": "# Architecture\n\nUse a queue-backed worker pool.",
            },
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={
                "breakdown": [
                    {
                        "agent": "designer",
                        "model_used": "gpt-5.4",
                        "calls": 2,
                        "tokens_prompt": 1200,
                        "tokens_completion": 800,
                        "cost_usd": 1.42,
                    }
                ],
                "total_cost_usd": 1.42,
            },
            project_name="Demo Project",
            failure_reason="",
            user_role="operator",
        )

        assert projection["hero_summary"]["status_label"] == "Waiting for approval"
        assert projection["review_workspace"]["kind"] == "document_review"
        assert projection["review_workspace"]["thread_id"] == "thread-91"
        assert projection["job_actions"]["page_actions"][0]["label"] == "Cancel job"
        assert (
            projection["job_actions"]["page_actions"][0]["path"]
            == "/api/jobs/11111111-1111-4111-8111-111111111111/cancel"
        )
        assert (
            projection["job_actions"]["page_actions"][0]["confirm"]
            == "Cancel this job? The current approval gate will be dismissed."
        )
        assert projection["next_action"]["body"] == (
            "Review workspace contains the only active controls for this gate, "
            "along with the context Flow Steward needs to continue."
        )
        assert projection["previous_decisions"] == []
        assert projection["cost_breakdown"]["total_cost_display"] == "$1.42"

    def test_project_job_detail_marks_unknown_cli_cost_as_unknown(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": "11111111-1111-4111-8111-111111111111",
                "project_id": "7",
                "status": "in_progress",
                "task_summary": "Implement the requested change",
                "task_source": "manual",
                "started_at": "2026-03-24T09:12:00+00:00",
            },
            runtime={"current_step_id": "development", "active_agent": "composer"},
            cost_breakdown={
                "breakdown": [
                    {
                        "agent": "composer",
                        "model_used": None,
                        "calls": 1,
                        "tokens_prompt": 22000,
                        "tokens_completion": 32,
                        "tokens_total": 22032,
                        "cost_usd": None,
                        "known_cost_usd": 0.0,
                        "unknown_cli_tokens": 22032,
                        "cost_knownness": "unknown",
                    }
                ],
                "total_cost_usd": 0.0,
                "known_cost_usd": 0.0,
                "unknown_cli_tokens": 22032,
                "cost_knownness": "unknown",
            },
            project_name="Demo Project",
            failure_reason="",
            user_role="operator",
        )

        row = projection["cost_breakdown"]["rows"][0]
        assert row["cost_display"] == "Unknown"
        assert row["cost_knownness"] == "unknown"
        assert projection["cost_breakdown"]["unknown_cli_tokens"] == 22032

    def test_project_job_detail_autonomy_gate_builds_user_facing_workspace(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 191,
                "project_id": "7",
                "status": "awaiting_approval",
                "task_summary": "Add a search bar to the task list page with autocomplete",
                "task_source": "manual",
                "thread_id": "thread-191",
                "started_at": "2026-04-03T05:35:00+00:00",
                "duration_ms": 61000,
            },
            runtime={
                "current_step_id": "analysis",
                "wait_reason": "Analysis: Flow Steward paused before starting the next step and needs your decision.",
                "selected_workflow": "weekly_report",
            },
            approval_gate={
                "thread_id": "thread-191",
                "stage": "Analysis",
                "gate_kind": "autonomy_gate",
                "message": "Flow Steward paused before starting the next step and needs your decision.",
                "content": (
                    "Flow Steward wants to start the analysis step with the scout agent.\n\n"
                    "It estimated confidence at 0.60, below the required 0.80 threshold."
                ),
                "technical_details": (
                    "Step autonomy gate requires operator review before execution.\n"
                    "- Step: analysis\n"
                    "- Target agent: scout\n"
                    "- Mode: assisted\n"
                    "- Confidence: 0.60\n"
                    "- Threshold: 0.80\n"
                ),
                "gate_meta": {
                    "step_id": "analysis",
                    "step_execution_id": "0b7e9a10-0000-4000-8000-000000000191",
                    "target_agent": "scout",
                    "mode": "assisted",
                    "confidence": 0.60,
                    "min_confidence": 0.80,
                },
            },
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="operator",
        )

        workspace = projection["review_workspace"]
        assert workspace["kind"] == "autonomy_gate"
        assert workspace["title"] == "Approve the analysis step"
        assert "Flow Steward wants to start the analysis step" in workspace["content"]
        assert workspace["details"].startswith("Step autonomy gate requires operator review")
        assert workspace["override_label"] == "Provide content instead"

    def test_project_job_detail_prefers_lifecycle_status_and_step_labels_when_provided(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": "33333333-3333-4333-8333-333333333333",
                "project_id": "7",
                "status": "failed",
                "task_summary": "Investigate one failing lifecycle projection",
                "started_at": "2026-04-03T05:35:00+00:00",
                "duration_ms": 61000,
            },
            runtime={
                "current_step_id": "implementation",
            },
            thread_state={},
            messages=[],
            events=[],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="operator",
            lifecycle={
                "status": {
                    "code": "failed",
                    "label": "Machine failed label",
                    "tone": "red",
                },
                "step": {
                    "code": "implementation",
                    "label": "Machine Implementation Step",
                },
            },
        )

        assert projection["hero_summary"]["status_label"] == "Machine failed label"
        assert projection["hero_summary"]["status_tone"] == "red"
        assert projection["hero_summary"]["facts"][0] == {
            "id": "current_step",
            "label": "Current step",
            "label_key": "react.jobs.current_step",
            "value": "Machine Implementation Step",
        }

    def test_project_job_detail_retryable_step_exposes_restart_from_selected_step(self):
        from core.application.reports.ui_projection import project_job_detail
        from tests._step_run_state import step_run_state

        projection = project_job_detail(
            job={
                "id": "22222222-2222-4222-8222-222222222222",
                "project_id": "7",
                "status": "retryable_interrupted_step",
                "task_summary": "Recover interrupted implementation run",
                "task_source": "manual",
                "thread_id": "thread-92",
                "started_at": "2026-03-24T10:00:00+00:00",
                "duration_ms": 301000,
            },
            runtime={
                "current_step_id": "implement_changes",
                "wait_reason": "Interrupted in-flight step detected.",
                "retryable_interrupted_step": {
                    "agent_id": "composer",
                    "skill": "implement_changes",
                    "step_id": "implement_changes",
                },
                "can_retry_interrupted_step": True,
            },
            thread_state=step_run_state(
                {
                    "step_id": "implement_changes",
                    "step_kind": "agent",
                    "agent": "composer",
                    "skill": "implement_changes",
                },
                job_id="job-1",
            ),
            messages=[],
            events=[],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="operator",
        )

        assert projection["selected_step"]["id"] == "implement_changes"
        assert projection["selected_step"]["action"]["label"] == "Restart from this step"
        assert (
            projection["selected_step"]["action"]["endpoint"]
            == "/api/jobs/22222222-2222-4222-8222-222222222222/retry-step"
        )
        selected_execution_step = next(
            step
            for step in projection["execution_steps"]
            if step.get("id") == projection["selected_step"]["id"]
        )
        assert selected_execution_step["can_restart_from_here"] is True
        assert selected_execution_step["action"]["label"] == "Restart from this step"
        assert projection["job_actions"]["page_actions"] == []

    def test_project_job_detail_failed_job_with_retry_snapshot_exposes_restart_from_selected_step(
        self,
    ):
        from core.application.reports.ui_projection import project_job_detail
        from tests._step_run_state import step_run_state

        projection = project_job_detail(
            job={
                "id": 921,
                "project_id": "7",
                "status": "failed",
                "task_summary": "Recover failed implementation run",
                "task_source": "manual",
                "thread_id": "thread-921",
                "started_at": "2026-03-24T10:00:00+00:00",
                "duration_ms": 301000,
            },
            runtime={
                "current_step_id": "implement_changes",
                "wait_reason": "The implementation step stopped unexpectedly.",
                "retryable_interrupted_step": {
                    "agent_id": "composer",
                    "skill": "implement_changes",
                    "step_id": "implement_changes",
                },
                "can_retry_interrupted_step": True,
            },
            thread_state=step_run_state(
                {
                    "step_id": "implement_changes",
                    "step_kind": "agent",
                    "agent": "composer",
                    "skill": "implement_changes",
                },
                job_id="job-1",
            ),
            messages=[],
            events=[],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="Worker process died",
            user_role="operator",
        )

        assert projection["selected_step"]["id"] == "implement_changes"
        assert projection["selected_step"]["status"] == "interrupted"
        assert projection["selected_step"]["action"]["label"] == "Restart from this step"

    def test_project_job_detail_interrupted_job_with_retry_snapshot_exposes_restart_from_selected_step(
        self,
    ):
        from core.application.reports.ui_projection import project_job_detail
        from tests._step_run_state import step_run_state

        projection = project_job_detail(
            job={
                "id": 923,
                "project_id": "7",
                "status": "interrupted",
                "task_summary": "Recover interrupted implementation run",
                "task_source": "manual",
                "thread_id": "thread-923",
                "started_at": "2026-03-24T10:00:00+00:00",
                "duration_ms": 301000,
            },
            runtime={
                "current_step_id": "implement_changes",
                "wait_reason": "The implementation step was interrupted.",
                "retryable_interrupted_step": {
                    "agent_id": "composer",
                    "skill": "implement_changes",
                    "step_id": "implement_changes",
                },
                "can_retry_interrupted_step": True,
            },
            thread_state=step_run_state(
                {
                    "step_id": "implement_changes",
                    "step_kind": "agent",
                    "agent": "composer",
                    "skill": "implement_changes",
                },
                job_id="job-1",
            ),
            messages=[],
            events=[],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="Worker process interrupted",
            user_role="operator",
        )

        assert projection["selected_step"]["id"] == "implement_changes"
        assert projection["selected_step"]["status"] == "interrupted"
        assert projection["selected_step"]["action"]["label"] == "Restart from this step"

    def test_project_job_detail_previous_decisions_uses_recorded_actor(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 922,
                "project_id": "7",
                "status": "completed",
                "task_summary": "Audit approval history",
                "task_source": "manual",
                "thread_id": "thread-922",
                "started_at": "2026-03-24T10:00:00+00:00",
                "duration_ms": 301000,
            },
            runtime={},
            approval_history=[
                {
                    "stage": "Quality Review",
                    "approved": False,
                    "action": "override",
                    "actor": "alice@example.com",
                    "comment": "Proceed with manual verification.",
                    "timestamp": "2026-03-24T11:00:00+00:00",
                }
            ],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="admin",
        )

        assert projection["previous_decisions"][0]["actor"] == "alice@example.com"

    def test_project_job_detail_viewer_hides_mutation_actions_and_diagnostics_entry(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 93,
                "project_id": "7",
                "status": "completed",
                "task_summary": "Read-only summary",
                "task_source": "manual",
                "thread_id": "thread-93",
            },
            runtime={"current_step_id": "delivery"},
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="viewer",
        )

        assert projection["job_actions"]["page_actions"] == []
        assert projection["job_actions"]["can_open_diagnostics"] is False

    def test_project_job_detail_header_builds_collapsible_task_description_preview(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 94,
                "project_id": "7",
                "status": "in_progress",
                "task_summary": (
                    "Transfer report email update:\n\n"
                    "## Objective\n"
                    "Move the escort block to a separate row and update the transfer email template "
                    "to include Escort name and Escort mob in the requested position."
                ),
                "task_source": "manual",
                "thread_id": "thread-94",
            },
            runtime={"current_step_id": "implementation"},
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="viewer",
        )

        header = projection["header"]
        assert header["task_title"] == "Transfer report email update:"
        assert header["has_task_description"] is True
        assert "escort block" in header["task_description_preview"].lower()

    def test_project_job_detail_diagnostics_omits_raw_causal_graph_from_ui(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 95,
                "project_id": "7",
                "status": "in_progress",
                "task_summary": "Diagnose execution path",
                "task_source": "manual",
                "thread_id": "thread-95",
            },
            runtime={"current_step_id": "analysis"},
            causal_chain={
                "nodes": [
                    {"title": "Research pass", "summary": "Requirements analysis completed."},
                ],
                "mermaid": 'graph TD\nm1["#1 scout:collect_findings [pass]"]',
            },
            messages=[
                {
                    "id": 1,
                    "step_id": "analysis",
                    "from_agent": "scout",
                    "skill": "collect_findings",
                    "status": "pass",
                    "summary": "Requirements analysis completed.",
                }
            ],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="operator",
        )

        diagnostics = projection["diagnostics"]
        assert diagnostics["causal_sequence"][0]["title"] == "Research pass"
        assert all(raw["title"] != "Raw causal graph" for raw in diagnostics["raw_refs"])

    def test_project_job_detail_diagnostics_surfaces_security_cause_with_event_reference(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 196,
                "project_id": "7",
                "status": "failed",
                "task_summary": "Handle policy resolution failure",
                "task_source": "manual",
                "thread_id": "thread-196",
            },
            runtime={"current_step_id": "development"},
            job_errors=[
                {
                    "source": "security",
                    "category": "policy",
                    "error_message": "Security policy resolution failed for postgres://admin:secret@db/app",
                    "context_jsonb": {"mode": "monitor_fail_closed"},
                }
            ],
            events=[
                {
                    "id": 77,
                    "rule_id": "policy:resolution_error",
                    "redacted_preview": "policy resolution error on inbound",
                }
            ],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="operator",
        )

        diagnostics = projection["diagnostics"]
        assert diagnostics["evidence_cards"][0]["title"] == "Security cause"
        assert diagnostics["evidence_cards"][0]["event_ref"] == "77"
        assert "postgres://" not in diagnostics["evidence_cards"][0]["summary"]

    def test_project_job_detail_message_summary_extracts_human_text_from_raw_output_json(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 96,
                "project_id": "7",
                "status": "completed",
                "task_summary": "Summarize implementation output",
                "task_source": "manual",
                "thread_id": "thread-96",
            },
            runtime={"current_step_id": "development"},
            messages=[
                {
                    "id": 101,
                    "step_id": "development",
                    "from_agent": "composer",
                    "skill": "revise_draft",
                    "status": "pass",
                    "summary": "",
                    "artifacts": {
                        "raw_output": json.dumps(
                            {
                                "schema_version": "phase_result_v2",
                                "status": "pass",
                                "summary": "All 8 login button color tests pass.",
                            }
                        ),
                    },
                    "errors": [],
                    "envelope": {},
                }
            ],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="viewer",
        )

        step = projection["execution_steps"][0]
        assert step["summary"] == "All 8 login button color tests pass."
        assert all(card["title"] != "Latest output" for card in step["evidence"])

    def test_project_job_detail_duplicate_scope_messages_keep_unique_selected_step_ids(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 97,
                "project_id": "7",
                "status": "completed",
                "task_summary": "Keep duplicate development steps independently selectable",
                "task_source": "manual",
                "thread_id": "thread-97",
            },
            runtime={},
            messages=[
                {
                    "id": 201,
                    "timestamp": "2026-03-25T12:00:00+00:00",
                    "step_id": "development",
                    "from_agent": "composer",
                    "skill": "revise_draft",
                    "status": "fail",
                    "summary": "Agent timed out after 2700s.",
                    "artifacts": {},
                    "errors": ["Agent timed out after 2700s."],
                    "envelope": {},
                },
                {
                    "id": 202,
                    "timestamp": "2026-03-25T12:05:00+00:00",
                    "step_id": "development",
                    "from_agent": "composer",
                    "skill": "revise_draft",
                    "status": "pass",
                    "summary": "Login button color tests all pass.",
                    "artifacts": {},
                    "errors": [],
                    "envelope": {},
                },
            ],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="viewer",
        )

        steps = projection["execution_steps"]
        assert len(steps) == 2
        assert steps[0]["scope_id"] == steps[1]["scope_id"]
        assert steps[0]["id"] != steps[1]["id"]
        assert projection["selected_step"]["id"] == "message-201"
        assert sum(1 for step in steps if step.get("selected")) == 1

    def test_project_job_detail_exposes_generated_artifact_evidence(self):
        from core.application.reports.ui_projection import project_job_detail

        projection = project_job_detail(
            job={
                "id": 98,
                "project_id": "7",
                "status": "completed",
                "task_summary": "Generate catalog",
                "task_source": "manual",
                "thread_id": "thread-98",
            },
            runtime={},
            messages=[
                {
                    "id": 301,
                    "timestamp": "2026-03-25T12:00:00+00:00",
                    "step_id": "store_artifact",
                    "from_agent": "connector_step",
                    "skill": "demo_store_product_catalog_artifact",
                    "status": "pass",
                    "summary": "Stored catalog artifact.",
                    "artifacts": {
                        "source_artifact": {
                            "artifact_id": "art_input",
                            "filename": "product_catalog_sample.csv",
                            "row_count": 10,
                            "size_bytes": 1200,
                            "download_url": "/api/jobs/98/attachments/6",
                        },
                        "catalog_artifact": {
                            "artifact_id": "art_demo",
                            "filename": "demo_enriched_catalog.csv",
                            "row_count": 10,
                            "size_bytes": 3123,
                            "download_url": "/api/jobs/98/attachments/7",
                        },
                    },
                    "errors": [],
                    "envelope": {},
                }
            ],
            approval_history=[],
            decision_overrides=[],
            cost_breakdown={"breakdown": [], "total_cost_usd": 0.0},
            project_name="Demo Project",
            failure_reason="",
            user_role="viewer",
        )

        cards = projection["execution_steps"][0]["evidence"]
        assert cards[0]["title"] == "Input catalog CSV"
        assert cards[0]["href"] == "/api/jobs/98/attachments/6"
        assert cards[1]["title"] == "Generated catalog CSV"
        assert cards[1]["href"] == "/api/jobs/98/attachments/7"
        assert "10 rows" in cards[1]["summary"]


class TestQueryServiceRegressions:
    @staticmethod
    def _job_service(bl: Any) -> JobQueryService:
        return JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: str(status or "running"),
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

    def test_job_list_restricted_scope_uses_db_project_ids_filter(self):
        bl = MagicMock()
        bl.list_jobs_for_attention.return_value = [
            {
                "id": 11,
                "project_id": "10",
                "project_name": "Demo",
                "task_id": "task-11",
                "task_summary": "Investigate",
                "status": "running",
            }
        ]
        bl.count_jobs_for_attention.return_value = 1

        service = JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, acknowledged_at=None: "running",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

        payload = service.list_jobs(
            page=1,
            per_page=25,
            scope=ScopeContext(account_id="acc_alpha", allowed_project_ids=frozenset({"10", "20"})),
        ).dump()

        assert payload["total"] == 1
        call_kwargs = bl.list_jobs_for_attention.call_args.kwargs
        assert sorted(call_kwargs["project_ids"]) == ["10", "20"]
        assert call_kwargs["project_id"] is None

    def test_job_list_matches_any_of_several_statuses_in_rows_and_count(self):
        bl = MagicMock()
        bl.list_jobs_for_attention.return_value = []
        bl.count_jobs_for_attention.return_value = 0

        payload = (
            self._job_service(bl)
            .list_jobs(status=["failed", "Stuck,failed"], scope=ScopeContext(account_id="acc_a"))
            .dump()
        )

        assert bl.list_jobs_for_attention.call_args.kwargs["status"] == ("failed", "stuck")
        assert bl.count_jobs_for_attention.call_args.kwargs["status"] == ("failed", "stuck")
        assert payload["status"] == "failed,stuck"

    def test_job_list_forwards_hide_acknowledged_to_rows_and_count(self):
        bl = MagicMock()
        bl.list_jobs_for_attention.return_value = []
        bl.count_jobs_for_attention.return_value = 0
        service = self._job_service(bl)

        service.list_jobs(
            page=1, per_page=25, hide_acknowledged=True, scope=ScopeContext(account_id="acc_alpha")
        )
        assert bl.list_jobs_for_attention.call_args.kwargs["hide_acknowledged"] is True
        assert bl.count_jobs_for_attention.call_args.kwargs["hide_acknowledged"] is True

        service.list_jobs(page=1, per_page=25, scope=ScopeContext(account_id="acc_alpha"))
        assert "hide_acknowledged" not in bl.list_jobs_for_attention.call_args.kwargs

    def test_job_list_forwards_workflow_and_search_filters_to_rows_and_count(self):
        bl = MagicMock()
        bl.list_jobs_for_attention.return_value = []
        bl.count_jobs_for_attention.return_value = 0
        service = self._job_service(bl)

        service.list_jobs(
            page=2,
            per_page=25,
            workflow_id="inventory-import",
            search="supplier feed",
            scope=ScopeContext(account_id="acc_alpha"),
        )

        assert bl.list_jobs_for_attention.call_args.kwargs["workflow_id"] == "inventory-import"
        assert bl.list_jobs_for_attention.call_args.kwargs["search"] == "supplier feed"
        assert bl.count_jobs_for_attention.call_args.kwargs["workflow_id"] == "inventory-import"
        assert bl.count_jobs_for_attention.call_args.kwargs["search"] == "supplier feed"

    def test_job_context_requires_account_binding_when_project_id_missing(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": "job-1",
            "project_id": None,
            "account_id": "acc_beta",
            "status": "in_progress",
        }
        service = self._job_service(bl)

        with pytest.raises(ServiceForbiddenError):
            service._job_context("job-1", scope=ScopeContext(account_id="acc_alpha"))

    def test_job_context_allows_same_account_job_without_project_id(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": "job-1",
            "project_id": None,
            "account_id": "acc_alpha",
            "status": "in_progress",
        }
        service = self._job_service(bl)

        _, job, _ = service._job_context("job-1", scope=ScopeContext(account_id="acc_alpha"))

        assert job["id"] == "job-1"

    def test_job_events_applies_requested_limit_at_database_boundary(self):
        bl = MagicMock()
        bl.get_job.return_value = {
            "id": "job-1",
            "project_id": None,
            "account_id": "acc_alpha",
            "status": "in_progress",
        }
        bl.get_events.return_value = [
            {"id": "evt-1", "event_type": "job.started"},
            {"id": "evt-2", "event_type": "job.completed"},
        ]
        service = self._job_service(bl)

        payload = service.list_events(
            "job-1",
            since_id="evt-0",
            limit=2,
            scope=ScopeContext(account_id="acc_alpha"),
        ).dump()

        bl.get_events.assert_called_once_with("job-1", since_id="evt-0", limit=2)
        assert [event["id"] for event in payload["events"]] == ["evt-1", "evt-2"]
        assert payload["last_id"] == "evt-2"

    def test_message_detail_requires_account_binding_for_jobless_message(self):
        bl = MagicMock()
        bl.get_agent_message_by_id.return_value = {
            "id": "msg-1",
            "job_id": "",
            "account_id": "acc_beta",
            "content": "private",
        }
        service = self._job_service(bl)

        with pytest.raises(ServiceForbiddenError):
            service.get_message_detail("msg-1", scope=ScopeContext(account_id="acc_alpha"))

    def test_message_detail_allows_same_account_jobless_message(self):
        bl = MagicMock()
        bl.get_agent_message_by_id.return_value = {
            "id": "msg-1",
            "job_id": "",
            "account_id": "acc_alpha",
            "content": "visible",
        }
        service = self._job_service(bl)

        payload = service.get_message_detail(
            "msg-1", scope=ScopeContext(account_id="acc_alpha")
        ).dump()

        assert payload["message"]["content"] == "visible"

    def test_events_list_skips_inaccessible_rows_instead_of_failing_whole_response(self):
        now = datetime.now(UTC)
        bl = MagicMock()
        bl.list_domain_events.return_value = [
            {
                "id": 1,
                "event_id": "evt-1",
                "event_type": "workflow.failed",
                "severity": "error",
                "status": "recorded",
                "summary": "allowed",
                "payload": {},
                "account_id": "acc_alpha",
                "project_id": "7",
                "workflow_id": "wf-1",
                "job_id": 1,
                "occurred_at": now,
                "updated_at": now,
            },
            {
                "id": 2,
                "event_id": "evt-2",
                "event_type": "workflow.failed",
                "severity": "error",
                "status": "recorded",
                "summary": "foreign",
                "payload": {},
                "account_id": "acc_beta",
                "project_id": "99",
                "workflow_id": "wf-9",
                "job_id": 2,
                "occurred_at": now,
                "updated_at": now,
            },
        ]
        bl.count_domain_events.return_value = 2

        service = EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda _bl, _project_id, _cache: "Demo",
            resolve_domain_event_by_ref=lambda current_bl, event_ref: current_bl.get_domain_event(
                event_ref
            ),
            serialize_domain_event_summary=lambda row, project_name="": {
                "id": row.get("id"),
                "event_id": row.get("event_id"),
                "event_type": row.get("event_type"),
                "severity": row.get("severity"),
                "status": row.get("status"),
                "title": row.get("summary"),
                "summary": row.get("summary"),
                "detail": "",
                "project_id": row.get("project_id"),
                "project_name": project_name,
                "workflow_id": row.get("workflow_id"),
                "job_id": row.get("job_id"),
                "group_id": row.get("group_id"),
                "step_id": row.get("step_id"),
                "created_at": row.get("occurred_at").isoformat(),
                "updated_at": row.get("updated_at").isoformat(),
            },
            serialize_domain_event_datetime=lambda value: (
                value.isoformat() if hasattr(value, "isoformat") else value
            ),
            to_json_obj=lambda value: value,
        )

        payload = service.list_events(scope=ScopeContext(account_id="acc_alpha")).dump()

        assert payload["total"] == 1
        assert [row["event_id"] for row in payload["events"]] == ["evt-1"]


@pytest.mark.parametrize("group_by", ["", "attention"])
def test_aggregate_job_list_filters_attention_and_count_at_scope_boundary(group_by):
    bl = MagicMock()
    bl.list_jobs_for_attention.return_value = []
    bl.count_jobs_for_attention.return_value = 0
    scope = ScopeContext(
        account_id="acc_alpha",
        tenant_id="tenant-a",
        allowed_project_ids=frozenset({"p"}),
        allowed_workflow_ids=frozenset({"release"}),
        allowed_workspace_ids=frozenset({"w"}),
    )
    service = TestQueryServiceRegressions._job_service(bl)
    service.list_jobs(
        group_by=group_by, workflow_id="release", status="failed", search="blocked", scope=scope
    )
    call = bl.list_jobs_for_attention.call_args.kwargs
    assert call["scope"] == scope
    assert call["workflow_id"] == "release"
    assert call["status"] == ("failed",)
    assert call["search"] == "blocked"
    if not group_by:
        assert bl.count_jobs_for_attention.call_args.kwargs["scope"] == scope


@pytest.mark.parametrize("filters", [{"project_id": "foreign"}, {"workflow_id": "foreign"}])
def test_job_list_explicit_foreign_scope_is_rejected(filters):
    service = TestQueryServiceRegressions._job_service(MagicMock())
    with pytest.raises(ServiceForbiddenError):
        service.list_jobs(
            **filters,
            scope=ScopeContext(
                account_id="acc_alpha",
                allowed_project_ids=frozenset({"p"}),
                allowed_workflow_ids=frozenset({"release"}),
            ),
        )


def test_pending_approval_filters_tenant_workspace_project_and_workflow():
    bl = MagicMock()
    row = {
        "id": "job",
        "account_id": "acc_alpha",
        "project_id": "p",
        "selected_workflow": "release",
        "tenant_id": "tenant-a",
        "workspace_id": "w",
    }
    bl.get_awaiting_approval_jobs.return_value = [
        row,
        {**row, "tenant_id": "foreign"},
        {**row, "workspace_id": "foreign"},
        {**row, "project_id": "foreign"},
        {**row, "selected_workflow": "foreign"},
        {**row, "account_id": "foreign"},
        {**row, "tenant_id": None},
    ]
    service = TestQueryServiceRegressions._job_service(bl)
    result = service.list_awaiting_approval_jobs(
        scope=ScopeContext(
            account_id="acc_alpha",
            tenant_id="tenant-a",
            allowed_project_ids=frozenset({"p"}),
            allowed_workspace_ids=frozenset({"w"}),
            allowed_workflow_ids=frozenset({"release"}),
        )
    )
    assert len(result) == 1


def test_pending_approval_dependency_failure_remains_unavailable():
    bl = MagicMock()
    bl.get_awaiting_approval_jobs.side_effect = RuntimeError("database down")
    service = TestQueryServiceRegressions._job_service(bl)
    with pytest.raises(ServiceUnavailableError, match="Approvals queue unavailable"):
        service.list_awaiting_approval_jobs(scope=ScopeContext(account_id="acc_alpha"))


@pytest.mark.parametrize(
    "foreign",
    [
        {"selected_workflow": "other"},
        {"tenant_id": "other"},
        {"account_id": "other"},
        {"workspace_id": "other"},
    ],
)
def test_lightweight_report_preflight_rejects_foreign_job_identity(foreign):
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": "job",
        "project_id": "p",
        "account_id": "acc_alpha",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "workspace_id": "w",
        "selected_workflow": "release",
        **foreign,
    }
    bl.get_project.return_value = {"id": "p", "account_id": "acc_alpha", "workspace_id": "w"}
    service = TestQueryServiceRegressions._job_service(bl)
    scope = ScopeContext(
        account_id="acc_alpha",
        allowed_project_ids=frozenset({"p"}),
        allowed_workspace_ids=frozenset({"w"}),
        allowed_workflow_ids=frozenset({"release"}),
    )
    with pytest.raises(ServiceForbiddenError):
        service.validate_job_diagnostic_report_access("job", scope=scope)


@pytest.mark.parametrize(
    "field, foreign", [("project_id", "other"), ("workflow_id", "other"), ("workspace_id", "other")]
)
def test_lightweight_report_preflight_preserves_exact_caller_scope(field, foreign):
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": "job",
        "project_id": "p",
        "account_id": "acc_alpha",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "workspace_id": "w",
        "selected_workflow": "release",
    }
    bl.get_project.return_value = {"id": "p", "account_id": "acc_alpha", "workspace_id": "w"}
    service = TestQueryServiceRegressions._job_service(bl)
    with pytest.raises(ServiceForbiddenError):
        service.validate_job_diagnostic_report_access(
            "job", scope=ScopeContext(account_id="acc_alpha", **{field: foreign})
        )


@pytest.mark.parametrize("field, own", [("project_id", "p"), ("workspace_id", "w")])
@pytest.mark.parametrize("group_by", ["", "attention"])
def test_aggregate_job_list_cannot_override_exact_caller_scope(field, own, group_by):
    bl = MagicMock()
    bl.list_jobs_for_attention.return_value = []
    bl.count_jobs_for_attention.return_value = 0
    service = TestQueryServiceRegressions._job_service(bl)
    with pytest.raises(ServiceForbiddenError):
        service.list_jobs(
            group_by=group_by,
            scope=ScopeContext(account_id="acc_alpha", **{field: own}),
            **{field: "other"},
        )
    bl.list_jobs_for_attention.assert_not_called()
    bl.count_jobs_for_attention.assert_not_called()


@pytest.mark.parametrize("group_by", ["", "attention"])
@pytest.mark.parametrize("project_id", [None, "p"])
def test_history_source_filter_preserves_scope_and_reaches_rows_and_count(group_by, project_id):
    bl = MagicMock()
    bl.list_jobs_for_attention.return_value = []
    bl.count_jobs_for_attention.return_value = 0
    scope = ScopeContext(
        account_id="acc_alpha",
        tenant_id="tenant-a",
        allowed_project_ids=frozenset({"p"}),
        allowed_workflow_ids=frozenset({"release"}),
        allowed_workspace_ids=frozenset({"w"}),
    )
    original_scope = scope.model_dump()
    service = TestQueryServiceRegressions._job_service(bl)
    service.list_jobs(
        group_by=group_by,
        project_id=project_id,
        workflow_id="release",
        page=1,
        per_page=8,
        task_source="workflow_test_run",
        scope=scope,
    )
    rows = bl.list_jobs_for_attention.call_args.kwargs
    assert rows["task_source"] == "workflow_test_run"
    assert rows["scope"].model_dump() == original_scope
    assert rows["workflow_id"] == "release"
    assert rows["account_id"] == scope.account_id
    assert rows["project_id"] == project_id
    if not group_by and project_id is None:
        assert rows["project_ids"] == ["p"]
    else:
        assert "project_ids" not in rows
    if not group_by:
        count = bl.count_jobs_for_attention.call_args.kwargs
        assert count["task_source"] == rows["task_source"]
        assert count["scope"] is rows["scope"]
        assert count["scope"].model_dump() == original_scope
        assert count["workflow_id"] == "release"
        assert count["account_id"] == rows["account_id"]
        assert count["project_id"] == rows["project_id"]
        assert count.get("project_ids") == rows.get("project_ids")
    else:
        # Attention grouping forwards the full allowed scope instead of a project_ids list.
        assert rows["scope"].allowed_project_ids == frozenset({"p"})
        bl.count_jobs_for_attention.assert_not_called()

    assert scope.model_dump() == original_scope


@pytest.mark.parametrize(
    "filters", [{"project_id": "foreign"}, {"workflow_id": "foreign"}, {"workspace_id": "foreign"}]
)
def test_history_source_filter_cannot_bypass_explicit_owner_scope(filters):
    bl = MagicMock()
    service = TestQueryServiceRegressions._job_service(bl)
    with pytest.raises(ServiceForbiddenError):
        service.list_jobs(
            task_source="workflow_test_run",
            **filters,
            scope=ScopeContext(
                account_id="acc_alpha",
                project_id="p",
                workspace_id="w",
                allowed_project_ids=frozenset({"p"}),
                allowed_workflow_ids=frozenset({"release"}),
            ),
        )
    bl.list_jobs_for_attention.assert_not_called()
    bl.count_jobs_for_attention.assert_not_called()


def test_history_source_filter_is_validated_for_direct_service_callers():
    from core.application.management.errors import ServiceValidationError

    bl = MagicMock()
    service = TestQueryServiceRegressions._job_service(bl)
    with pytest.raises(ServiceValidationError):
        service.list_jobs(task_source="step_test_run", scope=ScopeContext(account_id="acc_alpha"))
    bl.list_jobs_for_attention.assert_not_called()
    bl.count_jobs_for_attention.assert_not_called()


@pytest.mark.parametrize(
    ("auth_source", "principal_type"), [("pat", "user"), ("machine", "service_account")]
)
def test_token_approval_http_preserves_safe_recovery_decision_contract(
    client, auth_source, principal_type
):
    route_registration = importlib.import_module("core.entrypoints.api.route_registration")
    store = MagicMock()
    store.get_job.return_value = {
        "id": "effect-job",
        "status": "awaiting_approval",
        "thread_id": "effect-thread",
        "task_summary": "Publish fixture record",
        "project_id": "7",
        "selected_workflow": "effect-workflow",
    }
    store.get_project.return_value = {
        "id": "7",
        "name": "Fixture project",
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    interrupt_payload = {
        "approval_id": "effect-gate",
        "gate_kind": "recovery",
        "recovery_kind": "external_effect_unknown",
        "message": "Verify the provider outcome before deciding.",
        "allowed_actions": ["approve", "reject"],
        "expires_at": "2030-01-01T00:00:00Z",
        "for_each_id": "publication-batch",
        "approve_label": "Operation completed — continue",
        "reject_label": "Operation not completed — retry",
        "comment_required_on_approve": True,
        "comment_required_on_reject": True,
        "override_content_supported": False,
        "content": {"summary": "Provider response was lost", "raw": "private-content"},
        "external_effect": {"effect_id": "private-ledger-identity"},
        "source_ref": {"job_id": "private-source"},
        "technical_details": "private-provider-details",
    }
    snapshot = types.SimpleNamespace(
        tasks=[
            types.SimpleNamespace(
                interrupts=[types.SimpleNamespace(value=interrupt_payload, id="effect-interrupt")]
            )
        ]
    )
    service = ApprovalQueryService(
        get_logger=lambda: store,
        get_thread_state_snapshot=lambda thread_id: (
            snapshot if thread_id == "effect-thread" else None
        ),
        approval_fallback_payload=lambda _job: {},
    )
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="fixture-token-reader",
        principal_type=principal_type,
        auth_source=auth_source,
        allowed_project_ids=frozenset({"7"}),
        allowed_workflow_ids=frozenset({"effect-workflow"}),
    )
    with (
        patch.dict(
            client.app.state.runtime_container.service_factories,
            {"approval_query_service": lambda: service},
        ),
        patch.object(route_registration, "_request_scope", return_value=scope),
    ):
        response = client.get("/api/jobs/effect-job/approval")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval_id"] == "effect-gate"
    assert body["allowed_actions"] == ["approve", "reject"]
    assert body["expires_at"] == "2030-01-01T00:00:00Z"
    assert body["gate_kind"] == "recovery"
    assert body["recovery_kind"] == "external_effect_unknown"
    assert body["for_each_id"] == "publication-batch"
    assert body["approve_label"] == "Operation completed — continue"
    assert body["reject_label"] == "Operation not completed — retry"
    assert body["comment_required_on_approve"] is True
    assert body["comment_required_on_reject"] is True
    assert body["override_content_supported"] is False
    assert body["content"] == {"summary": "Provider response was lost"}
    assert not {"payload", "external_effect", "source_ref", "technical_details"} & body.keys()
    assert "private-" not in response.text
    store.get_job.assert_called_once_with("effect-job")


# A restricted job read must authorize its durable workflow before any projection.
def _restricted_job_read_case(identity=None, *, restricted=True):
    bl = MagicMock()
    bl.conn = None
    row = {
        "id": "job-restricted",
        "project_id": PROJECT_UUID,
        "account_id": DEFAULT_ACCOUNT_ID,
        "status": "in_progress",
        "task_summary": "Scoped run",
        "thread_id": "thread-restricted",
        "selected_workflow_display_name": "Display only",
        **(identity or {}),
    }
    bl.get_job.return_value = row
    bl.get_project.return_value = {
        "id": PROJECT_UUID,
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    bl.get_job_cost_summary.return_value = {}
    runtime = AsyncMock(return_value={"current_step_id": "work"})
    artifact = MagicMock()
    service = JobQueryService(
        get_logger=lambda: bl,
        serialize_attention_job_row=lambda value: value,
        serialize_awaiting_approval_job_row=lambda value: value,
        job_attention_group=lambda *_args: "running",
        build_runtime_payload_for_api=runtime,
        resolve_artifact=artifact,
    )
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="restricted-admin",
        principal_type="user",
        auth_source="pat",
        allowed_project_ids=frozenset({PROJECT_UUID}),
        allowed_workflow_ids=frozenset({"wf-allowed"}) if restricted else frozenset(),
    )
    return service, bl, runtime, artifact, scope


@pytest.mark.parametrize("include_result", [False, True], ids=["polling", "result"])
def test_restricted_job_status_denies_foreign_workflow_before_projection(include_result):
    service, bl, runtime, artifact, scope = _restricted_job_read_case(
        {"selected_workflow": "wf-foreign"}
    )
    with pytest.raises(ServiceForbiddenError):
        asyncio.run(
            service.get_job_status(
                "job-restricted",
                scope=scope,
                include_workflow_result=include_result,
            )
        )
    bl.get_job_cost_summary.assert_not_called()
    runtime.assert_not_awaited()
    artifact.assert_not_called()


@pytest.mark.parametrize(
    "identity",
    [
        {"selected_workflow": " wf-allowed "},
        {"selected_workflow_key": "wf-allowed"},
        {"selected_workflow_id": "wf-allowed"},
        {"meta": {"selected_workflow_id": "wf-allowed"}},
        {"meta": '{"selected_workflow_id":"wf-allowed"}'},
        {"meta": {"invocation": {"selected_workflow_id": "wf-allowed"}}},
        {"meta": '{"invocation":{"selected_workflow_id":"wf-allowed"}}'},
        {"meta": {"invocation": '{"selected_workflow_id":"wf-allowed"}'}},
    ],
)
def test_restricted_job_status_accepts_canonical_durable_workflow_encodings(identity):
    service, bl, runtime, _artifact, scope = _restricted_job_read_case(identity)
    result = asyncio.run(service.get_job_status("job-restricted", scope=scope)).dump()
    assert result["id"] == "job-restricted"
    assert result["status"] == "in_progress"
    bl.get_job_cost_summary.assert_called_once_with("job-restricted")
    runtime.assert_awaited_once()


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"meta": "not-json"},
        {"meta": "[]"},
        {"meta": {"invocation": "[]"}},
        {"runtime_state": {"selected_workflow": "wf-allowed"}},
        {"selected_workflow_display_name": "wf-allowed"},
        {"selected_workflow": "wf-foreign", "selected_workflow_key": "wf-allowed"},
        {"selected_workflow_key": "wf-foreign", "selected_workflow_id": "wf-allowed"},
        {"selected_workflow_id": "wf-foreign", "meta": {"selected_workflow_id": "wf-allowed"}},
        {
            "meta": {
                "selected_workflow_id": "wf-foreign",
                "invocation": {"selected_workflow_id": "wf-allowed"},
            }
        },
    ],
)
def test_restricted_job_status_rejects_missing_or_lower_priority_workflow_authority(identity):
    service, bl, runtime, artifact, scope = _restricted_job_read_case(identity)
    with pytest.raises(ServiceForbiddenError):
        asyncio.run(service.get_job_status("job-restricted", scope=scope))
    bl.get_job_cost_summary.assert_not_called()
    runtime.assert_not_awaited()
    artifact.assert_not_called()


@pytest.mark.parametrize("projectless", [False, True], ids=["orphan", "manual"])
def test_unrestricted_legacy_job_read_keeps_owned_orphan_and_manual_compatibility(projectless):
    service, bl, _runtime, _artifact, scope = _restricted_job_read_case(restricted=False)
    bl.get_project.return_value = None
    if projectless:
        bl.get_job.return_value["project_id"] = None
    result = asyncio.run(service.get_job_status("job-restricted", scope=scope)).dump()
    assert result["id"] == "job-restricted"
    assert result["status"] == "in_progress"


@pytest.mark.parametrize(
    "failure,error",
    [
        ("missing", ServiceNotFoundError),
        ("foreign-project", ServiceForbiddenError),
        ("same-account-denied-project", ServiceForbiddenError),
        ("foreign-orphan", ServiceForbiddenError),
        ("lookup-outage", ServiceUnavailableError),
    ],
)
def test_restricted_job_read_preserves_existing_access_error_controls(failure, error):
    service, bl, runtime, _artifact, scope = _restricted_job_read_case()
    if failure == "missing":
        bl.get_job.return_value = None
    elif failure == "foreign-project":
        bl.get_project.return_value["account_id"] = "foreign-account"
    elif failure == "same-account-denied-project":
        other_project = "22222222-2222-4222-8222-222222222222"
        bl.get_job.return_value["project_id"] = other_project
        bl.get_project.return_value["id"] = other_project
        bl.get_job.return_value["selected_workflow"] = "wf-allowed"
    elif failure == "foreign-orphan":
        bl.get_project.return_value = None
        bl.get_job.return_value["account_id"] = "foreign-account"
    else:
        bl.get_project.side_effect = RuntimeError("database unavailable")
    with pytest.raises(error):
        asyncio.run(service.get_job_status("job-restricted", scope=scope))
    bl.get_job_cost_summary.assert_not_called()
    runtime.assert_not_awaited()


def test_restricted_job_definition_denies_before_archive_lookup():
    service, bl, _runtime, _artifact, scope = _restricted_job_read_case(
        {"selected_workflow": "wf-foreign"}
    )
    bl.conn = object()
    with (
        patch(
            "core.application.jobs.workflow_definition_snapshot_repository.WorkflowDefinitionSnapshotRepository"
        ) as repository,
        pytest.raises(ServiceForbiddenError),
    ):
        service.get_job_definition("job-restricted", scope=scope)
    repository.assert_not_called()


def test_restricted_job_children_omit_child_without_durable_workflow_identity():
    service, bl, _runtime, _artifact, scope = _restricted_job_read_case(
        {"selected_workflow": "wf-allowed"}
    )
    parent = bl.get_job.return_value
    parent["meta"] = {"invoke_children": {}}
    children = {}
    for child_id in ("child-allowed", "child-missing-identity"):
        parent["meta"]["invoke_children"][child_id] = {
            "step_id": "invoke",
            "step_execution_id": "execution",
            "invocation_key": child_id,
            "child_job_id": child_id,
            "child_workflow_id": "wf-allowed",
        }
        children[child_id] = {
            **parent,
            "id": child_id,
            "meta": {"invoke_parent_job_id": "job-restricted", "invoke_parent_step_id": "invoke"},
        }
    children["child-missing-identity"].pop("selected_workflow")
    bl.get_job.side_effect = lambda job_id: {"job-restricted": parent, **children}[job_id]
    result = service.get_child_invocations("job-restricted", scope=scope)
    assert [entry["child_job_id"] for entry in result["invocations"]] == ["child-allowed"]
    assert result["truncated"] is False
