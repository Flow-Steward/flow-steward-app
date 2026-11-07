"""Usage-only dashboard metrics contracts."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from core.application.reports import dashboard_metrics_helpers as helpers


def test_dashboard_summary_keeps_operational_metrics_without_money():
    now = datetime.now(UTC)
    summary = helpers._dashboard_compute_summary(
        rows=[
            {
                "job_id": "job-1",
                "status": "completed",
                "project_name": "Example",
                "started_at": (now - timedelta(minutes=2)).isoformat(),
                "finished_at": (now - timedelta(minutes=1)).isoformat(),
            }
        ],
        groups={},
        range_hours=24,
    )
    assert summary["hero_kpis"]["completed"]["value"] == 1
    assert summary["hero_kpis"]["p50_duration"]["value_seconds"] == 60
    assert "spend" not in summary["hero_kpis"]
    assert set(summary["trends"]) == {"daily_throughput", "duration"}
    assert set(summary["breakdown_rows"][0]) == {
        "project_name",
        "attention_count",
        "completed",
        "failed",
        "finished",
        "success_rate_pct",
        "p50_duration_seconds",
    }


def test_dashboard_usage_enrichment_retains_token_totals_and_agent():
    store = Mock(spec=["get_jobs_usage_summary"])
    store.get_jobs_usage_summary.return_value = {
        "job-1": {
            "tokens_prompt": 10,
            "tokens_completion": 20,
            "tokens_total": 30,
            "dominant_agent": "writer",
        }
    }
    result = helpers._dashboard_fetch_usage_by_job(bl=store, rows=[{"job_id": "job-1"}])
    assert result == {
        "job-1": {
            "tokens_prompt": 10,
            "tokens_completion": 20,
            "tokens_total": 30,
            "dominant_agent": "writer",
        }
    }


def test_dashboard_usage_enrichment_falls_back_to_single_and_handles_failure():
    store = Mock(spec=["get_jobs_usage_summary", "get_job_usage_summary"])
    store.get_jobs_usage_summary.side_effect = RuntimeError("bulk unavailable")
    store.get_job_usage_summary.side_effect = [
        {
            "tokens_prompt": 3,
            "tokens_completion": 7,
            "tokens_total": 10,
            "dominant_agent": "editor",
        },
        RuntimeError("single unavailable"),
    ]
    result = helpers._dashboard_fetch_usage_by_job(
        bl=store,
        rows=[{"job_id": "job-1"}, {"job_id": "job-1"}, {"id": "job-2"}],
    )
    assert result["job-1"]["tokens_total"] == 10
    assert result["job-1"]["dominant_agent"] == "editor"
    assert result["job-2"] == {
        "tokens_prompt": 0,
        "tokens_completion": 0,
        "tokens_total": 0,
        "dominant_agent": None,
    }
    assert store.get_job_usage_summary.call_count == 2
