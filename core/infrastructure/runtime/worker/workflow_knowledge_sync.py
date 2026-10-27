"""Worker-owned execution of durable workflow-knowledge sync requests."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.jobs.workflow_knowledge_sync_requests import (
    WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
)
from core.application.jobs.workflow_knowledge_sync_runtime import (
    run_workflow_knowledge_sync_job,
)
from core.infrastructure.db.db_pool import transaction_connection

logger = logging.getLogger("platform.worker.workflow_knowledge_sync")


@contextmanager
def _maintain_claim_lease(
    *,
    heartbeat_store: Any,
    outbox_id: str,
    claim: Mapping[str, Any],
    lease_seconds: float,
    interval_seconds: float,
):
    """Keep one fenced outbox claim live while its effect runs."""

    lost_claim = threading.Event()
    stop = threading.Event()
    try:
        renewed = heartbeat_store.renew_domain_event_outbox_claim(
            outbox_id,
            lease_seconds=lease_seconds,
            **claim,
        )
    except Exception as exc:
        logger.warning("Workflow knowledge synchronization lease renewal failed: %s", exc)
        renewed = None
    if not renewed:
        lost_claim.set()
        yield lost_claim
        return

    def _heartbeat() -> None:
        while not stop.wait(max(0.001, float(interval_seconds))):
            try:
                live = heartbeat_store.renew_domain_event_outbox_claim(
                    outbox_id,
                    lease_seconds=lease_seconds,
                    **claim,
                )
            except Exception as exc:
                logger.warning("Workflow knowledge synchronization lease heartbeat failed: %s", exc)
                lost_claim.set()
                return
            if not live:
                lost_claim.set()
                return

    thread = threading.Thread(
        target=_heartbeat,
        name=f"workflow-knowledge-sync-lease-{outbox_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield lost_claim
    finally:
        stop.set()
        thread.join()


def _record_result(
    event_store: Any,
    row: Mapping[str, Any],
    *,
    status: str,
    result: Mapping[str, Any] | None = None,
    error: str = "",
) -> None:
    event_store.record_domain_event(
        event_id=str(row.get("event_id") or "").strip(),
        event_type=WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,
        account_id=str(row.get("account_id") or "").strip(),
        aggregate_type="workflow_knowledge_catalog",
        aggregate_id="workflow-knowledge-catalog",
        payload={
            "request": dict(row.get("payload") or {}),
            "result": dict(result or {}),
            "error": str(error or "").strip() or None,
        },
        envelope=dict(row.get("envelope") or {}),
        status=status,
        summary=f"Workflow knowledge synchronization {status}",
    )


def process_workflow_knowledge_sync_batch(
    *,
    event_store: Any,
    status_store: Any,
    db: Any,
    knowledge_store: Any,
    worker_id: str,
    run_sync: Callable[..., dict[str, Any]] = run_workflow_knowledge_sync_job,
    now: Callable[[], datetime] | None = None,
    heartbeat_store: Any | None = None,
    effect_lease_seconds: float = 300,
    heartbeat_interval_seconds: float | None = None,
) -> int:
    """Claim and execute requests through the existing domain-event outbox lease."""

    clock = now or (lambda: datetime.now(UTC))
    normalized_worker_id = str(worker_id or "").strip()
    if not normalized_worker_id:
        return 0
    lease_store = heartbeat_store or event_store
    heartbeat_interval = heartbeat_interval_seconds
    if heartbeat_interval is None:
        heartbeat_interval = max(1.0, float(effect_lease_seconds) / 3.0)
    try:
        rows = event_store.claim_pending_domain_event_outbox(
            claim_owner=normalized_worker_id,
            limit=10,
            lease_seconds=30,
            event_types=(WORKFLOW_KNOWLEDGE_SYNC_REQUESTED_EVENT_TYPE,),
        )
    except Exception as exc:
        logger.warning("Workflow knowledge synchronization claim failed: %s", exc)
        return 0
    processed = 0
    for raw_row in rows:
        row = dict(raw_row or {})
        outbox_id = str(row.get("id") or "").strip()
        if not outbox_id:
            continue
        claim = {
            "claim_owner": normalized_worker_id,
            "claim_token": str(row.get("claim_token") or "").strip(),
            "claim_version": int(row.get("claim_version") or 0),
        }
        try:
            summary: dict[str, Any] | None = None
            error = ""
            with _maintain_claim_lease(
                heartbeat_store=lease_store,
                outbox_id=outbox_id,
                claim=claim,
                lease_seconds=effect_lease_seconds,
                interval_seconds=heartbeat_interval,
            ) as lost_claim:
                if lost_claim.is_set():
                    continue
                status_store.record_workflow_knowledge_sync_loop_attempt(
                    outbox_id=outbox_id,
                    claim_owner=normalized_worker_id,
                    claim_version=claim["claim_version"],
                )
                try:
                    request = dict(row.get("payload") or {})
                    scopes = [dict(scope) for scope in request.get("scopes") or []]
                    results = [run_sync(scope, db=db, store=knowledge_store) for scope in scopes]
                    summary = {
                        "checked_scopes": len(scopes),
                        "skipped": sum(1 for result in results if bool(result.get("skipped"))),
                        "synced": sum(1 for result in results if not bool(result.get("skipped"))),
                    }
                except Exception as exc:
                    error = str(exc).strip() or type(exc).__name__
            if lost_claim.is_set():
                continue
            with transaction_connection(event_store.conn):
                if error:
                    finalized = event_store.mark_domain_event_outbox_status(
                        outbox_id,
                        status="pending",
                        last_error=error[:2000],
                        available_at=(clock() + timedelta(seconds=30)).isoformat(),
                        **claim,
                    )
                    if finalized:
                        status_store.record_workflow_knowledge_sync_loop_failure(error[:512])
                        _record_result(event_store, row, status="failed", error=error)
                else:
                    finalized = event_store.mark_domain_event_outbox_status(
                        outbox_id,
                        status="published",
                        **claim,
                    )
                    if finalized:
                        status_store.record_workflow_knowledge_sync_loop_success()
                        _record_result(event_store, row, status="succeeded", result=summary)
        except Exception as exc:
            logger.warning("Workflow knowledge synchronization transaction failed: %s", exc)
            continue
        if not finalized:
            continue
        if error:
            logger.warning("Workflow knowledge synchronization failed: %s", error)
            continue
        processed += 1
    return processed


def start_workflow_knowledge_sync_loop(
    *, worker_id: str, interval_seconds: float = 5.0
) -> tuple[threading.Event, threading.Thread] | None:
    """Start the worker-owned leased consumer when the feature is enabled."""

    from core.infrastructure.config.runtime_env import runtime_env

    enabled = str(runtime_env.get("FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED", "1")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    from core.application.registry.db_store import RegistryDBStore
    from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
    from core.infrastructure.db.platform_stores import PlatformWorkerStore
    from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore

    dsn = resolve_observability_dsn()
    if not dsn:
        logger.info("Workflow knowledge sync loop skipped: no database DSN configured")
        return None
    event_store = PlatformWorkerStore.get_shared_instance(dsn)
    heartbeat_store = PlatformWorkerStore(dsn)
    status_store = event_store
    db = RegistryDBStore(dsn)
    knowledge_store = PostgresAgeWorkflowKnowledgeStore(dsn=dsn, connection=event_store.conn)
    stop_event = threading.Event()

    def _loop() -> None:
        try:
            while not stop_event.is_set():
                process_workflow_knowledge_sync_batch(
                    event_store=event_store,
                    heartbeat_store=heartbeat_store,
                    status_store=status_store,
                    db=db,
                    knowledge_store=knowledge_store,
                    worker_id=worker_id,
                )
                stop_event.wait(max(1.0, float(interval_seconds)))
        finally:
            heartbeat_store.conn.close()

    thread = threading.Thread(target=_loop, name="workflow-knowledge-sync", daemon=True)
    thread.start()
    return stop_event, thread


__all__ = ["process_workflow_knowledge_sync_batch", "start_workflow_knowledge_sync_loop"]
