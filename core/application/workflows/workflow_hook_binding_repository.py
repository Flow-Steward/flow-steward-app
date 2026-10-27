"""Project hook bindings: one active pointer and immutable revisions (spec §9.2, §11).

``registry_workflow_hook_bindings`` holds one row per ``(account, project,
source install, local workflow, hook id)`` with an optimistic ``revision`` and
the active revision; ``registry_workflow_hook_binding_revisions`` holds every
saved revision, never updated, so a job's pin keeps meaning what it meant. A
cleared binding is a revision without a target: the hook is empty again.

The caller owns the transaction. A save locks the binding row, checks the
expected revision and writes the revision and the active pointer together.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from psycopg.rows import dict_row

from core.application.workflows.workflow_invocation_target import (
    HookBindingRevision,
    HookOwner,
)


class HookBindingConflictError(RuntimeError):
    """The binding changed after the caller read the revision it edits."""

    def __init__(self, hook_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"The binding of hook '{hook_id}' is at revision {actual}, not {expected}; "
            "reload it and try again"
        )
        self.hook_id = hook_id
        self.expected = expected
        self.actual = actual


def _mapping(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        value = json.loads(value)
    return {str(key): str(item) for key, item in dict(value or {}).items()}


def _revision(row: Mapping[str, Any] | None) -> HookBindingRevision | None:
    if not row:
        return None
    return HookBindingRevision(
        id=str(row["id"]),
        binding_id=str(row["binding_id"]),
        revision=int(row["revision"]),
        account_id=str(row["account_id"]),
        project_id=str(row["project_id"]),
        target_workflow_id=str(row["target_workflow_id"]) if row["target_workflow_id"] else None,
        input_mapping=_mapping(row["input_mapping_jsonb"]),
        output_mapping=_mapping(row["output_mapping_jsonb"]),
        hook_contract_version=int(row["hook_contract_version"]),
        hook_semantic_digest=str(row["hook_semantic_digest"]),
    )


class WorkflowHookBindingRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def _cursor(self):
        # Callers hand in either the store's adapter or a transaction cursor's raw
        # psycopg connection (``cur.connection``); psycopg's own row factory works
        # on both, while the adapter-only ``cursor_factory`` does not.
        return self.conn.cursor(row_factory=dict_row)

    @staticmethod
    def _scope(owner: HookOwner, hook_id: str) -> tuple[str, str, str, str, str]:
        return (
            owner.account_id,
            owner.project_id,
            owner.source_install_id,
            owner.local_workflow_id,
            hook_id,
        )

    def binding_revision(self, owner: HookOwner, hook_id: str) -> int:
        """The optimistic revision a save must name (0 before the first save)."""
        if not owner.project_id:
            return 0
        with self._cursor() as cur:
            cur.execute(
                """SELECT revision FROM registry_workflow_hook_bindings
                    WHERE account_id = %s AND project_id = %s AND source_install_id = %s
                      AND local_workflow_id = %s AND hook_id = %s""",
                self._scope(owner, hook_id),
            )
            row = cur.fetchone()
        return int(row["revision"]) if row else 0

    def binding(self, owner: HookOwner, hook_id: str) -> HookBindingRevision | None:
        """The active revision of *owner*'s binding, or None (no binding)."""
        return self.active(owner, [hook_id]).get(hook_id)

    def active(
        self, owner: HookOwner, hook_ids: Iterable[str]
    ) -> dict[str, HookBindingRevision | None]:
        """Every requested hook's active revision; None where nothing is bound.

        The binding rows are share-locked until the caller's transaction ends,
        so a job pins one consistent set while a concurrent save waits.
        """
        wanted = list(dict.fromkeys(str(item) for item in hook_ids))
        found: dict[str, HookBindingRevision | None] = dict.fromkeys(wanted)
        if not wanted or not owner.project_id or not owner.account_id:
            # Bindings belong to a project: without one every hook is empty.
            return found
        with self._cursor() as cur:
            cur.execute(
                """SELECT b.hook_id, r.id, r.binding_id, r.revision, r.account_id, r.project_id,
                          r.target_workflow_id, r.input_mapping_jsonb, r.output_mapping_jsonb,
                          r.hook_contract_version, r.hook_semantic_digest
                      FROM registry_workflow_hook_bindings b
                      JOIN registry_workflow_hook_binding_revisions r
                        ON r.id = b.active_revision_id
                     WHERE b.account_id = %s AND b.project_id = %s
                       AND b.source_install_id = %s AND b.local_workflow_id = %s
                       AND b.hook_id = ANY(%s)
                     FOR SHARE OF b""",
                (
                    owner.account_id,
                    owner.project_id,
                    owner.source_install_id,
                    owner.local_workflow_id,
                    wanted,
                ),
            )
            for row in cur.fetchall():
                found[str(row["hook_id"])] = _revision(row)
        return found

    def revision(self, revision_id: str, *, account_id: Any) -> HookBindingRevision | None:
        """One immutable revision, readable only inside its own account."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT r.id, r.binding_id, r.revision, r.account_id, r.project_id,
                          r.target_workflow_id, r.input_mapping_jsonb, r.output_mapping_jsonb,
                          r.hook_contract_version, r.hook_semantic_digest
                      FROM registry_workflow_hook_binding_revisions r
                     WHERE r.id = %s AND r.account_id = %s""",
                (revision_id, account_id),
            )
            return _revision(cur.fetchone())

    def save(
        self,
        owner: HookOwner,
        hook_id: str,
        *,
        expected_revision: int,
        target_workflow_id: str | None,
        input_mapping: Mapping[str, str],
        output_mapping: Mapping[str, str],
        hook_contract_version: int,
        hook_semantic_digest: str,
        created_by: str = "",
        staged_release_id: str | None = None,
    ) -> HookBindingRevision:
        """Write the next revision and make it active, or refuse a stale edit.

        With *staged_release_id* the revision is prepared for that extension
        release instead: it becomes active only when the release activates, in
        the same transaction, and the current binding keeps working until then.
        """
        scope = self._scope(owner, hook_id)
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO registry_workflow_hook_bindings
                       (account_id, project_id, source_install_id, local_workflow_id, hook_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (account_id, project_id, source_install_id, local_workflow_id,
                                hook_id) DO NOTHING""",
                scope,
            )
            cur.execute(
                """SELECT id, revision FROM registry_workflow_hook_bindings
                    WHERE account_id = %s AND project_id = %s AND source_install_id = %s
                      AND local_workflow_id = %s AND hook_id = %s
                    FOR UPDATE""",
                scope,
            )
            binding = cur.fetchone()
            current = int(binding["revision"])
            if current != int(expected_revision):
                raise HookBindingConflictError(hook_id, int(expected_revision), current)
            cleared = target_workflow_id is None
            cur.execute(
                """INSERT INTO registry_workflow_hook_binding_revisions AS r
                       (binding_id, revision, account_id, project_id, target_workflow_id,
                        input_mapping_jsonb, output_mapping_jsonb, hook_contract_version,
                        hook_semantic_digest, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                   RETURNING r.id, r.binding_id, r.revision, r.account_id, r.project_id,
                          r.target_workflow_id, r.input_mapping_jsonb, r.output_mapping_jsonb,
                          r.hook_contract_version, r.hook_semantic_digest""",
                (
                    binding["id"],
                    current + 1,
                    owner.account_id,
                    owner.project_id,
                    target_workflow_id,
                    json.dumps({} if cleared else dict(input_mapping)),
                    json.dumps({} if cleared else dict(output_mapping)),
                    int(hook_contract_version),
                    hook_semantic_digest,
                    created_by,
                ),
            )
            saved = _revision(cur.fetchone())
            assert saved is not None
            if staged_release_id:
                cur.execute(
                    """UPDATE registry_workflow_hook_bindings
                          SET revision = %s, staged_revision_id = %s, staged_release_id = %s,
                              updated_at = now()
                        WHERE id = %s""",
                    (current + 1, saved.id, staged_release_id, binding["id"]),
                )
            else:
                cur.execute(
                    """UPDATE registry_workflow_hook_bindings
                          SET revision = %s, active_revision_id = %s, updated_at = now()
                        WHERE id = %s""",
                    (current + 1, saved.id, binding["id"]),
                )
        return saved

    def install_bindings(self, *, account_id: Any, install_id: str) -> list[dict[str, Any]]:
        """Every binding of one extension install, with its active and staged contracts."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT b.id, b.project_id::text AS project_id, b.local_workflow_id, b.hook_id,
                          b.staged_release_id,
                          a.target_workflow_id AS active_target,
                          a.hook_contract_version AS active_version,
                          a.hook_semantic_digest AS active_digest,
                          s.target_workflow_id AS staged_target,
                          s.hook_semantic_digest AS staged_digest
                     FROM registry_workflow_hook_bindings b
                     LEFT JOIN registry_workflow_hook_binding_revisions a
                       ON a.id = b.active_revision_id
                     LEFT JOIN registry_workflow_hook_binding_revisions s
                       ON s.id = b.staged_revision_id
                    WHERE b.account_id = %s AND b.source_install_id = %s
                    ORDER BY b.project_id, b.local_workflow_id, b.hook_id
                    FOR UPDATE OF b""",
                (account_id, install_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def activate_staged(self, *, account_id: Any, install_id: str, release_id: str) -> int:
        """Make every binding staged for *release_id* active; return how many moved."""
        with self._cursor() as cur:
            cur.execute(
                """UPDATE registry_workflow_hook_bindings
                      SET active_revision_id = staged_revision_id,
                          staged_revision_id = NULL, staged_release_id = NULL,
                          updated_at = now()
                    WHERE account_id = %s AND source_install_id = %s
                      AND staged_release_id = %s""",
                (account_id, install_id, release_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def bound_targets(self, *, account_id: Any, target_workflow_id: str) -> list[dict[str, Any]]:
        """Active bindings that call *target_workflow_id* (where-used of a child)."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT b.project_id, b.source_install_id, b.local_workflow_id, b.hook_id,
                          b.revision
                     FROM registry_workflow_hook_bindings b
                     JOIN registry_workflow_hook_binding_revisions r
                       ON r.id = b.active_revision_id
                    WHERE b.account_id = %s AND r.target_workflow_id = %s
                    ORDER BY b.project_id, b.local_workflow_id, b.hook_id""",
                (account_id, target_workflow_id),
            )
            return [dict(row) for row in cur.fetchall()]


__all__ = ["HookBindingConflictError", "WorkflowHookBindingRepository"]
