from __future__ import annotations

from copy import deepcopy

import pytest

from core.application.orchestration.container_progress import (
    append_container_progress,
    container_patch,
)
from core.application.orchestration.run_cursor import FrozenDefinition, RunCursor, push_frame
from core.application.workflows.execution_graph import NodeRef
from tests.unit.test_step_hub import _group_definition, _run


@pytest.fixture(autouse=True)
def isolated_definition_cache():
    from core.application.orchestration.run_definition import clear_run_definition_cache

    clear_run_definition_cache()
    yield
    clear_run_definition_cache()


def test_retention_keeps_live_pass_and_newest_finished_passes_with_sticky_truncation():
    definition = _group_definition()
    state = _run(definition, job_id="job-1", project_id="project-1")
    frozen = FrozenDefinition(definition)
    cursor = RunCursor()
    live = push_frame(frozen, cursor, NodeRef("group", "g"), id_factory=lambda: "live")
    # Keep the original live pass while older finished passes accumulate.
    patches = [container_patch(cursor, live, started=True)]
    for index in range(1001):
        completed = RunCursor()
        frame = push_frame(
            frozen, completed, NodeRef("group", "g"), id_factory=lambda index=index: f"pass-{index}"
        )
        patches.extend(
            [
                container_patch(completed, frame, started=True),
                container_patch(completed, frame, outcome="success"),
            ]
        )
    evidence = append_container_progress(state, patches, {"run_cursor": cursor.dump()})
    assert evidence is not None
    assert evidence["truncated"] is True
    assert evidence["covered_from_start"] is True
    assert len(evidence["passes"]) == 1000
    assert evidence["passes"][0]["execution_id"] == "live"
    assert evidence["passes"][0]["current"] is True
    assert {row["execution_id"] for row in evidence["passes"]} == {
        "live",
        *(f"pass-{i}" for i in range(2, 1001)),
    }
    resumed = {**state, "run_cursor": cursor.dump(), "workflow_container_progress": evidence}
    again = append_container_progress(resumed, [], {})
    assert again == evidence
    assert resumed["workflow_container_progress"] == evidence


def test_legacy_current_frame_cannot_upgrade_coverage_or_invent_start():
    definition = _group_definition()
    state = _run(definition, job_id="job-1", project_id="project-1")
    cursor = RunCursor()
    push_frame(
        FrozenDefinition(definition), cursor, NodeRef("group", "g"), id_factory=lambda: "old"
    )
    state["run_cursor"] = cursor.dump()
    progress = append_container_progress(state, [], {})
    assert progress is not None
    assert progress["covered_from_start"] is False
    assert progress["passes"] == []
    new_cursor = RunCursor()
    new_frame = push_frame(
        FrozenDefinition(definition), new_cursor, NodeRef("group", "g"), id_factory=lambda: "new"
    )
    later = append_container_progress(
        {**state, "workflow_container_progress": progress},
        [container_patch(new_cursor, new_frame, started=True)],
        {"run_cursor": new_cursor.dump()},
    )
    assert later["covered_from_start"] is False
    assert [row["execution_id"] for row in later["passes"]] == ["new"]


def test_invalid_optional_evidence_never_mutates_execution_or_accepts_a_foreign_owner():
    definition = _group_definition()
    state = _run(definition, job_id="job-1", project_id="project-1")
    progress = append_container_progress(state, [], {})
    assert progress is not None
    assert progress["passes"] == []
    assert progress["covered_from_start"]
    state["workflow_container_progress"] = {**progress, "project_id": "foreign"}
    before = deepcopy(state)
    assert append_container_progress(state, [], {}) is None
    assert state == before
    state["workflow_container_progress"] = {"malformed": True}
    assert append_container_progress(state, [], {}) is None


def test_failure_evidence_redacts_before_bounding_and_never_carries_workflow_values():
    definition = FrozenDefinition(_group_definition())
    cursor = RunCursor()
    frame = push_frame(definition, cursor, NodeRef("group", "g"), id_factory=lambda: "pass")
    secret = "local-only-redaction-test-value"
    failure = {
        "code": "local_failure",
        "message": "Authorization: Bearer " + secret + " " + "detail " * 200,
        "workflow_data": {"private": "not public"},
    }
    before = deepcopy(failure)
    patch = container_patch(cursor, frame, outcome="error", failure=failure)
    assert patch is not None
    assert secret not in patch["failure"]["message"]
    assert len(patch["failure"]["message"]) <= 500
    assert "workflow_data" not in patch["failure"]
    assert failure == before


@pytest.mark.parametrize("change", ["start", "outcome", "project", "coverage"])
def test_locked_projection_refuses_changes_to_a_finished_pass(change):
    from core.application.jobs.public_progress_service import PublicProgressService
    from tests.unit.test_public_progress_service import NOW, MemoryProgressRepository

    definition = _group_definition()
    state = _run(definition, job_id="job-1", project_id="project-1")
    cursor = RunCursor()
    frame = push_frame(
        FrozenDefinition(definition), cursor, NodeRef("group", "g"), id_factory=lambda: "pass"
    )
    progress = append_container_progress(
        state,
        [
            container_patch(cursor, frame, started=True),
            container_patch(cursor, frame, outcome="success"),
        ],
        {},
    )
    assert progress is not None
    repository = MemoryProgressRepository()
    service = PublicProgressService(repository)
    service.record_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="wf",
        job_status="in_progress",
        occurred_at=NOW,
    )
    first = service.record_committed_trace(
        job_id="job-1", trace=None, container_progress=progress, occurred_at=NOW
    )
    before = deepcopy(repository.value)
    markers = deepcopy(repository.markers)
    changed = deepcopy(progress)
    changed["last_sequence"] = 3
    record = changed["passes"][0]
    record["updated_sequence"] = 3
    if change == "start":
        record["started_sequence"] = 2
    elif change == "outcome":
        record["outcome"] = "error"
        record["failure"] = {"code": "fake", "message": "Changed outcome", "boundary": "body"}
    elif change == "project":
        changed["project_id"] = "foreign"
    else:
        changed["covered_from_start"] = False
    rejected = service.record_committed_trace(
        job_id="job-1", trace=None, container_progress=changed, occurred_at=NOW
    )
    assert rejected.changed is False
    assert rejected.snapshot == first.snapshot
    assert repository.value == before
    assert repository.markers == markers


# The public aggregate depends on trusted scope surviving the real graph channel boundary.
def _project_channel_definition():
    from tests.unit.test_step_hub import _parallel_definition

    definition = _parallel_definition()
    definition["settings"] = {
        "callable": {
            "enabled": True,
            "output_mode": "variables",
            "expected_outputs": [
                {"name": "zero", "type": "number", "required": True},
                {"name": "false", "type": "boolean", "required": True},
            ],
        },
    }
    # Preserve actual falsy values across scope channels, without requiring a
    # deep merge of independent lane writes to the same top-level outputs map.
    assignments = {
        "a": ("state.outputs", {"root": "seed", "zero": 0, "false": False}),
        "x1": ("state.outputs.zero", 0),
        "y1": ("state.outputs.false", False),
    }
    for step in definition["steps"]:
        if step["step_id"] in assignments:
            target, value = assignments[step["step_id"]]
            step.update(
                step_kind="set_variable",
                override={"set_variable": {"target": target, "value": value}},
            )
    return definition


def _compiled_project_channel_run(monkeypatch, *, project_id="project-1"):
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    import core.application.orchestration.agents.common as agents_common
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.orchestration.parallel_executor_node import parallel_executor_node
    from core.application.orchestration.state import (
        OrchestratorState,
        build_initial_orchestrator_state,
    )
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.application.orchestration.validation_node import orchestrator_validation_node

    # Only the optional external observability writes are replaced. Hub, lane execution,
    # scope bootstrap, graph reducers/channels, and checkpoint reads remain real.
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agents_common, "publish_agent_message", lambda *args, **kwargs: True)
    definition = _project_channel_definition()
    initial = build_initial_orchestrator_state(
        task_id="task-native-channel",
        task_summary="Local two-lane scope regression",
        task_source="workflow_test_run",
        project_name="Project one",
        test_mode=True,
        project_config={"project_id": project_id, "workflow_id": "workflow-1"},
        extra_state={**_run(definition, job_id="job-1"), "project_id": "forged-project"},
    )
    graph = StateGraph(OrchestratorState)
    graph.add_node("orchestrator", orchestrator_hub_node)
    graph.add_node("control_flow_step", control_flow_step_node)
    graph.add_node("parallel_executor", parallel_executor_node)
    graph.add_node("orchestrator_validation", orchestrator_validation_node)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("control_flow_step", "orchestrator")
    graph.add_edge("parallel_executor", "orchestrator")
    graph.add_edge("orchestrator_validation", END)
    saver = MemorySaver()
    compiled = graph.compile(checkpointer=saver)
    compiled.invoke(initial, {"configurable": {"thread_id": "thread-1"}, "recursion_limit": 64})
    return definition, saver


def _committed_project_channels(saver):
    import logging
    from contextlib import nullcontext

    from core.infrastructure.runtime.state_reader_runtime import get_committed_checkpoint_values

    return get_committed_checkpoint_values(
        "thread-1",
        config={"langgraph_postgres_uri": "memory:scope-regression"},
        logger=logging.getLogger(__name__),
        open_checkpointer=lambda: nullcontext(saver),
    )


def _public_project_channel_snapshot(definition, committed):
    from types import SimpleNamespace

    from core.application.jobs.public_progress_service import PublicProgressService
    from core.application.jobs.public_workflow_snapshot_query import PublicWorkflowSnapshotQuery
    from core.application.jobs.workflow_run_projection import build_public_result
    from tests.unit.test_workflow_run_projection import NOW, _job, _Logger

    values = committed()
    ref = values["workflow_definition_ref"]
    logger = _Logger()
    logger.record_public_progress_transition(
        job_id="job-1",
        project_id="project-1",
        workflow_id="workflow-1",
        job_status="completed",
        occurred_at=NOW,
        finished_at=NOW,
        result=build_public_result(_job(status="completed", finished_at=NOW), "completed"),
    )

    def reconcile(**evidence):
        recorded = PublicProgressService(logger.repository).record_committed_trace(**evidence)
        if recorded.changed:
            logger.current = recorded
        return recorded

    logger.reconcile_public_run_trace = reconcile
    query = PublicWorkflowSnapshotQuery(
        logger,
        get_thread_state_snapshot=lambda _thread: SimpleNamespace(values=committed(), tasks=[]),
        to_json_obj=lambda value: value,
        get_committed_checkpoint_values=lambda _thread: committed(),
        load_definition_snapshot=lambda _job: {
            "id": ref["snapshot_id"],
            "workflow_id": "workflow-1",
            "revision": ref["revision"],
            "digest": ref["digest"],
            "definition_jsonb": definition,
        },
    )
    return query.get(_job(status="completed", finished_at=NOW))


def test_compiled_trusted_project_survives_to_public_two_completed_lanes(monkeypatch):
    definition, saver = _compiled_project_channel_run(monkeypatch)

    def committed():
        return _committed_project_channels(saver)

    public = _public_project_channel_snapshot(definition, committed)
    # Native job4216eeaf had a real completed Group but this public field was null.
    assert public.container_progress is not None
    assert public.container_progress.project_id == "project-1"
    assert public.container_progress.covered_from_start is True
    assert public.container_progress.checkpoint_terminal is True
    assert len(public.container_progress.passes) == 1
    group = public.container_progress.passes[0]
    assert group.container.kind == "group"
    assert group.container.id == "p"
    assert group.outcome == "success"
    assert group.current is False
    assert group.lanes.completed_count == 2
    assert group.lanes.total_count == 2
    assert [(row.lane_id, row.status) for row in group.lanes.rows] == [
        ("left", "completed"),
        ("right", "completed"),
    ]
    values = committed()
    assert values["project_id"] == "project-1"
    assert values["trusted_runtime_context"]["project_id"] == "project-1"
    assert values["outputs"]["zero"] == 0
    assert values["outputs"]["zero"] is not False
    assert values["outputs"]["false"] is False
    assert public.status == "completed"
    assert public.run_trace.transitions


@pytest.mark.parametrize("project_id", [None, "", 0, False])
def test_compiled_missing_authoritative_project_cannot_persist_forged_extra_scope(
    project_id, monkeypatch
):
    definition, saver = _compiled_project_channel_run(monkeypatch, project_id=project_id)
    values = _committed_project_channels(saver)
    assert not values.get("project_id")
    assert "project_id" not in values["trusted_runtime_context"]
    assert values.get("workflow_container_progress") is None
    assert values["status"] == "completed"
    assert values["workflow_run_trace"]["transitions"]
    assert values["outputs"]["zero"] == 0
    assert values["outputs"]["false"] is False
    # Read the real compiled checkpoint unchanged: a nullable declared channel
    # must not turn absent authority into an explicit contradictory scope claim.
    public = _public_project_channel_snapshot(
        definition, lambda: _committed_project_channels(saver)
    )
    assert public.container_progress is None
    assert public.run_trace is not None
    assert public.run_trace.transitions
    assert public.run_trace.covered_from_start is True
    assert public.status == "completed"


def test_legacy_missing_project_keeps_taken_trace_without_inventing_container_coverage(monkeypatch):
    definition, saver = _compiled_project_channel_run(monkeypatch)

    def legacy_committed():
        values = _committed_project_channels(saver)
        values.pop("project_id", None)
        values.pop("workflow_container_progress", None)
        return values

    public = _public_project_channel_snapshot(definition, legacy_committed)
    assert public.container_progress is None
    assert public.run_trace is not None
    assert public.run_trace.transitions
    assert public.run_trace.covered_from_start is True
    assert public.status == "completed"


@pytest.mark.parametrize("project_id", ["foreign-project", None, 0, False])
def test_public_reader_rejects_contradictory_committed_project_scope(project_id, monkeypatch):
    definition, saver = _compiled_project_channel_run(monkeypatch)

    def contradictory_committed():
        values = _committed_project_channels(saver)
        values["project_id"] = project_id
        return values

    public = _public_project_channel_snapshot(definition, contradictory_committed)
    assert public.container_progress is None
    assert public.run_trace is None
    assert public.status == "completed"
