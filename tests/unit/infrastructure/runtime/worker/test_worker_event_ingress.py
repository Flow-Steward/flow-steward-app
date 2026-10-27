from __future__ import annotations

from types import SimpleNamespace

_EXTENSION_ID = "acme.inbound-provider"


def test_worker_event_service_routes_provider_decision_to_approval_command_service() -> None:
    from core.application.management.command_models import ProviderEventIngestInput
    from core.application.management.scope import ScopeContext
    from core.entrypoints.cli.worker_event_ingress import (
        _event_command_service_factory,
    )

    class Store:
        def __init__(self) -> None:
            self.reaction_processed = False

        def get_project(self, project_id: str):
            return {"id": project_id, "account_id": "account-1"}

        def get_project_connection(self, **_kwargs):
            return {
                "extension_id": _EXTENSION_ID,
                "enabled": True,
                "connection_config": {
                    "channels": [
                        {
                            "channel_id": "approvals",
                            "chat_id": "-100123",
                            "enabled": True,
                            "capabilities": {"human_decision": True},
                        }
                    ]
                },
            }

        def get_job(self, job_id: str):
            return {
                "id": job_id,
                "project_id": "project-1",
                "thread_id": "thread-1",
                "meta": {},
            }

        def record_domain_event(self, **kwargs):
            return {"event_id": kwargs["event_id"]}

        def log_job_reaction_event(self, **kwargs):
            return {
                "id": "reaction-1",
                "processed": False,
                "event_type": kwargs["event_type"],
                "job_id": kwargs["job_id"],
                "payload": kwargs["payload"],
            }

        def enqueue_domain_event(self, **_kwargs):
            return None

        def mark_job_reaction_event_processed(self, _event_id: str, *, processed: bool):
            self.reaction_processed = processed
            return {"id": "reaction-1", "processed": processed}

    store = Store()
    approval_calls = []

    class ApprovalService:
        def resume_task(self, **kwargs):
            approval_calls.append(kwargs)
            return SimpleNamespace(status="in_progress")

    service = _event_command_service_factory(
        bl=store,
        approval_command_service_factory=ApprovalService,
    )()
    service.ingest_provider_events(
        ProviderEventIngestInput(
            provider_key=_EXTENSION_ID,
            project_id="project-1",
            events=[
                {
                    "event_type": "notification.interaction_received",
                    "job_id": "job-1",
                    "source": "chat.callback_query",
                    "payload": {
                        "approved": True,
                        "action": "approve",
                        "approval_id": "approval-1",
                        "source_id": "callback-1",
                        "event_key": "chat.callback_query.approve",
                        "chat": {"chat_id": -100123, "message_id": 94},
                    },
                }
            ],
        ),
        scope=ScopeContext(
            account_id="account-1",
            project_id="project-1",
            principal_type="system_actor",
        ),
    )

    assert len(approval_calls) == 1
    assert approval_calls[0]["thread_id"] == "thread-1"
    assert approval_calls[0]["decision"].approval_id == "approval-1"
    assert approval_calls[0]["decision"].approved is True
    assert approval_calls[0]["idempotency_key"] == (
        f"{_EXTENSION_ID}:chat.callback_query.approve:callback-1"
    )
    assert store.reaction_processed is True
