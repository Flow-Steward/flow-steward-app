"""Shared runtime logging bootstrap for long-running platform services."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_DEFAULT_BASENAME = "platform"
_DEFAULT_SERVICE_NAME = "platform"
_DEFAULT_LEVEL = "INFO"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_RETENTION_DAYS = 30
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "gunicorn",
    "gunicorn.error",
    "gunicorn.access",
    "fastapi",
    "starlette",
)
_EXCEPTION_HOOKS_INSTALLED = False
_CORRELATION_ID: ContextVar[str] = ContextVar("platform_log_correlation_id", default="")


def _config_value(
    config: dict[str, Any] | None,
    key: str,
    env_name: str,
    default: Any,
) -> Any:
    if config is not None and key in config:
        value = config[key]
        if value is not None and value != "":
            return value
    return _runtime_env.get(env_name, default)


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def _normalize_level_name(value: Any, default: str = _DEFAULT_LEVEL) -> str:
    text = str(value or "").strip().upper()
    if text == "WARN":
        text = "WARNING"
    if text in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return text
    return default


def _level_value(value: Any, default: str = _DEFAULT_LEVEL) -> int:
    return int(getattr(logging, _normalize_level_name(value, default), logging.INFO))


def _normalize_log_format(value: Any, default: str = _DEFAULT_LOG_FORMAT) -> str:
    text = str(value or "").strip().lower()
    if text in {"text", "plain"}:
        return "text"
    if text in {"json", "ecs", "ecs_json"}:
        return "json"
    return default


def _sanitize_name(value: str, *, default: str) -> str:
    raw = str(value or "").strip().lower()
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_") or default


def _resolve_log_dir(config: dict[str, Any] | None = None) -> Path:
    raw = str(_config_value(config, "runtime_log_dir", "FS_LOG_DIR", "") or "").strip()
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (_REPO_ROOT / candidate).resolve()
        return candidate
    return (_REPO_ROOT / "logs").resolve()


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in _runtime_env


def _ensure_directory_permissions(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    with suppress(OSError):
        os.chmod(path, stat.S_IRWXU)


def _ensure_file_permissions(path: Path) -> None:
    with suppress(OSError):
        os.chmod(path, 0o640)


def current_correlation_id() -> str:
    return str(_CORRELATION_ID.get() or "").strip()


def set_correlation_id(correlation_id: str | None) -> Token[str]:
    return _CORRELATION_ID.set(str(correlation_id or "").strip())


def reset_correlation_id(token: Token[str] | None) -> None:
    if token is None:
        return
    with suppress(Exception):
        _CORRELATION_ID.reset(token)


@contextmanager
def correlation_id_context(correlation_id: str | None):
    token = set_correlation_id(correlation_id)
    try:
        yield current_correlation_id()
    finally:
        reset_correlation_id(token)


class _RuntimeContextFilter(logging.Filter):
    def __init__(self, component: str, service_name: str) -> None:
        super().__init__()
        self._component = str(component or "runtime").strip() or "runtime"
        self._service_name = (
            str(service_name or _DEFAULT_SERVICE_NAME).strip() or _DEFAULT_SERVICE_NAME
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "component"):
            record.component = self._component
        if not hasattr(record, "service_name"):
            record.service_name = self._service_name
        correlation_id = str(getattr(record, "correlation_id", "") or "").strip()
        if not correlation_id:
            correlation_id = current_correlation_id()
        record.correlation_id = correlation_id
        record.correlation_id_display = correlation_id or "-"
        if not hasattr(record, "event_dataset"):
            component = str(getattr(record, "component", self._component) or self._component)
            service_name = str(
                getattr(record, "service_name", self._service_name) or self._service_name
            )
            record.event_dataset = f"{service_name}.{component}"
        return True


class RuntimeTextFormatter(logging.Formatter):
    def __init__(self, *, use_utc: bool = False) -> None:
        super().__init__(
            fmt=(
                "%(asctime)s %(levelname)s [%(component)s] [corr=%(correlation_id_display)s] "
                "[pid=%(process)d thread=%(threadName)s] [%(name)s] %(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S%z",
        )
        self._use_utc = bool(use_utc)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        if self._use_utc:
            dt = datetime.fromtimestamp(record.created, UTC)
        else:
            dt = datetime.fromtimestamp(record.created).astimezone()
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S%z")


class RuntimeJsonFormatter(logging.Formatter):
    def __init__(self, *, use_utc: bool = False) -> None:
        super().__init__()
        self._use_utc = bool(use_utc)

    def _timestamp(self, record: logging.LogRecord) -> str:
        if self._use_utc:
            dt = datetime.fromtimestamp(record.created, UTC)
        else:
            dt = datetime.fromtimestamp(record.created).astimezone()
        return dt.isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "@timestamp": self._timestamp(record),
            "message": record.getMessage(),
            "log.level": str(record.levelname or _DEFAULT_LEVEL).lower(),
            "log.logger": str(record.name or "root"),
            "service.name": str(
                getattr(record, "service_name", _DEFAULT_SERVICE_NAME) or _DEFAULT_SERVICE_NAME
            ),
            "service.component": str(getattr(record, "component", "runtime") or "runtime"),
            "event.dataset": str(
                getattr(record, "event_dataset", "platform.runtime") or "platform.runtime"
            ),
            "process.pid": int(getattr(record, "process", 0) or 0),
            "process.thread.name": str(getattr(record, "threadName", "") or ""),
        }
        correlation_id = str(getattr(record, "correlation_id", "") or "").strip()
        if correlation_id:
            payload["correlation.id"] = correlation_id
        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            if exc_type is not None:
                payload["error.type"] = getattr(exc_type, "__name__", str(exc_type))
            if exc_value is not None:
                payload["error.message"] = str(exc_value)
            payload["error.stack_trace"] = self.formatException(record.exc_info)
        elif record.stack_info:
            payload["error.stack_trace"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=True, default=str, sort_keys=True)


class DailyLogFileHandler(logging.Handler):
    """Append log records into YYYY-MM-DD files without rename-based rollover."""

    terminator = "\n"

    def __init__(
        self,
        *,
        log_dir: Path,
        basename: str = _DEFAULT_BASENAME,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        encoding: str = "utf-8",
        utc: bool = False,
        now_fn=None,
    ) -> None:
        super().__init__()
        self._log_dir = Path(log_dir)
        self._basename = _sanitize_name(basename, default=_DEFAULT_BASENAME)
        self._retention_days = max(0, int(retention_days))
        self._encoding = encoding
        self._utc = bool(utc)
        self._now_fn = now_fn
        self._stream: TextIO | None = None
        self._current_date: str | None = None
        self._current_path: Path | None = None
        self._last_cleanup_date: str | None = None
        self._io_lock = threading.RLock()

    def _now(self) -> datetime:
        if callable(self._now_fn):
            value = self._now_fn()
            if isinstance(value, datetime):
                return value
        if self._utc:
            return datetime.now(UTC)
        return datetime.now().astimezone()

    def _path_for_date(self, current_date: str) -> Path:
        return self._log_dir / f"{self._basename}-{current_date}.log"

    def _cleanup_old_files(self, *, current_date: date) -> None:
        if self._retention_days <= 0:
            return
        marker = current_date.isoformat()
        if self._last_cleanup_date == marker:
            return

        keep_since = current_date - timedelta(days=max(0, self._retention_days - 1))
        pattern = f"{self._basename}-*.log"
        for path in self._log_dir.glob(pattern):
            suffix = path.name.removeprefix(f"{self._basename}-").removesuffix(".log")
            try:
                file_date = date.fromisoformat(suffix)
            except ValueError:
                continue
            if file_date < keep_since:
                try:
                    path.unlink()
                except OSError:
                    continue
        self._last_cleanup_date = marker

    def _ensure_stream(self, now_dt: datetime) -> None:
        current_date = now_dt.date().isoformat()
        if self._stream is not None and self._current_date == current_date:
            return

        self._close_stream()
        _ensure_directory_permissions(self._log_dir)
        self._cleanup_old_files(current_date=now_dt.date())

        target = self._path_for_date(current_date)
        self._stream = target.open("a", encoding=self._encoding)
        self._current_date = current_date
        self._current_path = target
        _ensure_file_permissions(target)

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        with suppress(Exception):
            self._stream.flush()
        with suppress(Exception):
            self._stream.close()
        self._stream = None
        self._current_path = None
        self._current_date = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            now_dt = self._now()
            with self._io_lock:
                self._ensure_stream(now_dt)
                if self._stream is None:
                    return
                self._stream.write(message + self.terminator)
                self._stream.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        with self._io_lock:
            if self._stream is None:
                return
            with suppress(Exception):
                self._stream.flush()

    def close(self) -> None:
        with self._io_lock:
            self._close_stream()
        super().close()


def _remove_handlers(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        with suppress(Exception):
            handler.close()


def install_global_exception_logging() -> None:
    global _EXCEPTION_HOOKS_INSTALLED
    if _EXCEPTION_HOOKS_INSTALLED:
        return

    def _sys_hook(exc_type, exc_value, exc_traceback) -> None:
        if exc_type and issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.getLogger("platform.unhandled").critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _thread_hook(args) -> None:
        exc_type = getattr(args, "exc_type", None)
        if exc_type and issubclass(exc_type, KeyboardInterrupt):
            return
        logging.getLogger("platform.unhandled").critical(
            "Unhandled thread exception (thread=%s)",
            getattr(getattr(args, "thread", None), "name", "unknown"),
            exc_info=(
                exc_type,
                getattr(args, "exc_value", None),
                getattr(args, "exc_traceback", None),
            ),
        )

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook
    _EXCEPTION_HOOKS_INSTALLED = True


def install_asyncio_exception_logging(loop) -> None:
    if loop is None or getattr(loop, "_platform_asyncio_exception_handler_installed", False):
        return

    def _handler(_loop, context: dict[str, Any]) -> None:
        message = str(context.get("message") or "Unhandled asyncio exception")
        exc = context.get("exception")
        logger = logging.getLogger("platform.asyncio")
        if exc is not None:
            logger.error(message, exc_info=(type(exc), exc, exc.__traceback__))
            return
        payload = {
            key: value
            for key, value in context.items()
            if key not in {"message", "exception"} and value is not None
        }
        logger.error("%s | context=%s", message, payload or "{}")

    loop.set_exception_handler(_handler)
    loop._platform_asyncio_exception_handler_installed = True


def configure_runtime_logging(
    *,
    component: str,
    config: dict[str, Any] | None = None,
    force: bool = True,
    stderr_stream: TextIO | None = None,
    enable_when_testing: bool = False,
) -> dict[str, Any]:
    """Configure stderr and daily file logging for a long-running process."""

    if _running_under_pytest() and not enable_when_testing:
        return {"configured": False, "reason": "pytest"}

    log_level_name = _normalize_level_name(
        _config_value(config, "runtime_log_level", "FS_LOG_LEVEL", _DEFAULT_LEVEL)
    )
    stderr_level_name = _normalize_level_name(
        _config_value(
            config,
            "runtime_stderr_log_level",
            "FS_LOG_STDERR_LEVEL",
            log_level_name,
        )
    )
    file_level_name = _normalize_level_name(
        _config_value(
            config,
            "runtime_file_log_level",
            "FS_LOG_FILE_LEVEL",
            log_level_name,
        )
    )
    log_format = _normalize_log_format(
        _config_value(config, "runtime_log_format", "FS_LOG_FORMAT", _DEFAULT_LOG_FORMAT)
    )
    stderr_enabled = _parse_bool(
        _config_value(
            config,
            "runtime_log_stderr_enabled",
            "FS_LOG_STDERR_ENABLED",
            True,
        ),
        True,
    )
    file_enabled = _parse_bool(
        _config_value(config, "runtime_log_file_enabled", "FS_LOG_FILE_ENABLED", True),
        True,
    )
    capture_warnings = _parse_bool(
        _config_value(config, "runtime_log_capture_warnings", "FS_LOG_CAPTURE_WARNINGS", True),
        True,
    )
    retention_days = _parse_int(
        _config_value(
            config,
            "runtime_log_retention_days",
            "FS_LOG_RETENTION_DAYS",
            _DEFAULT_RETENTION_DAYS,
        ),
        _DEFAULT_RETENTION_DAYS,
        minimum=0,
    )
    use_utc = _parse_bool(
        _config_value(config, "runtime_log_utc", "FS_LOG_UTC", False),
        False,
    )
    basename = _sanitize_name(
        str(
            _config_value(
                config,
                "runtime_log_basename",
                "FS_LOG_FILE_BASENAME",
                _DEFAULT_BASENAME,
            )
        ),
        default=_DEFAULT_BASENAME,
    )
    service_name = _sanitize_name(
        str(
            _config_value(
                config,
                "runtime_service_name",
                "FS_SERVICE_NAME",
                basename or _DEFAULT_SERVICE_NAME,
            )
        ),
        default=basename or _DEFAULT_SERVICE_NAME,
    )

    handlers: list[logging.Handler] = []
    context_filter = _RuntimeContextFilter(component, service_name)
    formatter: logging.Formatter
    formatter = (
        RuntimeJsonFormatter(use_utc=use_utc)
        if log_format == "json"
        else RuntimeTextFormatter(use_utc=use_utc)
    )

    if stderr_enabled:
        stream_handler = logging.StreamHandler(stderr_stream or sys.stderr)
        stream_handler.setLevel(_level_value(stderr_level_name))
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(context_filter)
        handlers.append(stream_handler)

    if file_enabled:
        file_handler = DailyLogFileHandler(
            log_dir=_resolve_log_dir(config),
            basename=basename,
            retention_days=retention_days,
            utc=use_utc,
        )
        file_handler.setLevel(_level_value(file_level_name))
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    if force:
        _remove_handlers(root_logger)
    else:
        for existing in root_logger.handlers:
            handlers.append(existing)

    if handlers:
        for handler in handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)
        root_logger.setLevel(min(handler.level for handler in handlers))
    else:
        root_logger.setLevel(_level_value(log_level_name))

    for logger_name in _RUNTIME_LOGGERS:
        runtime_logger = logging.getLogger(logger_name)
        if force:
            _remove_handlers(runtime_logger)
        runtime_logger.propagate = True
        runtime_logger.setLevel(logging.NOTSET)

    logging.captureWarnings(capture_warnings)
    install_global_exception_logging()

    settings = {
        "configured": True,
        "component": component,
        "log_level": log_level_name,
        "stderr_enabled": stderr_enabled,
        "stderr_level": stderr_level_name,
        "file_enabled": file_enabled,
        "file_level": file_level_name,
        "format": log_format,
        "log_dir": str(_resolve_log_dir(config)),
        "basename": basename,
        "service_name": service_name,
        "retention_days": retention_days,
        "utc": use_utc,
    }
    root_logger.debug("Runtime logging configured: %s", settings)
    return settings


__all__ = [
    "DailyLogFileHandler",
    "RuntimeJsonFormatter",
    "RuntimeTextFormatter",
    "configure_runtime_logging",
    "correlation_id_context",
    "current_correlation_id",
    "install_asyncio_exception_logging",
    "install_global_exception_logging",
    "reset_correlation_id",
    "set_correlation_id",
]
