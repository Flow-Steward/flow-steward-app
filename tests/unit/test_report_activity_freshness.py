"""Operational reports count cancelled runs apart and say when their data was read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.scope import ScopeContext
from core.application.reports.reporting_service import ReportingService

SCOPE = ScopeContext(account_id="acc", tenant_id=DEFAULT_TENANT_ID)


class _Store:
    """The facts ``list_activity_facts`` returns: rollup rows and live rows."""

    def __init__(self) -> None:
        finished = datetime.now(UTC) - timedelta(minutes=1)
        self.facts: list[dict[str, Any]] = [
            {"job_id": "a", "result": "completed", "finished_at": finished, "source": "rollup"},
            {"job_id": "b", "result": "failed", "finished_at": finished, "source": "rollup"},
            # Finished after the rollup's last refresh: read live from jobs.
            {"job_id": "c", "result": "completed", "finished_at": finished, "source": "live"},
            {"job_id": "d", "result": "cancelled", "finished_at": finished, "source": "live"},
        ]

    def list_report_activity_facts(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.facts)


def _generate(report_type: str) -> dict[str, Any]:
    store = _Store()
    service = ReportingService(get_logger_only=lambda: store, job_query_service=lambda: None)
    return service.report_generate(report_type, {"range": "7d"}, scope=SCOPE, persist=False)


@pytest.mark.parametrize("report_type", ["activity_daily", "project_summary", "project_stats"])
def test_the_report_counts_cancelled_runs_apart_and_stamps_its_data(report_type: str) -> None:
    before = datetime.now(UTC)
    report = _generate(report_type)
    payload = report["payload"]

    kpis = payload["kpis"]
    assert (kpis["completed_count"], kpis["failed_count"], kpis["cancelled_count"]) == (2, 1, 1)
    assert kpis["total_jobs"] == 3
    assert kpis["overall_success_rate_pct"] == pytest.approx(66.7)
    assert sum(row["cancelled_count"] for row in payload["daily"]) == 1

    data_as_of = datetime.fromisoformat(payload["data_as_of"])
    assert before <= data_as_of <= datetime.now(UTC)
    assert (
        f"Data as of: {payload['data_as_of'][:19].replace('T', ' ')} UTC" in report["summary_text"]
    )
    assert "cancelled" in report["summary_text"].lower()


def test_the_finished_jobs_line_names_the_cancelled_runs() -> None:
    text = _generate("activity_daily")["summary_text"]

    assert (
        "Finished jobs: 3 (2 completed, 1 failed); 1 cancelled "
        "(not counted as finished or in the success rate)"
    ) in text
