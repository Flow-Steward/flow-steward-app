from __future__ import annotations

import queue
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests._step_run_state import step_run_state


def _run_state(steps, *, active=0, **state):
    """A run whose cursor is on ``steps[active]`` of a primed pinned definition."""
    named = [{"step_id": f"step_{index + 1}", **step} for index, step in enumerate(steps)]
    others = [step for index, step in enumerate(named) if index != active]
    return step_run_state(named[active], *others, **state)


def test_publish_agent_message_retries_with_fresh_bus(monkeypatch):
    from core.application.orchestration.agents import common

    calls: list[str] = []

    class FakeBus:
        _instances: ClassVar[dict[str, object]] = {"stale": object()}

        @classmethod
        def for_state(cls, _state):
            calls.append("for_state")
            return cls()

        def publish(self, _job_id, _envelope, *, project_id=None):
            calls.append(f"publish:{project_id}")
            return len([call for call in calls if call.startswith("publish")]) > 1

    fake_module = types.ModuleType("core.application.orchestration.message_bus")
    fake_module.MessageBus = FakeBus
    monkeypatch.setitem(sys.modules, "core.application.orchestration.message_bus", fake_module)
    monkeypatch.setattr(common, "resolve_security_scope", lambda _state: (123, 456))

    wrote = common.publish_agent_message(
        {"job_id": 456, "project_config": {"project_id": 123}},
        {"from_agent": "checker", "skill": "verify_section"},
    )

    assert wrote is True
    assert calls == ["for_state", "publish:123", "for_state", "publish:123"]
    assert FakeBus._instances == {}


def _stub_agents_common_module():
    module = types.ModuleType("core.application.orchestration.agents.common")

    def _noop(*_args, **_kwargs):
        return None

    module.emit_event = _noop
    module.publish_agent_message = _noop
    module.update_runtime_truth = _noop
    return module


@pytest.fixture
def captured_output_contract(monkeypatch):
    """Supply the selected dependency for isolated wrapper transport tests.

    These fake Agents exercise retry, timeout and input/output transport. Real
    assigned-Skill authorization and capture are covered by the receipt and
    PromptAgent transport tests, which do not use this fixture.
    """
    from core.application.orchestration import output_contract_receipt
    from core.application.registry.step_output_contract import resolve_step_output_contract

    declarations = []

    def capture(_agent, manifest, agent_input):
        selected = SimpleNamespace(expected_outputs=declarations, output_schema={})
        return resolve_step_output_contract(
            SimpleNamespace(),
            agent_id=manifest.agent_id,
            skill_id=agent_input.skill,
            account_id="test-account",
            skill_manifest=selected,
            agent_manifest=manifest,
        ), selected

    monkeypatch.setattr(output_contract_receipt, "capture_agent_output_contract", capture)
    return declarations


@pytest.mark.usefixtures("captured_output_contract")
def test_agent_node_publishes_only_the_host_owned_retry_channel() -> None:
    from core.application.orchestration.agents.base import AgentOutput
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def execute(self, _agent_input):
            return AgentOutput(
                status="fail",
                artifacts={"error": "custom_http_failed"},
                summary="custom HTTP failed",
                confidence=0.0,
                operation_retry_signal={
                    "schema_version": "operation_retry_signal_v1",
                    "provenance": "agent_managed_custom_http",
                    "signal": {
                        "operation_kind": "source",
                        "request_semantics": "safe_read",
                        "failure_class": "provider",
                        "retryable": True,
                        "definitely_no_external_effect": True,
                        "external_effect_status": "failed",
                    },
                    "deadline_epoch_seconds": 130.0,
                },
            )

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Developer",
        config={},
        timeout_minutes=0,
        skills=["correct_draft"],
    )
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(_Agent(), manifest)
        with patch(
            "core.application.orchestration.graph.nodes._validate_skill_input_contract",
            return_value=[],
        ):
            update = node(
                _run_state(
                    [{"step_kind": "agent", "skill": "correct_draft"}],
                    workflow_id="wf-1",
                    agent_messages=[],
                )
            )

    message = update["agent_messages"][-1]
    assert (message["step_id"], message["attempt"]) == ("step_1", 1)
    assert message["operation_retry"]["provenance"] == "agent_managed_custom_http"
    assert message["operation_retry"]["deadline_epoch_seconds"] == 130.0
    assert "retryable" not in message["artifacts"]


def test_extract_step_context_reads_the_cursor_step_from_the_pinned_definition():
    from core.application.orchestration.graph.nodes import _extract_step_context

    state = _run_state(
        [
            {"agent": "writer", "skill": "publish_report"},
            {"agent": "kb_author", "skill": "update_notes"},
        ],
        active=1,
        workflow_id="wf-1",
    )

    ctx = _extract_step_context(state)
    assert ctx["_step"]["agent"] == "kb_author"
    assert ctx["_step"]["skill"] == "update_notes"


def test_materialize_agent_step_input_mapping_synthesizes_request_for_request_only_skill():
    from core.application.orchestration.graph.nodes import _materialize_agent_step_input_mapping

    state = _run_state(
        [
            {
                "step_kind": "agent",
                "step_id": "prepare_workflow_step",
                "skill_id": "sample_write_launch_copy",
                "input_mapping_jsonb": {
                    "campaign_brief": "state.inputs.campaign_brief",
                    "target_audience": "state.inputs.target_audience",
                },
            }
        ],
        inputs={
            "campaign_brief": "Launch the analytics feature with a concise email.",
            "target_audience": "Product managers at SaaS companies",
        },
    )

    schema = {
        "type": "object",
        "properties": {"request": {"type": "string"}},
        "required": ["request"],
    }
    with (
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            return_value=(schema, None),
        ),
        patch(
            "core.application.orchestration.graph.nodes._active_agent_step_skill_id",
            return_value="sample_write_launch_copy",
        ),
    ):
        _materialize_agent_step_input_mapping(state)

    materialized_inputs = state["materialized_inputs"]
    assert materialized_inputs["campaign_brief"] == state["inputs"]["campaign_brief"]
    assert materialized_inputs["target_audience"] == state["inputs"]["target_audience"]
    assert "campaign_brief:" in materialized_inputs["request"]
    assert "target_audience:" in materialized_inputs["request"]


@pytest.mark.usefixtures("captured_output_contract")
def test_agent_node_rejects_unsupported_mapping_filter_before_agent_execution():
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def __init__(self):
            self.called = False

        def execute(self, _agent_input):
            self.called = True
            raise AssertionError("agent execution must not run")

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Developer",
        config={},
        timeout_minutes=0,
        skills=["correct_draft"],
    )
    agent = _Agent()
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(agent, manifest)

    state = _run_state(
        [
            {
                "step_kind": "agent",
                "skill": "correct_draft",
                "input_mapping_jsonb": {"request": "state.rows[0].sku | default('x')"},
            }
        ],
        workflow_id="wf-1",
        rows=[{"sku": "A1"}],
        agent_messages=[],
    )

    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        update = node(state)

    assert agent.called is False
    message = update["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"] == {
        "error": "invalid_input_selector",
        "selector": "state.rows[0].sku | default('x')",
        "path": "input_mapping_jsonb.request",
        "step_kind": "agent",
    }


def test_get_current_skill_prefers_active_step_for_same_agent():
    from core.application.orchestration.graph.nodes import _get_current_skill

    state = _run_state(
        [
            {"agent": "writer", "skill": "write_section"},
            {"agent": "writer", "skill": "publish_report"},
        ],
        active=1,
    )
    manifest = SimpleNamespace(agent_id="writer", skills=["write_section", "publish_report"])

    skill = _get_current_skill(state, manifest)
    assert skill == "publish_report"


def test_get_current_skill_does_not_implicitly_switch_to_apply_revision():
    from core.application.orchestration.graph.nodes import _get_current_skill

    state = _run_state(
        [
            {"agent": "writer", "skill": "write_section"},
        ],
        feedback="Review failed; please rework",
    )
    manifest = SimpleNamespace(agent_id="writer", skills=["write_section", "apply_revision"])

    skill = _get_current_skill(state, manifest)
    assert skill == "write_section"


@pytest.mark.usefixtures("captured_output_contract")
def test_wrap_agent_as_node_enforces_manifest_timeout():
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _SlowAgent:
        def execute(self, _agent_input):
            time.sleep(1.2)
            return SimpleNamespace(
                status="pass",
                artifacts={},
                summary="done",
                confidence=1.0,
                metadata={},
            )

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Developer",
        config={"timeout_minutes": 0.001},
        timeout_minutes=0.001,
        skills=["write_section"],
    )
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(_SlowAgent(), manifest)
    state = _run_state(
        [{"agent": "writer", "skill": "write_section"}],
        job_id=None,
        workflow_id="wf-1",
        agent_messages=[],
        feedback="",
    )

    with patch(
        "core.application.orchestration.graph.nodes._load_skill_schema",
        return_value=({"type": "object", "properties": {}, "required": []}, None),
    ):
        update = node(state)
    messages = update.get("agent_messages", [])
    assert isinstance(messages, list)
    assert messages
    output = messages[-1]
    assert output.get("from_agent") == "writer"
    assert output.get("status") == "fail"
    assert "timed out" in (output.get("summary") or "").lower()


def test_execute_agent_with_timeout_hard_stops_process():
    from core.application.orchestration.graph.nodes import _execute_agent_with_timeout

    class _SlowAgent:
        def execute(self, _agent_input):
            time.sleep(2.5)
            return SimpleNamespace(
                status="pass",
                artifacts={},
                summary="done",
                confidence=1.0,
                metadata={},
            )

    started = time.monotonic()
    _raises_input_463_1 = _SlowAgent()
    with pytest.raises(TimeoutError):
        _execute_agent_with_timeout(
            agent=_raises_input_463_1,
            agent_input={},
            timeout_seconds=1,
            agent_id="writer",
            state={"job_id": 123, "workflow_id": "wf-1"},
        )
    elapsed = time.monotonic() - started
    assert elapsed < 2.2


def test_execute_agent_with_timeout_hard_cancels_running_agent():
    from core.application.orchestration.graph.nodes import (
        AgentHardCancelled,
        _execute_agent_with_timeout,
    )

    class _SlowAgent:
        def execute(self, _agent_input):
            time.sleep(2.5)
            return SimpleNamespace(
                status="pass",
                artifacts={},
                summary="done",
                confidence=1.0,
                metadata={},
            )

    with (
        patch("core.application.orchestration.graph.nodes._is_job_cancelled", return_value=True),
        patch(
            "core.application.orchestration.graph.nodes._cancel_poll_interval_seconds",
            return_value=0.2,
        ),
    ):
        started = time.monotonic()
        _raises_input_501_1 = _SlowAgent()
        with pytest.raises(AgentHardCancelled):
            _execute_agent_with_timeout(
                agent=_raises_input_501_1,
                agent_input={},
                timeout_seconds=0,
                agent_id="writer",
                state={"job_id": 456, "workflow_id": "wf-1"},
            )
        elapsed = time.monotonic() - started
        assert elapsed < 1.5


def test_cancelled_agent_process_entry_does_not_publish_a_result(monkeypatch):
    from core.application.orchestration.graph import nodes_agent_runtime as runtime

    class _CancelledAgent:
        def execute(self, _agent_input):
            raise runtime._AgentProcessCancellation

    results = []
    monkeypatch.setattr(runtime.signal, "signal", lambda *_args: None)
    runtime._agent_process_entry(
        _CancelledAgent(), {}, SimpleNamespace(put=lambda result: results.append(result))
    )
    assert results == []


@pytest.mark.parametrize("inherit_worker_handler", [False, True])
def test_hard_cancel_stops_managed_extension_descendant(tmp_path, inherit_worker_handler):
    import contextlib
    import os
    import signal

    from core.application.orchestration.graph.nodes import (
        AgentHardCancelled,
        _execute_agent_with_timeout,
    )
    from core.infrastructure.extension_runtime.extension_runtime_invocation import (
        _run_subprocess_capped,
    )

    pid_path = tmp_path / "managed-extension.pid"
    completion_path = tmp_path / "agent-completed"
    previous_handler = signal.getsignal(signal.SIGTERM)

    class _ManagedExtensionAgent:
        def execute(self, _agent_input):
            result = _run_subprocess_capped(
                [
                    sys.executable,
                    "-c",
                    "import os, pathlib, sys, time; "
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
                    str(pid_path),
                ],
                cwd=None,
                env=dict(os.environ),
                timeout=30,
            )
            completion_path.write_text("continued after cancellation")
            return result

    def descendant_running(pid):
        try:
            # A killed process may remain a zombie until the container's init reaps it.
            return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
        except FileNotFoundError:
            return False

    managed_agent = _ManagedExtensionAgent()
    try:
        if inherit_worker_handler:
            signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
        with (
            patch(
                "core.application.orchestration.graph.nodes._is_job_cancelled",
                side_effect=lambda _job_id: pid_path.exists() and bool(pid_path.read_text()),
            ),
            patch(
                "core.application.orchestration.graph.nodes._cancel_poll_interval_seconds",
                return_value=0.2,
            ),
            pytest.raises(AgentHardCancelled),
        ):
            _execute_agent_with_timeout(
                agent=managed_agent,
                agent_input={},
                timeout_seconds=10,
                agent_id="managed-extension",
                state={"job_id": "cancelled-child", "workflow_id": "child-workflow"},
            )
        descendant_pid = int(pid_path.read_text())
        deadline = time.monotonic() + 1
        while descendant_running(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not descendant_running(descendant_pid), (
            "Managed extension survived child cancellation"
        )
        assert not completion_path.exists(), "Cancelled agent continued execution"
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        if pid_path.exists() and pid_path.read_text():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_path.read_text()), signal.SIGKILL)


def test_execute_agent_with_timeout_emits_heartbeats_during_long_run():
    from core.application.orchestration.graph.nodes import _execute_agent_with_timeout

    class _SlowAgent:
        def execute(self, _agent_input):
            time.sleep(1.3)
            return SimpleNamespace(
                status="pass",
                artifacts={},
                summary="done",
                confidence=1.0,
                metadata={},
            )

    with (
        patch(
            "core.application.orchestration.graph.nodes._heartbeat_interval_seconds",
            return_value=0.3,
        ),
        patch("core.application.orchestration.graph.nodes._is_job_cancelled", return_value=False),
        patch("core.application.orchestration.graph.nodes._emit_agent_heartbeat") as heartbeat_mock,
    ):
        output = _execute_agent_with_timeout(
            agent=_SlowAgent(),
            agent_input={},
            timeout_seconds=5,
            agent_id="writer",
            state={"job_id": 789, "workflow_id": "wf-1"},
        )
    assert output.status == "pass"
    assert heartbeat_mock.call_count >= 2


def test_emit_agent_heartbeat_updates_invoke_parent_job_when_child_runs_inline():
    from core.application.orchestration.graph.nodes import _emit_agent_heartbeat

    parent_runtime_calls: list[dict] = []

    class _FakeLogger:
        def update_job_runtime(self, job_id, **kwargs):
            parent_runtime_calls.append({"job_id": job_id, **dict(kwargs)})

    state = {
        "job_id": 1907,
        "task_source": "invoke_workflow",
        "workflow_id": "review_cycle",
        "parent_job_id": 1906,
        "invoke_parent_job_id": 1906,
        "invoke_parent_step_id": "story_loop",
        "invoke_parent_step_agent_id": "facilitator",
    }

    with (
        patch(
            "core.application.orchestration.agents.common.update_runtime_truth"
        ) as runtime_truth_mock,
        patch(
            "core.application.orchestration.graph.nodes._get_observability_logger",
            return_value=_FakeLogger(),
        ),
    ):
        _emit_agent_heartbeat(state, agent_id="writer")

    runtime_truth_mock.assert_called_once()
    assert parent_runtime_calls == [
        {
            "job_id": "1906",
            "current_agent": "facilitator",
            "heartbeat_now": True,
            "last_response_excerpt": "Inline child workflow 'review_cycle' is active (job 1907)",
        }
    ]


def test_execute_agent_with_timeout_drains_ready_result_queue_before_child_exits():
    from core.application.orchestration.graph.nodes import _execute_agent_with_timeout

    payload = {
        "ok": True,
        "output": {
            "status": "pass",
            "artifacts": {"code_output": "x" * 1024},
            "summary": "done",
            "confidence": 0.9,
            "metadata": {},
        },
    }

    class _FakeProcess:
        def __init__(self):
            self.alive = False
            self.exitcode = 0
            self.pid = 12345

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            return None

        def terminate(self):
            self.alive = False

        def kill(self):
            self.alive = False

    class _FakeQueue:
        def __init__(self, proc):
            self.proc = proc
            self._delivered = False

        def get(self, timeout=None):
            if self._delivered:
                raise queue.Empty
            self._delivered = True
            self.proc.alive = False
            return payload

        def close(self):
            return None

    class _FakeContext:
        def __init__(self):
            self.proc = _FakeProcess()
            self.q = _FakeQueue(self.proc)

        def Queue(self, maxsize=1):
            return self.q

        def Process(self, target, args, daemon, name):
            return self.proc

    fake_ctx = _FakeContext()

    with (
        patch(
            "core.application.orchestration.graph.nodes.mp.get_all_start_methods",
            return_value=["fork"],
        ),
        patch("core.application.orchestration.graph.nodes.mp.get_context", return_value=fake_ctx),
        patch("core.application.orchestration.graph.nodes._is_job_cancelled", return_value=False),
    ):
        output = _execute_agent_with_timeout(
            agent=object(),
            agent_input={},
            timeout_seconds=1,
            agent_id="writer",
            state={"job_id": 999, "workflow_id": "wf-1"},
        )

    assert output.status == "pass"
    assert output.summary == "done"
    assert output.artifacts["code_output"] == "x" * 1024


@pytest.mark.usefixtures("captured_output_contract")
def test_wrap_agent_as_node_fails_fast_on_missing_required_skill_input():
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def __init__(self):
            self.called = False

        def execute(self, _agent_input):
            self.called = True
            return SimpleNamespace(
                status="pass",
                artifacts={"git_diff": "diff --git"},
                summary="done",
                confidence=1.0,
                metadata={},
            )

    def _schema_loader(_skill: str, schema_kind: str):
        if schema_kind == "input_schema":
            return (
                {
                    "type": "object",
                    "properties": {"must_have_input": {"type": "text"}},
                    "required": ["must_have_input"],
                },
                None,
            )
        return ({"type": "object", "properties": {}, "required": []}, None)

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Developer",
        config={},
        timeout_minutes=0,
        skills=["write_section"],
    )
    agent = _Agent()
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(agent, manifest)
    state = _run_state(
        [{"agent": "writer", "skill": "write_section"}],
        job_id=None,
        workflow_id="wf-1",
        agent_messages=[],
    )

    with (
        patch.dict(
            sys.modules,
            {"core.application.orchestration.agents.common": _stub_agents_common_module()},
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = node(state)

    assert agent.called is False
    messages = update.get("agent_messages", [])
    assert messages
    output = messages[-1]
    assert output.get("status") == "fail"
    assert "must_have_input" in " ".join(output.get("errors") or [])


@pytest.mark.usefixtures("captured_output_contract")
def test_wrap_agent_as_node_reads_required_input_from_materialized_inputs():
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def __init__(self):
            self.called = False

        def execute(self, _agent_input):
            self.called = True
            return SimpleNamespace(
                status="pass",
                artifacts={"git_diff": "diff --git"},
                summary="done",
                confidence=1.0,
                metadata={},
            )

    def _schema_loader(_skill: str, schema_kind: str):
        if schema_kind == "input_schema":
            return (
                {
                    "type": "object",
                    "properties": {"bug_description": {"type": "text"}},
                    "required": ["bug_description"],
                },
                None,
            )
        return ({"type": "object", "properties": {}, "required": []}, None)

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Developer",
        config={},
        timeout_minutes=0,
        skills=["correct_draft"],
    )
    agent = _Agent()
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(agent, manifest)

    state = _run_state(
        [{"agent": "writer", "skill": "correct_draft"}],
        job_id=None,
        workflow_id="wf-1",
        materialized_inputs={"bug_description": "Fix homepage title typo"},
        agent_messages=[],
    )

    with (
        patch.dict(
            sys.modules,
            {"core.application.orchestration.agents.common": _stub_agents_common_module()},
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = node(state)

    assert agent.called is True
    messages = update.get("agent_messages", [])
    assert messages
    output = messages[-1]
    assert output.get("status") == "pass"


@pytest.mark.usefixtures("captured_output_contract")
def test_wrap_agent_as_node_materializes_required_input_from_step_mapping():
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def __init__(self):
            self.called = False

        def execute(self, _agent_input):
            self.called = True
            return SimpleNamespace(
                status="pass",
                artifacts={"risk_review": "Looks good"},
                summary="reviewed",
                confidence=1.0,
                metadata={},
            )

    def _schema_loader(_skill: str, schema_kind: str):
        if schema_kind == "input_schema":
            return (
                {
                    "type": "object",
                    "properties": {"request": {"type": "text"}},
                    "required": ["request"],
                },
                None,
            )
        return ({"type": "object", "properties": {}, "required": []}, None)

    manifest = SimpleNamespace(
        agent_id="demo_reviewer",
        display_name="Demo Reviewer",
        config={},
        timeout_minutes=0,
        skills=["review_skill"],
    )
    agent = _Agent()
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(agent, manifest)

    state = _run_state(
        [
            {
                "agent": "demo_reviewer",
                "skill": "review_skill",
                "input_mapping_jsonb": {"request": "state.content_brief"},
                "override": {
                    "agent": {
                        "input_mapping": {"request": "state.content_brief"},
                    }
                },
            }
        ],
        job_id=None,
        workflow_id="wf-1",
        content_brief={"headline": "Launch email", "body": "Draft body"},
        materialized_inputs={},
        agent_messages=[],
    )

    with (
        patch.dict(
            sys.modules,
            {"core.application.orchestration.agents.common": _stub_agents_common_module()},
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = node(state)

    assert agent.called is True
    assert state["materialized_inputs"]["request"] == {
        "headline": "Launch email",
        "body": "Draft body",
    }
    messages = update.get("agent_messages", [])
    assert messages
    output = messages[-1]
    assert output.get("status") == "pass"


@pytest.mark.usefixtures("captured_output_contract")
def test_wrap_agent_as_node_materializes_output_mapping_into_state_patch():
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def execute(self, _agent_input):
            return SimpleNamespace(
                status="pass",
                artifacts={
                    "draft_subject": "Approved subject",
                    "draft_body": "Approved body",
                },
                summary="generated",
                confidence=1.0,
                metadata={},
            )

    def _schema_loader(_skill: str, schema_kind: str):
        if schema_kind == "input_schema":
            return ({"type": "object", "properties": {}, "required": []}, None)
        return (
            {
                "type": "object",
                "properties": {
                    "draft_subject": {"type": "text"},
                    "draft_body": {"type": "text"},
                },
                "required": ["draft_subject", "draft_body"],
            },
            None,
        )

    manifest = SimpleNamespace(
        agent_id="sample_launch_copywriter",
        display_name="Demo Launch Copywriter",
        config={},
        timeout_minutes=0,
        skills=["write_launch_copy"],
    )
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(_Agent(), manifest)

    state = _run_state(
        [
            {
                "agent": "sample_launch_copywriter",
                "skill": "write_launch_copy",
                "output_mapping_jsonb": {
                    "draft_subject": "state.approved_subject",
                    "draft_body": "state.approved_body",
                },
            }
        ],
        job_id=None,
        workflow_id="wf-1",
        workflow_data={},
        materialized_inputs={},
        agent_messages=[],
    )

    with (
        patch.dict(
            sys.modules,
            {"core.application.orchestration.agents.common": _stub_agents_common_module()},
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = node(state)

    assert update["approved_subject"] == "Approved subject"
    assert update["approved_body"] == "Approved body"
    assert update["workflow_data"]["approved_subject"] == "Approved subject"
    assert update["workflow_data"]["approved_body"] == "Approved body"
    messages = update.get("agent_messages", [])
    assert messages
    artifacts = messages[-1]["artifacts"]
    assert artifacts["_state_patch"]["approved_subject"] == "Approved subject"
    assert artifacts["_state_patch"]["approved_body"] == "Approved body"


def test_agent_output_mapping_cannot_write_host_owned_test_mode():
    from core.application.orchestration.graph.nodes_contracts import (
        _apply_agent_step_output_mapping,
    )

    artifacts = {"enabled": True}
    state_updates: dict = {}

    _apply_agent_step_output_mapping(
        step={"output_mapping_jsonb": {"enabled": "state.test_mode"}},
        artifacts=artifacts,
        state={"test_mode": False},
        state_updates=state_updates,
    )

    assert state_updates == {}
    assert artifacts["output_mapping_errors"] == [
        {
            "code": "output_mapping_reserved_target",
            "blocked_targets": ["state.test_mode"],
        }
    ]


def test_http_response_target_cannot_overwrite_trusted_test_mode():
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.runtime_identity import runtime_identity_from_state

    state = _run_state(
        [
            {
                "step_kind": "http_request",
                "override": {
                    "http_request": {
                        "method": "GET",
                        "url": "https://example.invalid",
                        "response_target": "state.trusted_runtime_context.test_mode",
                    }
                },
            }
        ],
        trusted_runtime_context={"test_mode": False},
        agent_messages=[],
    )

    with (
        patch(
            "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
            return_value={
                "ok": True,
                "status_code": 200,
                "response_mode": "inline",
                "response_body": {"enabled": True},
            },
        ),
        patch(
            "core.application.orchestration.agents.common.publish_agent_message",
            return_value=True,
        ),
    ):
        update = execute_step_node(state)

    merged = {**state, **update}
    assert update.get("trusted_runtime_context") is None
    assert runtime_identity_from_state(merged).test_mode is False


def test_connector_message_uses_step_display_name_and_keeps_structured_registry_id():
    from core.application.orchestration.graph.nodes import execute_step_node

    connector_id = "extension.5cc90460-491e-45e3-a47c-c1110afc37db.sample_content_tools"
    state = _run_state(
        [
            {
                "step_id": "measure_failure_reason",
                "display_name": "Measure failure reason",
                "step_kind": "connector",
                "connector_id": connector_id,
            }
        ],
        agent_messages=[],
    )

    with (
        patch(
            "core.application.orchestration.connector_step_runtime.execute_connector_step",
            return_value={
                "ok": False,
                "runtime": "extension_action",
                "detail": "artifact_upload_missing",
            },
        ),
        patch(
            "core.application.orchestration.agents.common.publish_agent_message",
            return_value=True,
        ),
    ):
        update = execute_step_node(state)

    message = update["agent_messages"][-1]
    assert message["summary"] == (
        "Connector step 'Measure failure reason' failed via extension_action: "
        "artifact_upload_missing"
    )
    assert connector_id not in message["summary"]
    assert message["artifacts"]["connector_id"] == connector_id


def test_connector_message_records_the_actual_step_execution_window():
    from core.application.orchestration.graph.nodes import execute_step_node

    started_at = datetime(2026, 9, 10, 8, 9, 0, tzinfo=UTC)
    completed_at = datetime(2026, 9, 10, 8, 9, 2, 250000, tzinfo=UTC)

    class _Clock:
        values = iter((started_at, completed_at))

        @classmethod
        def now(cls, _tz):
            return next(cls.values)

    state = _run_state(
        [
            {
                "step_id": "compose_plan",
                "display_name": "Compose plan",
                "step_kind": "connector",
                "connector_id": "connector.std.text",
            }
        ],
        agent_messages=[],
    )

    with (
        patch(
            "core.application.orchestration.connector_step_runtime.execute_connector_step",
            return_value={
                "ok": True,
                "runtime": "tool_manifest",
                "result": {"text": "ready"},
            },
        ),
        patch("core.application.orchestration.graph.nodes.datetime", _Clock),
        patch(
            "core.application.orchestration.agents.common.publish_agent_message",
            return_value=True,
        ),
    ):
        update = execute_step_node(state)

    timestamps = update["agent_messages"][-1]["timestamps"]
    assert timestamps["started_at"] == started_at.isoformat()
    assert timestamps["completed_at"] == completed_at.isoformat()


def _connector_step_update(state: dict) -> dict:
    from core.application.orchestration.graph.nodes import execute_step_node

    with (
        patch(
            "core.application.orchestration.connector_step_runtime.execute_connector_step",
            return_value={"ok": True, "runtime": "tool_manifest", "result": {"text": "ready"}},
        ),
        patch(
            "core.application.orchestration.agents.common.publish_agent_message",
            return_value=True,
        ),
    ):
        return execute_step_node(state)


_CONNECTOR_STEP = {
    "step_id": "compose_plan",
    "step_kind": "connector",
    "connector_id": "connector.std.text",
}


def test_step_result_carries_the_exact_execution_and_attempt_of_the_cursor():
    state = _run_state([_CONNECTOR_STEP], attempt=3)
    cursor = state["run_cursor"]

    message = _connector_step_update(state)["agent_messages"][-1]

    assert message["step_id"] == "compose_plan"
    assert message["step_execution_id"] == cursor["step_execution_id"]
    assert message["attempt"] == 3
    assert message["group_id"] is None


def test_retry_dispatch_applies_only_to_the_execution_and_attempt_it_names():
    state = _run_state([_CONNECTOR_STEP], attempt=2)
    execution_id = state["run_cursor"]["step_execution_id"]
    dispatch = {
        "step_id": "compose_plan",
        "step_execution_id": execution_id,
        "attempt": 2,
        "deadline_epoch_seconds": time.time() - 1,
    }

    elapsed = _connector_step_update({**state, "operation_retry_dispatch": dispatch})
    assert elapsed["agent_messages"][-1]["artifacts"]["error"] == "retry_deadline_exhausted"
    assert elapsed["operation_retry_dispatch"] is None

    # A dispatch for an older attempt or another execution of the same step
    # is not this invocation's deadline.
    for stale in ({**dispatch, "attempt": 1}, {**dispatch, "step_execution_id": str(uuid4())}):
        update = _connector_step_update({**state, "operation_retry_dispatch": stale})
        assert update["agent_messages"][-1]["status"] == "pass"
        assert "operation_retry_dispatch" not in update


def test_connector_message_keeps_only_the_workflow_data_delta_from_state_patch():
    """Durable diagnostics must not copy the entire accumulated workflow state per loop."""
    from core.application.orchestration.graph.nodes import execute_step_node

    large_batches = [[{"id": index}] for index in range(10_000)]
    previous_workflow_data = {"update_batches": large_batches}
    next_workflow_data = {
        **previous_workflow_data,
        "current_update_result": {"update": [{"id": 1}]},
        "current_update_status": 200,
    }
    state = _run_state(
        [
            {
                "step_id": "update_current_batch",
                "display_name": "Update current stock batch",
                "step_kind": "connector",
                "connector_id": "extension.example.catalog",
            }
        ],
        workflow_data=previous_workflow_data,
        agent_messages=[],
    )

    with (
        patch(
            "core.application.orchestration.connector_step_runtime.execute_connector_step",
            return_value={
                "ok": True,
                "runtime": "extension_action",
                "state_patch": {
                    "current_update_result": {"update": [{"id": 1}]},
                    "current_update_status": 200,
                    "workflow_data": next_workflow_data,
                },
            },
        ),
        patch(
            "core.application.orchestration.agents.common.publish_agent_message",
            return_value=True,
        ),
    ):
        update = execute_step_node(state)

    assert update["workflow_data"] == next_workflow_data
    artifacts = update["agent_messages"][-1]["artifacts"]
    assert artifacts["_state_patch"] == {
        "current_update_result": {"update": [{"id": 1}]},
        "current_update_status": 200,
        "workflow_data": {
            "current_update_result": {"update": [{"id": 1}]},
            "current_update_status": 200,
        },
    }
    assert "update_batches" not in artifacts["workflow_data"]


@pytest.mark.usefixtures("captured_output_contract")
def test_wrap_agent_as_node_forces_needs_rework_when_output_contract_invalid(
    captured_output_contract,
):
    captured_output_contract.extend(
        {"name": name, "type": "text", "required": True} for name in ["git_diff"]
    )
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def execute(self, _agent_input):
            return SimpleNamespace(
                status="pass",
                artifacts={},
                summary="done",
                confidence=1.0,
                metadata={},
            )

    def _schema_loader(_skill: str, schema_kind: str):
        if schema_kind == "input_schema":
            return ({"type": "object", "properties": {}, "required": []}, None)
        return (
            {
                "type": "object",
                "properties": {"git_diff": {"type": "text"}},
                "required": ["git_diff"],
            },
            None,
        )

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Developer",
        config={},
        timeout_minutes=0,
        skills=["write_section"],
    )
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(_Agent(), manifest)
    state = _run_state(
        [{"agent": "writer", "skill": "write_section"}],
        job_id=None,
        workflow_id="wf-1",
        agent_messages=[],
    )

    with (
        patch.dict(
            sys.modules,
            {"core.application.orchestration.agents.common": _stub_agents_common_module()},
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = node(state)

    messages = update.get("agent_messages", [])
    assert messages
    output = messages[-1]
    assert output.get("status") == "fail"
    assert "missing required output 'git_diff'" in " ".join(output.get("errors") or []).lower()


@pytest.mark.usefixtures("captured_output_contract")
def test_wrap_agent_as_node_preserves_failed_agent_reason_without_output_contract_noise(
    captured_output_contract,
):
    captured_output_contract.extend(
        {"name": name, "type": "text", "required": True}
        for name in ["source_brief", "complexity_estimate", "research_brief"]
    )
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def execute(self, _agent_input):
            return SimpleNamespace(
                status="fail",
                artifacts={"error": "Connection error", "_execution_error": True},
                summary="Researcher failed: Connection error",
                confidence=0.0,
                metadata={},
            )

    def _schema_loader(_skill: str, schema_kind: str):
        if schema_kind == "input_schema":
            return ({"type": "object", "properties": {}, "required": []}, None)
        return (
            {
                "type": "object",
                "properties": {
                    "source_brief": {"type": "text"},
                    "complexity_estimate": {"type": "text"},
                    "research_brief": {"type": "text"},
                },
                "required": ["source_brief", "complexity_estimate", "research_brief"],
            },
            None,
        )

    manifest = SimpleNamespace(
        agent_id="researcher",
        display_name="Business Researcher",
        config={},
        timeout_minutes=0,
        skills=["draft_outline"],
    )
    with patch.dict(
        sys.modules, {"core.application.orchestration.agents.common": _stub_agents_common_module()}
    ):
        node = wrap_agent_as_node(_Agent(), manifest)
    state = _run_state(
        [{"agent": "researcher", "skill": "draft_outline"}],
        job_id=None,
        workflow_id="wf-1",
        agent_messages=[],
    )

    with (
        patch.dict(
            sys.modules,
            {"core.application.orchestration.agents.common": _stub_agents_common_module()},
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = node(state)

    messages = update.get("agent_messages", [])
    assert messages
    output = messages[-1]
    assert output.get("status") == "fail"
    assert output.get("summary") == "Researcher failed: Connection error"
    assert "connection error" in " ".join(output.get("errors") or []).lower()
    assert "missing required output" not in " ".join(output.get("errors") or []).lower()


def test_validate_skill_output_contract_accepts_empty_json_and_list_outputs():
    from core.application.orchestration.graph.nodes import _validate_skill_output_contract

    schema = {
        "type": "object",
        "properties": {
            "available_checks": {"type": "json"},
            "checks_run": {"type": "json"},
            "blocking_issues": {"type": "json"},
        },
        "required": ["available_checks", "checks_run", "blocking_issues"],
    }

    with patch(
        "core.application.orchestration.graph.nodes._load_skill_schema", return_value=(schema, None)
    ):
        errors = _validate_skill_output_contract(
            "review_summary",
            {
                "available_checks": {},
                "checks_run": [],
                "blocking_issues": [],
            },
        )

    assert errors == []


def test_validate_skill_output_contract_still_rejects_empty_required_text():
    from core.application.orchestration.graph.nodes import _validate_skill_output_contract

    schema = {
        "type": "object",
        "properties": {
            "review_verdict": {"type": "text"},
        },
        "required": ["review_verdict"],
    }

    with patch(
        "core.application.orchestration.graph.nodes._load_skill_schema", return_value=(schema, None)
    ):
        errors = _validate_skill_output_contract(
            "review_summary",
            {"review_verdict": ""},
        )

    assert errors == ["Required output 'review_verdict' is empty"]


def test_load_skill_schema_falls_back_to_output_rows_when_registry_row_load_fails():
    from core.application.orchestration.graph.nodes import _load_skill_schema

    class _Store:
        def __init__(self) -> None:
            self.cleared = False

        def get_skill(self, _skill: str):
            raise RuntimeError('prepared statement "_pg3_16" already exists')

        def _clear_failed_transaction(self) -> None:
            self.cleared = True

        def get_skill_outputs(self, _skill: str):
            return [
                {
                    "name": "item_verdict",
                    "type": "text",
                    "description": "Pass/fail verdict for the current story",
                    "required": True,
                },
                {
                    "name": "checker_report",
                    "type": "text",
                    "description": "Verifier narrative",
                    "required": False,
                },
            ]

    store = _Store()
    registry = SimpleNamespace(db_store=store)

    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry,
    ):
        schema, error = _load_skill_schema("verify_section", "output_schema")

    assert error is None
    assert store.cleared is True
    assert schema["type"] == "object"
    assert schema["required"] == ["item_verdict"]
    assert schema["properties"]["item_verdict"]["type"] == "string"
    assert schema["properties"]["checker_report"]["description"] == "Verifier narrative"


def test_load_skill_schema_uses_projected_skill_registry_manifest():
    from core.application.orchestration.graph.nodes import _load_skill_schema

    schema = {
        "type": "object",
        "properties": {"request": {"type": "string"}},
        "required": ["request"],
    }

    class _Store:
        def get_skill(self, _skill: str):
            return None

    projected_skill = SimpleNamespace(input_schema=schema, output_schema={"type": "object"})
    registry = SimpleNamespace(
        db_store=_Store(),
        skills=SimpleNamespace(get_skill=lambda _skill: projected_skill),
    )

    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry,
    ):
        loaded, error = _load_skill_schema(
            "extension.install-1.sample_review_launch_risks",
            "input_schema",
        )

    assert error is None
    assert loaded == schema


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param(None, id="direct-json-null"),
        pytest.param("literal:null", id="decoded-json-null"),
        pytest.param("state.inputs.nullable_value", id="resolved-present-null"),
    ],
)
@pytest.mark.usefixtures("captured_output_contract")
def test_agent_mapping_retains_explicit_optional_null_at_agent_boundary(selector):
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def __init__(self):
            self.received = None

        def execute(self, agent_input):
            self.received = dict(agent_input.state_ref["materialized_inputs"])
            return SimpleNamespace(
                status="pass", artifacts={}, summary="received", confidence=1.0, metadata={}
            )

    def _schema_loader(_skill, schema_kind):
        if schema_kind == "input_schema":
            return (
                {
                    "type": "object",
                    "properties": {
                        "nullable_value": {"type": ["string", "null"]},
                        "clear_existing": {"type": ["string", "null"]},
                        "zero": {"type": "integer"},
                        "flag": {"type": "boolean"},
                        "empty": {"type": "string"},
                    },
                    "required": ["zero", "flag"],
                },
                None,
            )
        return ({"type": "object", "properties": {}, "required": []}, None)

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Writer",
        config={},
        timeout_minutes=0,
        skills=["literal_contract"],
    )
    agent = _Agent()
    state = _run_state(
        [
            {
                "step_kind": "agent",
                "agent": "writer",
                "skill": "literal_contract",
                "input_mapping_jsonb": {
                    "nullable_value": selector,
                    "clear_existing": None,
                    "zero": 0,
                    "flag": False,
                    "empty": "literal:",
                },
            }
        ],
        job_id=None,
        workflow_id="wf-1",
        inputs={"nullable_value": None},
        materialized_inputs={"untouched": "prior", "clear_existing": "previous"},
        agent_messages=[],
    )
    assert "nullable_value" in state["inputs"]
    assert "nullable_value" not in state["materialized_inputs"]
    with (
        patch.dict(
            sys.modules,
            {
                "core.application.orchestration.agents.common": _stub_agents_common_module(),
            },
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = wrap_agent_as_node(agent, manifest)(state)

    assert agent.received is not None
    assert update["agent_messages"][-1]["status"] == "pass"
    assert "nullable_value" in agent.received
    assert agent.received["nullable_value"] is None
    assert agent.received["clear_existing"] is None
    assert "nullable_value" in state["materialized_inputs"]
    assert state["materialized_inputs"]["nullable_value"] is None
    assert agent.received["zero"] == 0
    assert type(agent.received["zero"]) is int
    assert agent.received["flag"] is False
    assert agent.received["empty"] == ""
    assert agent.received["untouched"] == "prior"
    preview = update["agent_messages"][-1]["artifacts"]["input_payload"]
    assert "nullable_value" in preview
    assert preview["nullable_value"] is None


@pytest.mark.parametrize(
    ("initial", "selector", "expected", "field_type"),
    [
        pytest.param(False, 0, 0, "integer", id="direct-number-replaces-boolean"),
        pytest.param(0, False, False, "boolean", id="direct-boolean-replaces-number"),
        pytest.param(False, "literal:0", 0, "integer", id="decoded-number-replaces-boolean"),
        pytest.param(0, "literal:false", False, "boolean", id="decoded-boolean-replaces-number"),
    ],
)
@pytest.mark.usefixtures("captured_output_contract")
def test_agent_mapping_replaces_equal_python_value_with_authored_json_type(
    initial,
    selector,
    expected,
    field_type,
):
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def __init__(self):
            self.received = None

        def execute(self, agent_input):
            self.received = dict(agent_input.state_ref["materialized_inputs"])
            return SimpleNamespace(
                status="pass", artifacts={}, summary="received", confidence=1.0, metadata={}
            )

    def _schema_loader(_skill, schema_kind):
        if schema_kind == "input_schema":
            return (
                {
                    "type": "object",
                    "properties": {"typed": {"type": field_type}},
                    "required": ["typed"],
                },
                None,
            )
        return ({"type": "object", "properties": {}, "required": []}, None)

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Writer",
        config={},
        timeout_minutes=0,
        skills=["literal_contract"],
    )
    agent = _Agent()
    state = _run_state(
        [
            {
                "step_kind": "agent",
                "agent": "writer",
                "skill": "literal_contract",
                "input_mapping_jsonb": {"typed": selector},
            }
        ],
        job_id=None,
        workflow_id="wf-1",
        materialized_inputs={"typed": initial, "untouched": "prior"},
        agent_messages=[],
    )
    with (
        patch.dict(
            sys.modules,
            {
                "core.application.orchestration.agents.common": _stub_agents_common_module(),
            },
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = wrap_agent_as_node(agent, manifest)(state)

    assert agent.received is not None
    assert update["agent_messages"][-1]["status"] == "pass"
    assert agent.received["typed"] == expected
    assert type(agent.received["typed"]) is type(expected)
    assert state["materialized_inputs"]["typed"] == expected
    assert type(state["materialized_inputs"]["typed"]) is type(expected)
    assert agent.received["untouched"] == "prior"
    preview = update["agent_messages"][-1]["artifacts"]["input_payload"]
    assert preview["typed"] == expected
    assert type(preview["typed"]) is type(expected)


@pytest.mark.parametrize(
    ("initial", "selector", "field_type", "leaf"),
    [
        pytest.param(
            {"enabled": False},
            {"enabled": 0},
            "object",
            "enabled",
            id="raw-object-numeric-leaf-replaces-boolean",
        ),
        pytest.param(
            [False],
            "literal:[0]",
            "array",
            0,
            id="decoded-list-numeric-leaf-replaces-boolean",
        ),
    ],
)
@pytest.mark.usefixtures("captured_output_contract")
def test_agent_mapping_replaces_nested_equal_python_boolean_with_authored_number(
    initial,
    selector,
    field_type,
    leaf,
):
    from core.application.orchestration.graph.nodes import wrap_agent_as_node

    class _Agent:
        def __init__(self):
            self.received = None

        def execute(self, agent_input):
            self.received = dict(agent_input.state_ref["materialized_inputs"])
            return SimpleNamespace(
                status="pass", artifacts={}, summary="received", confidence=1.0, metadata={}
            )

    def _schema_loader(_skill, schema_kind):
        if schema_kind == "input_schema":
            return (
                {
                    "type": "object",
                    "properties": {"typed": {"type": field_type}},
                    "required": ["typed"],
                },
                None,
            )
        return ({"type": "object", "properties": {}, "required": []}, None)

    manifest = SimpleNamespace(
        agent_id="writer",
        display_name="Writer",
        config={},
        timeout_minutes=0,
        skills=["literal_contract"],
    )
    agent = _Agent()
    state = _run_state(
        [
            {
                "step_kind": "agent",
                "agent": "writer",
                "skill": "literal_contract",
                "input_mapping_jsonb": {"typed": selector},
            }
        ],
        job_id=None,
        workflow_id="wf-1",
        materialized_inputs={"typed": initial, "untouched": {"note": "prior"}},
        agent_messages=[],
    )
    with (
        patch.dict(
            sys.modules,
            {
                "core.application.orchestration.agents.common": _stub_agents_common_module(),
            },
        ),
        patch(
            "core.application.orchestration.graph.nodes._load_skill_schema",
            side_effect=_schema_loader,
        ),
    ):
        update = wrap_agent_as_node(agent, manifest)(state)

    assert agent.received is not None
    assert update["agent_messages"][-1]["status"] == "pass"
    assert agent.received["typed"][leaf] == 0
    assert type(agent.received["typed"][leaf]) is int
    assert state["materialized_inputs"]["typed"][leaf] == 0
    assert type(state["materialized_inputs"]["typed"][leaf]) is int
    assert agent.received["untouched"] == {"note": "prior"}
    assert state["materialized_inputs"]["untouched"] == {"note": "prior"}
    assert initial[leaf] is False
    preview = update["agent_messages"][-1]["artifacts"]["input_payload"]
    assert preview["typed"][leaf] == 0
    assert type(preview["typed"][leaf]) is int
