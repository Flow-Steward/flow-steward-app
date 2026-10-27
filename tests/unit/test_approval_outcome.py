from __future__ import annotations

import json

import pytest

from core.application.management.approval_outcome import build_approval_resolution


@pytest.mark.parametrize(
    "reason",
    [
        "database not configured",
        "checkpointer not configured",
        "could not read graph state",
        "resume thread unavailable",
    ],
)
def test_ignored_infrastructure_outcome_remains_retryable(reason: str) -> None:
    assert (
        build_approval_resolution(
            event_type="notification.interaction_received",
            source="acme.chat",
            payload={
                "interaction_kind": "approval_decision",
                "action": "approve",
                "approval_id": "gate-1",
            },
            reactions=[
                {
                    "status": "ignored",
                    "reason": reason,
                    "outcome_code": "retryable_error",
                    "executed": False,
                }
            ],
        )
        is None
    )


@pytest.mark.parametrize("outcome_code", ["stale", "already_handled", "not_allowed"])
def test_only_explicit_terminal_machine_outcomes_resolve_as_not_applied(
    outcome_code: str,
) -> None:
    result = build_approval_resolution(
        event_type="notification.interaction_received",
        source="acme.chat",
        payload={
            "interaction_kind": "approval_decision",
            "action": "reject",
            "approval_id": "gate-1",
            "comment": "Keep the exact comment",
            "reply_target": {
                "provider_key": "acme.chat",
                "channel_id": "approvals",
                "external_conversation_ref": "opaque-conversation",
                "external_message_ref": "opaque-message",
            },
        },
        reactions=[
            {
                "status": "ignored",
                "reason": "provider-neutral reason",
                "outcome_code": outcome_code,
                "executed": False,
            }
        ],
    )

    assert result is not None
    assert result["applied"] is False
    assert result["accepted"] is False
    assert result["outcome_code"] == outcome_code
    assert result["comment"] == "Keep the exact comment"
    assert result["reply_target"]["external_message_ref"] == "opaque-message"


def test_reply_target_provider_is_bound_to_authoritative_event_source() -> None:
    result = build_approval_resolution(
        event_type="notification.interaction_received",
        source="acme.chat",
        payload={
            "interaction_kind": "approval_decision",
            "action": "approve",
            "approval_id": "gate-1",
            "reply_target": {
                "provider_key": "acme.attacker-selected-provider",
                "channel_id": "approvals",
                "external_conversation_ref": "opaque-conversation",
                "external_message_ref": "opaque-message",
            },
        },
        reactions=[{"status": "resumed", "executed": True}],
    )

    assert result is not None
    assert result["reply_target"]["provider_key"] == "acme.chat"


def test_resolution_payload_does_not_copy_provider_capability_token() -> None:
    result = build_approval_resolution(
        event_type="notification.interaction_received",
        source="acme.chat",
        payload={
            "interaction_kind": "approval_decision",
            "action": "approve",
            "approval_id": "gate-1",
            "chat": {"callback_data": "tgx:a:raw-secret-job-token"},
            "reply_target": {
                "channel_id": "approvals",
                "external_conversation_ref": "opaque-conversation",
                "external_message_ref": "opaque-message",
            },
        },
        reactions=[{"status": "resumed", "executed": True}],
    )

    assert result is not None
    assert "raw-secret-job-token" not in json.dumps(result)


def _resolution(action: str, approval_reason: str | None, *, executed: bool = True) -> dict:
    row = {"status": "resumed", "job_status": "resume_pending", "executed": executed}
    if approval_reason is not None:
        row["approval_reason"] = approval_reason
    if not executed:
        row["outcome_code"] = "not_allowed"
    resolution = build_approval_resolution(
        event_type="notification.interaction_received",
        source="acme.chat",
        payload={
            "interaction_kind": "approval_decision",
            "approval_id": "gate-1",
            "action": action,
        },
        reactions=[row],
    )
    assert resolution is not None
    return resolution


@pytest.mark.parametrize(
    ("action", "reason", "expected"),
    [
        ("approve", "authored", "approved_outcome"),
        ("reject", "authored", "rejected_outcome"),
        ("approve", "recovery", "continue"),
        ("reject", "recovery", "rework_or_retry"),
        ("reject", "autonomy", "rework_or_retry"),
        ("abort", "recovery", "stopped"),
        ("approve", None, "continue"),
        ("reject", None, "rejected"),
    ],
)
def test_the_next_transition_follows_the_decided_gate_kind(action, reason, expected) -> None:
    assert _resolution(action, reason)["next_transition"] == expected


def test_an_unaccepted_callback_promises_no_transition() -> None:
    resolution = _resolution("reject", "authored", executed=False)
    assert (resolution["accepted"], resolution["next_transition"]) == (False, "none")
