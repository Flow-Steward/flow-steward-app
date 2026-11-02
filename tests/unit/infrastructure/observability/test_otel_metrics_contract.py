from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("opentelemetry.sdk")

from core.infrastructure.observability import (
    otel_installation_metrics,
    otel_instruments,
    otel_process_metrics,
)
from core.infrastructure.observability.observability_metrics import (
    build_prometheus_metrics_payload,
)
from core.infrastructure.observability.otel_export_guards import OwnerProcess
from tests.unit.test_workflow_telemetry import _Telemetry

# The fixed export contract: name -> (instrument type, unit, metric attributes).
CONTRACT = {
    "fs.workflow.segment.duration": ("histogram", "s", {"fs.outcome", "error.type"}),
    "fs.workflow.step.duration": ("histogram", "s", {"fs.step.kind", "fs.outcome", "error.type"}),
    "gen_ai.client.operation.duration": (
        "histogram",
        "s",
        {"gen_ai.operation.name", "gen_ai.provider.name", "fs.llm.purpose", "error.type"},
    ),
    "gen_ai.client.token.usage": (
        "histogram",
        "{token}",
        {"gen_ai.operation.name", "gen_ai.provider.name", "fs.llm.purpose", "gen_ai.token.type"},
    ),
    "process.cpu.time": ("counter", "s", set()),
    "process.thread.count": ("up_down_counter", "{thread}", set()),
    "process.uptime": ("gauge", "s", set()),
    "fs.installation.projects": ("gauge", "{project}", set()),
    "fs.installation.jobs": ("gauge", "{job}", {"fs.job.status"}),
    "fs.installation.project_leases.active": ("gauge", "{lease}", set()),
    "fs.installation.ready_queue.depth": ("gauge", "{message}", set()),
    "fs.slo.workflow.p95_latency": ("gauge", "s", set()),
    "fs.slo.event_dispatch.retry_rate": ("gauge", "1", set()),
    "fs.slo.event_dispatch.dead_letter_rate": ("gauge", "1", set()),
    "fs.slo.jobs.stuck": ("gauge", "{job}", set()),
}
# Every /metrics series and where it goes in the OTLP export.
METRICS_ENDPOINT_MAPPING = {
    "platform_health_ready": None,  # readiness stays out of OTLP
    "platform_readiness_check": None,
    "platform_projects_total": "fs.installation.projects",
    "platform_jobs_total": "fs.installation.jobs",
    "platform_project_leases_active": "fs.installation.project_leases.active",
    "platform_ready_queue_depth": "fs.installation.ready_queue.depth",
    "platform_slo_p95_latency_seconds": "fs.slo.workflow.p95_latency",
    "platform_slo_retry_rate": "fs.slo.event_dispatch.retry_rate",
    "platform_slo_dead_letter_rate": "fs.slo.event_dispatch.dead_letter_rate",
    "platform_slo_stuck_jobs": "fs.slo.jobs.stuck",
    "platform_slo_agent_failure_rate": None,  # agent names are not a bounded attribute
}


class _Cursor:
    def __init__(self, rows) -> None:
        self._rows = rows
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql, params=None) -> None:
        self._result = self._rows["projects" if "projects" in sql else "jobs"]

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return self._result


def _store(slo=None, jobs=None):
    store = MagicMock()
    rows = {
        "projects": [(3,)],
        "jobs": jobs if jobs is not None else [("completed", 5), ("weird-status", 2)],
    }
    store.conn.cursor.side_effect = lambda: _Cursor(rows)
    store.list_project_execution_leases.return_value = [{"id": 1}, {"id": 2}]
    store.get_core_slo_metrics.return_value = (
        {"p95_latency_seconds": 1.5, "retry_rate": 0.1, "dead_letter_rate": None, "stuck_jobs": 0}
        if slo is None
        else slo
    )
    return store


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> _Telemetry:
    telemetry = _Telemetry()
    monkeypatch.setattr(otel_instruments, "current_telemetry", lambda: telemetry)
    otel_instruments._reset_for_tests()
    yield telemetry
    otel_instruments._reset_for_tests()


def _metrics(state: _Telemetry) -> dict:
    found = {}
    data = state.reader.get_metrics_data()
    for resource in getattr(data, "resource_metrics", None) or []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found[metric.name] = metric
    return found


def _type_of(metric) -> str:
    kind = type(metric.data).__name__
    if kind == "Sum":
        return "counter" if metric.data.is_monotonic else "up_down_counter"
    return kind.lower()


def _record_every_synchronous_instrument() -> None:
    for name in (
        otel_instruments.SEGMENT_DURATION,
        otel_instruments.STEP_DURATION,
        otel_instruments.OPERATION_DURATION,
        otel_instruments.TOKEN_USAGE,
    ):
        otel_instruments.record(name, 1, {})


def test_the_exported_metrics_are_exactly_the_contract(state: _Telemetry) -> None:
    _record_every_synchronous_instrument()
    otel_process_metrics.register_process_metrics()
    full_slo = {
        "p95_latency_seconds": 1.5,
        "retry_rate": 0.1,
        "dead_letter_rate": 0.0,
        "stuck_jobs": 0,
    }
    otel_installation_metrics.register_installation_metrics(
        store_factory=lambda: _store(slo=full_slo), get_ready_queue_depth=lambda: 4
    )

    metrics = _metrics(state)

    assert set(metrics) == set(CONTRACT)
    for name, (kind, unit, _attributes) in CONTRACT.items():
        assert (_type_of(metrics[name]), metrics[name].unit) == (kind, unit), name


def test_metric_attributes_stay_within_the_contract(state: _Telemetry) -> None:
    from core.application.observability.llm_usage import usage_from_mapping
    from core.application.observability.workflow_telemetry import RunTelemetry

    telemetry = RunTelemetry(job_id="job-1")
    telemetry.segment_started("root")
    telemetry.step_started("step", "root", kind="agent", attempt=1)
    telemetry.model_started("llm", "step", provider_type="openai", model="gpt-5.6-sol")
    telemetry.model_finished(
        "llm", usage=usage_from_mapping({"input_tokens": 3, "output_tokens": 1})
    )
    telemetry.step_finished("step", outcome="error", error=TimeoutError())
    telemetry.segment_finished("root", outcome="ok")
    otel_installation_metrics.register_installation_metrics(
        store_factory=_store, get_ready_queue_depth=lambda: 0
    )

    for name, metric in _metrics(state).items():
        allowed = CONTRACT[name][2]
        for point in metric.data.data_points:
            assert set(point.attributes) <= allowed, (name, dict(point.attributes))


def test_installation_values_follow_the_metrics_endpoint(state: _Telemetry) -> None:
    otel_installation_metrics.register_installation_metrics(
        store_factory=_store, get_ready_queue_depth=lambda: -1
    )

    metrics = _metrics(state)

    def points(name):
        return {
            tuple(sorted(p.attributes.items())): p.value for p in metrics[name].data.data_points
        }

    assert points("fs.installation.projects") == {(): 3}
    assert points("fs.installation.jobs") == {
        (("fs.job.status", "completed"),): 5,
        (("fs.job.status", "other"),): 2,
    }
    assert points("fs.installation.project_leases.active") == {(): 2}
    # Unknown values are absent, never zero.
    assert "fs.installation.ready_queue.depth" not in metrics
    assert "fs.slo.event_dispatch.dead_letter_rate" not in metrics
    assert points("fs.slo.jobs.stuck") == {(): 0}
    assert points("fs.slo.workflow.p95_latency") == {(): 1.5}


def test_one_collection_takes_one_snapshot(state: _Telemetry) -> None:
    store = _store()
    queue_depth = MagicMock(return_value=1)
    otel_installation_metrics.register_installation_metrics(
        store_factory=lambda: store, get_ready_queue_depth=queue_depth
    )

    _metrics(state)

    assert queue_depth.call_count == 1
    assert store.get_core_slo_metrics.call_count == 1


def test_a_forked_child_collects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    child = _Telemetry(owner=OwnerProcess(pid=1))
    monkeypatch.setattr(otel_instruments, "current_telemetry", lambda: child)
    factory = MagicMock(side_effect=_store)
    otel_installation_metrics.register_installation_metrics(
        store_factory=factory, get_ready_queue_depth=lambda: 1
    )
    otel_process_metrics.register_process_metrics()

    assert _metrics(child) == {}
    factory.assert_not_called()


def test_disabled_metrics_register_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _Telemetry(metrics=False)
    monkeypatch.setattr(otel_instruments, "current_telemetry", lambda: off)

    assert (
        otel_installation_metrics.register_installation_metrics(
            store_factory=_store, get_ready_queue_depth=lambda: 1
        )
        is None
    )
    assert otel_process_metrics.register_process_metrics() is False


def test_every_metrics_endpoint_series_has_a_mapping() -> None:
    payload = build_prometheus_metrics_payload(
        _store(),
        compute_readiness=lambda bl: {"ready": True, "checks": {"db": {"ok": True}}},
        to_json_obj=lambda value: value,
        get_ready_queue_depth=lambda: -1,
    )
    series = {line.split()[2] for line in payload.splitlines() if line.startswith("# TYPE ")}

    assert series == set(METRICS_ENDPOINT_MAPPING)
    assert {target for target in METRICS_ENDPOINT_MAPPING.values() if target} <= set(CONTRACT)


def test_the_metrics_endpoint_output_is_unchanged() -> None:
    payload = build_prometheus_metrics_payload(
        _store(slo={}),
        compute_readiness=lambda bl: {"ready": False, "checks": {}},
        to_json_obj=lambda value: value,
        get_ready_queue_depth=lambda: -1,
    )
    lines = [line for line in payload.splitlines() if not line.startswith("#")]

    assert lines == [
        "platform_health_ready 0",
        "platform_projects_total 3",
        'platform_jobs_total{status="completed"} 5',
        'platform_jobs_total{status="weird-status"} 2',
        "platform_project_leases_active 2",
        "platform_ready_queue_depth -1",
        "platform_slo_p95_latency_seconds 0",
        "platform_slo_retry_rate 0",
        "platform_slo_dead_letter_rate 0",
        "platform_slo_stuck_jobs 0",
    ]


def test_process_metrics_load_without_the_process_endpoint_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys

    # The fs-mcp image ships only the otel_* modules.
    monkeypatch.setitem(
        sys.modules, "core.infrastructure.observability.process_metrics_runtime", None
    )
    try:
        reloaded = importlib.reload(otel_process_metrics)
        assert reloaded._PROCESS_START_TIME_SECONDS > 0
    finally:
        monkeypatch.undo()
        importlib.reload(otel_process_metrics)
