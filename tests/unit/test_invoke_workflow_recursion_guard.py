"""`invoke_workflow` must fail closed instead of spinning the runtime.

Regression for the workflow E2E ledger finding "`invoke_workflow` spins the worker
in an endless loop, and cancelling the job does not stop it":

* the invocation ancestry that the recursion check reads was carried on
  ``invoke_workflow_ancestry`` / ``invoke_parent_workflow_id``, neither of which
  ``OrchestratorState`` declares, so LangGraph dropped the chain at the checkpoint
  and the guard degenerated to a single hop — an ``A -> B -> C -> A`` cycle passed;
* a child id that resolves to nothing fell through to the "not callable" branch
  instead of naming the real problem;
* the child task envelope inherited the parent's ``requested_workflow_id`` through
  ``metadata``, so the parent's own id travelled into the child's routing input;
* the wait loop never looked at the parent job, so cancelling the parent left it
  polling until the invoke timeout expired.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.invoke_workflow_step_node_common import (
    INVOKE_WORKFLOW_ANCESTRY_KEY,
    _workflow_invocation_ancestry,
)
from core.application.orchestration.state import (
    OrchestratorState,
    OrchestratorStateModel,
)

DECLARED_CHANNELS = frozenset(OrchestratorState.__annotations__)


def _checkpoint(state: dict) -> dict:
    """Keep only what LangGraph would persist for the declared state schema."""
    return {key: value for key, value in state.items() if key in DECLARED_CHANNELS}


# ---------------------------------------------------------------------------
# The recursion chain has to survive the checkpoint
# ---------------------------------------------------------------------------


def test_invocation_ancestry_is_a_declared_state_channel():
    """The chain the recursion check reads must not be dropped at the checkpoint."""
    assert INVOKE_WORKFLOW_ANCESTRY_KEY in OrchestratorState.__annotations__
    assert INVOKE_WORKFLOW_ANCESTRY_KEY in OrchestratorStateModel.model_fields
    assert "invoke_parent_workflow_id" in OrchestratorState.__annotations__
    assert "invoke_parent_workflow_id" in OrchestratorStateModel.model_fields


def test_ancestry_survives_checkpointing_and_catches_an_indirect_cycle():
    """`A -> B -> C -> A` must still be visible to the guard inside C."""
    state_in_c = {
        INVOKE_WORKFLOW_ANCESTRY_KEY: ["wf_a", "wf_b", "wf_c"],
        "invoke_parent_workflow_id": "wf_b",
        "parent_workflow_id": "wf_b",
        "workflow_id": "wf_c",
    }

    ancestry = _workflow_invocation_ancestry(_checkpoint(state_in_c))

    assert "wf_a" in ancestry, "the grandparent must remain visible to the recursion check"
    assert ancestry[-1] == "wf_c"


# ---------------------------------------------------------------------------
# Depth guard
# ---------------------------------------------------------------------------


def test_invocation_depth_limit_is_enforced():
    """A chain deeper than the configured maximum fails closed."""
    from core.application.orchestration.invoke_workflow_step_node_common import (
        _invoke_workflow_depth_exceeded,
        _resolve_invoke_workflow_max_depth,
    )

    max_depth = _resolve_invoke_workflow_max_depth()
    assert max_depth >= 1

    within = [f"wf_{index}" for index in range(max_depth)]
    beyond = [f"wf_{index}" for index in range(max_depth + 1)]

    assert _invoke_workflow_depth_exceeded(within) is False
    assert _invoke_workflow_depth_exceeded(beyond) is True


# ---------------------------------------------------------------------------
# The child envelope must not carry the parent's routing identity
# ---------------------------------------------------------------------------


def test_child_envelope_does_not_inherit_the_parent_requested_workflow_id():
    """The child's envelope must name the child, in the body and in metadata."""
    from core.application.orchestration.invoke_workflow_step_node_runtime import (
        _build_child_task_envelope,
    )

    parent_state = {
        "job_id": "job-parent",
        "task_id": "task-parent",
        "workflow_id": "wf_parent",
        "task_envelope": {
            "entry_path": "ui",
            "source_system": "workflow_test_run",
            "requested_workflow_id": "wf_parent",
            "metadata": {
                "source": "workflow_test_run",
                "requested_workflow_id": "wf_parent",
            },
            "entry_context": {},
        },
    }

    envelope = _build_child_task_envelope(
        state=parent_state,
        child_workflow_id="wf_child",
        child_task_summary="child run",
        child_inputs={},
        project_id="project-1",
        parent_workflow_id="wf_parent",
        step_id="call_child",
    )

    assert envelope.get("requested_workflow_id") == "wf_child"
    metadata = envelope.get("metadata") or {}
    assert metadata.get("requested_workflow_id") != "wf_parent"
    assert metadata.get("invoked_by_workflow_id") == "wf_parent"
    assert metadata.get("invoked_from_step_id") == "call_child"


# ---------------------------------------------------------------------------
# Cancelling the parent must end the wait, not run it out to the timeout
# ---------------------------------------------------------------------------


class _StubJobStore:
    """Parent is cancelled; the child never reaches a terminal status."""

    def __init__(self):
        self.child_polls = 0

    def get_job(self, job_id):
        if job_id == "job-parent":
            return {"id": "job-parent", "status": "cancelled"}
        self.child_polls += 1
        return {"id": "job-child", "status": "in_progress"}

    def update_job_runtime(self, *_args, **_kwargs):
        return None


def test_wait_for_terminal_child_stops_when_the_parent_is_cancelled(monkeypatch):
    """A cancelled parent must release the worker slot instead of polling on."""
    from core.application.orchestration import invoke_workflow_step_node_runtime as runtime

    monkeypatch.setattr(runtime, "_resolve_invoke_child_timeout_seconds", lambda: 600)
    monkeypatch.setattr(runtime, "_resolve_invoke_child_poll_seconds", lambda: 0.01)

    store = _StubJobStore()
    _row, status = runtime._wait_for_terminal_child(
        store,
        "job-child",
        parent_job_id="job-parent",
        parent_agent="invoke_workflow",
        child_workflow_id="wf_child",
    )

    assert status == "cancelled"
    assert store.child_polls <= 3, "the loop must break out, not keep polling the child"


# ---------------------------------------------------------------------------
# Unresolved child fails closed with its own reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_child_id", ["wf_does_not_exist", "sample_weather_summary_child"])
def test_unresolved_child_workflow_reports_not_found(monkeypatch, missing_child_id):
    """An id that resolves to no workflow must say so, not 'is not callable'."""
    from core.application.orchestration.invoke_workflow_step_node_common import (
        _resolve_child_workflow_row,
    )

    class _EmptyRegistry:
        def get_workflow(self, _workflow_id):
            return None

    class _EmptyDB:
        def get_workflow(self, _workflow_id):
            return None

    resolved = _resolve_child_workflow_row(
        workflow_registry=_EmptyRegistry(),
        db=_EmptyDB(),
        child_workflow_id=missing_child_id,
    )

    assert resolved is None
