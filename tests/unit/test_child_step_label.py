"""A child workflow run inside its parent's job marks its steps as the child's."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.application.reports.ui_projection_helpers import _build_message_steps


def _publish(state: dict) -> dict:
    from core.application.orchestration.agents.common import publish_agent_message

    envelope = {"from_agent": "connector_step", "artifacts": {"step_display_name": "Shout"}}
    bus = MagicMock()
    bus.publish.return_value = True
    with (
        patch(
            "core.application.orchestration.agents.common.resolve_security_scope",
            return_value=("project-1", "job-1"),
        ),
        patch("core.application.orchestration.message_bus.MessageBus.for_state", return_value=bus),
    ):
        assert publish_agent_message(state, envelope)
    return envelope


def test_a_child_step_in_the_parent_job_is_marked_with_the_child_name() -> None:
    envelope = _publish({"invoke_child_label": "Invoice child", "invoke_parent_job_id": "job-1"})

    assert envelope["artifacts"]["child_workflow"] == "Invoice child"


def test_a_child_running_as_its_own_job_is_not_marked() -> None:
    envelope = _publish({"invoke_child_label": "Invoice child", "invoke_parent_job_id": "job-0"})

    assert "child_workflow" not in envelope["artifacts"]


def test_the_run_view_titles_a_child_step_with_the_child_name() -> None:
    step = _build_message_steps(
        [
            {
                "id": "m1",
                "step_id": "shout",
                "from_agent": "connector_step",
                "status": "pass",
                "artifacts": {"step_display_name": "Shout", "child_workflow": "Invoice child"},
            }
        ]
    )[0]

    assert step["title"] == "Invoice child / Shout"
