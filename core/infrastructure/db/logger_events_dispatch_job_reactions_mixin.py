from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db.logger_events_dispatch_common import (
    ERR_EVENT_TYPE_REQUIRED,
    _as_dict,
    _coerce_bool,
    psycopg_runtime,
)


class PlatformLoggerEventsJobReactionsMixin:
    conn: Any
    _table_exists: Any

    @staticmethod
    def _normalize_job_reaction_event_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["payload"] = _as_dict(payload.get("payload") or payload.get("payload_jsonb"))
        payload["processed"] = _coerce_bool(payload.get("processed"), False)
        payload.pop("payload_jsonb", None)
        return payload

    def list_job_reaction_events(self, job_id: Any, limit: int = 200) -> list[dict]:
        if not self._table_exists("job_reaction_events"):
            return []
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    job_id,
                    workflow_id,
                    event_type,
                    source,
                    source_id,
                    event_key,
                    payload_jsonb,
                    dedupe_key,
                    processed,
                    created_at
                FROM job_reaction_events
                WHERE job_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (job_id, max(1, int(limit or 1))),
            )
            rows = cur.fetchall()
        return [self._normalize_job_reaction_event_row(dict(row)) for row in rows]

    def log_job_reaction_event(
        self,
        *,
        job_id: Any | None = None,
        workflow_id: str | None = None,
        event_type: str,
        source: str = "external",
        source_id: str | None = None,
        event_key: str | None = None,
        payload: dict | None = None,
        dedupe_key: str | None = None,
        processed: bool = False,
    ) -> dict | None:
        if not self._table_exists("job_reaction_events"):
            return None
        normalized_event_type = str(event_type or "").strip()
        if not normalized_event_type:
            raise ValueError(ERR_EVENT_TYPE_REQUIRED)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            normalized_workflow_id = str(workflow_id or "").strip() or None
            normalized_source = str(source or "external").strip() or "external"
            normalized_source_id = str(source_id or "").strip() or None
            normalized_event_key = str(event_key or "").strip() or None
            normalized_dedupe_key = str(dedupe_key or "").strip() or None
            serialized_payload = json.dumps(_as_dict(payload))

            row = None
            if normalized_dedupe_key:
                cur.execute(
                    """
                    SELECT
                        id,
                        job_id,
                        workflow_id,
                        event_type,
                        source,
                        source_id,
                        event_key,
                        payload_jsonb,
                        dedupe_key,
                        processed,
                        created_at
                    FROM job_reaction_events
                    WHERE dedupe_key = %s
                    LIMIT 1
                    """,
                    (normalized_dedupe_key,),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE job_reaction_events
                        SET
                            job_id = %s,
                            workflow_id = %s,
                            event_type = %s,
                            source = %s,
                            source_id = %s,
                            event_key = %s,
                            payload_jsonb = %s,
                            processed = (job_reaction_events.processed OR %s)
                        WHERE id::text = %s
                        RETURNING
                            id,
                            job_id,
                            workflow_id,
                            event_type,
                            source,
                            source_id,
                            event_key,
                            payload_jsonb,
                            dedupe_key,
                            processed,
                            created_at
                        """,
                        (
                            job_id,
                            normalized_workflow_id,
                            normalized_event_type,
                            normalized_source,
                            normalized_source_id,
                            normalized_event_key,
                            serialized_payload,
                            bool(processed),
                            str(row["id"]),
                        ),
                    )
                    row = cur.fetchone()

            if not row:
                cur.execute(
                    """
                    INSERT INTO job_reaction_events (
                        job_id,
                        workflow_id,
                        event_type,
                        source,
                        source_id,
                        event_key,
                        payload_jsonb,
                        dedupe_key,
                        processed
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING
                        id,
                        job_id,
                        workflow_id,
                        event_type,
                        source,
                        source_id,
                        event_key,
                        payload_jsonb,
                        dedupe_key,
                        processed,
                        created_at
                    """,
                    (
                        job_id,
                        normalized_workflow_id,
                        normalized_event_type,
                        normalized_source,
                        normalized_source_id,
                        normalized_event_key,
                        serialized_payload,
                        normalized_dedupe_key,
                        bool(processed),
                    ),
                )
                row = cur.fetchone()
        return self._normalize_job_reaction_event_row(dict(row)) if row else None

    def mark_job_reaction_event_processed(
        self, event_id: str, processed: bool = True
    ) -> dict | None:
        if not self._table_exists("job_reaction_events"):
            return None
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE job_reaction_events
                SET processed = %s
                WHERE id::text = %s
                RETURNING
                    id,
                    job_id,
                    workflow_id,
                    event_type,
                    source,
                    source_id,
                    event_key,
                    payload_jsonb,
                    dedupe_key,
                    processed,
                    created_at
                """,
                (bool(processed), str(event_id)),
            )
            row = cur.fetchone()
        return self._normalize_job_reaction_event_row(dict(row)) if row else None


__all__ = ["PlatformLoggerEventsJobReactionsMixin"]
