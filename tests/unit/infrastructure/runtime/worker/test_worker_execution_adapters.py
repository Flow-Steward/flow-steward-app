"""Worker graph adapter resource-ownership regressions."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.jobs.execution_contracts import TaskExecutionRequest
from core.application.orchestration.child_execution_runtime import (
    ChildExecutionRuntime,
    current_child_execution_runtime,
)
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION, seeded_step_cursor
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.workflows.workflow_definition_snapshot import definition_digest
from core.infrastructure.runtime.worker.worker_execution_adapters import (
    WorkerGraphExecutionAdapter,
)
from core.infrastructure.runtime.worker.worker_resume_adapters import (
    WorkerWorkflowResumeGraphAdapter,
)
from tests._workflow_definitions import edge, node

# The real constructor, kept for the tests that exercise it directly.
_REAL_BUILD_CHECKPOINTER = WorkerGraphExecutionAdapter._build_checkpointer


@pytest.fixture(autouse=True)
def _checkpointer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs here exercise the adapter, not checkpoint storage: give them one."""

    @contextmanager
    def _available(_self):
        yield object()

    monkeypatch.setattr(WorkerGraphExecutionAdapter, "_build_checkpointer", _available)


def test_graph_run_keeps_checkpointer_open_through_pending_state_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    checkpointer = object()

    class _Graph:
        def get_state(self, _config):
            events.append("get_state")
            return SimpleNamespace(tasks=[])

    @contextmanager
    def checkpointer_context():
        events.append("enter")
        try:
            yield checkpointer
        finally:
            events.append("exit")

    adapter = WorkerGraphExecutionAdapter(config={}, bl=SimpleNamespace())
    monkeypatch.setattr(adapter, "_build_checkpointer", checkpointer_context)
    monkeypatch.setattr(
        adapter,
        "_build_graph",
        lambda _request, *, checkpointer: (
            events.append("build") or _Graph()
            if checkpointer is not None
            else (_ for _ in ()).throw(AssertionError("missing checkpointer"))
        ),
    )
    monkeypatch.setattr(adapter, "_build_initial_state", lambda _request: {})
    monkeypatch.setattr(adapter, "_build_callback", lambda *_args: None)

    from core.application.orchestration.agents import common as graph_common

    monkeypatch.setattr(
        graph_common,
        "run_graph_with_cancel_check",
        lambda *_args, **_kwargs: events.append("invoke") or {"status": "in_progress"},
    )
    request = SimpleNamespace(
        thread_id="thread-1",
        job_id="job-1",
    )

    outcome = adapter.run(request, job_row=None, project_id=None)

    assert outcome.pending_interrupt is False
    assert events == ["enter", "build", "invoke", "get_state", "exit"]


@pytest.mark.parametrize("graph_raises", [False, True])
def test_graph_run_binds_and_resets_child_execution_runtime(
    monkeypatch: pytest.MonkeyPatch,
    graph_raises: bool,
) -> None:
    observed_runtimes: list[ChildExecutionRuntime | None] = []
    child_runtime = ChildExecutionRuntime(
        worker_id="worker-1",
        execute_inline=lambda _message: {"action": "ack"},
    )
    adapter = WorkerGraphExecutionAdapter(
        config={},
        bl=SimpleNamespace(),
        child_execution_runtime=child_runtime,
    )
    monkeypatch.setattr(adapter, "_build_graph", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(adapter, "_build_initial_state", lambda _request: {})
    monkeypatch.setattr(adapter, "_build_callback", lambda *_args: None)
    monkeypatch.setattr(adapter, "_pending_interrupt_state", lambda *_args: None)

    from core.application.orchestration.agents import common as graph_common

    def _run_graph(*_args, **_kwargs):
        observed_runtimes.append(current_child_execution_runtime())
        if graph_raises:
            raise RuntimeError("graph failed")
        return {"status": "completed"}

    monkeypatch.setattr(graph_common, "run_graph_with_cancel_check", _run_graph)
    request = SimpleNamespace(thread_id="thread-1", job_id="job-1")

    outcome = adapter.run(request, job_row=None, project_id=None)

    assert observed_runtimes == [child_runtime]
    assert current_child_execution_runtime() is None
    assert outcome.kind == ("error" if graph_raises else "result")


def test_workflow_resume_binds_and_resets_child_execution_runtime() -> None:
    observed_runtimes: list[ChildExecutionRuntime | None] = []
    child_runtime = ChildExecutionRuntime(
        worker_id="worker-1",
        execute_inline=lambda _message: {"action": "ack"},
        artifact_plane_provider=lambda: object(),
    )
    adapter = WorkerWorkflowResumeGraphAdapter(
        bl=SimpleNamespace(),
        child_execution_runtime=child_runtime,
    )

    def _resume_runner(**_kwargs):
        observed_runtimes.append(current_child_execution_runtime())
        return SimpleNamespace(
            started=True,
            status="completed",
            retryable=False,
            reason="",
            result={"status": "completed"},
            error=None,
            terminal=True,
        )

    adapter._runner = _resume_runner
    request = SimpleNamespace(
        thread_id="thread-1",
        resume_value={"approved": True},
        approved=True,
        action="approve",
        comment="ship it",
        interrupt_payload={"phase": "planning"},
        actor_username="operator",
        resume_revision=1,
        approval_id="approval-1",
    )

    outcome = adapter.resume(request, cancelled=lambda: False)

    assert observed_runtimes == [child_runtime]
    assert current_child_execution_runtime() is None
    assert outcome.status == "completed"


def test_missing_checkpointer_configuration_refuses_instead_of_running_without() -> None:
    from core.application.orchestration.checkpoint_requirement import (
        CheckpointUnavailableError,
    )

    adapter = WorkerGraphExecutionAdapter(config={}, bl=SimpleNamespace())

    with pytest.raises(CheckpointUnavailableError) as refused, _REAL_BUILD_CHECKPOINTER(adapter):
        pass
    assert refused.value.error_code == "checkpoint_unavailable"


def _postgres_saver_module(monkeypatch: pytest.MonkeyPatch, saver_class: type) -> None:
    langgraph = ModuleType("langgraph")
    checkpoint = ModuleType("langgraph.checkpoint")
    postgres = ModuleType("langgraph.checkpoint.postgres")
    postgres.PostgresSaver = saver_class
    monkeypatch.setitem(sys.modules, "langgraph", langgraph)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint", checkpoint)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", postgres)


@pytest.mark.parametrize("failure", ["no_uri", "saver_raises"])
def test_a_run_without_a_working_checkpointer_fails_before_any_step(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from core.application.orchestration.agents import common as graph_common

    monkeypatch.setattr(
        WorkerGraphExecutionAdapter, "_build_checkpointer", _REAL_BUILD_CHECKPOINTER
    )

    class _BrokenSaver:
        @classmethod
        def from_conn_string(cls, uri: str):
            raise ConnectionError(f"could not connect to {uri} password=secret")

    _postgres_saver_module(monkeypatch, _BrokenSaver)
    leaky_uri = "postgresql://u:secret@db/x"  # pragma: allowlist secret - redaction sentinel
    config = {} if failure == "no_uri" else {"langgraph_postgres_uri": leaky_uri}
    adapter = WorkerGraphExecutionAdapter(config=config, bl=SimpleNamespace())
    executed: list[str] = []
    monkeypatch.setattr(adapter, "_build_graph", lambda *_a, **_k: executed.append("graph"))
    monkeypatch.setattr(
        graph_common, "run_graph_with_cancel_check", lambda *_a, **_k: executed.append("run")
    )

    outcome = adapter.run(
        SimpleNamespace(thread_id="thread-1", job_id="job-1", test_mode=False),
        job_row=None,
        project_id=None,
    )

    assert executed == []
    assert outcome.kind == "error"
    assert outcome.error.error_code == "checkpoint_unavailable"
    assert "secret" not in str(outcome.error)
    assert "postgresql" not in str(outcome.error)


def test_step_test_run_worker_state_preserves_the_selected_single_step() -> None:
    cursor = seeded_step_cursor("compile", seed="exec-1").dump()
    context = {"workflow_id": "wf-demo", "step_id": "compile", "execution_id": "exec-1"}
    request = TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Compile only",
        task_source="step_test_run",
        project_name="Demo",
        project_config={"project_id": "project-1"},
        extra_state={
            "workflow_id": "wf-demo",
            "run_cursor": cursor,
            "step_test_run_context": context,
            "workflow_test_run_execution_id": "exec-1",
            "test_mode": True,
            "debug_test_run": True,
        },
        workflow_id="wf-demo",
        test_mode=True,
    )

    state = WorkerGraphExecutionAdapter._build_initial_state(request)

    assert state["task_source"] == "step_test_run"
    assert state["run_cursor"] == cursor
    assert state["step_test_run_context"] == context
    assert state["test_mode"] is True
    for removed in ("phase_stack", "current_phase", "current_step"):
        assert removed not in state
    assert "child_execution_runtime" not in state


@pytest.mark.parametrize("task_source", ["mcp_workflow", "provider"])
def test_prepared_upload_worker_state_pins_archive_and_dispatches_first_step(
    monkeypatch: pytest.MonkeyPatch, task_source: str, _isolated_definitions
) -> None:
    from core.application.jobs.task_execution_request import _build_request
    from core.application.orchestration import run_definition
    from core.application.orchestration.step_hub import orchestrator_hub_node

    descriptor = {
        "artifact_id": "art-upload",
        "filename": "REPORT.CSV",
        "content_type": "text/csv",
        "size_bytes": 29,
        "sha256": "a" * 64,
    }
    definition = {
        "workflow_id": "wf-upload",
        "steps": [
            {"step_id": "read-upload", "step_kind": "transform"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "settings": {},
        "execution_graph": {
            "schema_version": 1,
            "entry": node("read-upload"),
            "edges": [edge("read-upload", node("done"))],
        },
    }
    ref = {"snapshot_id": "snapshot-upload", "revision": 4, "digest": definition_digest(definition)}
    pin_calls = []

    def pin(job_id, workflow_id):
        pin_calls.append((job_id, workflow_id))
        return ref

    monkeypatch.setattr(run_definition, "_default_pin", pin)
    monkeypatch.setattr(
        run_definition,
        "_default_loader",
        lambda _job_id: {"id": "snapshot-upload", **ref, "definition_jsonb": definition},
    )
    monkeypatch.setattr(
        "core.application.orchestration.agents.common.emit_event", lambda *_a, **_k: None
    )
    request = _build_request(
        {
            "job_id": "job-upload",
            "task_id": "task-upload",
            "task_source": task_source,
            "task_summary": "Read the uploaded CSV",
            "project_config": {"project_id": "project-1"},
            "extra_state": {
                "workflow_id": "wf-upload",
                "files": [descriptor],
                "inputs": {"source_file": descriptor},
                "task_envelope": {
                    "payload_complete": True,
                    "files": [descriptor],
                    "inputs": {"source_file": descriptor},
                },
            },
        },
        {
            "selected_workflow_key": "wf-upload",
            "workflow_definition_snapshot_id": "snapshot-upload",
            "meta": {"workflow_definition_ref": {"revision": 4, "digest": ref["digest"]}},
        },
    )

    state = WorkerGraphExecutionAdapter._build_initial_state(request)
    assert state["runtime_schema_version"] is None
    assert not state.get("workflow_definition_ref")
    command = orchestrator_hub_node(state)
    started = {**state, **command.update}

    assert pin_calls == [("job-upload", "wf-upload")]
    assert command.goto == "execute_step"
    assert started["runtime_schema_version"] == 2
    assert started["workflow_definition_ref"] == ref
    assert started["run_cursor"]["step_id"] == "read-upload"
    assert started["dispatched_step_execution"]["step_execution_id"]
    assert started["files"] == [descriptor]
    assert started["inputs"]["source_file"] == descriptor
    assert started["task_envelope"]["inputs"]["source_file"] == descriptor


def _step_test_request(*, context: object) -> TaskExecutionRequest:
    return TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Compile only",
        task_source="step_test_run",
        project_name="Demo",
        project_config={"project_id": "project-1"},
        extra_state={"step_test_run_context": context},
        workflow_id="wf-demo",
        test_mode=True,
    )


@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        {"workflow_id": "wf-other", "step_id": "compile"},
        {"workflow_id": "wf-demo", "step_id": ""},
    ],
)
def test_step_test_graph_build_fails_closed_without_consistent_trusted_scope(
    context: object,
) -> None:
    adapter = WorkerGraphExecutionAdapter(config={}, bl=None)

    _raises_input_190_1 = _step_test_request(context=context)
    with pytest.raises(RuntimeError, match="step test run context"):
        adapter._build_graph(_raises_input_190_1, checkpointer=False)


def test_step_test_graph_build_forwards_exact_trusted_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.orchestration import graph_runtime

    build_graph = MagicMock(return_value=object())
    monkeypatch.setattr(graph_runtime, "build_registry_graph", build_graph)
    context = {"workflow_id": "wf-demo", "step_id": "compile", "execution_id": "exec-1"}

    WorkerGraphExecutionAdapter(config={}, bl=None)._build_graph(
        _step_test_request(context=context),
        checkpointer=False,
    )

    build_graph.assert_called_once_with(
        checkpointer=False,
        workflow_ids=["wf-demo"],
        workflow_step_scope={"workflow_id": "wf-demo", "step_id": "compile"},
    )


def _step_test_definition(step: dict) -> dict:
    """The selected step has an error edge a step test run must never follow."""
    return {
        "workflow_id": "wf-acme-store",
        "steps": [
            step,
            {"step_id": "other-step", "step_kind": "transform"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "execution_graph": {
            "schema_version": 1,
            "entry": node(step["step_id"]),
            "edges": [
                edge(step["step_id"], node("done")),
                edge(step["step_id"], node("other-step"), "error"),
                edge("other-step", node("done")),
            ],
        },
        "settings": {},
    }


@pytest.fixture
def _isolated_definitions():
    clear_run_definition_cache()
    yield
    clear_run_definition_cache()


def _run_durable_connector_step_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connector_result: dict,
    trusted_isolated_context: bool = True,
    context_step_id: str = "acme-store-locations",
    unprimed_intake: bool = False,
    pin_calls: list[tuple[str, str]] | None = None,
):
    from langgraph.graph import END, START, StateGraph

    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.state import OrchestratorState
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.application.orchestration.validation_node import orchestrator_validation_node

    graph = StateGraph(OrchestratorState)
    graph.add_node("orchestrator", orchestrator_hub_node)
    graph.add_node("execute_step", execute_step_node)
    graph.add_node("orchestrator_validation", orchestrator_validation_node)
    if unprimed_intake:
        from core.application.orchestration.agents.kb_intake_node import kb_intake_node

        graph.add_node("kb_intake", kb_intake_node)
        graph.add_edge(START, "kb_intake")
        graph.add_edge("kb_intake", "orchestrator")
    else:
        graph.add_edge(START, "orchestrator")
    graph.add_edge("execute_step", "orchestrator")
    graph.add_edge("orchestrator_validation", END)
    compiled = graph.compile()

    connector_calls: list[tuple[dict, dict]] = []

    def execute_connector_step(*, step, state):
        connector_calls.append((dict(step), dict(state)))
        return dict(connector_result)

    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "core.application.orchestration.connector_step_runtime.execute_connector_step",
        execute_connector_step,
    )
    step = {
        "step_id": "acme-store-locations",
        "step_kind": "connector",
        "connector_id": "acme_store",
        "operation_id": "list_locations",
        "connection_ref": "conn-acme-store",
        "connector_install_id": "install-acme-store",
        "error_policy": {"on_error": "route_to_step"},
    }
    definition = _step_test_definition(step)
    ref = {
        "snapshot_id": "snapshot-step-test",
        "revision": 1,
        "digest": definition_digest(definition),
    }
    if unprimed_intake:
        from core.application.orchestration import run_definition

        def _pin(job_id, workflow_id):
            if pin_calls is not None:
                pin_calls.append((job_id, workflow_id))
            return ref

        monkeypatch.setattr(run_definition, "_default_pin", _pin)
        monkeypatch.setattr(
            run_definition,
            "_default_loader",
            lambda _job: {**ref, "id": ref["snapshot_id"], "definition_jsonb": definition},
        )
    else:
        prime_run_definition(ref, definition)
    extra_state = {
        "workflow_id": "wf-acme-store",
        "requested_workflow_id": "wf-acme-store",
        "task_envelope": {"inputs": {"project_id": "project-1"}},
        "inputs": {"project_id": "project-1"},
        "materialized_inputs": {"project_id": "project-1"},
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "run_cursor": seeded_step_cursor("acme-store-locations", seed="exec-step-1").dump(),
        "test_mode": True,
        "debug_test_run": True,
        "workflow_test_run_execution_id": "exec-step-1",
    }
    if unprimed_intake:
        extra_state.pop("runtime_schema_version")
        extra_state.pop("workflow_definition_ref")
        extra_state["job_id"] = "job-1"
    if trusted_isolated_context:
        extra_state["step_test_run_context"] = {
            "workflow_id": "wf-acme-store",
            "step_id": context_step_id,
            "execution_id": "exec-step-1",
        }
    request = TaskExecutionRequest(
        message={},
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        task_summary="Test only the saved Acme Store step",
        task_source="step_test_run",
        project_name="Demo",
        project_config={"project_id": "project-1"},
        extra_state=extra_state,
        workflow_id="wf-acme-store",
        test_mode=True,
    )
    adapter = WorkerGraphExecutionAdapter(config={}, bl=None)
    monkeypatch.setattr(adapter, "_build_graph", lambda *_args, **_kwargs: compiled)

    return adapter.run(request, job_row=None, project_id="project-1"), connector_calls


def test_durable_step_test_run_loads_its_snapshot_before_project_context_intake(
    monkeypatch: pytest.MonkeyPatch, _isolated_definitions
) -> None:
    pin_calls: list[tuple[str, str]] = []
    outcome, calls = _run_durable_connector_step_test(
        monkeypatch,
        connector_result={
            "ok": True,
            "result": {"locations": [{"id": "location-1"}]},
            "state_patch": {"workflow_data": {"locations": [{"id": "location-1"}]}},
        },
        unprimed_intake=True,
        pin_calls=pin_calls,
    )

    assert outcome.kind == "result", outcome.error
    assert outcome.result["status"] == "completed"
    assert pin_calls == [("job-1", "wf-acme-store")]
    assert len(calls) == 1
    selected_step, state = calls[0]
    assert selected_step["step_id"] == "acme-store-locations"
    assert state["runtime_schema_version"] == RUNTIME_SCHEMA_VERSION
    assert state["workflow_definition_ref"]["snapshot_id"] == "snapshot-step-test"
    messages = outcome.result["agent_messages"]
    assert [message["step_id"] for message in messages] == ["acme-store-locations"]
    assert messages[0]["status"] == "pass"
    assert (
        state["run_cursor"]["step_execution_id"]
        == seeded_step_cursor("acme-store-locations", seed="exec-step-1").dump()[
            "step_execution_id"
        ]
    )


def test_durable_step_test_run_crosses_worker_graph_and_returns_selected_step_result(
    monkeypatch: pytest.MonkeyPatch, _isolated_definitions
) -> None:
    outcome, connector_calls = _run_durable_connector_step_test(
        monkeypatch,
        connector_result={
            "ok": True,
            "runtime": "extension",
            "detail": "ok",
            "result": {"locations": [{"id": "gid://acme-store/Location/1"}]},
            "state_patch": {"workflow_data": {"locations": [{"id": "location-1"}]}},
        },
    )

    assert outcome.kind == "result"
    assert outcome.result["status"] == "completed"
    assert len(connector_calls) == 1
    assert connector_calls[0][0]["step_id"] == "acme-store-locations"
    assert connector_calls[0][1]["test_mode"] is True
    message = outcome.result["agent_messages"][-1]
    assert message["status"] == "pass"
    assert message["artifacts"]["connector_result"]["locations"][0]["id"].endswith("/1")
    assert message["artifacts"]["_state_patch"] == {
        "workflow_data": {"locations": [{"id": "location-1"}]}
    }


def test_durable_step_test_run_surfaces_connector_failure_without_running_workflow(
    monkeypatch: pytest.MonkeyPatch, _isolated_definitions
) -> None:
    outcome, connector_calls = _run_durable_connector_step_test(
        monkeypatch,
        connector_result={
            "ok": False,
            "runtime": "extension",
            "detail": "access_denied",
            "errors": [{"code": "access_denied", "message": "Acme Store denied the request"}],
        },
    )

    assert outcome.kind == "result"
    assert outcome.result["status"] == "failed"
    assert len(connector_calls) == 1
    assert "access_denied" in outcome.result["final_report"]
    assert outcome.result["unresolved_step_failure"]["step_id"] == "acme-store-locations"


def test_step_test_run_without_trusted_isolated_context_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, _isolated_definitions
) -> None:
    outcome, connector_calls = _run_durable_connector_step_test(
        monkeypatch,
        connector_result={"ok": True, "result": {"unexpected": True}},
        trusted_isolated_context=False,
    )

    assert outcome.kind == "result"
    assert outcome.result["status"] == "failed"
    assert "isolated execution context" in outcome.result["final_report"]
    assert connector_calls == []


def test_step_test_run_whose_context_names_another_step_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, _isolated_definitions
) -> None:
    outcome, connector_calls = _run_durable_connector_step_test(
        monkeypatch,
        connector_result={"ok": True, "result": {"unexpected": True}},
        context_step_id="other-step",
    )

    assert outcome.kind == "result"
    assert outcome.result["status"] == "failed"
    assert "isolated execution context" in outcome.result["final_report"]
    assert connector_calls == []


def test_task_service_composes_child_runtime_from_same_worker_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.runtime.worker import worker_task_execution as task_execution

    dependencies = task_execution.compose_worker_execution_dependencies(
        worker_id=lambda: "worker-1"
    )
    calls: list[tuple[dict, object]] = []

    def _execute_task(message, *, dependencies):
        calls.append((dict(message), dependencies))
        return {"action": "ack"}

    monkeypatch.setattr(task_execution, "execute_task", _execute_task)

    service = task_execution._task_service(
        bl=None,
        config={},
        dependencies=dependencies,
    )
    child_runtime = service._graph._child_execution_runtime

    assert child_runtime.worker_id == "worker-1"
    assert child_runtime.execute_inline({"job_id": "child-1"}) == {"action": "ack"}
    assert calls == [({"job_id": "child-1"}, dependencies)]


@pytest.mark.parametrize("raise_inside", [False, True])
def test_postgres_checkpointer_context_is_closed_on_every_exit(
    monkeypatch: pytest.MonkeyPatch,
    raise_inside: bool,
) -> None:
    events: list[str] = []
    saver = object()

    @contextmanager
    def saver_context():
        events.append("enter")
        try:
            yield saver
        finally:
            events.append("exit")

    class _PostgresSaver:
        @classmethod
        def from_conn_string(cls, uri: str):
            assert uri == "postgresql://checkpoint"
            return saver_context()

    _postgres_saver_module(monkeypatch, _PostgresSaver)

    adapter = WorkerGraphExecutionAdapter(
        config={"langgraph_postgres_uri": "postgresql://checkpoint"},
        bl=SimpleNamespace(),
    )

    with (
        pytest.raises(RuntimeError) if raise_inside else _does_not_raise(),
        _REAL_BUILD_CHECKPOINTER(adapter) as checkpointer,
    ):
        assert checkpointer is saver
        if raise_inside:
            raise RuntimeError("graph failed")

    assert events == ["enter", "exit"]


@contextmanager
def _does_not_raise():
    yield


@pytest.mark.parametrize("test_mode", [False, True])
def test_graph_run_binds_the_step_event_sink_outside_test_mode(
    monkeypatch: pytest.MonkeyPatch, test_mode: bool
) -> None:
    from core.application.workflows.workflow_lifecycle_event_sink import (
        current_workflow_event_sink,
    )
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        WorkerLifecycleEventSink,
    )

    observed: list[object] = []
    adapter = WorkerGraphExecutionAdapter(config={}, bl=SimpleNamespace())
    monkeypatch.setattr(adapter, "_build_graph", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(adapter, "_build_initial_state", lambda _request: {})
    monkeypatch.setattr(adapter, "_build_callback", lambda *_args: None)
    monkeypatch.setattr(adapter, "_pending_interrupt_state", lambda *_args: None)

    from core.application.orchestration.agents import common as graph_common

    def _run_graph(*_args, **_kwargs):
        observed.append(current_workflow_event_sink())
        return {"status": "completed"}

    monkeypatch.setattr(graph_common, "run_graph_with_cancel_check", _run_graph)
    request = SimpleNamespace(thread_id="thread-1", job_id="job-1", test_mode=test_mode)

    adapter.run(request, job_row=None, project_id=None)

    if test_mode:
        assert observed == [None]
    else:
        assert isinstance(observed[0], WorkerLifecycleEventSink)
    assert current_workflow_event_sink() is None


def test_terminal_notify_keeps_reaction_only_moments_off_project_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from core.application.jobs.execution_contracts import TaskNotification

    # Other worker fixtures temporarily replace sys.modules. Resolve the same
    # module objects as notify's imports, rather than stale package attributes.
    notification_dispatch = importlib.import_module("core.application.notifications.dispatch")
    worker_execution_adapters = importlib.import_module(
        "core.infrastructure.runtime.worker.worker_execution_adapters"
    )
    WorkerTerminalEffectsAdapter = worker_execution_adapters.WorkerTerminalEffectsAdapter

    routed: list[str] = []
    reacted: list[str] = []
    monkeypatch.setattr(
        notification_dispatch,
        "dispatch_project_notification_event",
        lambda _bl, **kwargs: routed.append(kwargs["event_type"]) or [],
    )
    monkeypatch.setattr(
        worker_execution_adapters,
        "_run_lifecycle_reactions",
        lambda _store, **event: reacted.append(event["event_type"]),
    )
    adapter = WorkerTerminalEffectsAdapter(bl=MagicMock(), worker_id=lambda: "w")
    for event_type, dispatch_routes in (
        ("job.awaiting_timer", False),
        ("job.awaiting_approval", True),
    ):
        adapter.notify(
            TaskNotification(
                project_id="p-1",
                event_type=event_type,
                severity="info",
                workflow_id="wf",
                job_id="job-1",
                payload={"wait_id": "w"},
                dispatch_routes=dispatch_routes,
            ),
            repository=MagicMock(),
        )

    assert routed == ["job.awaiting_approval"]
    assert reacted == ["job.awaiting_timer", "job.awaiting_approval"]


@pytest.mark.parametrize("pinned", [True, False])
def test_queued_pinned_job_does_not_revalidate_changed_live_workflow(monkeypatch, pinned):
    from core.application.orchestration import graph_runtime
    from core.application.orchestration.agents import common

    def live_graph(**kwargs):
        if kwargs.get("validate_contracts", True):
            raise RuntimeError("live workflow was deleted after enqueue")
        return SimpleNamespace()

    monkeypatch.setattr(graph_runtime, "build_registry_graph", live_graph)
    adapter = WorkerGraphExecutionAdapter(config={}, bl=None)
    monkeypatch.setattr(adapter, "_build_initial_state", lambda _request: {})
    monkeypatch.setattr(
        common, "run_graph_with_cancel_check", lambda *_args, **_kwargs: {"status": "completed"}
    )
    request = SimpleNamespace(
        thread_id="thread-1",
        job_id="job-1",
        workflow_id="old-workflow",
        task_source="manual",
        test_mode=False,
    )
    job_row = {"workflow_definition_snapshot_id": "snapshot-1"} if pinned else {}

    outcome = adapter.run(request, job_row=job_row, project_id=None)

    assert outcome.kind == ("result" if pinned else "error"), outcome.error


@pytest.mark.parametrize("unavailable", [False, True])
def test_parent_fence_worker_outcome_preserves_cancellation_or_infrastructure_error(
    monkeypatch, unavailable
):
    from core.application.jobs.invoke_child_link_repository import (
        InvokeCancellationFenceUnavailable,
    )
    from core.infrastructure.runtime.worker import worker_execution_adapters as adapters

    actions = []
    child = {
        "id": "child",
        "project_id": "p",
        "status": "in_progress",
        "meta": {"invoke_parent_job_id": "parent"},
    }
    parent = {
        "id": "parent",
        "project_id": "p",
        "status": "cancelled",
        "meta": {
            "invoke_children": {
                "child": {"child_job_id": "child", "cancel_requested_at": "committed"}
            }
        },
    }

    def get_job(job_id):
        if job_id == "parent" and unavailable:
            raise OSError("store offline")
        return child if job_id == "child" else parent

    class Graph:
        def stream(self, *_args, **_kwargs):
            actions.append("action")
            yield {"status": "completed"}

    bl = SimpleNamespace(get_job=get_job)
    adapter = WorkerGraphExecutionAdapter(config={}, bl=bl)
    monkeypatch.setattr(adapter, "_build_graph", lambda *_args, **_kwargs: Graph())
    monkeypatch.setattr(adapter, "_build_initial_state", lambda _request: {})
    monkeypatch.setattr(adapter, "_build_callback", lambda *_args: None)
    request = SimpleNamespace(thread_id="t", job_id="child", test_mode=True)
    outcome = adapter.run(request, job_row=child, project_id="p")
    assert actions == []
    if unavailable:
        assert outcome.kind == "error"
        assert isinstance(outcome.error, InvokeCancellationFenceUnavailable)
        result = {"error_code": outcome.error.error_code, "error": str(outcome.error)}
        expected_status, expected_code, expected_category = (
            "failed",
            "invoke_cancellation_fence_unavailable",
            "transient",
        )
    else:
        assert outcome.kind == "cancelled"
        result = dict(outcome.result)
        expected_status, expected_code, expected_category = (
            "cancelled",
            "parent_cancelled",
            "cancellation",
        )
        assert "parent" in result["error"]
    persisted = []
    terminal_service = SimpleNamespace(
        complete_failure=lambda **kwargs: (
            persisted.append(kwargs) or SimpleNamespace(status=kwargs["status"])
        )
    )
    monkeypatch.setattr(adapters, "_terminal_persistence_service", lambda _bl: terminal_service)
    terminal = adapters.WorkerTerminalEffectsAdapter(bl=bl, worker_id=lambda: "owner")
    assert (
        terminal.persist(
            request,
            job_row=child,
            project_id="p",
            final_status=expected_status,
            result=result,
            graph_outcome=outcome,
        )
        == expected_status
    )
    assert persisted[0]["error_code"] == expected_code
    assert persisted[0]["category"] == expected_category
    assert persisted[0]["retryable"] is unavailable
    assert persisted[0]["expected_worker_id"] == "owner"
