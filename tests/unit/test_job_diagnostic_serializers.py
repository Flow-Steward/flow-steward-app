from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from core.application.management.snapshot_serializers import serialize_job_error_row


def test_job_error_serializer_preserves_safe_diagnostic_ids() -> None:
    payload = serialize_job_error_row(
        {
            "id": 1,
            "job_id": 42,
            "provider_error_code": "rate_limit",
            "provider_error_type": "rate_limit_error",
            "request_id": "req_abc-123",
            "context_jsonb": {"trace_id": "job:42", "correlation_id": "corr-safe-1"},
            "created_at": datetime(2026, 4, 22, tzinfo=UTC),
        },
        to_json_obj=lambda value: value,
    )

    assert payload["request_id"] == "req_abc-123"
    assert payload["provider_error_code"] == "rate_limit"
    assert payload["context_jsonb"]["trace_id"] == "job:42"
    assert payload["context_jsonb"]["correlation_id"] == "corr-safe-1"


def test_job_error_serializer_redacts_unsafe_diagnostic_ids() -> None:
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="redact", redacted_text="[REDACTED]"),
    ):
        payload = serialize_job_error_row(
            {
                "id": 2,
                "job_id": 43,
                "provider_error_code": "api_key=abcdefghijklmnopqrstuvwxyz",
                "request_id": "postgres://admin:secret@db/app",
                "context_jsonb": {"trace_id": "postgres://admin:secret@db/app"},
                "created_at": datetime(2026, 4, 22, tzinfo=UTC),
            },
            to_json_obj=lambda value: value,
        )

    assert payload["request_id"] == "[REDACTED_DIAGNOSTIC_ID]"
    assert payload["provider_error_code"] == "[REDACTED_DIAGNOSTIC_ID]"
    assert payload["context_jsonb"]["trace_id"] == "[REDACTED_DIAGNOSTIC_ID]"
