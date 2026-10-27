from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest


def test_new_service_name_needs_no_catalog(context, report_body, now):
    from core.application.monitoring.contracts import decode_heartbeat

    result = decode_heartbeat(
        report_body(service_type="image-indexer"),
        context=context,
        received_at=now,
        stale_seconds=180,
    )
    assert result.service_type == "image-indexer"
    assert result.incarnation_id == UUID("9ee9238d-23e9-4f78-847d-d5652e5b521b")


@pytest.mark.parametrize(
    "changes",
    [
        {"installation_id": "foreign"},
        {"deployment_profile": "compact"},
        {"service_type": ""},
        {"service_type": "a" * 129},
        {"service_type": "Invalid Name"},
        {"source": ""},
        {"sequence": 0},
        {"sequence": True},
        {"schema_version": 2},
        {"observed_at": "2026-10-01T12:00:00"},
        {"payload": {"type": "runtime_status", "version": 1, "health": True}},
        {"payload": {"type": "settings", "version": 1}},
    ],
)
def test_invalid_reports_are_rejected(changes, context, report_body, now):
    from core.application.monitoring.contracts import decode_heartbeat

    body = report_body(**changes)
    with pytest.raises(ValueError):
        decode_heartbeat(body, context=context, received_at=now, stale_seconds=180)


@pytest.mark.parametrize("offset,valid", [(30, True), (31, False), (-180, True), (-181, False)])
def test_report_age_boundaries(offset, valid, context, report_body, now):
    from core.application.monitoring.contracts import decode_heartbeat

    body = report_body(observed_at=(now + timedelta(seconds=offset)).isoformat())
    if valid:
        assert (
            decode_heartbeat(body, context=context, received_at=now, stale_seconds=180).sequence
            == 1
        )
    else:
        with pytest.raises(ValueError):
            decode_heartbeat(body, context=context, received_at=now, stale_seconds=180)


def test_oversized_message_is_rejected_before_decoding(context, now):
    from core.application.monitoring.contracts import decode_heartbeat

    with pytest.raises(ValueError):
        decode_heartbeat(b" " * 16385, context=context, received_at=now, stale_seconds=180)


def test_worker_mapping_preserves_full_opaque_identity(context):
    from core.application.monitoring.contracts import monitoring_worker_incarnation

    worker_id = "host-abc123def456-42"
    first = monitoring_worker_incarnation(context.installation_id, "worker", worker_id)
    assert first == monitoring_worker_incarnation(context.installation_id, "worker", worker_id)
    assert first != monitoring_worker_incarnation(
        context.installation_id, "worker", "host-fff123def456-42"
    )
    assert first != monitoring_worker_incarnation("another-install", "worker", worker_id)
    assert worker_id == "host-abc123def456-42"


def test_duplicate_json_keys_are_rejected(context, report_body, now):
    from core.application.monitoring.contracts import decode_heartbeat

    body = report_body().replace(b'"sequence": 1', b'"sequence": 2, "sequence": 1')
    with pytest.raises(ValueError):
        decode_heartbeat(body, context=context, received_at=now, stale_seconds=180)


@pytest.mark.parametrize(
    "health",
    [
        {"state": "healthy"},
        {"state": "healthy", "checked_at": "2026-10-01T12:00:00", "evidence": "protocol"},
        {"state": "healthy", "checked_at": "2026-10-01T12:01:00Z", "evidence": "protocol"},
        {"state": "not_observed", "evidence": "protocol"},
    ],
)
def test_health_cannot_be_asserted_without_valid_check(health, context, report_body, now):
    import json

    from core.application.monitoring.contracts import decode_heartbeat

    data = json.loads(report_body())
    data["payload"]["health"] = health
    body = json.dumps(data).encode()
    with pytest.raises(ValueError):
        decode_heartbeat(body, context=context, received_at=now, stale_seconds=180)


@pytest.mark.parametrize("field", ["schema_version", "type", "version"])
def test_version_and_payload_type_must_be_explicit(field, context, report_body, now):
    import json

    from core.application.monitoring.contracts import decode_heartbeat

    data = json.loads(report_body())
    if field == "schema_version":
        del data[field]
    else:
        del data["payload"][field]
    body = json.dumps(data).encode()
    with pytest.raises(ValueError):
        decode_heartbeat(body, context=context, received_at=now, stale_seconds=180)
