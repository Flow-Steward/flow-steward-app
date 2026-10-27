from __future__ import annotations

from core.application.orchestration.run_cursor import ForEachFrame
from tests._step_run_state import step_run_state

_TRUSTED = {
    "trusted_runtime_context": {
        "job_id": "job-1",
        "workflow_id": "workflow-1",
        "project_id": "project-1",
    }
}


def _item_frame(index: int, for_each_id: str = "push") -> ForEachFrame:
    return ForEachFrame(
        for_each_id=for_each_id,
        for_each_execution_id="8f1d6c3e-2c49-4f0b-9d55-1b0e0e9f1a10",
        selected_count=10,
        item_index=index,
    )


_PUSH = [{"for_each_id": "push", "over": "state.rows", "item_name": "row", "body": {}}]


def test_for_each_identity_names_the_container_and_the_item_position():
    """Each item claims its own external effect; the same item keeps its claim."""
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    def identity(index: int):
        return runtime_identity_from_state(
            step_run_state(
                {"step_id": "send", "step_kind": "connector", "for_each_id": "push"},
                frame=_item_frame(index),
                for_each=_PUSH,
                **_TRUSTED,
            )
        ).as_context()["loop"]

    first = identity(7)

    assert first == {"for_each_id": "push", "index": 7}
    assert identity(7) == first
    assert identity(8) != first


def test_outside_a_for_each_there_is_no_item_identity():
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    state = step_run_state({"step_id": "send", "step_kind": "connector"}, **_TRUSTED)
    assert runtime_identity_from_state(state).as_context()["loop"] == {}


def test_identity_names_the_cursor_step_execution_and_has_no_phase():
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    state = step_run_state({"step_id": "send", "step_kind": "connector"}, attempt=2, **_TRUSTED)

    context = runtime_identity_from_state(state).as_context()

    assert context["step_id"] == "send"
    assert context["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert context["attempt"] == 2
    assert context["group_id"] == ""
    assert context["loop"] == {}
    assert "phase_id" not in context


def test_identity_before_the_first_dispatch_has_no_step():
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    identity = runtime_identity_from_state(dict(_TRUSTED))

    assert identity.job_id == "job-1"
    assert identity.missing("step_id") == ("step_id",)
    assert identity.as_context()["loop"] == {}
