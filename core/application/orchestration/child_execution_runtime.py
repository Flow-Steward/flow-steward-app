"""Invocation-local primitives for same-worker child workflow execution."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChildExecutionRuntime:
    """Worker-owned primitives available only during one graph invocation."""

    worker_id: str
    execute_inline: Callable[[Mapping[str, Any]], Mapping[str, object]]
    artifact_plane_provider: Callable[[], Any] | None = None


_current_child_execution_runtime: ContextVar[ChildExecutionRuntime | None] = ContextVar(
    "current_child_execution_runtime",
    default=None,
)


def current_child_execution_runtime() -> ChildExecutionRuntime | None:
    """Return the runtime bound to the current graph invocation, if any."""

    return _current_child_execution_runtime.get()


@contextmanager
def bind_child_execution_runtime(
    runtime: ChildExecutionRuntime | None,
) -> Iterator[None]:
    """Bind and reliably reset one graph invocation's child runtime."""

    token = _current_child_execution_runtime.set(runtime)
    try:
        yield
    finally:
        _current_child_execution_runtime.reset(token)
