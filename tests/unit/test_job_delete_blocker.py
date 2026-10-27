from __future__ import annotations

import pytest

from core.application.management.command_services_common import _job_delete_blocker


@pytest.mark.parametrize("status", ["awaiting_approval", "awaiting_timer", "awaiting_child"])
def test_a_run_paused_on_a_durable_wait_is_active_for_delete(status: str) -> None:
    assert _job_delete_blocker({"status": status}) == ("active", status)
    assert _job_delete_blocker({"status": status}, allow_quiescing=True) == ("active", status)


def test_a_finished_run_is_not_blocked() -> None:
    assert _job_delete_blocker({"status": "completed", "finished_at": "2026-10-12"}) is None
