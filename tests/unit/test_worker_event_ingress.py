from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.jobs.workflow_resume_state import WorkflowResumeQueueRow
from core.application.management.command_models import ProviderEventIngestInput
from core.application.management.command_services_approval import ApprovalCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext
from core.entrypoints.cli import worker_event_ingress


def _worker_store(*, processed: bool = False) -> MagicMock:
    store = MagicMock()
    store.get_project.return_value = {"id": "project-1", "account_id": DEFAULT_ACCOUNT_ID}
    store.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "thread_id": "thread-1",
        "meta": {"selected_workflow_id": "weekly_report"},
    }
    store.get_project_connection.return_value = {
        "extension_id": "acme.chat",
        "enabled": True,
        "connection_config": {
            "provider_key": "acme.chat",
            "channels": [
                {
                    "channel_id": "approvals",
                    "chat_id": "-100123",
                    "enabled": True,
                    "capabilities": {"human_decision": True},
                }
            ],
        },
    }
    store.list_workflow_reactions.return_value = []
    store.record_domain_event.return_value = {"event_id": "11111111-1111-4111-8111-111111111111"}
    store.log_job_reaction_event.return_value = {
        "id": 101,
        "job_id": "job-1",
        "workflow_id": "weekly_report",
        "processed": processed,
    }
    store.mark_job_reaction_event_processed.return_value = {
        "id": 101,
        "processed": True,
    }
    return store


def _poll_invocation(action: str = "abort") -> dict:
    return {
        "response": {
            "events": [
                {
                    "event_type": "notification.interaction_received",
                    "source": "chat.callback_query",
                    "workflow_id": "weekly_report",
                    "job_id": "job-1",
                    "payload": {
                        "interaction_kind": "approval_decision",
                        "approval_id": "gate-1",
                        "action": action,
                        "approved": action == "approve",
                        "comment": "",
                        "job_id": "job-1",
                        "source_id": f"poll-{action}-1",
                        "event_key": f"chat.callback_query.{action}",
                        "chat": {
                            "channel_id": "approvals",
                            "chat_id": -100123,
                        },
                        "reply_target": {
                            "provider_key": "acme.chat",
                            "channel_id": "approvals",
                            "external_conversation_ref": "-100123",
                            "external_message_ref": "44",
                        },
                    },
                }
            ]
        }
    }


def _run_worker_poll(
    monkeypatch,
    *,
    store: MagicMock,
    approval_service: MagicMock,
    dispatch: MagicMock,
    action: str,
):
    invocation = _poll_invocation(action)
    store.log_job_reaction_event.return_value.update(
        {
            "event_type": "notification.interaction_received",
            "payload": invocation["response"]["events"][0]["payload"],
        }
    )
    monkeypatch.setattr(
        worker_event_ingress,
        "worker_event_ingress_ports",
        lambda: SimpleNamespace(
            fetch_provider_attachments=MagicMock(return_value=[]),
            dispatch_project_notification_event=dispatch,
        ),
    )
    monkeypatch.setattr(worker_event_ingress, "workflow_exists", lambda _workflow_id: True)

    def process_envelope(invocation, **kwargs):
        service = kwargs["event_command_service_factory"]()
        return service.ingest_provider_events(
            ProviderEventIngestInput(
                provider_key="acme.chat",
                project_id="project-1",
                events=invocation["response"]["events"],
            ),
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                project_id="project-1",
                actor_id="chat_polling_worker",
                principal_type="service_account",
                auth_source="machine",
            ),
        )

    processor = worker_event_ingress.build_worker_inbound_poll_response_processor(
        process_extension_response_envelope=process_envelope,
        approval_command_service_builder=lambda **_kwargs: approval_service,
    )
    return processor(invocation, bl=store)


def test_worker_polling_abort_uses_canonical_decision_and_dispatches_stopped_outcome(
    monkeypatch,
) -> None:
    store = _worker_store()
    store.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        "project-1",
        DEFAULT_ACCOUNT_ID,
        "workspace-1",
        "weekly_report",
        "job-1",
    )
    resume_row = WorkflowResumeQueueRow(
        job_id="job-1",
        project_id="project-1",
        task_id="TASK-1",
        resume_revision=1,
        publish_token="publish-1",
    )
    approval_service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: store,
        enqueue_workflow_resume=MagicMock(),
        workflow_resume_repository_factory=lambda _bl: MagicMock(),
    )
    approval_service._claim_and_record_decision = MagicMock(
        return_value=(
            {"approval_id": "gate-1", "allowed_actions": ["abort"]},
            "gate-1",
            None,
            None,
            True,
            True,
            resume_row,
        )
    )
    dispatch = MagicMock(return_value=[{"status": "delivered"}])

    result = _run_worker_poll(
        monkeypatch,
        store=store,
        approval_service=approval_service,
        dispatch=dispatch,
        action="abort",
    )

    decision = approval_service._claim_and_record_decision.call_args.kwargs["decision"]
    assert decision.action == "abort"
    assert decision.approved is False
    assert decision.comment == ""
    resolution = dispatch.call_args.kwargs
    assert resolution["event_type"] == "approval.resolved"
    assert resolution["payload"]["applied"] is True
    assert resolution["payload"]["next_transition"] == "stopped"
    assert result.notification_deliveries == [{"status": "delivered"}]


@pytest.mark.parametrize(
    ("conflict_code", "outcome_code"),
    [
        ("stale_approval_gate", "stale"),
        ("approval_already_resolved", "already_handled"),
        ("approval_action_not_allowed", "not_allowed"),
    ],
)
def test_worker_polling_dispatches_terminal_non_applied_outcome(
    monkeypatch, conflict_code: str, outcome_code: str
) -> None:
    store = _worker_store()
    approval_service = MagicMock()
    approval_service.resume_task.side_effect = ServiceConflictError(
        "Approval conflict",
        payload={"code": conflict_code},
    )
    dispatch = MagicMock(return_value=[])

    _run_worker_poll(
        monkeypatch,
        store=store,
        approval_service=approval_service,
        dispatch=dispatch,
        action="abort",
    )

    resolution = dispatch.call_args.kwargs
    assert resolution["event_type"] == "approval.resolved"
    assert resolution["payload"]["applied"] is False
    assert resolution["payload"]["outcome_code"] == outcome_code
    store.mark_job_reaction_event_processed.assert_called_once_with("101", processed=True)


def test_worker_event_service_creates_jobs_through_the_job_level_enqueue(monkeypatch) -> None:
    """A trigger started from a polled event needs the job-creating enqueue.

    The raw queue publisher takes no ``test_mode`` and creates no job row, so every
    provider trigger fired by background polling failed with an unexpected keyword.
    """
    handler = MagicMock(name="job_level_enqueue")
    monkeypatch.setattr(
        worker_event_ingress,
        "worker_event_ingress_ports",
        lambda: SimpleNamespace(
            fetch_provider_attachments=MagicMock(return_value=[]),
            dispatch_project_notification_event=MagicMock(),
        ),
    )
    monkeypatch.setattr(
        worker_event_ingress, "create_enqueue_task_handler", lambda **_kwargs: handler
    )
    factory = worker_event_ingress._event_command_service_factory(
        bl=_worker_store(),
        approval_command_service_factory=lambda: MagicMock(),
    )

    assert factory()._enqueue_task is handler


def test_the_worker_slice_takes_the_enqueue_handler_without_the_management_stack() -> None:
    """The handler lives in its own module so the worker image stays small.

    Importing it from management_runtime pulled portability, authoring and the template
    catalog (31 modules) into the worker's source slice.
    """
    import json
    from pathlib import Path

    slice_path = Path(__file__).resolve().parents[2] / "infra/docker/source-slices/worker.json"
    files = json.dumps(json.loads(slice_path.read_text(encoding="utf-8")))

    assert "core/application/jobs/enqueue_task_handler.py" in files
    assert "core/application/management/management_runtime.py" not in files
