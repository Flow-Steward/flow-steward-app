from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.management.errors import ServiceConflictError
from core.application.workflows.workflow_deletion_guard import (
    assert_workflow_has_no_nonterminal_jobs,
)


def _store(rows: list[dict]) -> MagicMock:
    store = MagicMock()
    cursor = store.conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows
    return store


def test_workflow_deletion_guard_returns_bounded_typed_blockers() -> None:
    rows = [
        {"id": f"job-{index}", "status": "awaiting_approval" if index == 0 else "queued"}
        for index in range(25)
    ]
    store = _store(rows)

    with pytest.raises(ServiceConflictError) as exc_info:
        assert_workflow_has_no_nonterminal_jobs(
            store,
            workflow_id="workflow-gated",
            account_id="account-1",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.payload["code"] == "workflow_has_nonterminal_jobs"
    assert exc_info.value.payload["workflow_id"] == "workflow-gated"
    assert len(exc_info.value.payload["blocking_jobs"]) == 20
    assert exc_info.value.payload["blocking_jobs"][0] == {
        "job_id": "job-0",
        "status": "awaiting_approval",
    }
    sql, params = store.conn.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert "selected_workflow_key" in sql
    assert "selected_workflow_id::text" in sql
    assert "meta->>'selected_workflow_id'" in sql
    assert "<> ALL" in sql
    assert params[-1] == 21


def test_workflow_deletion_guard_allows_only_terminal_references() -> None:
    store = _store([])

    assert_workflow_has_no_nonterminal_jobs(
        store,
        workflow_id="workflow-finished",
        account_id="account-1",
    )


def test_workflow_deletion_guard_fails_closed_without_job_storage() -> None:
    with pytest.raises(ServiceConflictError) as exc_info:
        assert_workflow_has_no_nonterminal_jobs(
            object(),
            workflow_id="workflow-unknown",
            account_id="account-1",
        )

    assert exc_info.value.payload["code"] == "workflow_dependency_check_unavailable"
