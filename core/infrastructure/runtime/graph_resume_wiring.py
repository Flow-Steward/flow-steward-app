"""Shared graph-resume wiring for HTTP and non-HTTP runtime entrypoints."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, cast

from core.infrastructure.runtime import graph_resume_runtime


class GraphBuilder(Protocol):
    """Build a runtime graph for one or more workflow identifiers."""

    def __call__(
        self,
        checkpointer: object | None = None,
        *,
        validate_contracts: bool = True,
        workflow_ids: object = None,
    ) -> object: ...


class ApprovalEntryBuilder(Protocol):
    """Build the persisted record for one approval decision."""

    def __call__(
        self,
        interrupt_payload: dict[str, object],
        approved: bool,
        action: str | None,
        comment: str,
        override_content: str = "",
        actor_username: str = "operator",
        timestamp: str | None = None,
    ) -> dict[str, object]: ...


class JobThreadStore(Protocol):
    """Persistence operations used while resuming a graph."""

    def get_job_context_by_thread_id(
        self, connection: object, thread_id: str
    ) -> Mapping[str, object] | None: ...

    def get_job_meta_by_thread_id(
        self, connection: object, thread_id: str
    ) -> Mapping[str, object] | None: ...

    def update_job_status_by_thread_id(
        self,
        connection: object,
        *,
        thread_id: str,
        status: str,
        commit: bool = False,
    ) -> int: ...

    def update_job_meta_by_thread_id(
        self,
        connection: object,
        *,
        thread_id: str,
        meta: object,
        commit: bool = False,
    ) -> int: ...


class ResumeThreadsLock(Protocol):
    """Lock contract used to protect the active resume-thread set."""

    def __enter__(self) -> bool: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class GraphResumeRunner(Protocol):
    """Execute one approval resume synchronously."""

    def __call__(
        self,
        thread_id: str,
        resume_value: dict[str, object],
        approved: bool,
        action: str | None,
        comment: str,
        interrupt_payload: dict[str, object],
        *,
        actor_username: str = "operator",
        resume_cancelled: Callable[[], bool] | None = None,
        defer_terminal_persistence: bool = False,
        resume_revision: int = 0,
    ) -> None: ...


class StartGraphResumeThread(Protocol):
    """Schedule one approval resume on the configured execution mode."""

    def __call__(
        self,
        thread_id: str,
        resume_value: dict[str, object],
        approved: bool,
        action: str | None,
        comment: str,
        interrupt_payload: dict[str, object],
        *,
        actor_username: str = "operator",
    ) -> threading.Thread | None: ...


@dataclass(frozen=True)
class GraphResumeRuntimePorts:
    """Typed technical dependencies required by graph-resume execution."""

    resume_shutdown_in_progress: Callable[[], bool]
    get_checkpointer: Callable[[], object | None]
    build_graph: GraphBuilder
    get_logger_only: Callable[[], object | None]
    job_thread_store: JobThreadStore
    to_json_obj: Callable[[object], object]
    append_approval_history: Callable[
        [list[dict[str, object]], dict[str, object]], list[dict[str, object]]
    ]
    build_approval_entry: ApprovalEntryBuilder
    logger: logging.Logger


@dataclass(frozen=True)
class GraphResumeThreadConfig:
    """Thread registry and executor selection for graph resumes."""

    unregister_resume_thread: Callable[[threading.Thread], None]
    resume_threads: set[threading.Thread]
    resume_threads_lock: ResumeThreadsLock
    execution_mode: Callable[[], str]


@dataclass(frozen=True)
class GraphResumeRuntimeHooks:
    """Runtime callbacks needed by approval/resume services."""

    run_graph_resume: GraphResumeRunner
    start_graph_resume_thread: StartGraphResumeThread


def build_graph_resume_runtime_hooks(
    *,
    ports: GraphResumeRuntimePorts,
    thread_config: GraphResumeThreadConfig,
    run_graph_resume_for_thread: GraphResumeRunner | None = None,
) -> GraphResumeRuntimeHooks:
    """Compose graph-resume callbacks once for every transport/runtime boundary."""

    run_graph_resume = cast(
        GraphResumeRunner,
        graph_resume_runtime.build_run_graph_resume_handler(
            resume_shutdown_in_progress=ports.resume_shutdown_in_progress,
            get_checkpointer=ports.get_checkpointer,
            build_graph=ports.build_graph,
            get_logger_only=ports.get_logger_only,
            job_thread_store=ports.job_thread_store,
            to_json_obj=ports.to_json_obj,
            append_approval_history=ports.append_approval_history,
            build_approval_entry=ports.build_approval_entry,
            logger=ports.logger,
        ),
    )

    start_graph_resume_thread = cast(
        StartGraphResumeThread,
        graph_resume_runtime.build_start_graph_resume_thread_handler(
            run_graph_resume=run_graph_resume_for_thread or run_graph_resume,
            unregister_resume_thread=thread_config.unregister_resume_thread,
            resume_shutdown_in_progress=ports.resume_shutdown_in_progress,
            resume_threads=thread_config.resume_threads,
            resume_threads_lock=thread_config.resume_threads_lock,
            logger=ports.logger,
            execution_mode=thread_config.execution_mode,
        ),
    )

    return GraphResumeRuntimeHooks(
        run_graph_resume=run_graph_resume,
        start_graph_resume_thread=start_graph_resume_thread,
    )


__all__ = [
    "GraphResumeRunner",
    "GraphResumeRuntimeHooks",
    "GraphResumeRuntimePorts",
    "GraphResumeThreadConfig",
    "StartGraphResumeThread",
    "build_graph_resume_runtime_hooks",
]
