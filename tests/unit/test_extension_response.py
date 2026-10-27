"""Tests for integrations.extension_response — canonical v1.1 response envelope."""

from __future__ import annotations

from core.infrastructure.db.extension_response import (
    KNOWN_COMMAND_CATALOG,
    filter_valid_commands,
    filter_valid_events,
    normalize_extension_response,
    validate_response_commands,
    validate_response_events,
)

# ---------------------------------------------------------------------------
# normalize_extension_response
# ---------------------------------------------------------------------------


class TestNormalizeExtensionResponse:
    def test_canonical_response_passthrough(self):
        raw = {
            "ok": True,
            "result": {"data": 1},
            "commands": [{"command_type": "resume_job", "target": {"job_id": 1}}],
            "events": [{"event_type": "job.completed"}],
            "reload": ["overview"],
            "errors": [],
        }
        env = normalize_extension_response(raw)
        assert env.ok is True
        assert env.result == {"data": 1}
        assert len(env.commands) == 1
        assert env.commands[0]["command_type"] == "resume_job"
        assert len(env.events) == 1
        assert env.reload == ["overview"]
        assert env.errors == []

    def test_missing_ok_marks_response_invalid(self):
        raw = {"result": {"data": 1}}
        env = normalize_extension_response(raw)
        assert env.ok is False
        assert env.result == {"data": 1}
        assert env.errors[0]["code"] == "missing_ok"

    def test_ok_false_without_errors_gets_default_error(self):
        raw = {"ok": False, "result": {}}
        env = normalize_extension_response(raw)
        assert env.ok is False
        assert env.errors == [{"code": "extension_error", "message": "Extension returned ok=false"}]

    def test_ok_false_without_errors_uses_top_level_error_message_when_present(self):
        raw = {"ok": False, "error": "missing_api_token"}
        env = normalize_extension_response(raw)
        assert env.ok is False
        assert env.errors == [{"code": "extension_error", "message": "missing_api_token"}]

    def test_result_must_be_object(self):
        raw = {"ok": True, "result": ["invalid"]}
        env = normalize_extension_response(raw)
        assert env.ok is True
        assert env.result == {}

    def test_non_dict_entries_are_filtered_from_arrays(self):
        raw = {
            "ok": True,
            "commands": [{"command_type": "resume_job"}, "bad"],
            "events": [{"event_type": "job.completed"}, 7],
            "errors": [{"code": "x", "message": "y"}, "bad"],
        }
        env = normalize_extension_response(raw)
        assert env.commands == [{"command_type": "resume_job"}]
        assert env.events == [{"event_type": "job.completed"}]
        assert env.errors == [{"code": "x", "message": "y"}]

    def test_reload_normalization_trims_and_drops_empty_values(self):
        raw = {"ok": True, "reload": ["overview", " ", "", None, "queue"]}
        env = normalize_extension_response(raw)
        assert env.reload == ["overview", "queue"]

    def test_empty_response_returns_error(self):
        env = normalize_extension_response(None)
        assert env.ok is False
        assert len(env.errors) == 1
        assert env.errors[0]["code"] == "empty_response"

    def test_empty_dict_response(self):
        env = normalize_extension_response({})
        assert env.ok is False
        assert env.result == {}
        assert env.commands == []
        assert env.events == []
        assert env.errors[0]["code"] == "missing_ok"

    def test_extra_fields_preserved(self):
        raw = {"ok": True, "custom_field": "hello", "result": {}}
        env = normalize_extension_response(raw)
        assert env.custom_field == "hello"


# ---------------------------------------------------------------------------
# validate_response_commands
# ---------------------------------------------------------------------------


class TestValidateResponseCommands:
    def test_valid_command_no_errors(self):
        commands = [{"command_type": "resume_job", "target": {"job_id": 1}}]
        errors = validate_response_commands(commands)
        assert errors == []

    def test_unknown_command_type_produces_error(self):
        commands = [{"command_type": "destroy_everything"}]
        errors = validate_response_commands(commands)
        assert len(errors) == 1
        assert errors[0]["code"] == "unknown_command_type"
        assert errors[0]["command_type"] == "destroy_everything"

    def test_missing_command_type_produces_error(self):
        commands = [{"target": {"job_id": 1}}]
        errors = validate_response_commands(commands)
        assert len(errors) == 1
        assert errors[0]["code"] == "missing_command_type"

    def test_non_dict_command_produces_error(self):
        commands = ["not_a_dict"]
        errors = validate_response_commands(commands)
        assert len(errors) == 1
        assert errors[0]["code"] == "invalid_command"

    def test_custom_allowed_types(self):
        commands = [{"command_type": "custom_action"}]
        errors = validate_response_commands(commands, allowed_types=["custom_action"])
        assert errors == []

    def test_all_catalog_types_accepted(self):
        commands = [{"command_type": ct} for ct in KNOWN_COMMAND_CATALOG]
        errors = validate_response_commands(commands)
        assert errors == []


# ---------------------------------------------------------------------------
# validate_response_events
# ---------------------------------------------------------------------------


class TestValidateResponseEvents:
    def test_declared_event_type_no_errors(self):
        events = [{"event_type": "job.completed"}]
        errors = validate_response_events(events, ["job.completed", "job.failed"])
        assert errors == []

    def test_undeclared_event_type_produces_error(self):
        events = [{"event_type": "secret.leaked"}]
        errors = validate_response_events(events, ["job.completed"])
        assert len(errors) == 1
        assert errors[0]["code"] == "undeclared_event_type"

    def test_missing_event_type_produces_error(self):
        events = [{"payload": {}}]
        errors = validate_response_events(events, ["job.completed"])
        assert len(errors) == 1
        assert errors[0]["code"] == "missing_event_type"

    def test_non_dict_event_produces_error(self):
        events = [42]
        errors = validate_response_events(events, ["job.completed"])
        assert len(errors) == 1
        assert errors[0]["code"] == "invalid_event"


# ---------------------------------------------------------------------------
# filter helpers
# ---------------------------------------------------------------------------


class TestFilterHelpers:
    def test_filter_valid_commands_keeps_known(self):
        commands = [
            {"command_type": "resume_job"},
            {"command_type": "nope"},
            {"command_type": "cancel_job"},
        ]
        valid = filter_valid_commands(commands)
        assert len(valid) == 2
        assert valid[0]["command_type"] == "resume_job"
        assert valid[1]["command_type"] == "cancel_job"

    def test_filter_valid_events_keeps_declared(self):
        events = [
            {"event_type": "alert.fired"},
            {"event_type": "secret.leaked"},
        ]
        valid = filter_valid_events(events, ["alert.fired"])
        assert len(valid) == 1
        assert valid[0]["event_type"] == "alert.fired"

    def test_filter_valid_commands_drops_non_dict(self):
        commands = [{"command_type": "resume_job"}, "bad"]
        valid = filter_valid_commands(commands)
        assert len(valid) == 1

    def test_filter_valid_events_drops_non_dict(self):
        events = [{"event_type": "alert.fired"}, None]
        valid = filter_valid_events(events, ["alert.fired"])
        assert len(valid) == 1
