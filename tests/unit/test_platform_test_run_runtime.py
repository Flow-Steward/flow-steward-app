from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application.orchestration.agents.prompt_agent import PromptAgent
from core.application.orchestration.agents.prompt_builder import PromptBuilder
from core.application.workflows import platform_test_run_runtime as runtime
from core.application.workflows import test_run_result_values as result_values


def test_build_initial_state_materializes_workflow_inputs_for_skill_contract_lookup():
    state = runtime._build_initial_state(
        workflow_id="wf_launch_copy",
        project_id="project_alpha",
        task_summary="Prepare launch copy",
        task_source="workflow_test_run",
        project_name="Flow Steward App",
        project_config={"project_id": "project_alpha"},
        task_id="task_123",
        task_envelope={
            "project_id": "project_alpha",
            "requested_workflow_id": "wf_launch_copy",
            "title": "Prepare launch copy",
            "description": "Prepare launch copy",
            "inputs": {
                "project_id": "project_alpha",
                "request": "Draft launch copy for the FlowSteward release.",
            },
            "metadata": {
                "source": "workflow_test_run",
                "requested_workflow_id": "wf_launch_copy",
            },
        },
    )

    assert state["inputs"]["request"] == "Draft launch copy for the FlowSteward release."
    assert state["materialized_inputs"]["request"] == (
        "Draft launch copy for the FlowSteward release."
    )


def test_ephemeral_child_initial_state_uses_selected_child_over_parent_project_workflow():
    child_workflow_id = (
        "extension.5cc90460-491e-45e3-a47c-c1110afc37db.sample_weather_summary_child"
    )
    parent_workflow_id = "extension.5cc90460-491e-45e3-a47c-c1110afc37db.sample_travel_day_planner"

    state = runtime._build_initial_state(
        workflow_id=child_workflow_id,
        project_id="project_alpha",
        task_summary="Invoke the weather child",
        task_source="workflow_test_run",
        project_name="Flow Steward App",
        project_config={
            "project_id": "project_alpha",
            "workflow_id": parent_workflow_id,
        },
        task_id="task_child",
        task_envelope={
            "project_id": "project_alpha",
            "requested_workflow_id": child_workflow_id,
            "inputs": {"city": "Vilnius", "latitude": 54.69, "longitude": 25.28},
        },
        extra_state={
            "workflow_id": child_workflow_id,
            "workflow_selection_source": "parent_call",
            "parent_workflow_id": parent_workflow_id,
            "invoke_workflow_ancestry": [parent_workflow_id, child_workflow_id],
        },
    )

    assert state["workflow_id"] == child_workflow_id
    assert state["trusted_runtime_context"]["workflow_id"] == child_workflow_id


def test_test_run_input_binding_cannot_overwrite_host_project_identity():
    state = runtime._build_initial_state(
        workflow_id="wf_launch_copy",
        project_id="project-a",
        task_summary="Validate project identity",
        task_source="workflow_test_run",
        project_name="Flow Steward App",
        project_config={"project_id": "project-a"},
        task_id="task-project-scope",
        task_envelope={
            "inputs": {"requested_project": "project-victim"},
            "metadata": {
                "source": "workflow_test_run",
                "input_bindings": [
                    {
                        "name": "requested_project",
                        "source": "state.trusted_runtime_context.project_id",
                    }
                ],
            },
        },
    )

    assert state["project_id"] == "project-a"
    assert state["trusted_runtime_context"]["project_id"] == "project-a"


@pytest.mark.parametrize(
    "source",
    [
        "state..trusted_runtime_context.test_mode",
        "state...trusted_runtime_context.test_mode",
    ],
)
def test_test_run_input_binding_rejects_malformed_reserved_path(source: str):
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    state = {"trusted_runtime_context": {"test_mode": False}, "test_mode": False}
    update = runtime._apply_test_run_input_bindings(
        state,
        {
            "inputs": {"enable_test_mode": True},
            "metadata": {
                "source": "workflow_test_run",
                "input_bindings": [{"name": "enable_test_mode", "source": source}],
            },
        },
    )
    merged = {**state, **update}

    assert update == {}
    assert runtime_identity_from_state(merged).test_mode is False


def test_workflow_test_run_public_inputs_reach_agent_prompt_context_unchanged():
    request = (
        "Full smoke-test request: preserve task_envelope.inputs.request for the "
        "agent prompt, not only the short summary."
    )
    state = runtime._build_initial_state(
        workflow_id="weekly_report",
        project_id="project_alpha",
        task_summary="Smoke-test the weekly report workflow",
        task_source="workflow_test_run",
        project_name="Flow Steward App",
        project_config={"project_id": "project_alpha"},
        task_id="weekly-report-smoke-20260601",
        task_envelope={
            "project_id": "project_alpha",
            "requested_workflow_id": "weekly_report",
            "title": "Smoke-test the weekly report workflow",
            "description": "Smoke-test the weekly report workflow",
            "inputs": {
                "project_id": "project_alpha",
                "request": request,
            },
            "metadata": {
                "source": "workflow_test_run",
                "requested_workflow_id": "weekly_report",
            },
        },
    )

    prompt_context = PromptBuilder._build_default_task_context(state)
    prompt_agent = PromptAgent.__new__(PromptAgent)
    prompt_agent.manifest = SimpleNamespace(tools=["workflow_ops"])
    tool_config = prompt_agent._build_tool_config(
        state,
        tool_ids=["workflow_ops"],
        skill="draft_summary",
    )

    assert state["request"] == request
    assert state["initial_requirements"] == request
    assert state["original_request"] == request
    assert f"Initial Requirements:\n{request}" in prompt_context
    assert "- project_name: Flow Steward App" in tool_config
    assert "- task_id: weekly-report-smoke-20260601" in tool_config


def test_materialize_workflow_outputs_backfills_outputs_and_artifacts_from_final_state():
    workflow_outputs, outputs, artifacts = result_values._materialize_workflow_outputs(
        {
            "workflow_outputs": {},
            "outputs": {"channel_summary": "ready"},
            "artifacts": {"report": {"artifact_id": "art_123"}},
        }
    )

    assert outputs == {"channel_summary": "ready"}
    assert artifacts == {"report": {"artifact_id": "art_123"}}
    assert workflow_outputs == {
        "outputs": {"channel_summary": "ready"},
        "artifacts": {"report": {"artifact_id": "art_123"}},
    }


def test_materialize_workflow_outputs_preserves_existing_workflow_outputs_payload():
    workflow_outputs, outputs, artifacts = result_values._materialize_workflow_outputs(
        {
            "workflow_outputs": {
                "outputs": {"content_brief": {"headline": "Hi"}},
                "artifacts": {"report": {"artifact_id": "art_999"}},
            },
            "outputs": {"content_brief": {"headline": "stale"}},
            "artifacts": {"report": {"artifact_id": "stale"}},
        }
    )

    assert outputs == {"content_brief": {"headline": "Hi"}}
    assert artifacts == {"report": {"artifact_id": "art_999"}}
    assert workflow_outputs == {
        "outputs": {"content_brief": {"headline": "Hi"}},
        "artifacts": {"report": {"artifact_id": "art_999"}},
    }


APPROVAL_EXECUTION = "0b8e6a52-5d1f-4c3e-9a57-3d2f8f6f9a10"
NEXT_EXECUTION = "6f3d2b1a-8c4e-4f5a-9b7c-2e1d0c9b8a76"


def _cursor(step_id: str, execution_id: str) -> dict:
    return {"step_id": step_id, "step_execution_id": execution_id, "attempt": 1, "frames": []}


def test_derive_workflow_final_outcome_reports_awaiting_approval_for_pending_human_gate():
    status, report = runtime._derive_workflow_final_outcome(
        {
            "status": "awaiting_approval",
            "needs_user_approval": True,
            "approval_context": "Review the generated content before continuing.",
            "current_step_id": "review_compose_email",
            "run_cursor": _cursor("review_compose_email", APPROVAL_EXECUTION),
        },
        [
            {
                "step_id": "compose_email_brief",
                "step_execution_id": NEXT_EXECUTION,
                "status": "pass",
                "summary": "Generated deterministic launch risk review for the demo workflow extension.",
            }
        ],
    )

    assert status == "awaiting_approval"
    assert report == "Review the generated content before continuing."


def test_pending_recovery_approval_reports_its_reason():
    status, report = runtime._pending_human_gate_outcome(
        {
            "status": "awaiting_approval",
            "needs_user_approval": True,
            "run_cursor": _cursor("compose", APPROVAL_EXECUTION),
            "recovery_pending": {
                "request_id": "req-1",
                "kind": "failure",
                "step_id": "compose",
                "step_execution_id": APPROVAL_EXECUTION,
                "reason": "step error_policy.on_error=await_approval",
            },
        },
        [],
    )

    assert status == "awaiting_approval"
    assert report == "step error_policy.on_error=await_approval"


def test_derive_workflow_final_outcome_does_not_treat_last_pass_trace_as_terminal_success():
    status, report = runtime._derive_workflow_final_outcome(
        {
            "status": "in_progress",
            "current_step_id": "compose_email_brief",
            "run_cursor": _cursor("compose_email_brief", NEXT_EXECUTION),
        },
        [
            {
                "step_id": "compose_email_brief",
                "step_execution_id": NEXT_EXECUTION,
                "status": "pass",
                "summary": "Connector step completed.",
            }
        ],
    )

    assert status == "in_progress"
    assert report == ""


def test_derive_workflow_final_outcome_ignores_stale_awaiting_approval_after_resume():
    status, report = runtime._derive_workflow_final_outcome(
        {
            "status": "awaiting_approval",
            "needs_user_approval": False,
            "current_step_id": "finalize_outputs_step",
            "run_cursor": _cursor("finalize_outputs_step", NEXT_EXECUTION),
        },
        [
            {
                "step_id": "approval_checkpoint_step",
                "step_execution_id": APPROVAL_EXECUTION,
                "status": "pass",
                "summary": "Approval checkpoint cleared.",
            },
            {
                "step_id": "finalize_outputs_step",
                "step_execution_id": NEXT_EXECUTION,
                "status": "pass",
                "summary": "Mapped approved outputs.",
            },
        ],
    )

    assert status == "in_progress"
    assert report == ""


def test_pending_human_gate_outcome_ignores_an_approval_that_already_decided():
    status, report = runtime._pending_human_gate_outcome(
        {
            "status": "awaiting_approval",
            "approval_context": "Review and approve this step before FlowSteward continues.",
            "current_step_id": "approval_checkpoint_step",
            "run_cursor": _cursor("approval_checkpoint_step", APPROVAL_EXECUTION),
        },
        [
            {
                "step_id": "approval_checkpoint_step",
                "step_execution_id": APPROVAL_EXECUTION,
                "status": "pass",
                "summary": "Approval step 'approval_checkpoint_step' approved",
            }
        ],
    )

    assert status == ""
    assert report == ""


def test_workflow_test_run_payload_hides_stale_approval_flag_after_resume_progress():
    payload = runtime._workflow_test_run_payload_from_state(
        {
            "status": "awaiting_approval",
            "needs_user_approval": True,
            "approval_context": "Review and approve this step before FlowSteward continues.",
            "current_step_id": "approval_checkpoint_step",
            "run_cursor": _cursor("approval_checkpoint_step", APPROVAL_EXECUTION),
            "agent_messages": [
                {
                    "step_id": "approval_checkpoint_step",
                    "step_execution_id": APPROVAL_EXECUTION,
                    "attempt": 1,
                    "from_agent": "human_approval_gate",
                    "status": "pass",
                    "summary": "Approval step 'approval_checkpoint_step' approved",
                    "artifacts": {
                        "step_kind": "approval",
                        "step_id": "approval_checkpoint_step",
                        "decision": "approved",
                    },
                }
            ],
            "inputs": {},
            "workflow_data": {},
        },
        workflow_id="wf_launch_copy",
        project_id="project_alpha",
        thread_id="test-run-1",
        execution_id="exec-1",
    )

    assert payload["status"] == "in_progress"
    assert payload["needs_user_approval"] is False
    assert payload["approval_context"] is None
    assert payload["current_step_id"] == "approval_checkpoint_step"
    assert payload["trace"][0]["step_execution_id"] == APPROVAL_EXECUTION
    assert "current_phase" not in payload


def test_durable_step_test_run_payload_returns_trace_result_and_state_patch():
    payload = runtime._workflow_test_run_payload_from_state(
        {
            "task_source": "step_test_run",
            "status": "completed",
            "final_report": "Connector step completed",
            "current_step_id": "store-locations",
            "agent_messages": [
                {
                    "step_id": "store-locations",
                    "step_execution_id": NEXT_EXECUTION,
                    "attempt": 1,
                    "status": "pass",
                    "summary": "Connector step completed",
                    "from_agent": "connector_step",
                    "artifacts": {
                        "step_kind": "connector",
                        "step_id": "store-locations",
                        "connector_result": {"locations": [{"id": "location-1"}]},
                        "_state_patch": {"workflow_data": {"locations": ["location-1"]}},
                    },
                }
            ],
            "inputs": {},
            "workflow_data": {"locations": ["location-1"]},
        },
        workflow_id="wf-acme-store",
        project_id="project-alpha",
        thread_id="thread-step-1",
        execution_id="exec-step-1",
    )

    assert payload["trace"][0]["step_id"] == "store-locations"
    assert "phase_id" not in payload
    assert "phase_id" not in payload["trace"][0]
    assert payload["step_id"] == "store-locations"
    assert payload["step_kind"] == "connector"
    assert payload["summary"] == "Connector step completed"
    assert payload["inputs"] == {}
    assert payload["result"]["connector_result"]["locations"] == [{"id": "location-1"}]
    assert payload["state_patch"] == {"workflow_data": {"locations": ["location-1"]}}


def test_durable_http_step_test_run_payload_keeps_actual_request_and_response():
    payload = runtime._workflow_test_run_payload_from_state(
        {
            "task_source": "step_test_run",
            "status": "completed",
            "final_report": "http_request POST https://example.test/orders -> 201",
            "current_step_id": "create-order",
            "agent_messages": [
                {
                    "step_id": "create-order",
                    "step_execution_id": NEXT_EXECUTION,
                    "attempt": 1,
                    "status": "pass",
                    "summary": "http_request POST https://example.test/orders -> 201",
                    "from_agent": "http_request_step",
                    "artifacts": {
                        "step_kind": "http_request",
                        "step_id": "create-order",
                        "http_request": {
                            "ok": True,
                            "status_code": 201,
                            "response_mode": "inline",
                            "request": {
                                "method": "POST",
                                "url": "https://example.test/orders",
                            },
                            "response_body": {"id": "order-1"},
                        },
                        "_state_patch": {"workflow_data": {"created_order": {"id": "order-1"}}},
                    },
                }
            ],
            "inputs": {"order_id": "order-1"},
            "workflow_data": {"created_order": {"id": "order-1"}},
        },
        workflow_id="wf-order-sync",
        project_id="project-alpha",
        thread_id="thread-http-step-1",
        execution_id="exec-http-step-1",
    )

    assert payload["step_kind"] == "http_request"
    assert payload["inputs"] == {"order_id": "order-1"}
    assert payload["result"]["http_request"]["request"] == {
        "method": "POST",
        "url": "https://example.test/orders",
    }
    assert payload["result"]["http_request"]["status_code"] == 201
    assert payload["result"]["http_request"]["response_body"] == {"id": "order-1"}


@pytest.mark.parametrize(
    ("step_kind", "runtime_artifact", "result_target", "target_key", "value"),
    [
        (
            "transform",
            {
                "ok": True,
                "detail": "transform step executed",
                "errors": [],
                "result_target": "state.normalized_rows",
            },
            "state.normalized_rows",
            "normalized_rows",
            [{"sku": "SKU-1"}],
        ),
        (
            "tabular_feed",
            {
                "ok": True,
                "runtime": "tabular_feed",
                "detail": "tabular_feed step executed",
                "operation": "read_rows",
                "action_id": "read_rows",
                "errors": [],
                "result_target": "state.feed_page",
            },
            "state.feed_page",
            "feed_page",
            {"rows": [{"sku": "SKU-1"}]},
        ),
    ],
)
def test_step_test_drawer_rehydrates_compacted_deterministic_result_from_final_state(
    step_kind, runtime_artifact, result_target, target_key, value
):
    artifacts = {
        "step_kind": step_kind,
        "step_id": f"{step_kind}-step",
        step_kind: runtime_artifact,
    }
    final_state = {
        "task_source": "step_test_run",
        "status": "completed",
        "current_step_id": f"{step_kind}-step",
        "agent_messages": [
            {
                "step_id": f"{step_kind}-step",
                "step_execution_id": NEXT_EXECUTION,
                "attempt": 1,
                "status": "pass",
                "summary": runtime_artifact["detail"],
                "from_agent": f"{step_kind}_step",
                "artifacts": artifacts,
            }
        ],
        target_key: value,
        "inputs": {},
        "workflow_data": {target_key: value},
    }

    durable = runtime._workflow_test_run_payload_from_state(
        final_state,
        workflow_id="wf-deterministic",
        project_id="project-alpha",
        thread_id="thread-step-deterministic",
        execution_id="exec-step-deterministic",
    )
    immediate = runtime._step_result_payload(
        state={"agent_messages": [], "workflow_data": {}},
        update=final_state,
        workflow_id="wf-deterministic",
        step={
            "step_id": f"{step_kind}-step",
            "step_kind": step_kind,
            "override": {step_kind: {"result_target": result_target}},
        },
        input_payload={},
    )

    for payload in (durable, immediate):
        assert (
            payload["result"][step_kind]["output" if step_kind == "transform" else "result"]
            == value
        )
        assert payload["state_patch"][target_key] == value
        assert payload["state_patch"]["workflow_data"][target_key] == value


@pytest.mark.parametrize("step_kind", ["transform", "tabular_feed"])
def test_step_test_drawer_rehydrates_empty_state_patch_for_deterministic_failure(step_kind):
    runtime_artifact = {
        "ok": False,
        "detail": f"{step_kind} step failed",
        "errors": [{"reason": "state_reference_missing", "selector": "state.missing"}],
        "result_target": "state.output",
    }
    payload = runtime._workflow_test_run_payload_from_state(
        {
            "task_source": "step_test_run",
            "status": "fail",
            "agent_messages": [
                {
                    "step_id": f"{step_kind}-step",
                    "step_execution_id": NEXT_EXECUTION,
                    "attempt": 1,
                    "status": "fail",
                    "summary": runtime_artifact["detail"],
                    "from_agent": f"{step_kind}_step",
                    "artifacts": {
                        "step_kind": step_kind,
                        "step_id": f"{step_kind}-step",
                        "error": f"{step_kind}_failed",
                        step_kind: runtime_artifact,
                    },
                }
            ],
            "inputs": {},
            "workflow_data": {},
        },
        workflow_id="wf-deterministic",
        project_id="project-alpha",
    )

    assert payload["result"][step_kind]["state_patch"] == {}
    assert payload["state_patch"] == {}
    assert payload["result"][step_kind]["errors"] == runtime_artifact["errors"]
    if step_kind == "tabular_feed":
        assert payload["result"][step_kind]["result"] == {}
    else:
        assert "output" not in payload["result"][step_kind]


@pytest.mark.parametrize(
    ("step_kind", "artifacts", "final_state", "expected_patch"),
    [
        (
            "batch",
            {
                "step_kind": "batch",
                "step_id": "batch-rows",
                "result_target": "state.batches",
                "item_count": 2,
                "batch_count": 1,
                "batch_size": 1000,
                "emit_partial_batch": True,
            },
            {"batches": [[{"sku": "A"}, {"sku": "B"}]]},
            {"batches": [[{"sku": "A"}, {"sku": "B"}]]},
        ),
        (
            "collect",
            {
                "step_kind": "collect",
                "step_id": "keep-rows",
                "collect": {"target": "mapped_rows", "operation": "append", "count": 2},
                "result_targets": ["state.mapped_rows"],
            },
            {"mapped_rows": [{"sku": "A"}, {"sku": "B"}]},
            {"mapped_rows": [{"sku": "A"}, {"sku": "B"}]},
        ),
        (
            "set_variable",
            {
                "step_kind": "set_variable",
                "step_id": "copy-rows",
                "assigned": ["copied_rows", "workflow_data"],
                "result_targets": ["state.copied_rows"],
            },
            {"copied_rows": [{"sku": "A"}, {"sku": "B"}]},
            {"copied_rows": [{"sku": "A"}, {"sku": "B"}]},
        ),
    ],
)
def test_step_test_drawer_rehydrates_compacted_control_flow_state_patch(
    step_kind, artifacts, final_state, expected_patch
):
    state = {
        "task_source": "step_test_run",
        "status": "completed",
        "current_step_id": artifacts["step_id"],
        "agent_messages": [
            {
                "step_id": artifacts["step_id"],
                "step_execution_id": NEXT_EXECUTION,
                "attempt": 1,
                "status": "pass",
                "summary": f"{step_kind} completed",
                "from_agent": "control_flow_step",
                "artifacts": artifacts,
            }
        ],
        "inputs": {},
        "workflow_data": dict(final_state),
        **final_state,
    }

    payload = runtime._workflow_test_run_payload_from_state(
        state,
        workflow_id="wf-control-flow",
        project_id="project-alpha",
    )

    assert payload["state_patch"] == {**expected_patch, "workflow_data": final_state}
    for key, value in expected_patch.items():
        assert payload["result"][key] == value


def test_run_workflow_test_run_ephemeral_surfaces_pending_human_gate(monkeypatch):
    class _FakeGraph:
        def stream(self, state, config, stream_mode="values"):
            yield {
                "status": "awaiting_approval",
                "needs_user_approval": True,
                "approval_context": "Review the generated content before continuing.",
                "current_step_id": "review_compose_email",
                "run_cursor": _cursor("review_compose_email", APPROVAL_EXECUTION),
                "agent_messages": [
                    {
                        "step_id": "compose_email_brief",
                        "step_execution_id": NEXT_EXECUTION,
                        "attempt": 1,
                        "status": "pass",
                        "summary": "Generated deterministic launch risk review for the demo workflow extension.",
                        "from_agent": "sample_launch_risk_reviewer",
                        "skill": "sample_review_launch_risks",
                        "artifacts": {
                            "step_kind": "agent",
                            "step_id": "compose_email_brief",
                        },
                    }
                ],
            }

    monkeypatch.setattr(runtime, "_reload_registry_for_test_run", lambda: None)
    monkeypatch.setattr(runtime, "build_registry_graph", lambda *args, **kwargs: _FakeGraph())

    result = runtime.run_workflow_test_run_ephemeral(
        workflow_id="test",
        project_id="project_alpha",
        task_summary="Run the workflow",
        task_id="task_123",
        task_envelope={
            "project_id": "project_alpha",
            "requested_workflow_id": "test",
            "title": "Run the workflow",
            "description": "Run the workflow",
            "inputs": {"project_id": "project_alpha", "channel": "email"},
            "metadata": {"source": "workflow_test_run", "requested_workflow_id": "test"},
        },
        project_name="Flow Steward App",
        project_config={"project_id": "project_alpha"},
    )

    assert result["status"] == "awaiting_approval"
    assert result["final_status"] == "awaiting_approval"
    assert result["needs_user_approval"] is True
    assert result["approval_context"] == "Review the generated content before continuing."
    assert str(result["thread_id"]).startswith("test-run-")
    assert str(result["execution_id"])


def test_workflow_test_run_payload_exposes_exact_current_interrupt_id(monkeypatch):
    interrupt = type("Interrupt", (), {"id": "approval-exact-42", "value": {}})()
    task = type("Task", (), {"interrupts": [interrupt]})()
    snapshot = type("Snapshot", (), {"tasks": [task]})()
    monkeypatch.setattr(runtime, "_get_thread_state_snapshot", lambda _thread_id: snapshot)

    result = runtime._workflow_test_run_payload_from_state(
        {
            "status": "awaiting_approval",
            "needs_user_approval": True,
            "approval_context": "Review this output.",
        },
        workflow_id="workflow-42",
        project_id="project-42",
        thread_id="thread-42",
    )

    assert result["approval_id"] == "approval-exact-42"


def test_run_workflow_test_run_ephemeral_stops_on_first_pending_human_gate_snapshot(
    monkeypatch,
):
    calls = {"count": 0}
    cursor = _cursor("approval_checkpoint_step", APPROVAL_EXECUTION)

    class _FakeGraph:
        def stream(self, state, config, stream_mode="values"):
            yield {
                "status": "in_progress",
                "current_step_id": "approval_checkpoint_step",
                "run_cursor": cursor,
                "agent_messages": [],
            }
            calls["count"] += 1
            yield {
                "status": "awaiting_approval",
                "needs_user_approval": True,
                "approval_context": "Review and approve before continuing.",
                "current_step_id": "approval_checkpoint_step",
                "run_cursor": cursor,
                "agent_messages": [],
            }
            calls["count"] += 1
            yield {
                "status": "awaiting_approval",
                "needs_user_approval": True,
                "approval_context": "stale repeated approval snapshot",
                "current_step_id": "approval_checkpoint_step",
                "run_cursor": cursor,
                "agent_messages": [],
            }

    monkeypatch.setattr(runtime, "_reload_registry_for_test_run", lambda: None)
    monkeypatch.setattr(runtime, "build_registry_graph", lambda *args, **kwargs: _FakeGraph())

    result = runtime.run_workflow_test_run_ephemeral(
        workflow_id="test",
        project_id="project_alpha",
        task_summary="Run the workflow",
        task_id="task_123",
        task_envelope={
            "project_id": "project_alpha",
            "requested_workflow_id": "test",
            "title": "Run the workflow",
            "description": "Run the workflow",
            "inputs": {"project_id": "project_alpha"},
            "metadata": {"source": "workflow_test_run", "requested_workflow_id": "test"},
        },
        project_name="Flow Steward App",
        project_config={"project_id": "project_alpha"},
    )

    assert calls["count"] == 1
    assert result["status"] == "awaiting_approval"
    assert result["approval_context"] == "Review and approve before continuing."


def test_get_workflow_test_run_state_rehydrates_payload_from_checkpoint(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_get_thread_state_values",
        lambda thread_id, *, force_refresh=False: {
            "selected_workflow": "sample_product_catalog_enrichment",
            "project_id": "project_alpha",
            "workflow_test_run_thread_id": thread_id,
            "workflow_test_run_execution_id": "exec_456",
            "status": "completed",
            "outputs": {"catalog_row_count": 10},
            "artifacts": {"catalog_artifact": {"artifact_id": "art_123"}},
            "agent_messages": [],
        },
    )

    result = runtime.get_workflow_test_run_state(
        "test-run-abc",
        durable_state_reader=lambda _thread_id: {
            "status": "completed",
            "job_id": "job-test-run-abc",
            "task_summary": "Validate the catalog",
        },
    )

    assert result["workflow_id"] == "sample_product_catalog_enrichment"
    assert result["project_id"] == "project_alpha"
    assert result["thread_id"] == "test-run-abc"
    assert result["execution_id"] == "exec_456"
    assert result["job_id"] == "job-test-run-abc"
    assert result["task_summary"] == "Validate the catalog"
    assert result["final_status"] == "completed"
    assert result["outputs"] == {"catalog_row_count": 10}
    assert result["artifacts"] == {"catalog_artifact": {"artifact_id": "art_123"}}


def test_get_workflow_test_run_state_returns_durable_queued_state_before_checkpoint(
    monkeypatch,
):
    monkeypatch.setattr(
        runtime,
        "_get_thread_state_values",
        lambda _thread_id, *, force_refresh=False: {},
    )

    result = runtime.get_workflow_test_run_state(
        "test-run-queued",
        durable_state_reader=lambda thread_id: {
            "status": "queued",
            "final_status": "queued",
            "thread_id": thread_id,
            "job_id": "job-test-run-queued",
            "workflow_id": "sample_product_catalog_enrichment",
            "project_id": "project_alpha",
            "step_id": "fetch_catalog",
            "step_kind": "http_request",
            "inputs": {"sku": "SKU-001"},
        },
    )

    assert result == {
        "status": "queued",
        "final_status": "queued",
        "thread_id": "test-run-queued",
        "job_id": "job-test-run-queued",
        "workflow_id": "sample_product_catalog_enrichment",
        "project_id": "project_alpha",
        "step_id": "fetch_catalog",
        "step_kind": "http_request",
        "inputs": {"sku": "SKU-001"},
    }


def test_get_workflow_test_run_state_refreshes_stale_awaiting_approval_snapshot(monkeypatch):
    calls: list[bool] = []

    def _reader(thread_id: str, *, force_refresh: bool = False):
        calls.append(force_refresh)
        if not force_refresh:
            return {
                "selected_workflow": "sample_product_catalog_enrichment",
                "project_id": "project_alpha",
                "status": "awaiting_approval",
                "needs_user_approval": True,
                "approval_context": "Review and approve this step before FlowSteward continues.",
                "agent_messages": [],
            }
        return {
            "selected_workflow": "sample_product_catalog_enrichment",
            "project_id": "project_alpha",
            "workflow_test_run_thread_id": thread_id,
            "workflow_test_run_execution_id": "exec_789",
            "status": "in_progress",
            "user_approved": True,
            "needs_user_approval": False,
            "approval_context": None,
            "agent_messages": [],
        }

    monkeypatch.setattr(
        runtime,
        "_get_thread_state_values",
        _reader,
    )

    result = runtime.get_workflow_test_run_state(
        "test-run-refresh", durable_state_reader=lambda _thread_id: {}
    )

    assert calls == [False, True]
    assert result["final_status"] == "in_progress"
    assert result["needs_user_approval"] is False
    assert result["approval_context"] is None


def test_get_workflow_test_run_state_prefers_durable_resume_pending(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_get_thread_state_values",
        lambda _thread_id, *, force_refresh=False: {
            "selected_workflow": "sample_product_catalog_enrichment",
            "project_id": "project_alpha",
            "status": "awaiting_approval",
            "needs_user_approval": True,
            "approval_context": "Review this step.",
            "agent_messages": [],
        },
    )
    durable_calls: list[str] = []

    result = runtime.get_workflow_test_run_state(
        "test-run-resume-pending",
        durable_state_reader=lambda thread_id: (
            durable_calls.append(thread_id) or {"status": "resume_pending"}
        ),
    )

    assert durable_calls == ["test-run-resume-pending"]
    assert result["status"] == "resume_pending"
    assert result["final_status"] == "resume_pending"
    assert result["needs_user_approval"] is False
    assert result["approval_context"] is None


def test_get_workflow_test_run_state_surfaces_failed_resume_after_refresh(monkeypatch):
    calls: list[bool] = []

    def _reader(thread_id: str, *, force_refresh: bool = False):
        calls.append(force_refresh)
        if not force_refresh:
            return {
                "selected_workflow": "sample_product_catalog_enrichment",
                "project_id": "project_alpha",
                "status": "awaiting_approval",
                "needs_user_approval": True,
                "approval_context": "Review and approve this step before FlowSteward continues.",
                "agent_messages": [],
            }
        return {
            "selected_workflow": "sample_product_catalog_enrichment",
            "project_id": "project_alpha",
            "workflow_test_run_thread_id": thread_id,
            "workflow_test_run_execution_id": "exec_failed",
            "status": "failed",
            "needs_user_approval": False,
            "approval_context": None,
            "error": "Recursion limit of 10000 reached",
            "final_report": "Recursion limit of 10000 reached",
            "agent_messages": [],
        }

    monkeypatch.setattr(
        runtime,
        "_get_thread_state_values",
        _reader,
    )

    result = runtime.get_workflow_test_run_state(
        "test-run-failed-refresh", durable_state_reader=lambda _thread_id: {}
    )

    assert calls == [False, True]
    assert result["final_status"] == "failed"
    assert result["status"] == "failed"
    assert result["needs_user_approval"] is False
    assert "Recursion limit" in result["error"]


def test_get_workflow_test_run_state_surfaces_durable_reader_failure(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_get_thread_state_values",
        lambda _thread_id, *, force_refresh=False: {
            "selected_workflow": "sample_product_catalog_enrichment",
            "project_id": "project_alpha",
            "status": "awaiting_approval",
            "agent_messages": [],
        },
    )

    def unavailable(_thread_id: str) -> dict[str, object]:
        raise RuntimeError("durable job state unavailable")

    with pytest.raises(RuntimeError, match="durable job state unavailable"):
        runtime.get_workflow_test_run_state(
            "test-run-reader-failure", durable_state_reader=unavailable
        )


def test_step_test_run_payload_lists_the_values_given_to_the_step_inputs():
    payload = runtime._workflow_test_run_payload_from_state(
        {
            "task_source": "step_test_run",
            "status": "completed",
            "current_step_id": "compose_post",
            "agent_messages": [
                {
                    "step_id": "compose_post",
                    "step_execution_id": NEXT_EXECUTION,
                    "attempt": 1,
                    "status": "pass",
                    "summary": "Rendered 5 characters of text",
                    "from_agent": "template_step",
                    "artifacts": {"step_kind": "template", "step_id": "compose_post"},
                }
            ],
            "inputs": {"project_id": "project-alpha"},
            "task_envelope": {
                "metadata": {"input_binding_values": {"header": "Spring", "items": [1]}}
            },
        },
        workflow_id="wf-template",
        project_id="project-alpha",
        thread_id="thread-template-1",
        execution_id="exec-template-1",
    )

    assert payload["inputs"] == {
        "project_id": "project-alpha",
        "header": "Spring",
        "items": [1],
    }


def test_ephemeral_step_test_run_executes_the_step_of_a_primed_definition(monkeypatch):
    from core.application.orchestration.active_step import active_step, step_message_fields
    from core.application.orchestration.run_definition import clear_run_definition_cache
    from tests._workflow_definitions import linear_workflow

    clear_run_definition_cache()
    workflow = linear_workflow(
        {
            "step_id": "normalize",
            "step_kind": "transform",
            "override": {"transform": {"input": {"source": "state.rows"}, "mode": "object"}},
        },
        workflow_id="wf-ephemeral",
        revision=7,
    )
    seen: dict = {}

    def _execute(state):
        current = active_step(state)
        seen["current"] = current
        seen["state"] = dict(state)
        return {
            "agent_messages": [
                {
                    **step_message_fields(state),
                    "from_agent": "transform_step",
                    "status": "pass",
                    "summary": "transform step executed",
                    "artifacts": {"step_kind": "transform", "step_id": "normalize"},
                }
            ]
        }

    monkeypatch.setattr(runtime, "_reload_registry_for_test_run", lambda: None)
    monkeypatch.setattr(runtime, "execute_step_node", _execute)
    try:
        payload = runtime.run_step_test_run_ephemeral(
            workflow_id="wf-ephemeral",
            workflow=workflow,
            step_id="normalize",
            project_id="proj_demo",
            task_summary="Normalize rows",
            task_id="task-ephemeral",
            task_envelope={"inputs": {"project_id": "proj_demo"}},
            project_name="Demo",
            project_config={},
        )
    finally:
        clear_run_definition_cache()

    state = seen["state"]
    current = seen["current"]
    assert current is not None
    assert current.step_id == "normalize"
    assert current.step["override"]["transform"]["mode"] == "object"
    assert state["runtime_schema_version"] == 2
    ref = state["workflow_definition_ref"]
    assert ref["snapshot_id"].startswith("ephemeral:")
    assert ref["revision"] == 7
    assert state["step_test_run_context"] == {
        "workflow_id": "wf-ephemeral",
        "execution_id": state["workflow_test_run_execution_id"],
        "step_id": "normalize",
    }
    for removed in ("phase_stack", "current_phase", "current_step"):
        assert removed not in state
    assert payload["status"] == "pass"
    assert payload["step_id"] == "normalize"
    assert "phase_id" not in payload


def test_ephemeral_step_test_run_rejects_an_unknown_step(monkeypatch):
    from fastapi import HTTPException

    from tests._workflow_definitions import linear_workflow

    monkeypatch.setattr(runtime, "_reload_registry_for_test_run", lambda: None)
    workflow = linear_workflow({"step_id": "normalize", "step_kind": "transform"})
    with pytest.raises(HTTPException) as missing:
        runtime.run_step_test_run_ephemeral(
            workflow_id="wf-ephemeral",
            workflow=workflow,
            step_id="unknown",
            project_id="proj_demo",
            task_summary="Normalize rows",
            task_id="task-ephemeral",
            task_envelope={},
            project_name="Demo",
            project_config={},
        )
    assert missing.value.status_code == 404


def test_resolve_step_for_test_run_looks_the_step_up_by_id():
    from fastapi import HTTPException

    from core.application.workflows.platform_step_test_run_helpers import (
        resolve_step_for_test_run,
    )

    workflow = {
        "workflow_id": "wf",
        "steps": [{"step_id": "first", "step_kind": "transform"}, {"step_id": "second"}],
    }
    assert resolve_step_for_test_run(
        None, workflow_id="wf", step_id="second", workflow=workflow
    ) == {"step_id": "second"}
    with pytest.raises(HTTPException) as missing:
        resolve_step_for_test_run(None, workflow_id="wf", step_id="2", workflow=workflow)
    assert missing.value.status_code == 404


def test_scope_workflow_validation_for_step_keeps_only_the_selected_step_and_inputs():
    from core.application.workflows.platform_step_test_run_helpers import (
        scope_workflow_validation_for_step,
    )

    validation = {
        "valid": False,
        "errors": [
            {"code": "workflow.steps_missing", "path": "workflow.steps"},
            {"code": "workflow.step_config_key_unknown", "path": "workflow.steps.send.override.x"},
            {"code": "workflow.step_config_key_unknown", "path": "workflow.steps.sender.override"},
            {"code": "workflow.terminal_missing", "path": "workflow.steps"},
            {"code": "workflow.input.bad", "path": "workflow.settings.callable.expected_inputs[0]"},
            {"code": "workflow.agent_tool_unavailable", "tool_id": "t1", "usage": "agent"},
            {"code": "workflow.agent_tool_unavailable", "tool_id": "t2", "usage": "agent"},
            {"code": "workflow.other", "step_id": "send"},
        ],
        "warnings": [{"code": "workflow.w", "path": "workflow.steps.other.override"}],
    }

    scoped = scope_workflow_validation_for_step(
        validation,
        step_id="send",
        selected_registry_tool_issues=[{"tool_id": "t1", "usage": "agent"}],
    )

    assert [
        issue.get("path") or issue.get("tool_id") or issue["code"] for issue in scoped["errors"]
    ] == [
        "workflow.steps",
        "workflow.steps.send.override.x",
        "workflow.settings.callable.expected_inputs[0]",
        "t1",
        "workflow.other",
    ]
    assert scoped["warnings"] == []
    assert scoped["valid"] is False


@pytest.mark.parametrize("durable_status", ["awaiting_timer", "awaiting_child"])
def test_get_workflow_test_run_state_reports_a_durable_wait_not_an_approval(
    monkeypatch, durable_status
):
    monkeypatch.setattr(
        runtime,
        "_get_thread_state_values",
        lambda _thread_id, *, force_refresh=False: {
            "selected_workflow": "sample_product_catalog_enrichment",
            "project_id": "project_alpha",
            "status": "in_progress",
            "agent_messages": [],
        },
    )

    result = runtime.get_workflow_test_run_state(
        "test-run-waiting",
        durable_state_reader=lambda _thread_id: {"status": durable_status},
    )

    assert result["status"] == durable_status
    assert result["final_status"] == durable_status
    assert result["needs_user_approval"] is False
    assert result["approval_context"] is None
