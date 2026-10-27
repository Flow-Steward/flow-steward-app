"""Queue-delivery retry policy and operator-visible queue health helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.runtime.worker.queue_names import (
    queue_dead_letter_queue_name as queue_dead_letter_queue_name,
)

DEFAULT_QUEUE_DELIVERY_RETRY_MAX_ATTEMPTS = 2
QUEUE_DELIVERY_SUPPORTED_POLICY_FIELDS = ("retry_max_attempts",)
QUEUE_DELIVERY_IGNORED_POLICY_FIELDS: dict[str, str] = {
    "retry_strategy": "Workflow/business retry classification does not control RabbitMQ delivery.",
    "retry_backoff_initial_seconds": (
        "Backoff timing is not applied to RabbitMQ delivery retry in the current "
        "per-project serial queue model."
    ),
    "retry_backoff_max_seconds": (
        "Backoff timing is not applied to RabbitMQ delivery retry in the current "
        "per-project serial queue model."
    ),
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def resolve_queue_delivery_retry_policy(project_policy: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(project_policy or {})
    retry_max_attempts = max(
        0,
        _coerce_int(
            source.get("retry_max_attempts"),
            DEFAULT_QUEUE_DELIVERY_RETRY_MAX_ATTEMPTS,
        ),
    )
    return {
        "retry_max_attempts": retry_max_attempts,
        "supported_fields": [
            {
                "field": "retry_max_attempts",
                "scope": "delivery_retry",
                "reason": "Maximum RabbitMQ redelivery retries before the job is dead-lettered.",
            }
        ],
        "ignored_fields": [
            {
                "field": field,
                "scope": "workflow_retry",
                "reason": reason,
            }
            for field, reason in QUEUE_DELIVERY_IGNORED_POLICY_FIELDS.items()
        ],
    }


def _base_queue_delivery_state(
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "state": "queued",
        "queue_name": str(queue_name or "").strip(),
        "dead_letter_queue_name": str(dead_letter_queue_name or "").strip(),
        "attempt_count": 0,
        "retry_count": 0,
        "delivery_succeeded": False,
        "dispatch_state": "not_started",
        "dispatch_attempted_at": None,
        "publisher_confirmed_at": None,
        "requires_intervention": False,
        "last_failure_class": None,
        "last_failure_code": None,
        "last_failure_message": None,
        "last_failure_at": None,
        "delivery_succeeded_at": None,
        "dead_lettered_at": None,
        "discarded_at": None,
        "last_disposition": "queued",
        "last_event_at": _now_iso(),
        "policy": resolve_queue_delivery_retry_policy(project_policy),
    }


def normalize_queue_delivery_state(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str = "",
    dead_letter_queue_name: str = "",
    project_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _json_obj((meta or {}).get("queue_delivery"))
    normalized_queue_name = (
        str(queue_name or "").strip() or str(payload.get("queue_name") or "").strip()
    )
    normalized_dead_letter_queue_name = (
        str(dead_letter_queue_name or "").strip()
        or str(payload.get("dead_letter_queue_name") or "").strip()
        or queue_dead_letter_queue_name(normalized_queue_name)
    )
    policy_source = project_policy or payload.get("policy")
    state = _base_queue_delivery_state(
        queue_name=normalized_queue_name,
        dead_letter_queue_name=normalized_dead_letter_queue_name,
        project_policy=policy_source if isinstance(policy_source, Mapping) else None,
    )
    dispatch_state = str(payload.get("dispatch_state") or "not_started").strip().lower()
    state.update(
        {
            "state": str(payload.get("state") or state["state"]).strip().lower() or "queued",
            "attempt_count": max(0, _coerce_int(payload.get("attempt_count"), 0)),
            "retry_count": max(0, _coerce_int(payload.get("retry_count"), 0)),
            "delivery_succeeded": bool(payload.get("delivery_succeeded")),
            "dispatch_state": dispatch_state,
            "dispatch_attempted_at": str(payload.get("dispatch_attempted_at") or "").strip()
            or None,
            "publisher_confirmed_at": str(payload.get("publisher_confirmed_at") or "").strip()
            or None,
            "requires_intervention": bool(payload.get("requires_intervention")),
            "last_failure_class": str(payload.get("last_failure_class") or "").strip() or None,
            "last_failure_code": str(payload.get("last_failure_code") or "").strip() or None,
            "last_failure_message": str(payload.get("last_failure_message") or "").strip() or None,
            "last_failure_at": str(payload.get("last_failure_at") or "").strip() or None,
            "delivery_succeeded_at": str(payload.get("delivery_succeeded_at") or "").strip()
            or None,
            "dead_lettered_at": str(payload.get("dead_lettered_at") or "").strip() or None,
            "discarded_at": str(payload.get("discarded_at") or "").strip() or None,
            "last_disposition": str(payload.get("last_disposition") or "").strip() or "queued",
            "last_event_at": str(payload.get("last_event_at") or "").strip()
            or state["last_event_at"],
        }
    )
    state["policy"] = resolve_queue_delivery_retry_policy(
        project_policy if isinstance(project_policy, Mapping) else payload.get("policy")
    )
    state["queue_name"] = normalized_queue_name
    state["dead_letter_queue_name"] = normalized_dead_letter_queue_name
    return state


def build_initial_queue_delivery_state(
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return normalize_queue_delivery_state(
        {},
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )


def append_runtime_journal_entry(
    meta: Mapping[str, Any] | None,
    *,
    event: str,
    reason: str,
    detail: dict[str, Any] | None = None,
    max_entries: int = 200,
) -> dict[str, Any]:
    normalized_meta = dict(meta or {})
    journal = normalized_meta.get("runtime_journal")
    if not isinstance(journal, list):
        journal = []
    journal.append(
        {
            "timestamp": _now_iso(),
            "event": str(event or "").strip() or "queue_delivery",
            "reason": str(reason or "").strip(),
            "detail": dict(detail or {}),
        }
    )
    if len(journal) > max(1, int(max_entries or 200)):
        journal = journal[-max_entries:]
    normalized_meta["runtime_journal"] = journal
    return normalized_meta


def _with_queue_delivery_state(
    meta: Mapping[str, Any] | None, state: dict[str, Any]
) -> dict[str, Any]:
    normalized_meta = dict(meta or {})
    normalized_meta["queue_delivery"] = dict(state)
    return normalized_meta


def record_queue_delivery_publish_attempted(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist the point after which publisher outcome is no longer safely inferable."""
    state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )
    now_iso = _now_iso()
    state["dispatch_state"] = "attempted"
    state["dispatch_attempted_at"] = now_iso
    state["last_disposition"] = "publish_attempted"
    state["last_event_at"] = now_iso
    updated_meta = append_runtime_journal_entry(
        _with_queue_delivery_state(meta, state),
        event="queue_publish_attempted",
        reason="Durable queue publish attempt started before RabbitMQ confirmation.",
        detail={"queue_name": state["queue_name"]},
    )
    return _with_queue_delivery_state(updated_meta, state), state


def record_queue_delivery_publish_not_sent(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Restore the pre-dispatch state after setup fails before any publish call."""
    state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )
    now_iso = _now_iso()
    state["dispatch_state"] = "not_started"
    state.pop("dispatch_attempted_at", None)
    state["last_disposition"] = "publish_not_sent"
    state["last_event_at"] = now_iso
    updated_meta = append_runtime_journal_entry(
        _with_queue_delivery_state(meta, state),
        event="queue_publish_not_sent",
        reason="RabbitMQ setup failed before a project queue message was published.",
        detail={"queue_name": state["queue_name"]},
    )
    return _with_queue_delivery_state(updated_meta, state), state


def record_queue_delivery_publisher_confirmed(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist RabbitMQ publisher confirmation without conflating it with worker claim."""
    state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )
    now_iso = _now_iso()
    state["dispatch_state"] = "publisher_confirmed"
    state["publisher_confirmed_at"] = now_iso
    state["last_disposition"] = "publisher_confirmed"
    state["last_event_at"] = now_iso
    updated_meta = append_runtime_journal_entry(
        _with_queue_delivery_state(meta, state),
        event="queue_publisher_confirmed",
        reason="RabbitMQ confirmed the durable project queue publication.",
        detail={"queue_name": state["queue_name"]},
    )
    return _with_queue_delivery_state(updated_meta, state), state


def record_queue_delivery_claimed(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )
    now_iso = _now_iso()
    state["state"] = "delivery_succeeded"
    state["attempt_count"] = max(1, int(state.get("attempt_count") or 0) + 1)
    state["delivery_succeeded"] = True
    state["requires_intervention"] = False
    state["delivery_succeeded_at"] = now_iso
    state["last_disposition"] = "claimed"
    state["last_event_at"] = now_iso
    updated_meta = append_runtime_journal_entry(
        _with_queue_delivery_state(meta, state),
        event="queue_delivery_claimed",
        reason="Worker claimed the queued job for execution.",
        detail={
            "attempt_count": state["attempt_count"],
            "retry_count": state["retry_count"],
            "queue_name": state["queue_name"],
        },
    )
    return _with_queue_delivery_state(updated_meta, state), state


def record_queue_delivery_failure(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
    failure_class: str,
    failure_code: str,
    failure_message: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )
    now_iso = _now_iso()
    state["attempt_count"] = max(1, int(state.get("attempt_count") or 0) + 1)
    state["last_failure_class"] = str(failure_class or "").strip() or None
    state["last_failure_code"] = str(failure_code or "").strip() or None
    state["last_failure_message"] = str(failure_message or "").strip() or None
    state["last_failure_at"] = now_iso
    retry_max_attempts = int((state.get("policy") or {}).get("retry_max_attempts") or 0)
    disposition = "retry"
    if int(state.get("retry_count") or 0) < retry_max_attempts:
        state["retry_count"] = int(state.get("retry_count") or 0) + 1
        state["state"] = "retrying_delivery"
        state["requires_intervention"] = False
        state["last_disposition"] = "retry"
        journal_event = "queue_delivery_retry"
        journal_reason = (
            "RabbitMQ delivery failed before execution and will retry "
            f"({state['retry_count']}/{retry_max_attempts})."
        )
    else:
        state["state"] = "dead_lettered"
        state["requires_intervention"] = True
        state["dead_lettered_at"] = now_iso
        state["last_disposition"] = "dead_letter"
        disposition = "dead_letter"
        journal_event = "queue_dead_lettered"
        journal_reason = (
            "RabbitMQ delivery retries were exhausted; operator intervention is required."
        )
    state["last_event_at"] = now_iso
    updated_meta = append_runtime_journal_entry(
        _with_queue_delivery_state(meta, state),
        event=journal_event,
        reason=journal_reason,
        detail={
            "attempt_count": state["attempt_count"],
            "retry_count": state["retry_count"],
            "retry_max_attempts": retry_max_attempts,
            "failure_class": state["last_failure_class"],
            "failure_code": state["last_failure_code"],
            "failure_message": state["last_failure_message"],
            "queue_name": state["queue_name"],
            "dead_letter_queue_name": state["dead_letter_queue_name"],
        },
    )
    return _with_queue_delivery_state(updated_meta, state), state, disposition


def record_queue_delivery_terminal_failure(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
    failure_class: str,
    failure_code: str,
    failure_message: str,
    count_attempt: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )
    now_iso = _now_iso()
    if count_attempt:
        state["attempt_count"] = max(1, int(state.get("attempt_count") or 0) + 1)
    state["state"] = "dead_lettered"
    state["requires_intervention"] = True
    state["last_failure_class"] = str(failure_class or "").strip() or None
    state["last_failure_code"] = str(failure_code or "").strip() or None
    state["last_failure_message"] = str(failure_message or "").strip() or None
    state["last_failure_at"] = now_iso
    state["dead_lettered_at"] = now_iso
    state["last_disposition"] = "dead_letter"
    state["last_event_at"] = now_iso
    updated_meta = append_runtime_journal_entry(
        _with_queue_delivery_state(meta, state),
        event="queue_dead_lettered",
        reason="RabbitMQ delivery entered a terminal queue failure state.",
        detail={
            "attempt_count": state["attempt_count"],
            "failure_class": state["last_failure_class"],
            "failure_code": state["last_failure_code"],
            "failure_message": state["last_failure_message"],
            "queue_name": state["queue_name"],
            "dead_letter_queue_name": state["dead_letter_queue_name"],
        },
    )
    return _with_queue_delivery_state(updated_meta, state), state


def record_queue_delivery_discard(
    meta: Mapping[str, Any] | None,
    *,
    queue_name: str,
    dead_letter_queue_name: str,
    project_policy: Mapping[str, Any] | None,
    failure_code: str,
    failure_message: str,
    count_attempt: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = normalize_queue_delivery_state(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=dead_letter_queue_name,
        project_policy=project_policy,
    )
    now_iso = _now_iso()
    if count_attempt:
        state["attempt_count"] = max(1, int(state.get("attempt_count") or 0) + 1)
    state["state"] = "discarded"
    state["requires_intervention"] = False
    state["last_failure_code"] = str(failure_code or "").strip() or None
    state["last_failure_message"] = str(failure_message or "").strip() or None
    state["discarded_at"] = now_iso
    state["last_disposition"] = "discard"
    state["last_event_at"] = now_iso
    updated_meta = append_runtime_journal_entry(
        _with_queue_delivery_state(meta, state),
        event="queue_delivery_discarded",
        reason="Queued message was discarded without another RabbitMQ delivery retry.",
        detail={
            "attempt_count": state["attempt_count"],
            "failure_code": state["last_failure_code"],
            "failure_message": state["last_failure_message"],
            "queue_name": state["queue_name"],
        },
    )
    return _with_queue_delivery_state(updated_meta, state), state
