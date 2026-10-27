"""Authored Wait: one deadline per logical entry, a durable pause, one wake.

The hub fixes the wait at dispatch, the checkpoint boundary before the node:
it resolves ``duration_ms`` once (literal or ``input_mapping.duration_ms``),
checks it, and stores ``pending_wait`` with a fresh ``wait_id`` and the due
time. A re-executed node (restart, resume) reads that record and never moves
the deadline. The node pauses with a LangGraph interrupt; the internal timer
dispatcher resumes it with ``{reason: "timer", wait_id, timer_elapsed: true}``
once the deadline has passed. A zero wait completes without pausing.

An authored Approval records its ``pending_wait`` the same way (kind
``approval``) so every durable pause has one shape in the checkpoint.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.workflow_resume_state import RESUME_REASON_TIMER
from core.application.jobs.workflow_wait_projection import (
    PENDING_WAIT_KEY,
    WAIT_KIND_APPROVAL,
    WAIT_KIND_CHILD,
    WAIT_KIND_TIMER,
    pending_wait_for,
)
from core.application.orchestration.active_step import ActiveStep
from core.application.orchestration.approval_payload import approval_id_for_execution

WAIT_DURATION_INVALID = "workflow.wait.duration_invalid"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat()


def _wait_config(step: Mapping[str, Any]) -> Mapping[str, Any]:
    override = step.get("override")
    config = override.get("wait") if isinstance(override, Mapping) else None
    return config if isinstance(config, Mapping) else {}


def _mapped_value(raw: Any, state: Mapping[str, Any]) -> tuple[Any, str]:
    """Resolve one input mapping value in the existing dialect."""
    from core.application.orchestration.state_paths import (
        resolve_state_path_with_workflow_data,
    )
    from core.application.orchestration.step_templating import STATE_PREFIX, _render_value

    if isinstance(raw, str) and raw.strip().startswith(STATE_PREFIX):
        token = raw.strip()
        value, found = resolve_state_path_with_workflow_data(
            dict(state), token[len(STATE_PREFIX) :]
        )
        return (value, "") if found else (None, f"'{token}' does not resolve")
    return _render_value(raw, dict(state)), ""


def resolve_wait_duration(
    step: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[int | None, str]:
    """The wait's duration in ms, or the reason it is not a valid duration."""
    from core.application.workflows.workflow_definition_validation import (
        wait_duration_issues,
    )

    config = _wait_config(step)
    mapping = config.get("input_mapping")
    if isinstance(mapping, Mapping) and "duration_ms" in mapping:
        value, error = _mapped_value(mapping["duration_ms"], state)
        if error:
            return None, f"Wait duration_ms {error}"
    else:
        value = config.get("duration_ms")
    issues = wait_duration_issues(value, "duration_ms")
    if issues:
        return None, f"{issues[0]['message']}; got {value!r}"
    return int(value), ""


def _base_wait(current: ActiveStep, *, kind: str, revision: int, now_ms: int) -> dict[str, Any]:
    return {
        "wait_id": uuid.uuid4().hex,
        "kind": kind,
        "step_id": current.step_id,
        "step_execution_id": current.step_execution_id,
        "attempt": current.attempt,
        "group_id": current.group_id,
        "for_each_id": current.for_each_id,
        "workflow_revision": revision,
        "started_at": _iso(now_ms),
    }


def timer_pending_wait(
    current: ActiveStep, *, duration_ms: int, revision: int, now_ms: int | None = None
) -> dict[str, Any]:
    started = _now_ms() if now_ms is None else now_ms
    due = started + duration_ms
    return {
        **_base_wait(current, kind=WAIT_KIND_TIMER, revision=revision, now_ms=started),
        "duration_ms": duration_ms,
        "due_at": _iso(due),
        "due_epoch_ms": due,
    }


def approval_pending_wait(
    current: ActiveStep, *, approval_id: str, revision: int, now_ms: int | None = None
) -> dict[str, Any]:
    started = _now_ms() if now_ms is None else now_ms
    return {
        **_base_wait(current, kind=WAIT_KIND_APPROVAL, revision=revision, now_ms=started),
        "approval_id": approval_id,
    }


def child_pending_wait(
    current: ActiveStep, *, revision: int, now_ms: int | None = None
) -> dict[str, Any]:
    """A child wait; the node adds the child job once it exists."""
    started = _now_ms() if now_ms is None else now_ms
    return _base_wait(current, kind=WAIT_KIND_CHILD, revision=revision, now_ms=started)


def timer_wake_is_valid(
    pending: Mapping[str, Any], value: Any, *, now_ms: int | None = None
) -> bool:
    """A wake resumes the wait only for its own wait id and only once it is due."""
    if not isinstance(value, Mapping):
        return False
    if str(value.get("reason") or "") != RESUME_REASON_TIMER:
        return False
    if str(value.get("wait_id") or "") != str(pending.get("wait_id") or ""):
        return False
    if value.get("timer_elapsed") is not True:
        return False
    current_ms = _now_ms() if now_ms is None else now_ms
    return current_ms >= int(pending.get("due_epoch_ms") or 0)


def _result_message(
    state: Mapping[str, Any], current: ActiveStep, pending: Mapping[str, Any]
) -> dict[str, Any]:
    from core.application.orchestration.active_step import step_message_fields
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.step_hub_support import HUB_NODE, WAIT_NODE

    timestamp = datetime.now(UTC).isoformat()
    return build_agent_message(
        from_agent=WAIT_NODE,
        to_agent=HUB_NODE,
        skill="wait",
        status="pass",
        correlation_id=uuid.uuid4().hex,
        caused_by=[],
        # Execution metadata only: a Wait changes no business state.
        artifacts={
            "step_kind": "wait",
            "step_id": current.step_id,
            "wait_id": pending.get("wait_id"),
            "duration_ms": pending.get("duration_ms"),
            "started_at": pending.get("started_at"),
            "due_at": pending.get("due_at"),
            "_outcome": {"name": "success"},
        },
        summary=f"Wait step '{current.step_id}' finished",
        errors=[],
        started_at=str(pending.get("started_at") or timestamp),
        completed_at=timestamp,
        published_at=timestamp,
        **step_message_fields(state),
    )


def wait_step_node(state: Mapping[str, Any]) -> dict[str, Any]:
    """Pause until the recorded deadline; resume only on a valid timer wake."""
    from langgraph.types import interrupt

    from core.application.orchestration.active_step import active_step

    current = active_step(state)
    if current is None:
        return {}
    pending = pending_wait_for(state, current)
    if pending is None or pending.get("kind") != WAIT_KIND_TIMER:
        # The hub records the wait before dispatch; without it there is no
        # deadline to honour, and the step returns no result (a contract failure).
        return {}
    if int(pending.get("duration_ms") or 0) > 0:
        payload = {**pending, "gate_kind": "wait"}
        # A stale or early wake does not advance the run: pause again. Every
        # wake value is kept by the checkpoint, so a re-executed node replays
        # them in order and stops at the same place.
        while True:
            wake = interrupt(payload)
            if timer_wake_is_valid(pending, wake):
                break
    message = _result_message(state, current, pending)
    return {
        "agent_messages": [*(state.get("agent_messages") or []), message],
        PENDING_WAIT_KEY: None,
    }


__all__ = [
    "PENDING_WAIT_KEY",
    "RESUME_REASON_TIMER",
    "WAIT_DURATION_INVALID",
    "WAIT_KIND_APPROVAL",
    "WAIT_KIND_CHILD",
    "WAIT_KIND_TIMER",
    "approval_id_for_execution",
    "approval_pending_wait",
    "child_pending_wait",
    "pending_wait_for",
    "resolve_wait_duration",
    "timer_pending_wait",
    "timer_wake_is_valid",
    "wait_step_node",
]
