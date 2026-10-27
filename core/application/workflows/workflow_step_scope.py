"""Pure workflow runtime step identity helpers."""

from __future__ import annotations

from typing import Any


def runtime_step_scope_id(runtime_state: dict[str, Any]) -> str:
    """The step the run cursor points at, as the hub mirrors it into state."""
    if not isinstance(runtime_state, dict):
        return ""
    current = str(runtime_state.get("current_step_id") or "").strip()
    if current:
        return current
    cursor = runtime_state.get("run_cursor")
    if isinstance(cursor, dict):
        return str(cursor.get("step_id") or "").strip()
    return ""


__all__ = ["runtime_step_scope_id"]
