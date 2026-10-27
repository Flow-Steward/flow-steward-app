"""Status polling tells which For each item a paused or running step is at (spec §15)."""

from __future__ import annotations

from core.application.jobs.job_polling import _active_step
from core.application.management.job_public_projection import job_public_summary_projection

PROGRESS = {
    "for_each_id": "each_order",
    "item_index": 2,
    "selected_count": 4,
    "source_count": 4,
    "applied_limit": 1000,
    "processed_count": 2,
    "truncated": False,
}
STEP = {
    "step_id": "check_customer",
    "step_execution_id": "e-1",
    "attempt": 1,
    "group_id": None,
    "for_each_id": "each_order",
    "lane_id": None,
    "iteration": 2,
    "status": "awaiting_child",
    # Anything else a frame might carry stays out of the polling surfaces.
    "for_each_progress": {**PROGRESS, "items": [{"email": "a@example.test"}]},
}


def test_polling_keeps_the_for_each_and_its_bounded_counts() -> None:
    row = _active_step(STEP)

    assert row["for_each_id"] == "each_order"
    assert row["for_each_progress"] == PROGRESS


def test_the_public_summary_keeps_them_too() -> None:
    row = job_public_summary_projection({"id": "j-1", "active_steps": [STEP]})["active_steps"][0]

    assert row["for_each_id"] == "each_order"
    assert row["for_each_progress"] == PROGRESS


def test_a_step_outside_a_for_each_has_no_progress() -> None:
    plain = {key: value for key, value in STEP.items() if key != "for_each_progress"}

    assert "for_each_progress" not in _active_step(plain)
    assert (
        "for_each_progress"
        not in job_public_summary_projection({"active_steps": [plain]})["active_steps"][0]
    )
