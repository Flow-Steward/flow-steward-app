"""Technical graph, runtime-mode, and terminal adapters for normal jobs."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from core.application.jobs.execution_contracts import (
    TaskExecutionRepository,
    TaskExecutionRequest,
    TaskGraphOutcome,
    TaskNotification,
    terminal_failure_context,
)
from core.application.jobs.invoke_child_link_repository import InvokeCancellationFenceUnavailable
from core.application.jobs.workflow_wait_projection import (
    DURABLE_WAIT_STATUSES,
    durable_wait_from_state,
    workflow_wait_meta,
)
from core.application.orchestration.checkpoint_requirement import CheckpointUnavailableError
from core.application.orchestration.child_execution_runtime import (
    ChildExecutionRuntime,
    bind_child_execution_runtime,
)
from core.application.workflows.workflow_lifecycle_event_sink import bind_workflow_event_sink
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.observability.exception_logging import redacted_exception_info

logger = logging.getLogger("platform.worker")


class WorkerGraphExecutionAdapter:
    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        bl: Any,
        child_execution_runtime: ChildExecutionRuntime | None = None,
        notification_store_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        self._bl = bl
        self._child_execution_runtime = child_execution_runtime
        self._notification_store_provider = notification_store_provider

    def run(
        self,
        request: TaskExecutionRequest,
        *,
        job_row: Mapping[str, Any] | None,
        project_id: str | None,
    ) -> TaskGraphOutcome:
        from core.application.orchestration.agents.common import (
            JobCancelledException,
            run_graph_with_cancel_check,
        )

        callback = None
        try:
            with (
                self._build_checkpointer() as checkpointer,
                bind_child_execution_runtime(self._child_execution_runtime),
                bind_workflow_event_sink(
                    None
                    if getattr(request, "test_mode", False)
                    else lifecycle_event_sink(self._bl, self._notification_store_provider)
                ),
            ):
                graph_options = (
                    {"pinned_definition": True}
                    if (job_row or {}).get("workflow_definition_snapshot_id")
                    else {}
                )
                graph = self._build_graph(request, checkpointer=checkpointer, **graph_options)
                initial_state = self._build_initial_state(request)
                if (
                    str(getattr(request, "task_source", "") or "").strip().lower()
                    == "step_test_run"
                ):
                    from core.application.orchestration.run_definition import ensure_run_definition

                    # The selected cursor is already active when project-context
                    # intake runs, before the hub can bind the archived definition.
                    _, pinned = ensure_run_definition(initial_state)
                    initial_state.update(pinned)
                from core.application.orchestration.graph_runtime import graph_run_config

                invoke_config: dict[str, Any] = graph_run_config(request.thread_id)
                callback = self._build_callback(project_id, request.job_id)
                if callback is not None:
                    invoke_config["callbacks"] = [callback]
                result = run_graph_with_cancel_check(
                    graph,
                    initial_state,
                    invoke_config,
                    bl=self._bl,
                    job_id=request.job_id,
                )
                interrupt_state = self._pending_interrupt_state(graph, invoke_config, result)
                return TaskGraphOutcome.from_result(
                    result if isinstance(result, Mapping) else {},
                    pending_interrupt=interrupt_state is not None,
                    interrupt_state=interrupt_state,
                    terminal_error_context=_terminal_error_context(callback),
                    root_run_id=_root_run_id(callback),
                )
        except JobCancelledException as exc:
            if exc.error_code:
                return TaskGraphOutcome(
                    kind="cancelled",
                    result={"error_code": exc.error_code, "error": str(exc)},
                    root_run_id=_root_run_id(callback),
                )
            return TaskGraphOutcome.cancelled(root_run_id=_root_run_id(callback))
        except GeneratorExit:
            return TaskGraphOutcome.generator_exit(root_run_id=_root_run_id(callback))
        except Exception as exc:
            return TaskGraphOutcome.failed(exc, root_run_id=_root_run_id(callback))

    def _build_graph(
        self, request: TaskExecutionRequest, *, checkpointer: Any, pinned_definition: bool = False
    ) -> Any:
        from core.application.orchestration.graph_runtime import build_registry_graph

        workflow_step_scope = None
        if str(request.task_source or "").strip().lower() == "step_test_run":
            context = request.extra_state.get("step_test_run_context")
            if not isinstance(context, Mapping):
                raise RuntimeError("Invalid trusted step test run context")
            workflow_id = str(context.get("workflow_id") or "").strip()
            step_id = str(context.get("step_id") or "").strip()
            if (
                not workflow_id
                or not step_id
                or workflow_id != str(request.workflow_id or "").strip()
            ):
                raise RuntimeError("Invalid trusted step test run context")
            workflow_step_scope = {"workflow_id": workflow_id, "step_id": step_id}

        # Submission already validated and pinned this job's definition. Live
        # edits after enqueue must not prevent its first worker execution.
        graph_options = {"validate_contracts": False} if pinned_definition else {}
        return build_registry_graph(
            checkpointer=checkpointer,
            workflow_ids=[request.workflow_id] if request.workflow_id else None,
            workflow_step_scope=workflow_step_scope,
            **graph_options,
        )

    @contextlib.contextmanager
    def _build_checkpointer(self) -> Iterator[Any]:
        """The run's checkpointer; a run without one refuses before any step."""
        uri = str(self._config.get("langgraph_postgres_uri") or "")
        if not uri:
            raise CheckpointUnavailableError("checkpoint storage is not configured")
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except Exception as exc:
            logger.error(
                "PostgresSaver is not importable: %s",
                type(exc).__name__,
                exc_info=redacted_exception_info(exc),
            )
            raise CheckpointUnavailableError("checkpoint support is not installed") from None
        stack = contextlib.ExitStack()
        try:
            checkpointer = stack.enter_context(PostgresSaver.from_conn_string(uri))
        except Exception as exc:
            stack.close()
            # Driver payloads and chained exceptions may contain connection credentials.
            logger.error(
                "PostgresSaver init failed: %s",
                type(exc).__name__,
                exc_info=redacted_exception_info(exc),
            )
            raise CheckpointUnavailableError("checkpoint storage could not be opened") from None
        with stack:
            yield checkpointer

    @staticmethod
    def _build_initial_state(request: TaskExecutionRequest) -> Any:
        from core.application.orchestration.state import build_initial_orchestrator_state

        return build_initial_orchestrator_state(
            task_id=request.task_id,
            task_summary=request.task_summary,
            task_source=request.task_source,
            project_name=request.project_name,
            project_config=dict(request.project_config),
            test_mode=request.test_mode,
            status="in_progress",
            extra_state=dict(request.extra_state),
        )

    def _build_callback(self, project_id: str | None, job_id: str) -> Any:
        if not self._bl or not project_id or not job_id:
            return None
        from core.infrastructure.db.platform_logger import PlatformLoggerCallback

        try:
            return PlatformLoggerCallback(self._bl, project_id, job_id)
        except TypeError:
            logger.debug(
                "PlatformLoggerCallback constructor rejected worker context; continuing without it"
            )
            return None

    @staticmethod
    def _pending_interrupt_state(
        graph: Any,
        invoke_config: Mapping[str, Any],
        result: Mapping[str, Any] | Any,
    ) -> Any | None:
        status = (
            str((result or {}).get("status") or "in_progress")
            if isinstance(result, Mapping)
            else "in_progress"
        )
        if status in {
            "completed",
            "failed",
            "stuck",
            "max_iterations",
            "cancelled",
        }:
            return None
        try:
            graph_state = graph.get_state(invoke_config)
            if graph_state.tasks and any(task.interrupts for task in graph_state.tasks):
                return graph_state
        except Exception as exc:
            logger.debug("Could not check graph state for interrupts: %s", exc)
        return None


class WorkerRuntimeModeAdapter:
    @contextlib.contextmanager
    def activate(self, enabled: bool) -> Iterator[None]:
        from core.infrastructure.runtime.test_mode import (
            reset_runtime_test_mode,
            set_runtime_test_mode,
        )

        token = set_runtime_test_mode(enabled)
        try:
            yield
        finally:
            reset_runtime_test_mode(token)


class _LifecycleReactionStore:
    """Notification and extension reads from one store, job reads from the other.

    Reactions need the workflow's rules, routes, connections and extension
    installs (the notification store) and the job itself (the worker store);
    neither store holds both.
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        self._primary = primary
        self._fallback = fallback

    def __getattr__(self, name: str) -> Any:
        for store in (self._primary, self._fallback):
            if store is not None and hasattr(store, name):
                return getattr(store, name)
        raise AttributeError(name)


def _run_lifecycle_reactions(bl: Any, **event: Any) -> None:
    """The workflow's own automations for this moment: "a job completes", and so on.

    Never raises: an automation must not fail the finalization of its job.
    """
    try:
        from core.application.workflows.job_lifecycle_reactions import (
            run_job_lifecycle_reactions,
        )
        from core.infrastructure.runtime.worker.worker_common import _to_json_obj

        run_job_lifecycle_reactions(bl, to_json_obj=_to_json_obj, **event)
    except Exception as exc:
        logger.warning("workflow automations skipped for job_id=%s: %s", event.get("job_id"), exc)


class WorkerLifecycleEventSink:
    """Hands the step hub's step and group events to the workflow's reactions.

    Bound for one graph run. The job row gives the project, account and test
    mode once per job; the workflow's enabled rules are read once per workflow,
    so a run without a matching rule costs no notification work per step.
    """

    def __init__(self, bl: Any, notification_store_provider: Callable[[], Any] | None) -> None:
        self._bl = bl
        self._notification_store_provider = notification_store_provider or (lambda: bl)
        self._store: Any = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._rule_events: dict[str, frozenset[str]] = {}

    def _reaction_store(self) -> Any:
        if self._store is None:
            self._store = _LifecycleReactionStore(self._notification_store_provider(), self._bl)
        return self._store

    def _job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            row = self._bl.get_job(job_id) if hasattr(self._bl, "get_job") else None
            self._jobs[job_id] = dict(row) if isinstance(row, Mapping) else {}
        return self._jobs[job_id]

    def _observed_events(self, store: Any, workflow_id: str) -> frozenset[str]:
        if workflow_id not in self._rule_events:
            rows: list[Any] = []
            if hasattr(store, "list_workflow_reactions"):
                rows = list(store.list_workflow_reactions(workflow_id, enabled_only=True) or [])
            self._rule_events[workflow_id] = frozenset(
                str(row.get("event_type") or "").strip() for row in rows if isinstance(row, Mapping)
            )
        return self._rule_events[workflow_id]

    def __call__(
        self,
        *,
        event_type: str,
        job_id: str,
        workflow_id: str,
        event_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        from .worker_common import _to_json_obj

        job = self._job(job_id)
        project_id = str(job.get("project_id") or "").strip()
        if not project_id:
            return
        meta = _to_json_obj(job.get("meta"))
        if isinstance(meta, Mapping) and meta.get("test_mode") in (True, "true", "1"):
            return
        store = self._reaction_store()
        if event_type not in self._observed_events(store, workflow_id):
            return
        _run_lifecycle_reactions(
            store,
            event_type=event_type,
            project_id=project_id,
            workflow_id=workflow_id,
            job_id=job_id,
            event_id=event_id,
            payload=dict(payload),
            account_id=str(job.get("account_id") or "").strip() or None,
        )


def lifecycle_event_sink(
    bl: Any, notification_store_provider: Callable[[], Any] | None
) -> WorkerLifecycleEventSink | None:
    """The step/group event sink for one graph run, or ``None`` without a store."""
    if bl is None:
        return None
    return WorkerLifecycleEventSink(bl, notification_store_provider)


def _catch_up_child_approval(bl: Any, *, job_id: str) -> None:
    """A parent that just parked on its child notifies for an approval already pending below."""
    try:
        from core.application.workflows.approval_waiting_propagation import (
            catch_up_child_approval,
        )
        from core.infrastructure.runtime.worker.worker_common import _to_json_obj

        catch_up_child_approval(bl, parent_job_id=job_id, to_json_obj=_to_json_obj)
    except Exception as exc:
        logger.warning("child approval catch-up skipped for job_id=%s: %s", job_id, exc)


class WorkerTerminalEffectsAdapter:
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

    def persist(
        self,
        request: TaskExecutionRequest,
        *,
        job_row: Mapping[str, Any] | None,
        project_id: str | None,
        final_status: str,
        result: dict[str, Any],
        graph_outcome: TaskGraphOutcome | None,
    ) -> str | None:
        del job_row
        if self._bl is None or not request.job_id:
            return None
        from core.application.jobs.terminal_persistence import TerminalPersistenceConflict

        try:
            if final_status == "completed":
                terminal_result = _terminal_persistence_service(self._bl).complete_success(
                    job_id=request.job_id,
                    workflow_outputs=result.get("workflow_outputs"),
                    final_report=result.get("final_report"),
                    root_run_id=(graph_outcome.root_run_id if graph_outcome else None),
                    expected_worker_id=self._worker_id(),
                )
                return str(terminal_result.status)
            if final_status in DURABLE_WAIT_STATUSES and final_status != "awaiting_approval":
                _persist_durable_wait_state(
                    bl=self._bl,
                    expected_worker_id=self._worker_id(),
                    job_id=request.job_id,
                    project_id=project_id,
                    selected_workflow_id=request.workflow_id or None,
                    status=final_status,
                    pending_interrupt_state=(
                        graph_outcome.interrupt_state if graph_outcome else None
                    ),
                )
                if final_status == "awaiting_child" and not request.test_mode:
                    _catch_up_child_approval(
                        _LifecycleReactionStore(self._notification_store_provider(), self._bl),
                        job_id=request.job_id,
                    )
                return final_status
            if final_status == "awaiting_approval":
                _persist_awaiting_approval_state(
                    bl=self._bl,
                    expected_worker_id=self._worker_id(),
                    job_id=request.job_id,
                    project_id=project_id,
                    selected_workflow_id=request.workflow_id or None,
                    result=result,
                    pending_interrupt_state=(
                        graph_outcome.interrupt_state if graph_outcome else None
                    ),
                )
                return final_status
            error_context = terminal_failure_context(
                result,
                observed=(graph_outcome.terminal_error_context if graph_outcome else None),
            )
            fence_unavailable = isinstance(
                graph_outcome.error if graph_outcome else None, InvokeCancellationFenceUnavailable
            )
            terminal_result = _terminal_persistence_service(self._bl).complete_failure(
                job_id=request.job_id,
                status=final_status,
                error_message=_terminal_error(result),
                # Only a real code. Falling back to the run status wrote
                # code="failed", which says nothing the status field did
                # not already say and displaced the projection's own
                # fallback ladder.
                error_code=str(
                    result.get("error_code") or error_context.get("error_code") or ""
                ).strip()
                or None,
                category=(
                    "transient"
                    if fence_unavailable
                    else "cancellation"
                    if final_status == "cancelled"
                    else "execution"
                ),
                retryable=fence_unavailable or final_status == "retryable_interrupted_step",
                final_report=result.get("final_report"),
                root_run_id=(graph_outcome.root_run_id if graph_outcome else None),
                step_id=str(error_context.get("step_id") or "").strip() or None,
                step_execution_id=str(error_context.get("step_execution_id") or "").strip() or None,
                group_id=str(error_context.get("group_id") or "").strip() or None,
                occurrence=error_context.get("occurrence"),
                selector=str(error_context.get("selector") or "").strip() or None,
                diagnostic_kind=str(error_context.get("diagnostic_kind") or "").strip() or None,
                diagnostic_path=str(error_context.get("path") or "").strip() or None,
                correlation_id=str(error_context.get("correlation_id") or "").strip() or None,
                expected_worker_id=self._worker_id(),
            )
            return str(terminal_result.status)
        except TerminalPersistenceConflict as exc:
            logger.info(
                "job_id=%s terminal persistence was superseded by lifecycle state: %s",
                request.job_id,
                exc,
            )
            return final_status
        except LookupError as exc:
            if str(exc) != f"Job {request.job_id} does not exist":
                raise
            logger.info(
                "job_id=%s terminal persistence was superseded by lifecycle state: %s",
                request.job_id,
                exc,
            )
            return final_status
        except Exception:
            if _requires_canonical_terminal_persistence(self._bl):
                raise
            return None

    def sync_provider(
        self,
        job_id: str,
        *,
        final_status: str,
        repository: TaskExecutionRepository,
    ) -> None:
        del repository
        from core.application.extensions.provider_completion import sync_provider_completion_status

        try:
            sync_provider_completion_status(job_id, final_status, self._bl)
        except Exception as exc:
            self._log_provider_error(job_id, final_status, exc)
            raise

    def notify(
        self,
        notification: TaskNotification,
        *,
        repository: TaskExecutionRepository,
    ) -> None:
        del repository
        from core.application.extensions.external_effects import external_effect_identity_hmac
        from core.application.notifications.dispatch import dispatch_project_notification_event

        event_id = external_effect_identity_hmac(
            purpose="notification-intent",
            value={
                "project_id": notification.project_id,
                "event_type": notification.event_type,
                "workflow_id": notification.workflow_id,
                "job_id": notification.job_id,
                "approval_id": str(notification.payload.get("approval_id") or "").strip() or None,
            },
        )
        if notification.dispatch_routes:
            dispatch_project_notification_event(
                self._notification_store_provider(),
                project_id=notification.project_id,
                event_type=notification.event_type,
                severity=notification.severity,
                workflow_id=notification.workflow_id,
                job_id=notification.job_id,
                payload=dict(notification.payload),
                event_id=event_id,
                correlation_id=event_id,
            )
        _run_lifecycle_reactions(
            _LifecycleReactionStore(self._notification_store_provider(), self._bl),
            event_type=notification.event_type,
            project_id=notification.project_id,
            workflow_id=notification.workflow_id,
            job_id=notification.job_id,
            event_id=event_id,
            payload=dict(notification.payload),
        )

    def _log_provider_error(
        self,
        job_id: str,
        final_status: str,
        error: Exception,
    ) -> None:
        log_error = getattr(self._bl, "log_job_error", None)
        if not callable(log_error):
            return
        try:
            log_error(
                job_id,
                source="provider_runtime",
                category="integration",
                provider_name="external_provider",
                error_message=str(error),
                context_jsonb={
                    "job_id": job_id,
                    "final_status": final_status,
                    "side_effect": "sync_provider_completion_status",
                },
            )
        except Exception as exc:
            logger.debug("Could not persist provider status sync error: %s", exc)


def _root_run_id(callback: Any) -> str | None:
    value = getattr(callback, "_root_run_id", None) if callback else None
    return str(value) if value is not None else None


def _terminal_error_context(callback: object | None) -> dict[str, Any]:
    getter = getattr(callback, "terminal_error_context", None)
    if not callable(getter):
        return {}
    try:
        context = getter()
    except Exception:
        return {}
    return dict(context) if isinstance(context, Mapping) else {}


def _terminal_error(result: Mapping[str, Any]) -> str | None:
    return str(result.get("error") or "").strip() or None


def _public_approval_gate_from_state(*, job: dict[str, Any], state: Any) -> Any:
    from core.application.jobs.workflow_run_projection import (
        _safe_approval,
        approval_notification_payload_from_state,
    )

    payload = approval_notification_payload_from_state(state)
    if not payload:
        return None
    return _safe_approval(
        job=job,
        status="awaiting_approval",
        gate=payload,
        previous=None,
        now=datetime.now(UTC),
    )


def _public_approval_gate_from_result(
    *, job: dict[str, Any], result: Mapping[str, Any] | None
) -> Any:
    from core.application.jobs.workflow_run_projection import _safe_approval

    state_gate = dict(result or {})
    if not state_gate.get("message"):
        state_gate["message"] = state_gate.get("approval_context") or state_gate.get("final_report")
    return _safe_approval(
        job=job,
        status="awaiting_approval",
        gate=state_gate,
        previous=None,
        now=datetime.now(UTC),
    )


def _merge_workflow_wait(
    bl: Any, *, job_id: str, job_row: Mapping[str, Any], state: Any
) -> dict[str, Any] | None:
    """Mirror the checkpoint's recorded wait into ``jobs.meta.workflow_wait``."""
    wait = durable_wait_from_state(state)
    if wait is None:
        return None
    projected = workflow_wait_meta(wait, thread_id=str(job_row.get("thread_id") or ""))
    bl.merge_job_meta(job_id, {"workflow_wait": projected})
    return projected


def _persist_durable_wait_state(
    *,
    bl: Any,
    expected_worker_id: str,
    job_id: str,
    project_id: str | None,
    selected_workflow_id: str | None,
    status: str,
    pending_interrupt_state: Any,
) -> None:
    """Park a run on a timer or child wait: status, ``workflow_wait``, progress."""
    job_row = bl.get_job(job_id) if hasattr(bl, "get_job") else {}
    job_row = job_row if isinstance(job_row, dict) else {}
    from core.application.jobs.workflow_pause_persistence import job_pause_transaction

    with job_pause_transaction(bl, job_id=job_id, expected_worker_id=expected_worker_id):
        # The wait identity lands before the status: the due-timer scan selects by
        # status and reads ``workflow_wait``, so it never sees one without the other.
        _merge_workflow_wait(bl, job_id=job_id, job_row=job_row, state=pending_interrupt_state)
        bl.update_job(job_id, status=status)
        if not job_row:
            return
        bl.record_public_progress_transition(
            job_id=job_id,
            project_id=str(job_row.get("project_id") or project_id or ""),
            workflow_id=str(
                job_row.get("selected_workflow")
                or job_row.get("selected_workflow_key")
                or selected_workflow_id
                or "unknown"
            ),
            job_status=status,
            occurred_at=datetime.now(UTC),
            queued_at=job_row.get("queued_at"),
            started_at=job_row.get("started_at"),
        )


def _persist_awaiting_approval_state(
    *,
    bl: Any,
    expected_worker_id: str,
    job_id: str,
    project_id: str | None,
    selected_workflow_id: str | None,
    pending_interrupt_state: Any,
    result: Mapping[str, Any] | None = None,
) -> None:
    from .worker_common import _to_json_obj

    job_row = bl.get_job(job_id) if hasattr(bl, "get_job") else {}
    from core.application.jobs.workflow_pause_persistence import job_pause_transaction

    with job_pause_transaction(bl, job_id=job_id, expected_worker_id=expected_worker_id):
        bl.update_job(job_id, status="awaiting_approval")
        if not isinstance(job_row, dict):
            return
        _merge_workflow_wait(bl, job_id=job_id, job_row=job_row, state=pending_interrupt_state)
        approval = _public_approval_gate_from_state(
            job=job_row,
            state=pending_interrupt_state,
        )
        meta = _to_json_obj(job_row.get("meta"))
        if approval is None and meta.get("workflow_test_run"):
            approval = _public_approval_gate_from_result(
                job=job_row,
                result=result,
            )
        if approval is not None and meta.get("workflow_test_run"):
            approval_meta = {"state_approval_id": approval.approval_id}
            if approval.message:
                approval_meta["approval_context"] = approval.message
            bl.merge_job_meta(job_id, approval_meta)
        bl.record_public_progress_transition(
            job_id=job_id,
            project_id=str(job_row.get("project_id") or project_id or ""),
            workflow_id=str(
                job_row.get("selected_workflow")
                or job_row.get("selected_workflow_key")
                or selected_workflow_id
                or "unknown"
            ),
            job_status="awaiting_approval",
            occurred_at=datetime.now(UTC),
            queued_at=job_row.get("queued_at"),
            started_at=job_row.get("started_at"),
            approval=approval,
        )


@lru_cache(maxsize=1)
def _artifact_plane() -> Any:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import create_storage_adapter
    from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

    return build_artifact_storage(
        environ=dict(_runtime_env),
        create_storage_adapter=create_storage_adapter,
        artifact_plane_factory=ArtifactDatasetPlane,
    )


def _resolve_artifact(artifact_id: str) -> dict[str, Any] | None:
    artifact = _artifact_plane().get_artifact(artifact_id)
    return artifact if isinstance(artifact, dict) else None


def _terminal_persistence_service(bl: Any) -> Any:
    from core.application.jobs.terminal_persistence import TerminalPersistenceService

    return TerminalPersistenceService(bl, resolve_artifact=_resolve_artifact)


def _requires_canonical_terminal_persistence(bl: Any) -> bool:
    return all(
        hasattr(bl, method_name)
        for method_name in (
            "merge_job_meta",
            "record_public_progress_transition",
            "finish_job",
        )
    )
