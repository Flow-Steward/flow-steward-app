"""Shared helpers for retryable interrupted-step snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.application.orchestration.active_step import active_step, run_cursor
from core.application.orchestration.helpers import resolve_step_agent, resolve_step_skill
from core.application.orchestration.run_cursor import RunCursorError
from core.application.orchestration.run_definition import (
    IncompatibleRuntimeStateError,
    RunDefinitionIntegrityError,
)


def sanitize_interrupted_step_snapshot(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    snapshot = {
        "detected_at": str(raw.get("detected_at") or "").strip() or None,
        "detection_source": str(raw.get("detection_source") or "").strip() or None,
        "job_id": str(raw.get("job_id") or "").strip() or None,
        "thread_id": str(raw.get("thread_id") or "").strip() or None,
        "step_id": str(raw.get("step_id") or "").strip() or None,
        "step_execution_id": str(raw.get("step_execution_id") or "").strip() or None,
        "agent_id": str(raw.get("agent_id") or "").strip() or None,
        "skill": str(raw.get("skill") or "").strip() or None,
        "correlation_id": str(raw.get("correlation_id") or "").strip() or None,
        "provider": str(raw.get("provider") or "").strip().lower() or None,
        "model_used": str(raw.get("model_used") or "").strip() or None,
        "prompt_version": str(raw.get("prompt_version") or "").strip() or None,
        "message_id": str(raw.get("message_id") or "").strip() or None,
    }
    system_prompt = str(raw.get("system_prompt") or "")
    user_prompt = str(raw.get("user_prompt") or "")
    if system_prompt:
        snapshot["system_prompt"] = system_prompt[:12000]
    if user_prompt:
        snapshot["user_prompt"] = user_prompt[:24000]
    if not any(
        snapshot.get(key)
        for key in ("agent_id", "skill", "step_id", "message_id", "correlation_id")
    ):
        return None
    if not snapshot.get("agent_id") or not snapshot.get("skill"):
        return None
    return snapshot


def extract_runtime_step_hint(runtime_state: dict) -> dict:
    """The step the run cursor points at, resolved in the run's pinned definition."""
    hint: dict[str, str] = {}
    if not isinstance(runtime_state, dict):
        return hint
    cursor = run_cursor(runtime_state)
    if not cursor.step_id:
        return hint
    hint["step_id"] = cursor.step_id
    if cursor.step_execution_id:
        hint["step_execution_id"] = cursor.step_execution_id
    try:
        current = active_step(runtime_state)
    except (RunDefinitionIntegrityError, IncompatibleRuntimeStateError, RunCursorError):
        # Without its pinned definition the cursor still names the step.
        return hint
    if current is None:
        return hint
    agent_id = resolve_step_agent(current.step, None, state=runtime_state)
    skill = resolve_step_skill(current.step, state=runtime_state, default="")
    if agent_id:
        hint["agent_id"] = agent_id
    if skill:
        hint["skill"] = skill
    return hint


def detect_interrupted_step_snapshot(
    bl,
    job: dict,
    *,
    get_thread_state_values_fn,
    to_json_obj,
    utc_now_fn=None,
    sanitize_snapshot_fn=sanitize_interrupted_step_snapshot,
    extract_step_hint_fn=extract_runtime_step_hint,
) -> dict | None:
    if not bl or not isinstance(job, dict):
        return None
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        return None

    runtime_state = get_thread_state_values_fn(str(job.get("thread_id") or "").strip())
    step_hint = extract_step_hint_fn(runtime_state if isinstance(runtime_state, dict) else {})

    try:
        logs = bl.get_logs(job_id, since_id=0, limit=4000) or []
    except Exception:
        logs = []

    latest_prompt_request = None
    prompt_response_by_corr: dict[str, int] = {}
    open_nodes: list[dict] = []
    latest_prompt_request_order = -1
    for row_order, row in enumerate(logs):
        if not isinstance(row, dict):
            continue
        action_type = str(row.get("action_type") or "").strip().lower()
        row_agent = str(row.get("agent") or "").strip()
        row_meta = to_json_obj(row.get("meta"))
        if not isinstance(row_meta, dict):
            row_meta = {}
        row_corr = str(row_meta.get("correlation_id") or "").strip()
        if action_type == "prompt_request":
            latest_prompt_request = row
            latest_prompt_request_order = row_order
        elif action_type in {"prompt_response", "cli_response"} and row_corr:
            prompt_response_by_corr[row_corr] = max(
                prompt_response_by_corr.get(row_corr, -1), row_order
            )
        elif action_type == "node_start" and row_agent:
            open_nodes.append({"id": row_order, "agent": row_agent})
        elif action_type in {"node_end", "node_error"} and row_agent:
            for idx in range(len(open_nodes) - 1, -1, -1):
                if str(open_nodes[idx].get("agent") or "") == row_agent:
                    open_nodes.pop(idx)
                    break

    utc_now = utc_now_fn or (lambda: datetime.now(UTC))
    snapshot: dict[str, Any] = {
        "detected_at": utc_now().isoformat(),
        "detection_source": "",
        "job_id": job_id,
        "thread_id": str(job.get("thread_id") or "").strip() or None,
        "step_id": step_hint.get("step_id"),
        "step_execution_id": step_hint.get("step_execution_id"),
        "agent_id": step_hint.get("agent_id")
        or str(job.get("current_agent") or "").strip()
        or None,
        "skill": step_hint.get("skill"),
        "correlation_id": None,
        "message_id": None,
        "provider": None,
        "model_used": None,
        "prompt_version": None,
        "system_prompt": "",
        "user_prompt": "",
    }

    if isinstance(latest_prompt_request, dict):
        req_meta = to_json_obj(latest_prompt_request.get("meta"))
        req_payload = to_json_obj(latest_prompt_request.get("payload"))
        req_corr = str((req_meta or {}).get("correlation_id") or "").strip()
        req_response_order = prompt_response_by_corr.get(req_corr, -1) if req_corr else -1
        if req_response_order <= latest_prompt_request_order:
            snapshot["detection_source"] = "prompt_request_without_response"
            snapshot["correlation_id"] = req_corr or None
            snapshot["agent_id"] = str(
                latest_prompt_request.get("agent") or ""
            ).strip() or snapshot.get("agent_id")
            snapshot["skill"] = str((req_meta or {}).get("skill") or "").strip() or snapshot.get(
                "skill"
            )
            snapshot["step_id"] = str(
                (req_meta or {}).get("step_id") or ""
            ).strip() or snapshot.get("step_id")
            snapshot["provider"] = (
                str((req_meta or {}).get("provider") or "").strip().lower() or None
            )
            snapshot["model_used"] = (
                str(latest_prompt_request.get("model_used") or "").strip() or None
            )
            snapshot["prompt_version"] = (
                str((req_payload or {}).get("prompt_version") or "").strip() or None
            )
            snapshot["system_prompt"] = str((req_payload or {}).get("system_prompt") or "")[:12000]
            snapshot["user_prompt"] = str((req_payload or {}).get("user_prompt") or "")[:24000]

    if not snapshot.get("detection_source") and open_nodes:
        latest_open = max(
            open_nodes,
            key=lambda row: (
                str(row.get("timestamp") or row.get("created_at") or ""),
                str(row.get("id") or ""),
            ),
        )
        snapshot["detection_source"] = "open_node_without_end"
        snapshot["agent_id"] = str(latest_open.get("agent") or "").strip() or snapshot.get(
            "agent_id"
        )

    latest_message = None
    try:
        if hasattr(bl, "get_latest_agent_message"):
            latest_message = bl.get_latest_agent_message(job_id)
    except Exception:
        latest_message = None
    if isinstance(latest_message, dict):
        if not snapshot.get("message_id"):
            snapshot["message_id"] = str(latest_message.get("id") or "").strip() or None
        msg_corr = str(latest_message.get("correlation_id") or "").strip()
        if not snapshot.get("correlation_id") and msg_corr:
            snapshot["correlation_id"] = msg_corr
        if not snapshot.get("skill"):
            snapshot["skill"] = str(latest_message.get("skill") or "").strip() or None
        if not snapshot.get("step_id"):
            snapshot["step_id"] = str(latest_message.get("step_id") or "").strip() or None
        if not snapshot.get("agent_id"):
            snapshot["agent_id"] = str(latest_message.get("from_agent") or "").strip() or None

    if not snapshot.get("detection_source"):
        return None
    if not snapshot.get("agent_id") or not snapshot.get("skill"):
        return None
    return sanitize_snapshot_fn(snapshot)


__all__ = [
    "detect_interrupted_step_snapshot",
    "extract_runtime_step_hint",
    "sanitize_interrupted_step_snapshot",
]
