from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_on_chain_error_treats_graph_interrupt_as_control_flow():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=42)

    err = RuntimeError(
        "langgraph.errors.GraphInterrupt: (Interrupt(value={'stage': 'Review'}, id='abc123'),)"
    )
    callback.on_chain_error(error=err, name="human_approval_gate")

    fake_logger.log_action.assert_called_once()
    _, kwargs = fake_logger.log_action.call_args
    assert kwargs["action_type"] == "node_interrupt"
    assert kwargs["status"] == "awaiting_approval"
    assert kwargs["job_id"] == 42
    assert kwargs["result"] == {"interrupt_code": "graph_interrupt"}
    assert "Review" not in str(kwargs)
    assert "abc123" not in str(kwargs)

    fake_logger.update_job_runtime.assert_called_once()
    _, runtime_kwargs = fake_logger.update_job_runtime.call_args
    assert runtime_kwargs["clear_last_error"] is True


_EXECUTION_ID = "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"


def _composer_run_state(**state):
    from tests._step_run_state import step_run_state

    return step_run_state(
        {"step_id": "configured-step", "step_kind": "agent", "agent": "composer"},
        step_execution_id=_EXECUTION_ID,
        dispatched_step_execution={
            "step_id": "configured-step",
            "step_execution_id": _EXECUTION_ID,
            "attempt": 1,
        },
        **state,
    )


def _progress_logger(*, steps=None) -> MagicMock:
    fake_logger = MagicMock()
    fake_logger.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "selected_workflow": "workflow-1",
        "status": "in_progress",
        "started_at": None,
        "queued_at": None,
    }
    fake_logger.get_public_progress_snapshot.return_value = SimpleNamespace(
        snapshot=SimpleNamespace(steps=list(steps or []))
    )
    return fake_logger


def test_callback_uses_cursor_step_and_durable_occurrence_after_restart() -> None:
    from core.application.jobs.workflow_run_models import PublicWorkflowStepV1
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    earlier = PublicWorkflowStepV1(
        step_id="configured-step",
        step_execution_id="0b0c5f0e-7a51-4c1b-8f8e-2b8c4f9a6d10",
        occurrence=4,
        step_kind="node",
        status="completed",
    )
    fake_logger = _progress_logger(steps=[earlier])

    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    callback.on_chain_start(run_id="run-new", name="composer", inputs=_composer_run_state())

    step = fake_logger.record_public_progress_transition.call_args.kwargs["step"]
    assert step["step_id"] == "configured-step"
    assert step["step_execution_id"] == _EXECUTION_ID
    assert step["occurrence"] == 5


def test_callback_retry_of_the_same_execution_keeps_its_durable_occurrence() -> None:
    from core.application.jobs.workflow_run_models import PublicWorkflowStepV1
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    recorded = PublicWorkflowStepV1(
        step_id="configured-step",
        step_execution_id=_EXECUTION_ID,
        occurrence=2,
        step_kind="node",
        status="failed",
    )
    fake_logger = _progress_logger(steps=[recorded])

    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    callback.on_chain_start(run_id="retry", name="composer", inputs=_composer_run_state())

    step = fake_logger.record_public_progress_transition.call_args.kwargs["step"]
    assert (step["step_execution_id"], step["occurrence"]) == (_EXECUTION_ID, 2)


def test_callback_end_reuses_cursor_step_identity_from_start() -> None:
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    callback.on_chain_start(run_id="run-1", name="composer", inputs=_composer_run_state())
    callback.on_chain_end(run_id="run-1", name="composer", outputs={})

    transitions = [
        call.kwargs["step"] for call in fake_logger.record_public_progress_transition.call_args_list
    ]
    assert [
        (step["step_id"], step["step_execution_id"], step["occurrence"], step["status"])
        for step in transitions
    ] == [
        ("configured-step", _EXECUTION_ID, 1, "in_progress"),
        ("configured-step", _EXECUTION_ID, 1, "completed"),
    ]


@pytest.mark.parametrize("outcome", ["end", "error"])
def test_callback_end_keeps_the_for_each_item_and_lane_captured_at_start(outcome) -> None:
    # A node's end/error event carries only its partial update, never the run
    # cursor: the finished row must keep the For each item, lane and attempt the
    # start saw, or the run history loses which item the step ran for.
    from core.application.orchestration.run_cursor import ForEachFrame
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    inputs = _composer_run_state(current_lane_id="lane-a")
    inputs["run_cursor"]["attempt"] = 2
    inputs["run_cursor"]["frames"] = [
        ForEachFrame(
            for_each_id="for_each_1",
            for_each_execution_id="fe-exec-1",
            selected_count=2,
            item_index=1,
        ).dump()
    ]
    inputs["dispatched_step_execution"]["attempt"] = 2
    callback.on_chain_start(run_id="run-1", name="composer", inputs=inputs)
    if outcome == "end":
        callback.on_chain_end(run_id="run-1", name="composer", outputs={})
    else:
        callback.on_chain_error(error=RuntimeError("boom"), run_id="run-1")

    transitions = [
        call.kwargs["step"] for call in fake_logger.record_public_progress_transition.call_args_list
    ]
    expected_status = "completed" if outcome == "end" else "failed"
    assert [
        (
            step["status"],
            step.get("for_each_id"),
            step.get("iteration"),
            step.get("lane_id"),
            step.get("attempt"),
        )
        for step in transitions
    ] == [
        ("in_progress", "for_each_1", 1, "lane-a", 2),
        (expected_status, "for_each_1", 1, "lane-a", 2),
    ]


def test_callback_end_without_start_initializes_a_public_step_occurrence() -> None:
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")

    callback.on_chain_end(
        run_id="orphan-run",
        name="internal-node",
        outputs={"current_step_id": "build"},
    )

    step = fake_logger.record_public_progress_transition.call_args.kwargs["step"]
    assert step["step_id"] == "internal-node"
    assert step["step_execution_id"] is None
    assert step["occurrence"] == 1
    assert step["status"] == "completed"


def test_callback_records_strict_repeated_public_step_occurrences() -> None:
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")

    callback.on_chain_start(run_id="run-1", name="composer", inputs={})
    callback.on_chain_end(run_id="run-1", name="composer", outputs={})
    callback.on_chain_start(run_id="run-2", name="composer", inputs={})

    transitions = [
        call.kwargs for call in fake_logger.record_public_progress_transition.call_args_list
    ]
    assert [item["step"]["occurrence"] for item in transitions] == [1, 1, 2]
    assert [item["step"]["status"] for item in transitions] == [
        "in_progress",
        "completed",
        "in_progress",
    ]


def test_callback_does_not_project_orchestrator_pass_as_a_second_step_occurrence() -> None:
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    payload = _composer_run_state()

    callback.on_chain_start(run_id="agent-run", name="composer", inputs=payload)
    callback.on_chain_end(run_id="agent-run", name="composer", outputs=payload)
    callback.on_chain_start(run_id="hub-run", name="orchestrator", inputs=payload)
    callback.on_chain_end(
        run_id="hub-run",
        name="orchestrator",
        outputs={"current_step_id": "configured-step"},
    )

    transitions = [
        call.kwargs["step"] for call in fake_logger.record_public_progress_transition.call_args_list
    ]
    assert [(step["step_id"], step["occurrence"], step["status"]) for step in transitions] == [
        ("configured-step", 1, "in_progress"),
        ("configured-step", 1, "completed"),
    ]


def test_callback_mirrors_the_cursor_step_scope_into_the_job_row() -> None:
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    callback.on_chain_end(
        run_id="hub-run",
        name="orchestrator",
        outputs={"current_step_id": "draft", "current_group_id": "per_item"},
    )

    runtime_kwargs = fake_logger.update_job_runtime.call_args.kwargs
    assert runtime_kwargs["current_step_id"] == "draft"
    assert runtime_kwargs["current_group_id"] == "per_item"


def test_on_chain_error_logs_non_interrupt_as_error():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=43)

    callback.on_chain_error(error=RuntimeError("boom"), name="designer")

    fake_logger.log_action.assert_called_once()
    _, kwargs = fake_logger.log_action.call_args
    assert kwargs["action_type"] == "node_error"
    assert kwargs["status"] == "error"
    assert kwargs["job_id"] == 43
    assert "RuntimeError: boom" in kwargs["error"]

    fake_logger.update_job_runtime.assert_called_once()
    _, runtime_kwargs = fake_logger.update_job_runtime.call_args
    assert "RuntimeError: boom" in runtime_kwargs["last_error"]


def test_on_chain_error_exposes_failed_public_step_identity_for_terminal_persistence():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    callback.on_chain_start(run_id="run-1", name="composer", inputs=_composer_run_state())

    callback.on_chain_error(error=RuntimeError("boom"), run_id="run-1")

    assert callback.terminal_error_context() == {
        "step_id": "configured-step",
        "step_execution_id": _EXECUTION_ID,
        "group_id": None,
        "occurrence": 1,
    }
    structured_error = fake_logger.log_job_error.call_args.kwargs
    assert structured_error["step_id"] == "configured-step"
    assert structured_error["step_execution_id"] == _EXECUTION_ID
    assert structured_error["group_id"] is None
    assert structured_error["context_jsonb"]["occurrence"] == 1


def test_callback_emits_trace_linkage_meta_for_node_and_llm_paths():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=44)

    callback.on_chain_start(run_id="root-run", name="LangGraph")
    callback.on_chain_start(
        run_id="node-run",
        parent_run_id="root-run",
        name="composer",
        inputs={"current_step_id": "delivery"},
    )
    callback.on_chain_end(
        run_id="node-run",
        name="composer",
        outputs={
            "task_id": "task-44",
            "task_summary": "Trace test",
            "workflow_id": "wf-1",
            "current_step_id": "delivery",
        },
    )
    callback.on_llm_end(
        response=MagicMock(
            llm_output={"token_usage": {"prompt_tokens": 3, "completion_tokens": 2}}, generations=[]
        ),
        run_id="llm-run",
        parent_run_id="node-run",
        name="chat",
    )

    log_calls = fake_logger.log_action.call_args_list
    assert len(log_calls) >= 2

    node_meta = log_calls[0].kwargs["meta"]
    assert node_meta["trace_id"] == "job:44"
    assert node_meta["span_kind"] == "node"
    assert node_meta["run_id"] == "node-run"
    assert node_meta["parent_run_id"] == "root-run"
    assert node_meta["root_run_id"] == "root-run"

    llm_meta = log_calls[-1].kwargs["meta"]
    assert llm_meta["trace_id"] == "job:44"
    assert llm_meta["span_kind"] == "llm"
    assert llm_meta["run_id"] == "llm-run"
    assert llm_meta["parent_run_id"] == "node-run"
    assert llm_meta["root_run_id"] == "root-run"


def test_on_llm_end_reads_cache_tokens_from_response_metadata_fallback():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=45)

    message = SimpleNamespace(
        usage_metadata={"input_tokens": 10, "output_tokens": 5},
        response_metadata={
            "usage": {
                "cache_creation_input_tokens": 4,
                "cache_read_input_tokens": 3,
            }
        },
    )
    response = SimpleNamespace(
        llm_output={},
        generations=[[SimpleNamespace(message=message)]],
    )

    callback.on_llm_end(response=response, run_id="llm-run", parent_run_id="node-run", name="chat")

    fake_logger.log_action.assert_called_once()
    _, kwargs = fake_logger.log_action.call_args
    assert kwargs["action_type"] == "llm_call"
    assert kwargs["tokens_prompt"] == 10
    assert kwargs["tokens_completion"] == 5
    assert kwargs["tokens_total"] == 22
    assert kwargs["model_used"] == "chat"


def test_on_llm_end_prices_exact_provider_and_model_from_start_metadata():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=46)
    callback.on_chat_model_start(
        {},
        [[SimpleNamespace(content="hello")]],
        run_id="priced-run",
        metadata={"flow_steward_provider": "openai"},
    )
    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={
                "model_name": "gpt-5.6-sol",
                "token_usage": {"prompt_tokens": 1_000, "completion_tokens": 1_000},
            },
            generations=[],
        ),
        run_id="priced-run",
    )

    assert fake_logger.log_action.call_args.kwargs["cost_usd"] == 0.035


def test_on_llm_end_prices_custom_route_by_provider_policy_and_preserves_identity():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=4600)
    callback.on_chat_model_start(
        {},
        [[SimpleNamespace(content="hello")]],
        run_id="custom-priced-run",
        metadata={
            "flow_steward_provider": "custom:provider-123",
            "flow_steward_provider_policy": "openai",
            "flow_steward_model": "gpt-5.6-luna",
        },
    )
    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 30_901,
                    "completion_tokens": 14_207,
                }
            },
            generations=[],
        ),
        run_id="custom-priced-run",
    )

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["cost_usd"] == 0.116143
    assert logged["meta"]["provider_identity"] == "custom:provider-123"
    assert logged["meta"]["provider_type"] == "openai"


def test_on_llm_end_persists_model_from_start_when_provider_response_omits_it():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=460)
    callback.on_chat_model_start(
        {},
        [[SimpleNamespace(content="hello")]],
        run_id="model-from-start",
        metadata={
            "flow_steward_provider": "openai",
            "flow_steward_model": "gpt-5.6-sol",
        },
    )
    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={
                "token_usage": {"prompt_tokens": 1_000, "completion_tokens": 1_000},
            },
            generations=[],
        ),
        run_id="model-from-start",
    )

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["model_used"] == "gpt-5.6-sol"
    assert logged["cost_usd"] == 0.035


def test_on_llm_end_persists_explicit_zero_for_known_priced_model_without_tokens():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=461)
    callback.on_chat_model_start(
        {},
        [[SimpleNamespace(content="hello")]],
        run_id="known-zero-run",
        metadata={
            "flow_steward_provider": "openai",
            "flow_steward_model": "gpt-5.6-sol",
        },
    )
    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={"token_usage": {"prompt_tokens": 0, "completion_tokens": 0}},
            generations=[],
        ),
        run_id="known-zero-run",
    )

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["model_used"] == "gpt-5.6-sol"
    assert logged["cost_usd"] == 0.0


def test_on_llm_end_keeps_unpriced_model_cost_unavailable():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=462)
    callback.on_chat_model_start(
        {},
        [[SimpleNamespace(content="hello")]],
        run_id="unknown-cost-run",
        metadata={
            "flow_steward_provider": "openai",
            "flow_steward_model": "unpriced-future-model",
        },
    )
    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}},
            generations=[],
        ),
        run_id="unknown-cost-run",
    )

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["model_used"] == "unpriced-future-model"
    assert logged["cost_usd"] is None


def test_on_llm_end_treats_openai_cached_tokens_as_prompt_subset():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=47)
    callback.on_chat_model_start(
        {},
        [[SimpleNamespace(content="hello")]],
        run_id="cached-run",
        metadata={"flow_steward_provider": "openai"},
    )
    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={
                "model_name": "gpt-5.6-sol",
                "token_usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 0,
                    "total_tokens": 1_000,
                    "prompt_tokens_details": {"cached_tokens": 1_000},
                },
            },
            generations=[],
        ),
        run_id="cached-run",
    )

    logged = fake_logger.log_action.call_args.kwargs
    assert logged["tokens_prompt"] == 1_000
    assert logged["tokens_total"] == 1_000
    assert logged["cost_usd"] == 0.0005


def test_on_llm_end_prefers_finite_nonnegative_provider_reported_cost():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    callback = PlatformLoggerCallback(fake_logger, project_id=1, job_id=48)
    callback.on_llm_end(
        response=SimpleNamespace(
            llm_output={
                "model_name": "owner/model",
                "provider_key": "llmext_acme_llm",
                "cost_usd": 0.0123,
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            },
            generations=[],
        ),
        run_id="reported-cost-run",
    )

    assert fake_logger.log_action.call_args.kwargs["cost_usd"] == 0.0123


def test_on_llm_error_releases_started_provider_state():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    callback = PlatformLoggerCallback(MagicMock(), project_id=1, job_id=49)
    callback.on_chat_model_start(
        {},
        [[SimpleNamespace(content="hello")]],
        run_id="failed-run",
        metadata={"flow_steward_provider": "openai"},
    )

    callback.on_llm_error(RuntimeError("upstream failed"), run_id="failed-run")

    assert "failed-run" not in callback._llm_providers


def test_infer_exception_hints_detects_tool_failures():
    try:
        from tests.support.platform_test_store import PlatformTestStore as PlatformLogger
    except Exception as e:
        pytest.skip(f"Flow Steward logger unavailable: {e}")

    source, category = PlatformLogger.infer_exception_hints(
        RuntimeError("Tool failed: playwright click action timed out"),
        context={"node_name": "composer"},
    )

    assert source == "tool"
    assert category == "tool"


def test_a_runnable_nested_inside_a_node_is_not_recorded_as_a_step() -> None:
    """An agent's own chain showed as "Workflow: Runnablesequence" in Job Details."""
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = MagicMock()
    fake_logger.get_job.return_value = {
        "id": "job-1",
        "project_id": "project-1",
        "selected_workflow": "workflow-1",
        "status": "in_progress",
        "started_at": None,
        "queued_at": None,
    }
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    payload = {"current_step_id": "write"}
    for run_id, name in (("node-run", "agent_writer"), ("chain-run", "RunnableSequence")):
        callback.on_chain_start(
            run_id=run_id,
            name=name,
            inputs=payload,
            metadata={"langgraph_node": "agent_writer"},
        )
    callback.on_chain_end(run_id="chain-run", name="RunnableSequence", outputs={})
    callback.on_chain_end(run_id="node-run", name="agent_writer", outputs=payload)

    recorded = {
        call.kwargs["step"]["step_id"]
        for call in fake_logger.record_public_progress_transition.call_args_list
    }
    assert "RunnableSequence" not in recorded
    assert recorded == {"agent_writer"}


@pytest.mark.parametrize(
    ("value", "pause_status"),
    [
        ({"gate_kind": "wait", "kind": "timer", "wait_id": "w-1"}, "awaiting_timer"),
        ({"gate_kind": "wait", "kind": "child", "wait_id": "w-2"}, "awaiting_child"),
        ({"gate_kind": "wait", "kind": "approval", "approval_id": "a-1"}, "awaiting_approval"),
        ({"stage": "Review"}, "awaiting_approval"),
    ],
)
def test_a_wait_interrupt_is_recorded_as_its_own_pause_not_an_approval(value, pause_status):
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    fake_logger = _progress_logger()
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    callback.on_chain_start(run_id="run-1", name="composer", inputs=_composer_run_state())

    callback.on_chain_error(
        error=GraphInterrupt([Interrupt(value=value, id="i-1")]),
        run_id="run-1",
        name="composer",
    )

    assert fake_logger.log_action.call_args.kwargs["status"] == pause_status
    transition = fake_logger.record_public_progress_transition.call_args.kwargs
    assert transition["step"]["status"] == pause_status
    assert transition["step"]["finished_at"] is None
    assert transition["job_status"] == pause_status


@pytest.mark.parametrize("task_source", ["workflow_test_run", "step_test_run"])
def test_state_based_approval_is_an_active_public_step_until_a_real_decision(task_source):
    from core.application.jobs.public_progress_service import PublicProgressService
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback
    from tests._step_run_state import step_run_state
    from tests.unit.test_public_progress_service import MemoryProgressRepository

    fake_logger = _progress_logger()
    service = PublicProgressService(MemoryProgressRepository())
    fake_logger.record_public_progress_transition.side_effect = service.record_transition
    callback = PlatformLoggerCallback(fake_logger, project_id="project-1", job_id="job-1")
    payload = step_run_state(
        {"step_id": "approval", "step_kind": "approval", "override": {"approval": {}}},
        step_execution_id=_EXECUTION_ID,
        task_source=task_source,
        status="in_progress",
        dispatched_step_execution={"step_execution_id": _EXECUTION_ID, "attempt": 1},
    )
    callback.on_chain_start(run_id="approval-run", name="human_approval_gate", inputs=payload)
    paused = {**payload, "status": "awaiting_approval", "needs_user_approval": True}
    callback.on_chain_end(run_id="approval-run", name="human_approval_gate", outputs=paused)

    snapshot = service.current_snapshot(job_id="job-1").snapshot
    assert snapshot.status == "awaiting_approval"
    assert snapshot.current_step_id == "approval"
    assert [
        (step.step_id, step.step_execution_id, step.status) for step in snapshot.active_steps
    ] == [("approval", _EXECUTION_ID, "awaiting_approval")]
    assert snapshot.steps[-1].finished_at is None

    callback.on_chain_start(run_id="resumed-run", name="human_approval_gate", inputs=paused)
    callback.on_chain_end(
        run_id="resumed-run",
        name="human_approval_gate",
        outputs={**payload, "needs_user_approval": False},
    )
    resumed = service.current_snapshot(job_id="job-1").snapshot
    assert len(resumed.steps) == 1
    assert resumed.steps[-1].status == "completed"
    assert resumed.steps[-1].finished_at is not None
    assert resumed.active_steps == []


@pytest.mark.parametrize("task_source", ["workflow_test_run", "step_test_run"])
def test_real_approval_command_reaches_durable_execution_history(task_source):
    import asyncio

    asyncio.run(_real_approval_command_reaches_durable_execution_history(task_source))


async def _real_approval_command_reaches_durable_execution_history(task_source):
    """The real gate returns Command.update, not the complete input state."""
    from langgraph.graph import END, StateGraph

    from core.application.jobs.public_progress_service import PublicProgressService
    from core.application.jobs.public_workflow_snapshot_query import PublicWorkflowSnapshotQuery
    from core.application.orchestration.approval_payload import state_based_approval_payload
    from core.application.orchestration.graph.builder_approval_gate import (
        _registry_approval_gate_node,
    )
    from core.application.orchestration.state import OrchestratorState
    from core.application.reports.job_detail_contract import (
        build_job_detail_contract,
        build_job_execution_steps_page,
    )
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback
    from tests._step_run_state import step_run_state
    from tests.unit.test_public_progress_service import MemoryProgressRepository

    class MarkedMemoryRepository(MemoryProgressRepository):
        def load(self, *, job_id):
            value = super().load(job_id=job_id)
            if value is not None:
                value["marker_id"] = f"marker-{len(self.markers)}"
            return value

    store = PublicProgressService(MarkedMemoryRepository())
    gateway = _progress_logger()
    gateway.record_public_progress_transition.side_effect = store.record_transition
    gateway.get_public_progress_snapshot.side_effect = lambda job_id: store.current_snapshot(
        job_id=job_id
    )
    callback = PlatformLoggerCallback(gateway, project_id="project-1", job_id="job-1")
    builder = StateGraph(OrchestratorState)
    builder.add_node("human_approval_gate", _registry_approval_gate_node)
    builder.add_node("orchestrator_validation", lambda state: {})
    builder.add_node("orchestrator", lambda state: {})
    builder.set_entry_point("human_approval_gate")
    builder.add_edge("orchestrator_validation", END)
    builder.add_edge("orchestrator", END)
    graph = builder.compile()
    state = {}
    job = {**gateway.get_job.return_value, "thread_id": "thread-1"}
    query = PublicWorkflowSnapshotQuery(
        gateway,
        get_thread_state_snapshot=lambda thread_id: SimpleNamespace(values=state, tasks=[]),
        to_json_obj=lambda value: value,
    )

    class DetailQueries:
        def get_job_status(self, job_id, **kwargs):
            return job

        def get_workflow_run(self, job_id, **kwargs):
            return query.get(job, approval_gate=state_based_approval_payload(state))

        def list_messages(self, job_id, **kwargs):
            return {"messages": []}

    # A retry keeps the same durable occurrence; a later loop visit gets its own.
    executions = [
        (_EXECUTION_ID, 1, 1),
        (_EXECUTION_ID, 2, 1),
        ("b9708910-21f1-463e-8d45-e5ad0a5dc731", 1, 2),
    ]
    for execution_id, attempt, occurrence in executions:
        payload = step_run_state(
            {"step_id": "approval", "step_kind": "approval", "override": {"approval": {}}},
            step_execution_id=execution_id,
            attempt=attempt,
            task_source=task_source,
            status="in_progress",
            dispatched_step_execution={"step_execution_id": execution_id, "attempt": attempt},
        )
        state = await graph.ainvoke(payload, config={"callbacks": [callback]})
        assert state["status"] == "awaiting_approval"
        job["status"] = state["status"]
        detail = query.get(job, approval_gate=state_based_approval_payload(state))
        assert detail.steps[-1].status == "awaiting_approval"
        assert detail.steps[-1].attempt == attempt
        page = await build_job_execution_steps_page(
            job_id="job-1", scope=None, job_query_service=DetailQueries()
        )
        rows = [row for row in page["steps"] if row["step_execution_id"] == execution_id]
        assert len(rows) == 1
        assert rows[0]["status"] == "awaiting_review", rows[0]
        assert not rows[0]["finished_at"]
        contract = await build_job_detail_contract(
            job_id="job-1", scope=None, job_query_service=DetailQueries()
        )
        contract_row = next(
            row
            for row in contract["workflow_run"]["steps"]
            if row["step_execution_id"] == execution_id
        )
        assert contract_row["status"] == "awaiting_approval"
        assert contract_row["finished_at"] is None
        assert [(step.step_execution_id, step.occurrence) for step in detail.active_steps] == [
            (execution_id, occurrence)
        ]
        state = await graph.ainvoke(
            {**state, "user_approved": True}, config={"callbacks": [callback]}
        )
        job["status"] = "in_progress"
        resumed = query.get(job)
        row = next(step for step in resumed.steps if step.step_execution_id == execution_id)
        assert row.status == "completed"
        assert row.finished_at is not None
        assert row.occurrence == occurrence
        assert resumed.active_steps == []
        assert callback._public_step_recorder._test_approval_runs == {}
        assert callback._public_step_recorder._run_steps == {}


@pytest.mark.parametrize(
    "case", ["error", "interrupt", "normal", "other_source", "wrong_dispatch", "wrong_run"]
)
def test_partial_approval_command_requires_matching_start_and_releases_context(case):
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command, Interrupt

    from core.infrastructure.db.platform_logger import PlatformLoggerCallback
    from tests._step_run_state import step_run_state

    gateway = _progress_logger()
    callback = PlatformLoggerCallback(gateway, project_id="project-1", job_id="job-1")
    payload = step_run_state(
        {"step_id": "approval", "step_kind": "approval", "override": {"approval": {}}},
        step_execution_id=_EXECUTION_ID,
        task_source="api" if case == "other_source" else "workflow_test_run",
        dispatched_step_execution={
            "step_execution_id": _EXECUTION_ID,
            "attempt": 2 if case == "wrong_dispatch" else 1,
        },
    )
    callback.on_chain_start(run_id="gate-run", name="human_approval_gate", inputs=payload)
    if case in {"error", "interrupt"}:
        error = (
            RuntimeError("gate failed")
            if case == "error"
            else GraphInterrupt([Interrupt(value={"stage": "Review"}, id="i-1")])
        )
        callback.on_chain_error(error=error, run_id="gate-run", name="human_approval_gate")
    else:
        callback.on_chain_end(
            run_id="other-run" if case == "wrong_run" else "gate-run",
            name="human_approval_gate",
            outputs=Command(
                update={
                    "status": "in_progress" if case == "normal" else "awaiting_approval",
                    "needs_user_approval": case != "normal",
                }
            ),
        )
        assert (
            gateway.record_public_progress_transition.call_args.kwargs["step"]["status"]
            == "completed"
        )
        if case == "wrong_run":
            callback.on_chain_error(
                error=RuntimeError("gate failed"), run_id="gate-run", name="human_approval_gate"
            )
    recorder = callback._public_step_recorder
    assert recorder._test_approval_runs == {}
    assert "gate-run" not in recorder._run_steps
