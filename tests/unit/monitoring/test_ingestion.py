from __future__ import annotations

import pytest


class RecordingRepository:
    def __init__(self, failure=None):
        self.reports = []
        self.failure = failure

    def store(self, report, *, history_enabled):
        from core.application.monitoring.contracts import IngestResult

        if self.failure:
            raise self.failure
        self.reports.append((report, history_enabled))
        return IngestResult(disposition="stored")


def service(repository, context, now):
    from core.application.monitoring.ingestion import RuntimeStatusIngestionService
    from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config

    return RuntimeStatusIngestionService(
        repository=repository,
        context=context,
        config=load_monitoring_runtime_config({}),
        clock=lambda: now,
    )


def test_valid_report_is_stored_with_history_disabled(context, report_body, now):
    repository = RecordingRepository()
    result = service(repository, context, now).accept(report_body())
    assert result.disposition == "stored"
    assert len(repository.reports) == 1
    assert repository.reports[0][0].service_type == "worker"
    assert repository.reports[0][1] is False


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"schema_version":2}'])
def test_invalid_report_never_reaches_storage(body, context, now):
    repository = RecordingRepository()
    result = service(repository, context, now).accept(body)
    assert result.disposition == "rejected"
    assert repository.reports == []


def test_foreign_scope_never_reaches_storage(context, report_body, now):
    repository = RecordingRepository()
    result = service(repository, context, now).accept(report_body(installation_id="foreign"))
    assert result.disposition == "rejected"
    assert repository.reports == []


def test_database_error_remains_retryable(context, report_body, now):
    failure = ConnectionError("disposable database unavailable")
    repository = RecordingRepository(failure)
    ingestion = service(repository, context, now)
    body = report_body()
    with pytest.raises(ConnectionError):
        ingestion.accept(body)
    assert repository.reports == []
