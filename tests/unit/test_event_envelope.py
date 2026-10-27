from __future__ import annotations

import pytest

from core.application.orchestration.models import (
    build_event_envelope_v1,
    validate_event_envelope_payload,
)


def test_validate_event_envelope_rejects_missing_required_fields():
    model, errors = validate_event_envelope_payload(
        {
            "schema_version": "event_envelope_v1",
            "event_id": "evt_1",
            "event_type": "job.failed",
            "occurred_at": "2026-03-23T10:00:00+00:00",
            "severity": "error",
            "summary": "failed",
            "payload": {},
        }
    )

    assert model is None
    assert any("correlation_id" in item for item in errors)
    assert any("account_id" in item for item in errors)


def test_build_event_envelope_v1_returns_canonical_required_fields():
    envelope = build_event_envelope_v1(
        event_type="job.failed",
        account_id="acc_alpha",
        correlation_id="job:55:job.failed",
        severity="error",
        summary="Job failed",
        payload={"reason": "boom"},
        project_id=7,
        workflow_id="weekly_report",
        job_id=55,
        source="runtime",
    )

    assert envelope["schema_version"] == "event_envelope_v1"
    assert envelope["account_id"] == "acc_alpha"
    assert envelope["event_type"] == "job.failed"
    assert envelope["correlation_id"] == "job:55:job.failed"
    assert envelope["project_id"] == "7"
    assert envelope["workflow_id"] == "weekly_report"
    assert envelope["job_id"] == "55"


def test_build_event_envelope_v1_rejects_invalid_timestamp():
    with pytest.raises(ValueError, match="occurred_at"):
        build_event_envelope_v1(
            event_type="job.failed",
            account_id="acc_alpha",
            correlation_id="corr-1",
            occurred_at="not-a-timestamp",
        )


def test_build_event_envelope_v1_carries_group_and_step_execution_scope():
    envelope = build_event_envelope_v1(
        event_type="job.step_failed",
        account_id="acc_alpha",
        correlation_id="corr-1",
        job_id=55,
        group_id="per_item",
        step_id="send_email",
        step_execution_id="6F1C2A1E-3B9D-4C55-9A0E-2F7F3E8D1B42",
    )

    assert envelope["group_id"] == "per_item"
    assert envelope["step_id"] == "send_email"
    assert envelope["step_execution_id"] == "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42"
    assert "phase_id" not in envelope


def test_build_event_envelope_v1_root_step_has_no_group():
    envelope = build_event_envelope_v1(
        event_type="job.step_completed",
        account_id="acc_alpha",
        correlation_id="corr-2",
        step_id="draft",
    )

    assert envelope["group_id"] is None
    assert envelope["step_execution_id"] is None


def test_build_event_envelope_v1_rejects_a_malformed_step_execution_id():
    with pytest.raises(ValueError, match="step_execution_id"):
        build_event_envelope_v1(
            event_type="job.step_failed",
            account_id="acc_alpha",
            correlation_id="corr-3",
            step_id="send_email",
            step_execution_id="not-a-uuid",
        )
