from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest

from core.application.monitoring.contracts import HeartbeatReport
from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config
from core.infrastructure.runtime.monitoring.observations import SourceObservation


@pytest.mark.parametrize("role", ["process", "worker", "compact", "receiver"])
@pytest.mark.parametrize("stage", ["installation_scope", "monitoring_policy"])
def test_reporting_failure_logs_safe_configuration_stage(role, stage, caplog):
    from core.application.monitoring import runtime_composition
    from core.infrastructure.runtime.compact.readiness import ReadinessRegistry

    values = {"FS_DEPLOYMENT_PROFILE": "compact", "FS_INSTALLATION_ID": "secret-canary"}
    if stage == "monitoring_policy":
        values.update(
            FS_INSTALLATION_ID="ef5c927a-a7d5-4b43-9187-051e38e7a389",
            FS_MONITORING_INTERVAL_SECONDS="secret-canary",
        )
    if role == "process":
        assert (
            runtime_composition.start_optional_process_reporting(
                runtime_values=values, service_type="app"
            )
            is None
        )
    elif role == "worker":
        assert runtime_composition.start_optional_worker_reporting(
            runtime_values=values, worker_id="worker", inherited_sink=None
        ) == (None, None)
    elif role == "compact":
        assert (
            runtime_composition.start_optional_compact_reporting(
                runtime_values=values, registry=ReadinessRegistry(worker_processes=1)
            )
            is None
        )
    else:
        with pytest.raises(ValueError):
            runtime_composition.start_configured_status_consumer(
                dsn="postgresql://secret-canary", runtime_values=values
            )
    assert f"stage={stage}" in caplog.text
    assert "secret-canary" not in caplog.text


@pytest.mark.parametrize(
    ("state", "readiness", "lifecycle"),
    [
        (None, None, "running"),
        ("registered", True, "running"),
        ("active", True, "running"),
        ("maintenance_paused", True, "maintenance_paused"),
        ("draining", False, "draining"),
        ("disconnected", False, "disconnected"),
    ],
)
def test_process_observation_preserves_consumer_readiness_and_lifecycle(
    state, readiness, lifecycle
):
    from core.infrastructure.runtime.monitoring.heartbeat_runtime import RuntimeObservationSource

    source = RuntimeObservationSource(service_type="worker")
    if state is not None:
        source.record(state)
    observation = source.observe()
    assert observation.consumer_state == state
    assert observation.readiness is readiness
    assert observation.lifecycle == lifecycle


def test_arbitrary_role_publishes_without_health_inference(monkeypatch, context):
    from core.application.monitoring import runtime_composition

    sent = []
    delivered = Event()

    class Publisher:
        def __init__(self, **_kwargs):
            pass

        def publish(self, body):
            sent.append(HeartbeatReport.model_validate_json(body))
            delivered.set()

        def poll(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(runtime_composition, "RabbitHeartbeatPublisher", Publisher)
    identity = uuid4()
    observation = SourceObservation(
        service_type="image-indexer",
        source="self",
        original_process_id="opaque raw identity",
        observer_id=identity,
        observation_kind="process",
        source_sequence=1,
        checked_at=datetime.now(UTC),
        source_age_seconds=0,
        availability="observed",
    )
    handle = runtime_composition.start_status_publisher(
        context=context,
        service_type="image-indexer",
        observe=lambda: observation,
        config=load_monitoring_runtime_config({}),
    )
    assert delivered.wait(2)
    handle.close()
    assert sent[0].service_type == "image-indexer"
    assert sent[0].payload.health.state == "not_observed"
    assert sent[0].payload.readiness.state == "not_observed"
    assert not any(thread.is_alive() for thread in handle.threads)


def test_worker_cache_preserves_raw_identity_and_source_age():
    from core.infrastructure.runtime.monitoring.heartbeat_runtime import RuntimeObservationSource

    tick = [0.0]
    source = RuntimeObservationSource(
        service_type="worker",
        original_process_id="  opaque:worker  ",
        stale_seconds=10,
        monotonic=lambda: tick[0],
    )
    source.record("maintenance_paused")
    first = source.observe()
    tick[0] = 11
    expired = source.observe()
    assert expired.checked_at == first.checked_at
    assert expired.original_process_id == "  opaque:worker  "
    assert expired.availability == "expired"
    assert expired.source_age_seconds == 11
    assert expired.readiness is True
    source.refresh()
    assert source.observe().availability == "observed"
    source.close()
    source.close()
    assert source.observe().consumer_state == "disconnected"


def test_installer_passes_explicit_runtime_role(monkeypatch):
    from core.entrypoints.api import app_factory, extension_installer

    captured = []
    monkeypatch.setattr(
        app_factory, "create_app", lambda **kwargs: captured.append(kwargs) or object()
    )
    extension_installer.create_app()
    assert captured[0]["runtime_service_type"] == "installer"


def test_lifespan_closes_injected_monitoring_handle_once():
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from core.infrastructure.runtime import app_lifecycle_runtime

    handle = MagicMock()

    async def start():
        return None

    handler = app_lifecycle_runtime.build_lifespan_handler(
        startup_handler=start,
        shutdown_app_fn=MagicMock(),
        resume_shutdown_event=Event(),
        join_resume_threads=lambda: 0,
        stop_control_threads=lambda _: None,
        get_control_threads=dict,
        logger=MagicMock(),
        resume_join_timeout_seconds=1,
        get_monitoring_handle=lambda: handle,
    )

    async def run():
        async with asynccontextmanager(handler)(object()):
            pass

    asyncio.run(run())
    handle.close.assert_called_once()


def test_state_change_wakes_periodic_publisher():
    from core.infrastructure.runtime.monitoring.heartbeat_runtime import start_monitoring_publisher

    sent = []
    initial = Event()
    changed = Event()

    def publish(body):
        sent.append(body)
        (initial if len(sent) == 1 else changed).set()

    handle = start_monitoring_publisher(
        collect=lambda: (b"latest",),
        publish=publish,
        idle=lambda: None,
        close=lambda: None,
        interval_seconds=60,
        close_timeout_seconds=2,
    )
    try:
        assert initial.wait(2)
        handle.request_publish()
        assert changed.wait(2)
    finally:
        handle.close()


def test_compact_notification_only_on_state_change_or_retirement():
    from core.infrastructure.runtime.compact.readiness import ReadinessRegistry

    changes = []
    registry = ReadinessRegistry(worker_processes=1)
    registry.set_observation_notification(lambda: changes.append(True))
    registry.record_worker(
        role="worker-0", worker_incarnation="original", state="registered", sequence=1
    )
    registry.record_worker(
        role="worker-0", worker_incarnation="original", state="registered", sequence=2
    )
    assert len(changes) == 1
    registry.reset_role("worker-0")
    assert len(changes) == 2


def test_worker_reporting_composes_fd_without_changing_identity(monkeypatch, context):
    from unittest.mock import MagicMock

    from core.application.monitoring import runtime_composition

    inherited = MagicMock()
    handle = MagicMock()
    observed = []
    monkeypatch.setattr(
        runtime_composition,
        "start_status_publisher",
        lambda **kwargs: observed.append(kwargs["observe"]) or handle,
    )
    sink, owned = runtime_composition.start_optional_worker_reporting(
        runtime_values={
            "FS_INSTALLATION_ID": context.installation_id,
            "FS_DEPLOYMENT_PROFILE": "scalable",
        },
        worker_id=" raw:identity ",
        inherited_sink=inherited,
    )
    assert owned is handle
    sink.record("registered")
    sink.refresh()
    assert observed[0]().original_process_id == " raw:identity "
    assert observed[0]().readiness is True
    inherited.record.assert_called_once_with("registered")
    inherited.refresh.assert_called_once()
    sink.close()
    sink.close()
    inherited.close.assert_called_once()


def test_publication_sequence_advances_without_rechecking_cached_evidence(monkeypatch, context):
    from core.application.monitoring import runtime_composition

    collectors = []
    monkeypatch.setattr(
        runtime_composition,
        "start_monitoring_publisher",
        lambda **kwargs: collectors.append(kwargs["collect"]) or object(),
    )
    observation = SourceObservation(
        service_type="worker",
        source="self",
        original_process_id="original",
        observer_id=uuid4(),
        observation_kind="process",
        checked_at=datetime.now(UTC),
        source_sequence=1,
        source_age_seconds=181,
        source_stale_seconds=180.0,
        availability="expired",
    )
    runtime_composition.start_status_publisher(
        context=context,
        service_type="worker",
        observe=lambda: observation,
        config=load_monitoring_runtime_config({}),
    )
    first = HeartbeatReport.model_validate_json(collectors[0]()[0])
    second = HeartbeatReport.model_validate_json(collectors[0]()[0])
    assert second.sequence > first.sequence
    assert first.payload == second.payload
    assert second.payload.source_availability == "expired"


def test_worker_runtime_injects_reporting_after_actual_identity_and_closes(monkeypatch):
    from unittest.mock import MagicMock

    from core.infrastructure.runtime.worker import worker

    monkeypatch.setattr("sys.argv", ["worker"])
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setattr(worker, "_enforce_schema_compatibility", lambda: None)
    monkeypatch.setattr(worker, "_repair_canonical_guardrails_for_worker", lambda **_: None)
    monkeypatch.setattr(worker, "_worker_id", lambda: " raw:worker ")
    monkeypatch.setattr(worker, "create_observation_sink", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_start_extension_inbound_poll_loop", lambda **_: None)
    monkeypatch.setattr(worker, "start_terminal_curation_loop", lambda **_: None)
    monkeypatch.setattr(worker, "start_workflow_knowledge_sync_loop", lambda **_: None)
    monkeypatch.setattr(
        "core.infrastructure.observability.process_metrics_runtime.start_process_metrics_server",
        lambda **_: None,
    )
    sink, handle = MagicMock(), MagicMock()
    start = MagicMock(return_value=(sink, handle))
    consume = MagicMock()
    monkeypatch.setattr(worker, "consume_all_projects", consume)
    worker.main(start_runtime_reporting=start)
    start.assert_called_once_with(" raw:worker ", None)
    assert consume.call_args.kwargs["observation_sink"] is sink
    sink.close.assert_called_once()
    handle.close.assert_called_once()
