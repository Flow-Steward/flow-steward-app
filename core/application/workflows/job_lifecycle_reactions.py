"""Run a workflow's automations for one runtime event of one of its jobs.

The worker knows the runtime moments -- a job finishes, a run parks on an
approval or a Wait, the step hub starts, finishes or fails a step or a real
group -- and hands each one here as a public reaction event
(:mod:`workflow_reaction_events`). The producer's own notification names map to
their public event (``job.awaiting_approval`` -> ``job.approval_waiting``,
``job.awaiting_timer`` -> ``job.timer_waiting``); only the public name is
evaluated. Only notification actions run: the job is terminal, parked or
mid-run, and resuming or cancelling it belongs to its own flows.

A ``job.approval_waiting`` of an invoked child also notifies its waiting
ancestors (:mod:`approval_waiting_propagation`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from core.application.workflows import reaction_runtime
from core.application.workflows.reaction_service_runtime import (
    build_worker_inbound_reaction_evaluator,
)
from core.application.workflows.workflow_reaction_events import (
    EVENT_APPROVAL_WAITING,
    PUBLIC_REACTION_EVENTS,
    public_reaction_event_type,
    reaction_event_id,
)

logger = logging.getLogger(__name__)


def _unavailable(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "status": "ignored",
        "reason": "only notification actions run on job lifecycle events",
        "executed": False,
    }


def build_lifecycle_reaction_evaluator(
    bl: Any, *, to_json_obj: Callable[[Any], Any]
) -> Callable[..., list[dict[str, Any]]]:
    """The reaction evaluator for runtime events: notification actions only."""

    def notify(event: dict[str, Any], action_payload: dict[str, Any]) -> dict[str, Any]:
        return reaction_runtime.dispatch_notification_from_reaction(
            event,
            action_payload,
            get_logger_only=lambda: bl,
            err_database_not_configured="Database is not configured",
            to_json_obj=to_json_obj,
            resolve_job_workflow_id=lambda job: reaction_runtime.resolve_job_workflow_id(
                job, to_json_obj=to_json_obj
            ),
            logger=logger,
        )

    return build_worker_inbound_reaction_evaluator(
        to_json_obj=to_json_obj,
        resume_job_from_reaction=_unavailable,
        cancel_job_from_reaction=_unavailable,
        retry_interrupted_step_from_reaction=_unavailable,
        dispatch_notification_from_reaction=notify,
        logger=logger,
    )


def build_lifecycle_reaction_event(
    *,
    event_type: str,
    project_id: str,
    workflow_id: str | None,
    job_id: Any,
    event_id: str,
    payload: Mapping[str, Any],
    account_id: str | None = None,
) -> dict[str, Any]:
    """One public reaction event with its stable identity and scope fields."""
    public_type = public_reaction_event_type(event_type)
    body = dict(payload)
    identity = reaction_event_id(
        job_id=job_id,
        event_type=public_type,
        payload=body,
        fallback_event_id=event_id,
    )
    event = {
        "event_type": public_type,
        "job_id": job_id,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "account_id": account_id,
        "event_id": identity,
        "correlation_id": identity,
        "source": "workflow_runtime",
        "payload": {**body, "source_event_type": public_type},
    }
    for key in ("step_id", "step_execution_id", "group_id"):
        value = str(body.get(key) or "").strip()
        if value:
            event[key] = value
    return event


def evaluate_lifecycle_event(
    bl: Any,
    event: dict[str, Any],
    *,
    evaluate: Callable[..., list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Evaluate one event; an automation never fails the run it reacts to."""
    try:
        return list(evaluate(bl, event))
    except Exception as exc:
        logger.warning(
            "job lifecycle reactions failed: job_id=%s event=%s error=%s",
            event.get("job_id"),
            event.get("event_type"),
            exc,
        )
        return []


def run_job_lifecycle_reactions(
    bl: Any,
    *,
    event_type: str,
    project_id: str,
    workflow_id: str | None,
    job_id: Any,
    event_id: str,
    payload: Mapping[str, Any],
    to_json_obj: Callable[[Any], Any],
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    """Evaluate the workflow's reactions to one runtime event. Never raises."""
    public_type = public_reaction_event_type(event_type)
    if bl is None or public_type not in PUBLIC_REACTION_EVENTS:
        return []
    evaluate = build_lifecycle_reaction_evaluator(bl, to_json_obj=to_json_obj)
    event = build_lifecycle_reaction_event(
        event_type=public_type,
        project_id=project_id,
        workflow_id=workflow_id,
        job_id=job_id,
        event_id=event_id,
        payload=payload,
        account_id=account_id,
    )
    results = evaluate_lifecycle_event(bl, event, evaluate=evaluate) if workflow_id else []
    if public_type == EVENT_APPROVAL_WAITING and payload.get("propagated") is not True:
        # The child's own reactions ran above; its waiting ancestors get a
        # projection of the same event. A propagated event never bubbles again.
        from core.application.workflows.approval_waiting_propagation import (
            propagate_approval_waiting,
        )

        try:
            results.extend(
                propagate_approval_waiting(
                    bl,
                    event,
                    evaluate=lambda current: evaluate_lifecycle_event(
                        bl, current, evaluate=evaluate
                    ),
                    to_json_obj=to_json_obj,
                )
            )
        except Exception as exc:
            logger.warning(
                "approval propagation to ancestors failed: job_id=%s error=%s", job_id, exc
            )
    return results


__all__ = [
    "build_lifecycle_reaction_evaluator",
    "build_lifecycle_reaction_event",
    "evaluate_lifecycle_event",
    "run_job_lifecycle_reactions",
]
