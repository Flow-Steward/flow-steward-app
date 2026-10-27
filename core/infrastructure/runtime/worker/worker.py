"""
Flow Steward Worker — standalone process that consumes the RabbitMQ task queue
and runs the LangGraph graph for each task.

Usage:
  # Generic worker — consumes ready signals and dynamically leases projects (recommended)
  python -m core.infrastructure.runtime.worker.worker

  # Dedicated worker for a single project (optional/debug)
  python -m core.infrastructure.runtime.worker.worker --project-id 1

  # Multiple dedicated workers (run these as separate containers or processes):
  python -m core.infrastructure.runtime.worker.worker --project-id 1  &
  python -m core.infrastructure.runtime.worker.worker --project-id 2  &

Design:
  - Project queues hold real jobs for each project.
  - Workers consume ready hints, then acquire short-lived
    DB project lease slots and drain one project queue at a time.
  - Same-project top-level parallelism is limited by project policy
    `concurrency_cap` (default: 1).
  - Multiple generic workers provide horizontal scale, failover, and the actual
    process capacity needed to realize cross-project and same-project parallelism.
  - The --project-id flag scopes the worker to one explicit project queue.

Recovery:
  - If the worker crashes mid-task, lease heartbeat stops and another worker can
    take over after lease expiry; failed message deliveries are requeued.
  - The worker reconnects to RabbitMQ automatically on disconnect.
  - LangGraph checkpointing means interrupted approval-gated tasks can be
    resumed later via /api/approvals/{thread_id} — they do NOT go through the queue again.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Callable, Collection

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.monitoring.heartbeat_runtime import (
    CompositeConsumerSink,
    MonitoringRuntimeHandle,
)
from core.infrastructure.runtime.worker.queue import consume_all_projects, consume_project_queue

from . import worker_schema_compat as _schema_compat_runtime
from . import worker_task_execution as _task_execution_runtime
from .consumer_observer import ConsumerStateSink, create_observation_sink, run_observation_heartbeat
from .terminal_curation import start_terminal_curation_loop
from .worker_bootstrap import (
    _get_all_project_ids,
    _repair_canonical_guardrails_for_worker,
    _worker_id,
    _worker_process_metrics_lines,
)
from .worker_common import (
    _RETRYABLE_INTERRUPTED_STEP_STATUS,
    _to_json_obj,
    _truncate_text,
)
from .worker_delivery import (
    _delivery_action,
    _extract_retryable_interrupted_step_snapshot,
    _mark_queue_delivery_terminal_job,
    _mark_retryable_interrupted_step,
    _persist_queue_delivery_meta,
    _queue_delivery_context,
    _queue_name_for_delivery,
)
from .worker_inbound_polling import (
    _background_poll_actions,
    _binding_poll_due,
    _parse_iso_datetime,
    _start_extension_inbound_poll_loop,
)
from .worker_schema_compat import (
    WorkerSchemaDependencies,
    _is_transient_schema_compat_error,
    _iter_exception_chain,
    _run_schema_compatibility_check,
    _schema_compat_retry_delay,
    _schema_compat_retry_settings,
)
from .workflow_knowledge_sync import start_workflow_knowledge_sync_loop

logger = logging.getLogger("platform.worker")
_shutdown_requested = threading.Event()


def _maintenance_inactive() -> bool:
    return False


__all__ = [
    "_RETRYABLE_INTERRUPTED_STEP_STATUS",
    "_background_poll_actions",
    "_binding_poll_due",
    "_delivery_action",
    "_enforce_schema_compatibility",
    "_execute_task",
    "_extract_retryable_interrupted_step_snapshot",
    "_get_all_project_ids",
    "_is_transient_schema_compat_error",
    "_iter_exception_chain",
    "_job_status_is_cancelled",
    "_mark_queue_delivery_terminal_job",
    "_mark_retryable_interrupted_step",
    "_parse_iso_datetime",
    "_persist_queue_delivery_meta",
    "_queue_delivery_context",
    "_queue_name_for_delivery",
    "_repair_canonical_guardrails_for_worker",
    "_run_schema_compatibility_check",
    "_schema_compat_retry_delay",
    "_schema_compat_retry_settings",
    "_sigterm_handler",
    "_start_extension_inbound_poll_loop",
    "_to_json_obj",
    "_truncate_text",
    "_worker_id",
    "_worker_process_metrics_lines",
    "main",
    "start_terminal_curation_loop",
    "start_workflow_knowledge_sync_loop",
]


def _sigterm_handler(_signum, _frame):
    """Handle SIGTERM (Docker stop / manual kill) gracefully."""
    logger.info("Received SIGTERM; shutting down gracefully.")
    _shutdown_requested.set()


def _enforce_schema_compatibility() -> None:
    """Fail fast when DB schema is older than worker runtime expectations."""
    return _schema_compat_runtime._enforce_schema_compatibility(
        WorkerSchemaDependencies(
            logger=logger,
            retry_settings=_schema_compat_retry_settings,
            run_check=_run_schema_compatibility_check,
            retry_delay=_schema_compat_retry_delay,
        )
    )


def _job_status_is_cancelled(bl: object, job_id: object) -> bool:
    return _task_execution_runtime._job_status_is_cancelled(bl, job_id)


def _execute_task(msg: dict) -> dict[str, object]:
    dependencies = _task_execution_runtime.compose_worker_execution_dependencies(
        worker_id=_worker_id,
        persist_queue_delivery_meta=_persist_queue_delivery_meta,
        mark_queue_delivery_terminal_job=_mark_queue_delivery_terminal_job,
        extract_retryable_interrupted_step_snapshot=_extract_retryable_interrupted_step_snapshot,
        mark_retryable_interrupted_step=_mark_retryable_interrupted_step,
        job_status_is_cancelled=_job_status_is_cancelled,
    )
    return _task_execution_runtime.execute_task(msg, dependencies=dependencies)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(
    *,
    ensure_guardrails_seeded: Callable[..., dict] | None = None,
    process_extension_response_envelope: Callable[..., object] | None = None,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
    curate_terminal_job: Callable[..., list[str]] | None = None,
    job_terminal_statuses: Collection[str] = (),
    start_runtime_reporting: Callable[
        [str, ConsumerStateSink | None],
        tuple[ConsumerStateSink | None, MonitoringRuntimeHandle | None],
    ]
    | None = None,
):
    _shutdown_requested.clear()
    signal.signal(signal.SIGTERM, _sigterm_handler)
    parser = argparse.ArgumentParser(description="Flow Steward Task Queue Worker")
    parser.add_argument(
        "--project-id",
        default=None,
        metavar="ID",
        help=(
            "Run as a dedicated worker for this project ID. "
            "Tasks for this project are processed serially, one at a time. "
            "If not specified, the worker handles ALL projects (development fallback)."
        ),
    )
    parser.add_argument(
        "--amqp-url",
        type=str,
        default=None,
        metavar="URL",
        help="RabbitMQ AMQP URL. Defaults to RABBITMQ_URL env var.",
    )
    args = parser.parse_args()
    metrics_server = None
    inbound_poll_handle: tuple[threading.Event, threading.Thread] | None = None
    terminal_curation_handle: tuple[threading.Event, threading.Thread] | None = None
    workflow_knowledge_sync_handle: tuple[threading.Event, threading.Thread] | None = None
    observation_heartbeat_handle: tuple[threading.Event, threading.Thread] | None = None

    try:
        from core.infrastructure.config.runtime_config import CONFIG
        from core.infrastructure.observability.process_metrics_runtime import (
            start_process_metrics_server,
        )
        from core.infrastructure.observability.runtime_logging import configure_runtime_logging
        from core.infrastructure.observability.sentry import init_sentry

        configure_runtime_logging(component="worker", config=CONFIG)
        init_sentry(config=CONFIG, component="worker")
        metrics_server = start_process_metrics_server(
            component="worker",
            config=CONFIG,
            logger=logger,
            port_key="worker_metrics_port",
            port_env_name="FS_WORKER_METRICS_PORT",
            extra_metrics_fn=lambda: _worker_process_metrics_lines(project_id=args.project_id),
        )
    except Exception as exc:
        logger.warning("Worker bootstrap failed (non-fatal): %s", exc)

    _enforce_schema_compatibility()
    _repair_canonical_guardrails_for_worker(
        ensure_guardrails_seeded=ensure_guardrails_seeded,
    )

    wid = _worker_id()
    _runtime_env["FS_WORKER_ID"] = wid
    observation_sink: ConsumerStateSink | None = create_observation_sink(
        _runtime_env,
        role=str(_runtime_env.get("FS_COMPACT_ROLE") or "worker"),
        worker_incarnation=wid,
    )
    reporting_handle = None
    if start_runtime_reporting is not None:
        try:
            observation_sink, reporting_handle = start_runtime_reporting(wid, observation_sink)
        except Exception:
            logger.warning("Worker reporting unavailable; verify monitoring configuration")
    if observation_sink is not None:
        observation_interval = (
            observation_sink.refresh_interval_seconds
            if isinstance(observation_sink, CompositeConsumerSink)
            else 60.0
        )
        if "FS_COMPACT_OBSERVATION_FD" in _runtime_env:
            observation_interval = float(
                _runtime_env.get("FS_COMPACT_HEALTH_INTERVAL_SECONDS") or "2"
            )
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=run_observation_heartbeat,
            kwargs={
                "sink": observation_sink,
                "stop": heartbeat_stop,
                "interval_seconds": observation_interval,
            },
            daemon=True,
            name="compact-observation-heartbeat",
        )
        heartbeat_thread.start()
        observation_heartbeat_handle = (heartbeat_stop, heartbeat_thread)
    inbound_poll_handle = _start_extension_inbound_poll_loop(
        worker_id=wid,
        project_id=args.project_id,
        process_extension_response_envelope=process_extension_response_envelope,
    )
    terminal_curation_handle = start_terminal_curation_loop(
        curate_terminal_job=curate_terminal_job,
        worker_id=wid,
    )
    workflow_knowledge_sync_handle = start_workflow_knowledge_sync_loop(worker_id=wid)

    try:
        if args.project_id is not None:
            logger.info("[%s] Dedicated worker for project_id=%s", wid, args.project_id)
            consume_project_queue(
                project_id=args.project_id,
                callback=_execute_task,
                amqp_url=args.amqp_url,
                worker_id=wid,
                recover_outbox=lambda: _task_execution_runtime.dispatch_pending_workflow_resumes(),
                stop_event=_shutdown_requested,
                maintenance_active=maintenance_active,
                terminal_statuses=job_terminal_statuses,
            )
        else:
            logger.info("[%s] Starting ready-queue scheduling for all projects", wid)
            consume_all_projects(
                project_ids=[],
                callback=_execute_task,
                amqp_url=args.amqp_url,
                worker_id=wid,
                recover_outbox=lambda: _task_execution_runtime.dispatch_pending_workflow_resumes(),
                stop_event=_shutdown_requested,
                observation_sink=observation_sink,
                maintenance_active=maintenance_active,
                terminal_statuses=job_terminal_statuses,
            )
    finally:
        if observation_heartbeat_handle is not None:
            stop_event, thread = observation_heartbeat_handle
            stop_event.set()
            thread.join(timeout=2)
        try:
            if observation_sink is not None:
                observation_sink.close()
        finally:
            if reporting_handle is not None:
                reporting_handle.close()
        if workflow_knowledge_sync_handle is not None:
            stop_event, thread = workflow_knowledge_sync_handle
            stop_event.set()
            thread.join(timeout=2)
        if terminal_curation_handle is not None:
            stop_event, thread = terminal_curation_handle
            stop_event.set()
            thread.join(timeout=2)
        if inbound_poll_handle is not None:
            stop_event, thread = inbound_poll_handle
            stop_event.set()
            thread.join(timeout=2)
        if metrics_server is not None:
            metrics_server.close()


if __name__ == "__main__":
    main()
