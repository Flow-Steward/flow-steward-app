from __future__ import annotations

from unittest.mock import call, patch

import pytest

from core.infrastructure.runtime.worker import worker


def test_schema_compat_retry_settings_use_defaults(monkeypatch) -> None:
    monkeypatch.delenv("FS_SCHEMA_COMPAT_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("FS_SCHEMA_COMPAT_RETRY_DELAY_SECONDS", raising=False)

    assert worker._schema_compat_retry_settings() == (12, 2.0)


def test_schema_compat_retry_settings_clamp_minimums(monkeypatch) -> None:
    monkeypatch.setenv("FS_SCHEMA_COMPAT_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("FS_SCHEMA_COMPAT_RETRY_DELAY_SECONDS", "0")

    assert worker._schema_compat_retry_settings() == (1, 0.5)


def test_schema_compat_success_runs_once_without_retry_delay() -> None:
    with (
        patch.object(worker, "_schema_compat_retry_settings", return_value=(3, 4.0)),
        patch.object(worker, "_run_schema_compatibility_check") as check_mock,
        patch("time.sleep") as sleep_mock,
        patch.object(worker, "logger") as logger_mock,
    ):
        worker._enforce_schema_compatibility()

    check_mock.assert_called_once_with()
    sleep_mock.assert_not_called()
    logger_mock.info.assert_not_called()
    logger_mock.error.assert_not_called()


def test_schema_compat_cannot_be_disabled_via_fs_env(monkeypatch) -> None:
    monkeypatch.setenv("FS_ENFORCE_SCHEMA_COMPAT", "0")

    with patch.object(worker, "_run_schema_compatibility_check") as check_mock:
        worker._enforce_schema_compatibility()

    check_mock.assert_called_once_with()


def test_schema_compat_retryable_failure_retries_then_succeeds_with_delay() -> None:
    with (
        patch.object(worker, "_schema_compat_retry_settings", return_value=(2, 1.25)),
        patch("core.infrastructure.config.runtime_config.CONFIG", {}),
        patch(
            "core.infrastructure.db.platform_logger.resolve_observability_dsn", return_value="dsn"
        ),
        patch.object(
            worker,
            "_run_schema_compatibility_check",
            wraps=worker._run_schema_compatibility_check,
        ) as check_mock,
        patch(
            "core.infrastructure.db.schema_contract.enforce_runtime_schema_contract_dsn",
            side_effect=[ConnectionError("connection refused"), None],
        ),
        patch("time.sleep") as sleep_mock,
        patch.object(worker, "logger") as logger_mock,
    ):
        worker._enforce_schema_compatibility()

    assert check_mock.call_count == 2
    sleep_mock.assert_called_once_with(1.25)
    logger_mock.info.assert_called_once()
    logger_mock.error.assert_not_called()


def test_schema_compat_missing_dsn_fails_closed() -> None:
    with (
        patch("core.infrastructure.config.runtime_config.CONFIG", {}),
        patch(
            "core.infrastructure.db.platform_logger.resolve_observability_dsn",
            return_value="",
        ),
        pytest.raises(RuntimeError, match="no database DSN"),
    ):
        worker._run_schema_compatibility_check()


def test_schema_compat_non_retryable_failure_raises_without_retry_delay() -> None:
    failure = ValueError("schema version is too old")

    with (
        patch.object(worker, "_schema_compat_retry_settings", return_value=(3, 2.0)),
        patch.object(worker, "_run_schema_compatibility_check", side_effect=failure) as check_mock,
        patch("time.sleep") as sleep_mock,
        patch.object(worker, "logger") as logger_mock,
        pytest.raises(ValueError, match="schema version is too old"),
    ):
        worker._enforce_schema_compatibility()

    check_mock.assert_called_once_with()
    sleep_mock.assert_not_called()
    logger_mock.info.assert_not_called()
    logger_mock.error.assert_called_once_with(
        "Worker schema compatibility check failed: %s",
        failure,
    )


def test_schema_compat_retryable_failure_exhausts_attempts() -> None:
    failure = ConnectionError("connection refused")

    with (
        patch.object(worker, "_schema_compat_retry_settings", return_value=(3, 0.75)),
        patch.object(worker, "_run_schema_compatibility_check", side_effect=failure) as check_mock,
        patch("time.sleep") as sleep_mock,
        patch.object(worker, "logger") as logger_mock,
        pytest.raises(ConnectionError, match="connection refused"),
    ):
        worker._enforce_schema_compatibility()

    assert check_mock.call_count == 3
    assert sleep_mock.call_args_list == [call(0.75), call(0.75)]
    assert logger_mock.info.call_count == 2
    logger_mock.error.assert_called_once_with(
        "Worker schema compatibility check failed: %s",
        failure,
    )
