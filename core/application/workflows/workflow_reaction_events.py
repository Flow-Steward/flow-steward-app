"""The public workflow reaction events: catalog, identity and payload shape.

A workflow's Automation reacts to these events only. The worker produces a few
runtime moments under its own notification names (``job.awaiting_approval``,
``job.awaiting_timer``); they reach reactions under their public names
(``job.approval_waiting``, ``job.timer_waiting``). Step and group events come
from the step hub through :mod:`workflow_lifecycle_event_sink`.

Every event has one stable identity, so a restart, a redelivered message or a
projector refresh produces the same event again instead of a new one, and the
notification runtime's external-effect claim (keyed by event id, connection and
route) turns the repeat into a replay:

* a wait event is identified by its ``wait_id`` (one event per wait);
* a step event by the step execution and attempt;
* a group event by the group execution;
* a terminal event by the job's terminal notification identity.

A recovery or autonomy approval records no durable wait. Its wait identity is
derived from the hub's recovery request id, which the checkpoint keeps in
``recovery_pending`` for the whole pause (``recovery:<request_id>``); without a
request id it falls back to the approval id (``approval:<approval_id>``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EVENT_JOB_COMPLETED = "job.completed"
EVENT_JOB_FAILED = "job.failed"
EVENT_STEP_STARTED = "job.step_started"
EVENT_STEP_COMPLETED = "job.step_completed"
EVENT_STEP_FAILED = "job.step_failed"
EVENT_STEP_BLOCKED = "job.step_blocked"
EVENT_GROUP_STARTED = "job.group_started"
EVENT_GROUP_COMPLETED = "job.group_completed"
EVENT_GROUP_FAILED = "job.group_failed"
EVENT_APPROVAL_WAITING = "job.approval_waiting"
EVENT_TIMER_WAITING = "job.timer_waiting"

#: The worker's notification names for a parked run.
PRODUCER_AWAITING_APPROVAL = "job.awaiting_approval"
PRODUCER_AWAITING_TIMER = "job.awaiting_timer"

STEP_EVENTS = (EVENT_STEP_STARTED, EVENT_STEP_COMPLETED, EVENT_STEP_FAILED, EVENT_STEP_BLOCKED)
GROUP_EVENTS = (EVENT_GROUP_STARTED, EVENT_GROUP_COMPLETED, EVENT_GROUP_FAILED)
WAIT_EVENTS = (EVENT_APPROVAL_WAITING, EVENT_TIMER_WAITING)
TERMINAL_EVENTS = (EVENT_JOB_COMPLETED, EVENT_JOB_FAILED)

PUBLIC_REACTION_EVENTS = frozenset((*TERMINAL_EVENTS, *STEP_EVENTS, *GROUP_EVENTS, *WAIT_EVENTS))

#: The producer notification name -> the public reaction event of the same moment.
PRODUCER_TO_PUBLIC_EVENT = {
    PRODUCER_AWAITING_APPROVAL: EVENT_APPROVAL_WAITING,
    PRODUCER_AWAITING_TIMER: EVENT_TIMER_WAITING,
}

#: Retired reaction events: refused everywhere, named with their replacement.
REMOVED_REACTION_EVENTS = {
    "job.phase_started": EVENT_GROUP_STARTED,
    "job.phase_completed": EVENT_GROUP_COMPLETED,
    "job.phase_failed": EVENT_GROUP_FAILED,
    "job.gate_waiting": EVENT_APPROVAL_WAITING,
    PRODUCER_AWAITING_APPROVAL: EVENT_APPROVAL_WAITING,
    PRODUCER_AWAITING_TIMER: EVENT_TIMER_WAITING,
}

REACTION_SCOPE_WORKFLOW = "workflow"
REACTION_SCOPE_GROUP = "group"
REACTION_SCOPE_STEP = "step"

#: The locale key prefix of every built-in event label.
_DISPLAY_NAME_KEY_PREFIX = "registry.workflow_detail.automation.event."

#: Scope -> event -> (English label, locale key suffix). The same event reads
#: differently per scope ("A step starts" vs "This step starts"), so each scope
#: names its own key.
_DISPLAY_NAMES: dict[str, dict[str, tuple[str, str]]] = {
    REACTION_SCOPE_WORKFLOW: {
        EVENT_JOB_COMPLETED: ("A job completes", "job_completed"),
        EVENT_JOB_FAILED: ("A job fails", "job_failed"),
        EVENT_STEP_STARTED: ("A step starts", "workflow_step_started"),
        EVENT_STEP_COMPLETED: ("A step completes", "workflow_step_completed"),
        EVENT_STEP_FAILED: ("A step fails", "workflow_step_failed"),
        EVENT_STEP_BLOCKED: ("A step is blocked", "workflow_step_blocked"),
        EVENT_GROUP_STARTED: ("A group starts", "group_started"),
        EVENT_GROUP_COMPLETED: ("A group completes", "group_completed"),
        EVENT_GROUP_FAILED: ("A group fails", "group_failed"),
        EVENT_APPROVAL_WAITING: ("An approval is waiting", "approval_waiting"),
        EVENT_TIMER_WAITING: ("A wait timer starts", "timer_waiting"),
    },
    REACTION_SCOPE_GROUP: {
        EVENT_GROUP_STARTED: ("This group starts", "group_started_local"),
        EVENT_GROUP_COMPLETED: ("This group completes", "group_completed_local"),
        EVENT_GROUP_FAILED: ("This group fails", "group_failed_local"),
        EVENT_STEP_STARTED: ("A step of this group starts", "group_step_started"),
        EVENT_STEP_COMPLETED: ("A step of this group completes", "group_step_completed"),
        EVENT_STEP_FAILED: ("A step of this group fails", "group_step_failed"),
        EVENT_STEP_BLOCKED: ("A step of this group is blocked", "group_step_blocked"),
        EVENT_APPROVAL_WAITING: ("An approval in this group is waiting", "group_approval_waiting"),
        EVENT_TIMER_WAITING: ("A wait timer in this group starts", "group_timer_waiting"),
    },
    REACTION_SCOPE_STEP: {
        EVENT_STEP_STARTED: ("This step starts", "step_started"),
        EVENT_STEP_COMPLETED: ("This step completes", "step_completed"),
        EVENT_STEP_FAILED: ("This step fails", "step_failed"),
        EVENT_STEP_BLOCKED: ("This step is blocked", "step_blocked"),
        EVENT_APPROVAL_WAITING: ("This step waits for approval", "step_approval_waiting"),
        EVENT_TIMER_WAITING: ("This step's wait timer starts", "step_timer_waiting"),
    },
}

#: Approval wait reasons in the ``job.approval_waiting`` payload.
APPROVAL_REASON_AUTHORED = "authored"
APPROVAL_REASON_RECOVERY = "recovery"
APPROVAL_REASON_AUTONOMY = "autonomy"

_GATE_KIND_REASONS = {
    "approval": APPROVAL_REASON_AUTHORED,
    "autonomy_gate": APPROVAL_REASON_AUTONOMY,
    "recovery": APPROVAL_REASON_RECOVERY,
}

#: Scope fields every step, group and wait event carries in its payload.
SCOPE_FIELDS = (
    "step_id",
    "step_execution_id",
    "attempt",
    "group_id",
    "for_each_id",
    "group_execution_id",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def builtin_reaction_events(scope_kind: str) -> list[dict[str, str]]:
    """The workflow runtime's events for one automation scope, each with its label key."""
    names = _DISPLAY_NAMES.get(scope_kind)
    if names is None:
        raise ValueError(f"Unsupported automation scope '{scope_kind}'")
    return [
        {
            "event_type": event,
            "display_name": label,
            "display_name_key": f"{_DISPLAY_NAME_KEY_PREFIX}{key}",
        }
        for event, (label, key) in names.items()
    ]


def removed_reaction_event_message(event_type: str) -> str | None:
    """The refusal for a retired event name, or ``None`` for any other name."""
    replacement = REMOVED_REACTION_EVENTS.get(_text(event_type))
    if replacement is None:
        return None
    return f"Event '{_text(event_type)}' is no longer supported; use '{replacement}'"


def public_reaction_event_type(event_type: str) -> str:
    """The public reaction event for a producer notification name."""
    normalized = _text(event_type)
    return PRODUCER_TO_PUBLIC_EVENT.get(normalized, normalized)


def approval_reason(gate_kind: Any) -> str:
    """``authored``/``recovery``/``autonomy`` for an approval gate kind."""
    return _GATE_KIND_REASONS.get(_text(gate_kind).lower(), APPROVAL_REASON_RECOVERY)


def recovery_wait_id(*, request_id: Any = None, approval_id: Any = None) -> str:
    """The stable wait identity of a recovery or autonomy approval."""
    request = _text(request_id)
    if request:
        return f"recovery:{request}"
    approval = _text(approval_id)
    return f"approval:{approval}" if approval else ""


def lifecycle_event_id(job_id: Any, event_type: str, *identity: Any) -> str:
    """The stable id of one runtime event of one job."""
    parts = [_text(part) for part in identity]
    return ":".join(["wf-event", _text(job_id), _text(event_type), *parts])


def reaction_event_id(
    *,
    job_id: Any,
    event_type: str,
    payload: Mapping[str, Any],
    fallback_event_id: str,
) -> str:
    """The reaction event id: wait events by ``wait_id``, the rest by producer id."""
    public_type = public_reaction_event_type(event_type)
    wait_id = _text(payload.get("wait_id"))
    if public_type in WAIT_EVENTS and wait_id:
        suffix = ("propagated",) if payload.get("propagated") is True else ()
        return lifecycle_event_id(job_id, public_type, wait_id, *suffix)
    return f"{_text(fallback_event_id)}:{public_type}"


def rule_scoped_event_id(event_id: Any, reaction_key: Any) -> str:
    """One reaction's notification identity for one event.

    Two rules that notify the same route are two explicit subscriptions and
    each delivers; one rule never delivers twice for one event and route.
    """
    base = _text(event_id)
    key = _text(reaction_key)
    if not base or not key:
        return base
    return f"{base}:rule:{key}"


def wait_event_payload(
    wait: Mapping[str, Any] | None,
    *,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The scope and wait identity of a ``job.approval_waiting``/``job.timer_waiting``.

    ``wait`` is the recorded wait (``jobs.meta.workflow_wait``); ``approval`` is
    the bounded approval notification payload. A recovery or autonomy approval
    has no recorded wait: its identity comes from the approval payload.
    """
    recorded = dict(wait or {})
    approval_payload = dict(approval or {})
    payload: dict[str, Any] = {}
    for key in ("step_id", "step_execution_id", "group_id"):
        value = _text(recorded.get(key)) or _text(approval_payload.get(key))
        payload[key] = value or None
    attempt = recorded.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        payload["attempt"] = attempt
    wait_id = _text(recorded.get("wait_id")) or _text(approval_payload.get("wait_id"))
    if wait_id:
        payload["wait_id"] = wait_id
    if _text(recorded.get("kind")) == "timer" or recorded.get("due_at"):
        if _text(recorded.get("due_at")):
            payload["due_at"] = _text(recorded.get("due_at"))
        if recorded.get("duration_ms") is not None:
            payload["duration_ms"] = recorded.get("duration_ms")
    approval_id = _text(approval_payload.get("approval_id")) or _text(recorded.get("approval_id"))
    if approval_id:
        payload["approval_id"] = approval_id
    if approval_payload.get("reason"):
        payload["reason"] = _text(approval_payload.get("reason"))
    for label in ("group_label", "for_each_label", "step_label"):
        if _text(approval_payload.get(label)):
            payload[label] = _text(approval_payload.get(label))
    return payload


__all__ = [
    "APPROVAL_REASON_AUTHORED",
    "APPROVAL_REASON_AUTONOMY",
    "APPROVAL_REASON_RECOVERY",
    "EVENT_APPROVAL_WAITING",
    "EVENT_GROUP_COMPLETED",
    "EVENT_GROUP_FAILED",
    "EVENT_GROUP_STARTED",
    "EVENT_JOB_COMPLETED",
    "EVENT_JOB_FAILED",
    "EVENT_STEP_BLOCKED",
    "EVENT_STEP_COMPLETED",
    "EVENT_STEP_FAILED",
    "EVENT_STEP_STARTED",
    "EVENT_TIMER_WAITING",
    "GROUP_EVENTS",
    "PRODUCER_AWAITING_APPROVAL",
    "PRODUCER_AWAITING_TIMER",
    "PRODUCER_TO_PUBLIC_EVENT",
    "PUBLIC_REACTION_EVENTS",
    "REACTION_SCOPE_GROUP",
    "REACTION_SCOPE_STEP",
    "REACTION_SCOPE_WORKFLOW",
    "REMOVED_REACTION_EVENTS",
    "SCOPE_FIELDS",
    "STEP_EVENTS",
    "TERMINAL_EVENTS",
    "WAIT_EVENTS",
    "approval_reason",
    "builtin_reaction_events",
    "lifecycle_event_id",
    "public_reaction_event_type",
    "reaction_event_id",
    "recovery_wait_id",
    "removed_reaction_event_message",
    "rule_scoped_event_id",
    "wait_event_payload",
]
