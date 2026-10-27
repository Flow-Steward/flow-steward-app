from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime


class _EventStore:
    def __init__(
        self,
        rows: list[dict],
        *,
        finalize_result: dict | None = None,
    ) -> None:
        self.rows = rows
        self.finalize_result = finalize_result or {"status": "published"}
        self.claim_calls: list[dict] = []
        self.status_calls: list[tuple[str, dict]] = []
        self.ledger_calls: list[dict] = []
        self.conn = self

    @contextmanager
    def transaction(self):
        yield

    @contextmanager
    def claimed_domain_event_effect(self, _outbox_id: str, **_claim):
        yield dict(self.rows[0]) if self.rows and self.finalize_result is not None else None

    def claim_pending_domain_event_outbox(self, **kwargs):
        self.claim_calls.append(dict(kwargs))
        return list(self.rows)

    def mark_domain_event_outbox_status(self, outbox_id: str, **kwargs):
        self.status_calls.append((str(outbox_id), dict(kwargs)))
        return self.finalize_result

    def record_domain_event(self, **kwargs):
        self.ledger_calls.append(dict(kwargs))


class _JobAwareEventStore(_EventStore):
    """An event store that can be asked whether the job still exists."""

    def __init__(self, rows: list[dict], *, job_exists: bool, **kwargs) -> None:
        super().__init__(rows, **kwargs)
        self.job_exists = job_exists
        self.existence_queries: list[tuple[str, tuple]] = []

    def cursor(self):
        store = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, sql: str, params=()):
                store.existence_queries.append((sql, tuple(params)))

            def fetchone(self):
                return (1,) if store.job_exists else None

        return _Cursor()


def test_terminal_curation_batch_claims_only_terminal_events_and_marks_success() -> None:
    from core.infrastructure.runtime.worker.terminal_curation import (
        TERMINAL_MEMORY_CURATION_EVENT_TYPE,
        process_terminal_curation_batch,
    )

    store = _EventStore(
        [
            {
                "id": "outbox-1",
                "account_id": "acct-1",
                "event_id": "job-terminal-memory:job-1",
                "event_type": TERMINAL_MEMORY_CURATION_EVENT_TYPE,
                "job_id": "job-1",
                "payload": {"terminal_status": "completed"},
                "envelope": {"correlation_id": "corr-1"},
                "claim_owner": "worker-1",
                "claim_token": "claim-1",
                "claim_version": 1,
            }
        ]
    )
    curated: list[str] = []

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=lambda *, job_id, logger_instance: curated.append(str(job_id)) or [],
        worker_id="worker-1",
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert processed == 1
    assert store.claim_calls == [
        {
            "limit": 25,
            "lease_seconds": 30,
            "event_types": (TERMINAL_MEMORY_CURATION_EVENT_TYPE,),
            "claim_owner": "worker-1",
        }
    ]
    assert curated == ["job-1"]
    assert store.status_calls == [
        (
            "outbox-1",
            {
                "status": "published",
                "claim_owner": "worker-1",
                "claim_token": "claim-1",
                "claim_version": 1,
            },
        )
    ]
    assert store.ledger_calls == [
        {
            "event_id": "job-terminal-memory:job-1",
            "event_type": TERMINAL_MEMORY_CURATION_EVENT_TYPE,
            "account_id": "acct-1",
            "job_id": "job-1",
            "aggregate_type": "job",
            "aggregate_id": "job-1",
            "payload": {"terminal_status": "completed"},
            "envelope": {"correlation_id": "corr-1"},
            "status": "succeeded",
            "summary": "Terminal job memory curation succeeded",
        }
    ]


def test_terminal_curation_batch_releases_failed_event_for_retry() -> None:
    from core.infrastructure.runtime.worker.terminal_curation import (
        TERMINAL_MEMORY_CURATION_EVENT_TYPE,
        process_terminal_curation_batch,
    )

    store = _EventStore(
        [
            {
                "id": "outbox-2",
                "account_id": "acct-1",
                "event_id": "job-terminal-memory:job-2",
                "event_type": TERMINAL_MEMORY_CURATION_EVENT_TYPE,
                "job_id": "job-2",
                "payload": {"terminal_status": "failed"},
                "envelope": {"correlation_id": "corr-2"},
                "claim_owner": "worker-1",
                "claim_token": "claim-2",
                "claim_version": 3,
            }
        ]
    )

    def fail_curator(**_kwargs):
        raise RuntimeError("provider unavailable")

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=fail_curator,
        worker_id="worker-1",
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert processed == 0
    assert store.status_calls == [
        (
            "outbox-2",
            {
                "status": "pending",
                "last_error": "provider unavailable",
                "available_at": "2026-08-29T00:00:30+00:00",
                "claim_owner": "worker-1",
                "claim_token": "claim-2",
                "claim_version": 3,
            },
        )
    ]
    assert store.ledger_calls == [
        {
            "event_id": "job-terminal-memory:job-2",
            "event_type": TERMINAL_MEMORY_CURATION_EVENT_TYPE,
            "account_id": "acct-1",
            "job_id": "job-2",
            "aggregate_type": "job",
            "aggregate_id": "job-2",
            "payload": {"terminal_status": "failed"},
            "envelope": {"correlation_id": "corr-2"},
            "status": "failed",
            "summary": "Terminal job memory curation failed",
        }
    ]


def test_terminal_curation_stale_claim_cannot_finalize_or_record_success() -> None:
    from core.infrastructure.runtime.worker.terminal_curation import (
        TERMINAL_MEMORY_CURATION_EVENT_TYPE,
        process_terminal_curation_batch,
    )

    row = {
        "id": "outbox-stale",
        "account_id": "acct-1",
        "event_id": "job-terminal-memory:job-stale",
        "event_type": TERMINAL_MEMORY_CURATION_EVENT_TYPE,
        "job_id": "job-stale",
        "payload": {"terminal_status": "completed"},
        "envelope": {},
        "claim_owner": "worker-a",
        "claim_token": "claim-a",
        "claim_version": 1,
    }
    store = _EventStore([row])
    store.finalize_result = None
    curated: list[str] = []

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=lambda *, job_id, logger_instance: curated.append(str(job_id)) or [],
        worker_id="worker-a",
    )

    assert curated == []
    assert processed == 0
    assert store.ledger_calls == []


def test_terminal_curation_unavailable_effect_claim_is_non_fatal_and_skips_work() -> None:
    from core.infrastructure.runtime.worker.terminal_curation import (
        TERMINAL_MEMORY_CURATION_EVENT_TYPE,
        process_terminal_curation_batch,
    )

    store = _EventStore(
        [
            {
                "id": "outbox-retry",
                "event_type": TERMINAL_MEMORY_CURATION_EVENT_TYPE,
                "job_id": "job-retry",
                "claim_owner": "worker-a",
                "claim_token": "claim-a",
                "claim_version": 1,
            }
        ],
    )
    store.finalize_result = None
    curated: list[str] = []

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=lambda **_kwargs: curated.append("called") or [],
        worker_id="worker-a",
    )

    assert processed == 0
    assert curated == []
    assert store.status_calls == []
    assert store.ledger_calls == []


def _curation_row() -> dict:
    return {
        "id": "outbox-1",
        "job_id": "job-1",
        "event_id": "event-1",
        "account_id": "account-1",
        "claim_token": "token-1",
        "claim_version": 1,
        "payload": {},
        "envelope": {},
    }


def test_curation_skips_a_job_that_was_deleted_and_does_not_retry_it() -> None:
    """OPS-005: a deleted job left curation retrying forever and logging failures.

    The ledger row that records the outcome carries the same job_id, so even the
    bookkeeping hit the foreign key. Nothing about a deleted job can be curated.
    """
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    store = _JobAwareEventStore([_curation_row()], job_exists=False)
    curated: list[str] = []

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=lambda **kwargs: curated.append(kwargs["job_id"]),
        worker_id="worker-1",
    )

    assert processed == 0
    assert curated == [], "a deleted job has nothing to curate"
    assert store.ledger_calls == [], "a ledger row would carry the missing job_id"
    assert store.status_calls == [
        (
            "outbox-1",
            {
                "status": "published",
                "claim_owner": "worker-1",
                "claim_token": "token-1",
                "claim_version": 1,
            },
        )
    ], "the event is closed out, not left pending for another attempt"


def test_curation_still_runs_when_the_job_is_present() -> None:
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    store = _JobAwareEventStore([_curation_row()], job_exists=True)
    curated: list[str] = []

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=lambda **kwargs: curated.append(kwargs["job_id"]),
        worker_id="worker-1",
    )

    assert processed == 1
    assert curated == ["job-1"]
    assert store.existence_queries, "the check must actually ask the database"


def test_curation_keeps_the_work_when_the_check_itself_fails() -> None:
    """An unanswerable question is not a licence to drop a job's curation."""
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    class _BrokenConn(_EventStore):
        def cursor(self):
            raise RuntimeError("connection is not available")

    store = _BrokenConn([_curation_row()])
    curated: list[str] = []

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=lambda **kwargs: curated.append(kwargs["job_id"]),
        worker_id="worker-1",
    )

    assert processed == 1
    assert curated == ["job-1"]


# ---------------------------------------------------------------------------
# The deletion race
#
# Checking that the job exists narrows the window; it does not close it. A job
# deleted between the check and the write leaves the same dangling foreign key,
# and the row goes back to pending for a worker that will hit it again on every
# pass — the bookkeeping that records the failure needs the same missing key, so
# the retry can never succeed and never stops.
# ---------------------------------------------------------------------------


class _ForeignKeyViolation(Exception):
    """Stands in for psycopg's ForeignKeyViolation, which carries this sqlstate."""

    sqlstate = "23503"


def test_a_job_deleted_after_the_check_does_not_retry_forever() -> None:
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    store = _JobAwareEventStore([_curation_row()], job_exists=True)

    def curator_racing_a_delete(**_kwargs):
        raise _ForeignKeyViolation(
            'insert or update on table "domain_event_ledger" violates foreign key '
            'constraint "domain_event_ledger_job_id_fkey"'
        )

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=curator_racing_a_delete,
        worker_id="worker-1",
    )

    statuses = [kwargs.get("status") for _outbox, kwargs in store.status_calls]
    assert "pending" not in statuses, "a vanished job must not be queued for another attempt"
    assert statuses == ["published"], statuses
    assert store.ledger_calls == [], "a ledger row would carry the same missing job_id"
    assert processed == 0, "nothing was curated; it is closed, not counted as work done"


def test_an_ordinary_curation_failure_is_still_retried() -> None:
    """Only the vanished-job shape is closed out. A real fault must come back."""
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    store = _JobAwareEventStore([_curation_row()], job_exists=True)

    def broken_curator(**_kwargs):
        raise RuntimeError("the curator itself is broken")

    process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=broken_curator,
        worker_id="worker-1",
    )

    statuses = [kwargs.get("status") for _outbox, kwargs in store.status_calls]
    assert statuses == ["pending"], statuses


def test_the_ledger_write_racing_a_delete_does_not_reopen_the_event() -> None:
    """The event is finalized before the ledger row is written, so a delete that
    lands in that gap must not turn a closed event back into pending work."""
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    class _LedgerRacingDelete(_JobAwareEventStore):
        def record_domain_event(self, **kwargs):
            raise _ForeignKeyViolation("domain_event_ledger job_id no longer exists")

    store = _LedgerRacingDelete([_curation_row()], job_exists=True)

    processed = process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=lambda **kwargs: None,
        worker_id="worker-1",
    )

    statuses = [kwargs.get("status") for _outbox, kwargs in store.status_calls]
    assert statuses == ["published"], statuses
    assert processed == 1


def test_an_unrelated_foreign_key_conflict_is_still_retried() -> None:
    """Any 23503 was read as "the job vanished", so a violation on a different
    relation closed the event and dropped the work with no retry and no trace."""
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    class _OtherRelationViolation(Exception):
        sqlstate = "23503"

        def __str__(self) -> str:
            return (
                'insert or update on table "memory_records" violates foreign key '
                'constraint "memory_records_project_id_fkey"'
            )

    store = _JobAwareEventStore([_curation_row()], job_exists=True)

    def curator(**_kwargs):
        raise _OtherRelationViolation()

    process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=curator,
        worker_id="worker-1",
    )

    statuses = [kwargs.get("status") for _outbox, kwargs in store.status_calls]
    assert statuses == ["pending"], statuses


def test_a_violation_naming_the_job_key_is_still_treated_as_vanished() -> None:
    from core.infrastructure.runtime.worker.terminal_curation import (
        process_terminal_curation_batch,
    )

    class _JobViolation(Exception):
        sqlstate = "23503"

        def __str__(self) -> str:
            return (
                'insert or update on table "domain_event_ledger" violates foreign '
                'key constraint "domain_event_ledger_job_id_fkey"'
            )

    store = _JobAwareEventStore([_curation_row()], job_exists=True)

    def curator(**_kwargs):
        raise _JobViolation()

    process_terminal_curation_batch(
        event_store=store,
        memory_store=object(),
        curate_terminal_job=curator,
        worker_id="worker-1",
    )

    statuses = [kwargs.get("status") for _outbox, kwargs in store.status_calls]
    assert statuses == ["published"], statuses
