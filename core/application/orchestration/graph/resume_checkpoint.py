"""Checkpoint identity, repair and approval updates for workflow resume."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from core.application.jobs.workflow_wait_projection import (
    WAIT_RESUME_APPLIED_KEY,
    interrupt_matches_wait,
)


def _workflow_id_from_state_values(values: Any, *, to_json_obj) -> str:
    if not isinstance(values, dict):
        return ""
    task_envelope = to_json_obj(values.get("task_envelope"))
    if not isinstance(task_envelope, dict):
        task_envelope = {}
    metadata = to_json_obj(task_envelope.get("metadata"))
    if not isinstance(metadata, dict):
        metadata = {}
    return str(
        values.get("workflow_id")
        or values.get("selected_workflow")
        or values.get("requested_workflow_id")
        or task_envelope.get("requested_workflow_id")
        or task_envelope.get("workflow_id")
        or metadata.get("requested_workflow_id")
        or metadata.get("workflow_id")
        or ""
    ).strip()


def _resolve_resume_workflow_id_from_checkpoint(
    *,
    thread_id: str,
    checkpointer,
    build_graph,
    to_json_obj,
    logger,
) -> str:
    if not thread_id or not checkpointer:
        return ""
    try:
        reader_graph = build_graph(
            checkpointer=checkpointer,
            validate_contracts=False,
        )
        snapshot = reader_graph.get_state({"configurable": {"thread_id": thread_id}})
        return _workflow_id_from_state_values(
            getattr(snapshot, "values", None),
            to_json_obj=to_json_obj,
        )
    except Exception as exc:
        logger.debug(
            "run_graph_resume: could not resolve workflow from checkpoint for thread %s: %s",
            thread_id,
            exc,
        )
        return ""


def _resume_timer_wait(
    *,
    snapshot: Any,
    values: Mapping[str, Any],
    wait_id: str,
    resume_value: Mapping[str, Any],
    repair_update: Callable[[], Mapping[str, Any] | None],
    run: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """Deliver a timer wake only to the wait it was issued for.

    The checkpoint records which wake it applied, in the same update as the
    wake itself. A redelivery after that continues from the checkpoint and
    never hands the wake to a later pause; a wake for a wait the checkpoint is
    not paused on is not delivered at all, and the caller re-projects the
    pause the checkpoint really names.
    """
    from langgraph.types import Command

    if str(values.get(WAIT_RESUME_APPLIED_KEY) or "") == wait_id:
        return run(None)
    if not interrupt_matches_wait(snapshot, wait_id):
        return dict(values) if isinstance(values, Mapping) else {}
    update = dict(repair_update() or {})
    update[WAIT_RESUME_APPLIED_KEY] = wait_id
    return run(Command(resume=dict(resume_value), update=update))


def _is_state_based_test_run_approval(snapshot: Any) -> bool:
    if snapshot is None:
        return False
    values = getattr(snapshot, "values", {}) or {}
    task_source = str(values.get("task_source") or "").strip().lower()
    if task_source not in {"workflow_test_run", "step_test_run"}:
        return False
    if not bool(values.get("needs_user_approval")):
        return False
    return str(values.get("status") or "").strip().lower() == "awaiting_approval"


def _is_state_based_manual_approval(snapshot: Any) -> bool:
    """Detect persisted approval gates that have no live LangGraph interrupt to resume."""
    if snapshot is None or _snapshot_has_pending_interrupts(snapshot):
        return False
    values = getattr(snapshot, "values", {}) or {}
    if not bool(values.get("needs_user_approval")):
        return False
    return str(values.get("status") or "").strip().lower() == "awaiting_approval"


def _snapshot_has_pending_interrupts(snapshot: Any) -> bool:
    if snapshot is None:
        return False
    tasks = getattr(snapshot, "tasks", None) or ()
    for task in tasks:
        interrupts = getattr(task, "interrupts", None) or ()
        if interrupts:
            return True
    return False


def _checkpoint_contains_resume_marker(values: Any, approval_id: str | None) -> bool:
    """Tell recovery whether this approval was already applied to the checkpoint."""
    marker = str(approval_id or "").strip()
    if not marker or not isinstance(values, dict):
        return False
    return str(values.get("approval_resume_applied_id") or "").strip() == marker


def _build_resume_state_repair_update(
    *,
    state_values: Any,
    bl,
    thread_id: str,
    job_thread_store,
    to_json_obj,
    logger,
) -> dict[str, Any]:
    values = state_values if isinstance(state_values, dict) else {}
    missing_keys = [
        key
        for key in ("task_envelope", "inputs")
        if not isinstance(values.get(key), dict) or not values.get(key)
    ]
    if not missing_keys or not bl:
        return {}
    try:
        row = job_thread_store.get_job_meta_by_thread_id(bl.conn, thread_id)
    except Exception as exc:
        logger.debug("run_graph_resume: could not load job meta for state repair: %s", exc)
        return {}
    if not isinstance(row, dict):
        return {}
    meta = to_json_obj(row.get("meta"))
    if not isinstance(meta, dict):
        return {}
    update: dict[str, Any] = {}
    for key in missing_keys:
        value = meta.get(key)
        if isinstance(value, dict) and value:
            update[key] = dict(value)
    task_envelope = update.get("task_envelope")
    envelope_inputs = task_envelope.get("inputs") if isinstance(task_envelope, dict) else None
    if isinstance(envelope_inputs, dict) and envelope_inputs:
        update.setdefault("inputs", dict(envelope_inputs))
    return update


def _cursor_execution_id(values: Mapping[str, Any]) -> str:
    """The step execution a checkpoint is at; a re-park elsewhere is a new gate."""
    cursor = values.get("run_cursor")
    return str(cursor.get("step_execution_id") or "").strip() if isinstance(cursor, Mapping) else ""


def _workflow_resume_journal_entry(
    *,
    state_values: dict[str, Any] | None,
    approved: bool,
    action: str | None,
    comment: str,
    route: str,
) -> dict[str, Any]:
    from core.application.orchestration.journal import make_journal_entry

    values = state_values if isinstance(state_values, dict) else {}
    try:
        iteration = int(values.get("iteration_count") or 0)
    except (TypeError, ValueError):
        iteration = 0
    step_id = str(values.get("current_step_id") or "").strip()
    effective_action = str(action or ("approve" if approved else "reject")).strip().lower()
    return make_journal_entry(
        iteration=iteration,
        event="approval_resume_decision",
        step_id=step_id,
        action="resume",
        reason=f"operator_{effective_action or 'decision'}",
        message=(f"Operator approval decision accepted; resuming workflow via {route}."),
        detail={
            "approved": bool(approved),
            "approval_action": effective_action,
            "operator_comment": str(comment or "").strip(),
            "resume_route": route,
            "target": "orchestrator",
        },
        severity="info",
    )


def _build_state_based_approval_update(
    *,
    approved: bool,
    action: str | None,
    comment: str,
    resume_value: dict[str, Any],
    state_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_action = str(action or resume_value.get("action") or "").strip().lower()
    effective_approved = bool(approved)
    if effective_action == "override" or (
        not (state_values or {}).get("external_effect_resolution_ref")
        and effective_action in {"approve", "skip"}
    ):
        effective_approved = True
    if effective_action == "abort":
        feedback = str(comment or resume_value.get("comment") or "").strip()
        return {
            "user_approved": False,
            "needs_user_approval": False,
            "approval_context": None,
            "review_feedback": feedback or "Job aborted by user.",
            "status": "cancelled",
            "external_effect_resolution_ref": None,
            "final_report": None,
        }
    feedback = str(comment or resume_value.get("comment") or "").strip()
    if not effective_approved and not feedback:
        feedback = "Please revise and resubmit for approval."
    update = {
        "user_approved": effective_approved,
        "needs_user_approval": False,
        "approval_context": None,
        "review_feedback": feedback or None,
        "status": "in_progress",
        "final_report": None,
    }
    from core.application.orchestration.external_effect_recovery import (
        CARRIER,
        checked_gate_resolution_ref,
    )

    update[CARRIER] = checked_gate_resolution_ref(
        state_values or {}, resume_value.get(CARRIER), approved=effective_approved
    )
    update["orchestrator_journal"] = [
        _workflow_resume_journal_entry(
            state_values=state_values,
            approved=effective_approved,
            action=effective_action,
            comment=feedback,
            route="state_update",
        )
    ]
    if effective_approved:
        approved_feedback = feedback or "Approved."
        update["review_feedback"] = approved_feedback
        update["reviewer_comments"] = approved_feedback
        update["risk_notes"] = feedback if feedback else "Approved without additional risk notes."
        workflow_data = (
            state_values.get("workflow_data") if isinstance(state_values, dict) else None
        )
        merged_workflow_data = dict(workflow_data) if isinstance(workflow_data, dict) else {}
        merged_workflow_data["reviewer_comments"] = update["reviewer_comments"]
        merged_workflow_data["risk_notes"] = update["risk_notes"]
        update["workflow_data"] = merged_workflow_data
    return update


def _rotate_state_approval_id(
    *,
    bl,
    thread_id: str,
    job_thread_store,
    logger,
) -> None:
    """Give a re-parked state-based gate its own identity.

    Each park at an approval is a *distinct* gate. A workflow Test
    Run parks by state, so its gate identity lives on the job row rather than in
    a LangGraph interrupt — and a resume that parks again kept the identity of
    the gate that was already decided.

    First-decision-wins then treated the next decision as a replay of the last
    one. Observed live: Reject was accepted, the run returned into rework and
    re-parked correctly, and the follow-up Approve came back
    `409 approval_already_resolved`, leaving the run permanently undecidable
    (AUDIT-038: approve/reject/override end to end).

    Best effort by design: rotation failing must not undo a resume that
    otherwise succeeded. Only a run that already carries a state-based identity
    is touched, so a gate backed by a real interrupt is never affected.
    """
    try:
        row = job_thread_store.get_job_meta_by_thread_id(bl.conn, thread_id)
        meta = row.get("meta") if isinstance(row, dict) else None
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        if not isinstance(meta, dict):
            return
        if not meta.get("workflow_test_run") or not meta.get("state_approval_id"):
            return
        meta["state_approval_id"] = f"state-approval-{uuid.uuid4().hex}"
        job_thread_store.update_job_meta_by_thread_id(
            bl.conn,
            thread_id=thread_id,
            meta=meta,
            commit=True,
        )
    except Exception as exc:  # pragma: no cover - never fail a completed resume
        logger.warning(
            "run_graph_resume: could not rotate the state approval id for %s: %s",
            thread_id,
            exc,
        )
