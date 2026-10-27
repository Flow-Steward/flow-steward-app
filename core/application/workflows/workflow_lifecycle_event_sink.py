"""Step and group reaction events from the step hub.

The hub decides when a step execution starts, is accepted, fails or is blocked
and when a real group execution starts, completes or fails. It reports each
moment through :func:`publish_step_event` / :func:`publish_group_event`; the
worker binds a sink for the duration of one graph run
(:func:`bind_workflow_event_sink`) that hands the event to the workflow's
reactions. Without a bound sink (a test run, an inline child, a unit test) the
call does nothing. Publishing never raises into the hub.

Identity: a step event is one per step execution and attempt
(``step_execution_id``/``attempt``); a group event one per group execution
(``group_execution_id``). A re-executed hub node publishes the same identity
again and the notification runtime replays instead of delivering twice.

A group event needs a real group and its execution: without a
``group_execution_id`` (given or found in the run cursor) nothing is published.
The moments are the hub's decisions, not every attempt of a group: started at
the logical entry, completed after the accepted success, failed once the hub
decided to leave the group with its failure.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from typing import Any

from core.application.jobs.public_secret_redaction import redact_public_secrets
from core.application.security.credential_redaction import redact_text
from core.application.workflows.workflow_reaction_events import (
    GROUP_EVENTS,
    STEP_EVENTS,
    lifecycle_event_id,
)

logger = logging.getLogger(__name__)

#: ``sink(event_type=..., job_id=..., workflow_id=..., project_id=..., event_id=..., payload=...)``
WorkflowEventSink = Callable[..., Any]

_SINK: ContextVar[WorkflowEventSink | None] = ContextVar("workflow_event_sink", default=None)
_ERROR_TEXT_LIMIT = 500


def _text(value: Any) -> str:
    return str(value or "").strip()


def _public_error_text(value: str) -> str:
    # Redact the full diagnostic before truncation can hide a token signature.
    return redact_public_secrets(redact_text(_text(value)))[:_ERROR_TEXT_LIMIT]


@contextlib.contextmanager
def bind_workflow_event_sink(sink: WorkflowEventSink | None) -> Iterator[None]:
    """Route the hub's step and group events to ``sink`` for one graph run."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def current_workflow_event_sink() -> WorkflowEventSink | None:
    return _SINK.get()


def _run_identity(state: Mapping[str, Any]) -> tuple[str, str]:
    return _text(state.get("job_id")), _text(state.get("workflow_id"))


def _publish(
    state: Mapping[str, Any],
    event_type: str,
    *,
    identity: tuple[Any, ...],
    payload: dict[str, Any],
) -> bool:
    sink = _SINK.get()
    if sink is None:
        return False
    job_id, workflow_id = _run_identity(state)
    if not job_id or not workflow_id:
        return False
    if _text(state.get("task_source")).lower() == "step_test_run":
        return False
    try:
        sink(
            event_type=event_type,
            job_id=job_id,
            workflow_id=workflow_id,
            event_id=lifecycle_event_id(job_id, event_type, *identity),
            payload=payload,
        )
    except Exception as exc:
        logger.warning(
            "workflow event %s for job_id=%s was not published: %s", event_type, job_id, exc
        )
        return False
    return True


def _group_execution_id(state: Mapping[str, Any], group_id: str) -> str:
    """The execution of a Group from the run cursor the hub started from."""
    try:
        from core.application.orchestration.active_step import run_cursor

        frames = run_cursor(state).frames
    except Exception:
        return ""
    for frame in reversed(frames):
        if frame.kind == "group" and frame.container_id == group_id:
            return _text(frame.execution_id)
    return ""


def publish_step_event(
    state: Mapping[str, Any],
    event_type: str,
    *,
    step_id: str,
    step_execution_id: str,
    attempt: int,
    group_id: str | None = None,
    for_each_id: str | None = None,
    group_execution_id: str | None = None,
    error: str | None = None,
    outcome: str | None = None,
) -> bool:
    """Publish ``job.step_started``/``completed``/``failed``/``blocked`` for one execution.

    ``group_id`` and ``for_each_id`` name the step's immediate container (at
    most one; both null at the root).
    """
    if event_type not in STEP_EVENTS:
        raise ValueError(f"'{event_type}' is not a step event")
    if not _text(step_id) or not _text(step_execution_id):
        return False
    group = _text(group_id)
    payload: dict[str, Any] = {
        "step_id": _text(step_id),
        "step_execution_id": _text(step_execution_id),
        "attempt": int(attempt or 1),
        "group_id": group or None,
        "for_each_id": _text(for_each_id) or None,
        "group_execution_id": (
            _text(group_execution_id) or (_group_execution_id(state, group) if group else "")
        )
        or None,
    }
    if outcome:
        payload["outcome"] = _text(outcome)
    if error:
        payload["error"] = _public_error_text(error)
    return _publish(
        state,
        event_type,
        identity=(payload["step_execution_id"], payload["attempt"]),
        payload=payload,
    )


def publish_active_step_event(
    state: Mapping[str, Any],
    event_type: str,
    current: Any,
    *,
    error: str | None = None,
    outcome: str | None = None,
) -> bool:
    """:func:`publish_step_event` for the hub's ``ActiveStep``."""
    frame = getattr(current, "frame", None)
    group_id = getattr(current, "group_id", None)
    group_execution_id = (
        _text(getattr(frame, "group_execution_id", ""))
        if frame is not None and _text(getattr(frame, "group_id", "")) == _text(group_id)
        else None
    )
    return publish_step_event(
        state,
        event_type,
        step_id=_text(getattr(current, "step_id", "")),
        step_execution_id=_text(getattr(current, "step_execution_id", "")),
        attempt=int(getattr(current, "attempt", 1) or 1),
        group_id=group_id,
        for_each_id=getattr(current, "for_each_id", None),
        group_execution_id=group_execution_id,
        error=error,
        outcome=outcome,
    )


def publish_group_event(
    state: Mapping[str, Any],
    event_type: str,
    *,
    group_id: str,
    group_execution_id: str | None = None,
    error: str | None = None,
    error_code: str | None = None,
) -> bool:
    """Publish ``job.group_started``/``completed``/``failed`` for one real group execution."""
    if event_type not in GROUP_EVENTS:
        raise ValueError(f"'{event_type}' is not a group event")
    group = _text(group_id)
    execution = _text(group_execution_id) or (_group_execution_id(state, group) if group else "")
    if not group or not execution:
        return False
    payload: dict[str, Any] = {
        "step_id": None,
        "step_execution_id": None,
        "group_id": group,
        "for_each_id": None,
        "group_execution_id": execution,
    }
    if error:
        payload["error"] = _public_error_text(error)
    if error_code:
        payload["error_code"] = _text(error_code)
    return _publish(state, event_type, identity=(execution,), payload=payload)


__all__ = [
    "WorkflowEventSink",
    "bind_workflow_event_sink",
    "current_workflow_event_sink",
    "publish_active_step_event",
    "publish_group_event",
    "publish_step_event",
]
