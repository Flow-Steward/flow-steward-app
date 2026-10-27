"""A final validated child contract outranks colliding diagnostic messages."""

from types import SimpleNamespace

import pytest

from core.application.orchestration.invoke_workflow_step_node_results import _collect_child_outputs


@pytest.mark.parametrize("outputs", [{"result": False}, {"result": None}, {"result": []}, {}])
def test_final_validated_output_projection_is_authoritative_even_for_absent_optional_fields(
    outputs,
):
    logger = SimpleNamespace(
        get_job=lambda _id: {"meta": {"workflow_outputs": {"ok": True, "outputs": outputs}}},
        get_agent_messages=lambda *_args, **_kwargs: [
            {"envelope": {"artifacts": {"result": True}}}
        ],
    )
    assert _collect_child_outputs(logger, "child", ["result"]) == outputs


def test_legacy_child_without_final_projection_uses_latest_message_output():
    logger = SimpleNamespace(
        get_job=lambda _id: {"meta": {}},
        get_agent_messages=lambda *_args, **_kwargs: [
            {"envelope": {"artifacts": {"result": False}}},
            {"envelope": {"artifacts": {"result": True}}},
        ],
    )
    assert _collect_child_outputs(logger, "child", ["result"]) == {"result": True}


@pytest.mark.parametrize(
    "artifacts", [{"report": {"artifact_id": "file-1"}, "only_file": {"artifact_id": "file-2"}}, {}]
)
def test_final_artifact_projection_uses_artifact_channel_without_output_collisions(artifacts):
    from unittest.mock import Mock

    from core.application.orchestration.invoke_workflow_step_node_results import (
        _collect_child_artifacts,
    )

    get_job = Mock(
        return_value={
            "meta": {
                "workflow_outputs": {
                    "ok": True,
                    "outputs": {"report": "scalar-output"},
                    "artifacts": artifacts,
                }
            }
        }
    )
    logger = SimpleNamespace(
        get_job=get_job,
        get_agent_messages=lambda *_args, **_kwargs: [
            {"envelope": {"artifacts": {"report": "stale", "only_file": "stale"}}}
        ],
    )
    assert _collect_child_artifacts(logger, "child", ["report", "only_file"]) == artifacts
    get_job.assert_called_once_with("child")
