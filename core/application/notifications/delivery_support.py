"""Notification delivery helpers: event preparation, effect claims, dispatch status."""

from __future__ import annotations

from typing import Any

from core.application.extensions.notification_capability import NotificationCapability
from core.application.notifications import sensitive_payload
from core.application.notifications.contracts import _as_dict, _as_str


def _notification_external_effect_source_ref(event: dict, route_payload: dict) -> dict[str, Any]:
    return {
        "event_id": _as_str(event.get("event_id")) or None,
        "correlation_id": _as_str(event.get("correlation_id")) or None,
        "workflow_id": _as_str(event.get("workflow_id")) or None,
        "job_id": event.get("job_id"),
        "route_id": _as_str(route_payload.get("route_id")) or None,
        "route_key": _as_str(route_payload.get("route_key")) or None,
    }


def _claim_notification_external_effect(
    *,
    bl,
    extension: dict,
    extension_id: str,
    extension_install_id: str,
    connection_id: str,
    account_id: str,
    project_id: str,
    event: dict,
    route_payload: dict,
    provider_payload: dict,
    action_payload: dict | None = None,
    test_mode: bool = False,
    capability: NotificationCapability | None = None,
) -> dict[str, Any]:
    from core.application.extensions import external_effects

    # A flagged event's body and subject never reach the ledger: its fingerprint
    # is taken over placeholders that carry only their length and hash.
    fingerprint_event = sensitive_payload.redact_event(event)
    # Delivery retries rebuild transport envelopes; wall-clock construction time is
    # not part of the durable notification intent, and neither is the run link
    # (it follows the platform's configured address).
    fingerprint_event.pop("occurred_at", None)
    fingerprint_event.pop("run_url", None)
    request_payload = {
        "action": "deliver_notification",
        "action_payload": _as_dict(action_payload),
        "event": fingerprint_event,
        "route": sensitive_payload.redact_route(route_payload, event),
        "provider": provider_payload,
    }
    source_ref = _notification_external_effect_source_ref(event, route_payload)
    declaration_overrides: dict[str, Any] | None = None
    if capability is not None and capability.declared:
        # The connection type's own target kind and ledger channel.
        source_ref["target_kind"] = capability.target_kind
        source_ref["ledger_channel"] = capability.ledger_channel
        declaration_overrides = {
            "channel": capability.ledger_channel,
            "observability": {"target_kind": capability.target_kind},
        }
    intent_key = external_effects.external_effect_identity_hmac(
        purpose="notification-intent",
        value={
            "account_id": account_id,
            "project_id": project_id,
            "extension_id": extension_id,
            "extension_install_id": extension_install_id,
            "connection_id": connection_id,
            "operation_id": "deliver_notification",
            "event_id": source_ref.get("event_id"),
            "route_id": source_ref.get("route_id"),
            "route_key": source_ref.get("route_key"),
            "test_mode": bool(test_mode),
        },
    )
    return external_effects.claim_external_effect_for_action(
        bl,
        extension=extension,
        account_id=account_id,
        project_id=project_id or None,
        extension_id=extension_id,
        extension_install_id=extension_install_id or None,
        connection_id=connection_id or None,
        operation_id="deliver_notification",
        source_type="notification_event",
        source_ref=source_ref,
        request_payload=request_payload,
        idempotency_key_override=intent_key,
        test_mode=test_mode,
        declaration_overrides=declaration_overrides,
    )


def _with_standard_email(target_kind: str, event: dict, route_config: dict) -> dict:
    """Give a workflow event sent by email the platform's standard subject and text.

    Only for a connection whose deliveries address a recipient (email); a chat
    connection formats its own messages. The decision is the connection type's,
    so one provider's recipient route gets it and its channel route does not. A
    subject set on the route is kept. The platform's own messages (``system.*``)
    already carry their text.
    """
    from core.application.auth.serve_auth_runtime import public_base_url
    from core.application.extensions.event_email import render_event_email

    if _as_str(event.get("event_type")).startswith("system."):
        return event
    if _as_str(target_kind) != "recipient":
        return event
    subject, body = render_event_email(event, base_url=public_base_url())
    route_config.setdefault("subject", subject)
    if not _as_str(route_config.get("subject")):
        route_config["subject"] = subject
    payload = {**_as_dict(event.get("payload")), "message": body}
    return {**event, "payload": payload}


def _with_run_url(event: dict) -> dict:
    """Give the event ``run_url``: the run's page, when the platform knows its address.

    Every provider gets it, so a channel can link to the run as the standard
    email does. Absent without a public base URL or a job.
    """
    from core.application.auth.serve_auth_runtime import public_base_url
    from core.application.extensions.event_email import event_run_url

    run_url = event_run_url(event, base_url=public_base_url())
    if not run_url:
        event = dict(event)
        event.pop("run_url", None)
        return event
    return {**event, "run_url": run_url}


def _dispatch_status_from_delivery(status: str, error_text: str) -> tuple[str, str | None]:
    normalized_status = _as_str(status).lower()
    normalized_error = _as_str(error_text).lower()
    if normalized_status == "delivered":
        return "succeeded", None
    if normalized_status == "skipped":
        if "disabled" in normalized_error:
            return "skipped", "extension_disabled"
        return "skipped", "condition_not_met"
    if "timeout" in normalized_error:
        return "failed", "delivery_timeout"
    return "failed", "handler_exception"
