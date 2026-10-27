from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime

from core.infrastructure.observability.runtime_logging import (
    DailyLogFileHandler,
    configure_runtime_logging,
    correlation_id_context,
)

_LOGGER_NAMES = (
    "",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "gunicorn",
    "gunicorn.error",
    "gunicorn.access",
    "fastapi",
    "starlette",
)


def _snapshot_logger_state() -> dict[str, tuple[list[logging.Handler], int, bool]]:
    snapshot: dict[str, tuple[list[logging.Handler], int, bool]] = {}
    for name in _LOGGER_NAMES:
        logger = logging.getLogger(name)
        snapshot[name] = (list(logger.handlers), logger.level, logger.propagate)
    return snapshot


def _restore_logger_state(snapshot: dict[str, tuple[list[logging.Handler], int, bool]]) -> None:
    for name, (handlers, level, propagate) in snapshot.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if handler not in handlers:
                handler.close()
        for handler in handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


def test_daily_log_file_handler_rolls_by_day_and_prunes_old_files(tmp_path) -> None:
    ticks = iter(
        (
            datetime(2026, 4, 10, 23, 59, 50, tzinfo=UTC),
            datetime(2026, 4, 11, 0, 0, 5, tzinfo=UTC),
        )
    )
    old_log = tmp_path / "platform-2026-04-08.log"
    old_log.write_text("stale\n", encoding="utf-8")

    handler = DailyLogFileHandler(
        log_dir=tmp_path,
        basename="platform",
        retention_days=2,
        utc=True,
        now_fn=lambda: next(ticks),
    )
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    first = logging.makeLogRecord(
        {"name": "tests.runtime_logging", "levelno": logging.ERROR, "levelname": "ERROR", "msg": "first"}
    )
    second = logging.makeLogRecord(
        {"name": "tests.runtime_logging", "levelno": logging.ERROR, "levelname": "ERROR", "msg": "second"}
    )
    handler.emit(first)
    handler.emit(second)
    handler.close()

    assert "ERROR first" in (tmp_path / "platform-2026-04-10.log").read_text(encoding="utf-8")
    assert "ERROR second" in (tmp_path / "platform-2026-04-11.log").read_text(encoding="utf-8")
    assert not old_log.exists()


def test_configure_runtime_logging_honors_file_level(tmp_path) -> None:
    snapshot = _snapshot_logger_state()
    stderr = io.StringIO()

    try:
        settings = configure_runtime_logging(
            component="worker",
            config={
                "runtime_log_level": "INFO",
                "runtime_file_log_level": "ERROR",
                "runtime_stderr_log_level": "ERROR",
                "runtime_log_dir": str(tmp_path),
                "runtime_log_basename": "platform",
                "runtime_log_file_enabled": True,
                "runtime_log_stderr_enabled": False,
                "runtime_log_retention_days": 30,
                "runtime_log_utc": True,
            },
            stderr_stream=stderr,
            enable_when_testing=True,
        )

        logger = logging.getLogger("tests.runtime_logging")
        logger.info("info should not be written")
        logger.error("error should be written")

        for handler in logging.getLogger().handlers:
            handler.flush()

        log_files = sorted(tmp_path.glob("platform-*.log"))
        assert settings["configured"] is True
        assert settings["file_level"] == "ERROR"
        assert len(log_files) == 1
        content = log_files[0].read_text(encoding="utf-8")
        assert "error should be written" in content
        assert "info should not be written" not in content
        assert "[worker]" in content
    finally:
        _restore_logger_state(snapshot)


def test_configure_runtime_logging_json_includes_correlation_id_and_traceback(tmp_path) -> None:
    snapshot = _snapshot_logger_state()
    stderr = io.StringIO()

    try:
        settings = configure_runtime_logging(
            component="serve",
            config={
                "runtime_log_level": "INFO",
                "runtime_file_log_level": "ERROR",
                "runtime_stderr_log_level": "ERROR",
                "runtime_log_dir": str(tmp_path),
                "runtime_log_basename": "platform",
                "runtime_log_file_enabled": True,
                "runtime_log_stderr_enabled": False,
                "runtime_log_retention_days": 30,
                "runtime_log_utc": True,
                "runtime_log_format": "json",
                "runtime_service_name": "platform",
            },
            stderr_stream=stderr,
            enable_when_testing=True,
        )

        logger = logging.getLogger("tests.runtime_logging")
        with correlation_id_context("corr-123"):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logger.exception("failure during request")

        for handler in logging.getLogger().handlers:
            handler.flush()

        log_files = sorted(tmp_path.glob("platform-*.log"))
        assert settings["configured"] is True
        assert settings["format"] == "json"
        assert len(log_files) == 1
        line = log_files[0].read_text(encoding="utf-8").strip()
        payload = json.loads(line)
        assert payload["message"] == "failure during request"
        assert payload["log.level"] == "error"
        assert payload["service.name"] == "platform"
        assert payload["service.component"] == "serve"
        assert payload["correlation.id"] == "corr-123"
        assert payload["error.type"] == "RuntimeError"
        assert "RuntimeError: boom" in payload["error.stack_trace"]
    finally:
        _restore_logger_state(snapshot)
