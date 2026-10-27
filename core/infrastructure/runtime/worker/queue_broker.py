"""RabbitMQ connection policy, topology, ready hints and broker inspection."""

import contextlib
import json
import logging
import time
from typing import Any

from core.infrastructure.config.rabbitmq_transport import build_rabbitmq_parameters
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.worker.queue_messages import (
    _ready_event_payload,
)
from core.infrastructure.runtime.worker.queue_names import queue_dead_letter_queue_name
from core.infrastructure.runtime.worker.queue_pika_compat import pika

_TOKEN_APPLICATION_JSON = "application/json"

logger = logging.getLogger(__name__)

_QUIET_PIKA_LOGGERS = (
    "pika.adapters.utils.io_services_utils",
    "pika.adapters.utils.connection_workflow",
    "pika.adapters.blocking_connection",
)


@contextlib.contextmanager
def _quiet_pika_connect_retry_logs(*, enabled: bool):
    if not enabled:
        yield
        return

    previous_levels: list[tuple[logging.Logger, int]] = []
    try:
        for name in _QUIET_PIKA_LOGGERS:
            target = logging.getLogger(name)
            previous_levels.append((target, target.level))
            target.setLevel(logging.CRITICAL + 1)
        yield
    finally:
        for target, level in previous_levels:
            target.setLevel(level)


_DEFAULT_RABBITMQ_HOST = str(_runtime_env.get("RABBITMQ_HOST", "localhost") or "localhost").strip()

_DEFAULT_RABBITMQ_PORT = str(_runtime_env.get("RABBITMQ_PORT", "5672") or "5672").strip()

_DEFAULT_RABBITMQ_USER = str(_runtime_env.get("RABBITMQ_USER", "guest") or "guest").strip()

_DEFAULT_RABBITMQ_PASSWORD = str(_runtime_env.get("RABBITMQ_PASSWORD", "guest") or "guest").strip()

_DEFAULT_URL = (
    f"amqp://{_DEFAULT_RABBITMQ_USER}:{_DEFAULT_RABBITMQ_PASSWORD}"
    f"@{_DEFAULT_RABBITMQ_HOST}:{_DEFAULT_RABBITMQ_PORT}/"
)

_DEFAULT_RABBITMQ_HEARTBEAT_SECONDS = 0

_EXCHANGE = ""  # default exchange (direct routing via queue name)

_READY_QUEUE = "platform.projects.ready"


def _rabbitmq_heartbeat_seconds() -> int:
    """Return heartbeat seconds suitable for blocking long-running job handlers."""
    raw = str(
        _runtime_env.get(
            "FS_RABBITMQ_HEARTBEAT_SECONDS",
            str(_DEFAULT_RABBITMQ_HEARTBEAT_SECONDS),
        )
        or str(_DEFAULT_RABBITMQ_HEARTBEAT_SECONDS)
    ).strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid FS_RABBITMQ_HEARTBEAT_SECONDS=%r; using %d",
            raw,
            _DEFAULT_RABBITMQ_HEARTBEAT_SECONDS,
        )
        return _DEFAULT_RABBITMQ_HEARTBEAT_SECONDS


def _queue_name(project_id: Any) -> str:
    """Return the RabbitMQ queue name for a project."""
    return f"platform.project.{project_id}"


def _dead_letter_queue_name(project_id: Any) -> str:
    return queue_dead_letter_queue_name(_queue_name(project_id))


def _connect(amqp_url: str | None = None, retries: int = 5, delay: float = 3.0) -> Any:
    """
    Open a blocking pika connection, retrying on failure.
    Used by both publisher and consumer.
    """
    url = amqp_url or _runtime_env.get("RABBITMQ_URL", _DEFAULT_URL)
    params = build_rabbitmq_parameters(url, parameter_factory=pika.URLParameters)
    params.heartbeat = _rabbitmq_heartbeat_seconds()
    params.blocked_connection_timeout = 300

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with _quiet_pika_connect_retry_logs(enabled=attempt < retries):
                conn = pika.BlockingConnection(params)
            logger.debug("RabbitMQ connected (attempt %d)", attempt)
            return conn
        except pika.exceptions.AMQPConnectionError as e:
            last_err = e
            log_fn = logger.info if attempt < retries else logger.warning
            log_fn("RabbitMQ connect attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError(
        f"Cannot connect to RabbitMQ after {retries} attempts: {last_err}"
    ) from last_err


def _declare_queue(channel: Any, project_id: Any):
    """Declare the project queue (idempotent, durable)."""
    queue_name = _queue_name(project_id)
    dead_letter_queue = _dead_letter_queue_name(project_id)
    channel.queue_declare(
        queue=dead_letter_queue,
        durable=True,
        arguments={
            "x-max-priority": 0,
        },
    )
    channel.queue_declare(
        queue=queue_name,
        durable=True,  # survives broker restart
        arguments={
            "x-max-priority": 0,  # no priority (FIFO)
            "x-dead-letter-exchange": _EXCHANGE,
            "x-dead-letter-routing-key": dead_letter_queue,
        },
    )


def _declare_ready_queue(channel: Any):
    """Declare the global ready-signal queue (idempotent, durable)."""
    channel.queue_declare(
        queue=_READY_QUEUE,
        durable=True,
        arguments={
            "x-max-priority": 0,
        },
    )


def delete_project_queues(
    project_id: Any,
    *,
    amqp_url: str | None = None,
) -> list[str]:
    """Delete exactly one project's primary and dead-letter queues."""
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        raise ValueError("project_id is required for queue cleanup")
    queue_names = [
        _queue_name(normalized_project_id),
        _dead_letter_queue_name(normalized_project_id),
    ]
    connection = _connect(amqp_url)
    try:
        channel = connection.channel()
        for queue_name in queue_names:
            channel.queue_delete(
                queue=queue_name,
                if_unused=False,
                if_empty=False,
            )
    finally:
        connection.close()
    return queue_names


def _ready_message_properties() -> Any:
    return pika.BasicProperties(
        delivery_mode=2,
        content_type=_TOKEN_APPLICATION_JSON,
    )


def _publish_ready_hint(
    channel: Any,
    *,
    project_id: Any,
    job_id: Any | None = None,
    task_id: str | None = None,
    enqueued_at: str | None = None,
) -> None:
    _declare_ready_queue(channel)
    channel.basic_publish(
        exchange=_EXCHANGE,
        routing_key=_READY_QUEUE,
        body=json.dumps(
            _ready_event_payload(
                project_id=project_id,
                job_id=job_id,
                task_id=task_id,
                enqueued_at=enqueued_at,
            )
        ).encode(),
        mandatory=True,
        properties=_ready_message_properties(),
    )


def _project_queue_depth(channel: Any, project_id: Any) -> int:
    try:
        result = channel.queue_declare(
            queue=_queue_name(project_id),
            durable=True,
            passive=True,
        )
    except Exception as e:
        if _is_missing_queue_error(e):
            return 0
        logger.debug("project queue depth lookup failed for project=%s: %s", project_id, e)
        return -1
    return int(getattr(getattr(result, "method", None), "message_count", 0) or 0)


def _is_missing_queue_error(exc: Exception) -> bool:
    reply_code = getattr(exc, "reply_code", None)
    if str(reply_code or "").strip() == "404":
        return True
    reply_text = str(getattr(exc, "reply_text", "") or "").strip().lower()
    message = str(exc or "").strip().lower()
    combined = " ".join(part for part in (reply_text, message) if part)
    return bool(
        "not_found" in combined
        or "no queue" in combined
        or ("queue" in combined and "not found" in combined)
    )


def get_queue_depth(project_id: Any, amqp_url: str | None = None) -> int:
    """
    Return the number of messages currently sitting in the project queue
    (ready + unacknowledged). Returns 0 when the queue has not been created yet,
    and -1 only when the broker/metrics are unreachable.
    """
    try:
        conn = _connect(amqp_url, retries=2, delay=1.0)
        ch = conn.channel()
        result = ch.queue_declare(
            queue=_queue_name(project_id),
            durable=True,
            passive=True,  # just inspect, do not create
        )
        depth = result.method.message_count
        conn.close()
        return depth
    except Exception as e:
        if _is_missing_queue_error(e):
            return 0
        logger.debug("get_queue_depth(%s) failed: %s", project_id, e)
        return -1


def get_ready_queue_depth(amqp_url: str | None = None) -> int:
    """Return message count currently in the global ready-signal queue."""
    try:
        conn = _connect(amqp_url, retries=2, delay=1.0)
        ch = conn.channel()
        result = ch.queue_declare(
            queue=_READY_QUEUE,
            durable=True,
            passive=True,
        )
        depth = result.method.message_count
        conn.close()
        return depth
    except Exception as e:
        if _is_missing_queue_error(e):
            return 0
        logger.debug("get_ready_queue_depth failed: %s", e)
        return -1


def get_ready_queue_consumer_count(amqp_url: str | None = None) -> int:
    """Return live consumers attached to the global ready-signal queue."""
    try:
        conn = _connect(amqp_url, retries=2, delay=1.0)
        ch = conn.channel()
        result = ch.queue_declare(
            queue=_READY_QUEUE,
            durable=True,
            passive=True,
        )
        consumers = int(getattr(result.method, "consumer_count", 0) or 0)
        conn.close()
        return consumers
    except Exception as e:
        if _is_missing_queue_error(e):
            return 0
        logger.debug("get_ready_queue_consumer_count failed: %s", e)
        return -1


def get_broker_facts(amqp_url: str | None = None) -> dict[str, Any]:
    """Return the broker's product, version and the ready queue it holds right now."""
    conn = _connect(amqp_url, retries=1, delay=0.0)
    try:
        properties = dict(getattr(conn._impl, "server_properties", None) or {})
        ch = conn.channel()
        try:
            ready = ch.queue_declare(queue=_READY_QUEUE, durable=True, passive=True)
            ready_queue: dict[str, Any] = {
                "messages": int(ready.method.message_count),
                "consumers": int(getattr(ready.method, "consumer_count", 0) or 0),
            }
        except Exception as e:
            if not _is_missing_queue_error(e):
                raise
            ready_queue = {"messages": 0, "consumers": 0, "declared": False}
    finally:
        conn.close()

    def _text(value: Any) -> str:
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")

    return {
        "product": _text(properties.get("product")),
        "version": _text(properties.get("version")),
        "platform": _text(properties.get("platform")),
        "ready_queue": ready_queue,
    }
