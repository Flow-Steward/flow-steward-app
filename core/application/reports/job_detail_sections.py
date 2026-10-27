from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.observability_vocabulary import job_status_meta

from .ui_projection_extras import _build_diagnostics, _build_previous_decisions
from .ui_projection_helpers import (
    _STEP_STATUS_META,
    _as_datetime,
    _build_cost_breakdown,
    _build_message_steps,
    _build_retry_step,
    _build_review_workspace,
    _build_runtime_steps,
    _humanize_identifier,
    _map_step_status,
    _normalize_dict,
    _normalize_role,
    _normalize_rows,
    _select_step,
    _selected_step_payload,
    _step_duration_ms,
    _step_row_title,
)


def _execution_step_timestamp(step: dict[str, Any]) -> float:
    timestamp = _as_datetime(
        step.get("started_at") or step.get("timestamp") or step.get("finished_at")
    )
    return timestamp.timestamp() if timestamp is not None else 0.0


#: Graph stages the platform runs around every workflow. They are not steps a
#: user declared, so Job Details leaves them out while they succeed; a failed
#: or interrupted one stays, because it is then why the run stopped.
_ENGINE_STEP_IDS = frozenset(
    {
        "task_intake",
        "kb_intake",
        "workflow_routing",
        "orchestrator",
        "orchestrator_validation",
        "parallel_executor",
        "operation_retry_wait",
    }
)
_ENGINE_STEP_STATUSES_SHOWN = frozenset({"failed", "interrupted"})


def _build_workflow_run_steps(
    workflow_run: dict[str, Any],
    *,
    seen_scope_ids: set[str],
    job_terminal: bool = False,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for index, row in enumerate(_normalize_rows(workflow_run.get("steps"))):
        step_id = str(row.get("step_id") or "").strip()
        step_execution_id = str(row.get("step_execution_id") or "").strip()
        group_id = str(row.get("group_id") or "").strip()
        occurrence = max(1, int(row.get("occurrence") or 1))
        scope_id = step_id or f"workflow-run:{index}"
        if occurrence > 1:
            scope_id = f"{scope_id}:{occurrence}"
        if scope_id in seen_scope_ids:
            continue
        status = _map_step_status(row.get("status"))
        if job_terminal and status == "in_progress":
            status = "interrupted"
        if step_id in _ENGINE_STEP_IDS and status not in _ENGINE_STEP_STATUSES_SHOWN:
            continue
        status_meta = _STEP_STATUS_META.get(status, _STEP_STATUS_META["pending"])
        display_name = str(row.get("display_name") or "").strip()
        step_label = _humanize_identifier(display_name or step_id) or f"Step {index + 1}"
        group_label = _humanize_identifier(group_id)
        started_at = str(row.get("started_at") or "").strip()
        finished_at = str(row.get("finished_at") or "").strip()
        projected.append(
            {
                "id": f"workflow-run-{scope_id}",
                "scope_id": scope_id,
                "step_id": step_id,
                "step_execution_id": step_execution_id or None,
                "group_id": group_id or None,
                "for_each_id": str(row.get("for_each_id") or "").strip() or None,
                "occurrence": occurrence,
                "group_label": group_label,
                "agent": "",
                "agent_label": "",
                "skill": "",
                "skill_label": step_label,
                "operation_id": None,
                "operation_label": None,
                "title": _step_row_title(name=step_label, group_label=group_label),
                "status": status,
                "status_label": status_meta["label"],
                "tone": status_meta["tone"],
                "summary": "",
                "reason": "",
                "result": "",
                "next": "",
                "timestamp": finished_at or started_at,
                "message_id": None,
                "evidence": [],
                "source": "workflow_run",
                "selected": False,
                "can_restart_from_here": False,
                "inputs": {},
                "outputs": {},
                "artifacts": {},
                "input_artifacts": [],
                "output_artifacts": [],
                "error": {},
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": _step_duration_ms(
                    started_at=started_at,
                    finished_at=finished_at,
                    explicit_ms=row.get("duration_ms"),
                ),
                "provider_processing_time": None,
                "provider_processing_time_unit": None,
                "model": "",
                "attempt": occurrence,
                # The For each item this execution ran for, and its lane.
                "iteration": row.get("iteration")
                if isinstance(row.get("iteration"), int)
                else None,
                "lane_id": str(row.get("lane_id") or "").strip() or None,
            }
        )
    return projected


def build_execution_steps(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    thread_state: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    workflow_run: dict[str, Any] | None = None,
    resolve_artifact: Callable[[str], Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    normalized_job = _normalize_dict(job)
    normalized_runtime = _normalize_dict(runtime)
    normalized_thread_state = _normalize_dict(thread_state)
    normalized_messages = _normalize_rows(messages)

    message_steps = _build_message_steps(
        normalized_messages,
        project_id=str(normalized_job.get("project_id") or "").strip(),
        job_id=str(normalized_job.get("id") or "").strip(),
        resolve_artifact=resolve_artifact,
    )
    workflow_run_steps = _build_workflow_run_steps(
        _normalize_dict(workflow_run),
        seen_scope_ids=set(),
        job_terminal=bool(job_status_meta(normalized_job.get("status"))["terminal"]),
    )
    workflow_steps_by_scope = {
        str(step.get("scope_id") or ""): step
        for step in workflow_run_steps
        if str(step.get("scope_id") or "").strip()
    }
    workflow_steps_by_base_scope: dict[str, list[dict[str, Any]]] = {}
    for step in workflow_run_steps:
        scope_id = str(step.get("scope_id") or "").strip()
        attempt = max(1, int(step.get("attempt") or 1))
        occurrence_suffix = f":{attempt}"
        base_scope_id = (
            scope_id[: -len(occurrence_suffix)]
            if attempt > 1 and scope_id.endswith(occurrence_suffix)
            else scope_id
        )
        if base_scope_id:
            workflow_steps_by_base_scope.setdefault(base_scope_id, []).append(step)
    matched_workflow_scopes: set[str] = set()

    def _merge_durable_timing(message_step: dict[str, Any], durable_step: dict[str, Any]) -> None:
        # Keep the richer message payload while retaining the durable execution's
        # identity and container ownership, including an explicit root owner.
        for field in ("group_id", "for_each_id", "occurrence", "iteration", "lane_id"):
            message_step[field] = durable_step.get(field)
        if durable_step.get("step_execution_id"):
            message_step["step_execution_id"] = durable_step["step_execution_id"]
        for field in ("status", "status_label", "tone"):
            message_step[field] = durable_step[field]
        for field in ("started_at", "finished_at", "duration_ms"):
            value = durable_step.get(field)
            if value not in (None, ""):
                message_step[field] = value
        message_step["timestamp"] = str(
            durable_step.get("finished_at")
            or durable_step.get("started_at")
            or message_step.get("timestamp")
            or ""
        )

    def _matching_durable_step(
        message_step: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        available = [
            candidate
            for candidate in candidates
            if str(candidate.get("scope_id") or "").strip() not in matched_workflow_scopes
        ]
        if not available:
            return None
        if len(available) == 1:
            return available[0]

        message_started = _as_datetime(message_step.get("started_at"))
        message_finished = _as_datetime(
            message_step.get("finished_at") or message_step.get("timestamp")
        )
        message_status = str(message_step.get("status") or "").strip()

        def _score(candidate: dict[str, Any]) -> tuple[int, int, float, int]:
            candidate_started = _as_datetime(candidate.get("started_at"))
            candidate_finished = _as_datetime(candidate.get("finished_at"))
            status_mismatch = int(
                bool(message_status)
                and str(candidate.get("status") or "").strip() != message_status
            )
            interval_mismatch = 1
            if (
                message_started is not None
                and message_finished is not None
                and candidate_started is not None
                and candidate_finished is not None
            ):
                interval_mismatch = int(
                    message_started < candidate_started or message_finished > candidate_finished
                )
            comparison_point = candidate_finished or candidate_started
            distance = (
                abs((message_finished - comparison_point).total_seconds())
                if message_finished is not None and comparison_point is not None
                else float("inf")
            )
            return (
                status_mismatch,
                interval_mismatch,
                distance,
                max(1, int(candidate.get("attempt") or 1)),
            )

        return min(available, key=_score)

    for step in message_steps:
        scope_id = str(step.get("scope_id") or "").strip()
        candidates = workflow_steps_by_base_scope.get(scope_id, [])
        if not candidates:
            exact_match = workflow_steps_by_scope.get(scope_id)
            candidates = [exact_match] if exact_match is not None else []
        durable_step = _matching_durable_step(step, candidates)
        durable_scope_id = str((durable_step or {}).get("scope_id") or "").strip()
        if durable_step is None or not durable_scope_id:
            continue
        step["scope_id"] = durable_scope_id
        matched_workflow_scopes.add(durable_scope_id)
        _merge_durable_timing(step, durable_step)

    # A message whose step row was not matched by scope still names its step
    # execution; one message and one durable row of the same execution are the
    # same step.
    unmatched_messages_by_execution: dict[str, list[dict[str, Any]]] = {}
    for step in message_steps:
        if str(step.get("scope_id") or "").strip() in matched_workflow_scopes:
            continue
        step_execution_id = str(step.get("step_execution_id") or "").strip()
        if step_execution_id:
            unmatched_messages_by_execution.setdefault(step_execution_id, []).append(step)
    unmatched_workflow_by_execution: dict[str, list[dict[str, Any]]] = {}
    for step in workflow_run_steps:
        scope_id = str(step.get("scope_id") or "").strip()
        if scope_id in matched_workflow_scopes:
            continue
        step_execution_id = str(step.get("step_execution_id") or "").strip()
        if step_execution_id:
            unmatched_workflow_by_execution.setdefault(step_execution_id, []).append(step)
    for step_execution_id, execution_messages in unmatched_messages_by_execution.items():
        execution_workflow_steps = unmatched_workflow_by_execution.get(step_execution_id, [])
        if len(execution_messages) != 1 or len(execution_workflow_steps) != 1:
            continue
        message_step = execution_messages[0]
        durable_step = execution_workflow_steps[0]
        durable_scope_id = str(durable_step.get("scope_id") or "").strip()
        if not durable_scope_id:
            continue
        message_step["scope_id"] = durable_scope_id
        matched_workflow_scopes.add(durable_scope_id)
        _merge_durable_timing(message_step, durable_step)
    workflow_run_steps = [
        step
        for step in workflow_run_steps
        if str(step.get("scope_id") or "").strip() not in matched_workflow_scopes
    ]
    seen_scope_ids = {
        str(step.get("scope_id") or "")
        for step in message_steps
        if str(step.get("scope_id") or "").strip()
    }
    seen_scope_ids.update(
        str(step.get("scope_id") or "")
        for step in workflow_run_steps
        if str(step.get("scope_id") or "").strip()
    )
    runtime_steps = _build_runtime_steps(
        job=normalized_job,
        runtime=normalized_runtime,
        thread_state=normalized_thread_state,
        seen_scope_ids=seen_scope_ids,
    )
    seen_scope_ids.update(
        str(step.get("scope_id") or "")
        for step in runtime_steps
        if str(step.get("scope_id") or "").strip()
    )
    retry_steps = _build_retry_step(runtime=normalized_runtime, seen_scope_ids=seen_scope_ids)
    execution_steps = message_steps + workflow_run_steps + runtime_steps + retry_steps
    execution_steps = [
        step
        for _index, step in sorted(
            enumerate(execution_steps),
            key=lambda item: (_execution_step_timestamp(item[1]), item[0]),
        )
    ]
    selected_step = _select_step(steps=execution_steps, runtime=normalized_runtime)
    selected_step_payload = _selected_step_payload(
        job=normalized_job,
        runtime=normalized_runtime,
        selected_step=selected_step,
    )
    if selected_step_payload:
        selected_id = str(selected_step_payload.get("id") or "")
        for step in execution_steps:
            if str(step.get("id") or "") == selected_id:
                step.update(selected_step_payload)
                step["selected"] = True
                step["can_restart_from_here"] = bool(
                    selected_step_payload.get("can_restart_from_here")
                )
    return execution_steps, selected_step_payload


def build_review_workspace(
    *,
    approval_gate: dict[str, Any] | None = None,
    user_role: str = "viewer",
) -> dict[str, Any] | None:
    return _build_review_workspace(
        approval_gate=_normalize_dict(approval_gate),
        user_role=_normalize_role(user_role),
    )


def build_cost_breakdown_payload(
    cost_breakdown: dict[str, Any] | None,
) -> dict[str, Any]:
    return _build_cost_breakdown(_normalize_dict(cost_breakdown))


def build_previous_decisions_payload(
    *,
    approval_history: list[dict[str, Any]] | None = None,
    decision_overrides: list[dict[str, Any]] | None = None,
    project_id: str | None = None,
    job_id: str | None = None,
) -> list[dict[str, Any]]:
    return _build_previous_decisions(
        approval_history=_normalize_rows(approval_history),
        decision_overrides=_normalize_rows(decision_overrides),
        project_id=str(project_id or "").strip() or None,
        job_id=str(job_id or "").strip() or None,
    )


def build_diagnostics_payload(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    approval_gate: dict[str, Any] | None = None,
    causal_chain: dict[str, Any] | None = None,
    incident: dict[str, Any] | None = None,
    job_errors: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    thread_state: dict[str, Any] | None = None,
    runtime_journal: list[dict[str, Any]] | None = None,
    previous_decisions: list[dict[str, Any]] | None = None,
    decision_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _build_diagnostics(
        job=_normalize_dict(job),
        runtime=_normalize_dict(runtime),
        approval_gate=_normalize_dict(approval_gate),
        causal_chain=_normalize_dict(causal_chain),
        incident=_normalize_dict(incident),
        job_errors=_normalize_rows(job_errors),
        messages=_normalize_rows(messages),
        events=_normalize_rows(events),
        thread_state=_normalize_dict(thread_state),
        runtime_journal=_normalize_rows(runtime_journal),
        previous_decisions=_normalize_rows(previous_decisions),
        decision_overrides=_normalize_rows(decision_overrides),
    )


def build_job_detail_sections(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    thread_state: dict[str, Any] | None = None,
    approval_gate: dict[str, Any] | None = None,
    approval_history: list[dict[str, Any]] | None = None,
    decision_overrides: list[dict[str, Any]] | None = None,
    cost_breakdown: dict[str, Any] | None = None,
    causal_chain: dict[str, Any] | None = None,
    incident: dict[str, Any] | None = None,
    job_errors: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    workflow_run: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    runtime_journal: list[dict[str, Any]] | None = None,
    user_role: str = "viewer",
    resolve_artifact: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    normalized_job = _normalize_dict(job)
    normalized_runtime = _normalize_dict(runtime)
    normalized_thread_state = _normalize_dict(thread_state)
    normalized_approval_gate = _normalize_dict(approval_gate)
    normalized_approval_history = _normalize_rows(approval_history)
    normalized_decision_overrides = _normalize_rows(decision_overrides)
    normalized_cost_breakdown = _normalize_dict(cost_breakdown)
    normalized_causal_chain = _normalize_dict(causal_chain)
    normalized_incident = _normalize_dict(incident)
    normalized_job_errors = _normalize_rows(job_errors)
    normalized_messages = _normalize_rows(messages)
    normalized_events = _normalize_rows(events)
    normalized_runtime_journal = _normalize_rows(runtime_journal)

    execution_steps, selected_step_payload = build_execution_steps(
        job=normalized_job,
        runtime=normalized_runtime,
        thread_state=normalized_thread_state,
        messages=normalized_messages,
        workflow_run=_normalize_dict(workflow_run),
        resolve_artifact=resolve_artifact,
    )
    review_workspace = build_review_workspace(
        approval_gate=normalized_approval_gate,
        user_role=user_role,
    )
    previous_decisions = build_previous_decisions_payload(
        approval_history=normalized_approval_history,
        decision_overrides=normalized_decision_overrides,
        project_id=str(normalized_job.get("project_id") or "").strip() or None,
        job_id=str(normalized_job.get("id") or "").strip() or None,
    )
    cost_payload = build_cost_breakdown_payload(normalized_cost_breakdown)
    diagnostics = build_diagnostics_payload(
        job=normalized_job,
        runtime=normalized_runtime,
        approval_gate=normalized_approval_gate,
        causal_chain=normalized_causal_chain,
        incident=normalized_incident,
        job_errors=normalized_job_errors,
        messages=normalized_messages,
        events=normalized_events,
        thread_state=normalized_thread_state,
        runtime_journal=normalized_runtime_journal,
        previous_decisions=previous_decisions,
        decision_overrides=normalized_decision_overrides,
    )
    return {
        "execution_steps": execution_steps,
        "selected_step": selected_step_payload,
        "review_workspace": review_workspace,
        "previous_decisions": previous_decisions,
        "cost_breakdown": cost_payload,
        "diagnostics": diagnostics,
    }
