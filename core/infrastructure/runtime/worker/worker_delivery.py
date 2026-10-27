"""Queue-delivery metadata helpers for worker task execution."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from core.infrastructure.runtime.worker.queue_names import queue_dead_letter_queue_name

from .worker_common import (
    _RETRYABLE_INTERRUPTED_STEP_STATUS,
    _to_json_obj,
    _truncate_text,
)


def _delivery_action(action: str, **detail: object) -> dict[str, object]:
    payload: dict[str, object] = {"action": str(action or "").strip().lower() or "ack"}
    payload.update(detail)
    return payload


def _queue_name_for_delivery(msg: dict[str, object], job_row: dict[str, object] | None) -> str:
    queue_name = str((job_row or {}).get("queue_name") or msg.get("queue_name") or "").strip()
    if queue_name:
        return queue_name
    project_id = str((job_row or {}).get("project_id") or msg.get("project_id") or "").strip()
    return f"platform.project.{project_id}" if project_id else ""


def _queue_delivery_context(
    msg: dict[str, object],
    job_row: dict[str, object] | None,
) -> tuple[dict[str, object], str, str, dict[str, object] | None]:
    meta = _to_json_obj((job_row or {}).get("meta"))
    policy = meta.get("project_policy_snapshot")
    if not isinstance(policy, dict):
        policy = None
    queue_name = _queue_name_for_delivery(msg, job_row)
    return meta, queue_name, queue_dead_letter_queue_name(queue_name), policy


def _persist_queue_delivery_meta(bl, job_id: str, patch: dict[str, object] | None) -> None:
    if not patch:
        return
    merger = getattr(bl, "merge_job_meta", None)
    if callable(merger):
        merger(job_id, patch)
        return
    getter = getattr(bl, "get_job", None)
    updater = getattr(bl, "update_job", None)
    if not callable(getter) or not callable(updater):
        return
    current = getter(str(job_id)) or {}
    meta = _to_json_obj((current or {}).get("meta"))
    meta.update(dict(patch))
    updater(job_id, meta=meta)


def _mark_queue_delivery_terminal_job(
    bl,
    *,
    job_id: str,
    status: str,
    error: str,
) -> None:
    finisher = getattr(bl, "finish_job", None)
    if callable(finisher):
        finisher(job_id, status=status, error=error)
        return
    updater = getattr(bl, "update_job", None)
    if callable(updater):
        updater(job_id, status=status)
    update_runtime = getattr(bl, "update_job_runtime", None)
    if callable(update_runtime):
        update_runtime(job_id, heartbeat_now=True, last_error=error)


def _extract_retryable_interrupted_step_snapshot(bl, job_row: dict) -> dict | None:
    """Best-effort interrupted-step snapshot from logs + latest runtime fields."""
    job_id = str(job_row.get("id") or "").strip()
    snapshot: dict = {
        "detected_at": datetime.now(UTC).isoformat(),
        "job_id": job_id,
        "thread_id": str(job_row.get("thread_id") or "").strip(),
        "step_id": str(job_row.get("current_step_id") or "").strip() or None,
        "step_execution_id": None,
        "agent_id": str(job_row.get("current_agent") or "").strip() or None,
        "skill": None,
        "correlation_id": None,
        "message_id": None,
        "provider": None,
        "model_used": None,
        "prompt_version": None,
        "system_prompt": "",
        "user_prompt": "",
        "detection_source": "job_runtime_fields",
    }
    if not job_id or not bl:
        return None

    try:
        logs = bl.get_logs(job_id, since_id=0, limit=4000) or []
    except Exception:
        logs = []

    latest_prompt_request = None
    prompt_response_by_corr: dict[str, int] = {}
    latest_prompt_request_order = -1
    for row_order, row in enumerate(logs):
        if not isinstance(row, dict):
            continue
        action_type = str(row.get("action_type") or "").strip().lower()
        meta = _to_json_obj(row.get("meta"))
        corr = str(meta.get("correlation_id") or "").strip()
        if action_type == "prompt_request":
            latest_prompt_request = row
            latest_prompt_request_order = row_order
        elif action_type in {"prompt_response", "cli_response"} and corr:
            prompt_response_by_corr[corr] = max(prompt_response_by_corr.get(corr, -1), row_order)

    if isinstance(latest_prompt_request, dict):
        req_meta = _to_json_obj(latest_prompt_request.get("meta"))
        req_payload = _to_json_obj(latest_prompt_request.get("payload"))
        req_corr = str(req_meta.get("correlation_id") or "").strip()
        resp_order = prompt_response_by_corr.get(req_corr, -1) if req_corr else -1
        if resp_order <= latest_prompt_request_order:
            snapshot["detection_source"] = "prompt_request_without_response"
            snapshot["correlation_id"] = req_corr or None
            snapshot["agent_id"] = str(
                latest_prompt_request.get("agent") or ""
            ).strip() or snapshot.get("agent_id")
            snapshot["skill"] = str(req_meta.get("skill") or "").strip() or snapshot.get("skill")
            snapshot["step_id"] = str(req_meta.get("step_id") or "").strip() or snapshot.get(
                "step_id"
            )
            snapshot["step_execution_id"] = (
                str(req_meta.get("step_execution_id") or "").strip() or None
            )
            snapshot["provider"] = str(req_meta.get("provider") or "").strip().lower() or None
            snapshot["model_used"] = (
                str(latest_prompt_request.get("model_used") or "").strip() or None
            )
            snapshot["prompt_version"] = (
                str(req_payload.get("prompt_version") or "").strip() or None
            )
            snapshot["system_prompt"] = _truncate_text(req_payload.get("system_prompt"), 12000)
            snapshot["user_prompt"] = _truncate_text(req_payload.get("user_prompt"), 24000)

    latest_message = None
    try:
        if hasattr(bl, "get_latest_agent_message"):
            latest_message = bl.get_latest_agent_message(job_id)
    except Exception:
        latest_message = None
    if isinstance(latest_message, dict):
        message_id = str(latest_message.get("id") or "").strip() or None
        corr = str(latest_message.get("correlation_id") or "").strip()
        if not snapshot.get("message_id"):
            snapshot["message_id"] = message_id
        if not snapshot.get("correlation_id") and corr:
            snapshot["correlation_id"] = corr
        if not snapshot.get("skill"):
            snapshot["skill"] = str(latest_message.get("skill") or "").strip() or None
        if not snapshot.get("step_id"):
            snapshot["step_id"] = str(latest_message.get("step_id") or "").strip() or None
        if not snapshot.get("step_execution_id") and str(
            latest_message.get("step_id") or ""
        ).strip() == str(snapshot.get("step_id") or ""):
            snapshot["step_execution_id"] = (
                str(latest_message.get("step_execution_id") or "").strip() or None
            )
        if not snapshot.get("agent_id"):
            snapshot["agent_id"] = str(latest_message.get("from_agent") or "").strip() or None

    if not str(snapshot.get("agent_id") or "").strip():
        return None
    if not str(snapshot.get("skill") or "").strip():
        return None
    return snapshot


def _mark_retryable_interrupted_step(bl, job_id: str, job_row: dict, snapshot: dict | None) -> bool:
    if not bl or not str(job_id or "").strip():
        return False
    if not isinstance(snapshot, dict):
        return False

    meta = _to_json_obj((job_row or {}).get("meta"))
    meta["retryable_interrupted_step"] = dict(snapshot or {})
    journal = meta.get("runtime_journal")
    if not isinstance(journal, list):
        journal = []
    journal.append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": _RETRYABLE_INTERRUPTED_STEP_STATUS,
            "reason": "Worker restart detected an interrupted in-flight step; manual retry required.",
            "detail": {
                "detection_source": snapshot.get("detection_source"),
                "agent_id": snapshot.get("agent_id"),
                "step_id": snapshot.get("step_id"),
                "step_execution_id": snapshot.get("step_execution_id"),
                "skill": snapshot.get("skill"),
                "correlation_id": snapshot.get("correlation_id"),
                "message_id": snapshot.get("message_id"),
            },
        }
    )
    if len(journal) > 100:
        journal = journal[-100:]
    meta["runtime_journal"] = journal

    bl.update_job(
        job_id,
        status=_RETRYABLE_INTERRUPTED_STEP_STATUS,
        worker_id="",
        meta=meta,
    )
    if hasattr(bl, "update_job_runtime"):
        bl.update_job_runtime(
            job_id,
            current_step_id=str(snapshot.get("step_id") or ""),
            current_group_id=(
                str((job_row or {}).get("current_group_id") or "").strip() or None
                if str((job_row or {}).get("current_step_id") or "").strip()
                == str(snapshot.get("step_id") or "").strip()
                else None
            ),
            current_agent=str(snapshot.get("agent_id") or ""),
            heartbeat_now=True,
            last_error="Interrupted in-flight step detected after worker restart. Use Retry Step.",
        )
    with contextlib.suppress(Exception):
        bl.log_event(
            job_id=job_id,
            phase="recovery",
            agent="worker",
            event_type=_RETRYABLE_INTERRUPTED_STEP_STATUS,
            title="Interrupted step is retryable",
            detail={
                "agent_id": snapshot.get("agent_id"),
                "step_id": snapshot.get("step_id"),
                "step_execution_id": snapshot.get("step_execution_id"),
                "skill": snapshot.get("skill"),
                "detection_source": snapshot.get("detection_source"),
            },
            severity="warn",
        )
    return True
