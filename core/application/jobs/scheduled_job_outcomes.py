"""Coordinate claimed schedule outcomes with current workflow/schedule authority."""

from __future__ import annotations

from typing import Any

from core.application.workflows.workflow_entrypoint_repository import _row_to_dict
from core.infrastructure.db.db_pool import transaction_connection


def update_scheduled_job_outcome(
    store: Any,
    *,
    project_id: str,
    scheduled_job_id: str,
    workflow_id: str,
    account_id: str,
    changes: dict[str, Any],
) -> None:
    """Serialize W then schedule; observations cannot reactivate a retired claim."""
    with transaction_connection(store.conn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT deleted_at FROM registry_workflows WHERE workflow_id=%s FOR SHARE",
            (workflow_id,),
        )
        owner = _row_to_dict(cursor, cursor.fetchone())
        if not owner:
            return
        cursor.execute(
            "SELECT enabled FROM scheduled_jobs WHERE id=%s AND project_id=%s "
            "AND workflow_id=%s AND (%s='' OR account_id::text=%s) FOR UPDATE",
            (scheduled_job_id, project_id, workflow_id, account_id, account_id),
        )
        schedule = _row_to_dict(cursor, cursor.fetchone())
        if not schedule:
            return
        current_changes = dict(changes)
        if owner.get("deleted_at") is not None or not schedule["enabled"]:
            current_changes.pop("status", None)
            current_changes.pop("next_run_at", None)
        # The store's cursor resolves to this pinned physical transaction.
        store.update_scheduled_job(
            project_id, scheduled_job_id, account_id=account_id or None, **current_changes
        )
