"""Operational analytics without monetary datasets."""

from datetime import date

import pytest

from core.application.management.errors import ServiceValidationError
from core.application.reports.analytics_service import AnalyticsService


def test_activity_daily_rows_and_summary_have_no_money_fields():
    rows = AnalyticsService._daily_rows_from_facts(
        [{"finished_date": "2026-11-01", "result": "completed", "duration_seconds": 60}],
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
    )
    assert rows[0]["completed_count"] == 1
    assert rows[0]["p50_seconds"] == 60
    assert rows[1]["total_jobs"] == 0
    assert all("spend_usd" not in row for row in rows)
    summary = AnalyticsService._activity_summary(rows)
    assert summary["total_jobs"] == 1
    assert "total_spend_usd" not in summary


def test_monetary_analytics_dataset_is_unsupported():
    service = AnalyticsService(get_logger_only=lambda: None)
    with pytest.raises(ServiceValidationError, match="Unsupported dataset_id"):
        service.analytics_query("dashboard_spend_by_agent", {}, scope=None)
