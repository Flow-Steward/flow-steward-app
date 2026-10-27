"""One physical workflow soft-deletion operation for registry and unified Trash."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.management.errors import ServiceNotFoundError
from core.application.registry.workflow_definition_rows import load_workflow_definition_rows
from core.application.workflows.workflow_credential_retirement import retire_entrypoint_credentials
from core.application.workflows.workflow_deletion_guard import (
    assert_workflow_has_no_nonterminal_jobs,
)
from core.application.workflows.workflow_entrypoint_repository import _row_to_dict
from core.application.workflows.workflow_history_snapshot import history_snapshot
from core.application.workflows.workflow_trigger_guards import lock_workflow_owner


class _DefinitionCursor:
    """Normalize rows for the existing definition reader without changing its owner."""

    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor

    def execute(self, statement: str, parameters: Any) -> None:
        self.cursor.execute(statement, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return [_row_to_dict(self.cursor, row) for row in self.cursor.fetchall()]


def retire_workflow(
    cursor: Any,
    *,
    store: Any,
    workflow_id: str,
    account_id: str,
    author: str,
    record_change: Callable[[Any, dict[str, Any]], None],
) -> None:
    """Caller owns commit/rollback and projection refresh; all SQL uses its cursor."""
    owner = lock_workflow_owner(cursor, workflow_id, account_id=account_id)
    assert_workflow_has_no_nonterminal_jobs(
        store, workflow_id=workflow_id, account_id=str(owner.get("account_id") or ""), cursor=cursor
    )
    definition = load_workflow_definition_rows(_DefinitionCursor(cursor), [workflow_id])[
        workflow_id
    ]
    snapshot = history_snapshot(
        {
            **owner,
            "steps": definition.steps,
            "groups": definition.groups,
            "for_each": definition.for_each,
        }
    )
    cursor.execute(
        "SELECT * FROM workflow_entrypoints WHERE workflow_id=%s ORDER BY entrypoint_id FOR UPDATE",
        (workflow_id,),
    )
    entrypoints = [_row_to_dict(cursor, row) for row in cursor.fetchall()]
    retire_entrypoint_credentials(cursor, entrypoints, retired_workflow=workflow_id)
    if "deleted_by" in owner:
        cursor.execute(
            "UPDATE registry_workflows SET deleted_at=now(),deleted_by=%s,revision=revision+1 WHERE workflow_id=%s",
            (author, workflow_id),
        )
    else:
        cursor.execute(
            "UPDATE registry_workflows SET deleted_at=now(),revision=revision+1 WHERE workflow_id=%s",
            (workflow_id,),
        )
    if cursor.rowcount != 1:
        raise ServiceNotFoundError("Workflow tombstone was not written")
    cursor.execute(
        "UPDATE workflow_entrypoints SET enabled=false,updated_at=now() WHERE workflow_id=%s",
        (workflow_id,),
    )
    cursor.execute(
        "UPDATE workflow_provider_triggers SET enabled=false,updated_at=now() WHERE workflow_id=%s",
        (workflow_id,),
    )
    cursor.execute(
        "UPDATE scheduled_jobs SET enabled=false,status='paused' WHERE workflow_id=%s",
        (workflow_id,),
    )
    record_change(cursor, snapshot)
