"""Worker-owned leased consumption of terminal memory-curation events."""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.db.platform_logger_jobs_mixin import (
    TERMINAL_MEMORY_CURATION_EVENT_TYPE,
)

logger = logging.getLogger("platform.worker.terminal_curation")

#: A foreign-key violation that names the job column or its constraint. The
#: message is the only place the database says which key failed.
_JOB_KEY_VIOLATION = re.compile(r"job_id", re.IGNORECASE)


def _record_result(event_store: Any, row: dict[str, Any], *, status: str) -> None:
    job_id = str(row.get("job_id") or row.get("aggregate_id") or "").strip()
    event_store.record_domain_event(
        event_id=str(row.get("event_id") or "").strip(),
        event_type=TERMINAL_MEMORY_CURATION_EVENT_TYPE,
        account_id=str(row.get("account_id") or "").strip(),
        job_id=job_id,
        aggregate_type="job",
        aggregate_id=job_id,
        payload=dict(row.get("payload") or {}),
        envelope=dict(row.get("envelope") or {}),
        status=status,
        summary=f"Terminal job memory curation {status}",
    )


def _job_still_exists(event_store: Any, job_id: str) -> bool:
    """Whether the job this event was raised for is still there.

    A project or job deleted between the terminal event and its curation leaves
    a `domain_event_ledger.job_id` foreign key with nothing to point at. The
    curation transaction then fails, the row goes back to pending, and the worker
    retries it forever — the bookkeeping that records the failure hits the same
    key. Nothing about a deleted job can be curated, so it is not an error to
    report; it is work that no longer exists.
    """
    normalized = str(job_id or "").strip()
    if not normalized:
        return False
    try:
        with event_store.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE id = %s", (normalized,))
            return cur.fetchone() is not None
    except Exception as exc:
        # An unanswerable question is not a licence to drop the work.
        logger.warning("Terminal memory curation could not check job_id=%s: %s", normalized, exc)
        return True


def _names_the_job_key(exc: BaseException) -> bool:
    """Whether this violation is about the job column rather than some other key.

    psycopg carries the constraint and column in `diag`, which is exact; the
    message is the fallback for drivers and wrappers that do not.
    """
    diag = getattr(exc, "diag", None)
    named = " ".join(
        str(getattr(diag, attribute, "") or "") for attribute in ("column_name", "constraint_name")
    ).strip()
    return bool(_JOB_KEY_VIOLATION.search(named or str(exc)))


def _is_vanished_job_error(exc: BaseException | None) -> bool:
    """Whether a failure is the database refusing to point at a deleted job.

    Checking that the job exists first narrows the race; it cannot close it,
    because the job can be deleted at any point afterwards. When that happens
    the database raises a foreign-key violation, and treating it as an ordinary
    failure puts the event back to pending for a worker that will hit the same
    missing key on every pass: the retry can never succeed and never stops.
    Recognising the shape makes the outcome the same whenever the delete lands.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        is_foreign_key = (
            str(getattr(exc, "sqlstate", "") or "") == "23503"
            or type(exc).__name__ == "ForeignKeyViolation"
        )
        # Narrowed to the job key. Any foreign-key violation used to read as
        # "the job vanished", so a violation on some other relation closed the
        # event and dropped real work with no retry and no trace of why.
        if is_foreign_key and _names_the_job_key(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _close_vanished_job_event(event_store: Any, outbox_id: str, job_id: str, claim: dict) -> None:
    """Close the event out and write no ledger row: it needs the same job_id."""
    with contextlib.suppress(Exception):
        event_store.mark_domain_event_outbox_status(outbox_id, status="published", **claim)
    logger.info("Terminal memory curation skipped for deleted job_id=%s", job_id)


def process_terminal_curation_batch(
    *,
    event_store: Any,
    memory_store: Any,
    curate_terminal_job: Callable[..., list[str]],
    worker_id: str,
    now: Callable[[], datetime] | None = None,
) -> int:
    clock = now or (lambda: datetime.now(UTC))
    normalized_worker_id = str(worker_id or "").strip()
    if not normalized_worker_id:
        return 0
    try:
        rows = event_store.claim_pending_domain_event_outbox(
            claim_owner=normalized_worker_id,
            limit=25,
            lease_seconds=30,
            event_types=(TERMINAL_MEMORY_CURATION_EVENT_TYPE,),
        )
    except Exception as exc:
        logger.warning("Terminal memory curation claim failed: %s", exc)
        return 0
    processed = 0
    for row in rows:
        outbox_id = str(row.get("id") or "").strip()
        job_id = str(row.get("job_id") or row.get("aggregate_id") or "").strip()
        if not outbox_id or not job_id:
            continue
        claim = {
            "claim_owner": normalized_worker_id,
            "claim_token": str(row.get("claim_token") or "").strip(),
            "claim_version": int(row.get("claim_version") or 0),
        }
        if not _job_still_exists(event_store, job_id):
            _close_vanished_job_event(event_store, outbox_id, job_id, claim)
            continue
        try:
            with event_store.claimed_domain_event_effect(
                outbox_id,
                lease_seconds=300,
                **claim,
            ) as locked_claim:
                if not locked_claim:
                    continue
                error = ""
                vanished = False
                try:
                    with transaction_connection(event_store.conn):
                        curate_terminal_job(job_id=job_id, logger_instance=memory_store)
                except Exception as exc:
                    if _is_vanished_job_error(exc):
                        vanished = True
                    else:
                        error = str(exc).strip() or type(exc).__name__
                if vanished:
                    _close_vanished_job_event(event_store, outbox_id, job_id, claim)
                    continue
                if error:
                    finalized = event_store.mark_domain_event_outbox_status(
                        outbox_id,
                        status="pending",
                        last_error=error[:2000],
                        available_at=(clock() + timedelta(seconds=30)).isoformat(),
                        **claim,
                    )
                    if finalized:
                        with contextlib.suppress(Exception):
                            _record_result(event_store, row, status="failed")
                else:
                    finalized = event_store.mark_domain_event_outbox_status(
                        outbox_id,
                        status="published",
                        **claim,
                    )
                    if finalized:
                        try:
                            _record_result(event_store, row, status="succeeded")
                        except Exception as exc:
                            if not _is_vanished_job_error(exc):
                                raise
                            logger.info(
                                "Terminal memory curation ledger skipped for deleted job_id=%s",
                                job_id,
                            )
        except Exception as exc:
            logger.warning(
                "Terminal memory curation transaction failed for job_id=%s: %s", job_id, exc
            )
            continue
        if not finalized:
            continue
        if error:
            logger.warning("Terminal memory curation failed for job_id=%s: %s", job_id, error)
            continue
        processed += 1
    return processed


def start_terminal_curation_loop(
    *,
    curate_terminal_job: Callable[..., list[str]] | None,
    worker_id: str,
    interval_seconds: float = 5.0,
) -> tuple[threading.Event, threading.Thread] | None:
    if not callable(curate_terminal_job):
        return None

    from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
    from core.infrastructure.db.platform_stores import PlatformWorkerStore

    dsn = resolve_observability_dsn()
    if not dsn:
        logger.info("Terminal memory curation loop skipped: no database DSN configured")
        return None
    event_store = PlatformWorkerStore.get_shared_instance(dsn)
    memory_store = event_store
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.is_set():
            process_terminal_curation_batch(
                event_store=event_store,
                memory_store=memory_store,
                curate_terminal_job=curate_terminal_job,
                worker_id=worker_id,
            )
            stop_event.wait(max(1.0, float(interval_seconds)))

    thread = threading.Thread(target=_loop, name="terminal-memory-curation", daemon=True)
    thread.start()
    return stop_event, thread


__all__ = [
    "TERMINAL_MEMORY_CURATION_EVENT_TYPE",
    "process_terminal_curation_batch",
    "start_terminal_curation_loop",
]
