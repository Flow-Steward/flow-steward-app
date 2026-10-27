"""Per-principal MCP tool rate-limit guard.

Provides in-memory per-principal, per-tool sliding-window rate limiting for
MCP tool invocations.  State is process-local (not distributed); suitable for
single-process deployments.  Replace the store with a Redis-backed implementation
for multi-worker deployments.

API
---
check_mcp_rate_limit(principal_id, tool_name, window_seconds, max_calls)
    -> (allowed: bool, retry_after: int)

record_mcp_call(principal_id, tool_name, window_seconds, max_calls)
    -> None

clear_mcp_principal(principal_id)
    -> None  (test helper — flush all state for a principal)
"""

from __future__ import annotations

import threading
import time
from typing import Any

# ---------------------------------------------------------------------------
# Module-level in-memory store
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# Key: (principal_id, tool_name)  Value: {"calls": [timestamp, ...]}
_store: dict[tuple[str, str], dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_mcp_rate_limit(
    principal_id: str,
    tool_name: str,
    *,
    window_seconds: int = 60,
    max_calls: int = 100,
    now_fn=time.time,
) -> tuple[bool, int]:
    """Return ``(allowed, retry_after_seconds)``.

    ``allowed`` is ``True`` when the principal is within budget.
    ``retry_after_seconds`` is 0 when allowed, or the estimated seconds until
    the oldest call falls outside the window when denied.
    """
    now = now_fn()
    key = (str(principal_id or ""), str(tool_name or ""))
    with _lock:
        state = _store.get(key) or {}
        calls = [
            float(ts) for ts in (state.get("calls") or []) if now - float(ts) <= window_seconds
        ]
        if len(calls) < max_calls:
            return True, 0
        # Denied: estimate how long until the oldest call ages out.
        oldest = min(calls)
        retry_after = max(1, int(oldest + window_seconds - now))
        return False, retry_after


def record_mcp_call(
    principal_id: str,
    tool_name: str,
    *,
    window_seconds: int = 60,
    _max_calls: int = 100,
    now_fn=time.time,
) -> None:
    """Record a successful MCP tool invocation for rate-limit accounting."""
    now = now_fn()
    key = (str(principal_id or ""), str(tool_name or ""))
    with _lock:
        state = _store.get(key) or {}
        calls = [
            float(ts) for ts in (state.get("calls") or []) if now - float(ts) <= window_seconds
        ]
        calls.append(now)
        _store[key] = {"calls": calls}


def clear_mcp_principal(principal_id: str) -> None:
    """Remove all rate-limit state for *principal_id* (test helper)."""
    prefix = str(principal_id or "")
    with _lock:
        keys_to_delete = [k for k in _store if k[0] == prefix]
        for k in keys_to_delete:
            del _store[k]
