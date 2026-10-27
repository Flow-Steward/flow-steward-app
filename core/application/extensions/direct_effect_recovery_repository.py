"""Scoped ledger reads for direct actions; the existing resolver owns writes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core.infrastructure.db.external_effect_records import _row_to_dict


class DirectEffectRecoveryRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def lock_intent(self, identity: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (identity,))

    def candidates(
        self,
        *,
        account_id: str,
        project_id: str | None,
        install_id: str,
        operation_id: str | None = None,
        connection_id: str | None = None,
        match_connection: bool = False,
        after: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.* FROM external_effects e
                 WHERE e.account_id = %s::uuid AND e.project_id IS NOT DISTINCT FROM %s::uuid
                   AND e.extension_install_id = %s AND e.source_type = 'api_action'
                   AND (e.status = 'timeout_unknown' OR (
                       e.status IN ('claimed', 'running') AND EXISTS (
                           SELECT 1 FROM command_audit_log a
                            WHERE a.account_id = e.account_id
                              AND a.command_type = 'external_effect.resolution.consume'
                              AND a.status = 'executed'
                              AND a.target_jsonb->>'effect_id' = e.effect_id::text
                              AND a.result_jsonb->>'claim_version' = e.claim_version::text
                       )
                   ))
                   AND (%s::text IS NULL OR e.operation_id = %s)
                   AND (NOT %s::boolean OR e.connection_id IS NOT DISTINCT FROM %s)
                   AND (%s::uuid IS NULL OR e.effect_id > %s::uuid)
                 ORDER BY e.effect_id LIMIT %s
                """,
                (
                    account_id,
                    project_id,
                    install_id,
                    operation_id,
                    operation_id,
                    match_connection,
                    connection_id,
                    after,
                    after,
                    limit,
                ),
            )
            return [_row_to_dict(row, cur.description) for row in cur.fetchall()]

    def effect(
        self, *, account_id: str, project_id: str, install_id: str, effect_id: str
    ) -> dict[str, Any] | None:
        try:
            effect_id = str(UUID(effect_id))
        except (ValueError, TypeError, AttributeError):
            return None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM external_effects WHERE account_id = %s::uuid
                  AND project_id = %s::uuid AND extension_install_id = %s
                  AND effect_id = %s::uuid AND source_type = 'api_action'
                """,
                (account_id, project_id, install_id, effect_id),
            )
            row = cur.fetchone()
            return _row_to_dict(row, cur.description) if row else None

    def resolution(self, effect: dict[str, Any]) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_jsonb, result_jsonb FROM command_audit_log
                 WHERE account_id = %s::uuid AND command_type = 'external_effect.resolution'
                   AND status = 'executed' AND target_jsonb->>'effect_id' = %s
                   AND payload_jsonb->>'action' = 'reject'
                   AND payload_jsonb->>'approval_id' = ''
                 ORDER BY created_at DESC LIMIT 1
                """,
                (str(effect["account_id"]), str(effect["effect_id"])),
            )
            row = cur.fetchone()
            return _row_to_dict(row, cur.description) if row else None
