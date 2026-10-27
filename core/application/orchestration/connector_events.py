"""Connector execution identity, shared stores and progress/event delivery."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.application.orchestration.runtime_identity import runtime_identity_from_state

logger = logging.getLogger(__name__)


def _shared_logger():
    try:
        from core.infrastructure.db.platform_stores import PlatformIntegrationStore

        return PlatformIntegrationStore.get_shared_instance()
    except Exception:
        return None


def _shared_job_store():
    """The store that owns job events and the job heartbeat.

    The integration store `_shared_logger` returns has neither `log_event` nor
    `update_job_runtime`, so step events written through it were silently dropped.
    """
    try:
        from core.infrastructure.db.platform_stores import PlatformJobStore

        return PlatformJobStore.get_shared_instance()
    except Exception:
        return None


def _step_update_sink(
    *,
    progress: Callable[[dict[str, Any]], None] | None,
    outputs: Callable[[str, str], None] | None,
) -> Callable[[dict[str, Any]], None] | None:
    """Route an extension's updates: finished outputs to `outputs`, the rest to `progress`."""
    if progress is None and outputs is None:
        return None

    def sink(update: dict[str, Any]) -> None:
        ready = update.get("output_ready")
        if isinstance(ready, dict):
            if outputs is not None:
                outputs(str(ready.get("kind") or ""), str(ready.get("id") or ""))
            return
        if progress is not None:
            progress(update)

    return sink


def _job_event_logger(bl: Any) -> Any:
    return bl if bl is not None and hasattr(bl, "log_event") else _shared_job_store()


def _runtime_ids_from_state(state: dict[str, Any]) -> dict[str, Any]:
    context = runtime_identity_from_state(state).as_context()
    ref = state.get("external_effect_resolution_ref")
    if ref is not None:
        if not isinstance(ref, dict) or not ref:
            raise ValueError("external_effect_resolution_reference_invalid")
        context["external_effect_resolution_ref"] = dict(ref)
    return context


def _connector_tool_call_id(
    *,
    job_id: str,
    group_id: str,
    step_id: str,
    tool_id: str,
    function_name: str,
) -> str:
    return (
        f"workflow:{str(job_id).strip()}:{group_id or 'workflow'}:{step_id or 'step'}:"
        f"{str(tool_id or '').strip()}:{str(function_name or '').strip()}"
    )


def _emit_connector_tool_event(
    bl: Any,
    *,
    job_id: Any,
    group_id: str,
    step_id: str,
    tool_id: str,
    function_name: str,
    event_type: str,
    excerpt: str,
    is_error: bool | None = None,
    include_excerpt: bool = True,
) -> None:
    normalized_job_id = str(job_id or "").strip()
    bl = _job_event_logger(bl)
    if not normalized_job_id or bl is None or not hasattr(bl, "log_event"):
        return

    normalized_group_id = str(group_id or "").strip()
    normalized_step_id = str(step_id or "").strip()
    normalized_tool_id = str(tool_id or "").strip()
    normalized_function_name = str(function_name or "").strip() or "invoke"
    tool_call_id = _connector_tool_call_id(
        job_id=normalized_job_id,
        group_id=normalized_group_id,
        step_id=normalized_step_id,
        tool_id=normalized_tool_id,
        function_name=normalized_function_name,
    )
    detail = {
        "tool_name": normalized_tool_id,
        "tool_call_id": tool_call_id,
        "function_name": normalized_function_name,
        "source": "connector_step_runtime",
        # Lets Job Details attach the event to the step it belongs to.
        "step_id": normalized_step_id,
    }
    if event_type == "tool_call":
        if include_excerpt:
            detail["args_excerpt"] = str(excerpt or "")[:1000]
        title = f"Tool call: {normalized_tool_id}"
    else:
        if include_excerpt:
            detail["result_excerpt"] = str(excerpt or "")[:1200]
        detail["is_error"] = bool(is_error)
        title = f"Tool result: {normalized_tool_id}"

    try:
        bl.log_event(
            job_id=normalized_job_id,
            phase=normalized_step_id or "workflow",
            agent="workflow_runtime",
            event_type=event_type,
            title=title,
            detail=detail,
            severity="info",
        )
    except Exception as exc:
        logger.warning(
            "connector-step tool event persistence failed: job_id=%s tool=%s type=%s error=%s",
            normalized_job_id,
            normalized_tool_id,
            event_type,
            exc,
        )
