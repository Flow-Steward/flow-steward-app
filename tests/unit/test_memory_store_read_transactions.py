"""Reads must not leave a transaction open.

Remediation epic 2026-07-29 — the shared root cause of RD-001 and RD-002.

`_fetchone` / `_fetchall` executed a `SELECT` on the repository's connection and
returned without commit or rollback. On a non-autocommit connection that leaves
the transaction open, which pins a snapshot and holds any row locks the
statement took.

Observed in production: two connections sat `idle in transaction` for 24 and 28
minutes, both on the job-detail query. `pg_blocking_pids` showed everything else
queued behind them —

    28825  SELECT status, worker_id FROM jobs WHERE id = $1 FOR UPDATE   <- worker terminalization
    28830  UPDATE jobs SET status = $1 WHERE id = $2
    29060  SELECT status, worker_id FROM jobs WHERE id = $1 FOR UPDATE   <- cancel
    40775  SELECT status, worker_id FROM jobs WHERE id = $1 FOR UPDATE   <- cancel

— so one job-detail page view was enough to stop the job ever being terminalized
(RD-001) and to make every cancel hang (RD-002).

A read has nothing to persist, so the release is a rollback.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from core.infrastructure.db.memory_store import PostgresMemoryRepository


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql, _params):
        return None

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, *, autocommit: bool, rows=()):
        self.autocommit = autocommit
        self.rows = list(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_kwargs):
        return FakeCursor(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def make_repository(conn) -> PostgresMemoryRepository:
    logger_instance = MagicMock()
    logger_instance.conn = conn
    logger_instance.dsn = "dbname=test host=localhost"
    logger_instance.auto_commit = conn.autocommit
    return PostgresMemoryRepository(logger_instance=logger_instance)


class TestNonAutocommitReadsAreReleased:
    def test_fetchone_rolls_back_so_no_transaction_is_left_open(self):
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])
        repo = make_repository(conn)

        assert repo._fetchone("SELECT 1", []) == {"id": "job-1"}
        assert conn.rollbacks == 1, "the read left its transaction open"
        assert conn.commits == 0, "a read must not commit"

    def test_fetchall_rolls_back_so_no_transaction_is_left_open(self):
        conn = FakeConnection(autocommit=False, rows=[{"id": "a"}, {"id": "b"}])
        repo = make_repository(conn)

        assert len(repo._fetchall("SELECT 1", [])) == 2
        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_an_empty_result_still_releases_the_transaction(self):
        conn = FakeConnection(autocommit=False, rows=[])
        repo = make_repository(conn)

        assert repo._fetchone("SELECT 1", []) is None
        assert conn.rollbacks == 1

    def test_a_failing_read_still_releases_the_transaction(self):
        """A raised query must not strand a transaction either."""
        conn = FakeConnection(autocommit=False)

        def _raising_cursor(**_kwargs):
            raise RuntimeError("query blew up")

        conn.cursor = _raising_cursor
        repo = make_repository(conn)

        with pytest.raises(RuntimeError):
            repo._fetchone("SELECT 1", [])
        assert conn.rollbacks == 1

    def test_repeated_reads_each_release(self):
        conn = FakeConnection(autocommit=False, rows=[{"id": "x"}])
        repo = make_repository(conn)

        for _ in range(3):
            repo._fetchone("SELECT 1", [])
        assert conn.rollbacks == 3


class TestAutocommitReadsAreLeftAlone:
    def test_no_rollback_is_issued_on_an_autocommit_connection(self):
        conn = FakeConnection(autocommit=True, rows=[{"id": "job-1"}])
        repo = make_repository(conn)

        repo._fetchone("SELECT 1", [])
        assert conn.rollbacks == 0
        assert conn.commits == 0


class TestCallerOwnedTransactionsAreNotDisturbed:
    def test_a_read_inside_an_open_transaction_does_not_roll_it_back(self):
        """The whole point of an explicit transaction is that reads join it."""
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])
        repo = make_repository(conn)
        repo._transaction_depth = 1

        repo._fetchone("SELECT 1", [])

        assert conn.rollbacks == 0, "a caller-owned transaction was rolled back mid-flight"

    def test_nested_depth_is_also_respected(self):
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])
        repo = make_repository(conn)
        repo._transaction_depth = 3

        repo._fetchall("SELECT 1", [])

        assert conn.rollbacks == 0

    def test_release_resumes_once_the_transaction_closes(self):
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])
        repo = make_repository(conn)

        repo._transaction_depth = 1
        repo._fetchone("SELECT 1", [])
        assert conn.rollbacks == 0

        repo._transaction_depth = 0
        repo._fetchone("SELECT 1", [])
        assert conn.rollbacks == 1


class TestManagedTransactionsAreNeverRolledBack:
    """The dangerous case: a caller-owned transaction must survive a read.

    `transaction_connection()` pins a connection so that several statements
    commit or roll back as one decision. A read that rolled that back would
    silently discard the caller's work — a far worse defect than the leak this
    release exists to prevent.
    """

    def test_a_pinned_managed_transaction_is_left_alone(self, monkeypatch):
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])
        pinned = FakeConnection(autocommit=False)
        repo = make_repository(conn)
        monkeypatch.setattr(
            "core.infrastructure.db.db_pool.active_transaction_connection",
            lambda _db: pinned,
        )

        repo._fetchone("SELECT 1", [])

        assert conn.rollbacks == 0, "rolled back the connection under a managed transaction"
        assert pinned.rollbacks == 0, "rolled back the caller's managed transaction"

    def test_release_resumes_when_no_transaction_is_pinned(self, monkeypatch):
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])
        repo = make_repository(conn)
        monkeypatch.setattr(
            "core.infrastructure.db.db_pool.active_transaction_connection",
            lambda _db: None,
        )

        repo._fetchone("SELECT 1", [])

        assert conn.rollbacks == 1

    def test_a_failing_pool_lookup_errs_toward_leaving_the_connection_alone(self, monkeypatch):
        """If we cannot tell whether a caller owns it, do not touch it."""
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])
        repo = make_repository(conn)

        def _boom(_db):
            raise RuntimeError("pool state unavailable")

        monkeypatch.setattr("core.infrastructure.db.db_pool.active_transaction_connection", _boom)

        repo._fetchone("SELECT 1", [])

        assert conn.rollbacks == 0


class TestReleaseIsDefensive:
    def test_a_rollback_failure_does_not_break_the_read(self):
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1"}])

        def _boom():
            raise RuntimeError("connection already returned to the pool")

        conn.rollback = _boom
        repo = make_repository(conn)

        assert repo._fetchone("SELECT 1", []) == {"id": "job-1"}

    def test_the_job_detail_read_path_goes_through_the_release(self):
        """`load_job_source` is the exact query that stalled the platform."""
        conn = FakeConnection(autocommit=False, rows=[{"id": "job-1", "project_name": "p"}])
        repo = make_repository(conn)

        assert repo.load_job_source(job_id="job-1")["id"] == "job-1"
        assert conn.rollbacks == 1


@contextmanager
def _noop():
    yield


def test_the_release_helper_is_wired_into_both_read_helpers():
    """Guard against someone adding a third read helper that skips it."""
    import inspect

    for name in ("_fetchone", "_fetchall"):
        source = inspect.getsource(getattr(PostgresMemoryRepository, name))
        assert "_release_read_transaction" in source, f"{name} does not release its transaction"
        assert "finally" in source, f"{name} must release even when the query raises"
