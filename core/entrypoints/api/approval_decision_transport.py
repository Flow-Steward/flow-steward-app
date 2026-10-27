"""Bounded HTTP transport parsing for public approval decisions."""

from __future__ import annotations

import json

from pydantic import ValidationError

from core.application.management.command_models import ApprovalDecisionInput
from core.application.management.errors import ServiceValidationError

MAX_APPROVAL_DECISION_BODY_BYTES = 64 * 1024


def _payload_too_large() -> ServiceValidationError:
    return ServiceValidationError(
        "Approval decision body exceeds the size limit",
        status_code=413,
        payload={
            "error": "Approval decision body exceeds the size limit",
            "code": "approval_payload_too_large",
        },
    )


async def approval_decision_from_request(
    request,
) -> ApprovalDecisionInput:
    content_type = str(request.headers.get("content-type") or "")
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise ServiceValidationError("Approval decisions require an application/json body")
    content_length = str(request.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > MAX_APPROVAL_DECISION_BODY_BYTES:
        raise _payload_too_large()

    try:
        cached = getattr(request, "_body", None)
        if isinstance(cached, (bytes, bytearray)):
            body = bytes(cached)
            if len(body) > MAX_APPROVAL_DECISION_BODY_BYTES:
                raise _payload_too_large()
        else:
            buffered = bytearray()
            async for chunk in request.stream():
                if not chunk:
                    continue
                if len(buffered) + len(chunk) > MAX_APPROVAL_DECISION_BODY_BYTES:
                    raise _payload_too_large()
                buffered.extend(chunk)
            body = bytes(buffered)
        raw_payload = json.loads(body)
    except ServiceValidationError:
        raise
    except Exception as exc:
        raise ServiceValidationError("Invalid JSON body; expected an object payload") from exc
    if not isinstance(raw_payload, dict):
        raise ServiceValidationError("Invalid JSON body; expected an object payload")

    try:
        return ApprovalDecisionInput.model_validate(dict(raw_payload))
    except ValidationError as exc:
        parts: list[str] = []
        for item in exc.errors(include_url=False):
            location = ".".join(str(part) for part in (item.get("loc") or []))
            message = str(item.get("msg") or "Invalid input").strip()
            parts.append(f"{location}: {message}" if location else message)
        raise ServiceValidationError("; ".join(parts) or "Invalid input") from exc


__all__ = [
    "MAX_APPROVAL_DECISION_BODY_BYTES",
    "approval_decision_from_request",
]
