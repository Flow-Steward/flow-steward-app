"""A scanner's invalid JSON replacement must not break report downloads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.responses import JSONResponse

from core.application.reports.job_diagnostic_report import build_job_diagnostic_report
from core.infrastructure.security.secret_detection import redact_detected_secrets


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "id",
            UUID("a417f5b8-26b3-41bc-a495-5396f502bb5b"),
            "a417f5b8-26b3-41bc-a495-5396f502bb5b",
        ),
        ("started_at", datetime(2026, 10, 4, 12, 30, tzinfo=UTC), "2026-10-04 12:30:00+00:00"),
        ("total_cost", Decimal("0.1250"), "0.1250"),
    ],
    ids=["uuid", "datetime", "decimal"],
)
def test_report_fallback_redacts_and_renders_database_scalars(monkeypatch, field, value, expected):
    marker = "diagnostic-fixture-private-marker"

    def scan_with_invalid_bundle_replacement(text, *_args, **_kwargs):
        # Control only the scanner decision. Keep both production redactors,
        # the report builder and the response serializer real. Removing the
        # closing brace models a custom replacement that invalidates JSON;
        # subsequent fallback leaf scans preserve their text.
        if text.startswith('{"schema_version":'):
            return SimpleNamespace(action="redact", redacted_text=text[:-1])
        return SimpleNamespace(action="allow", redacted_text=text)

    monkeypatch.setattr(
        "core.infrastructure.security.integration.scan_outbound",
        scan_with_invalid_bundle_replacement,
    )
    report = build_job_diagnostic_report(
        job_id="a417f5b8-26b3-41bc-a495-5396f502bb5b",
        project_id="project-fixture",
        job={
            "id": "a417f5b8-26b3-41bc-a495-5396f502bb5b",
            field: value,
            "invocation": {"password": marker},
        },
        detail_contract={},
        messages=[],
        logs=[],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    assert marker not in repr(report)
    response = JSONResponse(report)
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["schema_version"] == "flow-steward.job-diagnostic-report.v1"
    assert payload["job"][field] == expected
    assert marker not in response.body.decode("utf-8")


def test_report_fallback_blocks_free_text_and_preserves_uuid_identifier(monkeypatch):
    marker = "diagnostic-private-free-text"
    request_id = "a417f5b8-26b3-41bc-a495-5396f502bb5b"

    def scan_with_blocked_leaf(text, *_args, **_kwargs):
        if text.startswith('{"schema_version":'):
            return SimpleNamespace(action="redact", redacted_text=text[:-1])
        if marker in text:
            # A block decision must withhold the leaf even if its replacement
            # carries raw text. Only the real leaf redactor enforces this.
            return SimpleNamespace(action="block", redacted_text=text)
        return SimpleNamespace(action="allow", redacted_text=text)

    monkeypatch.setattr(
        "core.infrastructure.security.integration.scan_outbound",
        scan_with_blocked_leaf,
    )
    report = build_job_diagnostic_report(
        job_id="job-fixture",
        project_id="project-fixture",
        job={"id": "job-fixture"},
        detail_contract={},
        messages=[],
        logs=[{"request_id": request_id, "detail": f"provider returned {marker}"}],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    response = JSONResponse(report)
    payload = json.loads(response.body)
    assert payload["logs"][0]["detail"] == "[BLOCKED: security policy]"
    assert payload["logs"][0]["request_id"] == request_id
    assert marker not in response.body.decode("utf-8")


def test_report_fallback_redacts_canonical_token_when_leaf_scanner_allows(monkeypatch):
    token = "ghp_" + "x" * 36

    def scan_with_invalid_bundle_replacement(text, *_args, **_kwargs):
        if text.startswith('{"schema_version":'):
            return SimpleNamespace(action="redact", redacted_text=text[:-1])
        return SimpleNamespace(action="allow", redacted_text=text)

    monkeypatch.setattr(
        "core.infrastructure.security.integration.scan_outbound",
        scan_with_invalid_bundle_replacement,
    )
    report = build_job_diagnostic_report(
        job_id="job-fixture",
        project_id="project-fixture",
        job={"id": "job-fixture"},
        detail_contract={},
        messages=[],
        logs=[
            {
                "detail": f"provider diagnostic contains {token}",
                "context": {token: "fixture diagnostic"},
            }
        ],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )
    response = JSONResponse(report)
    assert token not in response.body.decode("utf-8")
    log = json.loads(response.body)["logs"][0]
    assert log["detail"] == redact_detected_secrets(f"provider diagnostic contains {token}")
    assert log["context"] == {redact_detected_secrets(token): "fixture diagnostic"}


def test_oversized_report_fallback_renders_nested_typed_values_and_redacts(monkeypatch):
    marker = "oversized-diagnostic-private-marker"

    def scan_with_invalid_row_replacement(text, *_args, **_kwargs):
        # The total report and messages list exceed the scan split threshold;
        # each smaller row then exercises the same invalid-JSON fallback.
        if text.startswith('{"id": "message-'):
            return SimpleNamespace(action="redact", redacted_text=text[:-1])
        if marker in text:
            return SimpleNamespace(action="block", redacted_text=text)
        return SimpleNamespace(action="allow", redacted_text=text)

    monkeypatch.setattr(
        "core.infrastructure.security.integration.scan_outbound",
        scan_with_invalid_row_replacement,
    )
    report = build_job_diagnostic_report(
        job_id="job-fixture",
        project_id="project-fixture",
        job={"id": "job-fixture"},
        detail_contract={},
        messages=[
            {
                "id": f"message-{index}",
                "payload": {
                    "text": "x" * 180_000,
                    "cost": Decimal("0.1250"),
                    "detail": marker,
                    "password": marker,
                },
            }
            for index in range(3)
        ],
        logs=[],
        events=[],
        errors=[],
        runtime_journal=[],
        causal_chain={},
        decisions=[],
        cost_breakdown={},
    )

    assert marker not in repr(report)
    response = JSONResponse(report)
    payload = json.loads(response.body)
    assert [row["id"] for row in payload["messages"]] == [
        "message-0",
        "message-1",
        "message-2",
    ]
    for row in payload["messages"]:
        assert row["payload"]["text"] == "x" * 180_000
        assert row["payload"]["cost"] == "0.1250"
        assert row["payload"]["detail"] == "[BLOCKED: security policy]"
    assert marker not in response.body.decode("utf-8")
