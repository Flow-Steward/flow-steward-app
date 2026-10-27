"""The one way a step executor learns which step it runs.

The run cursor in the state names the step and its logical execution; the step
itself comes from the run's pinned definition. A parallel lane runs on its own
lane state with its own cursor, so the same accessor serves root, Group, For
each and lane steps alike. Nothing here looks at sort order or a phase list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.orchestration.run_cursor import (
    ForEachFrame,
    Frame,
    FrozenDefinition,
    RunCursor,
)
from core.application.orchestration.run_definition import load_run_definition

RUN_CURSOR_KEY = "run_cursor"


@dataclass(frozen=True)
class ActiveStep:
    step: dict[str, Any]
    step_id: str
    step_execution_id: str
    attempt: int
    frames: tuple[Frame, ...] = ()

    @property
    def step_kind(self) -> str:
        return str(self.step.get("step_kind") or "")

    @property
    def group_id(self) -> str | None:
        """The step's immediate Group; None for a For each step and at the root."""
        value = self.step.get("group_id")
        return str(value) if value else None

    @property
    def for_each_id(self) -> str | None:
        """The step's immediate For each; None for a Group step and at the root."""
        value = self.step.get("for_each_id")
        return str(value) if value else None

    @property
    def lane_id(self) -> str | None:
        value = self.step.get("lane_id")
        return str(value) if value else None

    @property
    def frame(self) -> Frame | None:
        """The innermost container frame the step runs in."""
        return self.frames[-1] if self.frames else None

    @property
    def for_each_frame(self) -> ForEachFrame | None:
        return next(
            (frame for frame in reversed(self.frames) if isinstance(frame, ForEachFrame)), None
        )

    @property
    def item_index(self) -> int | None:
        frame = self.for_each_frame
        return frame.item_index if frame is not None else None

    def message_identity(self) -> dict[str, Any]:
        """The fields every step-scoped message carries for result acceptance.

        A step in a For each body also names the item it runs for, so the
        run history can tell one item's executions from another's.
        """
        identity: dict[str, Any] = {
            "step_id": self.step_id,
            "step_execution_id": self.step_execution_id,
            "attempt": self.attempt,
            "group_id": self.group_id,
            "for_each_id": self.for_each_id,
        }
        if self.for_each_id is not None and self.item_index is not None:
            identity["item_index"] = self.item_index
        return identity


def run_cursor(state: Mapping[str, Any]) -> RunCursor:
    return RunCursor.load(state.get(RUN_CURSOR_KEY))


def active_step(
    state: Mapping[str, Any],
    *,
    definition: FrozenDefinition | None = None,
) -> ActiveStep | None:
    """The step the cursor points at, or None before the first dispatch."""
    cursor = run_cursor(state)
    if not cursor.step_id or not cursor.step_execution_id:
        return None
    frozen = definition if definition is not None else load_run_definition(state)
    return ActiveStep(
        step=dict(frozen.step(cursor.step_id)),
        step_id=cursor.step_id,
        step_execution_id=cursor.step_execution_id,
        attempt=cursor.attempt,
        frames=tuple(cursor.frames),
    )


def step_message_fields(
    state: Mapping[str, Any],
    *,
    definition: FrozenDefinition | None = None,
) -> dict[str, Any]:
    """Identity keyword arguments for ``build_agent_message`` of the active step."""
    current = active_step(state, definition=definition)
    return current.message_identity() if current is not None else {}


def message_matches_execution(message: Mapping[str, Any], current: ActiveStep) -> bool:
    """Whether a message is the result of exactly this execution and attempt.

    An older result of the same step, or of the same agent on another step,
    never completes the current step.
    """
    try:
        attempt = int(message.get("attempt") or 0)
    except (TypeError, ValueError):
        return False
    return (
        str(message.get("step_execution_id") or "") == current.step_execution_id
        and str(message.get("step_id") or "") == current.step_id
        and attempt == current.attempt
    )


__all__ = [
    "RUN_CURSOR_KEY",
    "ActiveStep",
    "active_step",
    "message_matches_execution",
    "run_cursor",
    "step_message_fields",
]
