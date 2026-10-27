from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from core.application.jobs import workflow_resume_finalization_repository as finalization_module
from core.application.jobs import workflow_resume_repository as repository_module
from core.application.jobs.workflow_resume_state import (
    WORKFLOW_RESUME_FINALIZATION_STEPS,
    WORKFLOW_RESUME_MAX_ATTEMPTS,
    WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
    WORKFLOW_RESUME_RETRY_DELAY_SECONDS,
)
from core.application.management.errors import ServiceConflictError
from core.infrastructure.db.worker import WorkflowResumeRepositoryAdapter


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.rowcount = self._connection.rowcounts.pop(0)
        self._connection.calls.append((" ".join(str(sql).split()), tuple(params)))

    def fetchone(self) -> Any:
        return self._connection.rows.pop(0)

    def fetchall(self) -> list[Any]:
        rows = list(self._connection.rows)
        self._connection.rows.clear()
        return rows


class _Connection:
    def __init__(self, *rowcounts: int, rows: tuple[Any, ...] = ()) -> None:
        self.rowcounts = list(rowcounts)
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _FrozenDateTime:
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        assert tz is UTC
        return datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _freeze_repository_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(finalization_module, "datetime", _FrozenDateTime)


def _repository(
    *rowcounts: int,
    rows: tuple[Any, ...] = (),
) -> tuple[WorkflowResumeRepositoryAdapter, _Connection]:
    connection = _Connection(*rowcounts, rows=rows)
    return (
        WorkflowResumeRepositoryAdapter(
            SimpleNamespace(conn=connection),
            token_factory=lambda: "publish-token-7",
        ),
        connection,
    )


@pytest.mark.parametrize("rowcount", [1, 0])
def test_mark_pending_uses_prior_revision_cas(rowcount: int) -> None:
    updated = ("job-7", "project-7", "TASK-7", 8) if rowcount else None
    repository, connection = _repository(rowcount, rows=(updated,))
    job_context = {
        "job_id": "job-7",
        "project_id": "project-7",
        "task_id": "TASK-7",
        "meta": {"workflow_resume": {"revision": 7}},
    }

    if rowcount:
        result = repository.mark_pending(
            thread_id="thread-7",
            job_context=job_context,
            approval_id="gate-7",
            resume_value={"approved": True},
            approved=True,
            action="approve",
            comment="ship it",
            interrupt_payload={"stage": "review"},
            actor_username="alice",
        )
        assert result.resume_revision == 8
        assert result.publish_token == "publish-token-7"
        assert connection.commits == 1
        assert connection.rollbacks == 0
    else:
        with pytest.raises(ServiceConflictError):
            repository.mark_pending(
                thread_id="thread-7",
                job_context=job_context,
                approval_id="gate-7",
                resume_value={"approved": True},
                approved=True,
                action="approve",
                comment="ship it",
                interrupt_payload={"stage": "review"},
                actor_username="alice",
            )
        assert connection.commits == 0
        assert connection.rollbacks == 1

    sql, params = connection.calls[0]
    assert "status = 'awaiting_approval'" in sql
    assert "workflow_resume'->>'revision')::int, 0) = %s" in sql
    intent = json.loads(params[0])
    assert intent == {
        "state": "pending",
        "revision": 8,
        "approval_id": "gate-7",
        "resume_value": {"approved": True},
        "approved": True,
        "action": "approve",
        "comment": "ship it",
        "interrupt_payload": {"stage": "review"},
        "actor_username": "alice",
        "queued_at": "2026-07-20T12:00:00+00:00",
        "published_at": None,
        "lease_token": "",
        "lease_expires_at": None,
        "last_publish_error": "",
        "publish_token": "publish-token-7",
        "publish_lease_expires_at": None,
    }
    assert "make_interval(secs => %s)" in sql
    assert params[1:] == (
        WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
        "thread-7",
        7,
        8,
    )


@pytest.mark.parametrize(("rowcount", "claimed", "token"), [(1, True, "lease-7"), (0, False, "")])
def test_claim_execution_uses_revision_and_lease_cas(
    rowcount: int,
    claimed: bool,
    token: str,
) -> None:
    repository, connection = _repository(
        rowcount, rows=(({"execution_attempts": 1},),) if rowcount else ()
    )

    result = repository.claim_execution(
        job_id="job-7",
        resume_revision=7,
        worker_id="worker-7",
        lease_token="lease-7",
        lease_seconds=123,
    )

    assert result.claimed is claimed
    assert result.lease_token == token
    sql, params = connection.calls[0]
    assert "status = 'resume_pending'" in sql
    assert "status = 'in_progress'" in sql
    assert "lease_expires_at" in sql
    assert "make_interval(secs => %s)" in sql
    assert "<= NOW()" in sql
    assert params == (
        "worker-7",
        WORKFLOW_RESUME_MAX_ATTEMPTS,
        WORKFLOW_RESUME_MAX_ATTEMPTS,
        json.dumps("lease-7"),
        123,
        "job-7",
        7,
    )
    assert connection.commits == 1


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_renew_execution_uses_exact_execution_fence(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    renewed = repository.renew_execution(
        job_id="job-7",
        resume_revision=7,
        lease_token="lease-7",
        lease_seconds=123,
    )

    assert renewed is expected
    sql, params = connection.calls[0]
    assert "status = 'in_progress'" in sql
    assert "state', '') = 'running'" in sql
    assert "lease_token', '') = %s" in sql
    assert "lease_expires_at', '')::timestamptz" in sql
    assert "> NOW()" in sql
    assert "make_interval(secs => %s)" in sql
    assert params == (
        123,
        "job-7",
        7,
        "lease-7",
    )
    assert connection.commits == 1


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_mark_retryable_uses_exact_execution_fence(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    updated = repository.mark_retryable(
        job_id="job-7",
        resume_revision=7,
        lease_token="lease-7",
        error="retry later",
    )

    assert updated is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'running'" in sql
    assert "lease_token', '') = %s" in sql
    assert "lease_expires_at', '')::timestamptz" in sql
    assert "> NOW()" in sql
    assert params == (
        "retry later",
        WORKFLOW_RESUME_RETRY_DELAY_SECONDS,
        json.dumps("retry later"),
        "job-7",
        7,
        "lease-7",
    )
    assert connection.commits == 1


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_mark_finalizing_uses_execution_fence_and_reports_cas_loss(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)
    result_payload = {"status": "completed", "final_report": "done"}

    updated = repository.mark_finalizing(
        job_id="job-7",
        resume_revision=7,
        lease_token="lease-7",
        final_status="completed",
        result=result_payload,
        error=None,
    )

    assert updated is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'running'" in sql
    assert "state', '') IN ('running', 'finalizing')" not in sql
    assert "lease_token', '') = %s" in sql
    assert "lease_expires_at', '')::timestamptz" in sql
    assert "> NOW()" in sql
    assert "'lease_token', ''" in sql
    assert "'lease_expires_at', NULL" in sql
    assert params == (
        "completed",
        "completed",
        "completed",
        "done",
        "",
        "",
        json.dumps({}),
        json.dumps(dict.fromkeys(WORKFLOW_RESUME_FINALIZATION_STEPS, "pending")),
        json.dumps("completed"),
        json.dumps(result_payload),
        json.dumps(""),
        json.dumps("2026-07-20T12:00:00+00:00"),
        "job-7",
        7,
        "lease-7",
    )
    assert connection.commits == 1


def test_mark_finalizing_keeps_repeated_approval_gate_unfinished() -> None:
    repository, connection = _repository(1)

    updated = repository.mark_finalizing(
        job_id="job-7",
        resume_revision=7,
        lease_token="lease-7",
        final_status="awaiting_approval",
        result={"status": "awaiting_approval"},
    )

    assert updated is True
    sql, params = connection.calls[0]
    parked = "WHEN %s IN ('awaiting_approval', 'awaiting_timer', 'awaiting_child')"
    assert sql.count(parked) == 2
    assert params[:3] == (
        "awaiting_approval",
        "awaiting_approval",
        "awaiting_approval",
    )


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_finalization_journal_write_uses_revision_and_finalizer_cas(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    updated = repository.mark_finalization_step_completed(
        job_id="job-7",
        resume_revision=7,
        finalizer_token="finalizer-7",
        step="notification",
    )

    assert updated is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'finalizing'" in sql
    assert "finalizer_token', '') = %s" in sql
    assert "finalizer_expires_at', '')::timestamptz" in sql
    assert "> NOW()" in sql
    assert "'{workflow_resume,finalization_steps}'" in sql
    assert params == ("notification", "job-7", 7, "finalizer-7")
    assert connection.commits == 1


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_claim_finalizer_uses_revision_and_expired_or_empty_token_cas(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    claimed = repository.claim_finalizer(
        job_id="job-7",
        resume_revision=7,
        finalizer_token="finalizer-7",
        lease_seconds=123,
    )

    assert claimed is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'finalizing'" in sql
    assert "finalizer_token', '') = ''" in sql
    assert "<= NOW()" in sql
    assert "make_interval(secs => %s)" in sql
    assert params == (
        json.dumps("finalizer-7"),
        123,
        "job-7",
        7,
    )
    assert connection.commits == 1


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_renew_finalizer_uses_exact_finalizer_fence(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    renewed = repository.renew_finalizer(
        job_id="job-7",
        resume_revision=7,
        finalizer_token="finalizer-7",
        lease_seconds=123,
    )

    assert renewed is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'finalizing'" in sql
    assert "finalizer_token', '') = %s" in sql
    assert "finalizer_expires_at', '')::timestamptz" in sql
    assert "> NOW()" in sql
    assert "make_interval(secs => %s)" in sql
    assert params == (
        123,
        "job-7",
        7,
        "finalizer-7",
    )
    assert connection.commits == 1


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_release_finalizer_uses_exact_finalizer_fence(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    released = repository.release_finalizer(
        job_id="job-7",
        resume_revision=7,
        finalizer_token="finalizer-7",
        error="finalizer failed",
    )

    assert released is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'finalizing'" in sql
    assert "finalizer_token', '') = %s" in sql
    assert "finalizer_expires_at', '')::timestamptz" in sql
    assert "> NOW()" in sql
    assert params == (
        "finalizer failed",
        WORKFLOW_RESUME_MAX_ATTEMPTS,
        "finalizer failed",
        WORKFLOW_RESUME_MAX_ATTEMPTS,
        WORKFLOW_RESUME_RETRY_DELAY_SECONDS,
        "job-7",
        7,
        "finalizer-7",
    )
    assert connection.commits == 1


@pytest.mark.parametrize(
    ("lease_token", "finalizer_token", "rowcount", "expected"),
    [
        ("execution-7", "", 1, True),
        ("", "finalizer-7", 1, True),
        ("execution-7", "", 0, False),
        ("", "finalizer-7", 0, False),
    ],
)
def test_complete_keeps_execution_and_finalizer_cas_branches(
    lease_token: str,
    finalizer_token: str,
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    completed = repository.complete(
        job_id="job-7",
        resume_revision=7,
        lease_token=lease_token,
        finalizer_token=finalizer_token,
    )

    assert completed is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'running'" in sql
    assert "state', '') = 'finalizing'" in sql
    assert "NOT EXISTS" not in sql
    assert "'finalizer_token', ''" in sql
    assert "'finalizer_expires_at', NULL" in sql
    assert "finalizer_expires_at', '')::timestamptz" in sql
    assert "> NOW()" in sql
    assert "finalization_steps', '{}'::jsonb" in sql
    assert "= %s::jsonb" in sql
    assert params == (
        "job-7",
        7,
        lease_token,
        lease_token,
        finalizer_token,
        finalizer_token,
        json.dumps(dict.fromkeys(WORKFLOW_RESUME_FINALIZATION_STEPS, "completed")),
    )
    assert connection.commits == 1


def test_pending_rows_atomically_reserve_publish_lease() -> None:
    reserved_row = (
        "job-7",
        "project-7",
        "TASK-7",
        "thread-7",
        7,
        "publish_failed",
        "publish-token-7",
    )
    repository, connection = _repository(1, rows=(reserved_row,))

    rows = repository.pending_rows(batch_size=5)

    assert rows == [
        {
            "id": "job-7",
            "project_id": "project-7",
            "task_id": "TASK-7",
            "thread_id": "thread-7",
            "resume_revision": 7,
            "publish_state": "publish_failed",
            "publish_token": "publish-token-7",
        }
    ]
    sql, params = connection.calls[0]
    assert "WITH candidates AS" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "UPDATE jobs AS job" in sql
    assert "publish_token" in sql
    assert "publish_lease_expires_at" in sql
    assert "make_interval(secs => %s)" in sql
    assert "'awaiting_approval'" in sql
    assert "= 'finalizing'" in sql
    assert "RETURNING" in sql
    assert params == (
        5,
        json.dumps("publish-token-7"),
        WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
    )
    assert connection.commits == 1


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_mark_publish_state_uses_revision_token_and_expected_state_cas(
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)

    updated = repository.mark_publish_state(
        job_id="job-7",
        resume_revision=7,
        publish_token="publish-token-7",
        expected_state="publish_failed",
        state="published",
    )

    assert updated is expected
    sql, params = connection.calls[0]
    assert "state', '') = %s" in sql
    assert "publish_token', '') = %s" in sql
    assert "'publish_token', ''" in sql
    assert "'publish_lease_expires_at', NULL" in sql
    assert params == (
        "",
        json.dumps("published"),
        json.dumps("2026-07-20T12:00:00+00:00"),
        json.dumps(""),
        "job-7",
        7,
        "publish_failed",
        "publish-token-7",
    )
    assert connection.commits == 1


@pytest.mark.parametrize(
    ("method", "error", "target_state"),
    [("mark_published", "", "published"), ("mark_publish_failed", "broker down", "publish_failed")],
)
@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_command_publish_transition_uses_pending_token_cas(
    method: str,
    error: str,
    target_state: str,
    rowcount: int,
    expected: bool,
) -> None:
    repository, connection = _repository(rowcount)
    transition = getattr(repository, method)
    kwargs = {
        "thread_id": "thread-7",
        "resume_revision": 7,
        "publish_token": "publish-token-7",
    }
    if error:
        kwargs["error"] = error

    updated = transition(**kwargs)

    assert updated is expected
    sql, params = connection.calls[0]
    assert "state', '') = 'pending'" in sql
    assert "publish_token', '') = %s" in sql
    assert "'publish_token', ''" in sql
    assert "'publish_lease_expires_at', NULL" in sql
    assert json.dumps(target_state) in params
    assert params[-3:] == (
        "thread-7",
        7,
        "publish-token-7",
    )
    assert connection.commits == 1
