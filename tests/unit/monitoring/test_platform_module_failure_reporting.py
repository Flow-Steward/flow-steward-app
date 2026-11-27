"""Worker and control roles report the platform modules they skipped (ADR 0082 §5)."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from core.application.monitoring.contracts import (
    MAX_REPORTED_MODULE_FAILURES,
    HeartbeatReport,
    StatusRecord,
    build_observation_reports,
    encode_heartbeat,
)
from core.application.monitoring.queries import RuntimeStatusQueryService
from core.application.monitoring.system import (
    background_module_failures,
    build_monitoring_summary_payload,
)
from core.infrastructure.runtime.monitoring.heartbeat_runtime import RuntimeObservationSource

FAILURE = {
    "id": "acme.ledger",
    "version": "1.2.0",
    "status": "failed",
    "load_error": "ImportError: No module named 'acme_ledger_backend'",
}


class StoredRows:
    def __init__(self, rows):
        self.rows = rows

    def load(self, _context):
        return self.rows


def _record(report_body, now, *, service_type="worker", failures=(), age=0, **payload_changes):
    payload = {
        "type": "runtime_status",
        "version": 1,
        "lifecycle": "running",
        "readiness": {"state": "not_observed"},
        "health": {"state": "not_observed"},
        "consumer_state": "registered",
        "platform_module_failures": list(failures),
        **payload_changes,
    }
    identity = str(uuid4())
    body = json.loads(report_body())
    body.update(
        service_type=service_type,
        instance_id=identity,
        incarnation_id=identity,
        observed_at=(now - timedelta(seconds=age)).isoformat(),
        payload=payload,
    )
    report = HeartbeatReport.model_validate(body)
    return StatusRecord(**report.model_dump(), id=uuid4(), received_at=now)


def _components(rows, context, now):
    return RuntimeStatusQueryService(repository=StoredRows(rows)).get_components(context, now=now)


def test_a_role_source_reports_its_skipped_modules_in_every_heartbeat(context, now):
    source = RuntimeObservationSource(
        service_type="control", platform_module_failures=[FAILURE, {"id": ""}]
    )

    reports = build_observation_reports((source.observe(),), context=context, published_at=now)

    failures = reports[0].payload.platform_module_failures
    assert [(item.id, item.version, item.load_error) for item in failures] == [
        ("acme.ledger", "1.2.0", FAILURE["load_error"])
    ]


def test_a_source_without_failures_reports_none(context, now):
    source = RuntimeObservationSource(service_type="worker")

    reports = build_observation_reports((source.observe(),), context=context, published_at=now)

    assert reports[0].payload.platform_module_failures == ()


def test_reported_failures_are_bounded_so_a_heartbeat_stays_small(context, now):
    many = [
        {"id": f"acme.module-{index:03d}" + "x" * 100, "version": "9" * 64, "load_error": "E" * 900}
        for index in range(MAX_REPORTED_MODULE_FAILURES + 10)
    ]
    source = RuntimeObservationSource(service_type="worker", platform_module_failures=many)

    report = build_observation_reports((source.observe(),), context=context, published_at=now)[0]

    failures = report.payload.platform_module_failures
    assert len(failures) == MAX_REPORTED_MODULE_FAILURES
    assert all(len(item.load_error) <= 500 for item in failures)
    assert failures[0].load_error.endswith("...")
    assert len(encode_heartbeat(report)) <= 16384


def test_a_report_written_before_the_field_existed_still_loads(report_body):
    report = HeartbeatReport.model_validate_json(report_body())

    assert report.payload.platform_module_failures == ()


def test_the_component_row_carries_the_reported_failures(report_body, context, now):
    result = _components([_record(report_body, now, failures=[FAILURE])], context, now)

    assert [item.id for item in result.rows[0].platform_module_failures] == ["acme.ledger"]


def test_background_failures_name_the_role_once_across_replicas(report_body, context, now):
    rows = [
        _record(report_body, now, service_type="worker", failures=[FAILURE]),
        _record(report_body, now, service_type="worker", failures=[FAILURE]),
        _record(report_body, now, service_type="control", failures=[FAILURE]),
        # The app's failures come from the app itself, never from its heartbeat.
        _record(report_body, now, service_type="app", failures=[FAILURE]),
    ]

    records = background_module_failures(_components(rows, context, now))

    assert sorted(record["role"] for record in records) == ["control", "worker"]
    assert {record["id"] for record in records} == {"acme.ledger"}


def test_stale_and_stopped_reports_no_longer_count(report_body, context, now):
    rows = [
        _record(report_body, now, failures=[FAILURE], age=600),
        _record(report_body, now, service_type="control", failures=[FAILURE], lifecycle="stopped"),
    ]

    assert background_module_failures(_components(rows, context, now)) == []


def test_unavailable_components_report_nothing():
    assert background_module_failures(None) == []


def test_monitoring_summary_lists_worker_and_app_failures_in_one_degraded_check(
    report_body, context, now
):
    components = _components(
        [_record(report_body, now, service_type="worker", failures=[FAILURE])], context, now
    )
    app_failure = {**FAILURE, "id": "acme.reports", "version": "0.3.0"}

    payload = build_monitoring_summary_payload(
        readiness={"ready": True, "checks": {}},
        meta={},
        components=components,
        platform_module_failures=[app_failure],
    )

    check = payload["checks"]["platform_modules"]
    assert check["status"] == "degraded"
    assert check["error"] == (
        "acme.reports 0.3.0: Failed to load: ImportError: No module named "
        "'acme_ledger_backend'; acme.ledger 1.2.0 (worker role): Failed to load: "
        "ImportError: No module named 'acme_ledger_backend'"
    )
    assert [record.get("role") for record in check["failed_modules"]] == [None, "worker"]
    assert payload["status"] == "ready"
    assert payload["degraded"] is True
    assert payload["summary"]["degraded_checks"] == ["platform_modules"]
    assert payload["summary"]["checks_failed"] == 0


def test_a_control_failure_alone_marks_the_summary_degraded(report_body, context, now):
    components = _components(
        [_record(report_body, now, service_type="control", failures=[FAILURE])], context, now
    )

    payload = build_monitoring_summary_payload(
        readiness={"ready": True, "checks": {}}, meta={}, components=components
    )

    assert payload["summary"]["checks_degraded"] == 1
    assert "(control role)" in payload["checks"]["platform_modules"]["error"]


def test_no_failures_anywhere_adds_no_check(report_body, context, now):
    components = _components([_record(report_body, now)], context, now)

    payload = build_monitoring_summary_payload(
        readiness={"ready": True, "checks": {}},
        meta={},
        components=components,
        platform_module_failures=[],
    )

    assert "platform_modules" not in payload["checks"]
    assert "degraded" not in payload


JOB_FAILURE = {
    "id": "acme.ledger",
    "version": "1.2.0",
    "status": "job_failed",
    "load_error": "job sync: RuntimeError: upstream unavailable",
}


def test_a_source_reads_a_callable_at_every_report_and_carries_the_job_status(context, now):
    current: list[dict] = []
    source = RuntimeObservationSource(
        service_type="control", platform_module_failures=lambda: [FAILURE, *current]
    )

    first = build_observation_reports((source.observe(),), context=context, published_at=now)
    current.append({**JOB_FAILURE, "id": "acme.sync"})
    second = build_observation_reports((source.observe(),), context=context, published_at=now)

    assert [item.id for item in first[0].payload.platform_module_failures] == ["acme.ledger"]
    failures = second[0].payload.platform_module_failures
    assert [(item.id, item.status) for item in failures] == [
        ("acme.ledger", "failed"),
        ("acme.sync", "job_failed"),
    ]


def test_a_failure_list_that_cannot_be_read_never_breaks_the_report(context, now):
    def broken():
        raise RuntimeError("boom")

    source = RuntimeObservationSource(service_type="control", platform_module_failures=broken)

    reports = build_observation_reports((source.observe(),), context=context, published_at=now)

    assert reports[0].payload.platform_module_failures == ()


def test_a_failing_background_job_is_named_as_such_on_the_monitoring_page(
    report_body, context, now
):
    components = _components(
        [_record(report_body, now, service_type="control", failures=[JOB_FAILURE])], context, now
    )

    payload = build_monitoring_summary_payload(
        readiness={"ready": True, "checks": {}}, meta={}, components=components
    )

    check = payload["checks"]["platform_modules"]
    assert check["error"] == (
        "acme.ledger 1.2.0 (control role): Background job failing: "
        "job sync: RuntimeError: upstream unavailable"
    )
    assert check["failed_modules"][0]["status"] == "job_failed"
    assert payload["status"] == "ready"
