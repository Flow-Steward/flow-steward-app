"""Keep OAuth working across an extension update that did not change sign-in.

When an install is activated on a new release, every OAuth configuration still
pinned to an earlier release is judged against the new release, with the same
provider compilation the runtime uses:

- the provider signs in exactly as before (same security and revocation
  fingerprints) and no connection of the configuration lacks a scope the new
  release now requires: the configuration and its connections move to the new
  release at once, keeping their ids, credentials and revision. Nothing is
  blocked and nobody has to open a page;
- otherwise the block stays, and the configuration (``last_error``) and each
  affected connection (``oauth.attention_code``) name the reason, which runtime
  failures then report instead of a generic resolution failure.
"""

from __future__ import annotations

import json
from typing import Any

from core.application.extensions.oauth_contract import contract_section

RELEASE_REAPPROVAL_REQUIRED = "oauth_release_reapproval_required"
REASON_PROVIDER_REMOVED = "provider_removed"
REASON_SIGN_IN_CHANGED = "provider_sign_in_changed"
REASON_REQUIRED_SCOPE_ADDED = "required_scope_added"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _required_scopes(descriptor: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    required: dict[tuple[str, str], set[str]] = {}
    for source in (descriptor, _as_dict(descriptor.get("compiled_manifest"))):
        for row in contract_section(source, "connection_types"):
            auth = _as_dict(row.get("auth"))
            if _text(auth.get("type")).lower() != "oauth2":
                continue
            scopes = _as_dict(auth.get("scopes")).get("required")
            key = (_text(auth.get("provider_id")), _text(row.get("connection_type_id")))
            required[key] = {_text(item) for item in scopes or [] if _text(item)}
        if required:
            break
    return required


def release_attention_code(configuration: dict[str, Any] | None) -> str:
    """The reason a configuration stays blocked after an update, or ``""``."""
    last_error = _as_dict(_as_dict(configuration).get("last_error"))
    if _text(last_error.get("code")) == RELEASE_REAPPROVAL_REQUIRED:
        return RELEASE_REAPPROVAL_REQUIRED
    return ""


def reconcile_install_oauth_release(
    conn: Any,
    *,
    account_id: str,
    install_id: str,
    extension_id: str,
    release_id: str,
    descriptor: dict[str, Any],
) -> list[dict[str, str]]:
    """Move or flag every configuration of the install left on an earlier release.

    Runs inside the activation transaction. Returns one entry per configuration
    it looked at: ``{"configuration_id", "outcome", "reason"}``.
    """
    from core.application.extensions.oauth_contract import (
        compile_oauth_providers,
        provider_revocation_fingerprint,
        provider_security_fingerprint,
    )

    if not callable(getattr(conn, "cursor", None)) or not _text(release_id):
        return []
    try:
        providers = {
            provider.provider_id: provider for provider in compile_oauth_providers(descriptor)
        }
    except Exception:
        providers = {}
    required = _required_scopes(_as_dict(descriptor))
    results: list[dict[str, str]] = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT configuration_id::text, provider_id, provider_security_fingerprint,
                      provider_revocation_fingerprint
                 FROM extension_oauth_configurations
                WHERE account_id::text = %s AND extension_install_id = %s
                  AND extension_id = %s AND extension_release_id <> %s
                ORDER BY configuration_id
                FOR UPDATE""",
            (_text(account_id), _text(install_id), _text(extension_id), _text(release_id)),
        )
        configurations = cur.fetchall()
        for configuration_id, provider_id, security, revocation in configurations:
            provider = providers.get(_text(provider_id))
            cur.execute(
                """SELECT connection_id, connection_type, oauth_granted_scopes
                     FROM project_connections
                    WHERE oauth_configuration_id::text = %s
                    ORDER BY connection_id
                    FOR UPDATE""",
                (configuration_id,),
            )
            connections = cur.fetchall()
            reason = ""
            affected: list[str] = []
            if provider is None:
                reason = REASON_PROVIDER_REMOVED
            elif _text(security) != provider_security_fingerprint(provider) or _text(
                revocation
            ) != provider_revocation_fingerprint(provider):
                reason = REASON_SIGN_IN_CHANGED
            if reason:
                affected = [row[0] for row in connections]
            else:
                for connection_id, connection_type, granted in connections:
                    needed = required.get((_text(provider_id), _text(connection_type)), set())
                    granted_scopes = {_text(item) for item in (granted or []) if _text(item)}
                    if not needed.issubset(granted_scopes):
                        affected.append(connection_id)
                if affected:
                    reason = REASON_REQUIRED_SCOPE_ADDED
            if not reason:
                cur.execute(
                    """UPDATE extension_oauth_configurations
                          SET extension_release_id = %s, last_error = '{}'::jsonb, updated_at = now()
                        WHERE configuration_id::text = %s""",
                    (_text(release_id), configuration_id),
                )
                cur.execute(
                    """UPDATE project_connections
                          SET oauth_extension_release_id = %s,
                              connection_config = CASE
                                  WHEN jsonb_typeof(connection_config->'oauth') = 'object'
                                  THEN jsonb_set(connection_config, '{oauth}',
                                                 (connection_config->'oauth') - 'attention_code')
                                  ELSE connection_config END,
                              updated_at = now()
                        WHERE oauth_configuration_id::text = %s""",
                    (_text(release_id), configuration_id),
                )
                results.append(
                    {"configuration_id": configuration_id, "outcome": "moved", "reason": ""}
                )
                continue
            cur.execute(
                """UPDATE extension_oauth_configurations
                      SET last_error = %s::jsonb, updated_at = now()
                    WHERE configuration_id::text = %s""",
                (
                    json.dumps({"code": RELEASE_REAPPROVAL_REQUIRED, "reason": reason}),
                    configuration_id,
                ),
            )
            if affected:
                cur.execute(
                    """UPDATE project_connections
                          SET connection_config = COALESCE(connection_config, '{}'::jsonb)
                              || jsonb_build_object(
                                  'oauth',
                                  (CASE WHEN jsonb_typeof(connection_config->'oauth') = 'object'
                                        THEN connection_config->'oauth' ELSE '{}'::jsonb END)
                                  || jsonb_build_object('attention_code', %s::text)),
                              updated_at = now()
                        WHERE connection_id = ANY(%s)""",
                    (reason, affected),
                )
            results.append(
                {"configuration_id": configuration_id, "outcome": "blocked", "reason": reason}
            )
    return results


__all__ = [
    "REASON_PROVIDER_REMOVED",
    "REASON_REQUIRED_SCOPE_ADDED",
    "REASON_SIGN_IN_CHANGED",
    "RELEASE_REAPPROVAL_REQUIRED",
    "reconcile_install_oauth_release",
    "release_attention_code",
]
