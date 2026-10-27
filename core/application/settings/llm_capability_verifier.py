"""Fail-closed capability-probe contracts for routable chat models.

Provider metadata is only a discovery hint. These schemas are validated by the
host after every probe response and never invoke a semantic repair loop.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.messages import HumanMessage, ToolMessage

_MESSAGE_TIMEOUT_CAPABILITY_VERIFICATION_DEADLINE_EXCEEDED = (
    "timeout: capability verification deadline exceeded"
)

PROBE_SCHEMAS: dict[str, dict[str, Any]] = {
    "structured": {
        "title": "flow_steward_structured_probe",
        "type": "object",
        "properties": {
            "status": {"type": "string", "const": "ok"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "const": 7},
                        "label": {"type": "string", "const": "verified"},
                    },
                    "required": ["id", "label"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["status", "items"],
        "additionalProperties": False,
    },
    "combined": {
        "title": "flow_steward_combined_probe",
        "type": "object",
        "properties": {
            "status": {"type": "string", "const": "ok"},
            "tool_value": {"type": "string", "const": "probe-ok"},
        },
        "required": ["status", "tool_value"],
        "additionalProperties": False,
    },
}


class CapabilityMismatchError(ValueError):
    """The provider answered, but failed one mandatory route contract probe."""

    def __init__(self, message: str, *, probe: str = "") -> None:
        super().__init__(message)
        self.probe = probe


#: Which sub-check failed, as the safe code stored on the model row. The UI
#: names the capability from it, so a user knows what failed, not just that
#: "the required chat capabilities" were not met.
PROBE_FAILURE_CODES: dict[str, str] = {
    "basic": "capability_mismatch_chat",
    "tool_call": "capability_mismatch_tools",
    "tool_result": "capability_mismatch_tool_result",
    "structured": "capability_mismatch_structured_output",
    "combined": "capability_mismatch_tool_result_structured",
}

#: Typographic hyphens a model may print inside "probe-ok"; they are the same
#: value, so the replay check does not fail a model for its typography.
_HYPHEN_VARIANTS = str.maketrans({"\u2010": "-", "\u2011": "-"})


def verification_failure_code(error: Exception) -> str:
    """The safe code for a failed verification, naming the probe when one failed."""
    probe = getattr(error, "probe", "") if isinstance(error, CapabilityMismatchError) else ""
    return PROBE_FAILURE_CODES.get(probe) or normalize_verification_error(error)


def validate_probe_result(probe: str, value: Any) -> str | None:
    """Return a safe validation failure, or ``None`` for an exact result."""
    schema = PROBE_SCHEMAS.get(str(probe))
    if schema is None:
        return "invalid_response"
    error = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    return error.message if error is not None else None


def normalize_verification_error(error: Exception) -> str:
    """Map upstream failures to safe categories without leaking secrets."""
    message = str(error).lower()
    canonical_categories = (
        "authentication_error",
        "rate_limited",
        "timeout",
        "model_unavailable",
        "capability_mismatch",
        "invalid_response",
        "billing_quota_exceeded",
        "upstream_error",
    )
    for category in canonical_categories:
        if re.search(rf"(?<![a-z0-9_]){re.escape(category)}(?![a-z0-9_])", message):
            return category
    if isinstance(error, TimeoutError) or "timeout" in message or "timed out" in message:
        return "timeout"
    if any(
        token in message for token in ("401", "403", "api key", "unauthorized", "authentication")
    ):
        return "authentication_error"
    if any(token in message for token in ("429", "rate limit", "too many requests")):
        return "rate_limited"
    if any(
        token in message
        for token in (
            "credit balance",
            "billing quota",
            "insufficient balance",
            "insufficient credits",
        )
    ):
        return "billing_quota_exceeded"
    if any(token in message for token in ("not found", "model unavailable", "model_not_found")):
        return "model_unavailable"
    if isinstance(error, CapabilityMismatchError) or "capability mismatch" in message:
        return "capability_mismatch"
    if any(token in message for token in ("json", "schema", "malformed", "invalid response")):
        return "invalid_response"
    return "upstream_error"


_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "flow_steward_capability_probe",
        "description": "Return the supplied probe value.",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string", "const": "probe"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
}


def _require_probe_result(probe: str, value: Any) -> None:
    failure = validate_probe_result(probe, value)
    if failure:
        raise CapabilityMismatchError(
            f"capability mismatch: {probe} probe failed schema validation: {failure}",
            probe=probe,
        )


@contextmanager
def _probe(name: str) -> Iterator[None]:
    """Tag a capability failure raised during one probe with that probe's name.

    The provider extension reports a refused contract (no tool call where one
    is required, unparseable structured output) as ``capability_mismatch``;
    that is the same failure as the host's own check, so it carries the probe
    too. Timeouts, authentication and other errors pass through unchanged.
    """
    try:
        yield
    except CapabilityMismatchError as exc:
        if not exc.probe:
            exc.probe = name
        raise
    except Exception as exc:
        if normalize_verification_error(exc) == "capability_mismatch":
            raise CapabilityMismatchError(
                f"capability mismatch: {name} probe was refused by the provider", probe=name
            ) from exc
        raise


def _message_text(message: Any) -> str:
    """Extract only user-visible text from LangChain message content blocks."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    blocks = getattr(message, "content_blocks", None)
    if not isinstance(blocks, list):
        blocks = content if isinstance(content, list) else []
    text_parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return "".join(text_parts)


def _invoke_before_deadline(
    runnable: Any,
    value: Any,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> Any:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError(_MESSAGE_TIMEOUT_CAPABILITY_VERIFICATION_DEADLINE_EXCEEDED)
    bind = getattr(runnable, "bind", None)
    bounded = bind(timeout=remaining) if callable(bind) else runnable
    result = bounded.invoke(value)
    if monotonic() > deadline:
        raise TimeoutError(_MESSAGE_TIMEOUT_CAPABILITY_VERIFICATION_DEADLINE_EXCEEDED)
    return result


def run_capability_suite(
    model: Any,
    *,
    timeout_seconds: float = 60.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> set[str]:
    """Execute the mandatory no-repair routing contract against one model.

    The caller owns credentials and persistence. Any malformed answer or lost
    tool context raises immediately, leaving the model unverified.
    """
    budget = float(timeout_seconds)
    if budget <= 0:
        raise TimeoutError(_MESSAGE_TIMEOUT_CAPABILITY_VERIFICATION_DEADLINE_EXCEEDED)
    deadline = monotonic() + budget
    with _probe("basic"):
        basic = _invoke_before_deadline(
            model,
            "Return exactly basic-ok.",
            deadline=deadline,
            monotonic=monotonic,
        )
        if _message_text(basic).strip() != "basic-ok":
            raise CapabilityMismatchError(
                "capability mismatch: basic chat probe did not return basic-ok"
            )

    tool_model = model.bind_tools([_PROBE_TOOL], tool_choice="required", parallel_tool_calls=False)
    # The request names the follow-up as well. The value is only in the tool's
    # result, so repeating it still proves the model read that result; without
    # the instruction the replay turn had nothing to answer, and gpt-oss models
    # often summarised ("The probe capability has been confirmed.") instead.
    messages = [
        HumanMessage(
            content=(
                "Call flow_steward_capability_probe with key probe, then reply with "
                "exactly the value it returns."
            )
        )
    ]
    with _probe("tool_call"):
        tool_call_message = _invoke_before_deadline(
            tool_model,
            messages,
            deadline=deadline,
            monotonic=monotonic,
        )
        calls = getattr(tool_call_message, "tool_calls", None)
        if not isinstance(calls, list) or len(calls) != 1:
            raise CapabilityMismatchError(
                "capability mismatch: tool probe did not return exactly one tool call"
            )
        call = calls[0] if isinstance(calls[0], dict) else {}
        if (
            str(call.get("id") or "").strip() == ""
            or call.get("name") != "flow_steward_capability_probe"
            or call.get("args") != {"key": "probe"}
        ):
            raise CapabilityMismatchError(
                "capability mismatch: tool probe returned an invalid tool call"
            )
    messages.extend(
        [
            tool_call_message,
            ToolMessage(
                content='{"value":"probe-ok"}',
                tool_call_id=str(call["id"]),
                name="flow_steward_capability_probe",
            ),
        ]
    )
    final_model = model.bind_tools([_PROBE_TOOL], tool_choice="none", parallel_tool_calls=False)
    with _probe("tool_result"):
        final = _invoke_before_deadline(
            final_model,
            messages,
            deadline=deadline,
            monotonic=monotonic,
        )
        if "probe-ok" not in _message_text(final).translate(_HYPHEN_VARIANTS):
            raise CapabilityMismatchError(
                "capability mismatch: tool result replay did not produce probe-ok"
            )

    with _probe("structured"):
        structured = _invoke_before_deadline(
            model.with_structured_output(PROBE_SCHEMAS["structured"], strict=True),
            "Return the required structured probe result.",
            deadline=deadline,
            monotonic=monotonic,
        )
        _require_probe_result("structured", structured)
    with _probe("combined"):
        combined = _invoke_before_deadline(
            model.with_structured_output(PROBE_SCHEMAS["combined"], strict=True),
            messages,
            deadline=deadline,
            monotonic=monotonic,
        )
        _require_probe_result("combined", combined)
    from core.application.settings.llm_verification_policy import REQUIRED_ROUTING_CAPABILITIES

    return set(REQUIRED_ROUTING_CAPABILITIES)
