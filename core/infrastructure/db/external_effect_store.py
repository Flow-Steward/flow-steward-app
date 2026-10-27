"""Persistence helpers for generic extension external effects."""

from __future__ import annotations

from datetime import UTC, datetime
from math import floor
from typing import Any

from core.infrastructure.db.db_pool import (
    active_transaction_connection,
    managed_transaction_connection,
    register_transaction_after_commit,
    transaction_connection,
)
from core.infrastructure.db.external_effect_records import _jsonb, _row_to_dict, _uuid_text


class ExternalEffectReplayLeaseLostError(RuntimeError):
    code = "external_effect_replay_lease_lost"


class ExternalEffectReplayCommitError(RuntimeError):
    code = "external_effect_replay_persist_failed"

    def __init__(self, *, commit_outcome: str) -> None:
        normalized = str(commit_outcome or "").strip().lower()
        if normalized not in {"rolled_back", "committed", "unknown"}:
            raise ValueError("invalid external-effect replay commit outcome")
        self.commit_outcome = normalized
        super().__init__(self.code)


def claim_external_effect(
    db: Any,
    *,
    account_id: str,
    project_id: str | None,
    extension_id: str,
    extension_install_id: str | None,
    connection_id: str | None,
    operation_id: str,
    effect_kind: str,
    channel: str,
    source_type: str,
    source_ref_jsonb: dict[str, Any],
    idempotency_key: str,
    idempotency_scope_hash: str,
    request_fingerprint: str,
    target_count: int = 0,
    artifact_count: int = 0,
    artifact_total_bytes: int = 0,
    estimated_payload_bytes: int = 0,
    target_summary_redacted: str = "",
    target_hashes_jsonb: list[dict[str, Any]] | list[str] | None = None,
    artifact_refs_jsonb: list[dict[str, Any]] | None = None,
    test_mode: bool = False,
    expires_at: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Claim or re-claim one external effect.

    Returns ``(row, should_spawn)``. ``should_spawn`` is true only when this call
    inserted a new row or atomically moved a retryable existing row back to
    ``claimed``.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            WITH claimed_row AS (
                INSERT INTO external_effects (
                    account_id,
                    project_id,
                    extension_id,
                    extension_install_id,
                    connection_id,
                    operation_id,
                    effect_kind,
                    channel,
                    source_type,
                    source_ref_jsonb,
                    idempotency_key,
                    idempotency_scope_hash,
                    request_fingerprint,
                    target_count,
                    artifact_count,
                    artifact_total_bytes,
                    estimated_payload_bytes,
                    target_summary_redacted,
                    target_hashes_jsonb,
                    artifact_refs_jsonb,
                    status,
                    status_reason,
                    test_mode,
                    claim_version,
                    claim_token,
                    claimed_at,
                    started_at,
                    completed_at,
                    expires_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,
                    'claimed','claimed',%s,1,gen_random_uuid()::text,now(),NULL,NULL,%s
                )
                ON CONFLICT (account_id, idempotency_scope_hash, idempotency_key)
                DO UPDATE SET
                    project_id = EXCLUDED.project_id,
                    extension_id = EXCLUDED.extension_id,
                    extension_install_id = EXCLUDED.extension_install_id,
                    connection_id = EXCLUDED.connection_id,
                    operation_id = EXCLUDED.operation_id,
                    effect_kind = EXCLUDED.effect_kind,
                    channel = EXCLUDED.channel,
                    source_type = EXCLUDED.source_type,
                    source_ref_jsonb = EXCLUDED.source_ref_jsonb,
                    request_fingerprint = EXCLUDED.request_fingerprint,
                    target_count = EXCLUDED.target_count,
                    artifact_count = EXCLUDED.artifact_count,
                    artifact_total_bytes = EXCLUDED.artifact_total_bytes,
                    estimated_payload_bytes = EXCLUDED.estimated_payload_bytes,
                    target_summary_redacted = EXCLUDED.target_summary_redacted,
                    target_hashes_jsonb = EXCLUDED.target_hashes_jsonb,
                    artifact_refs_jsonb = EXCLUDED.artifact_refs_jsonb,
                    status = 'claimed',
                    status_reason = 'reclaimed',
                    claim_version = external_effects.claim_version + 1,
                    claim_token = gen_random_uuid()::text,
                    provider_message_id = NULL,
                    external_object_id = NULL,
                    error_code = NULL,
                    error_text = NULL,
                    response_summary_jsonb = '{}'::jsonb,
                    test_mode = EXCLUDED.test_mode,
                    claimed_at = now(),
                    started_at = NULL,
                    completed_at = NULL,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                WHERE external_effects.status IN ('failed', 'expired')
                  AND external_effects.request_fingerprint = EXCLUDED.request_fingerprint
                RETURNING external_effects.*, true AS should_spawn
            )
            SELECT * FROM claimed_row
            UNION ALL
            SELECT external_effects.*, false AS should_spawn
              FROM external_effects
             WHERE account_id = %s
               AND idempotency_scope_hash = %s
               AND idempotency_key = %s
               AND NOT EXISTS (SELECT 1 FROM claimed_row)
             LIMIT 1
            """,
            (
                str(account_id),
                str(project_id).strip() if project_id else None,
                str(extension_id),
                str(extension_install_id).strip() if extension_install_id else None,
                str(connection_id).strip() if connection_id else None,
                str(operation_id),
                str(effect_kind),
                str(channel),
                str(source_type),
                _jsonb(source_ref_jsonb, default={}),
                str(idempotency_key),
                str(idempotency_scope_hash),
                str(request_fingerprint),
                max(0, int(target_count or 0)),
                max(0, int(artifact_count or 0)),
                max(0, int(artifact_total_bytes or 0)),
                max(0, int(estimated_payload_bytes or 0)),
                str(target_summary_redacted or "")[:1000],
                _jsonb(target_hashes_jsonb or [], default=[]),
                _jsonb(artifact_refs_jsonb or [], default=[]),
                bool(test_mode),
                str(expires_at).strip() if expires_at else None,
                str(account_id),
                str(idempotency_scope_hash),
                str(idempotency_key),
            ),
        )
        row = cur.fetchone()
        payload = _row_to_dict(row, cur.description) if row else {}
    should_spawn = bool(payload.pop("should_spawn", False))
    return payload, should_spawn


def consume_external_effect_rate_limits(
    db: Any,
    *,
    scopes: list[dict[str, Any]],
    window_seconds: int,
) -> dict[str, Any]:
    """Atomically consume every applicable external-effect rate bucket.

    Each scope has a transaction advisory lock before its counter is examined.
    The ordered locks make the account, project, and connection limits behave as
    one decision without over-consuming broader scopes when a narrower one is
    already full.
    """
    normalized_window_seconds = max(1, int(window_seconds or 1))
    normalized_scopes: list[dict[str, Any]] = [
        {
            "scope_hash": str(scope.get("scope_hash") or "").strip(),
            "scope_type": str(scope.get("scope_type") or "").strip(),
            "account_id": str(scope.get("account_id") or "").strip(),
            "project_id": str(scope.get("project_id") or "").strip() or None,
            "connection_id": str(scope.get("connection_id") or "").strip() or None,
            "effect_kind": str(scope.get("effect_kind") or "").strip(),
            "channel": str(scope.get("channel") or "").strip(),
            "max_effects": max(1, int(scope.get("max_effects") or 1)),
        }
        for scope in scopes
        if str(scope.get("scope_hash") or "").strip()
    ]
    if not normalized_scopes:
        return {"allowed": True, "retry_after_seconds": 0}

    now = datetime.now(UTC)
    bucket_epoch = floor(now.timestamp() / normalized_window_seconds) * normalized_window_seconds
    bucket_started_at = datetime.fromtimestamp(bucket_epoch, tz=UTC)
    retry_after_seconds = max(
        1,
        int(bucket_epoch + normalized_window_seconds - now.timestamp()),
    )
    normalized_scopes.sort(key=lambda scope: scope["scope_hash"])
    scope_hashes = [scope["scope_hash"] for scope in normalized_scopes]

    with transaction_connection(db) as conn, conn.cursor() as cur:
        for scope in normalized_scopes:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (scope["scope_hash"],),
            )
        cur.execute(
            """
            SELECT scope_hash, effect_count
              FROM external_effect_rate_windows
             WHERE window_started_at = %s
               AND scope_hash = ANY(%s)
             FOR UPDATE
            """,
            (bucket_started_at, scope_hashes),
        )
        existing_rows = cur.fetchall() or []
        existing_counts: dict[str, int] = {}
        for row in existing_rows:
            if isinstance(row, dict):
                scope_hash = str(row.get("scope_hash") or "").strip()
                count = row.get("effect_count")
            else:
                scope_hash = str(row[0] if row else "").strip()
                count = row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else 0
            try:
                existing_counts[scope_hash] = max(0, int(count or 0))
            except (TypeError, ValueError):
                existing_counts[scope_hash] = 0
        for scope in normalized_scopes:
            if existing_counts.get(scope["scope_hash"], 0) >= scope["max_effects"]:
                return {
                    "allowed": False,
                    "scope_type": scope["scope_type"],
                    "retry_after_seconds": retry_after_seconds,
                }
        for scope in normalized_scopes:
            cur.execute(
                """
                INSERT INTO external_effect_rate_windows (
                    scope_hash,
                    scope_type,
                    account_id,
                    project_id,
                    connection_id,
                    effect_kind,
                    channel,
                    window_started_at,
                    effect_count
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON CONFLICT (scope_hash, window_started_at)
                DO UPDATE SET effect_count = external_effect_rate_windows.effect_count + 1,
                              updated_at = now()
                """,
                (
                    scope["scope_hash"],
                    scope["scope_type"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["connection_id"],
                    scope["effect_kind"],
                    scope["channel"],
                    bucket_started_at,
                ),
            )
    return {"allowed": True, "retry_after_seconds": 0}


def get_external_effect(db: Any, *, effect_id: str) -> dict[str, Any] | None:
    normalized_effect_id = _uuid_text(effect_id)
    if not normalized_effect_id:
        return None
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT *
              FROM external_effects
             WHERE effect_id = %s::uuid
             LIMIT 1
            """,
            (normalized_effect_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def get_external_effect_replay(
    db: Any,
    *,
    effect_id: str,
    account_id: str,
) -> dict[str, Any] | None:
    """Read replay metadata explicitly; ciphertext never joins the public effect row."""
    normalized_effect_id = _uuid_text(effect_id)
    normalized_account_id = _uuid_text(account_id)
    if not normalized_effect_id or not normalized_account_id:
        return None
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT effect_id,
                   account_id,
                   replay_state,
                   replay_reason,
                   encrypted_payload,
                   replay_binding_hash,
                   plaintext_bytes,
                   created_at,
                   updated_at
              FROM external_effect_replays
             WHERE effect_id = %s::uuid
               AND account_id = %s::uuid
             LIMIT 1
            """,
            (normalized_effect_id, normalized_account_id),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def get_exact_committed_external_effect_replay(
    db: Any,
    *,
    effect: dict[str, Any],
    replay_ciphertext: str,
    replay_binding_hash: str,
    replay_plaintext_bytes: int,
) -> dict[str, Any] | None:
    """Primary, tenant-fenced proof that the exact attempted replay committed."""
    effect_id = _uuid_text(effect.get("effect_id"))
    account_id = _uuid_text(effect.get("account_id"))
    if not effect_id or not account_id:
        return None
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT effect.*
              FROM external_effects AS effect
              JOIN external_effect_replays AS replay
                ON replay.effect_id = effect.effect_id
               AND replay.account_id = effect.account_id
             WHERE effect.effect_id = %s::uuid
               AND effect.account_id = %s::uuid
               AND effect.status = 'succeeded'
               AND effect.claim_token = %s
               AND effect.claim_version = %s
               AND effect.project_id IS NOT DISTINCT FROM %s::uuid
               AND effect.extension_id = %s
               AND effect.extension_install_id IS NOT DISTINCT FROM %s
               AND effect.connection_id IS NOT DISTINCT FROM %s
               AND effect.operation_id = %s
               AND effect.idempotency_scope_hash = %s
               AND effect.idempotency_key = %s
               AND effect.request_fingerprint = %s
               AND replay.replay_state = 'available'
               AND replay.encrypted_payload = %s
               AND replay.replay_binding_hash = %s
               AND replay.plaintext_bytes = %s
             LIMIT 1
            """,
            (
                effect_id,
                account_id,
                str(effect.get("claim_token") or "").strip(),
                max(1, int(effect.get("claim_version") or 1)),
                _uuid_text(effect.get("project_id")),
                str(effect.get("extension_id") or "").strip(),
                str(effect.get("extension_install_id") or "").strip() or None,
                str(effect.get("connection_id") or "").strip() or None,
                str(effect.get("operation_id") or "").strip(),
                str(effect.get("idempotency_scope_hash") or "").strip(),
                str(effect.get("idempotency_key") or "").strip(),
                str(effect.get("request_fingerprint") or "").strip(),
                str(replay_ciphertext or "").strip(),
                str(replay_binding_hash or "").strip(),
                max(0, int(replay_plaintext_bytes or 0)),
            ),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def register_external_effect_output_reconciliation(
    db: Any,
    *,
    effect_id: str,
    account_id: str,
    claim_token: str,
    minted_artifact_ids: list[str],
    minted_dataset_ids: list[str],
) -> None:
    artifact_ids = [str(value).strip() for value in minted_artifact_ids if str(value).strip()]
    dataset_ids = [str(value).strip() for value in minted_dataset_ids if str(value).strip()]
    if not artifact_ids and not dataset_ids:
        return
    if (
        len(artifact_ids) > 128
        or len(dataset_ids) > 128
        or any(len(value) > 255 for value in (*artifact_ids, *dataset_ids))
    ):
        raise ValueError("external effect output reconciliation IDs exceed bounds")
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO external_effect_output_reconciliations (
                effect_id,
                account_id,
                claim_token,
                claim_version,
                minted_artifact_ids_jsonb,
                minted_dataset_ids_jsonb,
                lifecycle_state
            )
            SELECT effect_id, account_id, claim_token, claim_version, %s::jsonb, %s::jsonb,
                   'pending'
              FROM external_effects
             WHERE effect_id = %s::uuid
               AND account_id = %s::uuid
               AND claim_token = %s
               AND status = 'claimed'
            ON CONFLICT (effect_id, account_id, claim_version)
            DO UPDATE SET claim_token = EXCLUDED.claim_token,
                          minted_artifact_ids_jsonb = EXCLUDED.minted_artifact_ids_jsonb,
                          minted_dataset_ids_jsonb = EXCLUDED.minted_dataset_ids_jsonb,
                          lifecycle_state = 'pending',
                          expires_at = now() + interval '24 hours',
                          updated_at = now()
                  WHERE external_effect_output_reconciliations.claim_token = EXCLUDED.claim_token
            RETURNING effect_id
            """,
            (
                _jsonb(artifact_ids, default=[]),
                _jsonb(dataset_ids, default=[]),
                _uuid_text(effect_id),
                _uuid_text(account_id),
                str(claim_token or "").strip(),
            ),
        )
        if not cur.fetchone():
            raise ExternalEffectReplayLeaseLostError(
                "external effect output reconciliation lease is stale or missing"
            )


def list_external_effect_output_reconciliations_for_cleanup(
    db: Any,
    *,
    account_id: str | None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    normalized_account_id = _uuid_text(account_id)
    account_predicate = "AND reconciliation.account_id = %s::uuid" if normalized_account_id else ""
    params: tuple[Any, ...] = (
        (normalized_account_id, max(1, min(int(limit or 100), 1000)))
        if normalized_account_id
        else (max(1, min(int(limit or 100), 1000)),)
    )
    with db.cursor() as cur:
        cur.execute(
            f"""
            WITH selected AS (
                SELECT reconciliation.effect_id,
                       reconciliation.account_id,
                       reconciliation.claim_version
                  FROM external_effect_output_reconciliations AS reconciliation
                  JOIN external_effects AS effect
                    ON effect.effect_id = reconciliation.effect_id
                   AND effect.account_id = reconciliation.account_id
                  LEFT JOIN external_effect_replays AS replay
                    ON replay.effect_id = reconciliation.effect_id
                   AND replay.account_id = reconciliation.account_id
                 WHERE 1 = 1
                   {account_predicate}
                   AND (
                        reconciliation.claim_version < effect.claim_version
                        OR (
                            effect.status IN ('claimed', 'running')
                            AND effect.expires_at IS NOT NULL
                            AND effect.expires_at < now()
                        )
                        OR (
                            effect.status IN (
                                'failed', 'timeout_unknown', 'suppressed', 'expired'
                            )
                            AND replay.effect_id IS NULL
                            AND (
                                effect.status = 'expired'
                                OR reconciliation.expires_at <= now()
                            )
                        )
                   )
                 ORDER BY reconciliation.updated_at, reconciliation.created_at
                 FOR UPDATE OF reconciliation SKIP LOCKED
                 LIMIT %s
            )
            UPDATE external_effect_output_reconciliations AS reconciliation
               SET updated_at = now()
              FROM selected
             WHERE reconciliation.effect_id = selected.effect_id
               AND reconciliation.account_id = selected.account_id
               AND reconciliation.claim_version = selected.claim_version
            RETURNING reconciliation.*
            """,  # nosec B608 -- account predicate is a fixed internal constant.
            params,
        )
        return [_row_to_dict(row, cur.description) for row in (cur.fetchall() or [])]


def list_external_effect_maintenance_accounts(
    db: Any,
    *,
    limit: int = 100,
) -> list[str]:
    with db.cursor() as cur:
        cur.execute(
            """
            WITH maintenance_candidates AS (
                SELECT effect.account_id, min(effect.expires_at) AS candidate_at
                  FROM external_effects AS effect
                 WHERE effect.status IN ('claimed', 'running')
                   AND effect.expires_at IS NOT NULL
                   AND effect.expires_at < now()
                 GROUP BY effect.account_id
                UNION ALL
                SELECT rate_window.account_id, min(rate_window.window_started_at)
                  FROM external_effect_rate_windows AS rate_window
                 WHERE rate_window.window_started_at < now() - interval '24 hours'
                 GROUP BY rate_window.account_id
                UNION ALL
                SELECT replay.account_id, min(replay.updated_at)
                  FROM external_effect_replays AS replay
                  LEFT JOIN system_settings AS setting
                    ON setting.account_id = replay.account_id
                   AND setting.setting_key = 'retention_policy'
                 WHERE replay.replay_state = 'available'
                   AND replay.updated_at < now() - make_interval(
                        days => COALESCE(
                            NULLIF(setting.setting_value_jsonb->>'action_logs_days', '')::integer,
                            30
                        )
                   )
                 GROUP BY replay.account_id
                UNION ALL
                SELECT reconciliation.account_id, min(reconciliation.updated_at)
                  FROM external_effect_output_reconciliations AS reconciliation
                  JOIN external_effects AS effect
                    ON effect.effect_id = reconciliation.effect_id
                   AND effect.account_id = reconciliation.account_id
                  LEFT JOIN external_effect_replays AS replay
                    ON replay.effect_id = reconciliation.effect_id
                   AND replay.account_id = reconciliation.account_id
                 WHERE reconciliation.claim_version < effect.claim_version
                    OR (
                        effect.status IN ('failed', 'timeout_unknown', 'suppressed', 'expired')
                        AND replay.effect_id IS NULL
                        AND (effect.status = 'expired' OR reconciliation.expires_at <= now())
                    )
                 GROUP BY reconciliation.account_id
            )
            , ranked AS (
                SELECT candidate.account_id,
                       min(candidate.candidate_at) AS candidate_at,
                       cursor.last_attempted_at
                  FROM maintenance_candidates AS candidate
                  LEFT JOIN external_effect_maintenance_cursors AS cursor
                    ON cursor.account_id = candidate.account_id
                 GROUP BY candidate.account_id, cursor.last_attempted_at
                 ORDER BY cursor.last_attempted_at NULLS FIRST,
                          min(candidate.candidate_at),
                          candidate.account_id
                 LIMIT %s
            )
            INSERT INTO external_effect_maintenance_cursors (account_id, last_attempted_at)
            SELECT ranked.account_id, now()
              FROM ranked
            ON CONFLICT (account_id)
            DO UPDATE SET last_attempted_at = EXCLUDED.last_attempted_at
            RETURNING account_id
            """,
            (max(1, min(int(limit or 100), 1000)),),
        )
        return [
            str(row.get("account_id") if isinstance(row, dict) else row[0]).strip()
            for row in (cur.fetchall() or [])
        ]


def resolve_external_effect_output_reconciliation(
    db: Any,
    *,
    effect_id: str,
    account_id: str,
    claim_token: str,
    claim_version: int,
) -> bool:
    with db.cursor() as cur:
        cur.execute(
            """
            DELETE FROM external_effect_output_reconciliations
             WHERE effect_id = %s::uuid
               AND account_id = %s::uuid
               AND claim_token = %s
               AND claim_version = %s
            RETURNING effect_id
            """,
            (
                _uuid_text(effect_id),
                _uuid_text(account_id),
                str(claim_token or "").strip(),
                max(1, int(claim_version or 1)),
            ),
        )
        return bool(cur.fetchone())


def confirm_external_effect_with_replay(
    db: Any,
    *,
    effect_id: str,
    account_id: str,
    claim_token: str,
    status: str,
    status_reason: str,
    provider_message_id: str | None,
    external_object_id: str | None,
    error_code: str | None,
    error_text: str | None,
    response_summary_jsonb: dict[str, Any] | None,
    replay_ciphertext: str,
    replay_binding_hash: str,
    replay_plaintext_bytes: int,
) -> dict[str, Any]:
    """Fence the lease and persist replay plus terminal status in one transaction."""
    normalized_effect_id = _uuid_text(effect_id)
    normalized_account_id = _uuid_text(account_id)
    normalized_claim_token = str(claim_token or "").strip()
    if not normalized_effect_id or not normalized_account_id or not normalized_claim_token:
        raise ExternalEffectReplayLeaseLostError(
            "external effect replay confirmation requires effect, account, and claim token"
        )
    normalized_status = str(status or "").strip().lower()
    if normalized_status != "succeeded":
        raise ValueError("atomic replay confirmation only supports succeeded effects")
    if (
        managed_transaction_connection(db) is not None
        or active_transaction_connection(db) is not None
    ):
        raise ExternalEffectReplayCommitError(commit_outcome="unknown")
    params = (
        normalized_effect_id,
        normalized_account_id,
        normalized_claim_token,
        str(replay_ciphertext or "").strip(),
        str(replay_binding_hash or "").strip(),
        max(0, int(replay_plaintext_bytes or 0)),
        normalized_status,
        str(status_reason or "").strip()[:500],
        str(provider_message_id).strip() if provider_message_id else None,
        str(external_object_id).strip() if external_object_id else None,
        str(error_code).strip()[:200] if error_code else None,
        str(error_text).strip()[:1000] if error_text else None,
        _jsonb(response_summary_jsonb, default={}),
    )
    committed = False
    statement_completed = False
    confirmed: dict[str, Any] = {}

    def mark_committed() -> None:
        nonlocal committed
        committed = True

    try:
        with transaction_connection(db) as conn, conn.cursor() as cur:
            if not register_transaction_after_commit(conn, mark_committed):
                raise ExternalEffectReplayCommitError(commit_outcome="unknown")
            cur.execute(
                """
            WITH locked_effect AS (
                SELECT effect_id, account_id
                  FROM external_effects
                 WHERE effect_id = %s::uuid
                   AND account_id = %s::uuid
                   AND claim_token = %s
                   AND status = 'running'
                 FOR UPDATE
            ), replay_write AS (
                INSERT INTO external_effect_replays (
                    effect_id,
                    account_id,
                    replay_state,
                    replay_reason,
                    encrypted_payload,
                    replay_binding_hash,
                    plaintext_bytes
                )
                SELECT effect_id, account_id, 'available', NULL, %s, %s, %s
                  FROM locked_effect
                ON CONFLICT (effect_id)
                DO UPDATE SET account_id = EXCLUDED.account_id,
                              replay_state = 'available',
                              replay_reason = NULL,
                              encrypted_payload = EXCLUDED.encrypted_payload,
                              replay_binding_hash = EXCLUDED.replay_binding_hash,
                              plaintext_bytes = EXCLUDED.plaintext_bytes,
                              updated_at = now()
                RETURNING effect_id
            ), updated_effect AS (
                UPDATE external_effects AS effect
                   SET status = %s,
                       status_reason = %s,
                       provider_message_id = COALESCE(%s, effect.provider_message_id),
                       external_object_id = COALESCE(%s, effect.external_object_id),
                       error_code = %s,
                       error_text = %s,
                       response_summary_jsonb = %s::jsonb,
                       completed_at = now(),
                       updated_at = now()
                  FROM locked_effect
                 WHERE effect.effect_id = locked_effect.effect_id
                   AND EXISTS (SELECT 1 FROM replay_write)
                RETURNING effect.*
            ), resolved_outputs AS (
                DELETE FROM external_effect_output_reconciliations AS reconciliation
                 USING updated_effect
                 WHERE reconciliation.effect_id = updated_effect.effect_id
                   AND reconciliation.account_id = updated_effect.account_id
                   AND reconciliation.claim_token = %s
                   AND reconciliation.claim_version = updated_effect.claim_version
                RETURNING reconciliation.effect_id
            )
            SELECT * FROM updated_effect
            """,
                (*params, normalized_claim_token),
            )
            row = cur.fetchone()
            if not row:
                raise ExternalEffectReplayLeaseLostError(
                    "external effect replay lease is stale or missing"
                )
            confirmed = _row_to_dict(row, cur.description)
            statement_completed = True
    except ExternalEffectReplayLeaseLostError:
        raise
    except ExternalEffectReplayCommitError:
        raise
    except Exception as exc:
        outcome = "rolled_back"
        if committed:
            outcome = "committed"
        elif statement_completed:
            outcome = "unknown"
        raise ExternalEffectReplayCommitError(commit_outcome=outcome) from exc
    return confirmed


def update_external_effect_status(
    db: Any,
    *,
    effect_id: str,
    claim_token: str | None = None,
    status: str,
    status_reason: str = "",
    provider_message_id: str | None = None,
    external_object_id: str | None = None,
    error_code: str | None = None,
    error_text: str | None = None,
    response_summary_jsonb: dict[str, Any] | None = None,
    expected_current_status: str | None = None,
) -> dict[str, Any] | None:
    normalized_effect_id = _uuid_text(effect_id)
    if not normalized_effect_id:
        return None
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {
        "claimed",
        "running",
        "succeeded",
        "failed",
        "timeout_unknown",
        "suppressed",
        "expired",
    }:
        raise ValueError(f"Unsupported external effect status '{status}'")
    started_expr = "now()" if normalized_status == "running" else "started_at"
    completed_expr = (
        "now()"
        if normalized_status in {"succeeded", "failed", "timeout_unknown", "suppressed", "expired"}
        else "completed_at"
    )
    with db.cursor() as cur:
        claim_predicate = ""
        params: list[Any] = [
            normalized_status,
            str(status_reason or "").strip()[:500],
            str(provider_message_id).strip() if provider_message_id else None,
            str(external_object_id).strip() if external_object_id else None,
            str(error_code).strip()[:200] if error_code else None,
            str(error_text).strip()[:1000] if error_text else None,
            _jsonb(response_summary_jsonb, default={}) if response_summary_jsonb else None,
            normalized_effect_id,
        ]
        if str(claim_token or "").strip():
            claim_predicate = "AND claim_token = %s"
            params.append(str(claim_token).strip())
        expected_status_predicate = ""
        if str(expected_current_status or "").strip():
            expected_status_predicate = "AND status = %s"
            params.append(str(expected_current_status).strip().lower())
        sql = f"""
            UPDATE external_effects
               SET status = %s,
                   status_reason = %s,
                   provider_message_id = COALESCE(%s, provider_message_id),
                   external_object_id = COALESCE(%s, external_object_id),
                   error_code = %s,
                   error_text = %s,
                   response_summary_jsonb = COALESCE(%s::jsonb, response_summary_jsonb),
                   started_at = {started_expr},
                   completed_at = {completed_expr},
                   updated_at = now()
             WHERE effect_id = %s::uuid
               {claim_predicate}
               {expected_status_predicate}
             RETURNING *
            """  # nosec B608 - status timestamp expressions are fixed constants.
        cur.execute(
            sql,
            tuple(params),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def cleanup_stale_external_effects(
    db: Any,
    *,
    account_id: str,
) -> dict[str, int]:
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        raise ValueError("account_id is required for external-effect cleanup")
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE external_effects
               SET status = 'expired',
                   status_reason = 'claimed_expired',
                   claim_version = claim_version + 1,
                   claim_token = gen_random_uuid()::text,
                   completed_at = now(),
                   updated_at = now()
             WHERE status = 'claimed'
               AND account_id = %s::uuid
               AND expires_at IS NOT NULL
               AND expires_at < now()
            """,
            (normalized_account_id,),
        )
        expired = int(getattr(cur, "rowcount", 0) or 0)
        cur.execute(
            """
            UPDATE external_effects
               SET status = 'timeout_unknown',
                   status_reason = 'running_timeout_unknown',
                   claim_version = claim_version + 1,
                   claim_token = gen_random_uuid()::text,
                   completed_at = now(),
                   updated_at = now()
             WHERE status = 'running'
               AND account_id = %s::uuid
               AND expires_at IS NOT NULL
               AND expires_at < now()
            """,
            (normalized_account_id,),
        )
        timeout_unknown = int(getattr(cur, "rowcount", 0) or 0)
        cur.execute(
            """
            DELETE FROM external_effect_rate_windows
             WHERE window_started_at < now() - interval '24 hours'
               AND account_id = %s::uuid
            """,
            (normalized_account_id,),
        )
        rate_windows_deleted = int(getattr(cur, "rowcount", 0) or 0)
        cur.execute(
            """
            WITH retention AS (
                SELECT COALESCE(
                    (
                        SELECT NULLIF(s.setting_value_jsonb->>'action_logs_days', '')::integer
                          FROM system_settings s
                         WHERE s.account_id = %s
                           AND s.setting_key = 'retention_policy'
                    ),
                    30
                ) AS action_logs_days
            )
            UPDATE external_effect_replays replay
               SET replay_state = 'nonreplayable',
                   replay_reason = 'retention_expired',
                   encrypted_payload = NULL,
                   updated_at = now()
              FROM retention
             WHERE replay.account_id = %s
               AND replay.replay_state = 'available'
               AND replay.updated_at < now() - make_interval(
                    days => retention.action_logs_days
               )
            """,
            (normalized_account_id, normalized_account_id),
        )
        replay_payloads_expired = int(getattr(cur, "rowcount", 0) or 0)
    return {
        "expired": expired,
        "timeout_unknown": timeout_unknown,
        "rate_windows_deleted": rate_windows_deleted,
        "replay_payloads_expired": replay_payloads_expired,
        "detached_effects_deleted": 0,
    }
