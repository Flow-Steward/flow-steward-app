"""
Unit tests for the workflow reaction system via the serve.py compatibility shim.

Covers: CRUD endpoints, ingestion, condition evaluation, debouncing,
action execution, and workflow chaining via completion events.

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_api_reactions.py -v
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs.workflow_resume_state import WorkflowResumeQueueRow
from core.application.management.command_services_approval import ApprovalCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext
from core.entrypoints.api import route_registration as route_composition
from core.infrastructure.runtime import http_app_reactions_extensions as reactions_runtime

_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
    "jinja2",
    "psycopg_runtime",
    "langgraph",
    "langchain_core",
}


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_bl() -> MagicMock:
    """Create a mock Flow Steward logger with reaction-related methods."""
    bl = MagicMock()
    bl.list_workflow_reactions = MagicMock(return_value=[])
    bl.replace_scoped_workflow_reactions = MagicMock(return_value=("workflow", "", []))
    bl.log_job_reaction_event = MagicMock(
        return_value={
            "id": 1,
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "event_type": "external.comment.received",
            "source": "external",
            "source_id": "",
            "event_key": "",
            "payload": {},
            "dedupe_key": "",
            "processed": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    bl.list_job_reaction_events = MagicMock(return_value=[])
    bl.mark_job_reaction_event_processed = MagicMock(return_value=None)
    bl.get_job = MagicMock(
        return_value={
            "id": 100,
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": 1,
            "thread_id": "thread-abc",
            "status": "awaiting_approval",
            "meta": json.dumps({"selected_workflow_id": "wf-deploy"}),
        }
    )
    bl.get_project = MagicMock(
        return_value={
            "id": 1,
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Test Project",
        }
    )
    bl.enqueue_domain_event = MagicMock(return_value=None)
    bl.conn = MagicMock()
    return bl


def test_reaction_cancellation_uses_canonical_terminal_persistence(monkeypatch) -> None:
    from core.application.workflows.reaction_runtime import cancel_job_from_reaction

    bl = _make_mock_bl()
    terminal_calls: list[dict] = []
    monkeypatch.setattr(
        "core.application.jobs.terminal_persistence.TerminalPersistenceService.complete_failure",
        lambda _self, **kwargs: terminal_calls.append(dict(kwargs)),
    )

    result = cancel_job_from_reaction(
        "100",
        "policy stop",
        get_logger_only=lambda: bl,
        err_database_not_configured="db unavailable",
        err_job_not_found_lower="job missing",
        logger=MagicMock(),
    )

    assert result == {"status": "cancelled", "job_id": "100", "executed": True}
    bl.update_job.assert_not_called()
    assert terminal_calls == [
        {
            "job_id": "100",
            "status": "cancelled",
            "error_message": "policy stop",
            "error_code": "reaction_cancelled",
            "category": "cancellation",
        }
    ]


def test_chat_reaction_uses_canonical_approval_command_path(monkeypatch) -> None:
    scope = ScopeContext(account_id="acc-1", actor_id="chat:alice")
    job_store = MagicMock()
    job_store.get_job.return_value = {"id": "job-1", "thread_id": "thread-1"}
    job_store.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        "project-1",
        "acc-1",
        "workspace-1",
        "workflow-1",
        "job-1",
    )
    resume_row = WorkflowResumeQueueRow(
        job_id="job-1",
        project_id="project-1",
        task_id="TASK-1",
        resume_revision=2,
        publish_token="publish-2",
    )
    resume_repository = MagicMock()
    enqueue_resume = MagicMock()
    approval_service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: job_store,
        enqueue_workflow_resume=enqueue_resume,
        workflow_resume_repository_factory=lambda _bl: resume_repository,
    )
    approval_service._claim_and_record_decision = MagicMock(
        return_value=(
            {"approval_id": "gate-2", "allowed_actions": ["approve"]},
            "gate-2",
            None,
            None,
            True,
            True,
            resume_row,
        )
    )
    monkeypatch.setattr(reactions_runtime, "_get_job_store", lambda: job_store)
    monkeypatch.setattr(
        reactions_runtime,
        "_approval_reaction_resumer",
        lambda **kwargs: route_composition._resume_approval_reaction(
            approval_command_service=lambda: approval_service,
            **kwargs,
        ),
    )
    # The threaded resume no longer exists: a reaction resumes through the
    # durable approval command only.
    assert not hasattr(reactions_runtime._reaction_runtime, "resume_job_from_reaction")

    result = reactions_runtime._apply_reaction_action(
        {
            "event_type": "notification.interaction_received",
            "job_id": "job-1",
            "correlation_id": "chat-callback-1",
            "payload": {
                "approval_id": "gate-2",
                "approved": True,
                "action": "approve",
            },
        },
        {"action": {"type": "approve_job"}},
        scope=scope,
    )

    assert result["status"] == "resumed"
    claim = approval_service._claim_and_record_decision.call_args.kwargs
    decision = claim["decision"]
    assert decision.approval_id == "gate-2"
    assert claim["scope"].account_id == scope.account_id
    assert claim["scope"].actor_id == scope.actor_id
    assert claim["idempotency_key"] == "chat-callback-1"
    enqueue_resume.assert_called_once_with(
        project_id="project-1",
        job_id="job-1",
        task_id="TASK-1",
        thread_id="thread-1",
        resume_revision=2,
    )


def test_chat_abort_uses_canonical_approval_command_path(monkeypatch) -> None:
    scope = ScopeContext(
        account_id="acc-1",
        actor_id="chat-bot",
        principal_type="service_account",
        auth_source="machine",
    )
    job_store = MagicMock()
    job_store.get_job.return_value = {"id": "job-1", "thread_id": "thread-1"}
    job_store.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        "project-1",
        "acc-1",
        "workspace-1",
        "workflow-1",
        "job-1",
    )
    resume_row = WorkflowResumeQueueRow(
        job_id="job-1",
        project_id="project-1",
        task_id="TASK-1",
        resume_revision=2,
        publish_token="publish-2",
    )
    approval_service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: job_store,
        enqueue_workflow_resume=MagicMock(),
        workflow_resume_repository_factory=lambda _bl: MagicMock(),
    )
    approval_service._claim_and_record_decision = MagicMock(
        return_value=(
            {"approval_id": "gate-2", "allowed_actions": ["abort"]},
            "gate-2",
            None,
            None,
            True,
            True,
            resume_row,
        )
    )
    monkeypatch.setattr(reactions_runtime, "_get_job_store", lambda: job_store)
    monkeypatch.setattr(
        reactions_runtime,
        "_approval_reaction_resumer",
        lambda **kwargs: route_composition._resume_approval_reaction(
            approval_command_service=lambda: approval_service,
            **kwargs,
        ),
    )
    direct_cancel = MagicMock(side_effect=AssertionError("direct cancellation path used"))
    monkeypatch.setattr(reactions_runtime, "_cancel_job_from_reaction", direct_cancel)

    result = reactions_runtime._apply_reaction_action(
        {
            "event_type": "notification.interaction_received",
            "job_id": "job-1",
            "correlation_id": "chat-abort-1",
            "payload": {
                "approval_id": "gate-2",
                "approved": False,
                "action": "abort",
                "comment": "",
            },
        },
        {"action": {"type": "reject_job", "resume_action": "abort"}},
        scope=scope,
    )

    assert result["status"] == "resumed"
    decision = approval_service._claim_and_record_decision.call_args.kwargs["decision"]
    assert decision.action == "abort"
    assert decision.approved is False
    assert decision.comment == ""
    direct_cancel.assert_not_called()


@pytest.mark.parametrize(
    ("conflict_code", "outcome_code"),
    [
        ("stale_approval_gate", "stale"),
        ("approval_already_resolved", "already_handled"),
        ("approval_action_not_allowed", "not_allowed"),
    ],
)
def test_chat_reaction_returns_terminal_machine_outcome_without_queueing_resume(
    monkeypatch, conflict_code: str, outcome_code: str
) -> None:
    scope = ScopeContext(account_id="acc-1", actor_id="chat:alice")
    job_store = MagicMock()
    job_store.get_job.return_value = {"id": "job-1", "thread_id": "thread-1"}
    approval_service = MagicMock()
    approval_service.resume_task.side_effect = ServiceConflictError(
        "Approval gate is stale",
        payload={"error": "Approval gate is stale", "code": conflict_code},
    )
    monkeypatch.setattr(reactions_runtime, "_get_job_store", lambda: job_store)
    monkeypatch.setattr(
        reactions_runtime,
        "_approval_reaction_resumer",
        lambda **kwargs: route_composition._resume_approval_reaction(
            approval_command_service=lambda: approval_service,
            **kwargs,
        ),
    )
    # The threaded resume no longer exists: a reaction resumes through the
    # durable approval command only.
    assert not hasattr(reactions_runtime._reaction_runtime, "resume_job_from_reaction")

    result = reactions_runtime._apply_reaction_action(
        {
            "event_type": "notification.interaction_received",
            "job_id": "job-1",
            "correlation_id": "chat-stale-callback",
            "payload": {
                "approval_id": "gate-a",
                "approved": True,
                "action": "approve",
            },
        },
        {"action": {"type": "approve_job"}},
        scope=scope,
    )

    assert result == {
        "status": "ignored",
        "reason": conflict_code,
        "outcome_code": outcome_code,
        "executed": False,
    }


@pytest.fixture(scope="module")
def client():
    """Create a TestClient with auth disabled."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app_factory import create_app

        with patch.dict(os.environ, {"FS_AUTH_ALLOW_DISABLED": "1"}):
            app = create_app()
            app.state.local_auth_enabled_override = lambda: False
            app.state.external_auth_enabled_override = lambda: False
            yield TestClient(app, raise_server_exceptions=False)
    except Exception as e:
        _skip_only_missing_optional_dep(e, context="Could not create TestClient")


@pytest.fixture
def mock_bl():
    return _make_mock_bl()


@pytest.fixture
def patched_bl(client, mock_bl):
    """Bind a mock Flow Steward logger to the current app."""
    with patch.dict(
        client.app.state.runtime_container.service_factories,
        {"db": lambda: mock_bl},
    ):
        yield mock_bl


@pytest.fixture
def patched_bl_with_workflow(patched_bl):
    """Patch _workflow_exists to return True."""
    with patch.object(reactions_runtime, "_workflow_exists", return_value=True):
        yield patched_bl


# ---------------------------------------------------------------------------
# TestReactionCRUD — list/create/update reactions via API
# ---------------------------------------------------------------------------


class TestReactionCRUD:
    def test_list_reactions_empty(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.list_workflow_reactions.return_value = []
        resp = client.get("/api/workflows/wf-deploy/reactions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["workflow_id"] == "wf-deploy"
        assert data["reactions"] == []

    def test_list_reactions_returns_existing(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.list_workflow_reactions.return_value = [
            {
                "reaction_key": "on-comment",
                "event_type": "external.comment.received",
                "conditions": {},
                "action": {"type": "resume_job"},
                "enabled": True,
                "debounce_seconds": 0,
            },
        ]
        resp = client.get("/api/workflows/wf-deploy/reactions")
        assert resp.status_code == 200
        assert len(resp.json()["reactions"]) == 1
        assert resp.json()["reactions"][0]["reaction_key"] == "on-comment"

    def test_list_reactions_workflow_not_found(self, client, patched_bl):
        with patch.object(reactions_runtime, "_workflow_exists", return_value=False):
            resp = client.get("/api/workflows/nonexistent/reactions")
        assert resp.status_code == 404

    def test_list_reactions_db_unavailable(self, client):
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"db": lambda: None},
        ):
            resp = client.get("/api/workflows/wf-deploy/reactions")
        assert resp.status_code == 503

    def test_save_reactions(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.list_workflow_reactions.return_value = []
        body = {
            "reactions": [
                {
                    "reaction_key": "on-complete",
                    "event_type": "job.completed",
                    "conditions": {"payload.status": "done"},
                    "action": {"type": "resume_job"},
                    "enabled": True,
                    "debounce_seconds": 10,
                },
            ],
        }
        resp = client.post("/api/workflows/wf-deploy/reactions", json=body)
        assert resp.status_code == 200
        patched_bl_with_workflow.replace_scoped_workflow_reactions.assert_called_once()

    def test_save_reactions_workflow_not_found(self, client, patched_bl):
        with patch.object(reactions_runtime, "_workflow_exists", return_value=False):
            resp = client.post("/api/workflows/nonexistent/reactions", json={"reactions": []})
        assert resp.status_code == 404

    def test_save_reactions_db_unavailable(self, client):
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"db": lambda: None},
        ):
            resp = client.post("/api/workflows/wf-deploy/reactions", json={"reactions": []})
        assert resp.status_code == 503

    def test_save_multiple_reactions(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.list_workflow_reactions.return_value = []
        body = {
            "reactions": [
                {
                    "reaction_key": "rule-a",
                    "event_type": "job.completed",
                    "conditions": {},
                    "action": {"type": "resume_job"},
                },
                {
                    "reaction_key": "rule-b",
                    "event_type": "job.failed",
                    "conditions": {"payload.status": "done"},
                    "action": {"type": "noop"},
                },
            ],
        }
        resp = client.post("/api/workflows/wf-deploy/reactions", json=body)
        assert resp.status_code == 200
        call_args = patched_bl_with_workflow.replace_scoped_workflow_reactions.call_args
        saved_rules = call_args[0][1]
        assert len(saved_rules) == 2

    def test_save_uses_persisted_target_scope_response(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.replace_scoped_workflow_reactions.return_value = (
            "group",
            "build",
            [
                {
                    "reaction_key": "notify_2",
                    "display_name": "Canonical",
                    "event_type": "job.group_completed",
                    "action": {"type": "noop"},
                    "idempotency_key_template": None,
                    "scope_kind": "group",
                    "scope_id": "build",
                },
            ],
        )
        response = client.post(
            "/api/workflows/wf-deploy/reactions?scope_kind=group&scope_id=build",
            json={
                "reactions": [
                    {
                        "reaction_key": "notify",
                        "event_type": "job.group_completed",
                        "action": {"type": "noop"},
                    }
                ]
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert (data["scope_kind"], data["scope_id"]) == ("group", "build")
        assert [(row["reaction_key"], row["display_name"]) for row in data["reactions"]] == [
            ("notify_2", "Canonical")
        ]
        assert data["reactions"][0]["idempotency_key_template"] is None
        patched_bl_with_workflow.list_workflow_reactions.assert_not_called()

    def test_save_reactions_requires_project_context_for_extension_sources(
        self, client, patched_bl_with_workflow
    ):
        patched_bl_with_workflow.list_workflow_reactions.return_value = []
        body = {
            "reactions": [
                {
                    "reaction_key": "mail-comment",
                    "event_type": "external.comment.received",
                    "conditions": {"source": "mail_hook"},
                    "action": {"type": "resume_job"},
                    "enabled": True,
                },
            ],
        }
        resp = client.post("/api/workflows/wf-deploy/reactions", json=body)
        assert resp.status_code == 400

    def test_save_reactions_accepts_extension_sources_for_selected_project(
        self, client, patched_bl_with_workflow
    ):
        patched_bl_with_workflow.get_project.return_value = {
            "id": 7,
            "account_id": DEFAULT_ACCOUNT_ID,
            "name": "Demo Project",
        }
        patched_bl_with_workflow.list_workflow_reactions.return_value = []
        patched_bl_with_workflow.list_project_connections.return_value = [
            {"connection_id": "conn_mail", "extension_id": "mail_hook", "enabled": True},
        ]
        patched_bl_with_workflow.get_extension.return_value = {
            "extension_key": "mail_hook",
            "display_name": "Mail Hook",
            "enabled": True,
            "compiled_manifest": {
                "supported_events": ["external.comment.received"],
            },
        }
        body = {
            "reactions": [
                {
                    "reaction_key": "mail-comment",
                    "event_type": "external.comment.received",
                    "conditions": {"source": "mail_hook"},
                    "action": {"type": "resume_job"},
                    "enabled": True,
                },
            ],
        }
        resp = client.post("/api/workflows/wf-deploy/reactions?project_id=7", json=body)
        assert resp.status_code == 200
        assert resp.json()["project_id"] == "7"
        saved_rules = patched_bl_with_workflow.replace_scoped_workflow_reactions.call_args[0][1]
        assert saved_rules[0]["conditions"]["source"] == "mail_hook"

    def test_list_with_group_scope(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.list_workflow_reactions.return_value = [
            {
                "reaction_key": "scope::group::build::on-done",
                "event_type": "job.group_completed",
                "conditions": {},
                "action": {"type": "noop"},
                "enabled": True,
            },
            {
                "reaction_key": "wf-rule",
                "event_type": "external.comment.received",
                "conditions": {},
                "action": {"type": "resume_job"},
                "enabled": True,
            },
        ]
        resp = client.get("/api/workflows/wf-deploy/reactions?scope_kind=group&scope_id=build")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope_kind"] == "group"
        assert data["scope_id"] == "build"
        assert len(data["reactions"]) == 1
        assert data["reactions"][0]["reaction_key"] == "on-done"

    def test_list_group_scope_missing_scope_id(self, client, patched_bl_with_workflow):
        resp = client.get("/api/workflows/wf-deploy/reactions?scope_kind=group&scope_id=")
        assert resp.status_code == 400

    def test_list_rejects_the_retired_phase_scope(self, client, patched_bl_with_workflow):
        resp = client.get("/api/workflows/wf-deploy/reactions?scope_kind=phase&scope_id=build")
        assert resp.status_code == 400

    def test_save_rejects_retired_phase_events(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.list_workflow_reactions.return_value = []
        for event_type in ("job.phase_completed", "job.gate_waiting"):
            body = {
                "reactions": [
                    {
                        "reaction_key": "legacy",
                        "event_type": event_type,
                        "conditions": {},
                        "action": {"type": "noop"},
                    }
                ]
            }
            resp = client.post("/api/workflows/wf-deploy/reactions", json=body)
            assert resp.status_code == 400
            patched_bl_with_workflow.replace_scoped_workflow_reactions.assert_not_called()


# ---------------------------------------------------------------------------
# TestReactionIngestion — ingest endpoint and event processing
# ---------------------------------------------------------------------------


class TestReactionIngestion:
    def test_ingest_basic_event(self, client, patched_bl_with_workflow):
        body = {
            "event_type": "external.comment.received",
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "source": "sample_tasks",
            "payload": {"text": "looks good"},
        }
        from tests import api_runtime_test_surface as api_runtime

        with patch.object(api_runtime, "_evaluate_workflow_reactions", return_value=[]):
            resp = client.post("/api/reactions/ingest", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert "event" in data
        assert "reactions" in data

    def test_ingest_without_job_id(self, client, patched_bl_with_workflow):
        body = {
            "event_type": "system.health_check",
            "workflow_id": "wf-deploy",
            "source": "monitoring",
            "payload": {},
        }
        from tests import api_runtime_test_surface as api_runtime

        with patch.object(api_runtime, "_evaluate_workflow_reactions", return_value=[]):
            resp = client.post("/api/reactions/ingest", json=body)
        assert resp.status_code == 200

    def test_ingest_job_not_found(self, client, patched_bl_with_workflow):
        patched_bl_with_workflow.get_job.return_value = None
        body = {
            "event_type": "external.comment.received",
            "job_id": 999,
            "source": "sample_tasks",
            "payload": {},
        }
        resp = client.post("/api/reactions/ingest", json=body)
        assert resp.status_code == 404

    def test_ingest_workflow_not_found(self, client, patched_bl):
        with patch.object(reactions_runtime, "_workflow_exists", return_value=False):
            patched_bl.get_job.return_value = None
            body = {
                "event_type": "external.comment.received",
                "workflow_id": "nonexistent",
                "source": "external",
                "payload": {},
            }
            resp = client.post("/api/reactions/ingest", json=body)
        assert resp.status_code == 404

    def test_ingest_db_unavailable(self, client):
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"db": lambda: None},
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "event_type": "test",
                    "source": "test",
                    "payload": {},
                },
            )
        assert resp.status_code == 503

    def test_ingest_triggers_default_comment_resume(self, client, patched_bl_with_workflow):
        """When no explicit rules, external.comment.received should trigger the default resume rule."""
        patched_bl_with_workflow.list_workflow_reactions.return_value = []
        event = {
            "id": 42,
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "source_id": "",
            "event_key": "",
            "payload": {"text": "approved"},
            "processed": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        patched_bl_with_workflow.log_job_reaction_event.return_value = event

        with patch.object(
            reactions_runtime,
            "_resume_job_from_reaction",
            return_value={"status": "resumed", "executed": True},
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "event_type": "external.comment.received",
                    "job_id": 100,
                    "workflow_id": "wf-deploy",
                    "source": "sample_tasks",
                    "payload": {"text": "approved"},
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        reactions = data.get("reactions", [])
        assert len(reactions) >= 1
        assert reactions[0]["reaction_key"] == "__default_external_comment_resume__"

    def test_ingest_with_dedupe_key(self, client, patched_bl_with_workflow):
        body = {
            "event_type": "external.comment.received",
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "source": "sample_tasks",
            "dedupe_key": "sample_tasks-comment-abc123",
            "payload": {"text": "ok"},
        }
        from tests import api_runtime_test_surface as api_runtime

        with patch.object(api_runtime, "_evaluate_workflow_reactions", return_value=[]):
            resp = client.post("/api/reactions/ingest", json=body)
        assert resp.status_code == 200

    def test_ingest_enqueues_domain_event(self, client, patched_bl_with_workflow):
        body = {
            "event_type": "external.comment.received",
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "source": "sample_tasks",
            "payload": {},
        }
        from tests import api_runtime_test_surface as api_runtime

        with patch.object(api_runtime, "_evaluate_workflow_reactions", return_value=[]):
            resp = client.post("/api/reactions/ingest", json=body)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestJobReactions — per-job reaction events endpoint
# ---------------------------------------------------------------------------


class TestJobReactions:
    def test_list_job_reactions(self, client, patched_bl):
        patched_bl.list_job_reaction_events.return_value = [
            {"id": 1, "event_type": "external.comment.received", "processed": True},
        ]
        resp = client.get("/api/jobs/100/reactions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "100"
        assert len(data["reactions"]) == 1

    def test_list_job_reactions_empty(self, client, patched_bl):
        patched_bl.list_job_reaction_events.return_value = []
        resp = client.get("/api/jobs/100/reactions")
        assert resp.status_code == 200
        assert resp.json()["reactions"] == []

    def test_list_job_reactions_job_not_found(self, client, patched_bl):
        patched_bl.get_job.return_value = None
        resp = client.get("/api/jobs/999/reactions")
        assert resp.status_code == 404

    def test_list_job_reactions_db_unavailable(self, client):
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"db": lambda: None},
        ):
            resp = client.get("/api/jobs/100/reactions")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# TestReactionConditionEvaluation — _reaction_value_matches, _reaction_conditions_match
# ---------------------------------------------------------------------------


class TestReactionConditionEvaluation:
    @pytest.fixture(autouse=True)
    def _import_serve(self):
        self.api_runtime = reactions_runtime

    # -- _reaction_value_matches --

    def test_value_matches_exact_string(self):
        assert self.api_runtime._reaction_value_matches("foo", "foo") is True

    def test_value_matches_exact_int(self):
        assert self.api_runtime._reaction_value_matches(42, 42) is True

    def test_value_no_match(self):
        assert self.api_runtime._reaction_value_matches("foo", "bar") is False

    def test_value_matches_in_list(self):
        assert (
            self.api_runtime._reaction_value_matches("warning", ["info", "warning", "error"])
            is True
        )

    def test_value_not_in_list(self):
        assert (
            self.api_runtime._reaction_value_matches("debug", ["info", "warning", "error"]) is False
        )

    def test_value_matches_nested_dict(self):
        actual = {"level": "error", "code": 500}
        expected = {"level": "error"}
        assert self.api_runtime._reaction_value_matches(actual, expected) is True

    def test_value_nested_dict_missing_key(self):
        actual = {"level": "error"}
        expected = {"level": "error", "code": 500}
        assert self.api_runtime._reaction_value_matches(actual, expected) is False

    def test_value_dict_expected_but_actual_not_dict(self):
        assert self.api_runtime._reaction_value_matches("string", {"key": "val"}) is False

    def test_value_matches_deeply_nested(self):
        actual = {"a": {"b": {"c": 1}}}
        expected = {"a": {"b": {"c": 1}}}
        assert self.api_runtime._reaction_value_matches(actual, expected) is True

    def test_value_matches_none(self):
        assert self.api_runtime._reaction_value_matches(None, None) is True

    def test_value_matches_bool(self):
        assert self.api_runtime._reaction_value_matches(True, True) is True
        assert self.api_runtime._reaction_value_matches(True, False) is False

    # -- _reaction_conditions_match --

    def test_conditions_empty_matches(self):
        event = {"event_type": "test", "source": "ext", "payload": {}}
        assert self.api_runtime._reaction_conditions_match(event, {}) is True

    def test_conditions_none_matches(self):
        event = {"event_type": "test", "source": "ext", "payload": {}}
        assert self.api_runtime._reaction_conditions_match(event, None) is True

    def test_conditions_event_type_match(self):
        event = {"event_type": "external.comment.received", "source": "sample_tasks", "payload": {}}
        conditions = {"event_type": "external.comment.received"}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True

    def test_conditions_event_type_mismatch(self):
        event = {"event_type": "external.comment.received", "source": "sample_tasks", "payload": {}}
        conditions = {"event_type": "external.status.changed"}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is False

    def test_conditions_source_match(self):
        event = {"event_type": "test", "source": "sentry", "payload": {}}
        conditions = {"source": "sentry"}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True

    def test_conditions_payload_dotted_key(self):
        event = {
            "event_type": "test",
            "source": "ext",
            "payload": json.dumps({"severity": "critical", "details": {"code": 500}}),
        }
        conditions = {"payload.severity": "critical"}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True

    def test_conditions_payload_nested_dotted_key(self):
        event = {
            "event_type": "test",
            "source": "ext",
            "payload": json.dumps({"details": {"code": 500}}),
        }
        conditions = {"payload.details.code": 500}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True

    def test_conditions_payload_dotted_key_missing(self):
        event = {
            "event_type": "test",
            "source": "ext",
            "payload": json.dumps({"severity": "info"}),
        }
        conditions = {"payload.nonexistent": "val"}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is False

    def test_conditions_multiple_all_match(self):
        event = {
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "payload": json.dumps({"status": "approved"}),
        }
        conditions = {
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "payload.status": "approved",
        }
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True

    def test_conditions_multiple_partial_mismatch(self):
        event = {
            "event_type": "external.comment.received",
            "source": "github",
            "payload": json.dumps({"status": "approved"}),
        }
        conditions = {
            "event_type": "external.comment.received",
            "source": "sample_tasks",  # mismatch
        }
        assert self.api_runtime._reaction_conditions_match(event, conditions) is False

    def test_conditions_payload_as_dict(self):
        event = {
            "event_type": "test",
            "source": "ext",
            "payload": json.dumps({"level": "error", "code": 500}),
        }
        conditions = {"payload": {"level": "error", "code": 500}}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True

    def test_conditions_payload_as_dict_partial(self):
        event = {
            "event_type": "test",
            "source": "ext",
            "payload": json.dumps({"level": "error", "code": 500, "extra": "data"}),
        }
        conditions = {"payload": {"level": "error"}}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True

    def test_conditions_list_membership(self):
        event = {"event_type": "test", "source": "sample_tasks", "payload": {}}
        conditions = {"source": ["sample_tasks", "github"]}
        assert self.api_runtime._reaction_conditions_match(event, conditions) is True


# ---------------------------------------------------------------------------
# TestReactionDebouncing — duplicate event detection
# ---------------------------------------------------------------------------


class TestReactionDebouncing:
    @pytest.fixture(autouse=True)
    def _import_serve(self):
        self.api_runtime = reactions_runtime

    def test_no_debounce_when_seconds_zero(self, mock_bl):
        event = {
            "job_id": 100,
            "event_type": "test",
            "id": 1,
            "created_at": datetime.now(UTC).isoformat(),
        }
        rule = {"debounce_seconds": 0}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is False

    def test_no_debounce_when_no_job_id(self, mock_bl):
        event = {
            "job_id": None,
            "event_type": "test",
            "id": 1,
            "created_at": datetime.now(UTC).isoformat(),
        }
        rule = {"debounce_seconds": 60}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is False

    def test_no_debounce_when_no_history(self, mock_bl):
        mock_bl.list_job_reaction_events.return_value = []
        event = {
            "job_id": 100,
            "event_type": "test",
            "id": 1,
            "created_at": datetime.now(UTC).isoformat(),
        }
        rule = {"debounce_seconds": 60}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is False

    def test_debounced_within_window(self, mock_bl):
        now = datetime.now(UTC)
        mock_bl.list_job_reaction_events.return_value = [
            {
                "id": 2,
                "event_type": "test",
                "created_at": (now - timedelta(seconds=10)).isoformat(),
            },
        ]
        event = {"job_id": 100, "event_type": "test", "id": 1, "created_at": now.isoformat()}
        rule = {"debounce_seconds": 60}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is True

    def test_not_debounced_outside_window(self, mock_bl):
        now = datetime.now(UTC)
        mock_bl.list_job_reaction_events.return_value = [
            {
                "id": 2,
                "event_type": "test",
                "created_at": (now - timedelta(seconds=120)).isoformat(),
            },
        ]
        event = {"job_id": 100, "event_type": "test", "id": 1, "created_at": now.isoformat()}
        rule = {"debounce_seconds": 60}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is False

    def test_debounce_skips_same_event_id(self, mock_bl):
        """The same event should not debounce itself."""
        now = datetime.now(UTC)
        mock_bl.list_job_reaction_events.return_value = [
            {
                "id": 1,  # same id as current
                "event_type": "test",
                "created_at": (now - timedelta(seconds=5)).isoformat(),
            },
        ]
        event = {"job_id": 100, "event_type": "test", "id": 1, "created_at": now.isoformat()}
        rule = {"debounce_seconds": 60}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is False

    def test_debounce_different_event_type_ignored(self, mock_bl):
        """Events of different type should not trigger debounce."""
        now = datetime.now(UTC)
        mock_bl.list_job_reaction_events.return_value = [
            {
                "id": 2,
                "event_type": "other.event",
                "created_at": (now - timedelta(seconds=5)).isoformat(),
            },
        ]
        event = {"job_id": 100, "event_type": "test", "id": 1, "created_at": now.isoformat()}
        rule = {"debounce_seconds": 60}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is False

    def test_debounce_handles_exception_gracefully(self, mock_bl):
        mock_bl.list_job_reaction_events.side_effect = Exception("DB error")
        event = {
            "job_id": 100,
            "event_type": "test",
            "id": 1,
            "created_at": datetime.now(UTC).isoformat(),
        }
        rule = {"debounce_seconds": 60}
        assert self.api_runtime._reaction_is_debounced(mock_bl, event, rule) is False


# ---------------------------------------------------------------------------
# TestReactionActionExecution — _apply_reaction_action
# ---------------------------------------------------------------------------


class TestReactionActionExecution:
    @pytest.fixture(autouse=True)
    def _import_serve(self):
        self.api_runtime = reactions_runtime

    def test_noop_action(self):
        result = self.api_runtime._apply_reaction_action(
            {"job_id": 100, "payload": {}},
            {"action": {"type": "noop"}},
        )
        assert result["status"] == "skipped"
        assert result["executed"] is False

    def test_empty_action_type(self):
        result = self.api_runtime._apply_reaction_action(
            {"job_id": 100, "payload": {}},
            {"action": {"type": ""}},
        )
        assert result["status"] == "skipped"
        assert result["executed"] is False

    def test_no_action_key(self):
        result = self.api_runtime._apply_reaction_action(
            {"job_id": 100, "payload": {}},
            {"action": {}},
        )
        assert result["status"] == "skipped"

    def test_unsupported_action_type(self):
        result = self.api_runtime._apply_reaction_action(
            {"job_id": 100, "payload": {}},
            {"action": {"type": "send_email"}},
        )
        assert result["status"] == "ignored"
        assert "unsupported" in result["reason"]
        assert result["executed"] is False

    def test_resume_job_action(self):
        with patch.object(
            self.api_runtime, "_resume_job_from_reaction", return_value={"status": "resumed"}
        ):
            result = self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": json.dumps({"text": "go"})},
                {"action": {"type": "resume_job"}},
            )
        assert result.get("executed") is True

    def test_resume_job_no_job_id(self):
        result = self.api_runtime._apply_reaction_action(
            {"job_id": None, "payload": {}},
            {"action": {"type": "resume_job"}},
        )
        assert result["status"] == "ignored"
        assert result["executed"] is False

    def test_resume_job_with_custom_comment(self):
        with patch.object(
            self.api_runtime, "_resume_job_from_reaction", return_value={"status": "resumed"}
        ) as mock_resume:
            self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": json.dumps({"text": "ship it"})},
                {"action": {"type": "resume_job", "comment": "Auto-approved by reaction"}},
            )
            call_args = mock_resume.call_args
            assert call_args[0][1] == "Auto-approved by reaction"

    def test_resume_orchestrator_alias(self):
        with patch.object(
            self.api_runtime, "_resume_job_from_reaction", return_value={"status": "resumed"}
        ):
            result = self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": json.dumps({"text": "ok"})},
                {"action": {"type": "resume_orchestrator"}},
            )
        assert result.get("executed") is True

    def test_resume_job_with_resume_action(self):
        with patch.object(
            self.api_runtime, "_resume_job_from_reaction", return_value={"status": "resumed"}
        ) as mock_resume:
            self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": "{}"},
                {"action": {"type": "resume_job", "resume_action": "retry"}},
            )
            assert mock_resume.call_args.kwargs.get("action") == "retry"

    def test_approve_job_action_uses_resume_helper(self):
        with patch.object(
            self.api_runtime, "_resume_job_from_reaction", return_value={"status": "resumed"}
        ) as mock_resume:
            result = self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": json.dumps({"text": "approve"})},
                {"action": {"type": "approve_job"}},
            )
        assert result.get("executed") is True
        assert mock_resume.call_args.kwargs.get("approved") is True

    def test_reject_job_action_uses_resume_helper(self):
        with patch.object(
            self.api_runtime, "_resume_job_from_reaction", return_value={"status": "resumed"}
        ) as mock_resume:
            result = self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": json.dumps({"text": "reject"})},
                {"action": {"type": "reject_job"}},
            )
        assert result.get("executed") is True
        assert mock_resume.call_args.kwargs.get("approved") is False

    def test_cancel_job_action_delegates_to_cancel_helper(self):
        with patch.object(
            self.api_runtime,
            "_cancel_job_from_reaction",
            return_value={"status": "cancelled", "executed": True},
        ) as mock_cancel:
            result = self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": json.dumps({"text": "cancel"})},
                {"action": {"type": "cancel_job"}},
            )
        assert result["status"] == "cancelled"
        assert result["executed"] is True
        mock_cancel.assert_called_once()

    def test_retry_step_action_delegates_to_retry_helper(self):
        with patch.object(
            self.api_runtime,
            "_retry_interrupted_step_from_reaction",
            return_value={"status": "pending", "executed": True},
        ) as mock_retry:
            result = self.api_runtime._apply_reaction_action(
                {"job_id": 100, "payload": "{}"},
                {"action": {"type": "retry_step"}},
            )
        assert result["status"] == "pending"
        assert result["executed"] is True
        mock_retry.assert_called_once_with("100", requested_by="automation")

    def test_notify_action_dispatches_project_notification(self, mock_bl):
        mock_bl.list_notification_routes.return_value = [
            {"route_id": 7, "route_key": "ops_alerts", "provider_key": "fixture_notifier"},
        ]
        with (
            patch.object(self.api_runtime, "_get_job_store", return_value=mock_bl),
            patch(
                "core.application.notifications.dispatch.dispatch_project_notification_event",
                return_value=[{"delivery_id": 1, "status": "delivered"}],
            ) as mock_dispatch,
        ):
            result = self.api_runtime._apply_reaction_action(
                {
                    "job_id": 100,
                    "workflow_id": "wf-deploy",
                    "event_type": "job.failed",
                    "payload": {"text": "broken"},
                },
                {
                    "action": {
                        "type": "notify",
                        "route_key": "ops_alerts",
                        "severity": "error",
                        "message": "Workflow failed",
                    }
                },
            )
        assert result["status"] == "completed"
        assert result["executed"] is True
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["project_id"] == "1"
        assert kwargs["severity"] == "error"
        assert kwargs["route_ids"] == ["7"]
        assert kwargs["ignore_filters"] is True


# ---------------------------------------------------------------------------
# TestEvaluateWorkflowReactions — _evaluate_workflow_reactions
# ---------------------------------------------------------------------------


class TestEvaluateWorkflowReactions:
    @pytest.fixture(autouse=True)
    def _import_serve(self):
        self.api_runtime = reactions_runtime

    def test_no_event_type_returns_empty(self, mock_bl):
        event = {"event_type": "", "workflow_id": "wf-deploy", "job_id": 100}
        results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        assert results == []

    def test_matching_rule_executes(self, mock_bl):
        mock_bl.list_workflow_reactions.return_value = [
            {
                "reaction_key": "auto-resume",
                "event_type": "external.comment.received",
                "conditions": {},
                "action": {"type": "resume_job"},
                "debounce_seconds": 0,
                "enabled": True,
            },
        ]
        event = {
            "event_type": "external.comment.received",
            "workflow_id": "wf-deploy",
            "job_id": 100,
            "payload": json.dumps({"text": "ok"}),
        }
        with patch.object(
            self.api_runtime,
            "_apply_reaction_action",
            return_value={"status": "resumed", "executed": True},
        ):
            results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        assert len(results) == 1
        assert results[0]["reaction_key"] == "auto-resume"

    def test_conditions_not_met_skipped(self, mock_bl):
        mock_bl.list_workflow_reactions.return_value = [
            {
                "reaction_key": "on-sentry",
                "event_type": "external.error",
                "conditions": {"source": "sentry"},
                "action": {"type": "resume_job"},
                "debounce_seconds": 0,
                "enabled": True,
            },
        ]
        event = {
            "event_type": "external.error",
            "workflow_id": "wf-deploy",
            "job_id": 100,
            "source": "github",
            "payload": {},
        }
        results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert results[0]["reason"] == "conditions_not_met"

    def test_debounced_rule_skipped(self, mock_bl):
        mock_bl.list_workflow_reactions.return_value = [
            {
                "reaction_key": "on-comment",
                "event_type": "external.comment.received",
                "conditions": {},
                "action": {"type": "resume_job"},
                "debounce_seconds": 60,
                "enabled": True,
            },
        ]
        event = {
            "event_type": "external.comment.received",
            "workflow_id": "wf-deploy",
            "job_id": 100,
            "payload": {},
        }
        with patch.object(self.api_runtime, "_reaction_is_debounced", return_value=True):
            results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert results[0]["reason"] == "debounced"

    def test_default_comment_resume_fallback(self, mock_bl):
        """When no rules exist, external.comment.received on a job uses the default resume rule."""
        mock_bl.list_workflow_reactions.return_value = []
        event = {
            "event_type": "external.comment.received",
            "workflow_id": "wf-deploy",
            "job_id": 100,
            "payload": json.dumps({"text": "approved"}),
        }
        with patch.object(
            self.api_runtime,
            "_apply_reaction_action",
            return_value={"status": "resumed", "executed": True},
        ):
            results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        assert len(results) == 1
        assert results[0]["reaction_key"] == "__default_external_comment_resume__"

    def test_no_default_fallback_without_job_id(self, mock_bl):
        """Default fallback requires job_id."""
        mock_bl.list_workflow_reactions.return_value = []
        event = {
            "event_type": "external.comment.received",
            "workflow_id": "wf-deploy",
            "job_id": None,
            "payload": {},
        }
        results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        assert results == []

    def test_multiple_rules_evaluated(self, mock_bl):
        mock_bl.list_workflow_reactions.return_value = [
            {
                "reaction_key": "rule-a",
                "event_type": "external.comment.received",
                "conditions": {"source": "sample_tasks"},
                "action": {"type": "resume_job"},
                "debounce_seconds": 0,
                "enabled": True,
            },
            {
                "reaction_key": "rule-b",
                "event_type": "external.comment.received",
                "conditions": {"source": "github"},
                "action": {"type": "noop"},
                "debounce_seconds": 0,
                "enabled": True,
            },
        ]
        event = {
            "event_type": "external.comment.received",
            "workflow_id": "wf-deploy",
            "job_id": 100,
            "source": "sample_tasks",
            "payload": {},
        }
        with patch.object(
            self.api_runtime,
            "_apply_reaction_action",
            return_value={"status": "resumed", "executed": True},
        ):
            results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        # rule-a matches (source=sample_tasks), rule-b skipped (source mismatch)
        assert len(results) == 2
        matched = [r for r in results if r.get("status") != "skipped"]
        skipped = [r for r in results if r.get("status") == "skipped"]
        assert len(matched) == 1
        assert len(skipped) == 1

    def test_list_failure_returns_error(self, mock_bl):
        mock_bl.list_workflow_reactions.side_effect = Exception("DB down")
        event = {
            "event_type": "external.comment.received",
            "workflow_id": "wf-deploy",
            "job_id": 100,
            "payload": {},
        }
        results = self.api_runtime._evaluate_workflow_reactions(mock_bl, event)
        assert len(results) == 1
        assert results[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# TestWorkflowChaining — completion events triggering child workflows
# ---------------------------------------------------------------------------


class TestWorkflowChaining:
    """Test that workflow completion events can be ingested and evaluated as reactions."""

    def test_completion_event_ingestion(self, client, patched_bl_with_workflow):
        """A workflow.completed event can be ingested and processed."""
        event = {
            "id": 50,
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "event_type": "workflow.completed",
            "source": "system",
            "source_id": "",
            "event_key": "",
            "payload": json.dumps({"result": "success", "output_artifact": "build-123"}),
            "processed": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        patched_bl_with_workflow.log_job_reaction_event.return_value = event
        patched_bl_with_workflow.list_workflow_reactions.return_value = []

        body = {
            "event_type": "workflow.completed",
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "source": "system",
            "payload": {"result": "success", "output_artifact": "build-123"},
        }
        resp = client.post("/api/reactions/ingest", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["event"]["event_type"] == "workflow.completed"

    def test_completion_event_triggers_chained_reaction(self, client, patched_bl_with_workflow):
        """A rule matching workflow.completed should fire when that event is ingested."""
        patched_bl_with_workflow.list_workflow_reactions.return_value = [
            {
                "reaction_key": "chain-to-review",
                "event_type": "workflow.completed",
                "conditions": {"payload.result": "success"},
                "action": {"type": "resume_job"},
                "debounce_seconds": 0,
                "enabled": True,
            },
        ]
        event = {
            "id": 51,
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "event_type": "workflow.completed",
            "source": "system",
            "source_id": "",
            "event_key": "",
            "payload": json.dumps({"result": "success"}),
            "processed": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        patched_bl_with_workflow.log_job_reaction_event.return_value = event

        with patch.object(
            reactions_runtime,
            "_resume_job_from_reaction",
            return_value={"status": "resumed", "executed": True},
        ):
            resp = client.post(
                "/api/reactions/ingest",
                json={
                    "event_type": "workflow.completed",
                    "job_id": 100,
                    "workflow_id": "wf-deploy",
                    "source": "system",
                    "payload": {"result": "success"},
                },
            )
        assert resp.status_code == 200
        reactions = resp.json().get("reactions", [])
        executed = [r for r in reactions if r.get("executed")]
        assert len(executed) >= 1
        assert executed[0]["reaction_key"] == "chain-to-review"

    def test_completion_event_skipped_on_failure_result(self, client, patched_bl_with_workflow):
        """A chained rule requiring success should not fire when result is failure."""
        patched_bl_with_workflow.list_workflow_reactions.return_value = [
            {
                "reaction_key": "chain-to-review",
                "event_type": "workflow.completed",
                "conditions": {"payload.result": "success"},
                "action": {"type": "resume_job"},
                "debounce_seconds": 0,
                "enabled": True,
            },
        ]
        event = {
            "id": 52,
            "job_id": 100,
            "workflow_id": "wf-deploy",
            "event_type": "workflow.completed",
            "source": "system",
            "source_id": "",
            "event_key": "",
            "payload": json.dumps({"result": "failure"}),
            "processed": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        patched_bl_with_workflow.log_job_reaction_event.return_value = event

        resp = client.post(
            "/api/reactions/ingest",
            json={
                "event_type": "workflow.completed",
                "job_id": 100,
                "workflow_id": "wf-deploy",
                "source": "system",
                "payload": {"result": "failure"},
            },
        )
        assert resp.status_code == 200
        reactions = resp.json().get("reactions", [])
        assert all(r.get("status") == "skipped" for r in reactions)


# ---------------------------------------------------------------------------
# TestReactionScopeHelpers — scope key composition/splitting
# ---------------------------------------------------------------------------


class TestReactionScopeHelpers:
    @pytest.fixture(autouse=True)
    def _import_serve(self):
        self.api_runtime = reactions_runtime

    def test_compose_workflow_scope(self):
        key = self.api_runtime._compose_scoped_reaction_key("workflow", "", "my-rule")
        assert key == "my-rule"

    def test_compose_group_scope(self):
        key = self.api_runtime._compose_scoped_reaction_key("group", "build", "on-done")
        assert key == "scope::group::build::on-done"

    def test_compose_rejects_the_retired_phase_scope(self):
        with pytest.raises(ValueError, match="phase automation scope was removed"):
            self.api_runtime._compose_scoped_reaction_key("phase", "build", "on-done")

    def test_compose_step_scope(self):
        key = self.api_runtime._compose_scoped_reaction_key("step", "deploy-prod", "on-fail")
        assert key == "scope::step::deploy-prod::on-fail"

    def test_split_workflow_key(self):
        kind, sid, base = self.api_runtime._split_scoped_reaction_key("my-rule")
        assert kind == "workflow"
        assert sid == ""
        assert base == "my-rule"

    def test_split_phase_key(self):
        kind, sid, base = self.api_runtime._split_scoped_reaction_key(
            "scope::phase::build::on-done"
        )
        assert kind == "phase"
        assert sid == "build"
        assert base == "on-done"

    def test_split_step_key(self):
        kind, sid, base = self.api_runtime._split_scoped_reaction_key(
            "scope::step::deploy-prod::on-fail"
        )
        assert kind == "step"
        assert sid == "deploy-prod"
        assert base == "on-fail"

    def test_scope_matches_event_workflow(self):
        rule = {"reaction_key": "my-rule"}
        event = {"payload": {}}
        assert self.api_runtime._reaction_scope_matches_event(rule, event) is True

    def test_scope_matches_event_group(self):
        rule = {"reaction_key": "scope::group::build::on-done"}
        event = {"payload": json.dumps({"group_id": "build"})}
        assert self.api_runtime._reaction_scope_matches_event(rule, event) is True

    def test_scope_no_match_event_group(self):
        rule = {"reaction_key": "scope::group::build::on-done"}
        event = {"payload": json.dumps({"group_id": "test"})}
        assert self.api_runtime._reaction_scope_matches_event(rule, event) is False

    def test_scope_never_matches_a_retired_phase_rule(self):
        rule = {"reaction_key": "scope::phase::build::on-done"}
        event = {"payload": json.dumps({"phase_id": "build"})}
        assert self.api_runtime._reaction_scope_matches_event(rule, event) is False

    def test_normalize_rejects_an_unknown_scope(self):
        with pytest.raises(ValueError, match="Unsupported automation scope 'invalid'"):
            self.api_runtime._normalize_reaction_scope("invalid", "some-id")


_RECIPIENT_CASES = json.loads(
    (
        Path(__file__).parents[2]
        / "assets/frontend/src/test/fixtures/notification-recipient-cases.json"
    ).read_text()
)


@pytest.mark.parametrize("case", _RECIPIENT_CASES, ids=lambda case: case["name"])
def test_declared_email_recipients_are_admitted_before_replacement(
    client, patched_bl_with_workflow, case
):
    store = patched_bl_with_workflow
    store.get_extension.return_value = {
        "compiled_manifest": {
            "schemas": {
                "subscriptions": {
                    "properties": {
                        "to": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string", "format": "email"}},
                            ]
                        }
                    }
                }
            }
        }
    }
    existing = [
        {
            "reaction_key": "old-invalid",
            "enabled": False,
            "action": {
                "type": "notify",
                "target": "extension_channel",
                "provider_key": "acme.mailer",
                "recipients": ["not-an-email"],
            },
        }
    ]
    store.list_workflow_reactions.return_value = existing
    rule = {
        "reaction_key": "old-invalid",
        "event_type": "job.completed",
        "enabled": True,
        "action": {
            "type": "notify",
            "target": "extension_channel",
            "provider_key": "acme.mailer",
            "recipients": case["recipients"],
        },
    }
    response = client.post("/api/workflows/wf-deploy/reactions", json={"reactions": [rule]})
    assert response.status_code == (200 if case["valid"] else 400), response.text
    if case["valid"]:
        assert (
            store.replace_scoped_workflow_reactions.call_args.args[1][0]["action"] == rule["action"]
        )
    else:
        store.replace_scoped_workflow_reactions.assert_not_called()
        assert store.list_workflow_reactions.return_value == existing
        assert "valid email addresses" in response.text


@pytest.mark.parametrize(
    "declared,enabled,target",
    [
        (True, False, "extension_channel"),
        (False, True, "extension_channel"),
        (True, True, "route_keys"),
    ],
)
def test_recipient_admission_preserves_disabled_drafts_and_other_contracts(
    client, patched_bl_with_workflow, declared, enabled, target
):
    store = patched_bl_with_workflow
    item = {"type": "string", **({"format": "email"} if declared else {})}
    store.get_extension.return_value = {
        "schemas": {"subscriptions": {"properties": {"to": {"type": "array", "items": item}}}}
    }
    rule = {
        "event_type": "job.completed",
        "enabled": enabled,
        "action": {
            "type": "notify",
            "target": target,
            "provider_key": "acme.mailer",
            "recipients": ["route:ops"],
            "route_keys": ["ops"],
        },
    }
    response = client.post("/api/workflows/wf-deploy/reactions", json={"reactions": [rule]})
    assert response.status_code == 200, response.text
    assert store.replace_scoped_workflow_reactions.call_args.args[1][0]["action"] == rule["action"]


@pytest.mark.parametrize(
    "action_type,target",
    [
        ("NOTIFY", "extension_channel"),
        (" notify ", "extension_channel"),
        ("NoTiFy", "extension_channel"),
        ("notify", "EXTENSION_CHANNEL"),
        ("notify", " extension_channel "),
        (" Notify ", " Extension_Channel "),
    ],
)
@pytest.mark.parametrize(
    "enabled,recipients,status",
    [
        (True, ["not-an-email"], 400),
        (True, ["Ops+tag@Example.COM"], 200),
        (False, ["not-an-email"], 200),
    ],
)
def test_email_admission_matches_runtime_discriminator_normalization(
    client, patched_bl_with_workflow, action_type, target, enabled, recipients, status
):
    store = patched_bl_with_workflow
    store.get_extension.return_value = {
        "schemas": {
            "subscriptions": {
                "properties": {
                    "to": {"type": "array", "items": {"type": "string", "format": "email"}}
                }
            }
        }
    }
    existing = [{"reaction_key": "old-invalid", "enabled": False}]
    store.list_workflow_reactions.return_value = existing
    action = {
        "type": action_type,
        "target": target,
        "provider_key": "acme.mailer",
        "recipients": recipients,
    }
    response = client.post(
        "/api/workflows/wf-deploy/reactions",
        json={"reactions": [{"event_type": "job.completed", "enabled": enabled, "action": action}]},
    )
    assert response.status_code == status, response.text
    if status == 400:
        store.replace_scoped_workflow_reactions.assert_not_called()
        assert store.list_workflow_reactions.return_value == existing
        assert "valid email addresses" in response.text
    else:
        assert store.replace_scoped_workflow_reactions.call_args.args[1][0]["action"] == action
