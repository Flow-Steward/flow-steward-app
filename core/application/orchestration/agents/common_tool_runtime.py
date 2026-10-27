"""Tool-call runtime helpers for agents.common."""

from __future__ import annotations

import signal
import threading
import time
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_DEFAULT_TOOL_CALL_TIMEOUT_SECONDS = 120
MCP_PROJECTED_TOOL_METADATA_KEY = "flow_steward_projected_mcp_v1"
MCP_CANONICAL_TOOL_ID_METADATA_KEY = "flow_steward_canonical_tool_id"


def _indent_tool_block(text: str, spaces: int = 2) -> str:
    """Indent multiline tool output while preserving real newlines."""
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else "" for line in text.splitlines())


def _stringify_tool_result(raw: Any) -> str:
    """Render structured tool results without Python repr escape noise."""
    if raw is None:
        return "(empty result)"
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        if not raw:
            return "{}"
        lines: list[str] = []
        for key, value in raw.items():
            rendered = _stringify_tool_result(value)
            if isinstance(value, (dict, list, tuple, set)) or "\n" in rendered:
                lines.append(f"{key}:")
                lines.append(_indent_tool_block(rendered))
            else:
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
        if not items:
            return "[]"
        lines = []
        for item in items:
            rendered = _stringify_tool_result(item)
            if isinstance(item, (dict, list, tuple, set)) or "\n" in rendered:
                lines.append("-")
                lines.append(_indent_tool_block(rendered))
            else:
                lines.append(f"- {rendered}")
        return "\n".join(lines)
    return str(raw)


def _resolve_tool_call_id(response: Any, tool_call: dict, index: int) -> str:
    """Return the provider tool-call id that must be echoed by ToolMessage.

    LangChain normally exposes the provider id on ``tool_call["id"]``. In live
    OpenAI runs we have also seen parsed calls with a missing id while the raw
    ``additional_kwargs["tool_calls"]`` payload still contains the authoritative
    ``call_*`` id. Echoing a fallback name in that case makes the next OpenAI
    request invalid because the assistant tool call appears unanswered.
    """
    parsed_id = tool_call.get("id")
    if parsed_id:
        return str(parsed_id)

    additional_kwargs = getattr(response, "additional_kwargs", None)
    raw_tool_calls = (
        additional_kwargs.get("tool_calls") if isinstance(additional_kwargs, dict) else None
    )
    if isinstance(raw_tool_calls, list) and 0 <= index < len(raw_tool_calls):
        raw_tool_call = raw_tool_calls[index]
        if isinstance(raw_tool_call, dict) and raw_tool_call.get("id"):
            return str(raw_tool_call["id"])

    tool_name = str(tool_call.get("name") or "").strip()
    return tool_name or f"tool_call_{index}"


def _agent_tool_call_timeout_seconds(agent_timeout_seconds: int = 0) -> int:
    raw = str(
        _runtime_env.get(
            "FS_AGENT_TOOL_CALL_TIMEOUT_SECONDS",
            str(_DEFAULT_TOOL_CALL_TIMEOUT_SECONDS),
        )
        or str(_DEFAULT_TOOL_CALL_TIMEOUT_SECONDS)
    ).strip()
    try:
        configured = int(float(raw))
    except (TypeError, ValueError):
        configured = _DEFAULT_TOOL_CALL_TIMEOUT_SECONDS
    configured = max(0, configured)
    try:
        agent_timeout = max(0, int(agent_timeout_seconds or 0))
    except (TypeError, ValueError):
        agent_timeout = 0
    if configured and agent_timeout:
        return max(1, min(configured, agent_timeout))
    return configured


def _invoke_tool_with_timeout(
    tool_fn: Any,
    tool_args: dict[str, Any],
    *,
    tool_name: str,
    timeout_seconds: int,
) -> Any:
    if timeout_seconds <= 0:
        return tool_fn.invoke(tool_args)
    if threading.current_thread() is not threading.main_thread():
        return tool_fn.invoke(tool_args)
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return tool_fn.invoke(tool_args)

    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)

    def _handle_timeout(_signum, _frame):
        raise TimeoutError(f"tool '{tool_name}' exceeded {timeout_seconds}s execution timeout")

    signal.signal(signal.SIGALRM, _handle_timeout)
    try:
        return tool_fn.invoke(tool_args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        previous_delay, previous_interval = previous_timer
        if previous_delay > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, previous_delay - elapsed),
                previous_interval,
            )


__all__ = [
    "_agent_tool_call_timeout_seconds",
    "_invoke_tool_with_timeout",
    "_resolve_tool_call_id",
    "_stringify_tool_result",
]
