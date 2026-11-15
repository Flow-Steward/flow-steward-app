"""A project's connection choices for extension workflow slots (ADR 0068).

``workflow_connection_slot_choices`` holds one row per ``(account, project,
source install, local workflow, slot)``. Deleting the connection, the project
or the account cascades; uninstall deletes the install's rows here.

The caller owns the cursor and its transaction. Rows come back as dicts
whether the cursor yields mappings or tuples.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SlotOwner:
    """Where a project's slot choices live: one project, one installed workflow."""

    account_id: str
    project_id: str
    source_install_id: str
    local_workflow_id: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.account_id, self.project_id, self.source_install_id, self.local_workflow_id)


def _rows(cur: Any) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    names = [column[0] for column in (cur.description or [])]
    return [
        dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
        for row in rows
    ]


class WorkflowConnectionSlotRepository:
    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def choices(self, owner: SlotOwner) -> dict[str, str]:
        """``slot → connection_id`` the project chose for one workflow."""
        if not all(owner.key()):
            return {}
        self.cur.execute(
            """SELECT slot, connection_id FROM public.workflow_connection_slot_choices
                WHERE account_id = %s AND project_id = %s
                  AND source_install_id = %s AND local_workflow_id = %s""",
            owner.key(),
        )
        return {str(row["slot"]): str(row["connection_id"]) for row in _rows(self.cur)}

    def install_choices(self, *, account_id: str, install_id: str) -> list[dict[str, Any]]:
        """Every choice of one install, for the release check."""
        self.cur.execute(
            """SELECT project_id::text AS project_id, local_workflow_id, slot, connection_id
                 FROM public.workflow_connection_slot_choices
                WHERE account_id = %s AND source_install_id = %s""",
            (account_id, install_id),
        )
        return _rows(self.cur)

    def save(self, owner: SlotOwner, slot: str, connection_id: str, *, actor: str = "") -> None:
        self.cur.execute(
            """INSERT INTO public.workflow_connection_slot_choices
                   (account_id, project_id, source_install_id, local_workflow_id, slot,
                    connection_id, chosen_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (account_id, project_id, source_install_id, local_workflow_id, slot)
               DO UPDATE SET connection_id = EXCLUDED.connection_id,
                             chosen_by = EXCLUDED.chosen_by,
                             updated_at = now()""",
            (*owner.key(), slot, connection_id, actor),
        )

    def clear(self, owner: SlotOwner, slot: str) -> None:
        self.cur.execute(
            """DELETE FROM public.workflow_connection_slot_choices
                WHERE account_id = %s AND project_id = %s AND source_install_id = %s
                  AND local_workflow_id = %s AND slot = %s""",
            (*owner.key(), slot),
        )

    def delete_install(self, *, account_id: str, install_id: str) -> int:
        """Remove every choice of an install; uninstall calls it."""
        self.cur.execute(
            """DELETE FROM public.workflow_connection_slot_choices
                WHERE account_id = %s AND source_install_id = %s""",
            (account_id, install_id),
        )
        return int(getattr(self.cur, "rowcount", 0) or 0)

    def delete_slots(
        self, *, account_id: str, install_id: str, keep: Iterable[tuple[str, str]]
    ) -> int:
        """Drop the install's choices for slots a release no longer declares.

        *keep* lists the ``(local workflow id, slot)`` pairs that remain.
        """
        kept = set(keep)
        removed = 0
        for row in self.install_choices(account_id=account_id, install_id=install_id):
            if (str(row["local_workflow_id"]), str(row["slot"])) in kept:
                continue
            self.cur.execute(
                """DELETE FROM public.workflow_connection_slot_choices
                    WHERE account_id = %s AND project_id = %s AND source_install_id = %s
                      AND local_workflow_id = %s AND slot = %s""",
                (
                    account_id,
                    row["project_id"],
                    install_id,
                    row["local_workflow_id"],
                    row["slot"],
                ),
            )
            removed += 1
        return removed

    def candidates(
        self, *, account_id: str, project_id: str, extension_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        """The project's extension connections of these extensions (non-secret columns)."""
        wanted = sorted({str(item) for item in extension_ids if str(item)})
        if not account_id or not project_id or not wanted:
            return []
        self.cur.execute(
            """SELECT connection_id, display_name, extension_id, extension_install_id,
                      connection_type, enabled, health_status, runtime_status
                 FROM public.project_connections
                WHERE account_id = %s AND project_id = %s
                  AND connection_kind = 'extension' AND extension_id = ANY(%s)
                ORDER BY lower(display_name), connection_id""",
            (account_id, project_id, wanted),
        )
        return _rows(self.cur)

    def linked_projects(
        self, *, account_id: str, workflow_ids: Iterable[str]
    ) -> list[dict[str, str]]:
        """``project_id``/``workflow_id`` of every live project link of these workflows."""
        wanted = sorted({str(item) for item in workflow_ids if str(item)})
        if not wanted:
            return []
        self.cur.execute(
            """SELECT pw.project_id::text AS project_id, pw.workflow_id
                 FROM public.project_workflows pw
                 JOIN public.projects p ON p.id = pw.project_id
                WHERE p.account_id = %s AND pw.workflow_id = ANY(%s)
                  AND pw.deleted_at IS NULL AND p.deleted_at IS NULL""",
            (account_id, wanted),
        )
        return [
            {"project_id": str(row["project_id"]), "workflow_id": str(row["workflow_id"])}
            for row in _rows(self.cur)
        ]


__all__ = ["SlotOwner", "WorkflowConnectionSlotRepository"]
