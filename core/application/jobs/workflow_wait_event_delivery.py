"""Store the events an extension delivers to waiting runs (ADR 0071).

A successful extension response may carry ``resume_waits``::

    {"ok": true, "result": {...},
     "resume_waits": [{"correlation_key": "run-42", "event_type": "run_completed",
                       "payload": {"rows": 120}}]}

The host takes the extension id, account and project from the trusted
invocation context, never from the response, checks and bounds every item and
stores it in the inbox. It resumes nothing here: the control process matches
stored events to runs parked ``awaiting_event`` (see
``workflow_event_wait_repository``). A malformed item is refused without failing
the action; a storage failure fails it, so a callback answers 502 and the
provider delivers again.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from core.application.jobs.workflow_wait_contract import (
    WAIT_EVENT_ITEMS_MAX,
    WAIT_EVENT_PAYLOAD_MAX_BYTES,
    wait_event_correlation_key,
    wait_event_type_is_valid,
)

logger = logging.getLogger(__name__)

RESUME_WAITS_KEY = "resume_waits"
WAIT_EVENTS_RESULT_KEY = "wait_events"
WAIT_EVENT_DELIVERY_FAILED = "wait_event_delivery_failed"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _rejection(index: int | None, code: str, message: str) -> dict[str, Any]:
    return {"index": index, "code": code, "message": message}


def checked_wait_event(index: int, item: Any) -> tuple[Any | None, dict[str, Any] | None]:
    """One ``resume_waits`` item as a storable delivery, or why it is refused."""
    from core.application.jobs.workflow_event_wait_repository import WaitEventDelivery
    from core.application.security.credential_redaction import redact

    if not isinstance(item, Mapping):
        return None, _rejection(index, "resume_wait_invalid", "a resume_waits item is an object")
    key = wait_event_correlation_key(item.get("correlation_key"))
    if key is None:
        return None, _rejection(
            index,
            "resume_wait_correlation_key_invalid",
            "correlation_key is a non-empty text or integer of at most 256 characters",
        )
    event_type = item.get("event_type")
    if not wait_event_type_is_valid(event_type):
        return None, _rejection(
            index,
            "resume_wait_event_type_invalid",
            "event_type uses lowercase letters, digits, '_', '.', ':' and '-'",
        )
    payload = item.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return None, _rejection(index, "resume_wait_payload_invalid", "payload is an object")
    redacted = redact(dict(payload))
    try:
        size = len(json.dumps(redacted, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None, _rejection(index, "resume_wait_payload_invalid", "payload is not plain JSON")
    if size > WAIT_EVENT_PAYLOAD_MAX_BYTES:
        return None, _rejection(
            index,
            "resume_wait_payload_too_large",
            f"payload is {size} bytes; at most {WAIT_EVENT_PAYLOAD_MAX_BYTES} are accepted",
        )
    return WaitEventDelivery(
        correlation_key=key,
        event_type=str(event_type),
        payload=redacted if isinstance(redacted, dict) else {},
    ), None


def deliver_wait_events(
    invocation: dict[str, Any],
    *,
    db: Any,
    extension_id: str,
    account_id: str,
    project_id: str,
    extension_install_id: str | None = None,
    repository_factory: Any = None,
) -> dict[str, Any] | None:
    """Store a successful response's ``resume_waits``; ``None`` when it has none.

    The outcome is recorded on the invocation as ``wait_events``:
    ``{stored, duplicates, rejected}``.
    """
    if str(invocation.get("status") or "") not in {"", "succeeded"}:
        return None
    response = invocation.get("response")
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        return None
    if RESUME_WAITS_KEY not in response:
        return None
    raw = response.get(RESUME_WAITS_KEY)
    outcome: dict[str, Any] = {"stored": 0, "duplicates": [], "rejected": []}
    invocation[WAIT_EVENTS_RESULT_KEY] = outcome
    if not isinstance(raw, list):
        outcome["rejected"].append(
            _rejection(None, "resume_waits_invalid", "resume_waits is a list of objects")
        )
        return outcome
    scope = {
        "account_id": _text(account_id),
        "project_id": _text(project_id),
        "extension_id": _text(extension_id),
    }
    missing = [name for name, value in scope.items() if not value]
    if missing:
        outcome["rejected"].append(
            _rejection(
                None,
                "resume_waits_scope_missing",
                f"resume_waits need a project run context; missing {', '.join(missing)}",
            )
        )
        logger.warning("resume_waits refused without %s", ", ".join(missing))
        return outcome
    deliveries: list[tuple[int, Any]] = []
    for index, item in enumerate(raw):
        if index >= WAIT_EVENT_ITEMS_MAX:
            outcome["rejected"].append(
                _rejection(
                    index,
                    "resume_waits_too_many",
                    f"at most {WAIT_EVENT_ITEMS_MAX} resume_waits items per response",
                )
            )
            continue
        delivery, rejection = checked_wait_event(index, item)
        if rejection is not None:
            outcome["rejected"].append(rejection)
        else:
            deliveries.append((index, delivery))
    if outcome["rejected"]:
        logger.warning(
            "extension %s: %d resume_waits item(s) refused: %s",
            scope["extension_id"],
            len(outcome["rejected"]),
            ", ".join(sorted({row["code"] for row in outcome["rejected"]})),
        )
    if not deliveries:
        return outcome
    if repository_factory is None:
        from core.application.jobs.workflow_event_wait_repository import (
            WorkflowEventWaitRepository,
        )

        repository_factory = WorkflowEventWaitRepository
    try:
        stored = repository_factory(db).deliver(
            account_id=scope["account_id"],
            project_id=scope["project_id"],
            source=scope["extension_id"],
            extension_install_id=_text(extension_install_id),
            items=[delivery for _index, delivery in deliveries],
        )
    except Exception as exc:
        logger.warning("resume_waits not stored (error_type=%s)", type(exc).__name__)
        error = {
            "code": WAIT_EVENT_DELIVERY_FAILED,
            "message": "The events for waiting runs could not be stored",
        }
        invocation["status"] = "failed"
        invocation["detail"] = WAIT_EVENT_DELIVERY_FAILED
        invocation["errors"] = [*list(invocation.get("errors") or []), error]
        return outcome
    duplicate_count = 0
    for (index, _delivery), was_stored in zip(deliveries, stored, strict=True):
        if was_stored:
            outcome["stored"] += 1
            continue
        outcome["duplicates"].append(index)
        duplicate_count += 1
    if duplicate_count:
        # The count only: item positions come from the extension's response, which
        # is never logged; the outcome names them for the caller.
        logger.info("%d duplicate resume_waits event(s) ignored", duplicate_count)
    return outcome


__all__ = [
    "RESUME_WAITS_KEY",
    "WAIT_EVENTS_RESULT_KEY",
    "WAIT_EVENT_DELIVERY_FAILED",
    "checked_wait_event",
    "deliver_wait_events",
]
