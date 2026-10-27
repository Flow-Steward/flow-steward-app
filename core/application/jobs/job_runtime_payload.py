"""Helpers for building job runtime payloads for API, UI, and WebSocket consumers."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.queue_delivery_queries import build_queue_delivery_view
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.security.prompt_safety import redact_structure_for_persistence

_PERSISTED_ERROR_HEADER_FIELDS = (
    "id",
    "source",
    "category",
    "provider_name",
    "http_status",
    "provider_error_code",
    "provider_error_type",
    "request_id",
    "retryable",
    "error_message",
    "response_excerpt",
    "created_at",
)

_TERMINAL_JOB_STATUSES = frozenset(
    {
        "cancelled",
        "completed",
        "failed",
        "max_iterations",
        "triage_needed",
    }
)


def _is_terminal_job_status(status: Any) -> bool:
    return str(status or "").strip().lower() in _TERMINAL_JOB_STATUSES


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


_LANE_STATUS = {"running": "in_progress", "pending": "pending"}


def _innermost_frame(cursor: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
    """The innermost container frame, or the innermost one of *kind*."""
    frames = cursor.get("frames")
    for frame in reversed(frames if isinstance(frames, list) else []):
        if isinstance(frame, dict) and (kind is None or frame.get("frame") == kind):
            return frame
    return {}


def _active_steps(runtime_state: dict[str, Any], *, job_status: str = "") -> list[dict[str, Any]]:
    """The step executions in flight: the cursor's step, or every running lane.

    Each entry is ``{step_id, step_execution_id, attempt, group_id,
    for_each_id, lane_id, iteration, status}``: ``iteration`` is the position
    in the innermost For each. In a lanes body the cursor names no step and the
    lanes are listed; a serial pause reports the step with the run's waiting
    status.
    """
    cursor = runtime_state.get("run_cursor")
    if not isinstance(cursor, dict):
        return []
    frame = _innermost_frame(cursor)
    for_each_frame = _innermost_frame(cursor, "for_each")
    # The immediate owner only: a For each step in a Group names its For each,
    # never the Group around it as well.
    owner_kind = str(frame.get("frame") or "") if frame else ""
    group_id = str(frame.get("group_id") or "").strip() or None if owner_kind == "group" else None
    for_each_id = (
        str(frame.get("for_each_id") or "").strip() or None if owner_kind == "for_each" else None
    )
    iteration = _coerce_int(for_each_frame.get("item_index")) if for_each_frame else None
    for_each_progress = _for_each_progress(for_each_frame)
    step_id = str(cursor.get("step_id") or "").strip()
    execution_id = str(cursor.get("step_execution_id") or "").strip()
    if step_id and execution_id:
        waiting = str(job_status or "").strip().lower()
        return [
            {
                "step_id": step_id,
                "step_execution_id": execution_id,
                "attempt": max(1, _coerce_int(cursor.get("attempt")) or 1),
                "group_id": group_id,
                "for_each_id": for_each_id,
                "lane_id": None,
                "iteration": iteration,
                "for_each_progress": for_each_progress,
                "status": waiting if waiting.startswith("awaiting_") else "in_progress",
            }
        ]
    lanes = runtime_state.get("parallel_lanes")
    if not frame or not isinstance(lanes, list):
        return []
    active: list[dict[str, Any]] = []
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        lane_step = str(lane.get("step_id") or "").strip()
        lane_execution = str(lane.get("step_execution_id") or "").strip()
        lane_status = str(lane.get("status") or "").strip().lower()
        if not lane_step or not lane_execution or lane_status == "completed":
            continue
        active.append(
            {
                "step_id": lane_step,
                "step_execution_id": lane_execution,
                "attempt": max(1, _coerce_int(lane.get("attempt")) or 1),
                "group_id": group_id,
                "for_each_id": for_each_id,
                "lane_id": str(lane.get("lane_id") or "").strip() or None,
                "iteration": iteration,
                "for_each_progress": for_each_progress,
                "status": _LANE_STATUS.get(lane_status, lane_status or "in_progress"),
            }
        )
    return active


def _for_each_progress(frame: dict[str, Any] | None) -> dict[str, Any] | None:
    """Where the innermost For each is: its item position and bounded counts, never items."""
    if not frame:
        return None
    return {
        "for_each_id": str(frame.get("for_each_id") or "").strip() or None,
        "item_index": _coerce_int(frame.get("item_index")),
        "selected_count": _coerce_int(frame.get("selected_count")),
        "source_count": _coerce_int(frame.get("source_count")),
        "applied_limit": _coerce_int(frame.get("applied_limit")),
        "processed_count": _coerce_int(frame.get("processed_count")),
        "truncated": bool(frame.get("truncated")),
    }


_PUBLIC_WAIT_FIELDS = (
    "kind",
    "wait_id",
    "step_id",
    "group_id",
    "due_at",
    "child_job_id",
    "approval_id",
)


def _public_workflow_wait(job: dict[str, Any], meta: Any) -> dict[str, Any] | None:
    """What a paused run waits for: its due time, its child job or its approval."""
    status = str(job.get("status") or "").strip().lower()
    wait = meta.get("workflow_wait") if isinstance(meta, dict) else None
    if not status.startswith("awaiting_") or not isinstance(wait, dict):
        return None
    return {field: wait.get(field) for field in _PUBLIC_WAIT_FIELDS if wait.get(field) is not None}


def current_step_scope(runtime_state: dict[str, Any], job: dict[str, Any]) -> tuple[str, str]:
    """``(current_step_id, current_group_id)`` from the checkpoint cursor.

    In a parallel group the cursor names no step: the current step is empty and
    the group is set. Only a run without a cursor (not started) falls back to
    the job row.
    """
    cursor = runtime_state.get("run_cursor") if isinstance(runtime_state, dict) else None
    if isinstance(cursor, dict):
        frame = _innermost_frame(cursor, "group")
        return (
            str(cursor.get("step_id") or "").strip(),
            str(frame.get("group_id") or "").strip(),
        )
    return (
        str(job.get("current_step_id") or "").strip(),
        str(job.get("current_group_id") or "").strip(),
    )


def _retry_count(runtime_state: dict[str, Any]) -> int:
    """Retries of the active step execution: its attempt beyond the first."""
    cursor = runtime_state.get("run_cursor")
    attempt = _coerce_int(cursor.get("attempt")) if isinstance(cursor, dict) else None
    return max(0, (attempt or 1) - 1)


def _active_agent(active_agents: Any) -> str:
    if not isinstance(active_agents, list) or not active_agents:
        return ""
    return str(active_agents[0])


def _compute_wait_reason_compat(
    compute_wait_reason,
    job: dict[str, Any],
    runtime_state: dict[str, Any],
    approval_payload: dict[str, Any] | None,
    queue_delivery: dict[str, Any],
) -> str:
    try:
        signature = inspect.signature(compute_wait_reason)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        params = signature.parameters
        if "queue_delivery" in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        ):
            return compute_wait_reason(
                job,
                runtime_state,
                approval_payload,
                queue_delivery=queue_delivery,
            )
    return compute_wait_reason(job, runtime_state, approval_payload)


def _normalize_meta(job: dict[str, Any], *, to_json_obj) -> dict[str, Any]:
    meta = to_json_obj(job.get("meta")) or {}
    return meta if isinstance(meta, dict) else {}


def _load_latest_structured_error(
    job_id: str, bl, *, serialize_job_error_row
) -> dict[str, Any] | None:
    if not bl or not hasattr(bl, "list_job_errors"):
        return None
    try:
        error_rows = bl.list_job_errors(job_id, limit=1)
        if error_rows:
            return serialize_job_error_row(dict(error_rows[0]))
    except Exception:
        return None
    return None


def build_persisted_diagnostic_payload(
    job: dict[str, Any],
    bl: Any,
    *,
    serialize_job_error_row: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Read a bounded diagnostic header without inspecting live execution state.

    Callers authorize the job before invoking this reader. Bulk traceback and
    context remain on the error subresource, not the status polling payload.
    """
    error = _load_latest_structured_error(
        str(job["id"]), bl, serialize_job_error_row=serialize_job_error_row
    )
    if not error:
        return {}
    header = redact_structure_for_persistence(
        {key: error[key] for key in _PERSISTED_ERROR_HEADER_FIELDS if key in error},
        project_id=job.get("project_id"),
        job_id=str(job["id"]),
        context_type="display",
    )
    return {
        "last_structured_error": {
            key: value[:2000] if isinstance(value, str) else value for key, value in header.items()
        }
    }


def _build_output_excerpts(
    job: dict[str, Any],
    latest_response_log: dict[str, Any] | None,
    latest_err: dict[str, Any] | None,
    latest_message: dict[str, Any] | None,
    *,
    to_json_obj,
) -> tuple[str, str]:
    last_stderr = ""
    last_response = str(job.get("last_response_excerpt") or "")
    if not last_response and latest_response_log:
        response_result = to_json_obj(latest_response_log.get("result")) or {}
        if isinstance(response_result, dict) and response_result.get("excerpt"):
            last_response = str(response_result.get("excerpt"))
        elif latest_response_log.get("payload"):
            payload = to_json_obj(latest_response_log.get("payload")) or {}
            if isinstance(payload, dict) and payload.get("chars"):
                last_response = f"(response captured, {payload.get('chars')} chars)"
    if latest_err:
        result = to_json_obj(latest_err.get("result")) or {}
        if isinstance(result, dict) and result.get("stderr_excerpt"):
            last_stderr = str(result.get("stderr_excerpt"))
        elif latest_err.get("error"):
            last_stderr = str(latest_err.get("error"))
    if not last_response and isinstance(latest_message, dict):
        summary = latest_message.get("summary")
        if isinstance(summary, str) and summary.strip():
            last_response = summary.strip()
        else:
            artifacts = to_json_obj(latest_message.get("artifacts")) or {}
            if isinstance(artifacts, dict) and artifacts.get("raw_output"):
                last_response = str(artifacts.get("raw_output"))[:1000]
    return last_response, last_stderr


def _build_heartbeat_payload(
    job: dict[str, Any],
    *,
    now_fn,
    stuck_threshold_seconds: int,
) -> dict[str, Any]:
    heartbeat_at = job.get("heartbeat_at")
    heartbeat_iso = heartbeat_at.isoformat() if heartbeat_at else None
    heartbeat_age_sec = None
    if heartbeat_at:
        try:
            heartbeat_age_sec = max(0, int((now_fn() - heartbeat_at).total_seconds()))
        except Exception:
            heartbeat_age_sec = None
    is_stuck = bool(
        heartbeat_age_sec is not None
        and (job.get("status") in ("in_progress",))
        and heartbeat_age_sec > stuck_threshold_seconds
    )
    return {
        "heartbeat_at": heartbeat_iso,
        "heartbeat_age_sec": heartbeat_age_sec,
        "stuck_threshold_sec": stuck_threshold_seconds,
        "is_stuck": is_stuck,
    }


def _workflow_selection_details(
    job: dict[str, Any],
    runtime_state: dict[str, Any],
    meta: dict[str, Any],
) -> tuple[str, str, str]:
    raw_invocation = meta.get("invocation")
    invocation: dict[str, Any] = raw_invocation if isinstance(raw_invocation, dict) else {}
    workflow_candidates = (
        ("job.selected_workflow", job.get("selected_workflow")),
        ("runtime_state.workflow_id", runtime_state.get("workflow_id")),
        ("job.workflow_id", job.get("workflow_id")),
        ("meta.selected_workflow_id", meta.get("selected_workflow_id")),
        ("meta.invocation.selected_workflow_id", invocation.get("selected_workflow_id")),
    )
    selected_workflow = ""
    selected_workflow_source = ""
    for source, value in workflow_candidates:
        normalized = str(value or "").strip()
        if normalized:
            selected_workflow = normalized
            selected_workflow_source = source
            break
    selected_workflow_source = (
        str(runtime_state.get("workflow_selection_source") or "").strip()
        or str(meta.get("workflow_selection_source") or "").strip()
        or selected_workflow_source
        or "explicit_request"
    )
    workflow_selection_reason = str(
        runtime_state.get("routing_reason") or meta.get("workflow_selection_reason") or ""
    ).strip()
    return selected_workflow, selected_workflow_source, workflow_selection_reason


def _normalize_positions(positions_raw: Any) -> list[int]:
    if not isinstance(positions_raw, list):
        return []
    positions: list[int] = []
    for value in positions_raw:
        coerced = _coerce_int(value)
        if coerced is not None:
            positions.append(coerced)
    return positions


def _parallel_lanes(runtime_state: dict[str, Any]) -> list[dict[str, Any]]:
    parallel_lanes_raw = runtime_state.get("parallel_lanes")
    if not isinstance(parallel_lanes_raw, list):
        return []
    parallel_lanes: list[dict[str, Any]] = []
    for lane in parallel_lanes_raw:
        if not isinstance(lane, dict):
            continue
        merge_id = lane.get("merge_id")
        parallel_lanes.append(
            {
                "lane_id": str(lane.get("lane_id") or "").strip() or "lane",
                "status": str(lane.get("status") or "pending").strip().lower() or "pending",
                "completed_steps": max(0, _coerce_int(lane.get("completed_steps")) or 0),
                "total_steps": max(0, _coerce_int(lane.get("total_steps")) or 0),
                "next_agent": str(lane.get("next_agent") or "").strip() or None,
                "positions": _normalize_positions(lane.get("positions")),
                "merge_status": str(lane.get("merge_status") or "").strip().lower() or None,
                "merge_id": str(merge_id or "").strip() or None,
            }
        )
    return parallel_lanes


def _parallel_concurrency_cap(runtime_state: dict[str, Any]) -> int:
    return max(1, _coerce_int(runtime_state.get("parallel_concurrency_cap")) or 1)


def _parallel_execution_order(runtime_state: dict[str, Any]) -> list[int]:
    execution_order_raw = runtime_state.get("parallel_execution_order")
    if not isinstance(execution_order_raw, list):
        return []
    execution_order: list[int] = []
    for idx in execution_order_raw:
        coerced = _coerce_int(idx)
        if coerced is not None:
            execution_order.append(coerced)
    return execution_order


def build_runtime_payload(
    job: dict[str, Any],
    bl,
    *,
    get_thread_state_values,
    to_json_obj,
    serialize_job_error_row,
    is_graph_interrupt_trace,
    ws_get_approval_payload,
    sanitize_retryable_interrupted_step_snapshot,
    compute_wait_reason,
    serialize_event_row,
    serialize_agent_message_row,
    now_fn=None,
    stuck_threshold_seconds: int | None = None,
) -> dict[str, Any]:
    """Build the live runtime payload for job pages, APIs, and streaming consumers."""
    from core.infrastructure.runtime.test_mode import parse_test_mode

    job_id = str(job["id"])
    runtime_state = get_thread_state_values(job.get("thread_id") or "")
    if not isinstance(runtime_state, dict):
        runtime_state = {}
    retry_count = _retry_count(runtime_state)
    active_agent = _active_agent(runtime_state.get("active_agents"))
    now_fn = now_fn or (lambda: datetime.now(UTC))
    if stuck_threshold_seconds is None:
        stuck_threshold_seconds = (
            _coerce_int(_runtime_env.get("FS_STUCK_JOB_SECONDS", "180")) or 180
        )

    latest_err = bl.get_latest_log_entry(job_id, require_error=True) if bl else None
    latest_response_log = bl.get_latest_log_entry(job_id, ["cli_response"]) if bl else None
    latest_tool = bl.get_latest_event(job_id, ["tool_call"]) if bl else None
    latest_route = bl.get_latest_event(job_id, ["routing_decision", "decision"]) if bl else None
    latest_message = bl.get_latest_agent_message(job_id) if bl else None
    latest_structured_error = _load_latest_structured_error(
        job_id,
        bl,
        serialize_job_error_row=serialize_job_error_row,
    )
    if latest_err and is_graph_interrupt_trace(latest_err.get("error")):
        latest_err = None

    last_response, last_stderr = _build_output_excerpts(
        job,
        latest_response_log,
        latest_err,
        latest_message,
        to_json_obj=to_json_obj,
    )
    heartbeat_payload = _build_heartbeat_payload(
        job,
        now_fn=now_fn,
        stuck_threshold_seconds=stuck_threshold_seconds,
    )
    approval_payload = (
        ws_get_approval_payload(job) if job.get("status") == "awaiting_approval" else None
    )
    meta = _normalize_meta(job, to_json_obj=to_json_obj)
    queue_delivery = build_queue_delivery_view(job, to_json_obj=to_json_obj)
    retryable_interrupted_step = sanitize_retryable_interrupted_step_snapshot(
        to_json_obj(meta.get("retryable_interrupted_step"))
    )
    runtime_test_mode = parse_test_mode(runtime_state.get("test_mode"), False) or parse_test_mode(
        meta.get("test_mode"), False
    )
    selected_workflow, selected_workflow_source, workflow_selection_reason = (
        _workflow_selection_details(job, runtime_state, meta)
    )
    # The checkpoint's cursor is where the run is; the job row only mirrors it.
    current_step_id, current_group_id = current_step_scope(runtime_state, job)
    active_steps = _active_steps(runtime_state, job_status=str(job.get("status") or ""))
    current_agent = (
        job.get("current_agent")
        or active_agent
        or (latest_message.get("from_agent") if isinstance(latest_message, dict) else "")
    )
    if _is_terminal_job_status(job.get("status")):
        current_step_id = ""
        current_group_id = ""
        active_steps = []
        current_agent = ""
    proof_of_work = runtime_state.get("proof_of_work")
    if not isinstance(proof_of_work, dict):
        proof_of_work = None
    last_failure_class = str(runtime_state.get("last_failure_class") or "").strip().lower() or None
    last_retry_reason = str(runtime_state.get("last_retry_reason") or "").strip() or None
    last_retry_retryable = runtime_state.get("last_retry_retryable")
    if not isinstance(last_retry_retryable, bool):
        last_retry_retryable = None
    retry_backoff_seconds = runtime_state.get("retry_backoff_seconds")
    if isinstance(retry_backoff_seconds, bool) or not isinstance(
        retry_backoff_seconds, int | float
    ):
        retry_backoff_seconds = None
    retry_telemetry = runtime_state.get("retry_telemetry")
    retry_telemetry = dict(retry_telemetry) if isinstance(retry_telemetry, dict) else None

    job_last_error = str(job.get("last_error") or "")
    if is_graph_interrupt_trace(job_last_error):
        job_last_error = ""

    return {
        "selected_workflow": selected_workflow,
        "test_mode": runtime_test_mode,
        "retryable_interrupted_step": retryable_interrupted_step,
        "can_retry_interrupted_step": bool(
            isinstance(retryable_interrupted_step, dict)
            and str(retryable_interrupted_step.get("agent_id") or "").strip()
            and str(retryable_interrupted_step.get("skill") or "").strip()
        ),
        "selected_workflow_source": selected_workflow_source,
        "workflow_selection_reason": workflow_selection_reason,
        "current_step_id": current_step_id or None,
        "current_group_id": current_group_id or None,
        "active_steps": active_steps,
        "workflow_wait": _public_workflow_wait(job, meta),
        "active_agent": current_agent,
        "retry_count": retry_count,
        "last_failure_class": last_failure_class,
        "last_retry_reason": last_retry_reason,
        "last_retry_retryable": last_retry_retryable,
        "retry_backoff_seconds": retry_backoff_seconds,
        "retry_telemetry": retry_telemetry,
        "proof_of_work": proof_of_work,
        "parallel_lanes": _parallel_lanes(runtime_state),
        "parallel_concurrency_cap": _parallel_concurrency_cap(runtime_state),
        "parallel_execution_order": _parallel_execution_order(runtime_state),
        "queue_delivery": queue_delivery,
        "wait_reason": _compute_wait_reason_compat(
            compute_wait_reason,
            job,
            runtime_state,
            approval_payload,
            queue_delivery,
        ),
        **heartbeat_payload,
        "last_response_excerpt": last_response,
        "last_stderr_excerpt": last_stderr or job_last_error,
        "last_tool_call": serialize_event_row(latest_tool) if latest_tool else None,
        "last_routing_decision": serialize_event_row(latest_route) if latest_route else None,
        "last_message": serialize_agent_message_row(latest_message) if latest_message else None,
        "last_structured_error": latest_structured_error,
    }


def build_runtime_payload_builder(
    *,
    get_thread_state_values,
    to_json_obj,
    serialize_job_error_row,
    is_graph_interrupt_trace,
    ws_get_approval_payload,
    sanitize_retryable_interrupted_step_snapshot,
    compute_wait_reason,
    serialize_event_row,
    serialize_agent_message_row,
):
    def _builder(job: dict[str, Any], bl) -> dict[str, Any]:
        return build_runtime_payload(
            job,
            bl,
            get_thread_state_values=get_thread_state_values,
            to_json_obj=to_json_obj,
            serialize_job_error_row=serialize_job_error_row,
            is_graph_interrupt_trace=is_graph_interrupt_trace,
            ws_get_approval_payload=ws_get_approval_payload,
            sanitize_retryable_interrupted_step_snapshot=sanitize_retryable_interrupted_step_snapshot,
            compute_wait_reason=compute_wait_reason,
            serialize_event_row=serialize_event_row,
            serialize_agent_message_row=serialize_agent_message_row,
        )

    return _builder


def build_runtime_payload_fallback(
    job: dict[str, Any],
    *,
    watchdog_stuck_seconds: int,
    now_fn=None,
) -> dict[str, Any]:
    """Return a lightweight runtime snapshot when live state inspection is unavailable."""
    import json

    from core.infrastructure.runtime.serve_request_runtime import compute_wait_reason

    def _fallback_to_json_obj(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    now_fn = now_fn or (lambda: datetime.now(UTC))
    heartbeat_at = job.get("heartbeat_at")
    isoformat_fn = getattr(heartbeat_at, "isoformat", None)
    heartbeat_iso = isoformat_fn() if callable(isoformat_fn) else None
    heartbeat_age_sec = None
    if heartbeat_at:
        try:
            heartbeat_age_sec = max(0, int((now_fn() - heartbeat_at).total_seconds()))
        except Exception:
            heartbeat_age_sec = None

    terminal_job = _is_terminal_job_status(job.get("status"))
    current_step_id = "" if terminal_job else str(job.get("current_step_id") or "").strip()
    current_group_id = "" if terminal_job else str(job.get("current_group_id") or "").strip()
    current_agent = "" if terminal_job else str(job.get("current_agent") or "").strip()
    selected_workflow = str(job.get("selected_workflow") or job.get("workflow_id") or "").strip()
    last_error = str(job.get("last_error") or "").strip()
    last_response = str(job.get("last_response_excerpt") or "").strip()
    queue_delivery = build_queue_delivery_view(
        job,
        to_json_obj=_fallback_to_json_obj,
    )
    is_stuck = bool(
        heartbeat_age_sec is not None
        and str(job.get("status") or "").strip().lower() == "in_progress"
        and heartbeat_age_sec > watchdog_stuck_seconds
    )

    return {
        "selected_workflow": selected_workflow,
        "test_mode": False,
        "retryable_interrupted_step": None,
        "can_retry_interrupted_step": False,
        "selected_workflow_source": "job_row" if selected_workflow else "unknown",
        "workflow_selection_reason": "",
        "current_step_id": current_step_id or None,
        "current_group_id": current_group_id or None,
        "active_steps": [],
        "active_agent": current_agent,
        "retry_count": 0,
        "last_failure_class": None,
        "last_retry_reason": None,
        "last_retry_retryable": None,
        "retry_backoff_seconds": None,
        "proof_of_work": None,
        "parallel_lanes": [],
        "parallel_concurrency_cap": 1,
        "parallel_execution_order": [],
        "queue_delivery": queue_delivery,
        "wait_reason": compute_wait_reason(
            job,
            {},
            retryable_interrupted_step_status="retryable_interrupted_step",
            queue_delivery=queue_delivery,
        ),
        "heartbeat_at": heartbeat_iso,
        "heartbeat_age_sec": heartbeat_age_sec,
        "stuck_threshold_sec": watchdog_stuck_seconds,
        "is_stuck": is_stuck,
        "last_response_excerpt": last_response,
        "last_stderr_excerpt": last_error,
        "last_tool_call": None,
        "last_routing_decision": None,
        "last_message": None,
        "last_structured_error": None,
        "runtime_fallback": True,
    }
