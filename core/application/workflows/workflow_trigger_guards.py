"""Physical workflow and trigger fences used by supported trigger mutations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core.application.management.errors import ServiceNotFoundError
from core.application.workflows.workflow_entrypoint_repository import _row_to_dict
from core.infrastructure.db.db_pool import transaction_connection


def lock_workflow_owner(
    cursor: Any, workflow_id: str, *, account_id: str = "", include_trashed: bool = False
) -> dict[str, Any]:
    cursor.execute(
        "SELECT * FROM registry_workflows WHERE workflow_id = %s "
        "AND (%s = '' OR account_id IS NULL OR account_id::text = %s) "
        "AND (%s OR deleted_at IS NULL) FOR UPDATE",
        (workflow_id, account_id, account_id, include_trashed),
    )
    row = _row_to_dict(cursor, cursor.fetchone())
    if not row:
        raise ServiceNotFoundError("Workflow not found")
    return row


@contextmanager
def workflow_trigger_transaction(
    store: Any, workflow_id: str, *, account_id: str = "", include_trashed: bool = False
) -> Iterator[Any]:
    with transaction_connection(store.conn) as connection, connection.cursor() as cursor:
        lock_workflow_owner(
            cursor, workflow_id, account_id=account_id, include_trashed=include_trashed
        )
        yield cursor


def lock_entrypoint(cursor: Any, *, entry_key: str, workflow_id: str) -> dict[str, Any]:
    cursor.execute(
        "SELECT * FROM workflow_entrypoints WHERE entry_key = %s AND workflow_id = %s FOR UPDATE",
        (entry_key, workflow_id),
    )
    row = _row_to_dict(cursor, cursor.fetchone())
    if not row:
        raise ServiceNotFoundError("Workflow entrypoint not found")
    return row


@contextmanager
def provider_trigger_admission(store: Any, trigger: dict[str, Any]) -> Iterator[bool]:
    """Hold the active owner through provider auth and deferred local effects."""
    with transaction_connection(store.conn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT workflow_id FROM registry_workflows WHERE workflow_id=%s AND deleted_at IS NULL FOR SHARE",
            (str(trigger.get("workflow_id") or ""),),
        )
        active = cursor.fetchone() is not None
        if active:
            cursor.execute(
                "SELECT trigger_id FROM workflow_provider_triggers WHERE trigger_id=%s AND workflow_id=%s AND project_id=%s AND enabled=true",
                (
                    str(trigger.get("trigger_id") or ""),
                    str(trigger.get("workflow_id") or ""),
                    str(trigger.get("project_id") or ""),
                ),
            )
            active = cursor.fetchone() is not None
        yield active
