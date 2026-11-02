from __future__ import annotations

import logging

import pytest

pytest.importorskip("opentelemetry.instrumentation.logging.handler")

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs import export as logs_export
from opentelemetry.sdk.trace import TracerProvider

from core.infrastructure.observability import otel_logs
from core.infrastructure.observability.otel_logs import safe_event

_InMemory = (
    getattr(logs_export, "InMemoryLogRecordExporter", None) or logs_export.InMemoryLogExporter
)
_JOB_LOGGER = "core.application.jobs.task_execution_service"


def test_safe_numeric_values_preserve_large_int_and_infinities() -> None:
    huge = 10**1000
    assert otel_logs._safe_value(huge) == huge
    assert otel_logs._safe_value(float("inf")) == float("inf")
    assert otel_logs._safe_value(float("-inf")) == float("-inf")
    assert otel_logs._safe_value(float("nan")) is None


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def exported():
    exporter = _InMemory()
    provider = LoggerProvider(shutdown_on_exit=False)
    provider.add_log_record_processor(logs_export.SimpleLogRecordProcessor(exporter))
    otel_logs._uninstall_for_tests()
    root_handlers = list(logging.getLogger().handlers)
    assert otel_logs.install_log_export(provider) is not None
    assert otel_logs.install_log_export(provider) is otel_logs._installed
    yield exporter
    otel_logs._uninstall_for_tests()
    assert logging.getLogger().handlers == root_handlers


def _records(exporter) -> list:
    return [getattr(item, "log_record", item) for item in exporter.get_finished_logs()]


def test_a_safe_event_exports_a_separate_clean_record(exported) -> None:
    logger = logging.getLogger(_JOB_LOGGER)
    local = _Capture()
    logger.addHandler(local)
    try:
        try:
            raise RuntimeError("secret detail")
        except RuntimeError:
            logger.error(
                "job_id=%s prompt=%s failed",
                "job-1",
                "secret prompt",
                exc_info=True,
                stack_info=True,
                extra={
                    **safe_event(
                        "fs.job.execution_failed",
                        {"fs.job.id": "job-1", "user.email": "a@example.com"},
                    ),
                    "custom": "secret extra",
                },
            )
    finally:
        logger.removeHandler(local)

    (record,) = _records(exported)
    assert record.body == "fs.job.execution_failed"
    assert record.event_name == "fs.job.execution_failed"
    assert dict(record.attributes) == {"fs.job.id": "job-1"}
    assert record.severity_text == "ERROR"
    exported_text = repr(record.body) + repr(dict(record.attributes))
    for secret in ("secret", "a@example.com", "Traceback"):
        assert secret not in exported_text
    # Local handlers see the original record unchanged.
    (original,) = local.records
    assert original.getMessage() == "job_id=job-1 prompt=secret prompt failed"
    assert original.exc_info is not None
    assert original.custom == "secret extra"


@pytest.mark.parametrize(
    ("logger_name", "extra"),
    [
        (_JOB_LOGGER, {}),
        (_JOB_LOGGER, safe_event("fs.unknown.event")),
        ("some.other.module", safe_event("fs.job.execution_failed", {"fs.job.id": "j"})),
        (_JOB_LOGGER, {otel_logs.SAFE_EVENT_KEY: "forged"}),
    ],
)
def test_only_marked_events_of_allowlisted_loggers_leave(exported, logger_name, extra) -> None:
    logging.getLogger(logger_name).warning("job %s", "job-1", extra=extra)

    assert _records(exported) == []


def test_unsafe_values_are_dropped_and_ids_kept_apart(exported) -> None:
    from core.infrastructure.observability.runtime_logging import correlation_id_context

    tracer = TracerProvider(shutdown_on_exit=False).get_tracer("test")
    with correlation_id_context("req-42"), tracer.start_as_current_span("step") as span:
        logging.getLogger(_JOB_LOGGER).warning(
            "failure",
            extra=safe_event(
                "fs.job.execution_failure_status",
                {"fs.job.id": "job 1; rm -rf", "fs.job.status": "failed"},
            ),
        )

    (record,) = _records(exported)
    assert dict(record.attributes) == {"fs.job.status": "failed", "correlation.id": "req-42"}
    assert record.trace_id == span.get_span_context().trace_id
    assert record.span_id == span.get_span_context().span_id


def test_the_export_enabled_event_is_safe() -> None:
    record = logging.LogRecord(
        "core.infrastructure.observability.otel_runtime",
        logging.INFO,
        "x.py",
        1,
        "OpenTelemetry export enabled (role=%s)",
        ("worker",),
        None,
    )
    record.__dict__.update(safe_event("fs.telemetry.export_enabled", {"fs.role": "worker"}))

    copy = otel_logs.sanitized_record(record)

    assert copy.getMessage() == "fs.telemetry.export_enabled"
    assert copy.__dict__["fs.role"] == "worker"
    assert copy.exc_info is None and copy.stack_info is None


def test_a_safe_event_still_exports_without_runtime_logging(
    exported, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    # The fs-mcp image ships only the otel_* modules.
    monkeypatch.setitem(sys.modules, "core.infrastructure.observability.runtime_logging", None)

    logging.getLogger(_JOB_LOGGER).warning(
        "x", extra=safe_event("fs.job.execution_failed", {"fs.job.id": "job-1"})
    )

    (record,) = _records(exported)
    assert dict(record.attributes) == {"fs.job.id": "job-1"}
