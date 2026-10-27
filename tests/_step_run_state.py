"""Run states for step executor tests: a primed pinned definition and a cursor.

An executor learns its step only through ``active_step(state)``, which reads the
run cursor and the run's pinned definition. ``step_run_state`` pins a small
definition holding the step under test (plus any sibling steps), primes it in
the run-definition cache and points a fresh cursor at the step.
"""

from __future__ import annotations

from typing import Any

from core.application.orchestration.run_cursor import (
    RUNTIME_SCHEMA_VERSION,
    Frame,
    RunCursor,
    begin_step,
)
from core.application.orchestration.run_definition import prime_run_definition
from core.application.workflows.workflow_definition_snapshot import definition_digest
from tests._workflow_definitions import linear_graph


def pinned_definition(
    *steps: dict[str, Any],
    groups: list[dict[str, Any]] | None = None,
    for_each: list[dict[str, Any]] | None = None,
) -> dict:
    ordered = [dict(step) for step in steps]
    for step in ordered:
        step.setdefault("group_id", None)
        step.setdefault("for_each_id", None)
        step.setdefault("lane_id", None)
    return {
        "workflow_id": "test_workflow",
        "steps": ordered,
        "groups": list(groups or []),
        "for_each": list(for_each or []),
        "execution_graph": linear_graph([step["step_id"] for step in ordered]),
        "settings": {},
    }


def prime(definition: dict[str, Any]) -> dict[str, Any]:
    """Prime *definition* and return the state keys that pin it to a run."""
    digest = definition_digest(definition)
    ref = {"snapshot_id": f"snap-{digest[:16]}", "revision": 1, "digest": digest}
    prime_run_definition(ref, definition)
    return {"runtime_schema_version": RUNTIME_SCHEMA_VERSION, "workflow_definition_ref": ref}


def step_run_state(
    step: dict[str, Any],
    *others: dict[str, Any],
    frame: Frame | None = None,
    attempt: int = 1,
    step_execution_id: str | None = None,
    groups: list[dict[str, Any]] | None = None,
    for_each: list[dict[str, Any]] | None = None,
    **state: Any,
) -> dict[str, Any]:
    """A run state whose cursor points at the first execution of *step*."""
    step = {"step_id": "step_1", **step}
    definition = pinned_definition(step, *others, groups=groups, for_each=for_each)
    cursor = begin_step(
        RunCursor(frames=[frame] if frame is not None else []),
        str(step["step_id"]),
        **({"id_factory": lambda: step_execution_id} if step_execution_id else {}),
    )
    cursor.attempt = attempt
    return {
        "agent_messages": [],
        **prime(definition),
        "run_cursor": cursor.dump(),
        "current_step_id": cursor.step_id,
        **state,
    }


__all__ = ["pinned_definition", "prime", "step_run_state"]
