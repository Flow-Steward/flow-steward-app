"""Unit tests for timeout configuration bounds in the invoke-workflow runtime node.

Covers:
- _coerce_positive_int_env: parsing, invalid input, non-positive coercion
- _resolve_invoke_child_timeout_seconds: 30 s min clamp, cap enforcement, env override
- _resolve_invoke_child_poll_seconds: 1 s min clamp, env override
- _cancel_child_job_on_timeout: flag disabled, already-terminal skip, terminal persistence,
  error message format and truncation
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.orchestration.invoke_workflow_step_node import (
    _cancel_child_job_on_timeout,
    _coerce_positive_int_env,
    _resolve_invoke_child_poll_seconds,
    _resolve_invoke_child_timeout_seconds,
)

_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "max_iterations")


# ---------------------------------------------------------------------------
# _coerce_positive_int_env
# ---------------------------------------------------------------------------


class TestCoercePositiveIntEnv:
    def test_reads_valid_integer(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT_ENV", "42")
        assert _coerce_positive_int_env("_TEST_INT_ENV", 10) == 42

    def test_uses_default_when_var_absent(self, monkeypatch):
        monkeypatch.delenv("_TEST_INT_ENV", raising=False)
        assert _coerce_positive_int_env("_TEST_INT_ENV", 99) == 99

    def test_uses_default_for_non_integer_string(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT_ENV", "not_a_number")
        assert _coerce_positive_int_env("_TEST_INT_ENV", 77) == 77

    def test_uses_default_for_empty_string(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT_ENV", "")
        assert _coerce_positive_int_env("_TEST_INT_ENV", 50) == 50

    def test_uses_default_for_zero(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT_ENV", "0")
        assert _coerce_positive_int_env("_TEST_INT_ENV", 100) == 100

    def test_uses_default_for_negative(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT_ENV", "-5")
        assert _coerce_positive_int_env("_TEST_INT_ENV", 100) == 100

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT_ENV", "  123  ")
        assert _coerce_positive_int_env("_TEST_INT_ENV", 10) == 123

    def test_minimum_valid_value_is_1(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT_ENV", "1")
        assert _coerce_positive_int_env("_TEST_INT_ENV", 50) == 1


# ---------------------------------------------------------------------------
# _resolve_invoke_child_timeout_seconds
# ---------------------------------------------------------------------------


class TestResolveInvokeChildTimeoutSeconds:
    _TIMEOUT_VAR = "FS_INVOKE_CHILD_TIMEOUT_SECONDS"
    _CAP_VAR = "FS_INVOKE_CHILD_TIMEOUT_CAP_SECONDS"

    def _clear(self, monkeypatch):
        monkeypatch.delenv(self._TIMEOUT_VAR, raising=False)
        monkeypatch.delenv(self._CAP_VAR, raising=False)

    def test_default_is_5400(self, monkeypatch):
        self._clear(monkeypatch)
        assert _resolve_invoke_child_timeout_seconds() == 5400

    def test_default_cap_is_7200(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "9000")
        monkeypatch.delenv(self._CAP_VAR, raising=False)
        # 9000 > default cap 7200 → must be capped
        assert _resolve_invoke_child_timeout_seconds() == 7200

    def test_value_below_30_is_clamped_to_30(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "1")
        monkeypatch.delenv(self._CAP_VAR, raising=False)
        assert _resolve_invoke_child_timeout_seconds() == 30

    def test_zero_falls_back_to_default_5400(self, monkeypatch):
        # _coerce_positive_int_env treats 0 as non-positive → returns default 5400.
        # The min-30 clamp then has no effect since 5400 > 30.
        monkeypatch.setenv(self._TIMEOUT_VAR, "0")
        monkeypatch.delenv(self._CAP_VAR, raising=False)
        assert _resolve_invoke_child_timeout_seconds() == 5400

    def test_invalid_string_uses_default_5400(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "garbage")
        monkeypatch.delenv(self._CAP_VAR, raising=False)
        assert _resolve_invoke_child_timeout_seconds() == 5400

    def test_custom_cap_is_respected(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "3600")
        monkeypatch.setenv(self._CAP_VAR, "1800")
        # 3600 > cap 1800 → capped at 1800
        assert _resolve_invoke_child_timeout_seconds() == 1800

    def test_value_within_custom_cap_is_preserved(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "1200")
        monkeypatch.setenv(self._CAP_VAR, "3600")
        assert _resolve_invoke_child_timeout_seconds() == 1200

    def test_value_exactly_at_cap(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "3600")
        monkeypatch.setenv(self._CAP_VAR, "3600")
        assert _resolve_invoke_child_timeout_seconds() == 3600

    def test_very_small_cap_clamps_both_to_30(self, monkeypatch):
        # timeout=1 → clamped to 30; cap=5 → clamped to 30; min(30, 30) = 30
        monkeypatch.setenv(self._TIMEOUT_VAR, "1")
        monkeypatch.setenv(self._CAP_VAR, "5")
        assert _resolve_invoke_child_timeout_seconds() == 30

    def test_invalid_cap_falls_back_to_default_7200(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "3000")
        monkeypatch.setenv(self._CAP_VAR, "bad_cap")
        # cap invalid → default 7200; 3000 < 7200 → 3000
        assert _resolve_invoke_child_timeout_seconds() == 3000

    def test_negative_timeout_uses_default_within_cap(self, monkeypatch):
        monkeypatch.setenv(self._TIMEOUT_VAR, "-100")
        monkeypatch.delenv(self._CAP_VAR, raising=False)
        # invalid → default 5400; max(30, 5400)=5400; cap 7200; result 5400
        assert _resolve_invoke_child_timeout_seconds() == 5400

    def test_result_is_always_at_least_30(self, monkeypatch):
        for val in ("0", "-1", "garbage", ""):
            monkeypatch.setenv(self._TIMEOUT_VAR, val)
            monkeypatch.delenv(self._CAP_VAR, raising=False)
            result = _resolve_invoke_child_timeout_seconds()
            assert result >= 30, f"Expected >= 30 for timeout env '{val}', got {result}"


# ---------------------------------------------------------------------------
# _resolve_invoke_child_poll_seconds
# ---------------------------------------------------------------------------


class TestResolveInvokeChildPollSeconds:
    _VAR = "FS_INVOKE_CHILD_POLL_SECONDS"

    def test_default_is_2(self, monkeypatch):
        monkeypatch.delenv(self._VAR, raising=False)
        assert _resolve_invoke_child_poll_seconds() == 2

    def test_custom_value(self, monkeypatch):
        monkeypatch.setenv(self._VAR, "10")
        assert _resolve_invoke_child_poll_seconds() == 10

    def test_zero_falls_back_to_default_2(self, monkeypatch):
        # _coerce_positive_int_env treats 0 as non-positive → returns default 2.
        # max(1, 2) = 2; result is 2, not 1.
        monkeypatch.setenv(self._VAR, "0")
        assert _resolve_invoke_child_poll_seconds() == 2

    def test_negative_uses_coerced_default_then_clamped(self, monkeypatch):
        # _coerce_positive_int_env("-1", 2) → 2; max(1, 2) = 2
        monkeypatch.setenv(self._VAR, "-1")
        assert _resolve_invoke_child_poll_seconds() == 2

    def test_invalid_string_uses_default(self, monkeypatch):
        monkeypatch.setenv(self._VAR, "invalid")
        assert _resolve_invoke_child_poll_seconds() == 2

    def test_result_always_at_least_1(self, monkeypatch):
        for val in ("0", "-5", "garbage"):
            monkeypatch.setenv(self._VAR, val)
            result = _resolve_invoke_child_poll_seconds()
            assert result >= 1, f"Expected >= 1 for poll env '{val}', got {result}"


# ---------------------------------------------------------------------------
# _cancel_child_job_on_timeout
# ---------------------------------------------------------------------------


class TestCancelChildJobOnTimeout:
    _FLAG_VAR = "FS_INVOKE_CHILD_CANCEL_ON_TIMEOUT"

    @pytest.fixture(autouse=True)
    def _terminal_persistence(self, monkeypatch):
        self.complete_failure = MagicMock(return_value=SimpleNamespace(status="cancelled"))
        service = SimpleNamespace(complete_failure=self.complete_failure)
        factory = MagicMock(return_value=service)
        monkeypatch.setattr(
            "core.application.jobs.terminal_persistence.TerminalPersistenceService",
            factory,
        )
        self.terminal_persistence_factory = factory

    def _make_bl(self, *, child_status: str = "in_progress"):
        bl = MagicMock()
        bl.get_job.return_value = {"id": 123, "status": child_status}
        return bl

    def test_cancels_active_in_progress_job(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status="in_progress")
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is True
        self.terminal_persistence_factory.assert_called_once_with(bl)
        kwargs = self.complete_failure.call_args.kwargs
        assert kwargs["status"] == "cancelled"

    def test_cancels_pending_job(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status="pending")
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is True
        self.complete_failure.assert_called_once()

    @pytest.mark.parametrize("terminal", _TERMINAL_STATUSES)
    def test_skips_already_terminal_job(self, monkeypatch, terminal):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status=terminal)
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is False
        self.complete_failure.assert_not_called()

    def test_returns_false_when_flag_disabled(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "0")
        bl = self._make_bl(child_status="in_progress")
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is False
        self.complete_failure.assert_not_called()

    def test_returns_false_when_flag_is_false_string(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "false")
        bl = self._make_bl(child_status="in_progress")
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is False

    def test_error_includes_timeout_duration(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status="in_progress")
        _cancel_child_job_on_timeout(bl, 123, timeout_seconds=600)
        error_text = self.complete_failure.call_args.kwargs["error_message"]
        assert "600" in error_text

    def test_error_includes_child_workflow_id(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status="in_progress")
        _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300, child_workflow_id="wf_abc123")
        error_text = self.complete_failure.call_args.kwargs["error_message"]
        assert "wf_abc123" in error_text

    def test_error_truncated_to_2000_chars(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status="in_progress")
        long_wf_id = "x" * 3000
        _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300, child_workflow_id=long_wf_id)
        error_text = self.complete_failure.call_args.kwargs["error_message"]
        assert len(error_text) <= 2000

    def test_uses_terminal_persistence_when_direct_update_methods_are_absent(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = SimpleNamespace(get_job=lambda job_id: {"id": 123, "status": "in_progress"})
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is True
        self.terminal_persistence_factory.assert_called_once_with(bl)
        self.complete_failure.assert_called_once()

    def test_returns_false_when_terminal_persistence_rejects_the_write(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status="in_progress")
        self.complete_failure.side_effect = RuntimeError("terminal conflict")
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is False

    def test_returns_false_when_bl_has_no_get_job(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = SimpleNamespace()  # no get_job — can't determine terminal status
        result = _cancel_child_job_on_timeout(bl, 123, timeout_seconds=300)
        assert result is False

    def test_job_id_preserved_as_opaque_string(self, monkeypatch):
        monkeypatch.setenv(self._FLAG_VAR, "1")
        bl = self._make_bl(child_status="in_progress")
        _cancel_child_job_on_timeout(bl, "123", timeout_seconds=300)  # type: ignore[arg-type]
        assert self.complete_failure.call_args.kwargs["job_id"] == "123"
