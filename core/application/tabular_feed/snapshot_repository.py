"""Database authority for tabular snapshot heads and durable candidate receipts.

The caller owns the transaction and validates immutable candidate access before
entering it. No artifact/network I/O belongs under the scope row lock. A caller
with an ambiguous commit outcome retries the same candidate to recover its
original receipt; a receipt does not imply that candidate is still the head.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from psycopg.rows import dict_row

from core.application.tabular_feed.snapshot_models import (
    SnapshotCommitReceipt,
    SnapshotHead,
    SnapshotRef,
    SnapshotScope,
)


class SnapshotConflictError(RuntimeError):
    code = "snapshot_conflict"

    def __init__(self, expected: SnapshotRef, actual: SnapshotRef) -> None:
        super().__init__("Snapshot baseline changed; rebuild the candidate from the current head")
        self.expected = expected
        self.actual = actual


def _head(row: Mapping[str, Any] | None) -> SnapshotHead:
    if row is None:
        return SnapshotHead(SnapshotRef(0))
    return SnapshotHead(
        ref=SnapshotRef(int(row["revision"]), str(row["snapshot_artifact_id"])),
        metadata=dict(row["metadata_jsonb"]),
    )


def _receipt(row: Mapping[str, Any]) -> SnapshotCommitReceipt:
    return SnapshotCommitReceipt(
        ref=SnapshotRef(int(row["revision"]), str(row["candidate_artifact_id"])),
        previous_snapshot_artifact_id=str(row["previous_snapshot_artifact_id"]),
        metadata=dict(row["metadata_jsonb"]),
    )


class SnapshotRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def read_current(self, scope: SnapshotScope) -> SnapshotHead:
        """Read the reference and checksum metadata together from one DB row."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT revision, snapshot_artifact_id, metadata_jsonb
                     FROM public.fs_tabular_snapshot_heads
                    WHERE account_id = %s AND project_id = %s
                      AND workflow_id = %s AND profile_key = %s""",
                scope.as_tuple(),
            )
            return _head(cur.fetchone())

    def read_head(self, scope: SnapshotScope) -> SnapshotRef:
        return self.read_current(scope).ref

    def read_metadata(self, scope: SnapshotScope) -> dict[str, Any]:
        return self.read_current(scope).metadata

    def read_commit(
        self, scope: SnapshotScope, candidate_artifact_id: str
    ) -> SnapshotCommitReceipt | None:
        """Read the immutable receipt for this exact baseline, independently of the head."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT revision, candidate_artifact_id,
                          previous_snapshot_artifact_id, metadata_jsonb
                     FROM public.fs_tabular_snapshot_commits
                    WHERE account_id = %s AND project_id = %s
                      AND workflow_id = %s AND profile_key = %s
                      AND candidate_artifact_id = %s""",
                (*scope.as_tuple(), candidate_artifact_id),
            )
            row = cur.fetchone()
            return _receipt(row) if row is not None else None

    def commit(
        self,
        scope: SnapshotScope,
        *,
        candidate_artifact_id: str,
        expected_ref: SnapshotRef,
        metadata: Mapping[str, Any],
    ) -> SnapshotCommitReceipt:
        """Advance once, replay a durable result, or raise a typed conflict.

        Both head and receipt writes are part of the caller's transaction. A
        database error must propagate so that transaction can be rolled back.
        All writers lock the same head before looking up a candidate receipt.
        """
        if self.conn.autocommit:
            raise ValueError("snapshot_transaction_required")
        if not isinstance(candidate_artifact_id, str) or not candidate_artifact_id.strip():
            raise ValueError("snapshot_candidate_required")
        candidate_artifact_id = candidate_artifact_id.strip()
        encoded_metadata = json.dumps(dict(metadata))
        owner = scope.as_tuple()
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO public.fs_tabular_snapshot_heads
                           (account_id, project_id, workflow_id, profile_key)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (account_id, project_id, workflow_id, profile_key)
                       DO NOTHING""",
                owner,
            )
            cur.execute(
                """SELECT revision, snapshot_artifact_id, metadata_jsonb
                     FROM public.fs_tabular_snapshot_heads
                    WHERE account_id = %s AND project_id = %s
                      AND workflow_id = %s AND profile_key = %s FOR UPDATE""",
                owner,
            )
            current = _head(cur.fetchone())
            cur.execute(
                """SELECT revision, candidate_artifact_id,
                          previous_snapshot_artifact_id, metadata_jsonb
                     FROM public.fs_tabular_snapshot_commits
                    WHERE account_id = %s AND project_id = %s
                      AND workflow_id = %s AND profile_key = %s
                      AND candidate_artifact_id = %s""",
                (*owner, candidate_artifact_id),
            )
            previous_receipt = cur.fetchone()
            if previous_receipt is not None:
                return _receipt(previous_receipt)
            if current.ref != expected_ref:
                raise SnapshotConflictError(expected_ref, current.ref)
            revision = current.ref.revision + 1
            cur.execute(
                """UPDATE public.fs_tabular_snapshot_heads
                      SET revision = %s, snapshot_artifact_id = %s,
                          metadata_jsonb = %s::jsonb, updated_at = now()
                    WHERE account_id = %s AND project_id = %s
                      AND workflow_id = %s AND profile_key = %s""",
                (revision, candidate_artifact_id, encoded_metadata, *owner),
            )
            cur.execute(
                """INSERT INTO public.fs_tabular_snapshot_commits
                           (account_id, project_id, workflow_id, profile_key,
                            candidate_artifact_id, revision,
                            previous_snapshot_artifact_id, metadata_jsonb)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING revision, candidate_artifact_id,
                              previous_snapshot_artifact_id, metadata_jsonb""",
                (
                    *owner,
                    candidate_artifact_id,
                    revision,
                    current.ref.snapshot_artifact_id,
                    encoded_metadata,
                ),
            )
            return _receipt(cur.fetchone())
