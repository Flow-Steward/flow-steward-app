from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.orchestration import step_replay_runtime
from core.application.orchestration.run_cursor import RunCursorError, seeded_step_cursor
from core.application.orchestration.run_definition import (
    RunDefinitionIntegrityError,
    clear_run_definition_cache,
)
from core.application.workflows.workflow_definition_snapshot import (
    definition_digest,
    frozen_definition,
)
from tests._workflow_definitions import linear_workflow


@pytest.fixture
def pinned_job(monkeypatch):
    """A job whose pinned archive row holds a two-step definition."""
    from core.application.jobs import workflow_definition_snapshot_repository as repository

    definition = frozen_definition(
        linear_workflow(
            {"step_id": "draft", "step_kind": "agent", "agent_id": "composer"},
            workflow_id="wf-replay",
        )
    )
    row = {
        "id": "snapshot-1",
        "revision": 3,
        "digest": definition_digest(definition),
        "definition_jsonb": definition,
    }
    loaded: list[str] = []

    class _Repository:
        def __init__(self, conn) -> None:
            assert conn == "conn"

        def load_for_job(self, job_id):
            loaded.append(job_id)
            return row if job_id == "42" else None

    monkeypatch.setattr(repository, "WorkflowDefinitionSnapshotRepository", _Repository)
    clear_run_definition_cache()
    yield row, loaded
    clear_run_definition_cache()


def test_extract_replay_prompt_capture_reads_prompt_and_meta() -> None:
    payload = step_replay_runtime.extract_replay_prompt_capture(
        {
            "prompt_request": {
                "payload": {
                    "prompt_version": "v1",
                    "system_prompt": "You are composer",
                    "user_prompt": "Implement feature",
                },
                "meta": {
                    "provider": "OpenAI",
                    "skill": "write_section",
                    "step_id": "implementation",
                    "correlation_id": "corr-1",
                },
                "model_used": "gpt-4o-mini",
            }
        }
    )

    assert payload["prompt_version"] == "v1"
    assert payload["provider"] == "openai"
    assert payload["skill"] == "write_section"
    assert payload["step_id"] == "implementation"
    assert "phase_id" not in payload
    assert payload["model_used"] == "gpt-4o-mini"


def test_derive_original_replay_outcome_prefers_excerpt_and_raw_output() -> None:
    payload = step_replay_runtime.derive_original_replay_outcome(
        {
            "message": {
                "status": "pass",
                "summary": "implemented",
                "artifacts": {"raw_output": "original output"},
            },
            "prompt_response": {
                "result": {"excerpt": "excerpted output"},
            },
        }
    )

    assert payload["message_status"] == "pass"
    assert payload["message_summary"] == "implemented"
    assert payload["response_text"] == "original output"
    assert payload["response_excerpt"] == "excerpted output"


def test_build_replay_diff_excerpt_returns_truncated_unified_diff() -> None:
    diff_excerpt = step_replay_runtime.build_replay_diff_excerpt(
        "line-1\nline-2",
        "line-1\nline-2-updated",
    )

    assert "--- original" in diff_excerpt
    assert "+++ replayed" in diff_excerpt
    assert "-line-2" in diff_excerpt
    assert "+line-2-updated" in diff_excerpt


def test_build_runtime_invokers_binds_shared_runtime_dependencies(monkeypatch) -> None:
    replay_mock = MagicMock(return_value={"mode": "replay"})
    reexecute_mock = MagicMock(return_value={"mode": "reexecute"})
    monkeypatch.setattr(step_replay_runtime, "invoke_step_replay_llm", replay_mock)
    monkeypatch.setattr(step_replay_runtime, "invoke_step_reexecute_runtime", reexecute_mock)

    invoke_replay, invoke_reexecute = step_replay_runtime.build_runtime_invokers(
        policy_snapshot_hash=lambda value: "hash",
        to_json_obj=lambda value: value,
        serialize_agent_message_row=lambda row: row,
    )

    replay_result = invoke_replay(
        project_config={},
        agent_name="composer",
        provider="openai",
        model="gpt-5-mini",
        system_prompt="system",
        user_prompt="user",
    )
    reexecute_result = invoke_reexecute(
        bl=MagicMock(),
        job={"id": 1},
        message={"from_agent": "composer", "skill": "build"},
        trace_payload={},
        project_config={},
    )

    assert replay_result == {"mode": "replay"}
    assert reexecute_result == {"mode": "reexecute"}
    assert replay_mock.call_args.kwargs["policy_snapshot_hash"]("x") == "hash"
    assert reexecute_mock.call_args.kwargs["policy_snapshot_hash"]("x") == "hash"


def test_replay_seeds_a_cursor_at_the_step_of_the_jobs_pinned_definition(pinned_job) -> None:
    from core.application.orchestration.active_step import active_step

    row, loaded = pinned_job
    bl = MagicMock(conn="conn")

    state = step_replay_runtime._replay_definition_state(
        bl, {"id": 42}, "draft", replayed_message_id="m-7"
    )

    assert loaded == ["42"]
    assert state["runtime_schema_version"] == 2
    assert state["workflow_definition_ref"] == {
        "snapshot_id": "snapshot-1",
        "revision": 3,
        "digest": row["digest"],
    }
    assert state["run_cursor"] == seeded_step_cursor("draft", seed="replay:42:m-7").dump()
    assert state["current_step_id"] == "draft"
    current = active_step(state)
    assert current is not None
    assert current.step["agent_id"] == "composer"


def test_replay_refuses_a_job_without_a_pinned_definition(pinned_job) -> None:
    bl = MagicMock(conn="conn")
    with pytest.raises(RunDefinitionIntegrityError):
        step_replay_runtime._replay_definition_state(
            bl, {"id": 7}, "draft", replayed_message_id="m-1"
        )


def test_replay_refuses_a_step_the_pinned_definition_does_not_have(pinned_job) -> None:
    bl = MagicMock(conn="conn")
    with pytest.raises(RunCursorError):
        step_replay_runtime._replay_definition_state(
            bl, {"id": 42}, "gone", replayed_message_id="m-1"
        )


def test_reexecute_requires_the_replayed_step_identity() -> None:
    bl = MagicMock()
    with pytest.raises(ValueError, match="missing step_id"):
        step_replay_runtime.invoke_step_reexecute_runtime(
            bl=bl,
            job={"id": 42},
            message={"from_agent": "composer", "skill": "build"},
            trace_payload={},
            project_config={},
            policy_snapshot_hash=lambda value: "",
            to_json_obj=lambda value: value,
            serialize_agent_message_row=lambda row: row,
        )
