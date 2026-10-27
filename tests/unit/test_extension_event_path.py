"""Tests for extension event emission path — Phase 4."""

from __future__ import annotations

from core.infrastructure.db.extension_response import (
    filter_valid_events,
    normalize_extension_response,
    validate_response_events,
)


class TestResponseEventsProcessing:
    def test_response_events_present_in_normalized_envelope(self):
        raw = {
            "ok": True,
            "events": [
                {"event_type": "alert.fired", "severity": "warning", "summary": "Test alert"},
            ],
        }
        env = normalize_extension_response(raw)
        assert len(env.events) == 1
        assert env.events[0]["event_type"] == "alert.fired"
        assert env.events[0]["severity"] == "warning"

    def test_undeclared_event_type_rejected_by_filter(self):
        events = [
            {"event_type": "alert.fired"},
            {"event_type": "secret.leaked"},
        ]
        valid = filter_valid_events(events, declared_event_types=["alert.fired"])
        assert len(valid) == 1
        assert valid[0]["event_type"] == "alert.fired"

    def test_event_validation_catches_undeclared(self):
        events = [{"event_type": "not.declared"}]
        errors = validate_response_events(events, declared_event_types=["job.completed"])
        assert len(errors) == 1
        assert errors[0]["code"] == "undeclared_event_type"

    def test_event_with_full_metadata(self):
        raw = {
            "ok": True,
            "events": [
                {
                    "event_type": "external.alert.received",
                    "severity": "error",
                    "summary": "Production alert from Sentry",
                    "payload": {"issue_id": "SENTRY-123", "title": "NullPointerException"},
                },
            ],
        }
        env = normalize_extension_response(raw)
        event = env.events[0]
        assert event["severity"] == "error"
        assert event["payload"]["issue_id"] == "SENTRY-123"

    def test_multiple_events_in_response(self):
        raw = {
            "ok": True,
            "events": [
                {"event_type": "alert.fired"},
                {"event_type": "alert.resolved"},
            ],
        }
        env = normalize_extension_response(raw)
        assert len(env.events) == 2

    def test_empty_events_array(self):
        raw = {"ok": True}
        env = normalize_extension_response(raw)
        assert env.events == []
