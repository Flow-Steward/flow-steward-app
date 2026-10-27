from __future__ import annotations

import uuid
from typing import Any

_SUPPORTED_ACTIONS = {"approve", "reject", "abort"}
_TERMINAL_OUTCOME_CODES = {"already_handled", "not_allowed", "stale"}
_APPROVAL_CONFLICT_OUTCOME_CODES = {
    "approval_action_not_allowed": "not_allowed",
    "approval_already_resolved": "already_handled",
    "stale_approval_gate": "stale",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def approval_conflict_outcome_code(conflict_code: str) -> str:
    return _APPROVAL_CONFLICT_OUTCOME_CODES.get(_text(conflict_code), "")


def is_approval_decision(event_type: str, payload: dict[str, Any]) -> bool:
    return (
        _text(event_type) == "notification.interaction_received"
        and _text(payload.get("interaction_kind")) == "approval_decision"
        and bool(_text(payload.get("approval_id")))
        and _text(payload.get("action")).lower() in _SUPPORTED_ACTIONS
    )


# What an accepted decision does next, by the kind of gate the host decided.
# An authored Approval follows its approved/rejected edge; a recovery or
# autonomy pause continues on approve and reruns the step on reject. When the
# host did not report the kind, a reject promises nothing beyond "rejected".
_NEXT_TRANSITIONS = {
    "authored": {"approve": "approved_outcome", "reject": "rejected_outcome"},
    "recovery": {"approve": "continue", "reject": "rework_or_retry"},
    "autonomy": {"approve": "continue", "reject": "rework_or_retry"},
}


def _next_transition(action: str, approval_reason: str) -> str:
    if action == "abort":
        return "stopped"
    by_reason = _NEXT_TRANSITIONS.get(approval_reason)
    if by_reason is not None:
        return by_reason[action]
    return "continue" if action == "approve" else "rejected"


def build_approval_resolution(
    *,
    event_type: str,
    source: str,
    payload: dict[str, Any],
    reactions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build a provider-neutral terminal approval outcome from reaction results.

    ``None`` means the reaction is not a supported approval decision or its
    result is transient/unknown and therefore must remain retryable.
    """

    if not is_approval_decision(event_type, payload):
        return None
    action = _text(payload.get("action")).lower()

    normalized_reactions = [row for row in reactions if isinstance(row, dict)]
    if not normalized_reactions:
        return None
    applied = any(bool(row.get("executed")) for row in normalized_reactions)
    if not applied:
        terminal_reaction = next(
            (
                row
                for row in normalized_reactions
                if _text(row.get("outcome_code")).lower() in _TERMINAL_OUTCOME_CODES
            ),
            None,
        )
        if terminal_reaction is None:
            return None
        authoritative = terminal_reaction
    else:
        authoritative = next(row for row in normalized_reactions if bool(row.get("executed")))
    reason = "" if applied else _text(authoritative.get("reason") or authoritative.get("error"))
    provider_key = _text(source).lower()
    reply_target = payload.get("reply_target")
    if not isinstance(reply_target, dict):
        reply_target = {}

    next_transition = (
        _next_transition(action, _text(authoritative.get("approval_reason")).lower())
        if applied
        else "none"
    )

    result = {
        "approval_id": _text(payload.get("approval_id")),
        "action": action,
        "accepted": applied,
        "applied": applied,
        "outcome_code": (
            "applied" if applied else _text(authoritative.get("outcome_code")).lower()
        ),
        "reason": reason,
        "job_status": _text(authoritative.get("job_status") or authoritative.get("status")),
        "next_transition": next_transition,
        "comment": _text(payload.get("comment")) if action == "reject" else "",
        "reply_target": {
            "provider_key": provider_key,
            "channel_id": _text(reply_target.get("channel_id")),
            "external_conversation_ref": _text(reply_target.get("external_conversation_ref")),
            "external_message_ref": _text(reply_target.get("external_message_ref")),
        },
    }
    for label in ("task_summary", "workflow_label", "group_label", "for_each_label", "step_label"):
        value = _text(payload.get(label))
        if value:
            result[label] = value
    return result


def approval_resolution_direct_route(outcome: dict[str, Any]) -> dict[str, Any] | None:
    reply_target = outcome.get("reply_target")
    if not isinstance(reply_target, dict):
        return None
    provider_key = _text(reply_target.get("provider_key")).lower()
    channel_id = _text(reply_target.get("channel_id"))
    if not provider_key or not channel_id:
        return None
    return {
        "route_key": f"extension_channel:{provider_key}:{channel_id}",
        "display_name": f"{provider_key}:{channel_id}",
        "provider_key": provider_key,
        "implementation_kind": "extension",
        "enabled": True,
        "event_type": "approval.resolved",
        "severity": "info" if bool(outcome.get("applied")) else "warning",
        "route_config": {
            "channel_id": channel_id,
            "required_capability": "human_decision",
        },
    }


def approval_resolution_event_id(correlation_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"flow-steward:approval.resolved:{correlation_id}"))
