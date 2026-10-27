from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.operation_support import process_extension_response_envelope
from core.application.management.command_services_common import (
    _process_extension_response_envelope as process_worker_extension_response_envelope,
)
from core.application.management.scope import ScopeContext


def test_response_events_use_explicitly_injected_command_service() -> None:
    project_id = "58eeaeb3-a9c1-4c51-b292-14eb86899d86"
    invocation = {
        "response": {
            "ok": True,
            "events": [
                {
                    "event_type": "external.comment.received",
                    "project_id": project_id,
                    "payload": {"approved": True},
                }
            ],
        }
    }
    extension = {"compiled_manifest": {"supported_events": ["external.comment.received"]}}
    ingest_provider_events = MagicMock()
    event_service = SimpleNamespace(ingest_provider_events=ingest_provider_events)
    factory = MagicMock(return_value=event_service)

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.emit_extension_event"
    ) as emit_extension_event:
        process_extension_response_envelope(
            invocation,
            extension,
            "fixture_events",
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                project_id=project_id,
                actor_id="test-actor",
            ),
            audit={"project_id": project_id},
            bl=MagicMock(),
            event_command_service_factory=factory,
        )

    factory.assert_called_once_with()
    ingest_provider_events.assert_called_once()
    request = ingest_provider_events.call_args.args[0]
    assert request.provider_key == "fixture_events"
    assert request.project_id == project_id
    emit_extension_event.assert_not_called()


def test_response_event_fallback_preserves_audit_project_scope() -> None:
    project_id = "58eeaeb3-a9c1-4c51-b292-14eb86899d86"
    invocation = {
        "status": "succeeded",
        "response": {
            "ok": True,
            "events": [
                {
                    "event_type": "external.comment.received",
                    "correlation_id": "poll-correlation",
                    "summary": "Inbound poll event",
                    "payload": {"source": "poll"},
                }
            ],
        },
    }
    extension = {"compiled_manifest": {"supported_events": ["external.comment.received"]}}

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.emit_extension_event"
    ) as emit_extension_event:
        process_extension_response_envelope(
            invocation,
            extension,
            "fixture_events",
            scope=None,
            audit={"project_id": project_id},
            bl=MagicMock(),
        )

    emit_extension_event.assert_called_once()
    assert emit_extension_event.call_args.kwargs["project_id"] == project_id


def test_worker_response_event_fallback_preserves_audit_project_scope() -> None:
    project_id = "58eeaeb3-a9c1-4c51-b292-14eb86899d86"
    invocation = {
        "status": "succeeded",
        "response": {
            "ok": True,
            "events": [
                {
                    "event_type": "external.comment.received",
                    "correlation_id": "poll-correlation",
                    "summary": "Inbound poll event",
                    "payload": {"source": "poll"},
                }
            ],
        },
    }
    extension = {"compiled_manifest": {"supported_events": ["external.comment.received"]}}

    with patch(
        "core.infrastructure.extension_runtime.extension_runtime.emit_extension_event"
    ) as emit_extension_event:
        process_worker_extension_response_envelope(
            invocation,
            extension,
            "fixture_events",
            scope=None,
            audit={"project_id": project_id},
            bl=MagicMock(),
        )

    emit_extension_event.assert_called_once()
    assert emit_extension_event.call_args.kwargs["project_id"] == project_id
