"""Public projections for Approval gates and decision history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.jobs.public_secret_redaction import redact_public_secrets

TOKEN_APPROVAL_KEYS = frozenset(
    {
        "approval_id",
        "interrupt_id",
        "message",
        "stage",
        "step_id",
        "step_execution_id",
        "group_id",
        "for_each_id",
        "allowed_actions",
        "expires_at",
        "content",
        "gate_kind",
        "recovery_kind",
        "approve_label",
        "reject_label",
        "comment_required_on_approve",
        "comment_required_on_reject",
        "override_content_supported",
    }
)
TOKEN_APPROVAL_CONTENT_KEYS = frozenset({"summary", "title", "reason", "description"})
APPROVAL_HISTORY_KEYS = frozenset(
    {
        "approval_id",
        "stage",
        "question",
        "content_preview",
        "approved",
        "action",
        "comment",
        "override_content_preview",
        "actor",
        "actor_type",
        "timestamp",
        "retry_ordinal",
    }
)
_FIELD_LIMITS = {
    "approval_id": 255,
    "interrupt_id": 255,
    "message": 1000,
    "stage": 128,
    "step_id": 255,
    "step_execution_id": 64,
    "group_id": 255,
    "for_each_id": 255,
    "expires_at": 64,
    "gate_kind": 128,
    "recovery_kind": 128,
    "approve_label": 255,
    "reject_label": 255,
}


def bounded_approval_text(value: Any, *, limit: int) -> str:
    return redact_public_secrets(str(value or "").strip())[:limit]


def bounded_approval_content(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): bounded_approval_text(item, limit=1000)
        for key, item in value.items()
        if key in TOKEN_APPROVAL_CONTENT_KEYS
        and isinstance(item, (str, int, float, bool))
        and bounded_approval_text(item, limit=1000)
    }


def bounded_approval_actions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        action
        for action in (bounded_approval_text(item, limit=64) for item in value[:16])
        if action
    ]


def serialize_token_approval_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the bounded typed approval contract, never checkpoint internals."""
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in TOKEN_APPROVAL_KEYS:
            continue
        if key in {
            "comment_required_on_approve",
            "comment_required_on_reject",
            "override_content_supported",
        }:
            if isinstance(value, bool):
                safe[key] = value
            continue
        if key == "content":
            safe[key] = bounded_approval_content(value)
            continue
        if key == "allowed_actions":
            safe[key] = bounded_approval_actions(value)
            continue
        if value is None:
            safe[key] = None
            continue
        if isinstance(value, (str, int, float, bool)):
            text = bounded_approval_text(value, limit=_FIELD_LIMITS[key])
            if text:
                safe[key] = text
    return safe


def serialize_approval_history_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project established decision fields without exposing durable retry context."""
    safe: dict[str, Any] = {
        key: value
        for key, value in entry.items()
        if key in APPROVAL_HISTORY_KEYS and isinstance(value, (str, bool, int, float, type(None)))
    }
    external_actor = entry.get("external_actor")
    if isinstance(external_actor, Mapping):
        safe["external_actor"] = {
            key: value
            for key, value in external_actor.items()
            if key in {"id", "display_name"} and isinstance(value, (str, type(None)))
        }
    return safe


def _humanized_label(value: Any) -> str:
    text = str(value or "").replace("-", " ").replace("_", " ").split()
    return " ".join(part.capitalize() for part in text)


def build_approval_notification_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the bounded provider-neutral context for one approval occurrence.

    Besides the token-safe approval contract it carries the wait identity
    (``wait_id``: the recorded wait of an authored Approval, else the stable
    recovery identity), why the run waits (``reason``: authored, recovery or
    autonomy) and the immediate container's label: ``group_label`` for a step
    directly in a Group, ``for_each_label`` for a step in a For each. The root
    of a workflow has no label.
    """
    from core.application.workflows.workflow_reaction_events import (
        approval_reason,
        recovery_wait_id,
    )

    safe = serialize_token_approval_payload(dict(payload))
    approval_id = str(safe.get("approval_id") or safe.get("interrupt_id") or "").strip()
    if not approval_id:
        return {}
    safe["approval_id"] = approval_id
    wait_id = bounded_approval_text(payload.get("wait_id"), limit=255) or recovery_wait_id(
        request_id=bounded_approval_text(payload.get("request_id"), limit=255),
        approval_id=approval_id,
    )
    if wait_id:
        safe["wait_id"] = wait_id
    gate_kind = str(payload.get("gate_kind") or "").strip()
    if gate_kind:
        safe["reason"] = approval_reason(gate_kind)
    group_id = str(safe.get("group_id") or "").strip()
    if group_id:
        group_label = bounded_approval_text(
            payload.get("group_label"), limit=255
        ) or bounded_approval_text(_humanized_label(group_id), limit=255)
        if group_label:
            safe["group_label"] = group_label
    for_each_id = str(safe.get("for_each_id") or "").strip()
    if for_each_id:
        for_each_label = bounded_approval_text(
            payload.get("for_each_label"), limit=255
        ) or bounded_approval_text(_humanized_label(for_each_id), limit=255)
        if for_each_label:
            safe["for_each_label"] = for_each_label
    raw_content = safe.get("content")
    content = raw_content if isinstance(raw_content, dict) else {}
    summary = next(
        (
            str(value).strip()
            for value in (
                safe.get("message"),
                content.get("summary"),
                content.get("reason"),
                content.get("title"),
                content.get("description"),
                safe.get("stage"),
            )
            if str(value or "").strip()
        ),
        "Approval required",
    )
    safe["summary"] = bounded_approval_text(summary, limit=1000)
    return safe


__all__ = [
    "bounded_approval_actions",
    "bounded_approval_content",
    "bounded_approval_text",
    "build_approval_notification_payload",
    "serialize_approval_history_entry",
    "serialize_token_approval_payload",
]
