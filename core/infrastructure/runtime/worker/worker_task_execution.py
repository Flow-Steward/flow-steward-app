"""Infrastructure composition bridge for application-owned worker execution policy."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from core.application.jobs.task_execution_service import TaskExecutionService
from core.application.jobs.workflow_resume_execution_service import WorkflowResumeExecutionService
from core.application.orchestration.child_execution_runtime import ChildExecutionRuntime
from core.infrastructure.db.worker import (
    JobExecutionRepositoryAdapter,
    JobExecutionRepositoryDependencies,
    WorkflowResumeRepositoryAdapter,
)

from .worker_bootstrap import _worker_id
from .worker_delivery import (
    _extract_retryable_interrupted_step_snapshot,
    _mark_queue_delivery_terminal_job,
    _mark_retryable_interrupted_step,
    _persist_queue_delivery_meta,
)
from .worker_execution_adapters import (
    WorkerGraphExecutionAdapter,
    WorkerRuntimeModeAdapter,
    WorkerTerminalEffectsAdapter,
    _artifact_plane,
)

_WORKFLOW_RESUME_OUTBOX_BATCH_SIZE = 100
_workflow_resume_outbox_logger: Any | None = None
_workflow_resume_outbox_logger_lock = threading.Lock()


@dataclass(frozen=True)
class WorkerExecutionDependencies:
    """Immutable snapshot of worker collaborators used for one delivery."""

    worker_id: Callable[[], str]
    persist_queue_delivery_meta: Callable[[Any, str, dict[str, Any] | None], None]
    mark_queue_delivery_terminal_job: Callable[..., None]
    extract_retryable_interrupted_step_snapshot: Callable[
        [Any, dict[str, Any]], dict[str, Any] | None
    ]
    mark_retryable_interrupted_step: Callable[
        [Any, str, dict[str, Any], dict[str, Any] | None], bool
    ]
    job_status_is_cancelled: Callable[[object, object], bool]


def compose_worker_execution_dependencies(
    *,
    worker_id: Callable[[], str] = _worker_id,
    persist_queue_delivery_meta: Callable[
        [Any, str, dict[str, Any] | None], None
    ] = _persist_queue_delivery_meta,
    mark_queue_delivery_terminal_job: Callable[..., None] = _mark_queue_delivery_terminal_job,
    extract_retryable_interrupted_step_snapshot: Callable[
        [Any, dict[str, Any]], dict[str, Any] | None
    ] = _extract_retryable_interrupted_step_snapshot,
    mark_retryable_interrupted_step: Callable[
        [Any, str, dict[str, Any], dict[str, Any] | None], bool
    ] = _mark_retryable_interrupted_step,
    job_status_is_cancelled: Callable[[object, object], bool] | None = None,
) -> WorkerExecutionDependencies:
    return WorkerExecutionDependencies(
        worker_id=worker_id,
        persist_queue_delivery_meta=persist_queue_delivery_meta,
        mark_queue_delivery_terminal_job=mark_queue_delivery_terminal_job,
        extract_retryable_interrupted_step_snapshot=extract_retryable_interrupted_step_snapshot,
        mark_retryable_interrupted_step=mark_retryable_interrupted_step,
        job_status_is_cancelled=job_status_is_cancelled or _job_status_is_cancelled,
    )


def execute_task(
    msg: Mapping[str, Any] | None,
    *,
    dependencies: WorkerExecutionDependencies | None = None,
) -> dict[str, object]:
    deps = dependencies or compose_worker_execution_dependencies(
        worker_id=_worker_id,
        persist_queue_delivery_meta=_persist_queue_delivery_meta,
        mark_queue_delivery_terminal_job=_mark_queue_delivery_terminal_job,
        extract_retryable_interrupted_step_snapshot=_extract_retryable_interrupted_step_snapshot,
        mark_retryable_interrupted_step=_mark_retryable_interrupted_step,
        job_status_is_cancelled=_job_status_is_cancelled,
    )
    message = dict(msg or {}) if isinstance(msg, Mapping) else {}
    config = _runtime_config()
    bl = _build_platform_logger(config)
    notification_store_provider = _notification_store_provider(config)
    if _is_workflow_resume(message):
        return _workflow_resume_service(
            bl=bl,
            dependencies=deps,
            notification_store_provider=notification_store_provider,
        ).execute(message)
    if _clean_message_type(message):
        # A task message has no type. Any other type — an ``approval_resume``
        # from before the workflow resume contract among them — is not an
        # intent this worker accepts.
        return {"action": "dead_letter", "reason": "unsupported_message_type"}
    return _task_service(
        bl=bl,
        config=config,
        dependencies=deps,
        notification_store_provider=notification_store_provider,
    ).execute(message)


def dispatch_pending_workflow_resumes(
    *,
    bl: Any | None = None,
    enqueue_workflow_resume: Any | None = None,
    batch_size: int = _WORKFLOW_RESUME_OUTBOX_BATCH_SIZE,
) -> int:
    if bl is None:
        bl = _default_workflow_resume_outbox_logger()
    if bl is None:
        return 0
    if enqueue_workflow_resume is None:
        from core.infrastructure.runtime.worker.queue_publisher import (
            enqueue_workflow_resume as publisher,
        )
    else:
        publisher = enqueue_workflow_resume
    dependencies = compose_worker_execution_dependencies()
    return _workflow_resume_service(bl=bl, dependencies=dependencies).dispatch_pending(
        publisher=publisher,
        batch_size=batch_size,
    )


def _task_service(
    *,
    bl: Any,
    config: Mapping[str, Any],
    dependencies: WorkerExecutionDependencies,
    notification_store_provider: Callable[[], Any] | None = None,
) -> TaskExecutionService:
    repository = None
    if bl is not None:
        repository = JobExecutionRepositoryAdapter(
            bl,
            JobExecutionRepositoryDependencies(
                persist_meta=dependencies.persist_queue_delivery_meta,
                mark_delivery_terminal=dependencies.mark_queue_delivery_terminal_job,
                interrupted_step_snapshot=dependencies.extract_retryable_interrupted_step_snapshot,
                mark_retryable_interrupted_step=dependencies.mark_retryable_interrupted_step,
                job_status_is_cancelled=dependencies.job_status_is_cancelled,
            ),
        )
    child_execution_runtime = _child_execution_runtime(dependencies)
    return TaskExecutionService(
        repository=repository,
        graph=WorkerGraphExecutionAdapter(
            config=config,
            bl=bl,
            child_execution_runtime=child_execution_runtime,
            notification_store_provider=notification_store_provider,
        ),
        terminal=WorkerTerminalEffectsAdapter(
            bl=bl,
            notification_store_provider=notification_store_provider,
            worker_id=dependencies.worker_id,
        ),
        runtime_mode=WorkerRuntimeModeAdapter(),
        worker_id=dependencies.worker_id,
    )


def _workflow_resume_service(
    *,
    bl: Any,
    dependencies: WorkerExecutionDependencies,
    notification_store_provider: Callable[[], Any] | None = None,
) -> WorkflowResumeExecutionService:
    from .worker_resume_adapters import (
        WorkerWorkflowResumeGraphAdapter,
        WorkerWorkflowResumeLeaseAdapter,
        WorkerWorkflowResumeTerminalAdapter,
    )

    repository = WorkflowResumeRepositoryAdapter(bl)
    return WorkflowResumeExecutionService(
        repository=repository,
        graph=WorkerWorkflowResumeGraphAdapter(
            bl=bl,
            child_execution_runtime=_child_execution_runtime(dependencies),
            notification_store_provider=notification_store_provider,
        ),
        lease=WorkerWorkflowResumeLeaseAdapter(
            repository=repository,
            repository_factory=WorkflowResumeRepositoryAdapter,
            bl=bl,
        ),
        terminal=WorkerWorkflowResumeTerminalAdapter(
            bl=bl,
            notification_store_provider=notification_store_provider,
            worker_id=dependencies.worker_id,
        ),
        worker_id=dependencies.worker_id,
        token_factory=lambda: uuid4().hex,
    )


def _child_execution_runtime(
    dependencies: WorkerExecutionDependencies,
) -> ChildExecutionRuntime:
    return ChildExecutionRuntime(
        worker_id=dependencies.worker_id(),
        execute_inline=lambda message: execute_task(message, dependencies=dependencies),
        artifact_plane_provider=_artifact_plane,
    )


def _default_workflow_resume_outbox_logger() -> Any:
    global _workflow_resume_outbox_logger
    config = _runtime_config()
    dsn = str(config.get("fs_observability_dsn") or config.get("fs_db_dsn") or "").strip()
    if not dsn:
        return None
    with _workflow_resume_outbox_logger_lock:
        if _workflow_resume_outbox_logger is None:
            _workflow_resume_outbox_logger = _platform_logger_factory()(dsn)
        return _workflow_resume_outbox_logger


def _runtime_config() -> Mapping[str, Any]:
    from core.infrastructure.config.runtime_config import CONFIG

    return CONFIG


def _platform_logger_factory() -> Any:
    from core.infrastructure.db.platform_stores import PlatformWorkerStore

    return PlatformWorkerStore


def _platform_notification_store_factory() -> Any:
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore

    return PlatformIntegrationStore


def _notification_store_provider(config: Mapping[str, Any]) -> Callable[[], Any]:
    dsn = str(config.get("fs_observability_dsn") or config.get("fs_db_dsn") or "").strip()
    store: Any | None = None

    def resolve() -> Any:
        nonlocal store
        if store is None and dsn:
            store = _platform_notification_store_factory().get_shared_instance(dsn)
        return store

    return resolve


def _build_platform_logger(config: Mapping[str, Any]) -> Any:
    dsn = config.get("fs_observability_dsn", "") or config.get("fs_db_dsn", "")
    return _platform_logger_factory()(dsn) if dsn else None


def _job_status_is_cancelled(bl: object, job_id: object) -> bool:
    if not bl or not job_id:
        return False
    getter = getattr(bl, "get_job", None)
    if not callable(getter):
        return False
    try:
        row = getter(str(job_id))
    except Exception:
        return False
    return isinstance(row, Mapping) and str(row.get("status") or "").strip().lower() == "cancelled"


def _clean_message_type(message: Mapping[str, Any]) -> str:
    return str(message.get("message_type") or "").strip().lower()


def _is_workflow_resume(message: Mapping[str, Any]) -> bool:
    return _clean_message_type(message) == "workflow_resume"
