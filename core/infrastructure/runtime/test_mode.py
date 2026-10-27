"""
Runtime helpers for workflow test mode.

`test_mode` executes the real orchestration path; it is not an external-effect
sandbox. HTTP requests and model calls can still execute. Declared extension
effect suppression and worker notification handling have their own contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

_RUNTIME_TEST_MODE: ContextVar[bool] = ContextVar("platform_runtime_test_mode", default=False)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}

# Optional explicit blocks; currently empty, not a general side-effect policy.
_BLOCKED_TOOL_NAMES: set[str] = set()

# Optional prefix-based blocks; extension effect suppression is separate.
_BLOCKED_TOOL_PREFIXES: tuple[str, ...] = ()


def parse_test_mode(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in _TRUTHY:
            return True
        if raw in _FALSY:
            return False
    return bool(value)


def set_runtime_test_mode(enabled: bool):
    return _RUNTIME_TEST_MODE.set(bool(enabled))


def reset_runtime_test_mode(token) -> None:
    _RUNTIME_TEST_MODE.reset(token)


def is_test_mode_enabled() -> bool:
    return bool(_RUNTIME_TEST_MODE.get())


def is_test_mode_state(state: Mapping[str, Any] | None) -> bool:
    if isinstance(state, Mapping):
        if "test_mode" in state:
            return parse_test_mode(state.get("test_mode"), False)
        extra_state = state.get("extra_state")
        if isinstance(extra_state, Mapping) and "test_mode" in extra_state:
            return parse_test_mode(extra_state.get("test_mode"), False)
    return is_test_mode_enabled()


def should_block_tool_call(tool_name: str, tool_args: dict | None = None) -> bool:
    _ = tool_args
    name = str(tool_name or "").strip()
    if not name:
        return False
    if name in _BLOCKED_TOOL_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _BLOCKED_TOOL_PREFIXES)
