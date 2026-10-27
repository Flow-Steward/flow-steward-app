"""Durable child invocations against a real PostgreSQL (T35, T36, T37).

One child per invocation, linked in the creating transaction; no child after a
parent cancel; a parent woken only once its child finished; cancel requests that
survive until applied, also to grandchildren; and child data kept while a
running parent has not consumed it.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.application.jobs.invoke_child_link_repository import (
    InvokeChildAlreadyLinked,
    InvokeParentClosed,
    link_child_in_transaction,
    mark_child,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def db(disposable_db):
    import psycopg

    from core.infrastructure.db.platform_stores import PlatformWorkerStore

    conn = psycopg.connect(disposable_db.dsn)
    logger = PlatformWorkerStore(disposable_db.dsn)
    try:
        yield SimpleNamespace(conn=conn, logger=logger, dsn=disposable_db.dsn)
    finally:
        conn.close()
        closer = getattr(logger, "close", None)
        if callable(closer):
            closer()


def _project(conn) -> str:
    project_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, description) VALUES (%s, %s, '')",
            (project_id, f"invoke-child-{project_id[:8]}"),
        )
    conn.commit()
    return project_id


def _job(conn, project_id: str, *, status: str, meta: dict | None = None) -> str:
    job_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (id, project_id, task_id, thread_id, status, meta, started_at,
                              finished_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW(),
                    CASE WHEN %s IN ('completed', 'failed', 'cancelled', 'max_iterations')
                         THEN NOW() - INTERVAL '90 days' END)
            """,
            (
                job_id,
                project_id,
                f"task-{job_id[:8]}",
                f"thread-{job_id}",
                status,
                json.dumps(meta or {}),
                status,
            ),
        )
    conn.commit()
    return job_id


def _row(conn, job_id: str) -> tuple[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, meta FROM jobs WHERE id = %s", (job_id,))
        status, meta = cur.fetchone()
    conn.commit()
    return status, meta or {}


def _entry(key: str) -> dict:
    return {"invocation_key": key, "step_id": "call_child", "child_workflow_id": "wf_child"}


def _child(conn, project_id: str, parent_id: str, *, status: str) -> str:
    child_id = _job(conn, project_id, status=status, meta={"invoke_parent_job_id": parent_id})
    link_child_in_transaction(
        conn, parent_job_id=parent_id, child_job_id=child_id, entry=_entry(f"k-{child_id}")
    )
    conn.commit()
    return child_id


# --------------------------------------------------------------------------- links


def test_one_child_per_invocation_and_none_after_the_parent_closed(db):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    first = _job(db.conn, project, status="pending")

    link_child_in_transaction(
        db.conn, parent_job_id=parent, child_job_id=first, entry=_entry("parent:exec-1")
    )
    db.conn.commit()

    # A retry of the same invocation finds the linked child instead of a second one.
    second = _job(db.conn, project, status="pending")
    retry_entry = _entry("parent:exec-1")
    with pytest.raises(InvokeChildAlreadyLinked) as linked:
        link_child_in_transaction(
            db.conn, parent_job_id=parent, child_job_id=second, entry=retry_entry
        )
    db.conn.rollback()
    assert linked.value.child_job_id == first

    _status, meta = _row(db.conn, parent)
    assert list(meta["invoke_children"]) == [first]
    assert meta["invoke_children"][first]["invocation_key"] == "parent:exec-1"

    # A cancelled parent takes no new child.
    with db.conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (parent,))
    db.conn.commit()
    closed_entry = _entry("parent:exec-2")
    with pytest.raises(InvokeParentClosed):
        link_child_in_transaction(
            db.conn, parent_job_id=parent, child_job_id=second, entry=closed_entry
        )
    db.conn.rollback()


def test_a_crash_before_commit_leaves_no_half_link(db):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _job(db.conn, project, status="pending")

    link_child_in_transaction(
        db.conn, parent_job_id=parent, child_job_id=child, entry=_entry("parent:exec-1")
    )
    db.conn.rollback()  # the creating transaction never committed

    _status, meta = _row(db.conn, parent)
    assert not meta.get("invoke_children")


def test_marks_never_drop_the_invocation_identity(db):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status="completed")

    assert mark_child(
        db.conn, parent_job_id=parent, child_job_id=child, fields={"consumed_at": "now"}
    )
    db.conn.commit()

    entry = _row(db.conn, parent)[1]["invoke_children"][child]
    assert entry["consumed_at"] == "now"
    assert entry["invocation_key"] == f"k-{child}"


# --------------------------------------------------------------------------- wake


def _park(conn, parent: str, child: str) -> None:
    wait = {"wait_id": f"w-{parent[:8]}", "kind": "child", "child_job_id": child}
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET status = 'awaiting_child',
                              meta = meta || jsonb_build_object('workflow_wait', %s::jsonb)
               WHERE id = %s""",
            (json.dumps(wait), parent),
        )
    conn.commit()


def _repository(conn):
    from core.application.jobs.workflow_resume_repository import (
        WorkflowResumeRepositoryAdapter,
    )

    return WorkflowResumeRepositoryAdapter(SimpleNamespace(conn=conn))


def test_a_parent_is_woken_only_once_its_child_finished(db):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status="in_progress")
    _park(db.conn, parent, child)

    assert _repository(db.conn).claim_finished_children(limit=100) == []

    with db.conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'failed' WHERE id = %s", (child,))
    db.conn.commit()

    ((row, _thread),) = _repository(db.conn).claim_finished_children(limit=100)
    status, meta = _row(db.conn, parent)
    assert row.job_id == parent
    assert status == "resume_pending"
    assert meta["workflow_resume"]["reason"] == "child"
    assert meta["workflow_resume"]["resume_value"] == {
        "reason": "child",
        "wait_id": f"w-{parent[:8]}",
        "child_job_id": child,
        "child_status": "failed",
    }
    # Claimed once: the next pass finds nothing.
    assert _repository(db.conn).claim_finished_children(limit=100) == []


def test_a_child_that_finished_before_the_parent_parked_is_not_lost(db):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status="completed")
    assert _repository(db.conn).claim_finished_children(limit=100) == []

    _park(db.conn, parent, child)

    assert len(_repository(db.conn).claim_finished_children(limit=100)) == 1


def test_a_cancelled_parent_is_never_woken_by_its_child(db):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status="completed")
    _park(db.conn, parent, child)
    with db.conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (parent,))
    db.conn.commit()

    assert _repository(db.conn).claim_finished_children(limit=100) == []


def test_the_child_lookup_can_use_its_partial_index(db):
    with db.conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT id FROM jobs WHERE status = 'awaiting_child' "
            "AND meta->'workflow_wait'->>'child_job_id' = 'x'"
        )
        plan = "\n".join(str(row[0]) for row in cur.fetchall())
    db.conn.rollback()
    assert "idx_jobs_workflow_wait_child" in plan


# --------------------------------------------------------------------------- cancel


def test_a_parent_cancel_reaches_running_children_and_grandchildren(db):
    from core.application.jobs.invoke_child_cancellation import apply_child_cancels
    from core.application.jobs.terminal_persistence import TerminalPersistenceService

    project = _project(db.conn)
    parent = _job(db.conn, project, status="awaiting_child")
    running = _child(db.conn, project, parent, status="in_progress")
    finished = _child(db.conn, project, parent, status="completed")
    grandchild = _child(db.conn, project, running, status="pending")

    TerminalPersistenceService(db.logger).complete_failure(
        job_id=parent, status="cancelled", error_code="cancelled", category="cancellation"
    )

    children = _row(db.conn, parent)[1]["invoke_children"]
    assert all("cancel_requested_at" in children[cid] for cid in (running, finished))

    first = apply_child_cancels(db.logger)
    assert first["cancelled"] == 1
    assert first["already_finished"] == 1
    assert _row(db.conn, running)[0] == "cancelled"
    assert _row(db.conn, finished)[0] == "completed"
    parent_meta = _row(db.conn, parent)[1]
    assert all(
        "cancel_applied_at" in parent_meta["invoke_children"][cid] for cid in (running, finished)
    )
    assert "invoke_cancel_pending" not in parent_meta

    # The child's own cancel recorded a request for its child: the next pass applies it.
    apply_child_cancels(db.logger)
    assert _row(db.conn, grandchild)[0] == "cancelled"


def test_a_cancel_request_survives_until_applied_on_a_terminal_parent(db):
    from core.application.jobs.invoke_child_cancellation import apply_child_cancels
    from core.application.jobs.terminal_persistence import TerminalPersistenceService

    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status="in_progress")
    TerminalPersistenceService(db.logger).complete_failure(
        job_id=parent, status="cancelled", error_code="cancelled", category="cancellation"
    )
    # The control process was down for a while: the request is still there.
    assert "cancel_applied_at" not in _row(db.conn, parent)[1]["invoke_children"][child]

    apply_child_cancels(db.logger)

    assert _row(db.conn, child)[0] == "cancelled"


# --------------------------------------------------------------------------- retention


def test_retention_keeps_an_unconsumed_child_of_a_running_parent(db):
    from core.application.tooling.ops.retention_repository import _list_terminal_job_ids

    project = _project(db.conn)
    parent = _job(db.conn, project, status="awaiting_child")
    held = _child(db.conn, project, parent, status="completed")
    consumed = _child(db.conn, project, parent, status="completed")
    mark_child(db.conn, parent_job_id=parent, child_job_id=consumed, fields={"consumed_at": "x"})
    db.conn.commit()

    def eligible() -> set[str]:
        with db.conn.cursor() as cur:
            ids = _list_terminal_job_ids(cur, cache={}, account_id="", days=30)
        db.conn.commit()
        return {str(item) for item in ids}

    assert held not in eligible()
    assert consumed in eligible()

    # The parent ended: normal retention resumes for every child.
    with db.conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (parent,))
    db.conn.commit()
    assert held in eligible()


def test_a_watchdog_kill_of_a_waiting_parent_cancels_its_child_durably(db):
    from core.application.jobs.invoke_child_cancellation import apply_child_cancels
    from core.application.jobs.terminal_persistence import cancel_job_terminally

    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status="in_progress")
    _park(db.conn, parent, child)

    assert cancel_job_terminally(
        db.logger,
        parent,
        reason="Killed by watchdog action: operator kill",
        error_code="watchdog_killed",
    )

    status, meta = _row(db.conn, parent)
    assert status == "cancelled"
    assert "cancel_requested_at" in meta["invoke_children"][child]

    apply_child_cancels(db.logger)

    assert _row(db.conn, child)[0] == "cancelled"
    # A second kill of an already ended job changes nothing.
    assert not cancel_job_terminally(
        db.logger, parent, reason="again", error_code="watchdog_killed"
    )


@pytest.mark.parametrize("crash_before_checkpoint", [False, True])
@pytest.mark.parametrize("child_status", ["completed", "failed"])
def test_child_result_hold_is_released_only_after_postgres_checkpoint(
    db, disposable_db, monkeypatch, crash_before_checkpoint, child_status
):
    from typing import TypedDict

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph

    from core.application.orchestration.agents.common_runtime_helpers import (
        run_graph_with_cancel_check,
    )

    class ParentState(TypedDict):
        agent_messages: list[dict]

    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status=child_status)
    execution_id = str(uuid.uuid4())
    mark_child(
        db.conn,
        parent_job_id=parent,
        child_job_id=child,
        fields={"step_execution_id": execution_id},
    )
    db.conn.commit()
    message = {
        "step_id": "call_child",
        "step_execution_id": execution_id,
        "artifacts": {"_invoke_workflow": {"child_job_id": child}},
    }
    if child_status == "failed":
        message["artifacts"] = {
            "_invoke_workflow_error": "child_failed",
            "_invoke_workflow_child_job_id": child,
            "_invoke_workflow_child_status": "failed",
        }
    config = {"configurable": {"thread_id": f"audit-consumption-{parent}"}}
    with PostgresSaver.from_conn_string(disposable_db.uri) as saver:
        saver.setup()
        original_put = saver.put
        crash = crash_before_checkpoint

        def put(config, checkpoint, metadata, new_versions):
            nonlocal crash
            if crash and checkpoint.get("channel_values", {}).get("agent_messages"):
                crash = False
                raise RuntimeError("crash before parent checkpoint commit")
            return original_put(config, checkpoint, metadata, new_versions)

        monkeypatch.setattr(saver, "put", put)
        graph = StateGraph(ParentState)
        graph.add_node("consume_child", lambda _state: {"agent_messages": [message]})
        graph.add_edge(START, "consume_child")
        graph.add_edge("consume_child", END)
        compiled = graph.compile(checkpointer=saver)
        if crash_before_checkpoint:
            with pytest.raises(RuntimeError, match="before parent checkpoint"):
                run_graph_with_cancel_check(
                    compiled, {"agent_messages": []}, config, bl=db.logger, job_id=parent
                )
            assert not _row(db.conn, parent)[1]["invoke_children"][child].get("consumed_at")
            initial = None  # Replay the persisted invocation after worker recovery.
        else:
            initial = {"agent_messages": []}
        run_graph_with_cancel_check(compiled, initial, config, bl=db.logger, job_id=parent)
        assert compiled.get_state(config).values["agent_messages"] == [message]
        assert _row(db.conn, parent)[1]["invoke_children"][child].get("consumed_at")


@pytest.mark.parametrize(
    "status,finished,expected",
    [
        ("stuck", False, False),
        ("orphaned", False, False),
        ("stuck", True, True),
    ],
)
def test_child_scan_distinguishes_finished_from_recoverable_watchdog_status(
    db, status, finished, expected
):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status=status)
    if finished:
        with db.conn.cursor() as cur:
            cur.execute("UPDATE jobs SET finished_at=NOW() WHERE id=%s", (child,))
        db.conn.commit()
    _park(db.conn, parent, child)
    claims = _repository(db.conn).claim_finished_children(limit=100)
    assert bool(claims) is expected
    assert _row(db.conn, parent)[0] == ("resume_pending" if expected else "awaiting_child")


def test_child_scan_wakes_missing_child_once_for_explicit_failure(db):
    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    missing_child = str(uuid.uuid4())
    _park(db.conn, parent, missing_child)
    claims = _repository(db.conn).claim_finished_children(limit=100)
    assert len(claims) == 1
    status, meta = _row(db.conn, parent)
    assert status == "resume_pending"
    assert meta["workflow_resume"]["resume_value"]["child_job_id"] == missing_child
    assert meta["workflow_resume"]["resume_value"]["child_status"] == "missing"
    assert _repository(db.conn).claim_finished_children(limit=100) == []


@pytest.mark.parametrize("queued_before_cancel", [False, True])
@pytest.mark.parametrize("grandchild", [False, True])
def test_cancelled_ancestor_fences_checkpointed_timer_before_reconciliation(
    db, disposable_db, monkeypatch, queued_before_cancel, grandchild
):
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    from core.application.jobs.invoke_child_cancellation import apply_child_cancels
    from core.application.jobs.terminal_persistence import TerminalPersistenceService
    from core.application.jobs.workflow_wait_projection import (
        durable_wait_from_state,
        workflow_wait_meta,
    )
    from core.application.orchestration.active_step import active_step
    from core.application.orchestration.agents.common_runtime_helpers import (
        JobCancelledException,
        run_graph_with_cancel_check,
    )
    from core.application.orchestration.run_cursor import (
        RUNTIME_SCHEMA_VERSION,
        RunCursor,
        begin_step,
    )
    from core.application.orchestration.run_definition import (
        clear_run_definition_cache,
        prime_run_definition,
    )
    from core.application.orchestration.state import OrchestratorState
    from core.application.orchestration.wait_step_runtime import timer_pending_wait, wait_step_node
    from core.application.workflows.workflow_definition_snapshot import definition_digest
    from tests._workflow_definitions import node

    project = _project(db.conn)
    parent = _job(db.conn, project, status="in_progress")
    child = _child(db.conn, project, parent, status="in_progress")
    leaf = _child(db.conn, project, child, status="in_progress") if grandchild else child
    completed = _child(db.conn, project, parent, status="completed")
    mark_child(
        db.conn, parent_job_id=parent, child_job_id=completed, fields={"consumed_at": "before"}
    )
    with db.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = meta || %s::jsonb WHERE id = %s",
            (json.dumps({"workflow_outputs": {"text": "already done"}}), completed),
        )
    db.conn.commit()
    _park(db.conn, parent, child)
    if grandchild:
        _park(db.conn, child, leaf)

    definition = {
        "steps": [
            {"step_id": "pause", "step_kind": "wait", "override": {"wait": {"duration_ms": 5}}}
        ],
        "groups": [],
        "execution_graph": {"schema_version": 1, "entry": node("pause"), "edges": []},
        "settings": {},
    }
    ref = {"snapshot_id": str(uuid.uuid4()), "revision": 1, "digest": definition_digest(definition)}
    prime_run_definition(ref, definition)
    state = {
        "job_id": leaf,
        "project_id": project,
        "workflow_id": "wf_child",
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "run_cursor": begin_step(RunCursor(), "pause").dump(),
        "agent_messages": [],
        "status": "in_progress",
    }
    clock = 1_760_000_000_000
    monkeypatch.setattr("core.application.orchestration.wait_step_runtime._now_ms", lambda: clock)
    state["pending_wait"] = timer_pending_wait(
        active_step(state), duration_ms=5, revision=1, now_ms=clock
    )
    config = {"configurable": {"thread_id": f"thread-{leaf}"}}
    actions = []

    def text_action(_state):
        actions.append("new text")
        return {"status": "completed", "final_report": "new text"}

    try:
        with PostgresSaver.from_conn_string(disposable_db.uri) as saver:
            saver.setup()
            builder = StateGraph(OrchestratorState)
            builder.add_node("wait_step", wait_step_node)
            builder.add_node("text_action", text_action)
            builder.add_edge(START, "wait_step")
            builder.add_edge("wait_step", "text_action")
            builder.add_edge("text_action", END)
            graph = builder.compile(checkpointer=saver)
            run_graph_with_cancel_check(graph, state, config, bl=db.logger, job_id=leaf)
            wait = durable_wait_from_state(graph.get_state(config))
            assert wait is not None
            with db.conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET status = 'awaiting_timer', meta = meta || %s::jsonb WHERE id = %s",
                    (
                        json.dumps(
                            {"workflow_wait": workflow_wait_meta(wait, thread_id=f"thread-{leaf}")}
                        ),
                        leaf,
                    ),
                )
            db.conn.commit()
            clock += 5
            repository = _repository(db.conn)
            if queued_before_cancel:
                assert [
                    row.job_id
                    for row, _ in repository.claim_due_timers(now_epoch_ms=clock, limit=100)
                ] == [leaf]
            TerminalPersistenceService(db.logger).complete_failure(
                job_id=parent, status="cancelled", error_code="cancelled", category="cancellation"
            )
            parent_status, parent_meta = _row(db.conn, parent)
            assert parent_status == "cancelled"
            assert parent_meta["invoke_children"][child].get("cancel_requested_at")
            assert not parent_meta["invoke_children"][child].get("cancel_applied_at")
            assert "cancel_requested_at" not in parent_meta["invoke_children"][completed]
            if grandchild:
                assert _row(db.conn, child)[0] == "awaiting_child"
                assert not _row(db.conn, child)[1]["invoke_children"][leaf].get(
                    "cancel_requested_at"
                )
            if not queued_before_cancel:
                assert _row(db.conn, leaf)[0] == "awaiting_timer"
                assert [
                    row.job_id
                    for row, _ in repository.claim_due_timers(now_epoch_ms=clock, limit=100)
                ] == [leaf]
            wake = _row(db.conn, leaf)[1]["workflow_resume"]["resume_value"]
            assert wake["wait_id"] == state["pending_wait"]["wait_id"]
            # Inspect actions before the exception assertion: old code enters the action.
            cancelled = None
            try:
                run_graph_with_cancel_check(
                    graph, Command(resume=wake), config, bl=db.logger, job_id=leaf
                )
            except JobCancelledException as exc:
                cancelled = exc
            assert actions == []
            assert cancelled is not None
            assert "parent" in str(cancelled).lower()
            assert not _row(db.conn, parent)[1]["invoke_children"][child].get("cancel_applied_at")
            assert graph.get_state(config).values.get("final_report") != "new text"

        apply_child_cancels(db.logger)
        if grandchild:
            apply_child_cancels(db.logger)
        assert _row(db.conn, leaf)[0] == "cancelled"
        assert _row(db.conn, parent)[1]["invoke_children"][child].get("cancel_applied_at")
        assert apply_child_cancels(db.logger)["cancelled"] == 0
        assert _row(db.conn, completed)[0] == "completed"
        assert _row(db.conn, completed)[1]["workflow_outputs"] == {"text": "already done"}
    finally:
        clear_run_definition_cache()
