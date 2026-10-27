"""Project execution lease persistence regressions."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.infrastructure.db import project_execution_store


def test_prune_keeps_unexpired_lease_when_job_projection_is_terminal() -> None:
    cur = MagicMock()

    project_execution_store._prune_project_execution_rows(
        cur=cur,
        table_name="project_execution_slots",
        project_id="project-1",
    )

    sql, params = cur.execute.call_args.args
    assert "expires_at <= now()" in sql
    assert "lease_job.status" not in sql
    assert params == ("project-1",)
