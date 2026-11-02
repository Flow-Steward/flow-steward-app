from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from core.application.jobs.task_dispatch_common import prepare_enqueue_inputs
from core.application.observability import job_trace_context
from core.application.observability.job_trace_context import (
    TRACE_CONTEXT_KEY,
    TRACE_LINK_KEY,
    job_trace_carriers,
    retry_link,
)
from core.infrastructure.observability import otel_instruments
from tests.unit.test_workflow_telemetry import _dispatched_payload, _node, _Telemetry
from tests.unit.test_workflow_telemetry import telemetry as telemetry

_TRACE = "1" * 32
_SPAN = "2" * 16


def _traceparent(flags: str = "01", trace_id: str = _TRACE, span_id: str = _SPAN) -> str:
    return f"00-{trace_id}-{span_id}-{flags}"


def _enqueue_meta(meta: dict | None = None) -> dict:
    effective_meta, _, _ = prepare_enqueue_inputs(
        project_config=None,
        test_mode=None,
        extra_state=None,
        meta=meta,
        build_project_config_snapshot=lambda config: {},
    )
    return effective_meta


def _callback(meta=None, store=None):
    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    return PlatformLoggerCallback(
        store or MagicMock(), project_id="project-1", job_id="job-1", job_meta=meta
    )


def _segment(callback, run_id: str = "root") -> None:
    callback.on_chain_start(inputs={}, run_id=run_id, name="LangGraph")
    callback.on_chain_end(outputs={"status": "completed"}, run_id=run_id, name="LangGraph")


def test_a_job_created_in_a_request_runs_under_that_request(telemetry: _Telemetry) -> None:
    tracer = telemetry.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("POST /api/run") as request:
        meta = _enqueue_meta({"selected_workflow_id": "wf"})

    _segment(_callback(json.loads(json.dumps(meta))))

    segment = telemetry.span("fs.workflow.segment")
    assert segment.context.trace_id == request.get_span_context().trace_id
    assert segment.parent.span_id == request.get_span_context().span_id
    assert segment.parent.is_remote
    assert set(meta[TRACE_CONTEXT_KEY]) == {"traceparent"}


def test_caller_supplied_trace_keys_are_replaced(telemetry: _Telemetry) -> None:
    meta = _enqueue_meta(
        {
            TRACE_CONTEXT_KEY: {"traceparent": _traceparent()},
            TRACE_LINK_KEY: {"traceparent": _traceparent(), "baggage": "user=secret"},
        }
    )

    # No active span: the forged parent is dropped, a valid link is kept bounded.
    assert TRACE_CONTEXT_KEY not in meta
    assert meta[TRACE_LINK_KEY] == {"traceparent": _traceparent()}


@pytest.mark.parametrize(
    "carrier",
    [
        None,
        "00-x",
        {"traceparent": "01-" + _TRACE + "-" + _SPAN + "-01"},
        {"traceparent": _traceparent(trace_id="0" * 32)},
        {"traceparent": _traceparent(span_id="0" * 16)},
        {"traceparent": _traceparent(trace_id="A" * 32)},
        {"traceparent": _traceparent() + "-extra"},
    ],
)
def test_malformed_carriers_are_rejected(carrier) -> None:
    assert otel_instruments.bounded_carrier(carrier) is None


def test_carrier_keeps_a_bounded_tracestate_only() -> None:
    assert otel_instruments.bounded_carrier(
        {"traceparent": _traceparent(), "tracestate": "vendor=1", "baggage": "a=b"}
    ) == {"traceparent": _traceparent(), "tracestate": "vendor=1"}
    assert otel_instruments.bounded_carrier(
        {"traceparent": _traceparent(), "tracestate": "x" * 513}
    ) == {"traceparent": _traceparent()}


def test_a_child_workflow_job_is_parented_to_the_invoking_step(telemetry: _Telemetry) -> None:
    parent = _callback()
    parent.on_chain_start(inputs={}, run_id="root", name="LangGraph")
    _node(parent, "node-1", "execute_step", _dispatched_payload("invoke_workflow"))
    child_meta = _enqueue_meta({"parent_job_id": "job-1"})
    parent.on_chain_end(outputs={"status": "done"}, run_id="node-1", name="execute_step")
    parent.on_chain_end(outputs={"status": "completed"}, run_id="root", name="LangGraph")

    from core.infrastructure.db.platform_logger import PlatformLoggerCallback

    child = PlatformLoggerCallback(
        MagicMock(), project_id="project-1", job_id="job-2", job_meta=child_meta
    )
    _segment(child, "child-root")

    step = telemetry.span("fs.workflow.step")
    segments = [s for s in telemetry.finished() if s.name == "fs.workflow.segment"]
    child_segment = next(s for s in segments if s.attributes["fs.job.id"] == "job-2")
    assert child_segment.parent.span_id == step.context.span_id
    assert child_segment.context.trace_id == step.context.trace_id


def test_a_job_without_context_stores_its_first_segment_for_resume(
    telemetry: _Telemetry,
) -> None:
    store = MagicMock()
    _segment(_callback(meta={}, store=store))

    (job_id, patch), _ = store.merge_job_meta.call_args
    first = telemetry.span("fs.workflow.segment")
    assert job_id == "job-1"
    assert store.merge_job_meta.call_count == 1

    resume_store = MagicMock()
    _segment(_callback(meta=json.dumps(patch), store=resume_store), "resume-root")

    resumed = [s for s in telemetry.finished() if s.name == "fs.workflow.segment"][-1]
    assert resumed.context.trace_id == first.context.trace_id
    assert resumed.parent.span_id == first.context.span_id
    resume_store.merge_job_meta.assert_not_called()


def test_an_unsampled_job_stays_unsampled_on_resume(telemetry: _Telemetry) -> None:
    _segment(_callback(meta={TRACE_CONTEXT_KEY: {"traceparent": _traceparent("00")}}))

    assert telemetry.finished() == []


def test_a_retry_is_a_new_trace_linked_to_the_previous_job(telemetry: _Telemetry) -> None:
    previous = {TRACE_CONTEXT_KEY: {"traceparent": _traceparent("00"), "tracestate": "v=1"}}
    link = retry_link(previous)
    assert link == {"traceparent": _traceparent("00")}

    retry_meta = _enqueue_meta({TRACE_LINK_KEY: link})
    _segment(_callback(meta=retry_meta))

    segment = telemetry.span("fs.workflow.segment")
    assert format(segment.context.trace_id, "032x") != _TRACE
    assert segment.parent is None
    assert segment.context.trace_flags.sampled
    (linked,) = segment.links
    assert format(linked.context.trace_id, "032x") == _TRACE


def test_restart_meta_carries_the_previous_trace_as_a_link() -> None:
    from core.application.workflows.serve_workflow_helpers import build_rerun_workflow_selection

    _, rerun_meta = build_rerun_workflow_selection(
        {
            "id": 42,
            "meta": {
                "selected_workflow_id": "wf_restartable",
                TRACE_CONTEXT_KEY: {"traceparent": _traceparent()},
            },
        },
        replay_snapshot={
            "workflow_id": "wf_restartable",
            "inputs": {},
            "files": [],
            "test_mode": False,
            "input_contract": "direct_inputs",
            "task_envelope": {"inputs": {}, "files": []},
            "_workflow_secret_ref_fingerprints": [],
        },
        to_json_obj=lambda value: value if isinstance(value, dict) else {},
        parse_test_mode=lambda value, default=False: bool(value or default),
    )

    assert rerun_meta[TRACE_LINK_KEY] == {"traceparent": _traceparent()}
    assert TRACE_CONTEXT_KEY not in rerun_meta


def test_disabled_traces_stamp_and_store_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _Telemetry(traces=False)
    monkeypatch.setattr(otel_instruments, "current_telemetry", lambda: state)
    tracer = state.tracer_provider.get_tracer("test")
    store = MagicMock()

    with tracer.start_as_current_span("request"):
        meta = _enqueue_meta({})
    _segment(_callback(meta={}, store=store))

    assert TRACE_CONTEXT_KEY not in meta
    store.merge_job_meta.assert_not_called()
    otel_instruments._reset_for_tests()


def test_stored_meta_is_read_as_a_mapping_or_json_text() -> None:
    meta = {
        TRACE_CONTEXT_KEY: {"traceparent": _traceparent()},
        TRACE_LINK_KEY: {"traceparent": _traceparent("00")},
    }

    assert job_trace_carriers(meta) == job_trace_carriers(json.dumps(meta))
    assert job_trace_carriers("{not json") == (None, ())
    assert job_trace_carriers(None) == (None, ())


def test_worker_and_resume_callbacks_receive_the_job_meta(telemetry: _Telemetry) -> None:
    from core.infrastructure.runtime import graph_resume_runtime
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        WorkerGraphExecutionAdapter,
    )

    meta = {TRACE_CONTEXT_KEY: {"traceparent": _traceparent()}}
    adapter = WorkerGraphExecutionAdapter.__new__(WorkerGraphExecutionAdapter)
    adapter._bl = MagicMock()
    worker_callback = adapter._build_callback("project-1", "job-1", job_meta=meta)

    config: dict = {}
    store = SimpleNamespace(
        get_job_context_by_thread_id=lambda conn, thread_id: {
            "id": "job-1",
            "project_id": "project-1",
            "meta": json.dumps(meta),
        }
    )
    graph_resume_runtime._attach_resume_callback(
        thread_id="thread-1",
        config=config,
        get_logger_only=lambda: MagicMock(),
        job_thread_store=store,
        to_json_obj=lambda value: json.loads(value) if isinstance(value, str) else value,
        logger=MagicMock(),
    )

    for callback in (worker_callback, config["callbacks"][0]):
        _segment(callback)
    for segment in telemetry.finished():
        assert format(segment.parent.span_id, "016x") == _SPAN


def test_stamping_uses_the_current_span_outside_a_workflow(telemetry: _Telemetry) -> None:
    remote = SpanContext(int(_TRACE, 16), int(_SPAN, 16), is_remote=True, trace_flags=TraceFlags(1))
    token = context_api.attach(trace.set_span_in_context(NonRecordingSpan(remote)))
    try:
        meta: dict = {}
        job_trace_context.stamp_job_meta(meta)
    finally:
        context_api.detach(token)

    assert meta[TRACE_CONTEXT_KEY] == {"traceparent": _traceparent()}
