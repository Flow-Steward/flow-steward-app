from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, Lock
from time import monotonic, sleep

from core.application.jobs.workflow_knowledge_sync_requests import (
    WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
    enqueue_workflow_knowledge_sync_batch,
)
from core.infrastructure.runtime.worker.workflow_knowledge_sync import (
    process_workflow_knowledge_sync_batch,
)


class _ChangelogStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def list_recent_registry_changelog(self, *, entity_types, limit):
        self.calls.append({"entity_types": tuple(entity_types), "limit": limit})
        return [
            {
                "id": "change-2",
                "entity_type": "tool",
                "entity_id": "tool-1",
                "created_at": "2026-08-29T00:01:00Z",
                "snapshot": {
                    "account_id": "acct-1",
                    "tenant_id": "tenant-1",
                    "project_id": "project-1",
                },
            },
            {
                "id": "change-1",
                "entity_type": "workflow",
                "entity_id": "workflow-1",
                "created_at": "2026-08-29T00:00:00Z",
                "snapshot": {
                    "account_id": "acct-1",
                    "tenant_id": "tenant-1",
                    "project_id": "project-1",
                },
            },
        ]


class _EventStore:
    def __init__(
        self,
        rows: list[dict] | None = None,
        *,
        finalize_result: dict | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.finalize_result = finalize_result or {"status": "published"}
        self.enqueue_calls: list[dict] = []
        self.claim_calls: list[dict] = []
        self.status_calls: list[tuple[str, dict]] = []
        self.ledger_calls: list[dict] = []
        self.renew_calls: list[dict] = []
        self.conn = self

    @contextmanager
    def transaction(self):
        yield

    @contextmanager
    def claimed_domain_event_effect(self, _outbox_id: str, **_claim):
        yield dict(self.rows[0]) if self.rows and self.finalize_result is not None else None

    def renew_domain_event_outbox_claim(self, _outbox_id: str, **_claim):
        self.renew_calls.append(dict(_claim))
        return dict(self.rows[0]) if self.rows and self.finalize_result is not None else None

    def enqueue_domain_event(self, **kwargs):
        self.enqueue_calls.append(dict(kwargs))

    def claim_pending_domain_event_outbox(self, **kwargs):
        self.claim_calls.append(dict(kwargs))
        return list(self.rows)

    def mark_domain_event_outbox_status(self, outbox_id: str, **kwargs):
        self.status_calls.append((str(outbox_id), dict(kwargs)))
        return self.finalize_result

    def record_domain_event(self, **kwargs):
        self.ledger_calls.append(dict(kwargs))


class _StatusStore:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.attempt_claims: list[dict] = []

    def record_workflow_knowledge_sync_loop_attempt(self, **claim):
        self.calls.append("attempt")
        self.attempt_claims.append(dict(claim))

    def record_workflow_knowledge_sync_loop_success(self):
        self.calls.append("success")

    def record_workflow_knowledge_sync_loop_failure(self, reason: str):
        self.calls.append(f"failure:{reason}")


def test_control_enqueues_one_bounded_immutable_batch_with_stable_idempotency() -> None:
    changelog = _ChangelogStore()
    events = _EventStore()
    now = datetime(2026, 8, 29, 0, 2, tzinfo=UTC)

    first = enqueue_workflow_knowledge_sync_batch(
        changelog_store=changelog,
        event_store=events,
        changelog_limit=10,
        cadence_seconds=120,
        now=lambda: now,
    )
    second = enqueue_workflow_knowledge_sync_batch(
        changelog_store=changelog,
        event_store=events,
        changelog_limit=10,
        cadence_seconds=120,
        now=lambda: now,
    )

    assert first == second
    assert first["status"] == "queued"
    assert first["scope_count"] == 1
    assert len(events.enqueue_calls) == 2
    assert events.enqueue_calls[0] == events.enqueue_calls[1]
    call = events.enqueue_calls[0]
    assert call["event_type"] == WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE
    assert call["event_id"] == first["idempotency_key"]
    assert call["correlation_id"] == first["idempotency_key"]
    assert call["payload"] == {
        "cadence_bucket": 14899681,
        "changelog_limit": 10,
        "idempotency_key": first["idempotency_key"],
        "scopes": [
            {
                "account_id": "acct-1",
                "ensure_indexes": True,
                "include_capability_catalog": True,
                "project_id": "project-1",
                "source_revision": call["payload"]["scopes"][0]["source_revision"],
                "tenant_id": "tenant-1",
            }
        ],
    }
    assert len(call["payload"]["scopes"][0]["source_revision"]) == 64
    assert "snapshot" not in repr(call["payload"])


def test_worker_executes_canonical_sync_and_persists_result() -> None:
    request_payload = {
        "cadence_bucket": 1,
        "changelog_limit": 10,
        "idempotency_key": "workflow-knowledge-sync:request-1",
        "scopes": [
            {
                "account_id": "acct-1",
                "tenant_id": "tenant-1",
                "project_id": "project-1",
                "include_capability_catalog": True,
                "ensure_indexes": True,
            }
        ],
    }
    events = _EventStore(
        [
            {
                "id": "outbox-1",
                "account_id": "acct-1",
                "event_id": "workflow-knowledge-sync:request-1",
                "event_type": WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
                "aggregate_id": "workflow-knowledge-catalog",
                "payload": request_payload,
                "envelope": {"correlation_id": "workflow-knowledge-sync:request-1"},
                "claim_owner": "worker-1",
                "claim_token": "claim-1",
                "claim_version": 1,
            }
        ]
    )
    status = _StatusStore()
    calls: list[dict] = []

    processed = process_workflow_knowledge_sync_batch(
        event_store=events,
        status_store=status,
        db=object(),
        knowledge_store=object(),
        run_sync=lambda payload, **_kwargs: (
            calls.append(dict(payload)) or {"skipped": False, "digest": "digest-1"}
        ),
        worker_id="worker-1",
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert processed == 1
    assert events.claim_calls == [
        {
            "limit": 10,
            "lease_seconds": 30,
            "event_types": (WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,),
            "claim_owner": "worker-1",
        }
    ]
    assert calls == request_payload["scopes"]
    assert status.calls == ["attempt", "success"]
    assert status.attempt_claims == [
        {
            "outbox_id": "outbox-1",
            "claim_owner": "worker-1",
            "claim_version": 1,
        }
    ]
    assert events.status_calls == [
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
    assert events.ledger_calls[0]["status"] == "succeeded"
    assert events.ledger_calls[0]["account_id"] == "acct-1"
    assert events.ledger_calls[0]["payload"]["result"] == {
        "checked_scopes": 1,
        "skipped": 0,
        "synced": 1,
    }


def test_worker_does_not_hold_event_store_transaction_during_long_sync() -> None:
    class _TransactionTrackingStore(_EventStore):
        def __init__(self) -> None:
            super().__init__(
                [
                    {
                        "id": "outbox-long-sync",
                        "account_id": "acct-1",
                        "event_id": "workflow-knowledge-sync:long",
                        "event_type": WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
                        "payload": {"scopes": [{"account_id": "acct-1"}]},
                        "envelope": {},
                        "claim_owner": "worker-1",
                        "claim_token": "claim-long",
                        "claim_version": 1,
                    }
                ]
            )
            self.in_transaction = False

        @contextmanager
        def transaction(self):
            assert not self.in_transaction
            self.in_transaction = True
            try:
                yield
            finally:
                self.in_transaction = False

        @contextmanager
        def claimed_domain_event_effect(self, _outbox_id: str, **_claim):
            with self.transaction():
                yield dict(self.rows[0])

    events = _TransactionTrackingStore()

    def _long_sync(*_args, **_kwargs):
        assert events.in_transaction is False, "run_sync is inside an idle event transaction"
        return {"skipped": False}

    processed = process_workflow_knowledge_sync_batch(
        event_store=events,
        status_store=_StatusStore(),
        db=object(),
        knowledge_store=object(),
        run_sync=_long_sync,
        worker_id="worker-1",
    )

    assert processed == 1
    assert events.status_calls[0][1]["status"] == "published"


def test_worker_releases_failed_sync_for_existing_outbox_retry() -> None:
    events = _EventStore(
        [
            {
                "id": "outbox-2",
                "account_id": "acct-1",
                "event_id": "workflow-knowledge-sync:request-2",
                "event_type": WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
                "aggregate_id": "workflow-knowledge-catalog",
                "payload": {
                    "idempotency_key": "workflow-knowledge-sync:request-2",
                    "scopes": [{"account_id": "acct-1"}],
                },
                "envelope": {"correlation_id": "workflow-knowledge-sync:request-2"},
                "claim_owner": "worker-1",
                "claim_token": "claim-2",
                "claim_version": 2,
            }
        ]
    )
    status = _StatusStore()

    def _fail(*_args, **_kwargs):
        raise RuntimeError("knowledge backend unavailable")

    processed = process_workflow_knowledge_sync_batch(
        event_store=events,
        status_store=status,
        db=object(),
        knowledge_store=object(),
        run_sync=_fail,
        worker_id="worker-1",
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert processed == 0
    assert status.calls == ["attempt", "failure:knowledge backend unavailable"]
    assert events.status_calls == [
        (
            "outbox-2",
            {
                "status": "pending",
                "last_error": "knowledge backend unavailable",
                "available_at": "2026-08-29T00:00:30+00:00",
                "claim_owner": "worker-1",
                "claim_token": "claim-2",
                "claim_version": 2,
            },
        )
    ]
    assert events.ledger_calls[0]["status"] == "failed"
    assert events.ledger_calls[0]["account_id"] == "acct-1"


def test_worker_stale_claim_cannot_finalize_or_record_success() -> None:
    events = _EventStore(
        [
            {
                "id": "outbox-stale",
                "account_id": "acct-1",
                "event_id": "workflow-knowledge-sync:stale",
                "event_type": WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
                "payload": {"scopes": [{"account_id": "acct-1"}]},
                "envelope": {},
                "claim_owner": "worker-a",
                "claim_token": "claim-a",
                "claim_version": 1,
            }
        ]
    )
    events.finalize_result = None
    status = _StatusStore()

    processed = process_workflow_knowledge_sync_batch(
        event_store=events,
        status_store=status,
        db=object(),
        knowledge_store=object(),
        run_sync=lambda *_args, **_kwargs: {"skipped": False},
        worker_id="worker-a",
    )

    assert processed == 0
    assert status.calls == []
    assert events.ledger_calls == []


def test_worker_unavailable_effect_claim_is_non_fatal_and_skips_sync() -> None:
    events = _EventStore(
        [
            {
                "id": "outbox-retry",
                "event_type": WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
                "payload": {"scopes": [{"account_id": "acct-1"}]},
                "claim_owner": "worker-a",
                "claim_token": "claim-a",
                "claim_version": 1,
            }
        ],
    )
    events.finalize_result = None
    status = _StatusStore()
    calls: list[str] = []

    processed = process_workflow_knowledge_sync_batch(
        event_store=events,
        status_store=status,
        db=object(),
        knowledge_store=object(),
        run_sync=lambda *_args, **_kwargs: calls.append("called") or {},
        worker_id="worker-a",
    )

    assert processed == 0
    assert calls == []
    assert status.calls == []
    assert events.status_calls == []
    assert events.ledger_calls == []


def test_competing_reclaim_cannot_enter_while_authoritative_lease_is_live() -> None:
    request = {
        "id": "outbox-race",
        "account_id": "acct-1",
        "event_id": "workflow-knowledge-sync:race",
        "event_type": WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
        "payload": {"scopes": [{"account_id": "acct-1"}]},
        "envelope": {},
    }

    class _CompetingStore:
        def __init__(self) -> None:
            self.owner = ""
            self.token = ""
            self.version = 0
            self.status = "pending"
            self.next_owner = "worker-a"
            self.ledger_calls: list[dict] = []
            self.conn = self
            self.lease_active = False

        @contextmanager
        def transaction(self):
            yield

        def renew_domain_event_outbox_claim(self, _outbox_id: str, **claim):
            if (
                claim["claim_owner"] != self.owner
                or claim["claim_token"] != self.token
                or claim["claim_version"] != self.version
            ):
                return None
            self.lease_active = True
            return self._row()

        def _row(self) -> dict:
            return {
                **request,
                "claim_owner": self.owner,
                "claim_token": self.token,
                "claim_version": self.version,
            }

        def claim_pending_domain_event_outbox(self, *, claim_owner: str, **_kwargs):
            if self.status != "pending" or claim_owner != self.next_owner:
                return []
            self.owner = claim_owner
            self.version += 1
            self.token = f"claim-{self.version}"
            self.next_owner = ""
            return [self._row()]

        def expire_and_reclaim_for(self, worker_id: str) -> None:
            if self.lease_active:
                return
            self.owner = ""
            self.token = ""
            self.next_owner = str(worker_id)

        def mark_domain_event_outbox_status(self, _outbox_id: str, **claim):
            if (
                claim["claim_owner"] != self.owner
                or claim["claim_token"] != self.token
                or claim["claim_version"] != self.version
            ):
                return None
            self.status = str(claim["status"])
            self.lease_active = False
            return self._row()

        def record_domain_event(self, **kwargs):
            self.ledger_calls.append(dict(kwargs))

    store = _CompetingStore()
    externally_observable_effects: list[str] = []

    def _stale_sync(*_args, **_kwargs):
        externally_observable_effects.append("workflow-knowledge-sync:race")
        store.expire_and_reclaim_for("worker-b")
        return {"skipped": False}

    assert (
        process_workflow_knowledge_sync_batch(
            event_store=store,
            status_store=_StatusStore(),
            db=object(),
            knowledge_store=object(),
            run_sync=_stale_sync,
            worker_id="worker-a",
        )
        == 1
    )
    assert len(store.ledger_calls) == 1

    assert (
        process_workflow_knowledge_sync_batch(
            event_store=store,
            status_store=_StatusStore(),
            db=object(),
            knowledge_store=object(),
            run_sync=lambda *_args, **_kwargs: (
                externally_observable_effects.append("workflow-knowledge-sync:race")
                or {"skipped": False}
            ),
            worker_id="worker-b",
        )
        == 0
    )
    assert store.status == "published"
    assert store.version == 1
    assert len(store.ledger_calls) == 1
    assert externally_observable_effects == ["workflow-knowledge-sync:race"]


def test_worker_heartbeats_claim_past_its_initial_expiration() -> None:
    request = {
        "id": "outbox-heartbeat",
        "account_id": "acct-1",
        "event_id": "workflow-knowledge-sync:heartbeat",
        "event_type": WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
        "payload": {"scopes": [{"account_id": "acct-1"}]},
        "envelope": {},
        "claim_owner": "worker-a",
        "claim_token": "claim-1",
        "claim_version": 1,
    }

    class _ExpiringLeaseStore(_EventStore):
        def __init__(self) -> None:
            super().__init__([request])
            self._lock = Lock()
            self.expires_at = monotonic() + 0.01
            self.first_expiration = self.expires_at
            self.reclaimed = False
            self.heartbeat_seen = Event()

        def renew_domain_event_outbox_claim(
            self, _outbox_id: str, *, lease_seconds: float, **claim
        ):
            with self._lock:
                if self.reclaimed or monotonic() >= self.expires_at:
                    return None
                self.renew_calls.append({"lease_seconds": lease_seconds, **claim})
                self.expires_at = monotonic() + lease_seconds
                if len(self.renew_calls) >= 2:
                    self.heartbeat_seen.set()
                return dict(request)

        def try_competing_reclaim(self) -> None:
            with self._lock:
                if monotonic() >= self.expires_at:
                    self.reclaimed = True

    store = _ExpiringLeaseStore()

    def _long_sync(*_args, **_kwargs):
        assert store.heartbeat_seen.wait(timeout=1)
        sleep(max(0.0, store.first_expiration - monotonic()) + 0.01)
        store.try_competing_reclaim()
        assert store.reclaimed is False
        return {"skipped": False}

    processed = process_workflow_knowledge_sync_batch(
        event_store=store,
        heartbeat_store=store,
        status_store=_StatusStore(),
        db=object(),
        knowledge_store=object(),
        run_sync=_long_sync,
        worker_id="worker-a",
        effect_lease_seconds=0.03,
        heartbeat_interval_seconds=0.005,
    )

    assert processed == 1
    assert len(store.renew_calls) >= 2
    assert store.reclaimed is False
