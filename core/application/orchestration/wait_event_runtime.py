"""A Wait that ends on an event an extension delivers (ADR 0071).

The hub resolves the correlation key once, at dispatch, and records an
``event`` pending wait with the source extension, the accepted event types,
the key and the deadline (``timeout_ms`` from entry). The node pauses with
that wait. The control process wakes it with
``{reason: "event", wait_id, event_type, correlation_key, payload, ...}`` when
a matching delivered event exists, or with ``{reason: "event", wait_id,
timed_out: true}`` once the deadline passed. The node accepts only a wake of
its own wait, a listed type and the exact key; anything else pauses again.

The event is the step's result; ``output_mapping`` copies its fields to state.
A timeout fails the step with ``workflow.wait.event_timed_out`` under its error
policy (fail, continue, route to a step or retry).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.workflow_resume_state import RESUME_REASON_EVENT
from core.application.jobs.workflow_wait_contract import (
    WAIT_KIND_EVENT,
    wait_event_correlation_key,
)
from core.application.orchestration.active_step import ActiveStep

WAIT_CORRELATION_KEY_INVALID = "workflow.wait.correlation_key_invalid"
WAIT_EVENT_TIMED_OUT = "workflow.wait.event_timed_out"

EVENT_WAKE_DELIVERED = "event"
EVENT_WAKE_TIMED_OUT = "timed_out"


def wait_config(step: Mapping[str, Any]) -> Mapping[str, Any]:
    override = step.get("override")
    config = override.get("wait") if isinstance(override, Mapping) else None
    return config if isinstance(config, Mapping) else {}


def is_event_wait(step: Mapping[str, Any]) -> bool:
    return isinstance(wait_config(step).get("event"), Mapping)


def resolve_wait_event(
    step: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """The event a Wait waits for, with its key resolved, or why it cannot wait."""
    from core.application.orchestration.wait_step_runtime import _mapped_value

    config = wait_config(step)
    event = config.get("event")
    if not isinstance(event, Mapping):
        return None, "the Wait has no event"
    raw_key = event.get("correlation_key")
    value, error = _mapped_value(raw_key, state)
    if error:
        return None, f"correlation_key {error}"
    key = wait_event_correlation_key(value)
    if key is None:
        return None, (
            "correlation_key must resolve to a non-empty text or integer of at most "
            f"256 characters; got {value!r}"
        )
    timeout_ms = config.get("timeout_ms")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        return None, f"timeout_ms must be a positive integer; got {timeout_ms!r}"
    return {
        "source": str(event.get("source") or ""),
        "event_types": [str(item) for item in event.get("event_types") or []],
        "correlation_key": key,
        "timeout_ms": timeout_ms,
    }, ""


def event_pending_wait(
    current: ActiveStep,
    *,
    event: Mapping[str, Any],
    revision: int,
    now_ms: int | None = None,
) -> dict[str, Any]:
    from core.application.orchestration.wait_step_runtime import _base_wait, _iso, _now_ms

    started = _now_ms() if now_ms is None else now_ms
    due = started + int(event["timeout_ms"])
    return {
        **_base_wait(current, kind=WAIT_KIND_EVENT, revision=revision, now_ms=started),
        "source": str(event["source"]),
        "event_types": list(event["event_types"]),
        "correlation_key": str(event["correlation_key"]),
        "timeout_ms": int(event["timeout_ms"]),
        "due_at": _iso(due),
        "due_epoch_ms": due,
    }


def event_wake_verdict(
    pending: Mapping[str, Any], value: Any, *, now_ms: int | None = None
) -> str | None:
    """``event`` or ``timed_out`` when the wake ends this wait; ``None`` otherwise."""
    from core.application.orchestration.wait_step_runtime import _now_ms

    if not isinstance(value, Mapping):
        return None
    if str(value.get("reason") or "") != RESUME_REASON_EVENT:
        return None
    if str(value.get("wait_id") or "") != str(pending.get("wait_id") or ""):
        return None
    if value.get("timed_out") is True:
        current_ms = _now_ms() if now_ms is None else now_ms
        due = int(pending.get("due_epoch_ms") or 0)
        return EVENT_WAKE_TIMED_OUT if due and current_ms >= due else None
    if str(value.get("event_type") or "") not in set(pending.get("event_types") or []):
        return None
    if str(value.get("correlation_key") or "") != str(pending.get("correlation_key") or ""):
        return None
    if not isinstance(value.get("payload", {}), Mapping):
        return None
    return EVENT_WAKE_DELIVERED


def _event_artifacts(
    current: ActiveStep, pending: Mapping[str, Any], wake: Mapping[str, Any]
) -> dict[str, Any]:
    # The event leads: the run view's evidence shows the first fields.
    event_type = str(wake.get("event_type") or "")
    correlation_key = str(wake.get("correlation_key") or "")
    received_at = str(wake.get("received_at") or "")
    return {
        "event_type": event_type,
        "received_at": received_at,
        "correlation_key": correlation_key,
        "source": pending.get("source"),
        "payload": dict(wake.get("payload") or {}),
        "step_kind": "wait",
        "step_id": current.step_id,
        "wait_id": pending.get("wait_id"),
        "started_at": pending.get("started_at"),
        "summary_key": "react.jobs.step_wait_event_received",
        "summary_params": {
            "event_type": event_type,
            "correlation_key": correlation_key,
            "received_at": received_at,
        },
        "_outcome": {"name": "success"},
    }


def _message(
    state: Mapping[str, Any],
    pending: Mapping[str, Any],
    *,
    status: str,
    artifacts: dict[str, Any],
    summary: str,
    errors: list[str],
) -> dict[str, Any]:
    from core.application.orchestration.active_step import step_message_fields
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.common import publish_agent_message
    from core.application.orchestration.step_hub_support import HUB_NODE, WAIT_NODE

    timestamp = datetime.now(UTC).isoformat()
    message = build_agent_message(
        from_agent=WAIT_NODE,
        to_agent=HUB_NODE,
        skill="wait",
        status=status,
        correlation_id=uuid.uuid4().hex,
        caused_by=[],
        artifacts=artifacts,
        summary=summary,
        errors=errors,
        started_at=str(pending.get("started_at") or timestamp),
        completed_at=timestamp,
        published_at=timestamp,
        **step_message_fields(state),
    )
    # Persisted like every other step's result, so the run view shows the
    # event (or the timeout) instead of an empty step.
    publish_agent_message(dict(state), message)
    return message


def event_wait_update(
    state: Mapping[str, Any], current: ActiveStep, pending: Mapping[str, Any]
) -> dict[str, Any]:
    """Pause until the event or the deadline; return the step's state update."""
    from langgraph.types import interrupt

    from core.application.orchestration.graph.nodes_contracts import apply_step_output_mapping
    from core.application.orchestration.wait_step_runtime import PENDING_WAIT_KEY

    payload = {**dict(pending), "gate_kind": "wait"}
    while True:
        # Every wake value stays in the checkpoint: a re-executed node replays
        # them in order and pauses again at the same place.
        wake = interrupt(payload)
        verdict = event_wake_verdict(pending, wake)
        if verdict is not None:
            break
    update: dict[str, Any] = {PENDING_WAIT_KEY: None}
    if verdict == EVENT_WAKE_TIMED_OUT:
        error = (
            f"Wait step '{current.step_id}' timed out at {pending.get('due_at')} waiting for "
            f"{', '.join(pending.get('event_types') or [])} "
            f"(key {pending.get('correlation_key')})"
        )
        message = _message(
            state,
            pending,
            status="fail",
            artifacts={
                "step_kind": "wait",
                "step_id": current.step_id,
                "wait_id": pending.get("wait_id"),
                "error_code": WAIT_EVENT_TIMED_OUT,
                "failure_class": "timeout",
                "retryable": True,
                "due_at": pending.get("due_at"),
                "source": pending.get("source"),
                "event_types": list(pending.get("event_types") or []),
                "correlation_key": pending.get("correlation_key"),
                "summary_key": "react.jobs.step_wait_event_timed_out",
                "summary_params": {
                    "event_types": ", ".join(pending.get("event_types") or []),
                    "correlation_key": str(pending.get("correlation_key") or ""),
                    "due_at": str(pending.get("due_at") or ""),
                },
            },
            summary=error,
            errors=[error],
        )
    else:
        artifacts = _event_artifacts(current, pending, wake)
        mapping = wait_config(current.step).get("output_mapping")
        if isinstance(mapping, Mapping):
            apply_step_output_mapping(
                output_mapping=mapping, artifacts=artifacts, state=state, state_updates=update
            )
        message = _message(
            state,
            pending,
            status="pass",
            artifacts=artifacts,
            summary=(
                f"Wait step '{current.step_id}' received {artifacts['event_type']} "
                f"(key {artifacts['correlation_key']}) at {artifacts['received_at']}"
            ),
            errors=[],
        )
    update["agent_messages"] = [*(state.get("agent_messages") or []), message]
    return update


__all__ = [
    "EVENT_WAKE_DELIVERED",
    "EVENT_WAKE_TIMED_OUT",
    "WAIT_CORRELATION_KEY_INVALID",
    "WAIT_EVENT_TIMED_OUT",
    "event_pending_wait",
    "event_wait_update",
    "event_wake_verdict",
    "is_event_wait",
    "resolve_wait_event",
    "wait_config",
]
