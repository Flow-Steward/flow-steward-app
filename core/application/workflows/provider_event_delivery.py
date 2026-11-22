"""Crash-safe delivery of provider events to the workflows bound to them.

A provider event is stored once, unique on (project, provider, ``source_id``),
together with one *dispatch record* per workflow trigger it matches. The
dispatch record is keyed like the delivery, ``project_id|workflow_id|task_id``.
For a connection poll both are written in the poll transaction, under the
connection row lock and with the poll cursor, so a cursor is never saved
without its events and an event never without its dispatch records.

Starting one workflow is one transaction: the job, its dispatch outbox row, the
delivery (``provider_webhook_deliveries``, status ``accepted``) and the dispatch
record (``accepted``) commit together. Inside that transaction, before the
delivery is written, the job re-checks under the same connection row lock that
the trigger is still enabled and the connection's ``config_generation`` is the
one the event was polled under. A switch-off that committed first leaves the
record ``failed``; one that commits later finds the job already queued.

Unfinished records are picked up by :func:`sweep_pending_provider_event_dispatches`,
which a worker runs periodically; the normal path right after the poll
transaction calls the same :func:`dispatch_provider_event`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.infrastructure.db.db_pool import (
    register_transaction_after_commit,
    transaction_after_commit_supported,
    transaction_connection,
)

logger = logging.getLogger(__name__)

#: A record that fails this often for a transient reason is given up as failed.
MAX_DISPATCH_ATTEMPTS = 10
#: The sweep leaves a record alone this long after it was written or last tried.
SWEEP_MIN_AGE_SECONDS = 30
SWEEP_BATCH_SIZE = 100

#: Test-only fault injection: name -> callable run at that point.
FAULT_POINTS: dict[str, Callable[[], None]] = {}


def fault_point(name: str) -> None:
    hook = FAULT_POINTS.get(name)
    if hook is not None:
        hook()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class DispatchRejected(Exception):
    """The job transaction refused to start the workflow; its job rolls back."""

    def __init__(self, reason: str, *, final: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        #: False when another attempt owns or finished the record.
        self.final = final


@dataclass(frozen=True)
class DeliveryBinding:
    """The connection and configuration an event was polled under."""

    connection_id: str
    config_generation: int

    @classmethod
    def from_mapping(cls, value: Any) -> DeliveryBinding | None:
        payload = _as_dict(value)
        connection_id = _text(payload.get("connection_id"))
        try:
            generation = int(payload["config_generation"])
        except (KeyError, TypeError, ValueError):
            return None
        if not connection_id:
            return None
        return cls(connection_id=connection_id, config_generation=generation)


@dataclass
class RecordedEvent:
    """What :func:`record_provider_event` wrote."""

    new: bool
    delivery_keys: list[str] = field(default_factory=list)


def provider_trigger_task_id(provider_key: str, source_id: str) -> str:
    return f"provider-trigger:{provider_key}:{source_id}"


def provider_trigger_delivery_key(project_id: str, workflow_id: str, task_id: str) -> str:
    return "|".join((project_id, workflow_id, task_id))


def record_provider_event(
    bl: Any,
    *,
    project_id: str,
    provider_key: str,
    source_id: str,
    event_type: str,
    payload: dict[str, Any],
    triggers: list[dict[str, Any]],
    binding: DeliveryBinding | None = None,
) -> RecordedEvent:
    """Store the event once and one pending dispatch record per matched trigger.

    Runs inside the caller's transaction when one is open on ``bl`` (the poll
    transaction), otherwise in its own. A replay of a stored ``source_id``
    writes nothing and returns ``new=False``.
    """
    task_id = provider_trigger_task_id(provider_key, source_id)
    with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO provider_event_inbox (
                account_id, project_id, provider_key, source_id, event_type, payload,
                connection_id, config_generation
            )
            SELECT p.account_id, p.id, %s, %s, %s, %s::jsonb, %s, %s
              FROM projects p
             WHERE p.id = %s
            ON CONFLICT (project_id, provider_key, source_id) DO NOTHING
            RETURNING event_row_id
            """,
            (
                provider_key,
                source_id,
                event_type,
                json.dumps(payload, default=str),
                binding.connection_id if binding else None,
                binding.config_generation if binding else None,
                project_id,
            ),
        )
        row = cur.fetchone()
        if not row:
            return RecordedEvent(new=False)
        event_row_id = row[0] if not isinstance(row, dict) else row["event_row_id"]
        keys: list[str] = []
        for trigger in triggers:
            workflow_id = _text(trigger.get("workflow_id"))
            delivery_key = provider_trigger_delivery_key(project_id, workflow_id, task_id)
            cur.execute(
                """
                INSERT INTO provider_event_dispatches (
                    delivery_key, event_row_id, project_id, provider_key, workflow_id,
                    trigger_id, connection_id, config_generation
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (delivery_key) DO NOTHING
                RETURNING delivery_key
                """,
                (
                    delivery_key,
                    event_row_id,
                    project_id,
                    provider_key,
                    workflow_id,
                    _text(trigger.get("trigger_id")),
                    binding.connection_id if binding else None,
                    binding.config_generation if binding else None,
                ),
            )
            if cur.fetchone():
                keys.append(delivery_key)
    return RecordedEvent(new=True, delivery_keys=keys)


_RECORD_COLUMNS = (
    "delivery_key",
    "project_id",
    "provider_key",
    "workflow_id",
    "trigger_id",
    "connection_id",
    "config_generation",
    "status",
    "job_id",
    "attempts",
    "event_type",
    "payload",
    "source_id",
    "trigger_display_name",
    "trigger_type_id",
)


def _load_record(bl: Any, delivery_key: str) -> dict[str, Any]:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.delivery_key, d.project_id::text, d.provider_key, d.workflow_id,
                   d.trigger_id, d.connection_id, d.config_generation, d.status,
                   d.job_id::text, d.attempts, i.event_type, i.payload, i.source_id,
                   t.display_name, t.trigger_type_id
              FROM provider_event_dispatches d
              JOIN provider_event_inbox i ON i.event_row_id = d.event_row_id
              LEFT JOIN workflow_provider_triggers t ON t.trigger_id::text = d.trigger_id
             WHERE d.delivery_key = %s
            """,
            (delivery_key,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(_RECORD_COLUMNS, row, strict=False))


def _mark_failed(bl: Any, delivery_key: str, reason: str) -> None:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE provider_event_dispatches
               SET status = 'failed', reason = %s, updated_at = now()
             WHERE delivery_key = %s AND status = 'pending'
            """,
            (reason[:2000], delivery_key),
        )


def _record_attempt_failure(bl: Any, delivery_key: str, reason: str) -> None:
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE provider_event_dispatches
               SET attempts = attempts + 1,
                   reason = %s,
                   status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END,
                   updated_at = now()
             WHERE delivery_key = %s AND status = 'pending'
            """,
            (reason[:2000], MAX_DISPATCH_ATTEMPTS, delivery_key),
        )


def accept_dispatch_in_job_transaction(conn: Any, job_id: str, *, record: dict[str, Any]) -> None:
    """``job_created_hook``: claim, re-check and record the delivery with the job.

    Raises :class:`DispatchRejected` to roll the new job back.
    """
    delivery_key = _text(record.get("delivery_key"))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM provider_event_dispatches WHERE delivery_key = %s "
            "FOR UPDATE SKIP LOCKED",
            (delivery_key,),
        )
        row = cur.fetchone()
        if not row:
            raise DispatchRejected("claimed_elsewhere", final=False)
        status = row["status"] if isinstance(row, dict) else row[0]
        if status != "pending":
            raise DispatchRejected("already_dispatched", final=False)
        fault_point("job_before_connection_lock")
        connection_id = _text(record.get("connection_id"))
        if connection_id:
            cur.execute(
                "SELECT enabled, config_generation FROM project_connections "
                "WHERE connection_id = %s FOR UPDATE",
                (connection_id,),
            )
            connection = cur.fetchone()
            if not connection:
                raise DispatchRejected("connection_missing")
            enabled, generation = (
                (connection["enabled"], connection["config_generation"])
                if isinstance(connection, dict)
                else (connection[0], connection[1])
            )
            if not enabled:
                raise DispatchRejected("connection_disabled")
            if int(generation) != int(record.get("config_generation") or 0):
                raise DispatchRejected("config_generation_changed")
        cur.execute(
            "SELECT 1 FROM registry_workflows WHERE workflow_id = %s AND deleted_at IS NULL "
            "FOR SHARE",
            (_text(record.get("workflow_id")),),
        )
        if cur.fetchone() is None:
            raise DispatchRejected("trigger_inactive")
        cur.execute(
            # FOR SHARE: disabling the trigger waits for this job, or is seen by it.
            "SELECT 1 FROM workflow_provider_triggers WHERE trigger_id = %s AND workflow_id = %s "
            "AND project_id = %s AND enabled = true FOR SHARE",
            (
                _text(record.get("trigger_id")),
                _text(record.get("workflow_id")),
                _text(record.get("project_id")),
            ),
        )
        if cur.fetchone() is None:
            raise DispatchRejected("trigger_inactive")
        cur.execute(
            """
            INSERT INTO provider_webhook_deliveries (
                provider_key, delivery_key, request_id, status, job_id
            )
            VALUES (%s, %s, %s, 'accepted', %s)
            ON CONFLICT (provider_key, delivery_key) DO UPDATE
                SET status = 'accepted',
                    request_id = EXCLUDED.request_id,
                    job_id = EXCLUDED.job_id,
                    last_error = '',
                    updated_at = now()
              WHERE provider_webhook_deliveries.status <> 'accepted'
            RETURNING delivery_key
            """,
            (
                _text(record.get("provider_key")),
                delivery_key,
                _text(record.get("source_id")),
                job_id,
            ),
        )
        if cur.fetchone() is None:
            raise DispatchRejected("already_dispatched", final=False)
        cur.execute(
            """
            UPDATE provider_event_dispatches
               SET status = 'accepted', job_id = %s, reason = '', updated_at = now()
             WHERE delivery_key = %s
            """,
            (job_id, delivery_key),
        )


def _workflow_inputs(
    *, provider_key: str, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    files = payload.get("files")
    return {
        **payload,
        "provider_key": provider_key,
        "event_name": event_type,
        "provider_event": payload,
        "files": list(files) if isinstance(files, list) else [],
    }


def dispatch_provider_event(
    bl: Any,
    *,
    delivery_key: str,
    enqueue_task_fn: Any,
) -> dict[str, Any]:
    """Start the workflow of one pending dispatch record, at most once."""
    # Imported here: the registry/launch stack is heavy and this module is also
    # imported by the poll transaction, which only records.
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService
    from core.application.registry.workflow_secret_defaults import (
        workflow_secret_launch_snapshot,
    )
    from core.application.workflows.workflow_entrypoint_repository import (
        resolve_project_run_config_for_workflow_project,
    )
    from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

    record = _load_record(bl, delivery_key)
    if not record:
        return {"delivery_key": delivery_key, "status": "missing"}
    workflow_id = _text(record.get("workflow_id"))
    trigger_id = _text(record.get("trigger_id"))
    outcome: dict[str, Any] = {
        "trigger_id": trigger_id,
        "workflow_id": workflow_id,
        "delivery_key": delivery_key,
    }
    if record.get("status") != "pending":
        return {
            **outcome,
            "status": "deduplicated" if record.get("status") == "accepted" else "skipped",
            "job_id": _text(record.get("job_id")),
        }
    provider_key = _text(record.get("provider_key"))
    project_id = _text(record.get("project_id"))
    event_type = _text(record.get("event_type"))
    source_id = _text(record.get("source_id"))
    payload = _as_dict(record.get("payload"))
    if isinstance(record.get("payload"), str):
        payload = _as_dict(json.loads(record["payload"]))

    _, run_cfg = resolve_project_run_config_for_workflow_project(bl, workflow_id, project_id)
    if not run_cfg:
        _mark_failed(bl, delivery_key, "workflow_project_binding_not_found")
        return {**outcome, "status": "skipped", "reason": "workflow_project_binding_not_found"}
    inputs = _workflow_inputs(provider_key=provider_key, event_type=event_type, payload=payload)
    validation = WorkflowInterfaceService().validate_inputs_for_workflow_id(
        workflow_id, inputs, channel="webhook"
    )
    if not validation.ok:
        _mark_failed(bl, delivery_key, "workflow_input_validation_failed")
        return {
            **outcome,
            "status": "rejected",
            "reason": "workflow_input_validation_failed",
            "errors": list(validation.errors or []),
        }
    if validation.interface.get("enabled"):
        inputs = dict(validation.inputs)
    task_id = provider_trigger_task_id(provider_key, source_id)

    def _hook(conn: Any, job_id: str) -> None:
        accept_dispatch_in_job_transaction(conn, job_id, record=record)

    try:
        job_id = enqueue_workflow_start(
            enqueue_task_fn=enqueue_task_fn,
            task_id=task_id,
            task_summary=_text(record.get("trigger_display_name"))
            or f"{provider_key} workflow trigger",
            task_source="provider",
            project_id=project_id,
            project_name=_text(run_cfg.get("project_name") or run_cfg.get("name")),
            project_config=dict(run_cfg),
            selected_workflow_id=workflow_id,
            workflow_selection_source="provider_trigger",
            entry_path="provider_webhook",
            input_contract="direct_inputs",
            source_system=provider_key,
            source_event_id=source_id,
            source_object_id=source_id,
            inputs=inputs,
            files=list(inputs.get("files") or []),
            extra_state={
                "provider_key": provider_key,
                "provider_trigger_id": trigger_id,
                "provider_trigger_type_id": _text(record.get("trigger_type_id")),
            },
            workflow_secret_ref_fingerprints=workflow_secret_launch_snapshot(
                validation.interface, inputs=inputs
            ),
            job_meta={
                "provider_key": provider_key,
                "provider_trigger_id": trigger_id,
                "event_name": event_type,
                "invocation": {
                    "channel": "provider_webhook",
                    "trigger_family": "provider_trigger",
                    "entry_path": "provider_webhook",
                    "source_system": provider_key,
                    "request_id": source_id,
                    "trigger_id": trigger_id,
                },
            },
            job_created_hook=_hook,
        )
    except DispatchRejected as exc:
        if exc.final:
            _mark_failed(bl, delivery_key, exc.reason)
            logger.info("Provider event %s not started: %s", delivery_key, exc.reason)
            return {**outcome, "status": "skipped", "reason": exc.reason}
        current = _load_record(bl, delivery_key)
        return {**outcome, "status": "deduplicated", "job_id": _text(current.get("job_id"))}
    except Exception as exc:
        _record_attempt_failure(bl, delivery_key, f"enqueue_failed: {exc}")
        logger.warning("Provider event %s did not start its workflow: %s", delivery_key, exc)
        return {**outcome, "status": "failed", "reason": "workflow_trigger_enqueue_failed"}
    return {**outcome, "status": "accepted", "job_id": str(job_id)}


def dispatch_recorded_event(
    bl: Any,
    recorded: RecordedEvent,
    *,
    enqueue_task_fn: Any,
) -> list[dict[str, Any]]:
    """Start the workflows of freshly recorded dispatch records.

    Inside an open transaction the start waits for its commit: a job must never
    exist for an event whose transaction may still roll back.
    """

    def _run() -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for key in recorded.delivery_keys:
            try:
                outcomes.append(
                    dispatch_provider_event(
                        bl,
                        delivery_key=key,
                        enqueue_task_fn=enqueue_task_fn,
                    )
                )
            except Exception as exc:
                logger.warning("Provider event %s dispatch crashed: %s", key, exc)
                outcomes.append({"delivery_key": key, "status": "failed"})
        return outcomes

    def _run_after_commit() -> None:
        # After the commit nobody reads the outcomes; each dispatch records its own.
        _run()

    if not recorded.delivery_keys or not callable(enqueue_task_fn):
        return [{"delivery_key": key, "status": "pending"} for key in recorded.delivery_keys]
    if transaction_after_commit_supported(bl.conn):
        register_transaction_after_commit(bl.conn, _run_after_commit)
        return [{"delivery_key": key, "status": "pending"} for key in recorded.delivery_keys]
    return _run()


def sweep_pending_provider_event_dispatches(
    bl: Any,
    *,
    enqueue_task_fn: Any,
    min_age_seconds: int = SWEEP_MIN_AGE_SECONDS,
    limit: int = SWEEP_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """Retry pending dispatch records no in-flight attempt holds.

    Candidates are taken with ``FOR UPDATE SKIP LOCKED``; each is then started
    in its own job transaction, which claims the record again with ``SKIP
    LOCKED``, so two workers never start the same record.
    """
    with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT delivery_key
              FROM provider_event_dispatches
             WHERE status = 'pending'
               AND updated_at < now() - (%s::integer * interval '1 second')
             ORDER BY updated_at
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """,
            (max(0, int(min_age_seconds)), max(1, int(limit))),
        )
        keys = [row["delivery_key"] if isinstance(row, dict) else row[0] for row in cur.fetchall()]
    outcomes: list[dict[str, Any]] = []
    for key in keys:
        try:
            outcomes.append(
                dispatch_provider_event(bl, delivery_key=key, enqueue_task_fn=enqueue_task_fn)
            )
        except Exception as exc:
            logger.warning("Provider event sweep could not dispatch %s: %s", key, exc)
            outcomes.append({"delivery_key": key, "status": "failed"})
    return outcomes


__all__ = [
    "FAULT_POINTS",
    "MAX_DISPATCH_ATTEMPTS",
    "DeliveryBinding",
    "DispatchRejected",
    "RecordedEvent",
    "accept_dispatch_in_job_transaction",
    "dispatch_provider_event",
    "dispatch_recorded_event",
    "fault_point",
    "provider_trigger_delivery_key",
    "provider_trigger_task_id",
    "record_provider_event",
    "sweep_pending_provider_event_dispatches",
]
