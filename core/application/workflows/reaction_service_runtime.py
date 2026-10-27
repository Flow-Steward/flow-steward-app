"""Transport-neutral workflow reaction service composition."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.management.approval_outcome import approval_conflict_outcome_code
from core.application.management.command_models import ApprovalDecisionInput
from core.application.management.errors import ServiceConflictError
from core.application.workflows import reaction_runtime
from core.application.workflows.reaction_scopes import split_scoped_reaction_key

WORKER_INBOUND_REACTION_ACTION_TYPES = frozenset(
    {"approve_job", "reject_job", "resume_job", "resume_orchestrator"}
)


def require_worker_inbound_reaction_contract(
    response: dict[str, Any],
    *,
    bl: Any,
    to_json_obj: Callable[[Any], dict[str, Any]],
) -> None:
    """Reject polling events the worker cannot safely execute before state advances."""
    result = to_json_obj(response.get("result"))
    raw_events = response.get("events")
    if raw_events is None:
        raw_events = result.get("events")
    if not isinstance(raw_events, list):
        if raw_events is not None:
            raise ValueError("worker inbound events must be a list")
        return

    for raw_event in raw_events:
        event = to_json_obj(raw_event)
        event_type = str(event.get("event_type") or "").strip()
        payload = to_json_obj(event.get("payload"))
        job_id = str(event.get("job_id") or payload.get("job_id") or "").strip()
        job = bl.get_job(job_id) if job_id and bl is not None and hasattr(bl, "get_job") else {}
        job_meta = to_json_obj((job or {}).get("meta"))
        workflow_id = str(
            event.get("workflow_id")
            or payload.get("workflow_id")
            or job_meta.get("selected_workflow_id")
            or job_meta.get("requested_workflow_id")
            or job_meta.get("workflow_id")
            or ""
        ).strip()
        if not workflow_id or bl is None or not hasattr(bl, "list_workflow_reactions"):
            continue
        rules = bl.list_workflow_reactions(workflow_id, enabled_only=True) or []
        for raw_rule in rules:
            rule = to_json_obj(raw_rule)
            if str(rule.get("event_type") or "").strip() != event_type:
                continue
            if not reaction_runtime.reaction_scope_matches_event(
                rule,
                event,
                split_scoped_reaction_key_fn=split_scoped_reaction_key,
                to_json_obj=to_json_obj,
            ):
                continue
            if not reaction_runtime.reaction_conditions_match(
                event,
                to_json_obj(rule.get("conditions")),
                to_json_obj=to_json_obj,
            ):
                continue
            action_type = str(to_json_obj(rule.get("action")).get("type") or "").strip().lower()
            if action_type not in WORKER_INBOUND_REACTION_ACTION_TYPES:
                raise ValueError(
                    f"worker inbound reaction action '{action_type or '<missing>'}' "
                    "is not supported"
                )


def resume_job_via_approval_command_service(
    job_id: str,
    comment: str,
    *,
    approved: bool = True,
    action: str | None = None,
    expected_approval_id: str | None = None,
    scope: Any = None,
    idempotency_key: str | None = None,
    get_job_store: Callable[[], Any],
    approval_command_service_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Route a reaction decision through the canonical approval command service."""
    bl = get_job_store()
    job = bl.get_job(str(job_id)) if bl is not None and hasattr(bl, "get_job") else None
    thread_id = str((job or {}).get("thread_id") or "").strip()
    approval_id = str(expected_approval_id or "").strip()
    if not job or not thread_id:
        return {"status": "ignored", "reason": "job not found"}
    if scope is None or not approval_id:
        return {"status": "ignored", "reason": "stale_approval_gate"}

    try:
        accepted = approval_command_service_factory().resume_task(
            thread_id=thread_id,
            scope=scope,
            decision=ApprovalDecisionInput(
                approved=bool(approved),
                action=action,
                comment=str(comment or ""),
                approval_id=approval_id,
            ),
            idempotency_key=str(idempotency_key or "").strip()
            or f"reaction:{job_id}:{approval_id}:{'approve' if approved else 'reject'}",
        )
    except ServiceConflictError as exc:
        code = str((exc.payload or {}).get("code") or "").strip()
        outcome_code = approval_conflict_outcome_code(code)
        if outcome_code:
            return {
                "status": "ignored",
                "reason": code,
                "outcome_code": outcome_code,
            }
        raise

    return {
        "status": "resumed",
        "job_id": str(job_id),
        "thread_id": thread_id,
        "approval_id": approval_id,
        "job_status": str(getattr(accepted, "status", "") or "in_progress"),
    }


def build_worker_inbound_reaction_evaluator(
    *,
    to_json_obj: Callable[[Any], dict[str, Any]],
    resume_job_from_reaction: Callable[..., dict[str, Any]],
    logger: Any,
    cancel_job_from_reaction: Callable[..., dict[str, Any]] | None = None,
    retry_interrupted_step_from_reaction: Callable[..., dict[str, Any]] | None = None,
    dispatch_notification_from_reaction: Callable[..., dict[str, Any]] | None = None,
) -> Callable[..., list[dict[str, Any]]]:
    """Build the reaction evaluator used by worker polling ingress."""

    def unavailable_action(*_args, **_kwargs) -> dict[str, Any]:
        raise ValueError("reaction action is unavailable in worker polling runtime")

    retry = retry_interrupted_step_from_reaction or unavailable_action
    dispatch = dispatch_notification_from_reaction or unavailable_action

    def apply_action(event: dict[str, Any], rule: dict[str, Any], scope: Any = None):
        cancel = cancel_job_from_reaction or unavailable_action
        return reaction_runtime.apply_reaction_action(
            event,
            rule,
            to_json_obj=to_json_obj,
            reaction_action_comment=lambda current_event, action_payload: (
                reaction_runtime.reaction_action_comment(
                    current_event,
                    action_payload,
                    to_json_obj=to_json_obj,
                )
            ),
            reaction_action_result_executed=reaction_runtime.reaction_action_result_executed,
            resume_job_from_reaction=resume_job_from_reaction,
            cancel_job_from_reaction=cancel,
            retry_interrupted_step_from_reaction=retry,
            dispatch_notification_from_reaction=dispatch,
            scope=scope,
        )

    def evaluate(bl: Any, event: dict[str, Any], scope: Any = None) -> list[dict[str, Any]]:
        return reaction_runtime.evaluate_workflow_reactions(
            bl,
            event,
            to_json_obj=to_json_obj,
            reaction_scope_matches_event=lambda rule, current_event: (
                reaction_runtime.reaction_scope_matches_event(
                    rule,
                    current_event,
                    split_scoped_reaction_key_fn=split_scoped_reaction_key,
                    to_json_obj=to_json_obj,
                )
            ),
            reaction_conditions_match=lambda current_event, conditions: (
                reaction_runtime.reaction_conditions_match(
                    current_event,
                    conditions,
                    to_json_obj=to_json_obj,
                )
            ),
            reaction_is_debounced=reaction_runtime.reaction_is_debounced,
            apply_reaction_action=apply_action,
            reaction_action_result_executed=reaction_runtime.reaction_action_result_executed,
            logger=logger,
            scope=scope,
        )

    return evaluate


__all__ = [
    "WORKER_INBOUND_REACTION_ACTION_TYPES",
    "build_worker_inbound_reaction_evaluator",
    "require_worker_inbound_reaction_contract",
    "resume_job_via_approval_command_service",
]
