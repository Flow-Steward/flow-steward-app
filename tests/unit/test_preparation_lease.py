from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from core.application.idempotency.models import IdempotencyScope
from core.application.idempotency.service import RequestIdempotencyService
from core.application.jobs.preparation_lease import (
    PreparationLeaseHeartbeat,
    PreparationLeaseLostError,
)


def test_background_heartbeat_renews_while_storage_call_is_blocked() -> None:
    renewed_twice = threading.Event()
    idempotency_renewed_twice = threading.Event()

    class _Logger:
        renewals = 0

        def renew_job_dispatch_preparation_lease(self, job_id, **kwargs):
            assert job_id == "job-1"
            assert kwargs["owner_token"] == "owner-1"
            self.renewals += 1
            if self.renewals >= 2:
                renewed_twice.set()
            return True

    logger = _Logger()

    idempotency_renewals = 0

    def _renew_idempotency(_connection=None) -> None:
        nonlocal idempotency_renewals
        idempotency_renewals += 1
        if idempotency_renewals >= 2:
            idempotency_renewed_twice.set()

    with PreparationLeaseHeartbeat(
        logger,
        job_id="job-1",
        owner_token="owner-1",
        lease_seconds=30,
        interval_seconds=0.01,
        renew_preparation=lambda _connection=None: logger.renew_job_dispatch_preparation_lease(
            "job-1", owner_token="owner-1", lease_seconds=30
        ),
        additional_heartbeat=_renew_idempotency,
    ):
        assert renewed_twice.wait(1)
        assert idempotency_renewed_twice.wait(1)

    assert logger.renewals >= 3
    assert idempotency_renewals >= 3


def test_heartbeat_uses_one_injected_connection_for_both_lease_renewals() -> None:
    connection = object()
    borrowed: list[object] = []
    preparation_connections: list[object | None] = []
    additional_connections: list[object | None] = []

    @contextmanager
    def connection_provider():
        borrowed.append(connection)
        yield connection

    heartbeat = PreparationLeaseHeartbeat(
        object(),
        job_id="job-1",
        owner_token="owner-1",
        connection_provider=connection_provider,
        renew_preparation=lambda current: preparation_connections.append(current) or True,
        additional_heartbeat=lambda current: additional_connections.append(current),
    )

    heartbeat.heartbeat()

    assert borrowed == [connection]
    assert preparation_connections == [connection]
    assert additional_connections == [connection]


def test_blocked_upload_longer_than_idempotency_ttl_cannot_be_reclaimed() -> None:
    """A duplicate cannot reclaim while upload_part blocks the request thread."""
    now = [datetime(2026, 7, 21, 9, 0, tzinfo=UTC)]
    lock = threading.Lock()
    renewal_count = 0
    renewal_observed = threading.Condition(lock)

    class _Repository:
        row: dict | None = None

        def insert_pending(self, **values):
            with lock:
                if self.row is not None:
                    return None
                self.row = {**values, "status": "pending"}
                return dict(self.row)

        def load_for_update(self, **_identity):
            with lock:
                return dict(self.row) if self.row is not None else None

        def renew(self, **values):
            nonlocal renewal_count
            with renewal_observed:
                if (
                    self.row is None
                    or self.row["request_id"] != values["request_id"]
                    or self.row["lease_expires_at"] <= now[0]
                ):
                    return False
                self.row["lease_expires_at"] = values["lease_expires_at"]
                renewal_count += 1
                renewal_observed.notify_all()
                return True

        def reclaim(self, **_values):
            raise AssertionError("a live multipart submission must not be reclaimed")

    repository = _Repository()
    scope = IdempotencyScope(
        operation="task.launch",
        account_id="account-1",
        dimensions=(("project", "project-1"), ("workflow", "workflow-1")),
    )
    owner_service = RequestIdempotencyService(repository, now=lambda: now[0])
    owner_claim = owner_service.claim(
        scope=scope,
        opaque_key="multipart-1",
        request_fingerprint="sha256:payload",
        request_id="request-owner",
    )

    def renew_idempotency(_connection=None) -> None:
        assert owner_service.renew(claim=owner_claim)

    def wait_for_renewal(target: int) -> None:
        with renewal_observed:
            assert renewal_observed.wait_for(lambda: renewal_count >= target, timeout=1)

    # The context body represents a blocking S3 upload_part call: it never
    # invokes a stream chunk callback while the background heartbeat advances.
    with PreparationLeaseHeartbeat(
        object(),
        job_id="job-1",
        owner_token="preparation-owner",
        lease_seconds=300,
        interval_seconds=0.01,
        renew_preparation=lambda _connection=None: True,
        additional_heartbeat=renew_idempotency,
    ):
        initial_renewals = renewal_count
        now[0] += timedelta(seconds=100)
        wait_for_renewal(initial_renewals + 1)
        now[0] += timedelta(seconds=100)
        wait_for_renewal(initial_renewals + 2)

        duplicate = RequestIdempotencyService(
            repository,
            now=lambda: now[0],
            reclaim_guard=lambda _row: False,
        ).claim(
            scope=scope,
            opaque_key="multipart-1",
            request_fingerprint="sha256:payload",
            request_id="request-duplicate",
        )

        assert duplicate.outcome == "in_progress"
        assert repository.row is not None
        assert repository.row["request_id"] == "request-owner"


def test_final_idempotency_heartbeat_failure_uses_safe_lease_loss_path() -> None:
    calls = 0

    def _renew_idempotency(_connection=None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("idempotency database unavailable")

    with (
        pytest.raises(PreparationLeaseLostError, match="Submission ownership heartbeat"),
        PreparationLeaseHeartbeat(
            object(),
            job_id="job-1",
            owner_token="preparation-owner",
            interval_seconds=60,
            renew_preparation=lambda _connection=None: True,
            additional_heartbeat=_renew_idempotency,
        ),
    ):
        # The upload hook has returned successfully; the second heartbeat
        # runs from __exit__ before final payload publication.
        pass

    assert calls == 2


def test_final_preparation_heartbeat_failure_uses_safe_lease_loss_path() -> None:
    calls = 0

    def _renew_preparation(_connection=None) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("outbox database unavailable")
        return True

    with (
        pytest.raises(PreparationLeaseLostError, match="Submission preparation lease"),
        PreparationLeaseHeartbeat(
            object(),
            job_id="job-1",
            owner_token="preparation-owner",
            interval_seconds=60,
            renew_preparation=_renew_preparation,
        ),
    ):
        pass

    assert calls == 2
