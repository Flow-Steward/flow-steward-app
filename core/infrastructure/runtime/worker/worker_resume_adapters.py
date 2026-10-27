"""Technical graph, lease, and terminal adapters for workflow resumes."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any

from core.application.jobs.execution_contracts import (
    WorkflowResumeGraphOutcome,
    WorkflowResumeRequest,
    WorkflowResumeTerminalContext,
)
from core.application.jobs.workflow_resume_execution_service import (
    WORKFLOW_RESUME_LEASE_SECONDS,
)
from core.application.jobs.workflow_resume_state import (
    WorkflowResumeExecutionRepository,
    coerce_resume_meta,
)
from core.application.orchestration.child_execution_runtime import (
    ChildExecutionRuntime,
    bind_child_execution_runtime,
)

logger = logging.getLogger("platform.worker")
_LEASE_RENEW_SECONDS = max(1, WORKFLOW_RESUME_LEASE_SECONDS // 3)


class WorkerWorkflowResumeGraphAdapter:
    def __init__(
        self,
        *,
        bl: Any,
        child_execution_runtime: ChildExecutionRuntime | None = None,
        notification_store_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._bl = bl
        self._child_execution_runtime = child_execution_runtime
        self._notification_store_provider = notification_store_provider
        self._runner: Callable[..., Any] | None = None

    def resume(
        self,
        request: WorkflowResumeRequest,
        *,
        cancelled: Callable[[], bool],
    ) -> WorkflowResumeGraphOutcome:
        runner = self._runner or self._build_runner()
        self._runner = runner
        from core.application.workflows.workflow_lifecycle_event_sink import (
            bind_workflow_event_sink,
        )
        from core.infrastructure.runtime.worker.worker_execution_adapters import (
            lifecycle_event_sink,
        )

        with (
            bind_child_execution_runtime(self._child_execution_runtime),
            bind_workflow_event_sink(
                lifecycle_event_sink(self._bl, self._notification_store_provider)
            ),
        ):
            outcome = runner(
                thread_id=request.thread_id,
                resume_value=dict(request.resume_value),
                approved=request.approved,
                action=request.action,
                comment=request.comment,
                interrupt_payload=dict(request.interrupt_payload),
                actor_username=request.actor_username,
                resume_cancelled=cancelled,
                defer_terminal_persistence=True,
                resume_revision=request.resume_revision,
                approval_id=request.approval_id,
            )
        if outcome is None:
            return WorkflowResumeGraphOutcome.not_started(reason="resume_outcome_missing")
        return WorkflowResumeGraphOutcome(
            started=bool(getattr(outcome, "started", False)),
            status=str(getattr(outcome, "status", "") or ""),
            retryable=bool(getattr(outcome, "retryable", False)),
            reason=str(getattr(outcome, "reason", "") or ""),
            result=(
                dict(getattr(outcome, "result", {}))
                if isinstance(getattr(outcome, "result", {}), Mapping)
                else {}
            ),
            error=(str(getattr(outcome, "error", "") or "") or None),
            terminal=bool(getattr(outcome, "terminal", False)),
        )

    def _build_runner(self) -> Callable[..., Any]:
        from core.application.orchestration.graph_runtime import build_registry_graph
        from core.infrastructure.db import job_thread_store
        from core.infrastructure.runtime import serve_interrupt_helpers
        from core.infrastructure.runtime.control import state_reader
        from core.infrastructure.runtime.graph_resume_wiring import (
            GraphResumeRuntimePorts,
            GraphResumeThreadConfig,
            build_graph_resume_runtime_hooks,
        )
        from core.infrastructure.runtime.serve_request_runtime import to_json_obj

        def build_graph(
            checkpointer: object | None = None,
            *,
            validate_contracts: bool = True,
            workflow_ids: object = None,
        ) -> object:
            return build_registry_graph(
                checkpointer=checkpointer,
                validate_contracts=validate_contracts,
                workflow_ids=workflow_ids,
            )

        resume_threads: set[threading.Thread] = set()
        resume_threads_lock = threading.Lock()
        hooks = build_graph_resume_runtime_hooks(
            ports=GraphResumeRuntimePorts(
                resume_shutdown_in_progress=_resume_shutdown_in_progress,
                get_checkpointer=state_reader.get_checkpointer,
                build_graph=build_graph,
                get_logger_only=lambda: self._bl,
                job_thread_store=job_thread_store,
                to_json_obj=to_json_obj,
                append_approval_history=serve_interrupt_helpers.append_approval_history,
                build_approval_entry=serve_interrupt_helpers.build_approval_entry,
                logger=logger,
            ),
            thread_config=GraphResumeThreadConfig(
                unregister_resume_thread=lambda _thread: None,
                resume_threads=resume_threads,
                resume_threads_lock=resume_threads_lock,
                execution_mode=lambda: "thread",
            ),
        )
        return hooks.run_graph_resume


class _ThreadLeaseHandle:
    def __init__(
        self,
        *,
        stop_event: threading.Event,
        lost_event: threading.Event,
        thread: threading.Thread | None,
    ) -> None:
        self._stop_event = stop_event
        self._lost_event = lost_event
        self._thread = thread

    def cancelled(self) -> bool:
        return self._lost_event.is_set()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class WorkerWorkflowResumeLeaseAdapter:
    def __init__(
        self,
        *,
        repository: WorkflowResumeExecutionRepository,
        repository_factory: Callable[[Any], WorkflowResumeExecutionRepository],
        bl: Any,
    ) -> None:
        self._repository = repository
        self._repository_factory = repository_factory
        self._bl = bl

    def start_execution(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
    ) -> _ThreadLeaseHandle:
        return self._start(
            name=f"approval-resume-lease-{job_id}",
            renew=lambda: self._repository.renew_execution(
                job_id=job_id,
                resume_revision=resume_revision,
                lease_token=lease_token,
                lease_seconds=WORKFLOW_RESUME_LEASE_SECONDS,
            ),
        )

    def start_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
    ) -> _ThreadLeaseHandle:
        stop_event = threading.Event()
        lost_event = threading.Event()
        heartbeat_bl = self._bl
        repository = self._repository

        def close_heartbeat_backend() -> None:
            if heartbeat_bl is self._bl:
                return
            connection = getattr(heartbeat_bl, "conn", None)
            close = getattr(connection, "close", None)
            if not callable(close):
                close = getattr(heartbeat_bl, "close", None)
            if not callable(close):
                return
            try:
                close()
            except Exception:
                logger.warning(
                    "workflow resume job_id=%s finalizer heartbeat connection close failed",
                    job_id,
                )

        try:
            dsn = str(getattr(self._bl, "dsn", "") or "").strip()
            if dsn:
                from core.infrastructure.db.platform_stores import PlatformWorkerStore

                heartbeat_bl = PlatformWorkerStore(dsn)
                repository = self._repository_factory(heartbeat_bl)
        except Exception:
            logger.error(
                "workflow resume job_id=%s finalizer heartbeat setup failed",
                job_id,
            )
            lost_event.set()
            close_heartbeat_backend()
            return _ThreadLeaseHandle(
                stop_event=stop_event,
                lost_event=lost_event,
                thread=None,
            )

        def heartbeat() -> None:
            try:
                while not stop_event.wait(_LEASE_RENEW_SECONDS):
                    try:
                        renewed = repository.renew_finalizer(
                            job_id=job_id,
                            resume_revision=resume_revision,
                            finalizer_token=finalizer_token,
                            lease_seconds=WORKFLOW_RESUME_LEASE_SECONDS,
                        )
                    except Exception:
                        logger.exception(
                            "workflow resume job_id=%s finalizer renewal failed",
                            job_id,
                        )
                        lost_event.set()
                        return
                    if not renewed:
                        lost_event.set()
                        return
            finally:
                close_heartbeat_backend()

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"approval-resume-finalizer-{job_id}",
            daemon=True,
        )
        thread: threading.Thread | None = heartbeat_thread
        try:
            heartbeat_thread.start()
        except Exception:
            logger.error(
                "workflow resume job_id=%s finalizer heartbeat start failed",
                job_id,
            )
            lost_event.set()
            close_heartbeat_backend()
            thread = None
        return _ThreadLeaseHandle(
            stop_event=stop_event,
            lost_event=lost_event,
            thread=thread,
        )

    @staticmethod
    def _start(*, name: str, renew: Callable[[], bool]) -> _ThreadLeaseHandle:
        stop_event = threading.Event()
        lost_event = threading.Event()

        def heartbeat() -> None:
            while not stop_event.wait(_LEASE_RENEW_SECONDS):
                try:
                    renewed = renew()
                except Exception:
                    logger.exception("%s renewal failed", name)
                    lost_event.set()
                    return
                if not renewed:
                    lost_event.set()
                    return

        thread = threading.Thread(target=heartbeat, name=name, daemon=True)
        thread.start()
        return _ThreadLeaseHandle(
            stop_event=stop_event,
            lost_event=lost_event,
            thread=thread,
        )


class WorkerWorkflowResumeTerminalAdapter:
    def __init__(
        self,
        *,
        bl: Any,
        notification_store_provider: Callable[[], Any] | None = None,
        worker_id: Callable[[], str],
    ) -> None:
        self._bl = bl
        self._notification_store_provider = notification_store_provider or (lambda: bl)
        self._worker_id = worker_id

    def release_lock(self, context: WorkflowResumeTerminalContext) -> None:
        release = getattr(self._bl, "release_scheduled_job_execution_lock_by_job_id", None)
        if callable(release):
            release(
                job_id=context.job_id,
                release_reason=f"job_{context.final_status}",
                worker_instance_id=self._worker_id(),
            )

    def notify(self, context: WorkflowResumeTerminalContext) -> None:
        from core.infrastructure.runtime.worker.worker_execution_adapters import (
            _catch_up_child_approval,
            _LifecycleReactionStore,
            _run_lifecycle_reactions,
        )

        if context.final_status == "awaiting_child":
            _catch_up_child_approval(
                _LifecycleReactionStore(self._notification_store_provider(), self._bl),
                job_id=context.job_id,
            )
            return
        if not context.notification_event_type:
            return
        project_id = str(context.job_row.get("project_id") or "").strip()
        if not project_id:
            return
        from core.application.jobs.workflow_wait_events import (
            wait_notification_payload_from_result,
        )
        from core.application.notifications.dispatch import dispatch_project_notification_event

        job_meta = coerce_resume_meta(context.job_row.get("meta"))
        raw_approval = context.result.get("approval")
        approval = dict(raw_approval) if isinstance(raw_approval, Mapping) else {}
        occurrence_id = str(approval.get("approval_id") or "").strip()
        wait_payload = wait_notification_payload_from_result(context.final_status, context.result)
        event_id = (
            f"approval-resume:{context.job_id}:{context.resume_revision}:"
            f"{context.notification_event_type}:{occurrence_id or 'terminal'}"
        )
        if context.final_status != "awaiting_timer":
            # A Wait timer starting is observed by workflow reactions only.
            dispatch_project_notification_event(
                self._notification_store_provider(),
                project_id=project_id,
                event_type=context.notification_event_type,
                severity=context.notification_severity,
                workflow_id=context.workflow_id or None,
                job_id=context.job_id,
                event_id=event_id,
                payload={
                    "status": context.final_status,
                    "task_id": str(context.job_row.get("task_id") or "").strip(),
                    "task_summary": str(
                        context.job_row.get("task_summary") or job_meta.get("task_summary") or ""
                    ),
                    "task_source": str(
                        context.job_row.get("task_source") or job_meta.get("task_source") or ""
                    ),
                    "error": str(context.result.get("error") or ""),
                    **approval,
                    **wait_payload,
                },
            )
        _run_lifecycle_reactions(
            _LifecycleReactionStore(self._notification_store_provider(), self._bl),
            event_type=context.notification_event_type,
            project_id=project_id,
            workflow_id=context.workflow_id or None,
            job_id=context.job_id,
            event_id=event_id,
            payload={
                "status": context.final_status,
                "task_summary": str(
                    context.job_row.get("task_summary") or job_meta.get("task_summary") or ""
                ),
                "error": str(context.result.get("error") or ""),
                **wait_payload,
            },
        )

    def sync_provider(self, context: WorkflowResumeTerminalContext) -> None:
        from core.infrastructure.runtime.graph_resume_runtime import _sync_provider_status

        _sync_provider_status(
            bl=self._bl,
            job_id=context.job_id,
            graph_status=context.final_status,
            logger=logger,
        )


def _resume_shutdown_in_progress() -> bool:
    is_finalizing = getattr(sys, "is_finalizing", None)
    return bool(is_finalizing and is_finalizing())
