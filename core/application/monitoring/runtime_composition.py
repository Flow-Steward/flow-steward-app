"""Application-owned ingestion, receiver evidence and retention composition."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from core.application.lifecycle.runtime_installation_context import (
    resolve_projected_runtime_installation_context,
)
from core.application.monitoring.contracts import (
    RuntimeInstallationContext,
    build_observation_reports,
    encode_heartbeat,
)
from core.application.monitoring.ingestion import RuntimeStatusIngestionService
from core.application.monitoring.repository import RuntimeStatusRepository
from core.infrastructure.config.connection_strings import resolve_rabbitmq_url
from core.infrastructure.config.monitoring_runtime import (
    MonitoringRuntimeConfig,
    load_monitoring_runtime_config,
)
from core.infrastructure.config.runtime_env import runtime_env
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.monitoring.compact_observations import read_compact_observations
from core.infrastructure.runtime.monitoring.heartbeat_runtime import (
    CompositeConsumerSink,
    MonitoringRuntimeHandle,
    RuntimeObservationSource,
    start_monitoring_publisher,
    start_monitoring_threads,
)
from core.infrastructure.runtime.monitoring.heartbeat_transport import (
    RabbitHeartbeatConsumer,
    RabbitHeartbeatPublisher,
    Settlement,
    status_namespace,
)
from core.infrastructure.runtime.monitoring.observations import SourceObservation
from core.infrastructure.runtime.monitoring.role_probes import probe_http_role
from core.infrastructure.runtime.worker.consumer_observer import ConsumerStateSink

logger = logging.getLogger(__name__)

_DEFAULT_MCP_URL = "http://fs-mcp:8100/mcp"


def _start_observation_publisher(
    *,
    context: RuntimeInstallationContext,
    observe: Callable[[], tuple[SourceObservation, ...]],
    config: MonitoringRuntimeConfig,
    on_stop: Callable[[], None] | None = None,
) -> MonitoringRuntimeHandle:
    publisher = RabbitHeartbeatPublisher(
        url=resolve_rabbitmq_url(),
        namespace=status_namespace(context.installation_id, context.deployment_profile),
        config=config,
    )
    sequence = 0

    def collect() -> tuple[bytes, ...]:
        nonlocal sequence
        reports = build_observation_reports(
            observe(), context=context, published_at=datetime.now(UTC)
        )
        bodies = []
        for report in reports:
            sequence += 1
            bodies.append(encode_heartbeat(report.model_copy(update={"sequence": sequence})))
        return tuple(bodies)

    def farewell() -> tuple[bytes, ...]:
        if on_stop is None:
            return ()
        on_stop()
        return collect()

    return start_monitoring_publisher(
        collect=collect,
        publish=publisher.publish,
        idle=publisher.poll,
        close=publisher.close,
        interval_seconds=config.interval_seconds,
        close_timeout_seconds=config.operation_timeout_seconds * 2 + 1,
        farewell=farewell if on_stop is not None else None,
    )


def start_status_publisher(
    *,
    context: RuntimeInstallationContext,
    service_type: str,
    observe: Callable[[], SourceObservation],
    config: MonitoringRuntimeConfig,
    on_stop: Callable[[], None] | None = None,
) -> MonitoringRuntimeHandle:
    def observations() -> tuple[SourceObservation, ...]:
        row = observe()
        if row.service_type != service_type:
            raise ValueError("publisher source role mismatch")
        return (row,)

    return _start_observation_publisher(
        context=context, observe=observations, config=config, on_stop=on_stop
    )


def start_optional_process_reporting(
    *, runtime_values: Mapping[str, str], service_type: str
) -> MonitoringRuntimeHandle | None:
    """Monitoring cannot prevent product startup or expose driver credentials."""
    stage = "installation_scope"
    try:
        context = resolve_projected_runtime_installation_context(runtime_values)
        stage = "monitoring_policy"
        config = load_monitoring_runtime_config(runtime_values)
        stage = "publisher_initialization"
        source = RuntimeObservationSource(
            service_type=service_type, stale_seconds=config.stale_seconds
        )
        handle = start_status_publisher(
            context=context,
            service_type=service_type,
            observe=source.observe,
            config=config,
            on_stop=source.mark_stopped,
        )
        source.set_notification(handle.request_publish)
        return handle
    except Exception:
        logger.warning("Runtime reporting unavailable; stage=%s", stage)
        return None


def start_optional_compact_reporting(
    *, runtime_values: Mapping[str, str], registry: ReadinessRegistry
) -> MonitoringRuntimeHandle | None:
    stage = "installation_scope"
    try:
        context = resolve_projected_runtime_installation_context(runtime_values)
        stage = "monitoring_policy"
        config = load_monitoring_runtime_config(runtime_values)
        stage = "publisher_initialization"
        handle = _start_observation_publisher(
            context=context,
            config=config,
            observe=lambda: read_compact_observations(registry),
            on_stop=registry.retire_all,
        )
        registry.set_observation_notification(handle.request_publish)
        return handle
    except Exception:
        logger.warning("Compact reporting unavailable; stage=%s", stage)
        return None


def start_optional_worker_reporting(
    *, runtime_values: Mapping[str, str], worker_id: str, inherited_sink: ConsumerStateSink | None
) -> tuple[ConsumerStateSink | None, MonitoringRuntimeHandle | None]:
    stage = "installation_scope"
    try:
        context = resolve_projected_runtime_installation_context(runtime_values)
        stage = "monitoring_policy"
        config = load_monitoring_runtime_config(runtime_values)
        stage = "publisher_initialization"
        source = RuntimeObservationSource(
            service_type="worker", original_process_id=worker_id, stale_seconds=config.stale_seconds
        )
        handle = start_status_publisher(
            context=context,
            service_type="worker",
            observe=source.observe,
            config=config,
            on_stop=source.mark_stopped,
        )
        source.set_notification(handle.request_publish)
        return CompositeConsumerSink(
            source, inherited_sink, interval_seconds=config.interval_seconds
        ), handle
    except Exception:
        logger.warning("Worker reporting unavailable; stage=%s", stage)
        return inherited_sink, None


def delivery_decision(ingestion: RuntimeStatusIngestionService, body: bytes) -> Settlement:
    try:
        result = ingestion.accept(body)
        if result.disposition in {"stored", "duplicate"}:
            return "ack"
        if result.disposition == "rejected":
            return "reject"
    except Exception:
        return "retry"
    return "retry"


def _receiver_dependencies(
    channel: SourceObservation, context: RuntimeInstallationContext
) -> list[SourceObservation]:
    """What the receiver itself proves about the broker and the database.

    In Scalable nothing else probes the shared dependencies; Compact's
    supervisor already does. The receiver's broker connection is the RabbitMQ
    check, and storing this very row is the PostgreSQL check: if the database
    is down, nothing is stored and the row turns stale. The MCP server cannot
    report itself (its image carries no broker client), so the receiver probes
    its endpoint the way Compact's supervisor probes every role.
    """
    if context.deployment_profile != "scalable":
        return []
    mcp_url = str(runtime_env.get("MCP_URL") or _DEFAULT_MCP_URL).strip()
    return [
        replace(
            channel,
            service_type="mcp",
            source="control.mcp",
            observation_kind="role_probe",
            readiness=probe_http_role(mcp_url),
        ),
        replace(
            channel,
            service_type="rabbitmq",
            source="control.rabbitmq",
            observation_kind="dependency",
        ),
        replace(
            channel,
            service_type="postgres",
            source="control.postgres",
            observation_kind="dependency",
            readiness=True,
        ),
    ]


def start_status_consumer(
    *,
    context: RuntimeInstallationContext,
    config: MonitoringRuntimeConfig,
    ingestion: RuntimeStatusIngestionService,
    repository: RuntimeStatusRepository,
) -> MonitoringRuntimeHandle:
    namespace = status_namespace(context.installation_id, context.deployment_profile)
    consumer = RabbitHeartbeatConsumer(
        url=resolve_rabbitmq_url(),
        namespace=namespace,
        expected_namespace=namespace,
        config=config,
    )

    def record_channel(row: SourceObservation) -> None:
        rows = (row, *_receiver_dependencies(row, context))
        for report in build_observation_reports(
            rows, context=context, published_at=datetime.now(UTC)
        ):
            repository.store(report, history_enabled=config.history_enabled)

    def cleanup() -> None:
        now = datetime.now(UTC)
        repository.prune(
            context=context,
            current_before=now - timedelta(days=config.current_retention_days),
            history_before=now - timedelta(days=config.history_retention_days),
            limit=config.cleanup_batch_size,
        )

    return start_monitoring_threads(
        consumer=lambda stop: consumer.run(
            stop, lambda body: delivery_decision(ingestion, body), record_channel
        ),
        cleanup=cleanup,
        interval_seconds=config.interval_seconds,
        close_timeout_seconds=config.operation_timeout_seconds * 2 + 1,
    )


def start_configured_status_consumer(
    *,
    dsn: str,
    runtime_values: Mapping[str, str],
    journal_identity: str | None = None,
) -> MonitoringRuntimeHandle:
    stage = "installation_scope"
    try:
        context = resolve_projected_runtime_installation_context(
            runtime_values, journal_identity=journal_identity
        )
        stage = "monitoring_policy"
        config = load_monitoring_runtime_config(runtime_values)
        stage = "receiver_initialization"
        repository = RuntimeStatusRepository(dsn, config=config)
        ingestion = RuntimeStatusIngestionService(
            repository=repository,
            context=context,
            config=config,
            clock=lambda: datetime.now(UTC),
        )
        return start_status_consumer(
            context=context, config=config, ingestion=ingestion, repository=repository
        )
    except Exception:
        # Static stage only: exception messages can contain connection credentials.
        logger.error("Runtime status receiver unavailable; stage=%s", stage)
        raise
