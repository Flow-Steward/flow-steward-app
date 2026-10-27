from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from core.application.idempotency.models import IdempotencyScope
from core.application.idempotency.repository import RequestIdempotencyRepository
from core.application.idempotency.service import RequestIdempotencyService

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


class _MemoryRepository:
    def __init__(self, row: dict | None = None) -> None:
        self.row = dict(row) if row else None
        self.accepted: dict | None = None
        self.failed: dict | None = None

    def insert_pending(self, **values):
        if self.row is not None:
            return None
        self.row = {
            **values,
            "status": "pending",
            "job_id": None,
            "replay_response_jsonb": None,
            "result_expires_at": None,
            "tombstone_expires_at": None,
            "updated_at": NOW,
        }
        return dict(self.row)

    def load_for_update(self, *, scope_key: str, key_digest: str):
        if not self.row:
            return None
        assert self.row["scope_key"] == scope_key
        assert self.row["key_digest"] == key_digest
        return dict(self.row)

    def reclaim(self, **values):
        assert self.row is not None
        self.row.update(
            request_id=values["request_id"],
            request_fingerprint=values["request_fingerprint"],
            status="pending",
            job_id=None,
            replay_response_jsonb=None,
            result_expires_at=None,
            tombstone_expires_at=None,
            lease_expires_at=values["lease_expires_at"],
            updated_at=NOW,
        )
        return dict(self.row)

    def renew(self, **values):
        if (
            self.row is None
            or self.row.get("request_id") != values["request_id"]
            or self.row.get("status") != "pending"
            or self.row.get("lease_expires_at") <= NOW
        ):
            return False
        self.row["lease_expires_at"] = values["lease_expires_at"]
        return True

    def journal(self, **values):
        if (
            self.row is None
            or self.row.get("request_id") != values["request_id"]
            or self.row.get("status") != "pending"
        ):
            return False
        self.row["replay_response_jsonb"] = dict(values["journal"])
        return True

    def accept(self, **values):
        self.accepted = dict(values)

    def fail(self, **values):
        self.failed = dict(values)


def _scope() -> IdempotencyScope:
    return IdempotencyScope(
        operation="task.launch",
        account_id="account-1",
        dimensions=(("project", "project-1"), ("workflow", "default")),
    )


def _stored_row(**overrides):
    scope = _scope()
    service = RequestIdempotencyService(_MemoryRepository(), now=lambda: NOW)
    first = service.claim(
        scope=scope,
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-1",
    )
    row = {
        "scope_key": first.scope_key,
        "key_digest": first.key_digest,
        "request_id": "request-1",
        "request_fingerprint": "sha256:request-a",
        "status": "pending",
        "job_id": None,
        "replay_response_jsonb": None,
        "result_expires_at": None,
        "tombstone_expires_at": None,
        "lease_expires_at": NOW + timedelta(seconds=120),
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def test_scope_key_is_stable_and_does_not_require_an_entrypoint() -> None:
    forward = _scope()
    reversed_dimensions = IdempotencyScope(
        operation="task.launch",
        account_id="account-1",
        dimensions=(("workflow", "default"), ("project", "project-1")),
    )

    assert forward.key() == reversed_dimensions.key()
    assert forward.key().startswith("request:v1:")
    assert "workflow_entrypoint" not in forward.key()


def test_claim_replays_the_exact_accepted_response() -> None:
    response = {"job_id": "job-1", "status": "pending", "file_count": 1}
    repository = _MemoryRepository(
        _stored_row(
            status="accepted",
            job_id="job-1",
            replay_response_jsonb=response,
            result_expires_at=NOW + timedelta(days=30),
            tombstone_expires_at=NOW + timedelta(days=37),
        )
    )

    claim = RequestIdempotencyService(repository, now=lambda: NOW).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )

    assert claim.outcome == "replay"
    assert claim.job_id == "job-1"
    assert claim.response == response


def test_claim_rejects_a_fingerprint_mismatch_before_recovery() -> None:
    repository = _MemoryRepository(_stored_row(updated_at=NOW - timedelta(hours=1)))

    claim = RequestIdempotencyService(repository, now=lambda: NOW).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-b",
        request_id="request-2",
    )

    assert claim.outcome == "mismatch"
    assert repository.row["request_id"] == "request-1"


def test_claim_recovers_an_expired_pending_lease() -> None:
    repository = _MemoryRepository(_stored_row(lease_expires_at=NOW - timedelta(seconds=1)))

    claim = RequestIdempotencyService(
        repository,
        now=lambda: NOW,
        stale_after=timedelta(seconds=120),
    ).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )

    assert claim.outcome == "claimed"
    assert claim.recovered is True
    assert repository.row["request_id"] == "request-2"


def test_claim_does_not_reclaim_when_linked_submission_is_still_preparing() -> None:
    repository = _MemoryRepository(_stored_row(lease_expires_at=NOW - timedelta(seconds=1)))
    guarded_rows: list[dict] = []

    claim = RequestIdempotencyService(
        repository,
        now=lambda: NOW,
        reclaim_guard=lambda row: guarded_rows.append(dict(row)) or False,
    ).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )

    assert claim.outcome == "in_progress"
    assert repository.row["request_id"] == "request-1"
    assert guarded_rows[0]["request_id"] == "request-1"


def test_recovered_claim_exposes_the_previous_pending_journal() -> None:
    journal = {"pending_artifact_ids": ["art_reserved"]}
    repository = _MemoryRepository(
        _stored_row(
            lease_expires_at=NOW - timedelta(seconds=1),
            replay_response_jsonb=journal,
        )
    )

    claim = RequestIdempotencyService(repository, now=lambda: NOW).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )

    assert claim.recovered is True
    assert claim.response == journal


def test_journal_is_fenced_to_the_current_pending_claim() -> None:
    repository = _MemoryRepository()
    service = RequestIdempotencyService(repository, now=lambda: NOW)
    claim = service.claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-1",
    )

    assert service.journal(
        claim=claim,
        journal={"pending_artifact_ids": ["art_reserved"]},
    )
    assert repository.row["replay_response_jsonb"] == {"pending_artifact_ids": ["art_reserved"]}


def test_claim_keeps_a_live_matching_reservation_in_progress() -> None:
    repository = _MemoryRepository(_stored_row(lease_expires_at=NOW + timedelta(seconds=30)))

    claim = RequestIdempotencyService(repository, now=lambda: NOW).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )

    assert claim.outcome == "in_progress"
    assert repository.row["request_id"] == "request-1"


def test_renew_fences_the_request_replaced_after_lease_expiry() -> None:
    repository = _MemoryRepository(_stored_row(lease_expires_at=NOW - timedelta(seconds=1)))
    service = RequestIdempotencyService(repository, now=lambda: NOW)

    replacement = service.claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )
    prior_claim = type(replacement)(
        outcome="claimed",
        scope_key=replacement.scope_key,
        key_digest=replacement.key_digest,
        request_fingerprint=replacement.request_fingerprint,
        request_id="request-1",
    )

    assert replacement.recovered is True
    assert service.renew(claim=prior_claim) is False
    assert service.renew(claim=replacement) is True


def test_claim_returns_expired_while_the_tombstone_is_retained() -> None:
    repository = _MemoryRepository(
        _stored_row(
            status="accepted",
            replay_response_jsonb={"job_id": "job-1"},
            result_expires_at=NOW - timedelta(seconds=1),
            tombstone_expires_at=NOW + timedelta(days=7),
        )
    )

    claim = RequestIdempotencyService(repository, now=lambda: NOW).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )

    assert claim.outcome == "expired"
    assert claim.response is None


def test_claim_replays_an_expired_result_while_its_job_is_retained() -> None:
    repository = _MemoryRepository(
        _stored_row(
            status="accepted",
            job_id="job-1",
            replay_response_jsonb={"job_id": "job-1"},
            result_expires_at=NOW - timedelta(seconds=1),
            tombstone_expires_at=NOW + timedelta(days=7),
        )
    )

    claim = RequestIdempotencyService(repository, now=lambda: NOW).claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-2",
    )

    assert claim.outcome == "replay"
    assert claim.job_id == "job-1"


class _Cursor:
    description: ClassVar[list[tuple[str]]] = [
        ("scope_key",),
        ("key_digest",),
        ("request_id",),
        ("request_fingerprint",),
        ("status",),
        ("job_id",),
        ("replay_response_jsonb",),
        ("result_expires_at",),
        ("tombstone_expires_at",),
        ("updated_at",),
    ]

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=()) -> None:
        self.executed.append((str(statement), tuple(params or ())))

    def fetchone(self):
        return None


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.commit_calls = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commit_calls += 1


def test_repository_uses_the_pinned_connection_without_hidden_commits() -> None:
    connection = _Connection()
    repository = RequestIdempotencyRepository(connection)

    repository.insert_pending(
        scope_key=_scope().key(),
        key_digest="sha256:key",
        request_id="request-1",
        request_fingerprint="sha256:request-a",
        lease_expires_at=NOW + timedelta(seconds=120),
    )
    repository.load_for_update(scope_key=_scope().key(), key_digest="sha256:key")
    repository.reclaim(
        scope_key=_scope().key(),
        key_digest="sha256:key",
        request_id="request-2",
        request_fingerprint="sha256:request-a",
        lease_expires_at=NOW + timedelta(seconds=120),
    )
    repository.accept(
        scope_key=_scope().key(),
        key_digest="sha256:key",
        request_id="request-2",
        request_fingerprint="sha256:request-a",
        job_id="11111111-1111-4111-8111-111111111111",
        response={"job_id": "11111111-1111-4111-8111-111111111111"},
        result_expires_at=NOW + timedelta(days=30),
        tombstone_expires_at=NOW + timedelta(days=37),
    )
    repository.fail(
        scope_key=_scope().key(),
        key_digest="sha256:key",
        request_id="request-2",
        request_fingerprint="sha256:request-a",
        error_code="publish_failed",
    )

    assert connection.commit_calls == 0
    reclaim_sql = connection.cursor_instance.executed[2][0]
    assert "tombstone_expires_at = now() + INTERVAL '1 day'" in reclaim_sql
    assert "tombstone_expires_at = NULL" not in reclaim_sql
    assert all("workflow_entrypoints" not in sql for sql, _ in connection.cursor_instance.executed)
    assert any("FOR UPDATE" in sql for sql, _ in connection.cursor_instance.executed)
    insert_sql = connection.cursor_instance.executed[0][0]
    fail_sql = connection.cursor_instance.executed[-1][0]
    assert "tombstone_expires_at" in insert_sql
    assert "now() + INTERVAL '1 day'" in fail_sql


def test_repository_finds_a_durable_job_by_internal_request_identity() -> None:
    class _JobCursor(_Cursor):
        description: ClassVar[list[tuple[str]]] = [
            ("id",),
            ("project_id",),
            ("task_id",),
            ("task_summary",),
            ("task_source",),
            ("thread_id",),
            ("mode",),
            ("status",),
            ("worker_id",),
            ("meta",),
        ]

        def fetchone(self):
            return (
                "11111111-1111-4111-8111-111111111111",
                "project-1",
                "source-42",
                "Review invoice",
                "manual",
                "thread-1",
                None,
                "in_progress",
                None,
                {"idempotency": {"request_fingerprint": "sha256:request-a"}},
            )

    connection = _Connection()
    connection.cursor_instance = _JobCursor()

    row = RequestIdempotencyRepository(connection).find_job_by_request_identity(
        scope_key="request:v1:scope",
        key_digest="sha256:key",
    )

    assert row is not None
    assert row["id"] == "11111111-1111-4111-8111-111111111111"
    sql, params = connection.cursor_instance.executed[-1]
    assert "meta #>> '{idempotency,scope_key}'" in sql
    assert "meta #>> '{idempotency,key_digest}'" in sql
    assert "ORDER BY queued_at ASC NULLS LAST, id ASC" in sql
    assert "created_at" not in sql
    assert params == (
        "request:v1:scope",
        "sha256:key",
        "request:v1:scope",
        "sha256:key",
    )


def test_accept_persists_the_exact_response_and_expiry() -> None:
    repository = _MemoryRepository()
    service = RequestIdempotencyService(repository, now=lambda: NOW)
    claim = service.claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-1",
    )
    response = {"job_id": "job-1", "status": "pending"}
    expires_at = NOW + timedelta(days=30)

    service.accept(
        claim=claim,
        job_id="job-1",
        response=response,
        result_expires_at=expires_at,
    )

    assert repository.accepted is not None
    assert repository.accepted["response"] == response
    assert repository.accepted["result_expires_at"] == expires_at
    assert repository.accepted["tombstone_expires_at"] > expires_at


def test_fail_marks_only_the_owned_claim_as_retryable() -> None:
    repository = _MemoryRepository()
    service = RequestIdempotencyService(repository, now=lambda: NOW)
    claim = service.claim(
        scope=_scope(),
        opaque_key="launch-42",
        request_fingerprint="sha256:request-a",
        request_id="request-1",
    )

    service.fail(claim=claim, error_code="publish_failed")

    assert repository.failed is not None
    assert repository.failed["error_code"] == "publish_failed"
