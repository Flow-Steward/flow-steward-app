"""Start workflows from a provider event that arrived through an extension.

A provider trigger (``workflow_provider_triggers``) binds one workflow of one
project to an event of one extension, for example "New Telegram message". The
HTTP route for task providers already starts workflows from such a row. An
extension that is a notification provider answers its callback with ``events``,
which :meth:`ingest_provider_events` records; this module is the second half:
the same recorded event also starts the workflows bound to it.

A trigger matches when its provider key and project are the event's, its
``event_name`` equals the event type exactly (an empty ``event_name`` never
matches: a trigger must name its event), and every scalar value of its
``trigger_config`` equals the event payload field of the same name. The payload
fields become the workflow inputs. A failing trigger never fails the event: the
event is already recorded, so each trigger is started on its own and a failure is
logged and returned.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from core.application.workflows.provider_event_delivery import (
    DeliveryBinding,
    RecordedEvent,
    dispatch_recorded_event,
    record_provider_event,
)
from core.application.workflows.workflow_provider_trigger_repository import (
    list_enabled_provider_triggers_for_provider,
)

logger = logging.getLogger(__name__)

_EVENT_KEYS = frozenset({"event", "event_name"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def trigger_matches_event(row: dict[str, Any], *, event_type: str, payload: dict[str, Any]) -> bool:
    """Whether the trigger row is bound to this event type and payload."""
    configured_event = _text(row.get("event_name"))
    if not configured_event or configured_event != _text(event_type):
        return False
    for key, expected in _as_dict(row.get("trigger_config_jsonb")).items():
        if key in _EVENT_KEYS:
            continue
        if isinstance(expected, bool):
            # A checkbox is a condition too: {"approved": true} needs approved true.
            if _boolean_value(payload.get(key)) is not expected:
                return False
            continue
        if not isinstance(expected, (str, int, float)) or _text(expected) == "":
            continue
        if _text(payload.get(key)) != _text(expected):
            return False
    return True


def _boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = _text(value).lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    return None


def match_provider_triggers(
    bl: Any,
    *,
    provider_key: str,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """The enabled triggers of this project bound to this event and payload."""
    try:
        bound = list_enabled_provider_triggers_for_provider(bl, provider_key=provider_key)
    except Exception as exc:
        # The event is recorded already; a missing trigger store must not fail it.
        logger.warning("Provider triggers of %s could not be listed: %s", provider_key, exc)
        return []
    return [
        row
        for row in bound
        if _text(row.get("project_id")) == project_id
        and trigger_matches_event(row, event_type=event_type, payload=payload)
    ]


def provider_event_source_id(source_id: str, payload: dict[str, Any]) -> str:
    """The provider's id for the event, or a digest of its payload."""
    return (
        _text(source_id)
        or hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    )


def start_provider_event_workflows(
    bl: Any,
    *,
    enqueue_task_fn: Any,
    provider_key: str,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    source_id: str,
    binding: DeliveryBinding | None = None,
    recorded: RecordedEvent | None = None,
) -> list[dict[str, Any]]:
    """Start every workflow whose provider trigger is bound to this event.

    The event and one dispatch record per matched trigger are stored first
    (once per ``source_id``), then each record starts its workflow in one job
    transaction. Inside an open transaction (a connection poll) the starts wait
    for its commit. Returns one outcome per started record (``accepted``,
    ``deduplicated``, ``skipped``, ``rejected``, ``failed`` or ``pending``); an
    event no trigger is bound to, or a replay, returns an empty list. Never
    raises for one trigger's failure.
    """
    key = _text(provider_key).lower()
    clean_project = _text(project_id)
    if not key or not clean_project or not _text(event_type) or not callable(enqueue_task_fn):
        return []
    if recorded is not None:
        # The caller recorded the event (inside its poll transaction).
        return dispatch_recorded_event(bl, recorded, enqueue_task_fn=enqueue_task_fn)
    event_payload = _as_dict(payload)
    rows = match_provider_triggers(
        bl,
        provider_key=key,
        project_id=clean_project,
        event_type=_text(event_type),
        payload=event_payload,
    )
    if not rows:
        return []
    try:
        recorded = record_provider_event(
            bl,
            project_id=clean_project,
            provider_key=key,
            source_id=provider_event_source_id(source_id, event_payload),
            event_type=_text(event_type),
            payload=event_payload,
            triggers=rows,
            binding=binding,
        )
    except Exception as exc:
        logger.warning("Provider event of %s could not be recorded: %s", key, exc)
        return [
            {
                "trigger_id": _text(row.get("trigger_id")),
                "workflow_id": _text(row.get("workflow_id")),
                "status": "failed",
                "reason": "workflow_trigger_enqueue_failed",
            }
            for row in rows
        ]
    return dispatch_recorded_event(bl, recorded, enqueue_task_fn=enqueue_task_fn)


__all__ = [
    "match_provider_triggers",
    "provider_event_source_id",
    "start_provider_event_workflows",
    "trigger_matches_event",
]
