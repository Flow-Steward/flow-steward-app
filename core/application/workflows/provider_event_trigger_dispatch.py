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
from contextlib import suppress
from typing import Any

from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.registry.workflow_secret_defaults import workflow_secret_launch_snapshot
from core.application.webhooks.provider_delivery_repository import (
    _claim_provider_webhook_delivery,
    _mark_provider_webhook_delivery_accepted,
    _mark_provider_webhook_delivery_failed,
)
from core.application.workflows.workflow_entrypoint_repository import (
    resolve_project_run_config_for_workflow_project,
)
from core.application.workflows.workflow_provider_trigger_repository import (
    list_enabled_provider_triggers_for_provider,
)
from core.application.workflows.workflow_start_runtime import enqueue_workflow_start
from core.application.workflows.workflow_trigger_guards import provider_trigger_admission

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


def _workflow_inputs(
    *, provider_key: str, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    files = payload.get("files")
    return {
        **payload,
        "provider_key": provider_key,
        "event_name": event_type,
        "provider_event": payload,
        "files": list(files) if isinstance(files, list) else [],
    }


def _start_one(
    bl: Any,
    *,
    enqueue_task_fn: Any,
    row: dict[str, Any],
    provider_key: str,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    source_id: str,
) -> dict[str, Any]:
    workflow_id = _text(row.get("workflow_id"))
    trigger_id = _text(row.get("trigger_id"))
    outcome: dict[str, Any] = {"trigger_id": trigger_id, "workflow_id": workflow_id}
    with provider_trigger_admission(bl, row) as active:
        if not active:
            return {**outcome, "status": "skipped", "reason": "trigger_inactive"}
        _, run_cfg = resolve_project_run_config_for_workflow_project(bl, workflow_id, project_id)
        if not run_cfg:
            return {**outcome, "status": "skipped", "reason": "workflow_project_binding_not_found"}
        inputs = _workflow_inputs(provider_key=provider_key, event_type=event_type, payload=payload)
        validation = WorkflowInterfaceService().validate_inputs_for_workflow_id(
            workflow_id, inputs, channel="webhook"
        )
        if not validation.ok:
            return {
                **outcome,
                "status": "rejected",
                "reason": "workflow_input_validation_failed",
                "errors": list(validation.errors or []),
            }
        if validation.interface.get("enabled"):
            inputs = dict(validation.inputs)
        task_id = f"provider-trigger:{provider_key}:{source_id}"
        delivery_key = "|".join((project_id, workflow_id, task_id))
        claim = _claim_provider_webhook_delivery(
            bl, provider_key=provider_key, delivery_key=delivery_key, request_id=source_id
        )
        if not claim.get("claimed"):
            return {
                **outcome,
                "status": "deduplicated",
                "job_id": _text(claim.get("job_id")),
            }
        try:
            job_id = enqueue_workflow_start(
                enqueue_task_fn=enqueue_task_fn,
                task_id=task_id,
                task_summary=_text(row.get("display_name")) or f"{provider_key} workflow trigger",
                task_source="provider",
                project_id=project_id,
                project_name=_text(run_cfg.get("project_name") or run_cfg.get("name")),
                project_config=dict(run_cfg),
                selected_workflow_id=workflow_id,
                workflow_selection_source="provider_trigger",
                entry_path="provider_webhook",
                input_contract="direct_inputs",
                source_system=provider_key,
                source_event_id=source_id,
                source_object_id=source_id,
                inputs=inputs,
                files=list(inputs.get("files") or []),
                extra_state={
                    "provider_key": provider_key,
                    "provider_trigger_id": trigger_id,
                    "provider_trigger_type_id": _text(row.get("trigger_type_id")),
                },
                workflow_secret_ref_fingerprints=workflow_secret_launch_snapshot(
                    validation.interface, inputs=inputs
                ),
                job_meta={
                    "provider_key": provider_key,
                    "provider_trigger_id": trigger_id,
                    "event_name": event_type,
                    "invocation": {
                        "channel": "provider_webhook",
                        "trigger_family": "provider_trigger",
                        "entry_path": "provider_webhook",
                        "source_system": provider_key,
                        "request_id": source_id,
                        "trigger_id": trigger_id,
                    },
                },
            )
        except Exception as exc:
            with suppress(Exception):
                _mark_provider_webhook_delivery_failed(
                    bl,
                    provider_key=provider_key,
                    delivery_key=delivery_key,
                    request_id=source_id,
                    error_text=f"enqueue_failed: {exc}",
                )
            raise
        with suppress(Exception):
            _mark_provider_webhook_delivery_accepted(
                bl,
                provider_key=provider_key,
                delivery_key=delivery_key,
                request_id=source_id,
                job_id=str(job_id),
            )
        return {**outcome, "status": "accepted", "job_id": str(job_id)}


def start_provider_event_workflows(
    bl: Any,
    *,
    enqueue_task_fn: Any,
    provider_key: str,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    source_id: str,
) -> list[dict[str, Any]]:
    """Start every workflow whose provider trigger is bound to this event.

    Returns one outcome per matched trigger (``accepted``, ``deduplicated``,
    ``skipped``, ``rejected`` or ``failed``); an event no trigger is bound to
    returns an empty list. Never raises for one trigger's failure.
    """
    key = _text(provider_key).lower()
    clean_project = _text(project_id)
    if not key or not clean_project or not _text(event_type) or not callable(enqueue_task_fn):
        return []
    event_payload = _as_dict(payload)
    try:
        bound = list_enabled_provider_triggers_for_provider(bl, provider_key=key)
    except Exception as exc:
        # The event is recorded already; a missing trigger store must not fail it.
        logger.warning("Provider triggers of %s could not be listed: %s", key, exc)
        return []
    rows = [
        row
        for row in bound
        if _text(row.get("project_id")) == clean_project
        and trigger_matches_event(row, event_type=event_type, payload=event_payload)
    ]
    if not rows:
        return []
    clean_source = (
        _text(source_id)
        or hashlib.sha256(
            json.dumps(event_payload, sort_keys=True, default=str).encode()
        ).hexdigest()
    )
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        try:
            outcomes.append(
                _start_one(
                    bl,
                    enqueue_task_fn=enqueue_task_fn,
                    row=row,
                    provider_key=key,
                    project_id=clean_project,
                    event_type=_text(event_type),
                    payload=event_payload,
                    source_id=clean_source,
                )
            )
        except Exception as exc:
            logger.warning(
                "Provider trigger %s did not start its workflow: %s",
                _text(row.get("trigger_id")),
                exc,
            )
            outcomes.append(
                {
                    "trigger_id": _text(row.get("trigger_id")),
                    "workflow_id": _text(row.get("workflow_id")),
                    "status": "failed",
                    "reason": "workflow_trigger_enqueue_failed",
                }
            )
    return outcomes


__all__ = ["start_provider_event_workflows", "trigger_matches_event"]
