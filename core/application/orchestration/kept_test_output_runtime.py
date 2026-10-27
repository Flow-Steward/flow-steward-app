"""A workflow test run returns a step's kept test output instead of running it.

The kept outputs are a snapshot the test-run submission took
(``workflow_test_run_kept_outputs``: step id → output target → value). When the
hub would dispatch such a step in a *workflow test run*, it records one passing
result of exactly that execution and attempt, with the kept values written to
their targets as the step's own output mapping would, and returns to the hub.
The hub then accepts the result like any other: guardrails, contract health and
the step's ``success`` edge. Real runs and step test runs never read the
snapshot, and steps whose effect is a decision or control flow always run.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.application.orchestration.active_step import ActiveStep
from core.application.workflows.workflow_kept_test_outputs import step_kind_keeps_output

KEPT_OUTPUTS_STATE_KEY = "workflow_test_run_kept_outputs"
KEPT_OUTPUT_SKILL = "kept_test_output"
KEPT_OUTPUT_AGENT = "kept_test_output"


def kept_output_for(state: Mapping[str, Any], current: ActiveStep) -> dict[str, Any] | None:
    """The kept values that stand in for *current* in this run, or None (it runs)."""
    if str(state.get("task_source") or "").strip().lower() != "workflow_test_run":
        return None
    snapshot = state.get(KEPT_OUTPUTS_STATE_KEY)
    if not isinstance(snapshot, Mapping):
        return None
    outputs = snapshot.get(current.step_id)
    if not isinstance(outputs, Mapping) or not outputs:
        return None
    if not step_kind_keeps_output(current.step_kind):
        return None
    return {str(key): copy.deepcopy(value) for key, value in outputs.items()}


def kept_output_result(
    state: Mapping[str, Any], current: ActiveStep, outputs: Mapping[str, Any], *, to_agent: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The state writes and the passing message a kept output stands in with."""
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.common import publish_agent_message
    from core.application.orchestration.step_templating import set_state_path

    updates: dict[str, Any] = {}
    working = dict(state)
    state_patch: dict[str, Any] = {}
    for target, value in outputs.items():
        set_state_path(updates, working, target, copy.deepcopy(value))
        working = {**working, **updates}
        # Readers that resolve a step's values from its result find them here too.
        key = target.removeprefix("state.")
        if key:
            state_patch[key] = copy.deepcopy(value)
    timestamp = datetime.now(UTC).isoformat()
    message = build_agent_message(
        from_agent=KEPT_OUTPUT_AGENT,
        to_agent=to_agent,
        skill=KEPT_OUTPUT_SKILL,
        status="pass",
        correlation_id=uuid.uuid4().hex,
        caused_by=[],
        artifacts={
            "step_kind": current.step_kind,
            "step_id": current.step_id,
            "kept_test_output": True,
            "output_payload": copy.deepcopy(dict(outputs)),
            "_state_patch": state_patch,
        },
        summary="Returned its kept test output",
        errors=[],
        started_at=timestamp,
        completed_at=timestamp,
        published_at=timestamp,
        **current.message_identity(),
    )
    publish_agent_message(dict(state), message)
    return updates, message


__all__ = [
    "KEPT_OUTPUTS_STATE_KEY",
    "KEPT_OUTPUT_SKILL",
    "kept_output_for",
    "kept_output_result",
]
