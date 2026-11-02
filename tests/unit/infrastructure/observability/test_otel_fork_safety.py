"""Fork behaviour of the pinned SDK, measured with real forks."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace import TracerProvider

from core.infrastructure.observability.otel_environment import apply_environment_policy
from core.infrastructure.observability.otel_export_guards import (
    OwnerProcess,
    PidGuardedMetricExporter,
)
from core.infrastructure.observability.otel_resource import build_resource

pytestmark = pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(), reason="needs fork"
)


class _PidRecordingExporter(MetricExporter):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"shutdown {os.getpid()}\n")


def _run_forked_child(target, *args) -> int:
    process = multiprocessing.get_context("fork").Process(target=target, args=args)
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    return int(process.pid or 0)


def _child_records_and_shuts_down(provider: MeterProvider, seconds: float) -> None:
    provider.get_meter("flow_steward").create_counter("fs.test.child").add(1)
    time.sleep(seconds)
    provider.shutdown()
    os._exit(0)


def _metric_provider(path: Path, *, guarded: bool) -> MeterProvider:
    exporter: MetricExporter = _PidRecordingExporter(path)
    if guarded:
        exporter = PidGuardedMetricExporter(exporter, owner=OwnerProcess())
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=50)
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    # The SDK skips an export with no data; the parent records so it has some.
    provider.get_meter("flow_steward").create_counter("fs.test.parent").add(1)
    return provider


def test_unguarded_inherited_reader_exports_from_the_child(tmp_path: Path) -> None:
    # The hazard the guard exists for: proves this test can see a child export.
    path = tmp_path / "exports.txt"
    provider = _metric_provider(path, guarded=False)
    try:
        child_pid = _run_forked_child(_child_records_and_shuts_down, provider, 0.3)
    finally:
        provider.shutdown()

    assert str(child_pid) in path.read_text(encoding="utf-8").split()


def test_guarded_exporter_sends_nothing_from_a_forked_child(tmp_path: Path) -> None:
    path = tmp_path / "exports.txt"
    provider = _metric_provider(path, guarded=True)
    try:
        child_pid = _run_forked_child(_child_records_and_shuts_down, provider, 0.3)
    finally:
        provider.shutdown()

    records = path.read_text(encoding="utf-8").splitlines()
    assert str(os.getpid()) in records
    assert not any(str(child_pid) in record for record in records)


def _child_reports_resource(provider: TracerProvider, path: Path) -> None:
    attributes = dict(provider.resource.attributes)
    path.write_text(json.dumps(attributes), encoding="utf-8")
    os._exit(0)


def _resource_after_fork(tmp_path: Path) -> tuple[dict, dict]:
    resource = build_resource(role="worker", service_version="9.9.9", operator_attributes={})
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    path = tmp_path / "resource.json"
    _run_forked_child(_child_reports_resource, provider, path)
    return dict(provider.resource.attributes), json.loads(path.read_text(encoding="utf-8"))


def test_policy_keeps_process_metadata_out_of_the_child_resource(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OTEL_EXPERIMENTAL_RESOURCE_DETECTORS", "process")
    apply_environment_policy()

    parent, child = _resource_after_fork(tmp_path)

    assert not any(key.startswith(("process.", "host.")) for key in child)
    assert child["service.name"] == "flow-steward-worker"
    assert child["service.instance.id"] != parent["service.instance.id"]


def test_without_the_policy_the_sdk_adds_process_metadata_after_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Green-direction check: the detector variable really is re-read at fork.
    monkeypatch.setenv("OTEL_EXPERIMENTAL_RESOURCE_DETECTORS", "process")

    _, child = _resource_after_fork(tmp_path)

    assert "process.pid" in child
