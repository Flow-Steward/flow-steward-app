from __future__ import annotations

import ast
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.support.source_index import parse_file

from core.infrastructure.runtime import state_reader_runtime

ROOT = Path(__file__).resolve().parents[2]


def _reachable_core_modules(start: str) -> set[str]:
    modules: dict[str, Path] = {}
    for path in (ROOT / "core").rglob("*.py"):
        module = path.with_suffix("").relative_to(ROOT).as_posix().replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        modules[module] = path

    reachable: set[str] = set()
    pending = [start]
    while pending:
        module = pending.pop()
        if module in reachable or module not in modules:
            continue
        reachable.add(module)
        tree = parse_file(modules[module]).tree

        def module_level_nodes(statements: list[ast.stmt]):
            for node in statements:
                yield node
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for field in ("body", "orelse", "finalbody"):
                    nested = getattr(node, field, None)
                    if isinstance(nested, list):
                        yield from module_level_nodes(nested)
                for handler in getattr(node, "handlers", ()):
                    yield from module_level_nodes(handler.body)

        for node in module_level_nodes(tree.body):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = (node.module,)
            for candidate in imported:
                if not candidate.startswith("core."):
                    continue
                resolved = candidate
                while resolved and resolved not in modules:
                    resolved = resolved.rpartition(".")[0]
                if resolved in modules:
                    pending.append(resolved)
    return reachable


def test_get_state_reader_graph_reuses_cached_graph() -> None:
    state = {"graph": None, "dsn": ""}
    state_lock = threading.Lock()
    graph = MagicMock()
    get_checkpointer = MagicMock(return_value=object())
    build_graph = MagicMock(return_value=graph)

    def get_cached_graph():
        return state["graph"], state["dsn"]

    def set_cached_graph(value, dsn):
        state["graph"] = value
        state["dsn"] = dsn

    first = state_reader_runtime.get_state_reader_graph(
        config={"langgraph_postgres_uri": "postgresql://example"},
        force_refresh=False,
        state_lock=state_lock,
        get_cached_graph=get_cached_graph,
        set_cached_graph=set_cached_graph,
        get_checkpointer=get_checkpointer,
        build_graph=build_graph,
    )
    second = state_reader_runtime.get_state_reader_graph(
        config={"langgraph_postgres_uri": "postgresql://example"},
        force_refresh=False,
        state_lock=state_lock,
        get_cached_graph=get_cached_graph,
        set_cached_graph=set_cached_graph,
        get_checkpointer=get_checkpointer,
        build_graph=build_graph,
    )

    assert first is graph
    assert second is graph
    get_checkpointer.assert_called_once()
    build_graph.assert_called_once_with(
        checkpointer=get_checkpointer.return_value,
        validate_contracts=False,
    )


def test_get_thread_state_snapshot_refreshes_after_failure() -> None:
    stale_graph = MagicMock()
    stale_graph.get_state.side_effect = RuntimeError("stale checkpointer")
    fresh_snapshot = MagicMock()
    fresh_graph = MagicMock()
    fresh_graph.get_state.return_value = fresh_snapshot
    logger = MagicMock()
    force_refresh_calls: list[bool] = []

    def get_state_reader_graph(*, force_refresh: bool):
        force_refresh_calls.append(force_refresh)
        return fresh_graph if force_refresh else stale_graph

    snapshot = state_reader_runtime.get_thread_state_snapshot(
        "thread-1",
        get_state_reader_graph=get_state_reader_graph,
        logger=logger,
    )

    assert snapshot is fresh_snapshot
    assert force_refresh_calls == [False, True]
    logger.warning.assert_called_once()


def test_prewarm_state_reader_graph_skips_without_dsn() -> None:
    get_state_reader_graph = MagicMock()
    logger = MagicMock()

    state_reader_runtime.prewarm_state_reader_graph(
        config={"langgraph_postgres_uri": ""},
        get_state_reader_graph=get_state_reader_graph,
        logger=logger,
        getenv=lambda name: "",
    )

    get_state_reader_graph.assert_not_called()
    logger.info.assert_not_called()


def test_committed_checkpoint_values_match_existing_snapshot_state() -> None:
    expected = {
        "current_phase": "implementation",
        "current_step": 2,
        "phase_stack": [
            {
                "phase_id": "implementation",
                "steps": [{"agent": "composer", "skill": "write_section"}],
            }
        ],
    }
    legacy_snapshot = SimpleNamespace(values=expected)
    checkpoint_tuple = SimpleNamespace(checkpoint={"channel_values": expected})
    checkpointer = MagicMock()
    checkpointer.get_tuple.return_value = checkpoint_tuple

    @contextmanager
    def open_checkpointer():
        yield checkpointer

    actual = state_reader_runtime.get_committed_checkpoint_values(
        "thread-1",
        config={"langgraph_postgres_uri": "postgresql://example"},
        logger=MagicMock(),
        open_checkpointer=open_checkpointer,
    )

    assert actual == legacy_snapshot.values
    checkpointer.get_tuple.assert_called_once_with({"configurable": {"thread_id": "thread-1"}})


def test_committed_checkpoint_values_reconnect_once_after_stale_reader() -> None:
    stale = MagicMock()
    stale.get_tuple.side_effect = RuntimeError("stale connection")
    fresh = MagicMock()
    fresh.get_tuple.return_value = SimpleNamespace(
        checkpoint={"channel_values": {"status": "running"}}
    )
    readers = iter((stale, fresh))
    opens = 0

    @contextmanager
    def open_checkpointer():
        nonlocal opens
        opens += 1
        yield next(readers)

    logger = MagicMock()
    actual = state_reader_runtime.get_committed_checkpoint_values(
        "thread-2",
        config={"langgraph_postgres_uri": "postgresql://example"},
        logger=logger,
        open_checkpointer=open_checkpointer,
    )

    assert actual == {"status": "running"}
    assert opens == 2
    logger.warning.assert_called_once()


def test_committed_checkpoint_values_fail_closed_without_thread_or_dsn() -> None:
    open_checkpointer = MagicMock()
    logger = MagicMock()

    assert (
        state_reader_runtime.get_committed_checkpoint_values(
            "",
            config={"langgraph_postgres_uri": "postgresql://example"},
            logger=logger,
            open_checkpointer=open_checkpointer,
        )
        == {}
    )
    assert (
        state_reader_runtime.get_committed_checkpoint_values(
            "thread-3",
            config={"langgraph_postgres_uri": ""},
            logger=logger,
            open_checkpointer=open_checkpointer,
        )
        == {}
    )
    open_checkpointer.assert_not_called()


def test_control_import_closure_excludes_execution_and_graph_composition() -> None:
    reachable = _reachable_core_modules("core.entrypoints.cli.control_process")

    forbidden = {
        module
        for module in reachable
        if module == "core.application.orchestration.graph_runtime"
        or module.startswith("core.infrastructure.runtime.worker")
        or "model_provider" in module
        or "tabular_feed" in module
    }
    assert forbidden == set()


def test_committed_trace_ignores_pending_command_writes():
    committed = {"job_id": "job-1", "workflow_run_trace": {"last_sequence": 0, "transitions": []}}
    pending = {"workflow_run_trace": {"last_sequence": 1, "transitions": [{"id": "not-committed"}]}}
    checkpointer = MagicMock()
    checkpointer.get_tuple.return_value = SimpleNamespace(
        checkpoint={"channel_values": committed},
        pending_writes=[("task", "workflow_run_trace", pending)],
    )

    @contextmanager
    def open_checkpointer():
        yield checkpointer

    values = state_reader_runtime.get_committed_checkpoint_values(
        "thread-1",
        config={"langgraph_postgres_uri": "postgresql://example"},
        logger=MagicMock(),
        open_checkpointer=open_checkpointer,
    )
    assert values == committed
    assert values["workflow_run_trace"]["transitions"] == []
