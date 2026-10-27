from __future__ import annotations

from core.application.orchestration.graph.nodes_step_context import extract_task_context


def test_extract_task_context_prefers_initial_requirements_over_task_summary() -> None:
    context = extract_task_context(
        {
            "task_id": "task-1",
            "task_summary": "Bare task summary",
            "initial_requirements": "Operator-authored requirements",
            "container_root": "/repo",
        }
    )

    assert context["initial_requirements"] == "Operator-authored requirements"


def test_extract_task_context_falls_back_to_task_summary_without_initial_requirements() -> None:
    context = extract_task_context(
        {
            "task_id": "task-2",
            "task_summary": "Bare task summary",
            "container_root": "/repo",
        }
    )

    assert context["initial_requirements"] == "Bare task summary"


def test_extract_step_context_is_empty_before_the_first_dispatch() -> None:
    from core.application.orchestration.graph.nodes_step_context import extract_step_context

    assert extract_step_context({"workflow_id": "wf-1"}) == {}


def test_extract_step_context_names_the_cursor_execution() -> None:
    from core.application.orchestration.graph.nodes_step_context import extract_step_context
    from tests._step_run_state import step_run_state

    state = step_run_state(
        {"step_id": "draft", "step_kind": "agent", "display_name": "Draft"},
        attempt=2,
        workflow_id="wf-1",
    )

    context = extract_step_context(state)

    assert context["step_id"] == "draft"
    assert context["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert context["attempt"] == 2
    assert context["group_id"] is None
    assert context["step_display_name"] == "Draft"
    assert context["_step"]["step_kind"] == "agent"
