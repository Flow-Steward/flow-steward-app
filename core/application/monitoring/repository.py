"""Transactional domain storage for current observations and state-change history."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from core.application.monitoring.contracts import (
    HeartbeatReport,
    IngestResult,
    RuntimeInstallationContext,
    RuntimeStatusPayload,
    StatusRecord,
    utc_time,
)
from core.infrastructure.config.monitoring_runtime import (
    MonitoringRuntimeConfig,
    load_monitoring_runtime_config,
)
from core.infrastructure.db.db_pool import get_threaded_pool, pool_bounds, pooled_connection
from core.infrastructure.db.psycopg_runtime.extras import RealDictCursor


def _state(payload: RuntimeStatusPayload) -> tuple[object, ...]:
    """Check/report timestamps and sequence are not state transitions."""
    return (
        payload.lifecycle,
        payload.readiness.state,
        payload.readiness.evidence,
        payload.health.state,
        payload.health.evidence,
        payload.consumer_state,
        payload.source_availability,
    )


class RuntimeStatusRepository:
    """Borrow independent pooled connections; never share a logger's transaction."""

    def __init__(self, dsn: str, *, config: MonitoringRuntimeConfig | None = None) -> None:
        minimum, maximum = pool_bounds()
        self._pool = get_threaded_pool(dsn, minconn=minimum, maxconn=maximum)
        self._config = config or load_monitoring_runtime_config({})

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with pooled_connection(self._pool, autocommit=False) as borrowed:
            connection = cast(Any, borrowed)
            try:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{self._config.operation_timeout_seconds * 1000}ms",),
                    )
                    yield cursor
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def store(self, report: HeartbeatReport, *, history_enabled: bool) -> IngestResult:
        """Serialize one observation key; only newer sequences change current/history."""
        key = (
            report.installation_id,
            report.service_type,
            report.instance_id,
            report.incarnation_id,
            report.source,
        )
        with self._transaction() as cursor:
            cursor.execute("SELECT clock_timestamp() AS received_at")
            received_at = cursor.fetchone()["received_at"].astimezone(UTC)
            age = (received_at - report.observed_at).total_seconds()
            if age < -30 or age > self._config.stale_seconds:
                return IngestResult(disposition="rejected")
            for check in (report.payload.readiness, report.payload.health):
                if (
                    check.checked_at is not None
                    and (check.checked_at - received_at).total_seconds() > 30
                ):
                    return IngestResult(disposition="rejected")
            payload = json.dumps(report.payload.model_dump(mode="json"), separators=(",", ":"))
            record_id = uuid4()
            cursor.execute(
                """INSERT INTO public.fs_runtime_status_current
                (id, schema_version, installation_id, deployment_profile, service_type,
                 instance_id, incarnation_id, sequence, observed_at, received_at, source, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (installation_id,service_type,instance_id,incarnation_id,source)
                DO NOTHING RETURNING id""",
                (
                    record_id,
                    report.schema_version,
                    report.installation_id,
                    report.deployment_profile,
                    report.service_type,
                    report.instance_id,
                    report.incarnation_id,
                    report.sequence,
                    report.observed_at,
                    received_at,
                    report.source,
                    payload,
                ),
            )
            inserted = cursor.fetchone() is not None
            changed = inserted
            if not inserted:
                cursor.execute(
                    """SELECT id, sequence, payload FROM public.fs_runtime_status_current
                    WHERE installation_id=%s AND service_type=%s AND instance_id=%s
                    AND incarnation_id=%s AND source=%s FOR UPDATE""",
                    key,
                )
                previous = cursor.fetchone()
                # A concurrent retention delete may win between INSERT and row lock.
                # Retry the delivery rather than guessing the missing row was committed.
                if previous is None:
                    raise RuntimeError("runtime observation changed during transaction")
                record_id = UUID(str(previous["id"]))
                if report.sequence <= previous["sequence"]:
                    return IngestResult(disposition="duplicate", record_id=record_id)
                changed = _state(
                    RuntimeStatusPayload.model_validate(previous["payload"])
                ) != _state(report.payload)
                cursor.execute(
                    """UPDATE public.fs_runtime_status_current
                    SET schema_version=%s, deployment_profile=%s, sequence=%s,
                        observed_at=%s, received_at=%s, payload=%s::jsonb
                    WHERE id=%s AND sequence<%s""",
                    (
                        report.schema_version,
                        report.deployment_profile,
                        report.sequence,
                        report.observed_at,
                        received_at,
                        payload,
                        record_id,
                        report.sequence,
                    ),
                )
            if history_enabled and changed:
                cursor.execute(
                    """INSERT INTO public.fs_runtime_status_history
                    (id, current_record_id, schema_version, installation_id, deployment_profile,
                     service_type, instance_id, incarnation_id, sequence, observed_at,
                     received_at, source, payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                    (
                        uuid4(),
                        record_id,
                        report.schema_version,
                        report.installation_id,
                        report.deployment_profile,
                        report.service_type,
                        report.instance_id,
                        report.incarnation_id,
                        report.sequence,
                        report.observed_at,
                        received_at,
                        report.source,
                        payload,
                    ),
                )
        return IngestResult(disposition="stored", record_id=record_id)

    def load(self, context: RuntimeInstallationContext) -> list[StatusRecord]:
        with self._transaction() as cursor:
            cursor.execute(
                """SELECT * FROM public.fs_runtime_status_current
                WHERE installation_id=%s AND deployment_profile=%s
                ORDER BY service_type, instance_id, incarnation_id, source""",
                (context.installation_id, context.deployment_profile),
            )
            rows = cursor.fetchall()
        for row in rows:
            row["observed_at"] = row["observed_at"].astimezone(UTC)
            row["received_at"] = row["received_at"].astimezone(UTC)
        return [StatusRecord.model_validate(row) for row in rows]

    def prune(
        self,
        *,
        context: RuntimeInstallationContext,
        current_before: datetime,
        history_before: datetime,
        limit: int = 500,
    ) -> dict[str, int]:
        """Clean both stores independently, scoped to this installation in bounded batches."""
        if isinstance(limit, bool) or not 1 <= limit <= 5000:
            raise ValueError("runtime retention batch must be between 1 and 5000")
        current_before, history_before = utc_time(current_before), utc_time(history_before)
        with self._transaction() as cursor:
            cursor.execute(
                """WITH expired AS (
                    SELECT id FROM public.fs_runtime_status_current
                    WHERE installation_id=%s AND received_at<%s
                    ORDER BY received_at, id LIMIT %s FOR UPDATE SKIP LOCKED)
                DELETE FROM public.fs_runtime_status_current AS current
                USING expired WHERE current.id=expired.id""",
                (context.installation_id, current_before, limit),
            )
            current_count = cursor.rowcount
            cursor.execute(
                """WITH expired AS (
                    SELECT id FROM public.fs_runtime_status_history
                    WHERE installation_id=%s AND received_at<%s
                    ORDER BY received_at, id LIMIT %s FOR UPDATE SKIP LOCKED)
                DELETE FROM public.fs_runtime_status_history AS history
                USING expired WHERE history.id=expired.id""",
                (context.installation_id, history_before, limit),
            )
            history_count = cursor.rowcount
        return {"current": current_count, "history": history_count}
