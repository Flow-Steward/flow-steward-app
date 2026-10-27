"""Host-owned retry of one failed operation inside the current step execution.

Retry authority comes only from the validated ``operation_retry`` channel of
the step's own result, never from model-written artifacts. The budget belongs
to the logical step execution (and its loop item or lane), so a technical retry
keeps counting while a rework re-entry, which is a new execution, starts over.
The decision itself is the existing ``retry_policy.decide_retry`` algorithm.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.active_step import ActiveStep
from core.application.workflows.workflow_step_kinds import (
    normalize_step_kind,
    runtime_actor_for_step,
)

ACTION_RETRY_OPERATION = "retry_operation"
ACTION_ESCALATE = "escalate_to_human"

_OPERATION_RETRY_MARKERS = frozenset(
    {
        "operation_kind",
        "request_semantics",
        "external_effect_status",
        "definitely_no_external_effect",
        "retry_after_seconds",
        "provider_error_code",
        "http_status",
    }
)


@dataclass(frozen=True)
class OperationRetryDecision:
    action: str
    reason: str
    identity: str = ""
    retry_number: int = 0
    deadline_epoch_seconds: float = 0.0
    delay_seconds: float = 0.0
    reason_code: str = ""
    failure_class: str = ""
    signal: dict[str, Any] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)
    lane_id: str = ""
    position: int = -1
    target_actor: str = ""


def host_operation_retry_signal(message: Mapping[str, Any]) -> dict[str, Any]:
    """The retry signal from the validated host-owned channel, or nothing."""
    envelope = message.get("operation_retry")
    if not isinstance(envelope, Mapping):
        return {}
    signal = envelope.get("signal")
    if not isinstance(signal, Mapping):
        return {}
    projected = dict(signal)
    for source_key, target_key in (
        ("deadline_epoch_seconds", "operation_deadline_epoch_seconds"),
        ("operation_loop", "operation_loop"),
        ("operation_parallel", "operation_parallel"),
    ):
        value = envelope.get(source_key)
        if value not in (None, "", {}):
            projected[target_key] = value
    projected["_retry_provenance"] = str(envelope.get("provenance") or "")
    return projected


def _host_executor_message(current: ActiveStep, message: Mapping[str, Any]) -> bool:
    return (
        type(message.get("attempt")) is int
        and message.get("attempt") == current.attempt
        and message.get("step_id") == current.step_id
        and message.get("step_execution_id") == current.step_execution_id
        and message.get("from_agent") == runtime_actor_for_step(current.step)
    )


def trusted_operation_retry_signal(
    current: ActiveStep, message: Mapping[str, Any]
) -> dict[str, Any]:
    """Accept host facts only from this pinned executor and accepted execution."""
    if not _host_executor_message(current, message):
        return {}
    kind = normalize_step_kind(current.step_kind, default="")
    provenance = {
        "connector": "deterministic_connector_step",
        "http_request": "deterministic_http_step",
        "agent": "agent_managed_custom_http",
    }.get(kind)
    if not provenance:
        return {}
    if kind != "agent" and message.get("skill") not in {kind, "operation_retry_wait"}:
        return {}
    envelope = message.get("operation_retry")
    if isinstance(envelope, Mapping):
        if envelope.get("provenance") != provenance or (
            kind != "agent" and envelope.get("step_id") != current.step_id
        ):
            return {}
        signal = host_operation_retry_signal(message)
        return signal if _OPERATION_RETRY_MARKERS.intersection(signal) else {}
    # The deterministic worker can reject an expired checkpoint before invoking
    # a provider. Its fixed host error cannot be supplied by an agent's artifacts.
    artifacts = message.get("artifacts")
    if (
        kind != "agent"
        and message.get("skill") in {kind, "operation_retry_wait"}
        and isinstance(artifacts, Mapping)
        and artifacts.get("step_kind") == kind
        and artifacts.get("error") == "retry_deadline_exhausted"
    ):
        return {
            "retryable": False,
            "failure_class": "transient",
            "external_effect_status": "failed",
            "_retry_provenance": provenance,
        }
    return {}


def has_unknown_external_effect(current: ActiveStep, message: Mapping[str, Any]) -> bool:
    """Classify ambiguity without granting effect resolution or retry authority."""
    if (
        trusted_operation_retry_signal(current, message).get("external_effect_status")
        == "timeout_unknown"
    ):
        return True
    if (
        current.step_kind != "connector"
        or not _host_executor_message(current, message)
        or message.get("skill") != "connector"
    ):
        return False
    artifacts = message.get("artifacts")
    if not isinstance(artifacts, Mapping) or artifacts.get("step_kind") != "connector":
        return False
    observations = artifacts.get("external_effect_observations")
    return isinstance(observations, list) and any(
        isinstance(value, Mapping)
        and value.get("step_execution_id") == current.step_execution_id
        and type(value.get("attempt")) is int
        and value.get("attempt") == current.attempt
        and type(value.get("claim_version")) is int
        and value["claim_version"] > 0
        and bool(str(value.get("effect_id") or "").strip())
        for value in observations
    )


def operation_retry_identity(
    state: Mapping[str, Any], current: ActiveStep, signal: Mapping[str, Any]
) -> str:
    loop = signal.get("operation_loop")
    parallel = signal.get("operation_parallel")
    material = {
        "job_id": str(state.get("job_id") or ""),
        "workflow_id": str(state.get("workflow_id") or ""),
        "step_id": current.step_id,
        "step_execution_id": current.step_execution_id,
        "loop": dict(loop) if isinstance(loop, Mapping) else {},
        "parallel": dict(parallel) if isinstance(parallel, Mapping) else {},
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"operation:{digest}"


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def decide_operation_retry(
    state: Mapping[str, Any],
    current: ActiveStep,
    message: Mapping[str, Any],
    *,
    configured_retries: int = 0,
    now: float | None = None,
) -> OperationRetryDecision | None:
    """Apply technical retry fences, or defer an ordinary failure to authored policy."""
    from core.application.orchestration.retry_policy import (
        decide_retry,
        normalize_retry_signal,
        policy_from_state,
    )

    raw_signal = trusted_operation_retry_signal(current, message)
    if not _OPERATION_RETRY_MARKERS.intersection(raw_signal):
        return None
    artifacts = message.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    if str(artifacts.get("retry_reason_code") or "") == "nested_operation_retry_unsupported":
        return OperationRetryDecision(
            action=ACTION_ESCALATE,
            reason="nested_operation_retry_unsupported",
            reason_code="nested_operation_retry_unsupported",
        )
    signal = normalize_retry_signal(raw_signal)
    provenance = str(raw_signal.get("_retry_provenance") or "")
    if provenance == "agent_managed_custom_http":
        return OperationRetryDecision(
            action=ACTION_ESCALATE,
            reason="agent_managed_tool_retry_unsupported",
            reason_code="agent_managed_tool_retry_unsupported",
            signal=signal,
            telemetry={
                "reason_code": "agent_managed_tool_retry_unsupported",
                "provenance": provenance,
            },
        )

    policy = policy_from_state(state, configured_retries=max(2, int(configured_retries or 0)))
    identity = operation_retry_identity(state, current, raw_signal)
    rows = state.get("operation_retry_state")
    row = rows.get(identity) if isinstance(rows, Mapping) else None
    row = row if isinstance(row, Mapping) else {}
    try:
        completed = max(0, int(row.get("retry_count") or 0))
    except (TypeError, ValueError):
        completed = 0
    clock = time.time() if now is None else now
    deadline = _float(row.get("deadline_epoch_seconds"))
    signal_deadline = _float(raw_signal.get("operation_deadline_epoch_seconds"))
    if signal_deadline > 0:
        deadline = min(deadline, signal_deadline) if deadline > 0 else signal_deadline
    if deadline <= 0:
        deadline = clock + policy.operation_deadline_seconds
    decision = decide_retry(
        signal=signal,
        retry_number=completed + 1,
        policy=policy,
        remaining_deadline_seconds=max(0.0, deadline - clock),
        rng=random.random,
    )
    # Preserve the host's refusal even for a safe read: authored step retry
    # must not reset a refused logical operation. The caller selects its policy.
    for key in ("operation_loop", "operation_parallel"):
        value = raw_signal.get(key)
        if isinstance(value, Mapping):
            signal[key] = dict(value)
    parallel = raw_signal.get("operation_parallel")
    parallel = parallel if isinstance(parallel, Mapping) else {}
    try:
        position = int(str(parallel.get("position")))
    except (TypeError, ValueError):
        position = -1
    return OperationRetryDecision(
        action=ACTION_RETRY_OPERATION if decision.should_retry else ACTION_ESCALATE,
        reason=decision.reason_code,
        identity=identity,
        retry_number=decision.retry_number,
        deadline_epoch_seconds=deadline,
        delay_seconds=float(decision.applied_delay_seconds or 0.0),
        reason_code=decision.reason_code,
        failure_class=str(decision.failure_class or signal.get("failure_class") or ""),
        signal=signal,
        telemetry=decision.telemetry(),
        lane_id=str(parallel.get("lane_id") or ""),
        position=position,
        target_actor=str(message.get("from_agent") or ""),
    )


__all__ = [
    "ACTION_ESCALATE",
    "ACTION_RETRY_OPERATION",
    "OperationRetryDecision",
    "decide_operation_retry",
    "has_unknown_external_effect",
    "host_operation_retry_signal",
    "operation_retry_identity",
    "trusted_operation_retry_signal",
]
