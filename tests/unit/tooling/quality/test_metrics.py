from __future__ import annotations

import pytest

from core.application.tooling.quality.metrics import MetricSample, aggregate


def test_metrics_report_reuse_rate_saved_time_and_repetition() -> None:
    report = aggregate(
        [
            MetricSample(status="executed", duration_seconds=10),
            MetricSample(status="reused", duration_seconds=20),
            MetricSample(status="executed", duration_seconds=30),
            MetricSample(status="executed", duration_seconds=5, intentional_repeat=True),
        ]
    )

    assert report.executed_seconds == 45
    assert report.saved_seconds == 20
    assert report.reuse_rate == pytest.approx(0.25)
    assert report.repeated_check_percentage == pytest.approx(25.0)


def test_metrics_keep_failure_and_invalidation_diagnostics() -> None:
    report = aggregate(
        [
            MetricSample(
                status="failed",
                duration_seconds=7,
                invalidation_reason="fingerprint_mismatch",
            ),
            MetricSample(
                status="executed",
                duration_seconds=11,
                invalidation_reason="fingerprint_mismatch",
            ),
        ]
    )

    assert report.time_to_first_failure_seconds == 7
    assert report.invalidation_reasons == {"fingerprint_mismatch": 2}
