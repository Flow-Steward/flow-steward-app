"""Interrupt and approval helper functions extracted from serve.py."""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def is_graph_interrupt_trace(text: object | None) -> bool:
    if text is None:
        return False
    lowered = str(text).lower()
    return "graphinterrupt" in lowered and "interrupt(value=" in lowered


def extract_interrupt_payload_from_text(text: object | None) -> dict | None:
    """Parse Interrupt(value={...}) payload from traceback/log text."""
    if text is None:
        return None
    err_str = str(text)
    marker = "Interrupt(value="
    idx = err_str.find(marker)
    if idx == -1:
        return None
    start = err_str.find("{", idx)
    if start == -1:
        return None
    depth = 0
    in_quote: str | None = None
    escaped = False
    for pos in range(start, len(err_str)):
        ch = err_str[pos]
        if in_quote is not None:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == in_quote:
                in_quote = None
            continue
        if ch in {"'", '"'}:
            in_quote = ch
            escaped = False
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                candidate = err_str[start : pos + 1]
                try:
                    payload = ast.literal_eval(candidate)
                    if isinstance(payload, dict):
                        return dict(payload)
                except Exception:
                    return None
    return None


def ws_get_approval_payload(
    job: dict,
    *,
    get_thread_state_snapshot: Any,
    state_based_approval_payload: Callable[[dict[str, Any]], dict[str, Any] | None],
    get_logger_only: Any,
    logger: Any,
) -> dict | None:
    """Read the current LangGraph interrupt payload for a job (best-effort)."""
    thread_id = job.get("thread_id")
    if not thread_id:
        return None
    try:
        state = get_thread_state_snapshot(thread_id)
        interrupts = state.tasks[0].interrupts if state and state.tasks else []
        if interrupts:
            interrupt = interrupts[0]
            payload = dict(interrupt.value) if isinstance(interrupt.value, dict) else None
            if payload is not None:
                interrupt_id = getattr(interrupt, "id", None) or getattr(
                    interrupt, "interrupt_id", None
                )
                if interrupt_id:
                    payload["interrupt_id"] = str(interrupt_id)
                    if not payload.get("approval_id"):
                        payload["approval_id"] = str(interrupt_id)
            return payload
        payload = state_based_approval_payload(getattr(state, "values", {}) or {})
        if payload:
            return payload
    except Exception:
        pass
    try:
        bl = get_logger_only()
        if bl:
            with bl.conn.cursor() as cur:
                cur.execute(
                    """SELECT COALESCE(error, result::text) AS interrupt_text
                       FROM platform_action_log
                       WHERE job_id = %s
                         AND action_type IN ('node_error', 'node_interrupt')
                         AND (
                            error LIKE '%%Interrupt%%'
                            OR result::text LIKE '%%Interrupt%%'
                         )
                       ORDER BY timestamp DESC, id DESC LIMIT 1""",
                    (job["id"],),
                )
                row = cur.fetchone()
            if row and row[0]:
                payload = extract_interrupt_payload_from_text(row[0])
                if payload:
                    return payload
    except Exception as fb_err:
        logger.debug("_ws_get_approval_payload fallback failed: %s", fb_err)
    return None


def approval_fallback_payload(job: dict, *, to_json_obj: Any, get_logger_only: Any) -> dict:
    """Best-effort approval payload when interrupt payload is unavailable."""

    def _approval_stage_label(step_id: str) -> str:
        normalized = str(step_id or "").strip()
        if not normalized:
            return "Review"
        return " ".join(part for part in normalized.replace("-", "_").split("_") if part).title()

    def _approval_message_content(message: dict | None, task_summary: str) -> str:
        if not isinstance(message, dict):
            return ""
        artifacts = to_json_obj(message.get("artifacts")) or {}
        if not isinstance(artifacts, dict):
            artifacts = {}
        candidates: list[Any] = []
        for key, value in artifacts.items():
            if str(key).startswith("_") or key == "raw_output":
                continue
            candidates.append(value)
        candidates.append(message.get("summary"))
        candidates.append(artifacts.get("raw_output"))
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and text != task_summary:
                return text
        for candidate in artifacts.values():
            if not isinstance(candidate, list):
                continue
            items = [str(item).strip() for item in candidate if str(item).strip()]
            if items:
                return "\n".join(f"- {item}" for item in items[:10])
        return ""

    task_summary = str(job.get("task_summary") or "").strip()
    content = str(job.get("last_response_excerpt") or "").strip()
    latest_message = None
    if not content or content == task_summary:
        bl = get_logger_only()
        job_id = str(job.get("id") or "").strip()
        if bl and job_id and hasattr(bl, "get_agent_messages"):
            try:
                message_rows = bl.get_agent_messages(job_id, limit=20, newest=True) or []
                if message_rows:
                    latest_message = message_rows[-1]
            except Exception:
                latest_message = None
        content = _approval_message_content(latest_message, task_summary)
    if not content:
        content = "Approval details are loading. If this persists, refresh the page."
    return {
        "stage": _approval_stage_label(
            str((latest_message or {}).get("step_id") or job.get("current_step_id") or "")
        ),
        "message": "Awaiting your approval.",
        "content": content,
    }


def build_approval_entry(
    interrupt_payload: dict,
    approved: bool,
    action: str | None,
    comment: str,
    override_content: str = "",
    actor_username: str = "operator",
    timestamp: str | None = None,
) -> dict:
    return {
        "stage": interrupt_payload.get("stage", ""),
        "question": interrupt_payload.get("message", ""),
        "content_preview": str(interrupt_payload.get("content", ""))[:200],
        "approved": approved,
        "action": action,
        "comment": comment.strip(),
        "override_content_preview": str(override_content or "").strip()[:200],
        "actor": str(actor_username or "operator").strip() or "operator",
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }


def same_approval_decision(left: dict, right: dict) -> bool:
    keys = (
        "stage",
        "question",
        "content_preview",
        "approved",
        "action",
        "comment",
        "override_content_preview",
        "actor",
    )
    return all(left.get(key) == right.get(key) for key in keys)


def append_approval_history(approvals: list[dict], entry: dict) -> list[dict]:
    try:
        resume_revision = int(entry.get("resume_revision") or 0)
    except (TypeError, ValueError):
        resume_revision = 0
    if resume_revision > 0 and any(
        int(item.get("resume_revision") or 0) == resume_revision
        for item in approvals
        if isinstance(item, dict)
    ):
        return approvals
    if approvals and same_approval_decision(approvals[-1], entry):
        merged = dict(approvals[-1])
        merged["repeat_count"] = int(merged.get("repeat_count") or 1) + 1
        merged["timestamp"] = entry["timestamp"]
        if resume_revision > 0:
            merged["resume_revision"] = resume_revision
        approvals[-1] = merged
        return approvals

    fresh_entry = dict(entry)
    fresh_entry.setdefault("repeat_count", 1)
    approvals.append(fresh_entry)
    return approvals


__all__ = [
    "append_approval_history",
    "approval_fallback_payload",
    "extract_interrupt_payload_from_text",
    "is_graph_interrupt_trace",
    "same_approval_decision",
    "ws_get_approval_payload",
]
