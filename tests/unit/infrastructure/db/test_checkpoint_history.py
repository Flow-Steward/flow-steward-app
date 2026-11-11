"""Checkpoint storage stays the size of a thread's current state.

Workflow nodes return the whole ``agent_messages`` list on every step
(``[*state["agent_messages"], message]`` with a last-write-wins reducer). With
LangGraph's PostgresSaver every step then stored another full copy of that list,
so a run of N steps stored O(N^2) bytes: 1.9 GB for one 434-step run on the demo.
These tests run a graph of exactly that shape on a real PostgreSQL.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("disposable_db")

STEPS = 30
MESSAGE = "x" * 20_000


class _State(TypedDict):
    agent_messages: Annotated[list[str], lambda _old, new: new]
    steps: Annotated[int, operator.add]


def _graph(saver: Any, *, interrupt_at: int | None = None) -> Any:
    from langgraph.graph import END, START, StateGraph

    def step(state: _State) -> dict[str, Any]:
        return {"agent_messages": [*state["agent_messages"], MESSAGE], "steps": 1}

    def route(state: _State) -> str:
        if state["steps"] >= STEPS:
            return END
        if interrupt_at is not None and state["steps"] == interrupt_at:
            return "pause"
        return "step"

    builder = StateGraph(_State)
    builder.add_node("step", step)
    builder.add_node("pause", lambda _state: {})
    builder.add_edge(START, "step")
    builder.add_conditional_edges("step", route, ["step", "pause", END])
    builder.add_edge("pause", "step")
    return builder.compile(
        checkpointer=saver,
        interrupt_before=["pause"] if interrupt_at is not None else None,
    )


def _stored_bytes(conn: Any, thread_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE((SELECT sum(octet_length(blob)) FROM checkpoint_blobs
                          WHERE thread_id = %(t)s), 0)
                + COALESCE((SELECT sum(octet_length(blob)) FROM checkpoint_writes
                            WHERE thread_id = %(t)s), 0)
                + COALESCE((SELECT sum(octet_length(checkpoint::text)) FROM checkpoints
                            WHERE thread_id = %(t)s), 0)
            """,
            {"t": thread_id},
        )
        return int(cur.fetchone()[0])


def _checkpoint_count(conn: Any, thread_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,))
        return int(cur.fetchone()[0])


def _forget(conn: Any, thread_id: str) -> None:
    with conn.cursor() as cur:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))  # noqa: S608


@pytest.fixture
def saver_pair(disposable_db):
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    from core.infrastructure.db.checkpoint_saver import history_bounded_saver_class

    with (
        psycopg.connect(disposable_db.dsn, autocommit=True) as plain_conn,
        psycopg.connect(disposable_db.dsn, autocommit=True) as bounded_conn,
        psycopg.connect(disposable_db.dsn, autocommit=True) as probe,
    ):
        from psycopg.rows import dict_row

        plain_conn.row_factory = dict_row
        bounded_conn.row_factory = dict_row
        plain = PostgresSaver(plain_conn)
        plain.setup()
        bounded = history_bounded_saver_class()(bounded_conn)
        threads: list[str] = []
        yield plain, bounded, probe, threads
        for thread_id in threads:
            _forget(probe, thread_id)


def _run(saver: Any, thread_id: str) -> dict[str, Any]:
    return _graph(saver).invoke(
        {"agent_messages": [], "steps": 0},
        {"configurable": {"thread_id": thread_id}, "recursion_limit": 1000},
    )


def test_storage_stays_the_size_of_the_current_state(saver_pair) -> None:
    plain, bounded, probe, threads = saver_pair
    plain_thread, bounded_thread = f"plain-{uuid4().hex}", f"bounded-{uuid4().hex}"
    threads += [plain_thread, bounded_thread]

    assert len(_run(plain, plain_thread)["agent_messages"]) == STEPS
    assert len(_run(bounded, bounded_thread)["agent_messages"]) == STEPS

    final_state = STEPS * len(MESSAGE)
    # The unbounded saver keeps a copy per step: about STEPS^2 / 2 messages.
    assert _stored_bytes(probe, plain_thread) > 10 * final_state
    # One checkpoint: its blob plus the last step's write, each one full list.
    assert _stored_bytes(probe, bounded_thread) < 3 * final_state
    assert _checkpoint_count(probe, bounded_thread) == 1


def test_bounded_state_reads_back_whole(saver_pair) -> None:
    _plain, bounded, _probe, threads = saver_pair
    thread_id = f"bounded-{uuid4().hex}"
    threads.append(thread_id)
    _run(bounded, thread_id)

    state = _graph(bounded).get_state({"configurable": {"thread_id": thread_id}})

    assert state.values["agent_messages"] == [MESSAGE] * STEPS
    assert state.values["steps"] == STEPS


def test_an_interrupted_run_resumes_from_the_kept_checkpoint(saver_pair) -> None:
    _plain, bounded, probe, threads = saver_pair
    thread_id = f"bounded-{uuid4().hex}"
    threads.append(thread_id)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 1000}
    graph = _graph(bounded, interrupt_at=12)

    graph.invoke({"agent_messages": [], "steps": 0}, config)
    paused = graph.get_state(config)
    assert paused.next == ("pause",)
    assert len(paused.values["agent_messages"]) == 12
    assert _checkpoint_count(probe, thread_id) == 1

    finished = graph.invoke(None, config)

    assert finished["agent_messages"] == [MESSAGE] * STEPS
    assert _checkpoint_count(probe, thread_id) == 1


def test_retention_compacts_history_saved_before_the_bound(saver_pair) -> None:
    from core.infrastructure.db.checkpoint_history import prune_threads_to_latest

    plain, _bounded, probe, threads = saver_pair
    thread_id = f"plain-{uuid4().hex}"
    threads.append(thread_id)
    _run(plain, thread_id)
    before = _stored_bytes(probe, thread_id)

    with probe.cursor() as cur:
        dropped = prune_threads_to_latest(cur, [thread_id])

    assert dropped > STEPS
    assert _checkpoint_count(probe, thread_id) == 1
    assert _stored_bytes(probe, thread_id) < before / 10
    state = _graph(plain).get_state({"configurable": {"thread_id": thread_id}})
    assert state.values["agent_messages"] == [MESSAGE] * STEPS


def test_a_failed_prune_never_fails_the_run(saver_pair, monkeypatch, caplog) -> None:
    from core.infrastructure.db import checkpoint_saver

    _plain, bounded, _probe, threads = saver_pair
    thread_id = f"bounded-{uuid4().hex}"
    threads.append(thread_id)

    def fail(*_args, **_kwargs):
        raise RuntimeError("prune failed")

    monkeypatch.setattr(checkpoint_saver, "prune_thread_history", fail)

    assert len(_run(bounded, thread_id)["agent_messages"]) == STEPS
    assert "Checkpoint history pruning failed: RuntimeError" in caplog.text
