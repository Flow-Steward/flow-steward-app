"""Domain persistence for operator effect decisions and their one-use receipts."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db.external_effect_records import _row_to_dict


class ExternalEffectResolutionRepository:
    """Take only effect locks; callers that also own a job lock acquire it first."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def lock_effect(
        self, *, account_id: str, project_id: str | None, effect_id: str
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM external_effects
                 WHERE effect_id = %s::uuid AND account_id = %s::uuid
                   AND project_id IS NOT DISTINCT FROM %s::uuid
                 FOR UPDATE
                """,
                (effect_id, account_id, project_id),
            )
            row = cur.fetchone()
            return _row_to_dict(row, cur.description) if row else None

    def receipt(self, *, account_id: str, command_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT command_type, payload_jsonb, result_jsonb
                  FROM command_audit_log
                 WHERE command_id = %s AND account_id = %s::uuid AND status = 'executed'
                """,
                (command_id, account_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            decoded = _row_to_dict(row, cur.description)
            return {
                "command_type": decoded["command_type"],
                "payload": decoded["payload_jsonb"],
                "result": decoded["result_jsonb"],
            }

    def has_pending_outputs(self, effect: dict[str, Any]) -> bool:
        """Unresolved grants cannot be certified by response ciphertext alone."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM external_effect_output_reconciliations
                 WHERE effect_id = %s::uuid AND account_id = %s::uuid
                 LIMIT 1
                """,
                (str(effect["effect_id"]), str(effect["account_id"])),
            )
            return cur.fetchone() is not None

    def insert_receipt(
        self,
        *,
        account_id: str,
        command_id: str,
        command_type: str,
        actor_type: str,
        actor_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        # Unique(command_id) is the durable domain receipt. Never use the generic
        # UPDATE-by-command-id audit helper as an authorization compare-and-swap.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO command_audit_log (
                    command_id, command_type, account_id, actor_type, actor_id,
                    target_jsonb, payload_jsonb, status, result_jsonb
                ) VALUES (%s, %s, %s::uuid, %s, %s, %s::jsonb, %s::jsonb, 'executed', %s::jsonb)
                """,
                (
                    command_id,
                    command_type,
                    account_id,
                    actor_type,
                    actor_id,
                    json.dumps(target),
                    json.dumps(payload),
                    json.dumps(result),
                ),
            )

    def fence(self, effect: dict[str, Any], *, action: str) -> dict[str, Any]:
        status, reason = {
            "approve": ("succeeded", "operator_confirmed_success"),
            "reject": ("timeout_unknown", "operator_confirmed_no_effect"),
            "retry_authorized": ("timeout_unknown", "approval_retry_authorized"),
        }[action]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE external_effects
                   SET status = %s, status_reason = %s,
                       claim_version = claim_version + 1,
                       claim_token = gen_random_uuid()::text,
                       updated_at = now()
                 WHERE effect_id = %s::uuid AND account_id = %s::uuid
                   AND claim_version = %s AND status = 'timeout_unknown'
                 RETURNING *
                """,
                (
                    status,
                    reason,
                    str(effect["effect_id"]),
                    str(effect["account_id"]),
                    effect["claim_version"],
                ),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("external_effect_resolution_fence_lost")
            return _row_to_dict(row, cur.description)

    def tombstone_replay(self, effect: dict[str, Any], *, binding_hash: str, reason: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_effect_replays (
                    effect_id, account_id, replay_state, replay_reason,
                    encrypted_payload, replay_binding_hash, plaintext_bytes
                ) VALUES (%s::uuid, %s::uuid, 'nonreplayable', %s, NULL, %s, 0)
                ON CONFLICT (effect_id) DO UPDATE
                   SET replay_state = 'nonreplayable', replay_reason = EXCLUDED.replay_reason,
                       encrypted_payload = NULL, plaintext_bytes = 0,
                       replay_binding_hash = EXCLUDED.replay_binding_hash, updated_at = now()
                """,
                (str(effect["effect_id"]), str(effect["account_id"]), reason, binding_hash),
            )

    def claim_retry(self, effect: dict[str, Any], *, expires_at: str | None) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE external_effects
                   SET status = 'claimed', status_reason = 'operator_retry_claimed',
                       claim_version = claim_version + 1,
                       claim_token = gen_random_uuid()::text,
                       provider_message_id = NULL, external_object_id = NULL,
                       error_code = NULL, error_text = NULL, response_summary_jsonb = '{}'::jsonb,
                       claimed_at = now(), started_at = NULL, completed_at = NULL,
                       expires_at = %s, updated_at = now()
                 WHERE effect_id = %s::uuid AND account_id = %s::uuid
                   AND claim_version = %s AND status = 'timeout_unknown'
                   AND status_reason IN ('operator_confirmed_no_effect', 'approval_retry_authorized')
                 RETURNING *
                """,
                (
                    expires_at,
                    str(effect["effect_id"]),
                    str(effect["account_id"]),
                    effect["claim_version"],
                ),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("external_effect_resolution_claim_lost")
            return _row_to_dict(row, cur.description)
