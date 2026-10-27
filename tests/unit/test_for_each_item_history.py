"""The run history tells one For each item's executions from another's (spec A17)."""

from __future__ import annotations

from core.application.orchestration.active_step import ActiveStep
from core.application.orchestration.agent_messages import build_agent_message
from core.application.orchestration.run_cursor import ForEachFrame, GroupFrame
from core.application.reports.ui_projection_helpers import _build_message_steps


def _step(for_each_id: str | None, frames: tuple) -> ActiveStep:
    step = {"step_id": "keep", "step_kind": "collect", "for_each_id": for_each_id}
    return ActiveStep(
        step=step,
        step_id="keep",
        step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
        attempt=1,
        frames=frames,
    )


def _for_each_frame(index: int) -> ForEachFrame:
    return ForEachFrame(
        for_each_id="each_order", for_each_execution_id="fe-1", selected_count=4, item_index=index
    )


def test_a_step_in_a_for_each_names_the_item_it_runs_for() -> None:
    current = _step("each_order", (GroupFrame("process", "g-1"), _for_each_frame(2)))

    identity = current.message_identity()
    assert identity["for_each_id"] == "each_order"
    assert identity["item_index"] == 2
    message = build_agent_message(
        from_agent="control_flow_step",
        to_agent="orchestrator",
        skill="collect",
        status="pass",
        correlation_id="c-1",
        published_at="2026-10-16T00:00:00Z",
        **identity,
    )
    assert message["item_index"] == 2


def test_a_step_outside_a_for_each_keeps_its_plain_identity() -> None:
    current = _step(None, (GroupFrame("process", "g-1"),))

    assert "item_index" not in current.message_identity()


def test_the_run_view_titles_each_execution_with_its_item() -> None:
    rows = _build_message_steps(
        [
            {
                "id": f"m{index}",
                "step_id": "keep",
                "for_each_id": "each_order",
                "from_agent": "control_flow_step",
                "status": "pass",
                "artifacts": {"step_display_name": "Keep email", "step_kind": "collect"},
                "envelope": {"item_index": index},
                "timestamp": f"2026-10-16T00:00:0{index}Z",
            }
            for index in (0, 2)
        ]
    )

    assert [row["title"] for row in rows] == [
        "Each Order #1: Keep email",
        "Each Order #3: Keep email",
    ]
    assert [row["item_number"] for row in rows] == [1, 3]
    assert rows[0]["for_each_id"] == "each_order"
