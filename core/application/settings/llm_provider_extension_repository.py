"""Persistence helpers for extension-contributed LLM provider groups/models.

Account-scoped CRUD over ``llm_provider_sources`` (provider groups) and
``llm_provider_models`` (provider-returned models). Mirrors the conventions in
``model_provider_store`` (RealDictCursor rows, str-normalized ids, fail-closed
account scoping). Credentials reuse the account-scoped ``secret_refs`` store via
``model_provider_store.upsert_secret_ref``; decryption happens in the runtime
layer (``core.application.auth.secrets.decrypt_secret``).
"""

from __future__ import annotations

import json
from typing import Any

from core.application.settings.llm_provider_model_repository import (
    cancel_model_verification_generation as cancel_model_verification_generation,
)
from core.application.settings.llm_provider_model_repository import (
    claim_model_verification as claim_model_verification,
)
from core.application.settings.llm_provider_model_repository import (
    get_model as get_model,
)
from core.application.settings.llm_provider_model_repository import (
    get_model_runtime_snapshot as get_model_runtime_snapshot,
)
from core.application.settings.llm_provider_model_repository import (
    list_enabled_structured_models as list_enabled_structured_models,
)
from core.application.settings.llm_provider_model_repository import (
    list_enabled_structured_models_for_sources as list_enabled_structured_models_for_sources,
)
from core.application.settings.llm_provider_model_repository import (
    list_models as list_models,
)
from core.application.settings.llm_provider_model_repository import (
    mark_install_models_stale as mark_install_models_stale,
)
from core.application.settings.llm_provider_model_repository import (
    reconcile_models_for_source as reconcile_models_for_source,
)
from core.application.settings.llm_provider_model_repository import (
    set_model_enabled as set_model_enabled,
)
from core.application.settings.llm_provider_model_repository import (
    set_model_verification as set_model_verification,
)
from core.application.settings.llm_provider_model_repository import (
    set_model_verification_if_current as set_model_verification_if_current,
)
from core.application.settings.llm_provider_model_repository import (
    upsert_discovered_models as upsert_discovered_models,
)
from core.application.settings.llm_provider_repository_rows import _cursor_kwargs, _row_to_dict

PROVIDER_KEY_PREFIX = "llmext_"
INSTALL_CREDENTIAL_REF_KEY = "llm_provider_credential_secret_ref"


class InstallDiscoveryReleaseMismatchError(RuntimeError):
    """The locked install no longer owns the resolved release descriptor snapshot."""


def _normalize_source(payload: dict[str, Any]) -> dict[str, Any]:
    payload["id"] = str(payload.get("id") or "").strip()
    if "account_id" in payload:
        payload["account_id"] = str(payload.get("account_id") or "").strip()
    return payload


# --------------------------------------------------------------------------- #
# Provider sources (groups)                                                    #
# --------------------------------------------------------------------------- #


def upsert_source(
    conn,
    *,
    account_id: str,
    extension_install_id: str,
    extension_id: str,
    provider_key: str,
    display_label: str,
    supplier_key: str,
    supplier_label: str,
    upstream_owner_key: str | None,
    upstream_owner_label: str | None,
    models_query_id: str = "llm.list_models",
    chat_action_id: str = "llm.chat",
    structured_chat_action_id: str | None = None,
    credential_secret_ref: str | None = None,
    capabilities: dict[str, Any] | None = None,
    connection_config: dict[str, Any] | None = None,
) -> str | None:
    """Insert or refresh a provider source; returns its id.

    ``enabled`` and ``credential_secret_ref`` are preserved on conflict unless a
    new credential ref is supplied (so a model refresh never clears credentials
    or re-enables a disabled source).
    """
    next_contract = (
        str(models_query_id or ""),
        str(chat_action_id or ""),
        str(structured_chat_action_id or ""),
        json.dumps(capabilities or {}, sort_keys=True, separators=(",", ":")),
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT models_query_id, chat_action_id, structured_chat_action_id,
                   capabilities_jsonb::text
              FROM llm_provider_sources
             WHERE account_id = %s AND provider_key = %s
            """,
            (str(account_id), str(provider_key)),
        )
        previous = cur.fetchone()
        cur.execute(
            """
            INSERT INTO llm_provider_sources
                   (account_id, extension_install_id, extension_id, provider_key,
                    display_label, supplier_key, supplier_label,
                    upstream_owner_key, upstream_owner_label,
                    models_query_id, chat_action_id, structured_chat_action_id,
                    credential_secret_ref, capabilities_jsonb, connection_config_jsonb)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (account_id, provider_key) DO UPDATE SET
                    extension_install_id = EXCLUDED.extension_install_id,
                    extension_id = EXCLUDED.extension_id,
                    display_label = EXCLUDED.display_label,
                    supplier_key = EXCLUDED.supplier_key,
                    supplier_label = EXCLUDED.supplier_label,
                    upstream_owner_key = EXCLUDED.upstream_owner_key,
                    upstream_owner_label = EXCLUDED.upstream_owner_label,
                    models_query_id = EXCLUDED.models_query_id,
                    chat_action_id = EXCLUDED.chat_action_id,
                    structured_chat_action_id = EXCLUDED.structured_chat_action_id,
                    credential_secret_ref = COALESCE(
                        EXCLUDED.credential_secret_ref,
                        llm_provider_sources.credential_secret_ref
                    ),
                    capabilities_jsonb = EXCLUDED.capabilities_jsonb,
                    connection_config_jsonb = EXCLUDED.connection_config_jsonb,
                    updated_at = now()
               RETURNING id
            """,
            (
                account_id,
                extension_install_id,
                extension_id,
                provider_key,
                display_label,
                supplier_key,
                supplier_label,
                upstream_owner_key,
                upstream_owner_label,
                models_query_id,
                chat_action_id,
                structured_chat_action_id,
                credential_secret_ref,
                json.dumps(capabilities or {}),
                json.dumps(connection_config or {}),
            ),
        )
        row = cur.fetchone()
        source_id = str(row[0]) if row else None
        if previous and source_id:
            if isinstance(previous, dict):
                previous_contract = (
                    str(previous.get("models_query_id") or ""),
                    str(previous.get("chat_action_id") or ""),
                    str(previous.get("structured_chat_action_id") or ""),
                    str(previous.get("capabilities_jsonb") or ""),
                )
            else:
                previous_contract = tuple(str(value or "") for value in previous)
            if previous_contract != next_contract:
                cur.execute(
                    """
                    UPDATE llm_provider_models
                       SET verification_status = 'stale',
                           verified_capabilities_jsonb = '[]'::jsonb,
                           verification_error_safe = 'source_contract_changed',
                           verified_at = NULL,
                           enabled = false,
                           updated_at = now()
                     WHERE account_id = %s AND provider_source_id::text = %s
                    """,
                    (str(account_id), source_id),
                )
    return source_id


def get_source(conn, *, account_id: str, source_id: str) -> dict[str, Any] | None:
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            "SELECT * FROM llm_provider_sources WHERE id::text = %s AND account_id = %s",
            (str(source_id), str(account_id)),
        )
        row = cur.fetchone()
        description = list(cur.description or [])
    payload = _row_to_dict(row, description) if row is not None else {}
    return _normalize_source(payload) if payload else None


def get_source_by_provider_key(
    conn, *, account_id: str, provider_key: str
) -> dict[str, Any] | None:
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            "SELECT * FROM llm_provider_sources WHERE account_id = %s AND provider_key = %s",
            (str(account_id), str(provider_key)),
        )
        row = cur.fetchone()
        description = list(cur.description or [])
    payload = _row_to_dict(row, description) if row is not None else {}
    return _normalize_source(payload) if payload else None


def list_sources(conn, *, account_id: str) -> list[dict[str, Any]]:
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            """SELECT source.*, install.remote_base_url
                 FROM llm_provider_sources AS source
                 JOIN extension_installs AS install
                   ON install.account_id = source.account_id
                  AND install.install_id = source.extension_install_id
                WHERE source.account_id = %s
             ORDER BY source.display_label, source.provider_key""",
            (str(account_id),),
        )
        rows = cur.fetchall()
        description = list(cur.description or [])
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _row_to_dict(row, description)
        if payload:
            out.append(_normalize_source(payload))
    return out


def list_enabled_sources(conn, *, account_id: str) -> list[dict[str, Any]]:
    """Enabled sources whose extension install is active and healthy.

    Disabled/deleted/unhealthy installs (or disabled sources) contribute no
    selectable provider groups (brief: "must not contribute selectable
    provider groups").
    """
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        # Join on the EXACT install_id the source was discovered under (not just
        # extension_id) so a reinstall with a new install_id, or a stale row whose
        # install no longer exists, drops out instead of riding a sibling install's
        # health. Combined with state/health filters this guarantees stale sources
        # are neither selectable nor runtime-resolvable.
        cur.execute(
            """
            SELECT s.*
              FROM llm_provider_sources s
              JOIN extension_installs ei
                ON ei.install_id = s.extension_install_id
               AND ei.account_id = s.account_id
             WHERE s.account_id = %s
               AND s.enabled = true
               AND ei.state = 'active'
               AND ei.health_status <> 'failed'
               AND ei.handshake_status <> 'failed'
               AND ei.runtime_status <> 'incompatible'
             ORDER BY s.display_label, s.provider_key
            """,
            (str(account_id),),
        )
        rows = cur.fetchall()
        description = list(cur.description or [])
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _row_to_dict(row, description)
        if payload:
            out.append(_normalize_source(payload))
    return out


def get_enabled_source_by_provider_key(
    conn, *, account_id: str, provider_key: str
) -> dict[str, Any] | None:
    """Like get_source_by_provider_key but enforces the active/healthy-install join.

    Reuses ``list_enabled_sources`` so save-time validation, catalog options, and
    runtime resolution all apply the *same* gate (no SQL drift). Returns None for a
    disabled source or one whose install is disabled/uninstalled/unhealthy/stale.
    """
    target = str(provider_key)
    for source in list_enabled_sources(conn, account_id=account_id):
        if str(source.get("provider_key") or "") == target:
            return source
    return None


def set_source_enabled(conn, *, account_id: str, source_id: str, enabled: bool) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_sources
               SET enabled = %s, updated_at = now()
             WHERE id::text = %s AND account_id = %s
            """,
            (bool(enabled), str(source_id), str(account_id)),
        )
        return cur.rowcount > 0


def set_source_credential_ref(
    conn, *, account_id: str, source_id: str, credential_secret_ref: str
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_sources
               SET credential_secret_ref = %s, updated_at = now()
             WHERE id::text = %s AND account_id = %s
            """,
            (str(credential_secret_ref), str(source_id), str(account_id)),
        )
        return cur.rowcount > 0


def set_source_connection_config(
    conn, *, account_id: str, source_id: str, connection_config: dict[str, Any]
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_sources
               SET connection_config_jsonb = %s::jsonb, updated_at = now()
             WHERE id::text = %s AND account_id = %s
            """,
            (json.dumps(connection_config or {}), str(source_id), str(account_id)),
        )
        return cur.rowcount > 0


def set_install_connection_config(
    conn, *, account_id: str, extension_install_id: str, connection_config: dict[str, Any]
) -> bool:
    """Persist pre-discovery LLM connection config in the existing install record.

    A provider has no ``llm_provider_sources`` row until its first successful
    discovery.  Keeping this non-secret config under the install's existing
    ``config_jsonb`` avoids a second registry and prevents a save/discover
    navigation from dropping a self-hosted provider's upstream URL.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = jsonb_set(
                       COALESCE(config_jsonb, '{}'::jsonb),
                       '{llm_provider_connection_config}',
                       %s::jsonb,
                       true
                   )
             WHERE account_id = %s AND install_id = %s
            """,
            (json.dumps(connection_config or {}), str(account_id), str(extension_install_id)),
        )
        return cur.rowcount > 0


def set_install_remote_base_url(
    conn, *, account_id: str, extension_install_id: str, remote_base_url: str
) -> bool:
    """Persist the host-to-runtime URL on the existing account-scoped install."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET remote_base_url = %s,
                   handshake_status = 'unknown',
                   health_status = 'unknown',
                   runtime_status = 'unknown',
                   last_error = NULL
             WHERE account_id = %s AND install_id = %s
            """,
            (
                str(remote_base_url).strip() or None,
                str(account_id),
                str(extension_install_id),
            ),
        )
        return cur.rowcount > 0


def set_install_provider_state_revision(
    conn, *, account_id: str, extension_install_id: str, revision: str
) -> bool:
    """Invalidate in-flight discovery when credentials/config/runtime identity changes."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = jsonb_set(
                       COALESCE(config_jsonb, '{}'::jsonb),
                       '{llm_provider_state_revision}',
                       to_jsonb(%s::text),
                       true
                   )
             WHERE account_id = %s AND install_id = %s
            """,
            (str(revision), str(account_id), str(extension_install_id)),
        )
        return bool(cur.rowcount)


def invalidate_extension_models_for_release_change(
    conn,
    *,
    account_id: str,
    extension_install_id: str,
    changed_release_id: str,
) -> int:
    """Invalidate every route whose install identity or global release changed.

    ``extension_releases.release_id`` is global, so descriptor drift must fan out
    to every account that references the release.  The account/install arm also
    covers an individual install moving to another release.  Both discovery CAS
    inputs are rotated in the same statement that makes published models stale.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH affected_installs AS (
                UPDATE extension_installs AS install
                   SET config_jsonb = jsonb_set(
                           jsonb_set(
                               COALESCE(install.config_jsonb, '{}'::jsonb),
                               '{llm_provider_state_revision}',
                               to_jsonb(gen_random_uuid()::text),
                               true
                           ),
                           '{llm_provider_discovery_generation}',
                           to_jsonb(gen_random_uuid()::text),
                           true
                       )
                 WHERE install.release_id = %s
                    OR (
                        install.account_id = %s::uuid
                        AND install.install_id = %s
                    )
             RETURNING install.account_id, install.install_id
            )
            UPDATE llm_provider_models AS model
               SET verification_status = 'stale',
                   verified_capabilities_jsonb = '[]'::jsonb,
                   verification_error_safe = %s,
                   verified_at = NULL,
                   verification_generation = NULL,
                   enabled = false,
                   updated_at = now()
              FROM llm_provider_sources AS source,
                   affected_installs AS install
             WHERE model.account_id = source.account_id
               AND model.provider_source_id = source.id
               AND source.account_id = install.account_id
               AND source.extension_install_id = install.install_id
            """,
            (
                str(changed_release_id),
                str(account_id),
                str(extension_install_id),
                "extension_release_or_descriptor_changed",
            ),
        )
        return int(cur.rowcount)


def deactivate_invalid_extension_routes(conn, *, extension_id: str) -> int:
    """Fail closed every account-scoped route for an invalid or missing bundle."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH affected_installs AS (
                UPDATE extension_installs AS install
                   SET state = 'disabled',
                       state_reason = 'extension_invalid_or_missing',
                       disabled_at = COALESCE(install.disabled_at, now()),
                       config_jsonb = jsonb_set(
                           jsonb_set(
                               COALESCE(install.config_jsonb, '{}'::jsonb),
                               '{llm_provider_state_revision}',
                               to_jsonb(gen_random_uuid()::text),
                               true
                           ),
                           '{llm_provider_discovery_generation}',
                           to_jsonb(gen_random_uuid()::text),
                           true
                       )
                 WHERE install.extension_id = %s
             RETURNING install.account_id, install.install_id
            )
            UPDATE llm_provider_models AS model
               SET verification_status = 'stale',
                   verified_capabilities_jsonb = '[]'::jsonb,
                   verification_error_safe = %s,
                   verified_at = NULL,
                   verification_generation = NULL,
                   enabled = false,
                   updated_at = now()
              FROM llm_provider_sources AS source,
                   affected_installs AS install
             WHERE model.account_id = source.account_id
               AND model.provider_source_id = source.id
               AND source.account_id = install.account_id
               AND source.extension_install_id = install.install_id
            """,
            (
                str(extension_id),
                "extension_invalid_or_missing",
            ),
        )
        return int(cur.rowcount)


def set_install_credential_ref(
    conn, *, account_id: str, extension_install_id: str, credential_secret_ref: str
) -> bool:
    """Persist the current install-scoped secret pointer for pre-discovery use."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = jsonb_set(
                       COALESCE(config_jsonb, '{}'::jsonb),
                       '{llm_provider_credential_secret_ref}',
                       to_jsonb(%s::text),
                       true
                   )
             WHERE account_id = %s AND install_id = %s
            """,
            (str(credential_secret_ref), str(account_id), str(extension_install_id)),
        )
        return bool(cur.rowcount)


def lock_install_for_credential_rotation(
    conn, *, account_id: str, extension_install_id: str
) -> dict[str, Any] | None:
    """Serialize credential generations on the owning account/install row.

    The returned install config is read only after the row lock is held, so a
    credential patch merges against the latest committed configuration.  Every
    credential save takes this lock before reading secret/source pointers and
    keeps it through pointer updates and old-generation cleanup.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT config_jsonb, remote_base_url
              FROM extension_installs
             WHERE account_id = %s AND install_id = %s
             FOR UPDATE
            """,
            (str(account_id), str(extension_install_id)),
        )
        row = cur.fetchone()
    if not row:
        return None
    value = row.get("config_jsonb") if isinstance(row, dict) else row[0]
    payload = dict(value) if isinstance(value, dict) else {}
    remote_base_url = row.get("remote_base_url") if isinstance(row, dict) else row[1]
    payload["remote_base_url"] = str(remote_base_url or "").strip()
    return payload


def begin_install_discovery(
    conn,
    *,
    account_id: str,
    extension_install_id: str,
    generation: str,
    expected_release_id: str,
    expected_descriptor_sha256: str,
) -> dict[str, Any] | None:
    """Claim discovery and return one release/endpoint/config/credential snapshot.

    Credential rotation and runtime endpoint updates serialize on the owning
    install row. PostgreSQL READ COMMITTED can keep lateral reads on a stale
    statement snapshot after EvalPlanQual waits for an UPDATE row lock, so the
    lock, generation claim, and dependent source/secret read are deliberately
    separate statements in one transaction.
    """
    normalized_expected_release_id = str(expected_release_id or "").strip()
    normalized_expected_descriptor_sha256 = str(expected_descriptor_sha256 or "").strip()
    if not normalized_expected_release_id or not normalized_expected_descriptor_sha256:
        raise InstallDiscoveryReleaseMismatchError
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT install_id, release_id, extension_id
              FROM extension_installs
             WHERE account_id = %s AND install_id = %s
             FOR UPDATE
            """,
            (str(account_id), str(extension_install_id)),
        )
        locked_install = _row_to_dict(cur.fetchone(), cur.description)
        if not locked_install:
            return None
        locked_release_id = str(locked_install.get("release_id") or "").strip()
        if not locked_release_id or locked_release_id != normalized_expected_release_id:
            raise InstallDiscoveryReleaseMismatchError
        locked_extension_id = str(locked_install.get("extension_id") or "").strip()
        cur.execute(
            """
            SELECT release_id, extension_id, descriptor_jsonb, descriptor_sha256
              FROM extension_releases
             WHERE release_id = %s
             FOR UPDATE
            """,
            (locked_release_id,),
        )
        locked_release = _row_to_dict(cur.fetchone(), cur.description)
        locked_descriptor_sha256 = str(locked_release.get("descriptor_sha256") or "").strip()
        if (
            not locked_release
            or not locked_extension_id
            or str(locked_release.get("extension_id") or "").strip() != locked_extension_id
            or not locked_descriptor_sha256
            or locked_descriptor_sha256 != normalized_expected_descriptor_sha256
        ):
            raise InstallDiscoveryReleaseMismatchError
        cur.execute(
            """
            UPDATE extension_installs
               SET config_jsonb = jsonb_set(
                   COALESCE(config_jsonb, '{}'::jsonb),
                   '{llm_provider_discovery_generation}',
                   to_jsonb(%s::text),
                   true
               )
             WHERE account_id = %s AND install_id = %s
         RETURNING config_jsonb, remote_base_url
            """,
            (str(generation), str(account_id), str(extension_install_id)),
        )
        install_payload = _row_to_dict(cur.fetchone(), cur.description)
        install_config = install_payload.get("config_jsonb")
        install_credential_ref = (
            str(install_config.get(INSTALL_CREDENTIAL_REF_KEY) or "").strip()
            if isinstance(install_config, dict)
            else ""
        )
        cur.execute(
            """
            SELECT COALESCE(source.credential_secret_ref, %s) AS credential_secret_ref,
                   source.connection_config_jsonb,
                   secret.encrypted_value
              FROM (SELECT 1) AS anchor
         LEFT JOIN LATERAL (
                   SELECT credential_secret_ref, connection_config_jsonb
                     FROM llm_provider_sources
                    WHERE account_id = %s
                      AND extension_install_id = %s
                 ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                   ) AS source ON TRUE
         LEFT JOIN secret_refs AS secret
                ON secret.account_id = %s
               AND secret.ref_key = COALESCE(source.credential_secret_ref, %s)
            """,
            (
                install_credential_ref,
                str(account_id),
                str(extension_install_id),
                str(account_id),
                install_credential_ref,
            ),
        )
        source_payload = _row_to_dict(cur.fetchone(), cur.description)
    value = install_payload.get("config_jsonb")
    connection_config = source_payload.get("connection_config_jsonb")
    snapshot = dict(value) if isinstance(value, dict) else {}
    locked_descriptor = locked_release.get("descriptor_jsonb")
    snapshot.update(
        {
            "release_id": locked_release_id,
            "extension_id": locked_extension_id,
            "descriptor_jsonb": (
                dict(locked_descriptor) if isinstance(locked_descriptor, dict) else {}
            ),
            "descriptor_sha256": locked_descriptor_sha256,
            "remote_base_url": str(install_payload.get("remote_base_url") or "").strip(),
            "credential_secret_ref": str(source_payload.get("credential_secret_ref") or "").strip(),
            "encrypted_value": str(source_payload.get("encrypted_value") or ""),
            "connection_config_jsonb": (
                dict(connection_config) if isinstance(connection_config, dict) else {}
            ),
        }
    )
    return snapshot


def lock_install_discovery_generation_current(
    conn,
    *,
    account_id: str,
    extension_install_id: str,
    generation: str,
    state_revision: str,
    release_id: str,
    descriptor_sha256: str,
) -> bool:
    """CAS completion against its generation, state, and release descriptor snapshot."""
    normalized_expected_release_id = str(release_id or "").strip()
    normalized_expected_descriptor_sha256 = str(descriptor_sha256 or "").strip()
    if not normalized_expected_release_id or not normalized_expected_descriptor_sha256:
        return False
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT config_jsonb ->> 'llm_provider_discovery_generation' AS discovery_generation,
                   config_jsonb ->> 'llm_provider_state_revision' AS state_revision,
                   release_id,
                   extension_id
              FROM extension_installs
             WHERE account_id = %s AND install_id = %s
             FOR UPDATE
            """,
            (str(account_id), str(extension_install_id)),
        )
        locked_install = _row_to_dict(cur.fetchone(), cur.description)
        current_generation = locked_install.get("discovery_generation")
        current_state_revision = locked_install.get("state_revision")
        current_release_id = locked_install.get("release_id")
        current_extension_id = str(locked_install.get("extension_id") or "").strip()
        generation_matches = str(current_generation or "") == str(generation)
        state_matches = str(current_state_revision or "") == str(state_revision or "")
        normalized_current_release_id = str(current_release_id or "").strip()
        release_matches = bool(normalized_current_release_id) and (
            normalized_current_release_id == normalized_expected_release_id
        )
        if not (generation_matches and state_matches and release_matches and current_extension_id):
            return False
        cur.execute(
            """
            SELECT extension_id, descriptor_sha256
              FROM extension_releases
             WHERE release_id = %s
             FOR UPDATE
            """,
            (normalized_current_release_id,),
        )
        locked_release = _row_to_dict(cur.fetchone(), cur.description)
    locked_descriptor_sha256 = str(locked_release.get("descriptor_sha256") or "").strip()
    descriptor_matches = bool(locked_descriptor_sha256) and (
        locked_descriptor_sha256 == normalized_expected_descriptor_sha256
    )
    extension_matches = bool(locked_release) and (
        str(locked_release.get("extension_id") or "").strip() == current_extension_id
    )
    return descriptor_matches and extension_matches


def get_install_current_credential_ref(conn, *, account_id: str, extension_install_id: str) -> str:
    """Resolve the source pointer, falling back to the pre-discovery install pointer."""
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT COALESCE(
                       source.credential_secret_ref,
                       install.config_jsonb ->> 'llm_provider_credential_secret_ref'
                   ) AS credential_secret_ref
              FROM extension_installs AS install
         LEFT JOIN LATERAL (
                   SELECT credential_secret_ref
                     FROM llm_provider_sources
                    WHERE account_id = install.account_id
                      AND extension_install_id = install.install_id
                      AND credential_secret_ref IS NOT NULL
                 ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                   ) AS source ON TRUE
             WHERE install.account_id = %s
               AND install.install_id = %s
            """,
            (str(account_id), str(extension_install_id)),
        )
        row = cur.fetchone()
        description = cur.description
    return str(_row_to_dict(row, description).get("credential_secret_ref") or "").strip()


def get_install_current_credential_snapshot(
    conn, *, account_id: str, extension_install_id: str
) -> tuple[str, str]:
    """Atomically read the current source pointer and its encrypted value."""
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT COALESCE(
                       source.credential_secret_ref,
                       install.config_jsonb ->> 'llm_provider_credential_secret_ref'
                   ) AS credential_secret_ref,
                   secret.encrypted_value
              FROM extension_installs AS install
         LEFT JOIN LATERAL (
                   SELECT credential_secret_ref
                     FROM llm_provider_sources
                    WHERE account_id = install.account_id
                      AND extension_install_id = install.install_id
                      AND credential_secret_ref IS NOT NULL
                 ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                   ) AS source ON TRUE
              JOIN secret_refs AS secret
                ON secret.account_id = install.account_id
               AND secret.ref_key = COALESCE(
                       source.credential_secret_ref,
                       install.config_jsonb ->> 'llm_provider_credential_secret_ref'
                   )
             WHERE install.account_id = %s
               AND install.install_id = %s
            """,
            (str(account_id), str(extension_install_id)),
        )
        row = cur.fetchone()
        description = cur.description
    payload = _row_to_dict(row, description)
    return (
        str(payload.get("credential_secret_ref") or "").strip(),
        str(payload.get("encrypted_value") or ""),
    )


def get_install_runtime_credential_snapshot(
    conn, *, account_id: str, extension_install_id: str
) -> dict[str, Any]:
    """Read runtime identity and current credential in one MVCC statement."""
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT install.install_id,
                   install.release_id,
                   install.runtime_mode,
                   install.remote_base_url,
                   COALESCE(
                       source.credential_secret_ref,
                       install.config_jsonb ->> 'llm_provider_credential_secret_ref'
                   ) AS credential_secret_ref,
                   secret.encrypted_value
              FROM extension_installs AS install
         LEFT JOIN LATERAL (
                   SELECT credential_secret_ref
                     FROM llm_provider_sources
                    WHERE account_id = install.account_id
                      AND extension_install_id = install.install_id
                      AND credential_secret_ref IS NOT NULL
                 ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                   ) AS source ON TRUE
         LEFT JOIN secret_refs AS secret
                ON secret.account_id = install.account_id
               AND secret.ref_key = COALESCE(
                       source.credential_secret_ref,
                       install.config_jsonb ->> 'llm_provider_credential_secret_ref'
                   )
             WHERE install.account_id = %s
               AND install.install_id = %s
            """,
            (str(account_id), str(extension_install_id)),
        )
        row = cur.fetchone()
        description = cur.description
    return _row_to_dict(row, description)


def mark_install_runtime_unknown(conn, *, account_id: str, extension_install_id: str) -> bool:
    """Clear a stale lifecycle failure after its runtime credentials change.

    The subsequent authenticated discovery/verification remains mandatory; this
    only prevents an unauthenticated pre-credential handshake from permanently
    hiding an otherwise installable provider source.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET handshake_status = 'unknown',
                   health_status = 'unknown',
                   last_error = NULL
             WHERE account_id = %s AND install_id = %s
            """,
            (str(account_id), str(extension_install_id)),
        )
        return cur.rowcount > 0


def reconcile_sources_for_install(
    conn, *, account_id: str, extension_install_id: str, keep_provider_keys: list[str]
) -> int:
    """Delete provider groups of an install no longer present in the latest discovery.

    Cascades to their models via FK. Empty keep list removes all sources for the
    install (e.g. the provider returned no usable models).
    """
    keep = [str(k) for k in (keep_provider_keys or []) if str(k)]
    base = "DELETE FROM llm_provider_sources WHERE account_id = %s AND extension_install_id = %s"
    with conn.cursor() as cur:
        if keep:
            in_clause = ",".join(["%s"] * len(keep))
            cur.execute(
                base + f" AND provider_key NOT IN ({in_clause})",
                (str(account_id), str(extension_install_id), *keep),
            )
        else:
            cur.execute(base, (str(account_id), str(extension_install_id)))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def delete_sources_for_extension(conn, *, extension_id: str) -> int:
    """Remove provider sources (and, via FK cascade, their models) for an extension.

    Called from the extension uninstall sinks so uninstall/reinstall does not
    leave orphaned ``llm_provider_sources`` rows. Keyed by ``extension_id`` to
    match the granularity of the surrounding ``extension_installs`` cleanup.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM llm_provider_sources WHERE extension_id = %s",
            (str(extension_id),),
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def read_secret_ref_ciphertext(conn, *, account_id: str, ref_key: str) -> str | None:
    """Return the encrypted value for an account-scoped secret ref (or None)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT encrypted_value FROM secret_refs WHERE ref_key = %s AND account_id = %s",
            (str(ref_key), str(account_id)),
        )
        row = cur.fetchone()
    if not row:
        return None
    return row[0]


def delete_install_credential_refs_except(
    conn,
    *,
    account_id: str,
    base_ref_key: str,
    keep_ref_keys: list[str] | None = None,
) -> int:
    """Bound rotated credentials while retaining one in-flight generation."""
    keep = list(dict.fromkeys(str(ref or "").strip() for ref in (keep_ref_keys or []) if ref))
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM secret_refs
             WHERE account_id = %s
               AND (
                    ref_key = %s
                    OR left(ref_key, length(%s) + 1) = %s || '_'
               )
               AND NOT (ref_key = ANY(%s))
            """,
            (
                str(account_id),
                str(base_ref_key),
                str(base_ref_key),
                str(base_ref_key),
                keep,
            ),
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
