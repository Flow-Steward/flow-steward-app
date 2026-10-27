"""Checkpointed wait boundary for one provider-neutral operation retry."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from langgraph.types import Command

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState


def _sleep_with_runtime_heartbeat(state: dict[str, Any], delay_seconds: float) -> None:
    """Keep watchdog-visible runtime truth fresh while retry backoff is active."""
    from core.application.orchestration.graph.nodes_agent_runtime import (
        _emit_agent_heartbeat,
        _heartbeat_interval_seconds,
    )

    remaining = max(0.0, float(delay_seconds))
    heartbeat_interval = max(0.1, float(_heartbeat_interval_seconds()))
    while remaining > 0:
        _emit_agent_heartbeat(
            cast("OrchestratorState", state),
            agent_id="operation_retry_wait",
        )
        chunk = min(remaining, heartbeat_interval)
        time.sleep(chunk)
        remaining -= chunk
    _emit_agent_heartbeat(
        cast("OrchestratorState", state),
        agent_id="operation_retry_wait",
    )


def _deadline_failure(state: dict[str, Any], dispatch: dict[str, Any]) -> Command:
    from core.application.orchestration.active_step import active_step, step_message_fields
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.common import publish_agent_message

    timestamp = datetime.now(UTC).isoformat()
    retry_signal = dispatch.get("retry_signal")
    artifacts = dict(retry_signal) if isinstance(retry_signal, dict) else {}
    artifacts.update(
        {
            "error": "retry_deadline_exhausted",
            "step_id": str(dispatch.get("step_id") or ""),
            "failure_class": str(artifacts.get("failure_class") or "transient"),
            "retryable": False,
            "external_effect_status": "failed",
        }
    )
    current = active_step(state)
    envelope = None
    if (
        current is not None
        and dispatch.get("step_id") == current.step_id
        and dispatch.get("step_execution_id") == current.step_execution_id
        and type(dispatch.get("attempt")) is int
        and dispatch.get("attempt") == current.attempt
    ):
        provenance = {
            "connector": "deterministic_connector_step",
            "http_request": "deterministic_http_step",
        }.get(current.step_kind)
        if provenance:
            envelope = {
                "schema_version": "operation_retry_signal_v1",
                "provenance": provenance,
                "step_id": current.step_id,
                "signal": artifacts,
                **(
                    {"deadline_epoch_seconds": dispatch["deadline_epoch_seconds"]}
                    if type(dispatch.get("deadline_epoch_seconds")) in (int, float)
                    and dispatch["deadline_epoch_seconds"] > 0
                    else {}
                ),
            }
            for key in ("operation_loop", "operation_parallel"):
                if isinstance(artifacts.get(key), dict):
                    envelope[key] = dict(artifacts[key])
    message = build_agent_message(
        from_agent=str(dispatch.get("target_actor") or "operation_retry_wait"),
        to_agent="orchestrator",
        # The cursor already names the retried attempt this dispatch was for.
        **step_message_fields(state),
        skill="operation_retry_wait",
        status="fail",
        correlation_id=uuid.uuid4().hex,
        caused_by=[],
        artifacts=artifacts,
        operation_retry=envelope,
        summary="Operation retry deadline elapsed before invocation",
        errors=["retry_deadline_exhausted"],
        started_at=timestamp,
        completed_at=timestamp,
        published_at=timestamp,
    )
    publish_agent_message(state, message)
    return Command(
        update={
            "agent_messages": [*(state.get("agent_messages") or []), message],
            "operation_retry_dispatch": None,
            "retry_backoff_seconds": 0,
        },
        goto="orchestrator",
    )


def operation_retry_wait_node(state: OrchestratorState) -> Command:
    """Sleep only after retry identity/deadline have been checkpointed."""
    state_dict = dict(state or {})
    dispatch = state_dict.get("operation_retry_dispatch")
    if not isinstance(dispatch, dict):
        return Command(update={}, goto="orchestrator")
    try:
        deadline_epoch = float(dispatch.get("deadline_epoch_seconds") or 0.0)
        delay_seconds = max(0.0, float(dispatch.get("delay_seconds") or 0.0))
    except (TypeError, ValueError):
        return _deadline_failure(state_dict, dispatch)
    remaining = deadline_epoch - time.time()
    if deadline_epoch <= 0 or remaining <= 0 or delay_seconds > remaining:
        return _deadline_failure(state_dict, dispatch)
    if delay_seconds > 0:
        _sleep_with_runtime_heartbeat(state_dict, delay_seconds)
    if time.time() >= deadline_epoch:
        return _deadline_failure(state_dict, dispatch)
    target = str(dispatch.get("target") or "").strip()
    if not target:
        return _deadline_failure(state_dict, dispatch)
    return Command(update={"retry_backoff_seconds": delay_seconds}, goto=target)


__all__ = ["operation_retry_wait_node"]
