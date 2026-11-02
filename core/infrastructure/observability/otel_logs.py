"""Log export limited to explicitly safe operational events.

A record is exported only when both hold:

* its logger is in ``EXPORTED_LOGGERS``;
* the call marked it with ``extra=safe_event(code, fields)`` for a code in
  ``SAFE_EVENTS``.

The standard ``LoggingHandler`` then receives a separate, newly built record:
the event code as its fixed message and event name, the original timestamp and
severity, the event's allowed scalar fields, and ``correlation.id``. The trace
and span ids come from the current context as separate fields. The original
``msg``, ``args``, extras, ``exc_info``, ``exc_text`` and ``stack_info`` never
reach it, and the original record is left untouched for local handlers.

The handler is attached to the allowlisted loggers only: no root handler, no
``LoggingInstrumentor``, no record factory or ``basicConfig`` change.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SAFE_EVENT_KEY = "fs_safe_event"

# Event code -> the scalar fields it may carry.
SAFE_EVENTS: Mapping[str, frozenset[str]] = {
    "fs.telemetry.export_enabled": frozenset({"fs.role"}),
    "fs.job.execution_failed": frozenset({"fs.job.id"}),
    "fs.job.execution_failure_status": frozenset({"fs.job.id", "fs.job.status"}),
}
EXPORTED_LOGGERS = frozenset(
    {
        "core.application.jobs.task_execution_service",
        "core.infrastructure.observability.otel_runtime",
    }
)
_SAFE_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


@dataclass(frozen=True)
class SafeEvent:
    code: str
    fields: Mapping[str, Any] = field(default_factory=dict)


def safe_event(code: str, fields: Mapping[str, Any] | None = None) -> dict[str, SafeEvent]:
    """``extra=`` marking a log call as a safe operational event."""
    return {SAFE_EVENT_KEY: SafeEvent(code, dict(fields or {}))}


def _safe_value(value: Any) -> Any | None:
    if isinstance(value, (bool, int)) or (isinstance(value, float) and not math.isnan(value)):
        return value
    if isinstance(value, str) and _SAFE_TEXT.fullmatch(value):
        return value
    return None


def _correlation_id(record: logging.LogRecord) -> str | None:
    value = getattr(record, "correlation_id", None)
    if not value:
        try:
            from core.infrastructure.observability.runtime_logging import current_correlation_id
        except ImportError:  # the fs-mcp image ships only the otel_* modules
            current_correlation_id = None
        value = current_correlation_id() if current_correlation_id is not None else None
    return value if isinstance(value, str) and _SAFE_TEXT.fullmatch(value) else None


def sanitized_record(record: logging.LogRecord) -> logging.LogRecord | None:
    """The exportable copy of *record*, or ``None`` when it must not leave."""
    if record.name not in EXPORTED_LOGGERS:
        return None
    event = getattr(record, SAFE_EVENT_KEY, None)
    if not isinstance(event, SafeEvent) or event.code not in SAFE_EVENTS:
        return None
    copy = logging.LogRecord(
        name=record.name,
        level=record.levelno,
        pathname="",
        lineno=0,
        msg=event.code,
        args=None,
        exc_info=None,
    )
    copy.created = record.created
    copy.msecs = record.msecs
    copy.__dict__["otel.event.name"] = event.code
    allowed = SAFE_EVENTS[event.code]
    for key, value in event.fields.items():
        safe = _safe_value(value)
        if key in allowed and safe is not None:
            copy.__dict__[key] = safe
    correlation_id = _correlation_id(record)
    if correlation_id:
        copy.__dict__["correlation.id"] = correlation_id
    return copy


class SafeEventHandler(logging.Handler):
    """Forwards sanitized copies of safe events to the standard handler."""

    def __init__(self, delegate: logging.Handler) -> None:
        super().__init__()
        self._delegate = delegate

    def emit(self, record: logging.LogRecord) -> None:
        try:
            copy = sanitized_record(record)
            if copy is not None:
                self._delegate.handle(copy)
        except Exception:
            # Export problems never reach the application or its local logs.
            return

    def flush(self) -> None:
        self._delegate.flush()


_installed: SafeEventHandler | None = None


def install_log_export(logger_provider: Any) -> SafeEventHandler | None:
    """Attach the safe-event handler to the allowlisted loggers, once per process."""
    global _installed
    if _installed is not None:
        return _installed
    try:
        from opentelemetry.instrumentation.logging.handler import LoggingHandler
    except ImportError:
        return None
    handler = SafeEventHandler(LoggingHandler(logger_provider=logger_provider))
    for name in sorted(EXPORTED_LOGGERS):
        logging.getLogger(name).addHandler(handler)
    _installed = handler
    return handler


def _uninstall_for_tests() -> None:
    global _installed
    if _installed is not None:
        for name in EXPORTED_LOGGERS:
            logging.getLogger(name).removeHandler(_installed)
    _installed = None


__all__ = [
    "EXPORTED_LOGGERS",
    "SAFE_EVENTS",
    "SAFE_EVENT_KEY",
    "SafeEvent",
    "SafeEventHandler",
    "install_log_export",
    "safe_event",
    "sanitized_record",
]
