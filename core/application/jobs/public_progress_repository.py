"""Atomic PostgreSQL persistence for the public workflow progress projection."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.db_pool import transaction_connection


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        return dict(value.items())
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


class PublicProgressRepository:
    """Owns row locking and strict snapshot/marker atomicity."""

    def __init__(self, db) -> None:
        self.db = db

    def load(self, *, job_id: str) -> dict[str, Any] | None:
        with self.db.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute("SELECT meta FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        if not row:
            return None
        return _object(_object(row).get("meta")).get("public_progress_v1")

    def atomic_update(
        self,
        *,
        job_id: str,
        update: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> tuple[dict[str, Any], str | None, bool]:
        with (
            transaction_connection(self.db) as conn,
            conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur,
        ):
            cur.execute("SELECT meta FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError(f"Job {job_id} does not exist")
            meta = _object(_object(row).get("meta"))
            current = _object(meta.get("public_progress_v1")) or None
            candidate = update(current)
            if candidate is None:
                if current is None:
                    raise RuntimeError("Public progress update returned no initial snapshot")
                return current, None, False

            stored = dict(candidate)
            marker_detail = _object(stored.pop("marker_detail", None))
            meta["public_progress_v1"] = stored
            cur.execute(
                "UPDATE jobs SET meta = %s WHERE id = %s",
                (json.dumps(meta, separators=(",", ":")), job_id),
            )
            cur.execute(
                """
                INSERT INTO platform_events (
                    job_id, phase, agent, event_type, severity, title, detail
                )
                VALUES (%s, 'workflow_run', 'flow_steward', 'workflow_run.state',
                        'info', 'Public workflow state changed', %s)
                RETURNING id
                """,
                (job_id, json.dumps(marker_detail, separators=(",", ":"))),
            )
            marker_row = cur.fetchone()
            marker_id = str(_object(marker_row).get("id") or "").strip()
            if not marker_id:
                raise RuntimeError("Public progress marker insert returned no id")
            stored["marker_id"] = marker_id
            meta["public_progress_v1"] = stored
            cur.execute(
                "UPDATE jobs SET meta = %s WHERE id = %s",
                (json.dumps(meta, separators=(",", ":")), job_id),
            )
            return stored, marker_id, True


__all__ = ["PublicProgressRepository"]
