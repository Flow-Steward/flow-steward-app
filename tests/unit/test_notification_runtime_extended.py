"""
Unit tests for integrations/notification_runtime.py — notification routing and delivery.

Tests:
- Severity filtering
- Event type pattern matching
- Message rendering
- Test mode gating
- Extension resolution

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_notification_runtime_extended.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.application.extensions.install import ExtensionInstallRecord
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _selected_notification_install(
    store,
    *,
    runtime_row: dict | None,
    extension_key: str = "fixture.notification-provider",
):
    """Expose one selected install and its exact runtime-gate projection."""
    install_id = f"install-{extension_key}"
    release_id = f"release-{extension_key}"
    store.conn = object()
    store.get_extension_by_install_id.return_value = (
        {
            **runtime_row,
            "extension_key": extension_key,
            "extension_id": extension_key,
            "extension_install_id": install_id,
            "install_id": install_id,
            "release_id": release_id,
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        if runtime_row is not None
        else None
    )
    install_store = MagicMock()
    install_store.list_for_account.return_value = [
        ExtensionInstallRecord(
            extension_id=extension_key,
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            install_id=install_id,
            release_id=release_id,
        )
    ]
    return patch(
        "core.application.extensions.install.DBExtensionInstallStore",
        return_value=install_store,
    )


# ---------------------------------------------------------------------------
# Tests: _severity_allows
# ---------------------------------------------------------------------------


class TestSeverityFiltering:
    """Severity-based route filtering."""

    def test_any_severity_passes_all(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("any", "debug") is True
        assert _severity_allows("any", "critical") is True

    def test_error_route_blocks_info_event(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("error", "info") is False

    def test_error_route_allows_error_event(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("error", "error") is True

    def test_error_route_allows_critical_event(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("error", "critical") is True

    def test_warning_route_blocks_info(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("warning", "info") is False

    def test_warning_route_allows_warning(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("warning", "warning") is True

    def test_info_route_allows_info(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("info", "info") is True

    def test_info_route_blocks_debug(self):
        from core.application.notifications.contracts import _severity_allows

        assert _severity_allows("info", "debug") is False

    def test_empty_severity_defaults_to_any(self):
        from core.application.notifications.contracts import _severity_allows

        # Empty route severity normalizes to "any"
        assert _severity_allows("", "debug") is True

    def test_unknown_severity_defaults(self):
        from core.application.notifications.contracts import _severity_allows

        # Unknown severity normalizes to info-level
        assert _severity_allows("unknown_level", "error") is True


# ---------------------------------------------------------------------------
# Tests: _normalize_severity
# ---------------------------------------------------------------------------


class TestNormalizeSeverity:
    """Severity normalization."""

    def test_known_severity_returns_lowercase(self):
        from core.application.notifications.contracts import _normalize_severity

        assert _normalize_severity("ERROR") == "error"
        assert _normalize_severity("Warning") == "warning"

    def test_unknown_severity_returns_default(self):
        from core.application.notifications.contracts import _normalize_severity

        assert _normalize_severity("garbage") == "info"

    def test_none_returns_default(self):
        from core.application.notifications.contracts import _normalize_severity

        assert _normalize_severity(None) == "info"

    def test_empty_string_returns_default(self):
        from core.application.notifications.contracts import _normalize_severity

        assert _normalize_severity("") == "info"

    def test_custom_default(self):
        from core.application.notifications.contracts import _normalize_severity

        assert _normalize_severity("garbage", default="warning") == "warning"


# ---------------------------------------------------------------------------
# Tests: _event_type_matches
# ---------------------------------------------------------------------------


class TestEventTypeMatching:
    """Event type pattern matching for notification routes."""

    def test_wildcard_matches_all(self):
        from core.application.notifications.contracts import _event_type_matches

        assert _event_type_matches("*", "job.completed") is True
        assert _event_type_matches("*", "error.fatal") is True

    def test_any_keyword_matches_all(self):
        from core.application.notifications.contracts import _event_type_matches

        assert _event_type_matches("any", "job.completed") is True

    def test_empty_route_matches_all(self):
        from core.application.notifications.contracts import _event_type_matches

        assert _event_type_matches("", "job.completed") is True

    def test_exact_match(self):
        from core.application.notifications.contracts import _event_type_matches

        assert _event_type_matches("job.completed", "job.completed") is True
        assert _event_type_matches("job.completed", "job.failed") is False

    def test_glob_pattern_with_wildcard(self):
        from core.application.notifications.contracts import _event_type_matches

        assert _event_type_matches("job.*", "job.completed") is True
        assert _event_type_matches("job.*", "job.failed") is True
        assert _event_type_matches("job.*", "error.fatal") is False

    def test_glob_pattern_with_question_mark(self):
        from core.application.notifications.contracts import _event_type_matches

        assert _event_type_matches("job.?ompleted", "job.completed") is True
        assert _event_type_matches("job.?ompleted", "job.failed") is False

    def test_no_pattern_chars_exact_match_required(self):
        from core.application.notifications.contracts import _event_type_matches

        assert _event_type_matches("job.completed", "job.completed") is True
        assert _event_type_matches("job.completed", "JOB.COMPLETED") is False


# ---------------------------------------------------------------------------
# Tests: _render_notification_message
# ---------------------------------------------------------------------------


class TestNotificationRendering:
    """Notification message rendering."""

    def test_explicit_message_from_payload(self):
        from core.application.notifications.contracts import _render_notification_message

        event = {"payload": {"message": "Custom notification"}, "event_type": "test"}
        result = _render_notification_message(event, {})
        assert result == "Custom notification"

    def test_builds_from_event_fields(self):
        from core.application.notifications.contracts import _render_notification_message

        event = {
            "event_type": "job.completed",
            "severity": "info",
            "job_id": 42,
            "workflow_id": "bugfix",
            "payload": {"summary": "Login bug fixed"},
        }
        result = _render_notification_message(event, {})
        assert "[INFO]" in result
        assert "job.completed" in result
        assert "job_id=42" in result
        assert "workflow_id=bugfix" in result
        assert "Login bug fixed" in result

    def test_missing_payload_still_renders(self):
        from core.application.notifications.contracts import _render_notification_message

        event = {"event_type": "test", "severity": "warning", "payload": {}}
        result = _render_notification_message(event, {})
        assert "[WARNING]" in result
        assert "test" in result

    def test_route_config_message_suffix(self):
        from core.application.notifications.contracts import _render_notification_message

        event = {"event_type": "test", "severity": "info", "payload": {}}
        route = {"route_config": {"message_suffix": "— Sent by Flow Steward"}}
        result = _render_notification_message(event, route)
        assert "Sent by Flow Steward" in result

    def test_none_job_id_omitted(self):
        from core.application.notifications.contracts import _render_notification_message

        event = {"event_type": "test", "severity": "info", "payload": {}}
        result = _render_notification_message(event, {})
        assert "job_id" not in result

    def test_payload_text_field_used_as_summary(self):
        from core.application.notifications.contracts import _render_notification_message

        event = {"event_type": "test", "severity": "info", "payload": {"text": "Details here"}}
        result = _render_notification_message(event, {})
        assert "Details here" in result


# ---------------------------------------------------------------------------
# Tests: _is_test_mode_dispatch
# ---------------------------------------------------------------------------


class TestTestModeGate:
    """Test mode gating for notification dispatch."""

    def test_global_test_mode_returns_true(self):
        from core.application.notifications.contracts import _is_test_mode_dispatch

        with patch(
            "core.application.notifications.contracts.is_test_mode_enabled", return_value=True
        ):
            assert _is_test_mode_dispatch(None, {}) is True

    def test_event_test_mode_flag_returns_true(self):
        from core.application.notifications.contracts import _is_test_mode_dispatch

        with patch(
            "core.application.notifications.contracts.is_test_mode_enabled", return_value=False
        ):
            event = {"test_mode": True}
            assert _is_test_mode_dispatch(None, event) is True

    def test_payload_test_mode_flag_returns_true(self):
        from core.application.notifications.contracts import _is_test_mode_dispatch

        with patch(
            "core.application.notifications.contracts.is_test_mode_enabled", return_value=False
        ):
            event = {"payload": {"test_mode": True}}
            assert _is_test_mode_dispatch(None, event) is True

    def test_job_meta_test_mode_returns_true(self):
        from core.application.notifications.contracts import _is_test_mode_dispatch

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {"meta": {"test_mode": True}}
        with patch(
            "core.application.notifications.contracts.is_test_mode_enabled", return_value=False
        ):
            event = {"job_id": 42, "payload": {}}
            assert _is_test_mode_dispatch(mock_bl, event) is True

    def test_no_test_mode_returns_false(self):
        from core.application.notifications.contracts import _is_test_mode_dispatch

        mock_bl = MagicMock()
        mock_bl.get_job.return_value = {"meta": {}}
        with patch(
            "core.application.notifications.contracts.is_test_mode_enabled", return_value=False
        ):
            event = {"job_id": 42, "payload": {}}
            assert _is_test_mode_dispatch(mock_bl, event) is False

    def test_missing_bl_returns_false(self):
        from core.application.notifications.contracts import _is_test_mode_dispatch

        with patch(
            "core.application.notifications.contracts.is_test_mode_enabled", return_value=False
        ):
            event = {"job_id": 42, "payload": {}}
            assert _is_test_mode_dispatch(None, event) is False


# ---------------------------------------------------------------------------
# Tests: _resolve_notification_extension
# ---------------------------------------------------------------------------


class TestResolveNotificationExtension:
    """Extension resolution for notification delivery."""

    def test_resolves_enabled_extension(self):
        from core.application.notifications.delivery import _resolve_notification_extension

        mock_bl = MagicMock()
        provider = {
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
        }
        with _selected_notification_install(
            mock_bl,
            runtime_row={"enabled": True, "kind": "notification_provider"},
        ):
            result = _resolve_notification_extension(mock_bl, provider)
        assert result["enabled"] is True

    def test_raises_on_missing_extension(self):
        from core.application.notifications.delivery import _resolve_notification_extension

        mock_bl = MagicMock()
        provider = {
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
        }
        with (
            _selected_notification_install(mock_bl, runtime_row=None),
            pytest.raises(ValueError, match="not found"),
        ):
            _resolve_notification_extension(mock_bl, provider)

    def test_raises_on_disabled_extension(self):
        from core.application.notifications.delivery import _resolve_notification_extension

        mock_bl = MagicMock()
        provider = {
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
        }
        with (
            _selected_notification_install(
                mock_bl,
                runtime_row={"enabled": False, "kind": "notification_provider"},
            ),
            pytest.raises(ValueError, match="disabled"),
        ):
            _resolve_notification_extension(mock_bl, provider)

    def test_raises_on_wrong_kind(self):
        from core.application.notifications.delivery import _resolve_notification_extension

        mock_bl = MagicMock()
        provider = {
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
        }
        with (
            _selected_notification_install(
                mock_bl,
                runtime_row={"enabled": True, "kind": "data_source"},
            ),
            pytest.raises(ValueError, match="does not match"),
        ):
            _resolve_notification_extension(mock_bl, provider)

    def test_raises_on_missing_extension_key(self):
        from core.application.notifications.delivery import _resolve_notification_extension

        mock_bl = MagicMock()
        provider = {"provider_key": "", "provider_config": {}}
        with pytest.raises(ValueError, match="missing extension_key"):
            _resolve_notification_extension(mock_bl, provider)

    def test_raises_without_install_store_capability(self):
        from core.application.notifications.delivery import _resolve_notification_extension

        mock_bl = MagicMock(spec=[])  # no connection-backed install store
        provider = {
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
        }
        with pytest.raises(RuntimeError, match="install store is not available"):
            _resolve_notification_extension(mock_bl, provider)


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for _as_dict, _as_str helpers."""

    def test_as_dict_with_dict(self):
        from core.application.notifications.contracts import _as_dict

        assert _as_dict({"a": 1}) == {"a": 1}

    def test_as_dict_with_non_dict(self):
        from core.application.notifications.contracts import _as_dict

        assert _as_dict("string") == {}
        assert _as_dict(None) == {}
        assert _as_dict(42) == {}
        assert _as_dict([1, 2]) == {}

    def test_as_str_with_string(self):
        from core.application.notifications.contracts import _as_str

        assert _as_str("hello") == "hello"

    def test_as_str_strips_whitespace(self):
        from core.application.notifications.contracts import _as_str

        assert _as_str("  hello  ") == "hello"

    def test_as_str_with_none(self):
        from core.application.notifications.contracts import _as_str

        assert _as_str(None) == ""

    def test_as_str_with_number(self):
        from core.application.notifications.contracts import _as_str

        assert _as_str(42) == "42"
