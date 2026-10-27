from __future__ import annotations

import pytest

from core.application.observability_vocabulary import (
    JOB_FAILURE_STATUSES,
    JOB_SUCCESS_STATUSES,
    JOB_TERMINAL_STATUSES,
    job_attention_group,
    job_status_meta,
    normalize_job_status,
)


@pytest.mark.parametrize(
    ("raw", "canonical", "terminal"),
    [
        ("pending", "pending", False),
        ("queued", "queued", False),
        ("running", "in_progress", False),
        ("blocked", "blocked", False),
        ("waiting_external", "external_wait", False),
        ("awaiting_approval", "awaiting_approval", False),
        ("awaiting_timer", "awaiting_timer", False),
        ("awaiting_child", "awaiting_child", False),
        ("retryable_interrupted_step", "retryable_interrupted_step", True),
        ("triage-needed", "triage_needed", True),
        ("completed", "completed", True),
        ("failed", "failed", True),
        ("max-iterations", "max_iterations", True),
        ("canceled", "cancelled", True),
    ],
)
def test_job_status_vocabulary_has_one_canonical_terminal_classification(
    raw: str,
    canonical: str,
    terminal: bool,
) -> None:
    assert normalize_job_status(raw) == canonical
    assert job_status_meta(raw)["terminal"] is terminal
    assert (canonical in JOB_TERMINAL_STATUSES) is terminal


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("awaiting_timer", "Waiting for timer"),
        ("awaiting_child", "Waiting for child workflow"),
    ],
)
def test_durable_waits_read_as_paused_running_work_not_as_an_approval(
    status: str, label: str
) -> None:
    meta = job_status_meta(status)

    assert meta["label"] == label
    assert meta["outcome"] == "waiting"
    assert meta["dashboard_group"] == "running"
    assert status not in JOB_SUCCESS_STATUSES | JOB_FAILURE_STATUSES
    assert job_attention_group(status) == "running"
    assert job_attention_group("awaiting_approval") == "needs_attention"
