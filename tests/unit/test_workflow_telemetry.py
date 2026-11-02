from __future__ import annotations

import multiprocessing
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from core.application.observability import workflow_telemetry
from core.infrastructure.observability import otel_instruments
from core.infrastructure.observability.otel_export_guards import (
    DEFAULT_SPAN_EXPORT_POLICY,
    OwnerProcess,
    SanitizingSpanExporter,
)


class _Telemetry:
    def __init__(self, *, owner: OwnerProcess | None = None, traces=True, metrics=True) -> None:
        self.spans = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        self.tracer_provider = TracerProvider(shutdown_on_exit=False)
        self.tracer_provider.add_span_processor(
            SimpleSpanProcessor(SanitizingSpanExporter(self.spans, DEFAULT_SPAN_EXPORT_POLICY))
        )
        self.meter_provider = MeterProvider(metric_readers=[self.reader], shutdown_on_exit=False)
        self.owner = owner or OwnerProcess()
        self.settings = SimpleNamespace(
            traces=SimpleNamespace(enabled=traces),
            metrics=SimpleNamespace(enabled=metrics),
            logs=SimpleNamespace(enabled=False),
        )

    def finished(self) -> list:
        return list(self.spans.get_finished_spans())

    def span(self, name: str):
        matches = [span for span in self.finished() if span.name == name]
        assert len(matches) == 1, [span.name for span in self.finished()]
        return matches[0]

    def points(self, metric_name: str) -> list:
        data = self.reader.get_metrics_data()
        found = []
        for resource in getattr(data, "resource_metrics", None) or []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == metric_name:
                        found.extend(metric.data.data_points)
        return found


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> _Telemetry:
    state = _Telemetry()
    monkeypatch.setattr(otel_instruments, "current_telemetry", lambda: state)
    otel_instruments._reset_for_tests()
    # Tests share one context; a node binding must not leak into the next test.
    token = workflow_telemetry._node_scope.set(None)
    yield state
    workflow_telemetry._node_scope.reset(token)
    otel_instruments._reset_for_tests()


def _callback():
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    return PlatformLoggerCallback(MagicMock(), project_id="project-1", job_id="job-1")


def _dispatched_payload(kind: str = "agent", attempt: int = 1) -> dict:
    return {
        "run_cursor": {"step_id": "s1", "step_execution_id": "exec-1", "attempt": attempt},
        "dispatched_step_execution": {
            "step_id": "s1",
            "step_execution_id": "exec-1",
            "attempt": attempt,
            "step_kind": kind,
        },
    }


def _node(callback, run_id: str, name: str, payload: dict) -> None:
    callback.on_chain_start(
        inputs=payload,
        run_id=run_id,
        parent_run_id="root",
        name=name,
        metadata={"langgraph_node": name},
    )


def _model_call(callback, run_id: str, parent: str, *, provider="openai", repeat=1) -> None:
    callback.on_chat_model_start(
        {},
        [[]],
        run_id=run_id,
        parent_run_id=parent,
        metadata={"flow_steward_provider_policy": provider, "flow_steward_model": "gpt-5.6-sol"},
    )
    response = SimpleNamespace(
        llm_output={
            "model_name": "gpt-5.6-sol",
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        },
        generations=[],
    )
    for _ in range(repeat):
        callback.on_llm_end(response=response, run_id=run_id, parent_run_id=parent)


def test_segment_step_and_model_spans_nest_and_measure_once(telemetry: _Telemetry) -> None:
    callback = _callback()
    callback.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    _node(callback, "node-1", "execute_step", _dispatched_payload("agent"))
    _model_call(callback, "llm-1", "node-1", repeat=2)
    callback.on_chain_end(outputs={"status": "done"}, run_id="node-1", name="execute_step")
    callback.on_chain_end(outputs={"status": "completed"}, run_id="root", name="LangGraph")

    segment = telemetry.span("fs.workflow.segment")
    step = telemetry.span("fs.workflow.step")
    model = telemetry.span("chat gpt-5.6-sol")
    assert step.parent.span_id == segment.context.span_id
    assert model.parent.span_id == step.context.span_id
    assert dict(segment.attributes) == {"fs.job.id": "job-1", "fs.outcome": "ok"}
    assert dict(step.attributes) == {
        "fs.step.kind": "agent",
        "fs.step.attempt": 1,
        "fs.outcome": "ok",
    }
    assert model.attributes["gen_ai.provider.name"] == "openai"
    assert model.attributes["gen_ai.usage.input_tokens"] == 100
    assert model.attributes["gen_ai.usage.cache_read.input_tokens"] == 40
    assert model.attributes["fs.llm.purpose"] == "workflow"

    assert [p.count for p in telemetry.points("fs.workflow.segment.duration")] == [1]
    [step_point] = telemetry.points("fs.workflow.step.duration")
    assert dict(step_point.attributes) == {"fs.step.kind": "agent", "fs.outcome": "ok"}
    tokens = {
        p.attributes["gen_ai.token.type"]: p.sum
        for p in telemetry.points("gen_ai.client.token.usage")
    }
    assert tokens == {"input": 100, "output": 7}
    [operation] = telemetry.points("gen_ai.client.operation.duration")
    assert operation.count == 1
    assert "gen_ai.request.model" not in operation.attributes


def test_nodes_that_are_not_the_dispatched_executor_are_not_steps(telemetry: _Telemetry) -> None:
    callback = _callback()
    callback.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    _node(callback, "hub-1", "orchestrator", {"run_cursor": {"step_execution_id": "exec-1"}})
    callback.on_chain_end(outputs={}, run_id="hub-1", name="orchestrator")
    callback.on_chain_end(outputs={"status": "completed"}, run_id="root", name="LangGraph")

    assert [span.name for span in telemetry.finished()] == ["fs.workflow.segment"]


def test_the_hub_reading_the_dispatch_record_back_is_not_a_second_step(
    telemetry: _Telemetry,
) -> None:
    # Seen on a live stand: the record stays in state until the hub consumes the
    # executor's result, so the hub and validation runs carry it as well.
    callback = _callback()
    callback.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    for run_id, name in (
        ("exec-node", "execute_step"),
        ("hub-node", "orchestrator"),
        ("validation-node", "orchestrator_validation"),
    ):
        _node(callback, run_id, name, _dispatched_payload("transform"))
        callback.on_chain_end(outputs={"status": "done"}, run_id=run_id, name=name)
    _node(callback, "retry-node", "execute_step", _dispatched_payload("transform", attempt=2))
    callback.on_chain_end(outputs={"status": "done"}, run_id="retry-node", name="execute_step")
    callback.on_chain_end(outputs={"status": "completed"}, run_id="root", name="LangGraph")

    steps = [span for span in telemetry.finished() if span.name == "fs.workflow.step"]
    assert [span.attributes["fs.step.attempt"] for span in steps] == [1, 2]


def test_interrupt_pauses_without_error_or_payload(telemetry: _Telemetry) -> None:
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    callback = _callback()
    callback.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    _node(callback, "node-1", "human_approval_gate", _dispatched_payload("approval"))
    callback.on_chain_error(
        error=GraphInterrupt([Interrupt(value={"secret": "s3cr3t"}, id="i-1")]),
        run_id="node-1",
        name="human_approval_gate",
    )
    callback.on_chain_end(outputs={"status": "awaiting_approval"}, run_id="root", name="LangGraph")

    step = telemetry.span("fs.workflow.step")
    segment = telemetry.span("fs.workflow.segment")
    for span in (step, segment):
        assert span.status.status_code is StatusCode.UNSET
        assert span.attributes["fs.outcome"] == "paused"
        assert span.events == ()
        assert "s3cr3t" not in span.to_json()


def test_failed_step_has_bounded_error_and_no_exception_text(telemetry: _Telemetry) -> None:
    callback = _callback()
    callback.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    _node(callback, "node-1", "execute_step", _dispatched_payload("http_request"))
    callback.on_chain_error(
        error=RuntimeError("token=s3cr3t"), run_id="node-1", name="execute_step"
    )

    step = telemetry.span("fs.workflow.step")
    assert step.status.status_code is StatusCode.ERROR
    assert not step.status.description
    assert step.attributes["error.type"] == "_OTHER"
    assert "s3cr3t" not in step.to_json()
    [point] = telemetry.points("fs.workflow.step.duration")
    assert dict(point.attributes) == {
        "fs.step.kind": "http_request",
        "fs.outcome": "error",
        "error.type": "_OTHER",
    }


def test_extension_provider_and_unsafe_names_are_bounded(telemetry: _Telemetry) -> None:
    callback = _callback()
    callback.on_chat_model_start(
        {},
        [[]],
        run_id="llm-x",
        metadata={"flow_steward_provider": "llmext_remote_abc123", "flow_steward_model": "a b"},
    )
    callback.on_llm_end(response=SimpleNamespace(llm_output={}, generations=[]), run_id="llm-x")
    callback.on_tool_start({"name": "http://evil/?q=1 x"}, "secret input", run_id="tool-x")
    callback.on_tool_end("secret output", run_id="tool-x")

    model = telemetry.span("chat")
    tool = telemetry.span("execute_tool")
    assert model.attributes["gen_ai.provider.name"] == "other"
    assert "gen_ai.request.model" not in model.attributes
    assert "gen_ai.tool.name" not in tool.attributes
    assert "secret" not in tool.to_json()
    assert workflow_telemetry.step_kind("made-up") == "other"


def test_lane_step_attempt_parents_the_calls_made_inside_it(telemetry: _Telemetry) -> None:
    callback = _callback()
    callback.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    _node(callback, "par-1", "parallel_executor", {})

    with workflow_telemetry.lane_step_attempt("transform", 2) as attempt:
        _model_call(callback, "llm-lane", "par-1")
        attempt.failed()

    lane = telemetry.span("fs.workflow.step")
    model = telemetry.span("chat gpt-5.6-sol")
    assert model.parent.span_id == lane.context.span_id
    assert lane.attributes["fs.step.attempt"] == 2
    assert lane.attributes["fs.outcome"] == "error"


def test_model_call_scope_outside_a_workflow_has_its_own_spans(telemetry: _Telemetry) -> None:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from core.application.observability.model_call_telemetry import model_call_scope

    with model_call_scope("replay"):
        FakeListChatModel(responses=["ok"]).invoke("hi")

    model = telemetry.span("chat")
    assert model.attributes["fs.llm.purpose"] == "replay"
    assert [p.count for p in telemetry.points("gen_ai.client.operation.duration")] == [1]


def test_model_call_scope_inside_a_workflow_leaves_spans_to_its_callback(
    telemetry: _Telemetry,
) -> None:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from core.application.observability.model_call_telemetry import model_call_scope

    callback = _callback()
    callback.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    _node(callback, "node-1", "execute_step", _dispatched_payload("agent"))

    with model_call_scope("classification"):
        FakeListChatModel(responses=["ok"]).invoke("hi", config={"callbacks": [callback]})

    model = telemetry.span("chat")
    assert model.attributes["fs.llm.purpose"] == "classification"


def test_disabled_export_records_and_keeps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel_instruments, "current_telemetry", lambda: None)
    run = workflow_telemetry.RunTelemetry(job_id="job-1")

    run.segment_started("root")
    run.model_started("m", "root", provider_type="openai", model="x")
    run.model_finished("m")
    run.segment_finished("root", outcome="ok")

    assert run._open == {}


def test_a_non_owner_process_never_records_and_only_captures_inside_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _Telemetry(owner=OwnerProcess(pid=-1))
    monkeypatch.setattr(otel_instruments, "current_telemetry", lambda: state)
    otel_instruments._reset_for_tests()

    otel_instruments.record(otel_instruments.STEP_DURATION, 1.0, {"fs.outcome": "ok"})
    otel_instruments.begin_child_capture()
    otel_instruments.record(
        otel_instruments.STEP_DURATION, 2.0, {"fs.outcome": "ok", "fs.job.id": "dropped"}
    )
    captured = otel_instruments.drain_child_capture()

    assert captured == [["fs.workflow.step.duration", 2.0, {"fs.outcome": "ok"}]]
    assert state.points("fs.workflow.step.duration") == []
    assert otel_instruments.drain_child_capture() == []


class _RecordingAgent:
    def execute(self, agent_input):
        otel_instruments.record(otel_instruments.TOKEN_USAGE, 11, {"gen_ai.token.type": "input"})
        if agent_input == "fail":
            raise RuntimeError("agent failed")
        return {"status": "pass", "summary": "done"}


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="needs fork")
@pytest.mark.parametrize("agent_input", ["ok", "fail"])
def test_forked_agent_returns_measurements_the_parent_records_once(
    telemetry: _Telemetry, agent_input: str
) -> None:
    from core.application.orchestration.graph.nodes_agent_runtime import _agent_process_entry

    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_agent_process_entry, args=(_RecordingAgent(), agent_input, result_queue)
    )
    process.start()
    payload = result_queue.get(timeout=10)
    process.join(timeout=10)

    # Nothing reached the parent's reader from the child itself.
    assert telemetry.points("gen_ai.client.token.usage") == []
    assert payload["ok"] is (agent_input == "ok")
    assert otel_instruments.record_captured(payload["telemetry"]) == 1
    [point] = telemetry.points("gen_ai.client.token.usage")
    assert point.sum == 11
