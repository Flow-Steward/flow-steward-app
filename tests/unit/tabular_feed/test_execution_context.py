from __future__ import annotations

from core.application.tabular_feed.context import context_from_state_and_step
from tests._step_run_state import step_run_state

_TRUSTED = {
    "trusted_runtime_context": {
        "account_id": "acct-1",
        "project_id": "proj-1",
        "workflow_id": "wf-1",
        "job_id": "job-1",
    }
}


def test_context_names_the_step_it_runs_and_no_phase() -> None:
    context = context_from_state_and_step(
        state=dict(_TRUSTED), step={"step_id": "feed", "step_kind": "tabular_feed"}
    )

    runtime = context.as_runtime_context()

    assert runtime["step_id"] == "feed"
    assert runtime["run_id"] == "job-1"
    assert "phase_id" not in runtime


def test_a_step_without_an_id_is_the_cursor_step() -> None:
    state = step_run_state({"step_id": "feed", "step_kind": "tabular_feed"}, **_TRUSTED)

    context = context_from_state_and_step(state=state, step={"step_kind": "tabular_feed"})

    assert context.step_id == "feed"
