"""Fence checkpoint pause projections against cancellation and worker takeover."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core.application.jobs.terminal_persistence import TerminalPersistenceConflict
from core.infrastructure.db.db_pool import transaction_connection


@contextmanager
def job_pause_transaction(store: Any, *, job_id: str, expected_worker_id: str) -> Iterator[None]:
    """Keep pause status, metadata and progress under one active-owner row lock."""
    with transaction_connection(store.conn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, worker_id FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
            row = cur.fetchone()
        if isinstance(row, dict):
            status = row.get("status")
            owner = row.get("worker_id")
        elif row:
            status, owner = row[0], row[1]
        else:
            status, owner = None, None
        if status != "in_progress" or str(owner or "") != str(expected_worker_id or ""):
            raise TerminalPersistenceConflict(f"Job {job_id} pause ownership changed")
        yield
