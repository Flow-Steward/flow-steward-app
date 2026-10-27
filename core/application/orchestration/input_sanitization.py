"""Dependency-light application policy for untrusted external text."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from itertools import islice

from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)
REDACTED_MARKER = "[REDACTED]"
_INJECTION_RE = re.compile(
    r"ignore\s+(previous|all|above|prior)\s+instructions?"
    r"|you\s+are\s+now\s+"
    r"|new\s+instructions?:"
    r"|disregard\s+(all|previous|your)"
    r"|system\s+prompt"
    r"|\[INST\]|\[\/INST\]"
    r"|<\|im_start\|>|<\|im_end\|>"
    r"|<\|endoftext\|>",
    re.IGNORECASE,
)
_EXTERNAL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_EXTERNAL_ERROR_MAX_CHUNKS = 64
_EXTERNAL_ERROR_INSTRUCTION_RE = re.compile(
    r"\b(?:reveal|expose|exfiltrate)\b.{0,160}\b(?:api[- ]?keys?|credentials?|secrets?|tokens?)\b"
    r"(?:.{0,160}\b(?:send|upload|transmit)\b.{0,80}\bhttps?://\S+)?"
    r"|\b(?:send|upload|transmit)\b.{0,160}\b(?:api[- ]?keys?|credentials?|secrets?|tokens?)\b"
    r".{0,80}\bhttps?://\S+",
    re.IGNORECASE,
)


def _coerce_scope_id(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    return normalized or None


def resolve_security_scope(
    state: OrchestratorState | dict | None = None,
    *,
    project_id: object = None,
    job_id: object = None,
) -> tuple[str | None, str | None]:
    """Resolve scanner scope IDs from explicit args and runtime state."""

    state_dict = state if isinstance(state, dict) else {}
    project_cfg = state_dict.get("project_config") if isinstance(state_dict, dict) else {}
    if not isinstance(project_cfg, dict):
        project_cfg = {}
    resolved_project_id = (
        _coerce_scope_id(project_id)
        or _coerce_scope_id(state_dict.get("project_id"))
        or _coerce_scope_id(project_cfg.get("project_id"))
    )
    resolved_job_id = (
        _coerce_scope_id(job_id)
        or _coerce_scope_id(state_dict.get("job_id"))
        or _coerce_scope_id(project_cfg.get("job_id"))
    )
    return resolved_project_id, resolved_job_id


def _normalize_inbound_context_type(context_type: str) -> str:
    normalized = str(context_type or "").strip().lower()
    if normalized in {"prompt", "display", "shell"}:
        return normalized
    return "prompt"


def sanitize_external_input(
    text: str,
    context_type: str = "general",
    *,
    state: OrchestratorState | dict | None = None,
    project_id: object = None,
    job_id: object = None,
) -> str:
    """Apply the canonical inbound security scan with a fail-safe token scrubber."""

    if not text:
        return text
    try:
        from core.infrastructure.security.integration import scan_inbound

        resolved_project_id, resolved_job_id = resolve_security_scope(
            state,
            project_id=project_id,
            job_id=job_id,
        )
        decision = scan_inbound(
            text,
            project_id=resolved_project_id,
            job_id=resolved_job_id,
            context_type=_normalize_inbound_context_type(context_type),
        )
        if decision.action == "block":
            logger.warning(
                "Security scan BLOCKED content [context=%s]. Original length=%d, findings=%d",
                context_type,
                len(text),
                len(decision.findings),
            )
            return "[BLOCKED: content rejected by security policy]"
        result = decision.redacted_text if decision.redacted_text is not None else text
        if result != text:
            logger.warning(
                "Security scan redacted content [context=%s]. "
                "Original length=%d, sanitized length=%d, findings=%d",
                context_type,
                len(text),
                len(result),
                len(decision.findings),
            )
        sanitized_result = _INJECTION_RE.sub(REDACTED_MARKER, result)
        sanitized_result = re.sub(
            r"\[INJECTION:[^\]]*REDACTED\]",
            REDACTED_MARKER,
            sanitized_result,
            flags=re.IGNORECASE,
        )
        if sanitized_result != result:
            logger.warning(
                "Prompt injection pattern detected and redacted [context=%s]. "
                "Original length=%d, sanitized length=%d",
                context_type,
                len(result),
                len(sanitized_result),
            )
        return sanitized_result
    except Exception:
        sanitized = _INJECTION_RE.sub(REDACTED_MARKER, text)
        if sanitized != text:
            logger.warning(
                "Prompt injection pattern detected and redacted [context=%s]. "
                "Original length=%d, sanitized length=%d",
                context_type,
                len(text),
                len(sanitized),
            )
        return sanitized


def sanitize_external_error_text(
    chunks: Iterable[object],
    *,
    max_chars: int,
    state: OrchestratorState | dict | None = None,
) -> str:
    """Return a bounded, redacted diagnostic prefix from untrusted text chunks."""

    budget = max(0, int(max_chars))
    if budget == 0:
        return ""
    bounded_chunks: list[str] = []
    remaining = budget
    truncated = False
    for chunk in islice(chunks, _EXTERNAL_ERROR_MAX_CHUNKS):
        if remaining <= 0:
            truncated = True
            break
        if not isinstance(chunk, str):
            continue
        bounded_chunk = chunk[:remaining]
        remaining -= len(bounded_chunk)
        if len(bounded_chunk) < len(chunk):
            truncated = True
        bounded_chunks.append(bounded_chunk)
        if truncated:
            break
    if not bounded_chunks:
        return ""

    normalized = " ".join(_EXTERNAL_CONTROL_RE.sub(" ", "".join(bounded_chunks)).split())
    if not normalized:
        return ""

    from core.application.security.credential_redaction import redact_text
    from core.infrastructure.security.secret_detection import redact_detected_secrets

    sanitized = redact_detected_secrets(redact_text(normalized))
    sanitized = _EXTERNAL_ERROR_INSTRUCTION_RE.sub(REDACTED_MARKER, sanitized)
    sanitized = sanitize_external_input(sanitized, context_type="prompt", state=state)
    sanitized = redact_detected_secrets(redact_text(sanitized)).strip()
    if not sanitized:
        return ""
    if truncated and len(sanitized) < budget:
        return f"{sanitized}…"
    if truncated or len(sanitized) > budget:
        candidate = sanitized[: budget - 1].rstrip()
        boundary = candidate.rfind(" ")
        candidate = candidate[:boundary].rstrip() if boundary > 0 else ""
        return f"{candidate}…" if candidate else "…"
    return sanitized


__all__ = [
    "resolve_security_scope",
    "sanitize_external_error_text",
    "sanitize_external_input",
]
