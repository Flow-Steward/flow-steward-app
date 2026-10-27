"""Account-scoped model discovery, verification and runtime snapshots."""

from __future__ import annotations

import json
from typing import Any

from core.application.settings.llm_provider_repository_rows import (
    _cursor_kwargs,
    _normalize_model,
    _row_to_dict,
)

# Why a verified model must be verified again. Both are safe UI categories.
MODEL_STALE_REASONS = frozenset({"configuration_changed", "credentials_changed"})


def get_model_runtime_snapshot(
    conn,
    *,
    account_id: str,
    extension_install_id: str,
    provider_key: str,
    model_id: str,
    verification_generation: str = "",
) -> dict[str, Any]:
    """Resolve one generation-consistent, account-scoped model invocation snapshot."""
    generation = str(verification_generation or "").strip()
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT install.install_id,
                   install.extension_id,
                   install.release_id,
                   install.state,
                   install.runtime_mode,
                   install.remote_base_url,
                   install.handshake_status,
                   install.health_status,
                   install.runtime_status,
                   release.descriptor_jsonb,
                   source.connection_config_jsonb,
                   COALESCE(
                       source.credential_secret_ref,
                       install.config_jsonb ->> 'llm_provider_credential_secret_ref'
                   ) AS credential_secret_ref,
                   secret.encrypted_value
              FROM extension_installs AS install
              JOIN extension_releases AS release
                ON release.release_id = install.release_id
               AND release.extension_id = install.extension_id
              JOIN llm_provider_sources AS source
                ON source.account_id = install.account_id
               AND source.extension_install_id = install.install_id
               AND source.provider_key = %s
               AND source.enabled = true
              JOIN llm_provider_models AS model
                ON model.account_id = source.account_id
               AND model.provider_source_id = source.id
               AND model.model_id = %s
         LEFT JOIN secret_refs AS secret
                ON secret.account_id = install.account_id
               AND secret.ref_key = COALESCE(
                       source.credential_secret_ref,
                       install.config_jsonb ->> 'llm_provider_credential_secret_ref'
                   )
             WHERE install.account_id = %s
               AND install.install_id = %s
               AND install.state = 'active'
               AND (
                    install.runtime_mode IS DISTINCT FROM 'remote_https'
                    OR COALESCE(btrim(install.remote_base_url), '') <> ''
               )
               AND install.health_status <> 'failed'
               AND install.handshake_status <> 'failed'
               AND install.runtime_status <> 'incompatible'
               AND (
                    (
                        %s <> ''
                        AND model.verification_status = 'pending'
                        AND model.verification_generation = %s
                    )
                    OR
                    (
                        %s = ''
                        AND model.enabled = true
                        AND model.supports_structured_output = true
                        AND model.verification_status = 'passed'
                        AND model.lifecycle = 'active'
                        AND model.verified_at >= now() - interval '30 days'
                        AND model.verified_at <= now()
                    )
               )
             LIMIT 1
            """,
            (
                str(provider_key),
                str(model_id),
                str(account_id),
                str(extension_install_id),
                generation,
                generation,
                generation,
            ),
        )
        row = cur.fetchone()
        description = cur.description
    return _row_to_dict(row, description)


def upsert_discovered_models(
    conn,
    *,
    account_id: str,
    provider_source_id: str,
    models: list[dict[str, Any]],
) -> int:
    """Bulk upsert discovered models. Never rewrites model_id, never auto-enables.

    On conflict the operator's ``enabled`` flag is preserved; only discovery
    metadata (label, capabilities, structured-output support, raw metadata) is
    refreshed.
    """
    count = 0
    with conn.cursor() as cur:
        for model in models or []:
            model_id = str((model or {}).get("model_id") or "").strip()
            if not model_id:
                continue
            cur.execute(
                """
                INSERT INTO llm_provider_models
                       (account_id, provider_source_id, model_id, display_label,
                        upstream_owner_key, upstream_owner_label, capabilities_jsonb, declared_capabilities_jsonb,
                        supports_structured_output, raw_provider_metadata_jsonb,
                        upstream_revision, upstream_digest, discovered_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (account_id, provider_source_id, model_id) DO UPDATE SET
                        display_label = EXCLUDED.display_label,
                        upstream_owner_key = EXCLUDED.upstream_owner_key,
                        upstream_owner_label = EXCLUDED.upstream_owner_label,
                        capabilities_jsonb = EXCLUDED.capabilities_jsonb,
                        declared_capabilities_jsonb = EXCLUDED.declared_capabilities_jsonb,
                        supports_structured_output = EXCLUDED.supports_structured_output,
                        raw_provider_metadata_jsonb = EXCLUDED.raw_provider_metadata_jsonb,
                        verification_status = CASE
                            WHEN llm_provider_models.upstream_revision IS DISTINCT FROM EXCLUDED.upstream_revision
                              OR llm_provider_models.upstream_digest IS DISTINCT FROM EXCLUDED.upstream_digest
                              OR llm_provider_models.declared_capabilities_jsonb IS DISTINCT FROM EXCLUDED.declared_capabilities_jsonb
                            THEN 'stale' ELSE llm_provider_models.verification_status END,
                        verified_capabilities_jsonb = CASE
                            WHEN llm_provider_models.upstream_revision IS DISTINCT FROM EXCLUDED.upstream_revision
                              OR llm_provider_models.upstream_digest IS DISTINCT FROM EXCLUDED.upstream_digest
                              OR llm_provider_models.declared_capabilities_jsonb IS DISTINCT FROM EXCLUDED.declared_capabilities_jsonb
                            THEN '[]'::jsonb ELSE llm_provider_models.verified_capabilities_jsonb END,
                        verification_error_safe = CASE
                            WHEN llm_provider_models.upstream_revision IS DISTINCT FROM EXCLUDED.upstream_revision
                              OR llm_provider_models.upstream_digest IS DISTINCT FROM EXCLUDED.upstream_digest
                              OR llm_provider_models.declared_capabilities_jsonb IS DISTINCT FROM EXCLUDED.declared_capabilities_jsonb
                            THEN 'upstream_revision_or_capabilities_changed' ELSE llm_provider_models.verification_error_safe END,
                        verified_at = CASE
                            WHEN llm_provider_models.upstream_revision IS DISTINCT FROM EXCLUDED.upstream_revision
                              OR llm_provider_models.upstream_digest IS DISTINCT FROM EXCLUDED.upstream_digest
                              OR llm_provider_models.declared_capabilities_jsonb IS DISTINCT FROM EXCLUDED.declared_capabilities_jsonb
                            THEN NULL ELSE llm_provider_models.verified_at END,
                        enabled = CASE
                            WHEN llm_provider_models.upstream_revision IS DISTINCT FROM EXCLUDED.upstream_revision
                              OR llm_provider_models.upstream_digest IS DISTINCT FROM EXCLUDED.upstream_digest
                              OR llm_provider_models.declared_capabilities_jsonb IS DISTINCT FROM EXCLUDED.declared_capabilities_jsonb
                            THEN false ELSE llm_provider_models.enabled END,
                        upstream_revision = EXCLUDED.upstream_revision,
                        upstream_digest = EXCLUDED.upstream_digest,
                        discovered_at = now(),
                        updated_at = now()
                """,
                (
                    account_id,
                    provider_source_id,
                    model_id,
                    str((model or {}).get("display_label") or model_id),
                    (model or {}).get("upstream_owner_key"),
                    (model or {}).get("upstream_owner_label"),
                    json.dumps((model or {}).get("capabilities") or []),
                    json.dumps((model or {}).get("capabilities") or []),
                    bool((model or {}).get("supports_structured_output")),
                    json.dumps((model or {}).get("metadata") or {}),
                    str((model or {}).get("upstream_revision") or "") or None,
                    str((model or {}).get("upstream_digest") or "") or None,
                ),
            )
            count += 1
    return count


def list_models(
    conn, *, account_id: str, provider_source_id: str | None = None
) -> list[dict[str, Any]]:
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        if provider_source_id:
            cur.execute(
                """
                SELECT * FROM llm_provider_models
                 WHERE account_id = %s AND provider_source_id::text = %s
                 ORDER BY display_label, model_id
                """,
                (str(account_id), str(provider_source_id)),
            )
        else:
            cur.execute(
                """
                SELECT * FROM llm_provider_models
                 WHERE account_id = %s
                 ORDER BY display_label, model_id
                """,
                (str(account_id),),
            )
        rows = cur.fetchall()
        description = list(cur.description or [])
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _row_to_dict(row, description)
        if payload:
            out.append(_normalize_model(payload))
    return out


def get_model(conn, *, account_id: str, model_row_id: str) -> dict[str, Any] | None:
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            "SELECT * FROM llm_provider_models WHERE id::text = %s AND account_id = %s",
            (str(model_row_id), str(account_id)),
        )
        row = cur.fetchone()
        description = list(cur.description or [])
    payload = _row_to_dict(row, description) if row is not None else {}
    return _normalize_model(payload) if payload else None


def claim_model_verification(
    conn,
    *,
    account_id: str,
    model_row_id: str,
    generation: str,
    expected_model_updated_at: Any,
    expected_source_updated_at: Any,
    expected_upstream_revision: str | None,
    expected_upstream_digest: str | None,
) -> dict[str, Any] | None:
    """Atomically claim one paid verification suite before any upstream call."""
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            """
            UPDATE llm_provider_models m
               SET verification_status = 'pending',
                   verification_generation = %s,
                   verification_started_at = now(),
                   verification_error_safe = NULL,
                   verified_at = NULL,
                   enabled = false,
                   updated_at = now()
              FROM llm_provider_sources s
             WHERE m.id::text = %s
               AND m.account_id = %s
               AND s.id = m.provider_source_id
               AND m.updated_at = %s
               AND s.updated_at IS NOT DISTINCT FROM %s
               AND COALESCE(m.upstream_revision, '') = %s
               AND COALESCE(m.upstream_digest, '') = %s
               AND (
                    m.verification_status <> 'pending'
                    OR m.verification_started_at < now() - interval '10 minutes'
               )
         RETURNING m.verification_generation
            """,
            (
                str(generation),
                str(model_row_id),
                str(account_id),
                expected_model_updated_at,
                expected_source_updated_at,
                str(expected_upstream_revision or ""),
                str(expected_upstream_digest or ""),
            ),
        )
        row = cur.fetchone()
        description = cur.description
    payload = _row_to_dict(row, description) if row is not None else {}
    return payload or None


def list_enabled_structured_models(
    conn, *, account_id: str, provider_source_id: str
) -> list[dict[str, Any]]:
    """Enabled, structured-output-capable models for a source (global-routing set)."""
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            """
            SELECT * FROM llm_provider_models
             WHERE account_id = %s
               AND provider_source_id::text = %s
               AND enabled = true
               AND supports_structured_output = true
               AND verification_status = 'passed'
               AND lifecycle = 'active'
               AND verified_at >= now() - interval '30 days'
               AND verified_at <= now()
             ORDER BY display_label, model_id
            """,
            (str(account_id), str(provider_source_id)),
        )
        rows = cur.fetchall()
        description = list(cur.description or [])
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _row_to_dict(row, description)
        if payload:
            out.append(_normalize_model(payload))
    return out


def list_enabled_structured_models_for_sources(
    conn, *, account_id: str, provider_source_ids: list[str]
) -> list[dict[str, Any]]:
    """Batch the globally-routable model projection for multiple sources."""
    normalized_ids = [str(value).strip() for value in provider_source_ids if str(value).strip()]
    if not normalized_ids:
        return []
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            """
            SELECT * FROM llm_provider_models
             WHERE account_id = %s
               AND provider_source_id = ANY(%s::uuid[])
               AND enabled = true
               AND supports_structured_output = true
               AND verification_status = 'passed'
               AND lifecycle = 'active'
               AND verified_at >= now() - interval '30 days'
               AND verified_at <= now()
             ORDER BY provider_source_id, display_label, model_id
            """,
            (str(account_id), normalized_ids),
        )
        rows = cur.fetchall()
        description = list(cur.description or [])
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _row_to_dict(row, description)
        if payload:
            out.append(_normalize_model(payload))
    return out


def set_model_enabled(conn, *, account_id: str, model_row_id: str, enabled: bool) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_models
               SET enabled = %s,
                   verification_status = CASE
                       WHEN %s = false AND verification_status = 'pending' THEN 'stale'
                       ELSE verification_status
                   END,
                   verification_generation = CASE WHEN %s = false THEN NULL ELSE verification_generation END,
                   verification_error_safe = CASE
                       WHEN %s = false AND verification_status = 'pending' THEN 'verification_cancelled'
                       ELSE verification_error_safe
                   END,
                   updated_at = now()
             WHERE id::text = %s AND account_id = %s
            """,
            (
                bool(enabled),
                bool(enabled),
                bool(enabled),
                bool(enabled),
                str(model_row_id),
                str(account_id),
            ),
        )
        return cur.rowcount > 0


def set_model_verification(
    conn,
    *,
    account_id: str,
    model_row_id: str,
    status: str,
    verified_capabilities: list[str] | None,
    safe_error: str | None,
) -> bool:
    """Persist one live verification outcome without storing provider error detail."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_models
               SET verification_status = %s,
                   verification_generation = NULL,
                   verified_capabilities_jsonb = %s::jsonb,
                   verification_error_safe = %s,
                   verified_at = CASE WHEN %s = 'passed' THEN now() ELSE NULL END,
                   updated_at = now()
             WHERE id::text = %s AND account_id = %s
            """,
            (
                str(status),
                json.dumps(sorted({str(item) for item in (verified_capabilities or [])})),
                str(safe_error or "") or None,
                str(status),
                str(model_row_id),
                str(account_id),
            ),
        )
        return cur.rowcount > 0


def set_model_verification_if_current(
    conn,
    *,
    account_id: str,
    model_row_id: str,
    status: str,
    verified_capabilities: list[str] | None,
    safe_error: str | None,
    enabled: bool,
    expected_generation: str,
    expected_source_updated_at: Any,
    expected_upstream_revision: str | None,
    expected_upstream_digest: str | None,
) -> bool:
    """Persist a probe result only if its source/configuration is unchanged.

    Live verification runs outside the database transaction. Only the owner of
    the pending generation may publish, and source/upstream identity must still
    match the snapshot claimed before the first provider call.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_models m
               SET verification_status = %s,
                   verification_generation = NULL,
                   verified_capabilities_jsonb = %s::jsonb,
                   verification_error_safe = %s,
                   verified_at = CASE WHEN %s = 'passed' THEN now() ELSE NULL END,
                   enabled = %s,
                   updated_at = now()
              FROM llm_provider_sources s
             WHERE m.id::text = %s
               AND m.account_id = %s
               AND s.id = m.provider_source_id
               AND m.verification_status = 'pending'
               AND m.verification_generation = %s
               AND s.updated_at = %s
               AND COALESCE(m.upstream_revision, '') = %s
               AND COALESCE(m.upstream_digest, '') = %s
            """,
            (
                str(status),
                json.dumps(sorted({str(item) for item in (verified_capabilities or [])})),
                str(safe_error or "") or None,
                str(status),
                bool(enabled),
                str(model_row_id),
                str(account_id),
                str(expected_generation),
                expected_source_updated_at,
                str(expected_upstream_revision or ""),
                str(expected_upstream_digest or ""),
            ),
        )
        return cur.rowcount > 0


def cancel_model_verification_generation(
    conn,
    *,
    account_id: str,
    model_row_id: str,
    expected_generation: str,
) -> bool:
    """Release only the pending verification generation owned by this caller."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_models
               SET verification_status = 'stale',
                   verification_generation = NULL,
                   verification_error_safe = 'configuration_changed',
                   verified_at = NULL,
                   enabled = false,
                   updated_at = now()
             WHERE id::text = %s
               AND account_id = %s
               AND verification_status = 'pending'
               AND verification_generation = %s
            """,
            (str(model_row_id), str(account_id), str(expected_generation)),
        )
        return cur.rowcount > 0


def mark_install_models_stale(
    conn,
    *,
    account_id: str,
    extension_install_id: str,
    reason: str = "configuration_changed",
) -> int:
    """Invalidate routes verified against prior install credentials/config.

    A credential or endpoint change changes the upstream identity.  Keep the
    discovery snapshot for UI inspection, but remove every verified model from
    routing until it passes the live suite again.  Only models that were
    verified (or are being verified) become stale: a never-verified model keeps
    ``unverified`` and a failed one keeps its own result, since nothing about
    them was checked against the old settings.
    """
    if reason not in MODEL_STALE_REASONS:
        raise ValueError(f"unsupported stale reason: {reason}")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_models m
               SET verification_status = 'stale',
                   verification_generation = NULL,
                   verified_capabilities_jsonb = '[]'::jsonb,
                   verification_error_safe = %s,
                   verified_at = NULL,
                   enabled = false,
                   updated_at = now()
              FROM llm_provider_sources s
             WHERE s.id = m.provider_source_id
               AND m.account_id = %s
               AND s.account_id = m.account_id
               AND s.extension_install_id = %s
               AND m.verification_status IN ('passed', 'pending', 'stale')
            """,
            (str(reason), str(account_id), str(extension_install_id)),
        )
        return cur.rowcount


def reconcile_models_for_source(
    conn, *, account_id: str, provider_source_id: str, keep_model_ids: list[str]
) -> int:
    """Delete models of a source whose model_id is absent from the latest discovery.

    Ensures a model removed upstream stops being catalog/runtime-valid after a
    refresh. Empty keep list removes all models for the source.
    """
    keep = [str(m) for m in (keep_model_ids or []) if str(m)]
    base = "DELETE FROM llm_provider_models WHERE account_id = %s AND provider_source_id::text = %s"
    with conn.cursor() as cur:
        if keep:
            # Verb-less placeholders fragment only (values are parameterized).
            in_clause = ",".join(["%s"] * len(keep))
            cur.execute(
                base + f" AND model_id NOT IN ({in_clause})",
                (str(account_id), str(provider_source_id), *keep),
            )
        else:
            cur.execute(base, (str(account_id), str(provider_source_id)))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
