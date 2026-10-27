from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.db.logger_events_dispatch_common import (
    SQL_LAST_ERROR_EQ,
    SQL_STATUS_EQ,
    _as_dict,
    _default_account_id,
    build_event_envelope_v1,
    psycopg_runtime,
)


class PlatformLoggerEventsOutboxMixin:
    conn: Any
    _column_exists: Any
    _table_exists: Any

    def enqueue_domain_event(
        self,
        *,
        event_type: str,
        event_id: str | None = None,
        aggregate_type: str = "job",
        aggregate_id: str = "",
        payload: dict | None = None,
        status: str = "pending",
        account_id: str | None = None,
        project_id: Any | None = None,
        workflow_id: str | None = None,
        job_id: Any | None = None,
        severity: str | None = None,
        summary: str | None = None,
        actor: dict | None = None,
        envelope: dict | None = None,
        correlation_id: str | None = None,
    ) -> None:
        if not self._table_exists("domain_event_outbox"):
            return
        normalized_envelope = _as_dict(envelope)
        normalized_payload = _as_dict(payload)
        normalized_event_id = (
            str(event_id or normalized_envelope.get("event_id") or "").strip() or None
        )
        if not normalized_envelope:
            normalized_envelope = {
                "event_id": normalized_event_id,
                "event_type": str(event_type or "").strip(),
                "correlation_id": str(correlation_id or normalized_event_id or "").strip(),
                "aggregate_type": str(aggregate_type or "job").strip() or "job",
                "aggregate_id": str(aggregate_id or "").strip(),
                "account_id": str(account_id or "").strip() or _default_account_id(),
                "project_id": str(project_id) if project_id is not None else None,
                "workflow_id": str(workflow_id or "").strip() or None,
                "job_id": str(job_id) if job_id is not None else None,
                "severity": str(severity or "info").strip().lower() or "info",
                "summary": str(summary or "").strip(),
                "payload": normalized_payload,
                "actor": _as_dict(actor),
            }
        elif normalized_event_id and not str(normalized_envelope.get("event_id") or "").strip():
            normalized_envelope["event_id"] = normalized_event_id
        normalized_envelope = build_event_envelope_v1(
            event_type=str(event_type or normalized_envelope.get("event_type") or "").strip(),
            account_id=str(account_id or normalized_envelope.get("account_id") or "").strip()
            or _default_account_id(),
            correlation_id=(
                str(correlation_id or normalized_envelope.get("correlation_id") or "").strip()
                or str(normalized_event_id or normalized_envelope.get("event_id") or "").strip()
            ),
            severity=str(severity or normalized_envelope.get("severity") or "info").strip().lower()
            or "info",
            summary=str(summary or normalized_envelope.get("summary") or event_type or "").strip()
            or str(event_type or normalized_envelope.get("event_type") or "").strip(),
            payload=normalized_payload or _as_dict(normalized_envelope.get("payload")),
            project_id=project_id
            if project_id is not None
            else normalized_envelope.get("project_id"),
            workflow_id=str(workflow_id or normalized_envelope.get("workflow_id") or "").strip()
            or None,
            job_id=job_id if job_id is not None else normalized_envelope.get("job_id"),
            source=str(normalized_envelope.get("source") or "runtime").strip() or "runtime",
            aggregate_type=str(
                aggregate_type or normalized_envelope.get("aggregate_type") or "job"
            ).strip()
            or "job",
            aggregate_id=str(aggregate_id or normalized_envelope.get("aggregate_id") or "").strip(),
            actor=_as_dict(actor) or _as_dict(normalized_envelope.get("actor")),
            urls=_as_dict(normalized_envelope.get("urls")),
            metadata=_as_dict(normalized_envelope.get("metadata")),
            event_id=normalized_event_id
            or str(normalized_envelope.get("event_id") or "").strip()
            or None,
            occurred_at=str(normalized_envelope.get("occurred_at") or "").strip() or None,
        )

        columns = [
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "payload_jsonb",
            "status",
        ]
        values = [
            str(normalized_envelope.get("event_type") or event_type or "").strip(),
            str(normalized_envelope.get("aggregate_type") or aggregate_type or "job").strip()
            or "job",
            str(normalized_envelope.get("aggregate_id") or aggregate_id or "").strip(),
            json.dumps(_as_dict(normalized_envelope.get("payload")) or normalized_payload),
            str(status or "pending").strip() or "pending",
        ]

        optional_columns: list[tuple[str, Any]] = []
        if self._column_exists("domain_event_outbox", "account_id"):
            optional_columns.append(
                (
                    "account_id",
                    str(account_id or normalized_envelope.get("account_id") or "").strip()
                    or _default_account_id(),
                )
            )
        if self._column_exists("domain_event_outbox", "project_id"):
            raw_project_id = (
                project_id if project_id is not None else normalized_envelope.get("project_id")
            )
            try:
                project_id_value = str(raw_project_id) if raw_project_id is not None else None
                optional_columns.append(
                    (
                        "project_id",
                        project_id_value,
                    )
                )
            except (TypeError, ValueError):
                optional_columns.append(("project_id", None))
        if self._column_exists("domain_event_outbox", "workflow_id"):
            optional_columns.append(
                (
                    "workflow_id",
                    str(workflow_id or normalized_envelope.get("workflow_id") or "").strip()
                    or None,
                )
            )
        if self._column_exists("domain_event_outbox", "job_id"):
            raw_job_id = job_id if job_id is not None else normalized_envelope.get("job_id")
            optional_columns.append(
                (
                    "job_id",
                    str(raw_job_id).strip() if raw_job_id is not None else None,
                )
            )
        if self._column_exists("domain_event_outbox", "severity"):
            optional_columns.append(
                (
                    "severity",
                    str(severity or normalized_envelope.get("severity") or "info").strip().lower()
                    or "info",
                )
            )
        if self._column_exists("domain_event_outbox", "summary"):
            optional_columns.append(
                (
                    "summary",
                    str(summary or normalized_envelope.get("summary") or "").strip() or None,
                )
            )
        if self._column_exists("domain_event_outbox", "actor_jsonb"):
            optional_columns.append(
                (
                    "actor_jsonb",
                    json.dumps(_as_dict(actor) or _as_dict(normalized_envelope.get("actor"))),
                )
            )
        if self._column_exists("domain_event_outbox", "event_envelope_jsonb"):
            optional_columns.append(("event_envelope_jsonb", json.dumps(normalized_envelope)))
        if self._column_exists("domain_event_outbox", "event_id"):
            optional_columns.append(
                (
                    "event_id",
                    normalized_event_id
                    or str(normalized_envelope.get("event_id") or "").strip()
                    or None,
                )
            )

        for column_name, column_value in optional_columns:
            columns.append(column_name)
            values.append(column_value)

        placeholders = ", ".join(["%s"] * len(columns))
        conflict_clause = (
            " ON CONFLICT (event_id) DO NOTHING"
            if normalized_event_id and "event_id" in columns
            else ""
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO domain_event_outbox (
                    {", ".join(columns)}
                ) VALUES ({placeholders}){conflict_clause}
                """,
                tuple(values),
            )

    @staticmethod
    def _normalize_domain_outbox_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["event_id"] = str(payload.get("event_id") or "").strip() or None
        payload["event_type"] = str(payload.get("event_type") or "").strip()
        payload["aggregate_type"] = str(payload.get("aggregate_type") or "").strip() or "job"
        payload["aggregate_id"] = str(payload.get("aggregate_id") or "").strip()
        payload["status"] = str(payload.get("status") or "pending").strip().lower() or "pending"
        payload["account_id"] = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload["workflow_id"] = str(payload.get("workflow_id") or "").strip() or None
        payload["severity"] = str(payload.get("severity") or "info").strip().lower() or "info"
        payload["summary"] = str(payload.get("summary") or "").strip() or None
        payload["claim_owner"] = str(payload.get("claim_owner") or "").strip() or None
        payload["claim_token"] = str(payload.get("claim_token") or "").strip() or None
        payload["claim_version"] = max(0, int(payload.get("claim_version") or 0))
        payload["payload"] = _as_dict(payload.get("payload") or payload.get("payload_jsonb"))
        payload["actor"] = _as_dict(payload.get("actor") or payload.get("actor_jsonb"))
        payload["envelope"] = _as_dict(
            payload.get("envelope") or payload.get("event_envelope_jsonb")
        )
        payload.pop("payload_jsonb", None)
        payload.pop("actor_jsonb", None)
        payload.pop("event_envelope_jsonb", None)
        return payload

    def claim_pending_domain_event_outbox(
        self,
        *,
        claim_owner: str,
        limit: int = 200,
        lease_seconds: int = 30,
        event_types: tuple[str, ...] | None = None,
    ) -> list[dict]:
        if not self._table_exists("domain_event_outbox"):
            return []
        columns = [
            "id",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "payload_jsonb",
            "status",
            "available_at",
            "published_at",
            "last_error",
            "created_at",
            "claim_owner",
            "claim_token",
            "claim_version",
            "claim_expires_at",
        ]
        if self._column_exists("domain_event_outbox", "event_id"):
            columns.append("event_id")
        if self._column_exists("domain_event_outbox", "account_id"):
            columns.append("account_id")
        if self._column_exists("domain_event_outbox", "project_id"):
            columns.append("project_id")
        if self._column_exists("domain_event_outbox", "workflow_id"):
            columns.append("workflow_id")
        if self._column_exists("domain_event_outbox", "job_id"):
            columns.append("job_id")
        if self._column_exists("domain_event_outbox", "severity"):
            columns.append("severity")
        if self._column_exists("domain_event_outbox", "summary"):
            columns.append("summary")
        if self._column_exists("domain_event_outbox", "actor_jsonb"):
            columns.append("actor_jsonb")
        if self._column_exists("domain_event_outbox", "event_envelope_jsonb"):
            columns.append("event_envelope_jsonb")
        returning_columns = [f"o.{column}" for column in columns]
        normalized_lease_seconds = max(5, int(lease_seconds or 5))
        normalized_claim_owner = str(claim_owner or "").strip()
        if not normalized_claim_owner:
            raise ValueError("Outbox claim owner is required")
        normalized_event_types = tuple(
            value for value in (str(item or "").strip() for item in (event_types or ())) if value
        )
        event_type_filter = ""
        params: list[Any] = []
        if normalized_event_types:
            event_type_filter = " AND event_type = ANY(%s)"
            params.append(list(normalized_event_types))
        params.extend(
            (
                max(1, int(limit or 1)),
                normalized_claim_owner,
                normalized_lease_seconds,
                normalized_lease_seconds,
            )
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                WITH claimed AS (
                    SELECT id
                    FROM domain_event_outbox
                    WHERE status = 'pending'
                      AND available_at <= now()
                      AND (claim_expires_at IS NULL OR claim_expires_at <= now())
                      {event_type_filter}
                    ORDER BY available_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE domain_event_outbox o
                SET
                    claim_owner = %s,
                    claim_token = gen_random_uuid()::text,
                    claim_version = o.claim_version + 1,
                    claim_expires_at = now() + (%s * interval '1 second'),
                    available_at = now() + (%s * interval '1 second')
                FROM claimed c
                WHERE o.id = c.id
                RETURNING
                    {", ".join(returning_columns)}
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [self._normalize_domain_outbox_row(dict(row)) for row in rows]

    @contextmanager
    def claimed_domain_event_effect(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        claim_version: int,
        lease_seconds: int = 300,
    ) -> Iterator[dict | None]:
        """Fence one authoritative effect with the durable claimed-row lock."""

        normalized_owner = str(claim_owner or "").strip()
        normalized_token = str(claim_token or "").strip()
        normalized_version = int(claim_version or 0)
        if (
            not self._table_exists("domain_event_outbox")
            or not normalized_owner
            or not normalized_token
            or normalized_version < 1
        ):
            yield None
            return
        normalized_lease_seconds = max(30, int(lease_seconds or 300))
        with transaction_connection(self.conn):
            with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE domain_event_outbox
                       SET claim_expires_at = now() + (%s * interval '1 second'),
                           available_at = now() + (%s * interval '1 second')
                     WHERE id = %s
                       AND status = 'pending'
                       AND claim_owner = %s
                       AND claim_token = %s
                       AND claim_version = %s
                       AND claim_expires_at > now()
                    RETURNING *
                    """,
                    (
                        normalized_lease_seconds,
                        normalized_lease_seconds,
                        str(outbox_id),
                        normalized_owner,
                        normalized_token,
                        normalized_version,
                    ),
                )
                row = cur.fetchone()
            yield self._normalize_domain_outbox_row(dict(row)) if row else None

    def renew_domain_event_outbox_claim(
        self,
        outbox_id: str,
        *,
        claim_owner: str,
        claim_token: str,
        claim_version: int,
        lease_seconds: int = 300,
    ) -> dict | None:
        """Renew a live claim without holding a transaction across its effect."""

        normalized_owner = str(claim_owner or "").strip()
        normalized_token = str(claim_token or "").strip()
        normalized_version = int(claim_version or 0)
        if (
            not self._table_exists("domain_event_outbox")
            or not normalized_owner
            or not normalized_token
            or normalized_version < 1
        ):
            return None
        normalized_lease_seconds = max(30, int(lease_seconds or 300))
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE domain_event_outbox
                   SET claim_expires_at = now() + (%s * interval '1 second'),
                       available_at = now() + (%s * interval '1 second')
                 WHERE id = %s
                   AND status = 'pending'
                   AND claim_owner = %s
                   AND claim_token = %s
                   AND claim_version = %s
                   AND claim_expires_at > now()
                RETURNING *
                """,
                (
                    normalized_lease_seconds,
                    normalized_lease_seconds,
                    str(outbox_id),
                    normalized_owner,
                    normalized_token,
                    normalized_version,
                ),
            )
            row = cur.fetchone()
        return self._normalize_domain_outbox_row(dict(row)) if row else None

    def get_domain_event_outbox_claim_status(
        self,
        *,
        outbox_id: str,
        claim_owner: str,
        claim_version: int,
    ) -> dict[str, Any]:
        """Read whether one exact fenced claim still has a live lease."""

        normalized_outbox_id = str(outbox_id or "").strip()
        normalized_owner = str(claim_owner or "").strip()
        normalized_version = int(claim_version or 0)
        if (
            not self._table_exists("domain_event_outbox")
            or not normalized_outbox_id
            or not normalized_owner
            or normalized_version < 1
        ):
            return {"active": False, "claim_expires_at": ""}
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT claim_expires_at > now() AS active,
                       claim_expires_at
                  FROM domain_event_outbox
                 WHERE id = %s
                   AND status = 'pending'
                   AND claim_owner = %s
                   AND claim_version = %s
                 LIMIT 1
                """,
                (normalized_outbox_id, normalized_owner, normalized_version),
            )
            row = cur.fetchone()
        payload = dict(row) if row else {}
        return {
            "active": bool(payload.get("active")),
            "claim_expires_at": str(payload.get("claim_expires_at") or ""),
        }

    def list_pending_domain_event_outbox(self, limit: int = 200) -> list[dict]:
        if not self._table_exists("domain_event_outbox"):
            return []
        columns = [
            "id",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "payload_jsonb",
            "status",
            "available_at",
            "published_at",
            "last_error",
            "created_at",
        ]
        if self._column_exists("domain_event_outbox", "event_id"):
            columns.append("event_id")
        if self._column_exists("domain_event_outbox", "account_id"):
            columns.append("account_id")
        if self._column_exists("domain_event_outbox", "project_id"):
            columns.append("project_id")
        if self._column_exists("domain_event_outbox", "workflow_id"):
            columns.append("workflow_id")
        if self._column_exists("domain_event_outbox", "job_id"):
            columns.append("job_id")
        if self._column_exists("domain_event_outbox", "severity"):
            columns.append("severity")
        if self._column_exists("domain_event_outbox", "summary"):
            columns.append("summary")
        if self._column_exists("domain_event_outbox", "actor_jsonb"):
            columns.append("actor_jsonb")
        if self._column_exists("domain_event_outbox", "event_envelope_jsonb"):
            columns.append("event_envelope_jsonb")
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    {", ".join(columns)}
                FROM domain_event_outbox
                WHERE status = 'pending'
                  AND available_at <= now()
                ORDER BY available_at ASC, id ASC
                LIMIT %s
                """,
                (max(1, int(limit or 1)),),
            )
            rows = cur.fetchall()
        return [self._normalize_domain_outbox_row(dict(row)) for row in rows]

    def mark_domain_event_outbox_status(
        self,
        outbox_id: str,
        *,
        status: str,
        claim_owner: str,
        claim_token: str,
        claim_version: int,
        last_error: str | None = None,
        available_at: str | None = None,
    ) -> dict | None:
        if not self._table_exists("domain_event_outbox"):
            return None
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"pending", "published", "failed"}:
            raise ValueError(f"Unsupported outbox status '{status}'")
        normalized_owner = str(claim_owner or "").strip()
        normalized_token = str(claim_token or "").strip()
        normalized_version = int(claim_version or 0)
        if not normalized_owner or not normalized_token or normalized_version < 1:
            return None
        fields = [
            SQL_STATUS_EQ,
            "claim_owner = NULL",
            "claim_token = NULL",
            "claim_expires_at = NULL",
        ]
        params: list[Any] = [normalized_status]
        if self._column_exists("domain_event_outbox", "published_at"):
            if normalized_status == "published":
                fields.append("published_at = now()")
            elif normalized_status == "pending":
                fields.append("published_at = NULL")
        if self._column_exists("domain_event_outbox", "last_error"):
            fields.append(SQL_LAST_ERROR_EQ)
            params.append(str(last_error or "").strip() or None)
        if available_at is not None:
            fields.append("available_at = %s")
            params.append(str(available_at).strip())
        params.extend(
            (
                str(outbox_id),
                normalized_owner,
                normalized_token,
                normalized_version,
            )
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE domain_event_outbox
                SET {", ".join(fields)}
                WHERE id = %s
                  AND status = 'pending'
                  AND claim_owner = %s
                  AND claim_token = %s
                  AND claim_version = %s
                  AND claim_expires_at > now()
                RETURNING *
                """,
                tuple(params),
            )
            row = cur.fetchone()
        return self._normalize_domain_outbox_row(dict(row)) if row else None

    def mark_domain_event_outbox_published_for_event(self, event_id: str) -> int:
        if not self._table_exists("domain_event_outbox"):
            return 0
        if not self._column_exists("domain_event_outbox", "event_id"):
            return 0
        normalized_event_id = str(event_id or "").strip()
        if not normalized_event_id:
            return 0
        fields = ["status = 'published'"]
        if self._column_exists("domain_event_outbox", "published_at"):
            fields.append("published_at = now()")
        if self._column_exists("domain_event_outbox", "last_error"):
            fields.append("last_error = NULL")
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE domain_event_outbox
                SET {", ".join(fields)}
                WHERE event_id = %s
                  AND status = 'pending'
                  AND claim_owner IS NULL
                  AND claim_token IS NULL
                """,
                (normalized_event_id,),
            )
            return int(cur.rowcount or 0)


__all__ = ["PlatformLoggerEventsOutboxMixin"]
