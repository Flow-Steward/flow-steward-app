"""Project queue consumers, lease heartbeats and durable backlog recovery."""

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Collection
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.worker.consumer_observer import ConsumerStateSink
from core.infrastructure.runtime.worker.queue_broker import (
    _DEFAULT_URL,
    _READY_QUEUE,
    _connect,
    _declare_queue,
    _declare_ready_queue,
    _project_queue_depth,
    _publish_ready_hint,
    _queue_name,
)
from core.infrastructure.runtime.worker.queue_lease_store import (
    ProjectLeaseStore as _ProjectLeaseStore,
)
from core.infrastructure.runtime.worker.queue_lease_store import (
    lease_worker_id as _lease_worker_id,
)
from core.infrastructure.runtime.worker.queue_lease_store import (
    normalize_entity_id as _normalize_entity_id,
)
from core.infrastructure.runtime.worker.queue_messages import (
    _decode_ready_event,
)
from core.infrastructure.runtime.worker.queue_pika_compat import pika

logger = logging.getLogger(__name__)

ProjectQueueCallbackResult = dict[str, object] | str | None

ProjectQueueCallback = Callable[[dict[str, Any]], ProjectQueueCallbackResult]


def _maintenance_inactive() -> bool:
    return False


class _ProjectMessageFailed(Exception):
    """Raised when a project queue message was nacked for retry."""


def _is_missing_project_execution_slot_reference_error(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    if "project_execution_slots_project_id_fkey" not in message:
        return False
    pgcode = str(getattr(exc, "pgcode", "") or "").strip()
    return not pgcode or pgcode == "23503"


_LEASE_HEARTBEAT_SECONDS = max(5, int(_runtime_env.get("FS_PROJECT_LEASE_HEARTBEAT_SECONDS", "15")))

_READY_RECOVERY_IDLE_SECONDS = max(
    5,
    int(_runtime_env.get("FS_READY_RECOVERY_IDLE_SECONDS", "30")),
)


# Fairness cap: how many job messages a single ready-hint drains before yielding
# the worker back to the ready queue. Bounding the per-hint drain prevents one
# busy project from starving other projects' pickup when a single worker handles
# many projects (head-of-line blocking). The residual backlog is re-hinted so the
# project is rescheduled round-robin behind other waiting projects. 0/negative ->
# unbounded (legacy full-drain behavior).
_READY_DRAIN_MAX_MESSAGES = max(
    0,
    int(_runtime_env.get("FS_READY_DRAIN_MAX_MESSAGES", "1")),
)


def _ready_recovery_project_ids(
    lease_store: _ProjectLeaseStore,
    allowed_project_ids: set[str] | None,
) -> list[str]:
    if allowed_project_ids:
        return sorted(allowed_project_ids)
    try:
        project_ids = lease_store.list_pending_project_ids()
    except Exception as e:
        logger.debug("ready recovery project discovery failed: %s", e)
        return []
    if not isinstance(project_ids, list):
        return []
    return sorted(
        {normalized for project_id in project_ids if (normalized := str(project_id or "").strip())}
    )


def _recover_ready_hints_for_project_backlog(
    conn: Any,
    *,
    lease_store: _ProjectLeaseStore,
    allowed_project_ids: set[str] | None,
    worker_id: str,
) -> int:
    recovered = 0
    for project_id in _ready_recovery_project_ids(lease_store, allowed_project_ids):
        channel = None
        try:
            channel = conn.channel()
            backlog_depth = _project_queue_depth(channel, project_id)
            if backlog_depth <= 0:
                continue
            _publish_ready_hint(channel, project_id=project_id)
            recovered += 1
            logger.info(
                "[%s] Recovered ready hint project=%s backlog=%d",
                worker_id,
                project_id,
                backlog_depth,
            )
        except Exception as e:
            logger.debug("[%s] Ready recovery skipped project=%s: %s", worker_id, project_id, e)
        finally:
            if channel is not None:
                with contextlib.suppress(Exception):
                    channel.close()
    return recovered


def _consume_ready_events_with_idle_recovery(
    conn: Any,
    *,
    recover_ready_hints: Callable[[], int],
    recover_outbox: Callable[[], int] | None = None,
    idle_recovery_seconds: int = _READY_RECOVERY_IDLE_SECONDS,
    stop_event: threading.Event | None = None,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
) -> None:
    idle_recovery_seconds = max(5, int(idle_recovery_seconds or _READY_RECOVERY_IDLE_SECONDS))
    last_recovery_at = time.monotonic()
    while True:
        conn.process_data_events(time_limit=1.0)
        if stop_event is not None and stop_event.is_set():
            return
        if maintenance_active():
            return
        now = time.monotonic()
        if now - last_recovery_at < idle_recovery_seconds:
            continue
        _recover_workflow_resume_outbox(recover_outbox, worker_id="ready-idle")
        recover_ready_hints()
        last_recovery_at = now


def _recover_workflow_resume_outbox(
    recover_outbox: Callable[[], int] | None,
    *,
    worker_id: str,
) -> int:
    if recover_outbox is None:
        return 0
    try:
        recovered = int(recover_outbox() or 0)
    except Exception as exc:
        logger.debug("[%s] Workflow resume outbox recovery skipped: %s", worker_id, exc)
        return 0
    if recovered:
        logger.info("[%s] Recovered %d workflow resume outbox message(s)", worker_id, recovered)
    return recovered


def _start_lease_heartbeat(
    lease_store: _ProjectLeaseStore,
    lease: dict,
    *,
    active_job_id: Any | None = None,
    drain_status: str = "draining",
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    terminal_probe = getattr(type(lease_store), "active_job_is_terminal", None)

    try:
        updated = lease_store.heartbeat(
            lease,
            drain_status=drain_status,
            active_job_id=active_job_id,
        )
        if isinstance(updated, dict):
            lease.update(updated)
    except Exception as e:
        logger.debug(
            "Initial lease heartbeat failed for project=%s: %s",
            lease.get("project_id"),
            e,
        )

    def _loop():
        while not stop_event.wait(_LEASE_HEARTBEAT_SECONDS):
            if active_job_id is not None and callable(terminal_probe):
                try:
                    if terminal_probe(lease_store, active_job_id):
                        lease["_active_job_terminal"] = str(active_job_id)
                        logger.warning(
                            "Stopping project lease renewal because active job is terminal "
                            "project=%s job=%s",
                            lease.get("project_id"),
                            active_job_id,
                        )
                        return
                except Exception as e:
                    logger.debug(
                        "Terminal job probe failed for project=%s job=%s: %s",
                        lease.get("project_id"),
                        active_job_id,
                        e,
                    )
            try:
                updated = lease_store.heartbeat(
                    lease,
                    drain_status=drain_status,
                    active_job_id=active_job_id,
                )
                if isinstance(updated, dict):
                    lease.update(updated)
            except Exception as e:
                logger.debug(
                    "Lease heartbeat failed for project=%s: %s", lease.get("project_id"), e
                )

    thread = threading.Thread(
        target=_loop,
        daemon=True,
        name=f"lease-heartbeat-{lease.get('project_id')}",
    )
    thread.start()
    return stop_event, thread


def consume_project_queue(
    project_id: Any,
    callback: ProjectQueueCallback,
    amqp_url: str | None = None,
    worker_id: str | None = None,
    recover_outbox: Callable[[], int] | None = None,
    stop_event: threading.Event | None = None,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
    terminal_statuses: Collection[str] = (),
) -> None:
    """
    Dedicated single-project consumer with lease protection.

    Retained to support explicit --project-id workers. New deployments should
    prefer consume_all_projects()/consume_ready_projects() so workers discover
    ready projects dynamically.
    """
    project_id = _normalize_entity_id(project_id)
    url = amqp_url or _runtime_env.get("RABBITMQ_URL", _DEFAULT_URL)
    wid = _lease_worker_id(worker_id or f"worker-project-{project_id}")
    lease_store = _ProjectLeaseStore(wid, terminal_statuses=terminal_statuses)

    logger.info(
        "[%s] Starting dedicated consumer for project %s queue=%s",
        wid,
        project_id,
        _queue_name(project_id),
    )

    while stop_event is None or not stop_event.is_set():
        if maintenance_active():
            if stop_event is not None:
                stop_event.wait(1)
            else:
                time.sleep(1)
            continue
        try:
            conn = _connect(url)
            if maintenance_active():
                with contextlib.suppress(Exception):
                    conn.close()
                if stop_event is not None:
                    stop_event.wait(1)
                else:
                    time.sleep(1)
                continue
            ch = conn.channel()
            _declare_queue(ch, project_id)
            if not maintenance_active():
                _recover_workflow_resume_outbox(recover_outbox, worker_id=wid)

            while stop_event is None or not stop_event.is_set():
                if maintenance_active():
                    if stop_event is not None:
                        stop_event.wait(1)
                    else:
                        time.sleep(1)
                    continue
                try:
                    lease = lease_store.acquire(project_id)
                except Exception as exc:
                    if _is_missing_project_execution_slot_reference_error(
                        exc
                    ) and not lease_store.project_exists(project_id):
                        logger.info(
                            "[%s] Project %s no longer exists — stopping dedicated consumer",
                            wid,
                            project_id,
                        )
                        return
                    raise
                if not lease:
                    if not lease_store.project_exists(project_id):
                        logger.info(
                            "[%s] Project %s no longer exists — stopping dedicated consumer",
                            wid,
                            project_id,
                        )
                        return
                    if stop_event is not None:
                        stop_event.wait(1)
                    else:
                        time.sleep(1)
                    continue
                try:
                    drained, failed = _drain_project_queue(
                        ch,
                        project_id=project_id,
                        callback=callback,
                        worker_id=wid,
                        lease_store=lease_store,
                        lease=lease,
                        stop_event=stop_event,
                        maintenance_active=maintenance_active,
                    )
                    if drained == 0 and not failed:
                        if not maintenance_active():
                            _recover_workflow_resume_outbox(recover_outbox, worker_id=wid)
                        if stop_event is not None:
                            stop_event.wait(1)
                        else:
                            time.sleep(1)
                finally:
                    lease_store.release(lease)

        except pika.exceptions.AMQPConnectionError as e:
            logger.warning("[%s] RabbitMQ connection lost: %s — reconnecting in 5s…", wid, e)
            if stop_event is not None:
                stop_event.wait(5)
            else:
                time.sleep(5)
        except KeyboardInterrupt:
            logger.info("[%s] Shutting down (KeyboardInterrupt)", wid)
            break
        except Exception as e:
            logger.exception(
                "[%s] Unexpected error in consumer loop: %s — restarting in 10s…", wid, e
            )
            if stop_event is not None:
                stop_event.wait(10)
            else:
                time.sleep(10)


def _process_project_message(
    ch: Any,
    method: Any,
    body: bytes,
    *,
    callback: ProjectQueueCallback,
    worker_id: str,
    lease_store: _ProjectLeaseStore | None = None,
    lease: dict | None = None,
) -> None:
    try:
        msg = json.loads((body or b"{}").decode("utf-8"))
    except Exception as e:
        logger.exception(
            "[%s] Bad JSON in project queue message: %s — dead-lettering invalid payload",
            worker_id,
            e,
        )
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    if not isinstance(msg, dict):
        logger.error(
            "[%s] Non-object project queue payload received — dead-lettering invalid payload",
            worker_id,
        )
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    # RabbitMQ is authoritative for this transport-only bit.  Overwrite any
    # untrusted payload value so application delivery policy can distinguish a
    # genuine redelivery after worker loss from a concurrent duplicate message.
    msg["_queue_redelivered"] = getattr(method, "redelivered", False) is True

    hb_stop = None
    hb_thread = None
    try:
        if lease_store and isinstance(lease, dict):
            hb_stop, hb_thread = _start_lease_heartbeat(
                lease_store,
                lease,
                active_job_id=str(msg.get("job_id") or "").strip() or None,
            )
        result = callback(msg)
        action = "ack"
        if isinstance(result, dict):
            action = str(result.get("action") or "ack").strip().lower() or "ack"
        elif isinstance(result, str):
            action = result.strip().lower() or "ack"

        if action == "retry":
            logger.warning(
                "[%s] job_id=%s delivery retry requested — nacking with requeue",
                worker_id,
                msg.get("job_id"),
            )
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            raise _ProjectMessageFailed
        if action == "dead_letter":
            logger.warning(
                "[%s] job_id=%s terminal queue failure — routing to dead-letter queue",
                worker_id,
                msg.get("job_id"),
            )
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        ch.basic_ack(delivery_tag=method.delivery_tag)
    except _ProjectMessageFailed:
        raise
    except Exception as e:
        logger.exception(
            "[%s] job_id=%s failed: %s — nacking with requeue", worker_id, msg.get("job_id"), e
        )
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        raise _ProjectMessageFailed from e
    finally:
        if hb_stop is not None:
            hb_stop.set()
        if hb_thread is not None:
            hb_thread.join(timeout=1.0)


def _drain_project_queue(
    ch: Any,
    *,
    project_id: Any,
    callback: ProjectQueueCallback,
    worker_id: str,
    lease_store: _ProjectLeaseStore | None = None,
    lease: dict | None = None,
    max_messages: int | None = None,
    stop_event: threading.Event | None = None,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
) -> tuple[int, bool]:
    """Drain queued job messages for a project.

    When ``max_messages`` is a positive int, at most that many messages are
    processed before returning so the caller can yield to other projects'
    ready hints (fairness). ``None``/<=0 drains the whole queue.
    """
    drained = 0
    failed = False
    bounded = isinstance(max_messages, int) and max_messages > 0
    _declare_queue(ch, project_id)

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        if maintenance_active():
            break
        if bounded and drained >= max_messages:
            break
        method, _properties, body = ch.basic_get(queue=_queue_name(project_id), auto_ack=False)
        if method is None:
            break
        if lease_store and isinstance(lease, dict):
            try:
                updated = lease_store.heartbeat(
                    lease,
                    drain_status="draining",
                )
                if isinstance(updated, dict):
                    lease.update(updated)
            except Exception as e:
                logger.debug("[%s] Lease heartbeat before process failed: %s", worker_id, e)
        try:
            _process_project_message(
                ch,
                method,
                body,
                callback=callback,
                worker_id=worker_id,
                lease_store=lease_store,
                lease=lease,
            )
        except _ProjectMessageFailed:
            failed = True
            break
        drained += 1
        if isinstance(lease, dict) and lease.get("_active_job_terminal"):
            break
    return drained, failed


def consume_ready_projects(
    callback: ProjectQueueCallback,
    *,
    allowed_project_ids: set[Any] | None = None,
    amqp_url: str | None = None,
    worker_id: str | None = None,
    recover_outbox: Callable[[], int] | None = None,
    stop_event: threading.Event | None = None,
    observation_sink: ConsumerStateSink | None = None,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
    terminal_statuses: Collection[str] = (),
) -> None:
    """Consume ready signals and dynamically drain project queues under DB lease slots."""
    url = amqp_url or _runtime_env.get("RABBITMQ_URL", _DEFAULT_URL)
    wid = _lease_worker_id(worker_id or "worker-ready")
    lease_store = _ProjectLeaseStore(wid, terminal_statuses=terminal_statuses)
    allowed = {
        str(pid).strip() for pid in (allowed_project_ids or set()) if str(pid).strip()
    } or None

    logger.info(
        "[%s] Starting ready-queue consumer queue=%s allowlist=%s",
        wid,
        _READY_QUEUE,
        sorted(allowed) if allowed else "all",
    )

    def _record_maintenance_pause() -> None:
        if observation_sink is not None:
            observation_sink.record("maintenance_paused")

    while stop_event is None or not stop_event.is_set():
        try:
            conn = _connect(url)
            if not maintenance_active():
                _recover_workflow_resume_outbox(recover_outbox, worker_id=wid)
            if not maintenance_active():
                _recover_ready_hints_for_project_backlog(
                    conn,
                    lease_store=lease_store,
                    allowed_project_ids=allowed,
                    worker_id=wid,
                )
            ch = conn.channel()
            _declare_ready_queue(ch)
            ch.basic_qos(prefetch_count=1)
            if maintenance_active():
                _record_maintenance_pause()
                with contextlib.suppress(Exception):
                    conn.close()
                if stop_event is not None:
                    stop_event.wait(1)
                else:
                    time.sleep(1)
                continue
            if observation_sink is not None:
                observation_sink.record("registered")

            def _on_ready(ch, method, properties, body):
                if maintenance_active():
                    return
                settled = False

                def _ack_ready() -> None:
                    nonlocal settled
                    if settled:
                        return
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    settled = True

                def _requeue_ready() -> None:
                    nonlocal settled
                    if settled:
                        return
                    if stop_event is not None:
                        stop_event.wait(0.25)
                    else:
                        time.sleep(0.25)
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                    settled = True

                try:
                    event = _decode_ready_event(body)
                except Exception as e:
                    logger.exception("[%s] Invalid ready-event payload: %s", wid, e)
                    _ack_ready()
                    return

                project_id = str(event.get("project_id") or "").strip()
                if not project_id:
                    _ack_ready()
                    return
                if allowed and project_id not in allowed:
                    _ack_ready()
                    return

                lease = None
                active_observation = False
                try:
                    try:
                        lease = lease_store.acquire(project_id)
                    except Exception as exc:
                        if _is_missing_project_execution_slot_reference_error(
                            exc
                        ) and not lease_store.project_exists(project_id):
                            logger.info(
                                "[%s] Ignoring stale ready event for deleted project=%s",
                                wid,
                                project_id,
                            )
                            return
                        raise
                    if not lease:
                        _requeue_ready()
                        return

                    logger.debug(
                        "[%s] Acquired project lease project=%s",
                        wid,
                        project_id,
                    )
                    if observation_sink is not None:
                        observation_sink.record("active")
                        active_observation = True
                    drained, failed = _drain_project_queue(
                        ch,
                        project_id=project_id,
                        callback=callback,
                        worker_id=wid,
                        lease_store=lease_store,
                        lease=lease,
                        max_messages=_READY_DRAIN_MAX_MESSAGES or None,
                        stop_event=stop_event,
                        maintenance_active=maintenance_active,
                    )
                    logger.info(
                        "[%s] Project drain complete project=%s drained=%d failed=%s",
                        wid,
                        project_id,
                        drained,
                        failed,
                    )
                    if failed:
                        _requeue_ready()
                        return
                    if not lease_store.project_exists(project_id):
                        logger.info(
                            "[%s] Project deleted during drain project=%s; skipping backlog probe",
                            wid,
                            project_id,
                        )
                        return
                    backlog_depth = _project_queue_depth(ch, project_id)
                    if backlog_depth > 0:
                        _publish_ready_hint(
                            ch,
                            project_id=project_id,
                            job_id=str(event.get("job_id") or "").strip() or None,
                            task_id=str(event.get("task_id") or "").strip() or None,
                        )
                        logger.info(
                            "[%s] Re-published ready hint project=%s backlog=%d",
                            wid,
                            project_id,
                            backlog_depth,
                        )
                except Exception as e:
                    logger.exception(
                        "[%s] Ready-event processing failed for project=%s: %s", wid, project_id, e
                    )
                finally:
                    if active_observation and observation_sink is not None:
                        observation_sink.record("registered")
                    if isinstance(lease, dict):
                        lease_store.release(lease)
                    _ack_ready()

            if maintenance_active():
                _record_maintenance_pause()
                with contextlib.suppress(Exception):
                    conn.close()
                if stop_event is not None:
                    stop_event.wait(1)
                else:
                    time.sleep(1)
                continue

            ch.basic_consume(
                queue=_READY_QUEUE,
                on_message_callback=_on_ready,
                auto_ack=False,
            )

            def _recover_idle_ready_hints(conn=conn) -> int:
                return _recover_ready_hints_for_project_backlog(
                    conn,
                    lease_store=lease_store,
                    allowed_project_ids=allowed,
                    worker_id=wid,
                )

            _consume_ready_events_with_idle_recovery(
                conn,
                recover_ready_hints=_recover_idle_ready_hints,
                recover_outbox=lambda: _recover_workflow_resume_outbox(
                    recover_outbox,
                    worker_id=wid,
                ),
                stop_event=stop_event,
                maintenance_active=maintenance_active,
            )
            paused_after_pump = False
            if stop_event is not None and stop_event.is_set():
                if observation_sink is not None:
                    observation_sink.record("draining")
            elif maintenance_active():
                _record_maintenance_pause()
                paused_after_pump = True
            with contextlib.suppress(Exception):
                conn.close()
            if paused_after_pump:
                if stop_event is not None:
                    stop_event.wait(1)
                else:
                    time.sleep(1)

        except pika.exceptions.AMQPConnectionError as e:
            if observation_sink is not None:
                observation_sink.record("disconnected")
            logger.warning("[%s] Connection lost: %s — reconnecting in 5s…", wid, e)
            if stop_event is not None:
                stop_event.wait(5)
            else:
                time.sleep(5)
        except KeyboardInterrupt:
            logger.info("[%s] Shutting down", wid)
            break
        except Exception as e:
            if observation_sink is not None:
                observation_sink.record("disconnected")
            logger.exception("[%s] Unexpected error: %s — restarting in 10s…", wid, e)
            if stop_event is not None:
                stop_event.wait(10)
            else:
                time.sleep(10)


def consume_all_projects(
    project_ids: list,
    callback: ProjectQueueCallback,
    amqp_url: str | None = None,
    worker_id: str | None = None,
    recover_outbox: Callable[[], int] | None = None,
    stop_event: threading.Event | None = None,
    observation_sink: ConsumerStateSink | None = None,
    maintenance_active: Callable[[], bool] = _maintenance_inactive,
    terminal_statuses: Collection[str] = (),
) -> None:
    """
    Worker entrypoint for dynamic ready-queue consumption.

    `project_ids` is now treated as an optional allowlist; scheduling is driven
    by platform.projects.ready events plus short DB project leases.
    """
    allowlist = {str(pid).strip() for pid in (project_ids or []) if str(pid).strip()}
    consume_ready_projects(
        callback=callback,
        allowed_project_ids=allowlist or None,
        amqp_url=amqp_url,
        worker_id=worker_id,
        recover_outbox=recover_outbox,
        stop_event=stop_event,
        observation_sink=observation_sink,
        maintenance_active=maintenance_active,
        terminal_statuses=terminal_statuses,
    )
