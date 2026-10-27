"""Caller-transaction-owned OAuth state persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.oauth_repository_values import _json_payload, _row_to_dict

_CREATE_STATE_SQL = """
    INSERT INTO extension_oauth_authorization_states (
        state_hash, tenant_id, account_id, project_id,
        extension_install_id, extension_id, extension_release_id, callback_id,
        provider_id, configuration_id, configuration_revision,
        authorization_revision, connection_type, intent_id,
        existing_connection_id, display_name, intent_payload,
        requested_scopes, redirect_uri, pkce_verifier_encrypted,
        provider_security_fingerprint, expires_at
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s
    ) RETURNING state_hash, tenant_id, account_id, project_id,
                extension_install_id, extension_id, extension_release_id, callback_id,
                provider_id, configuration_id, configuration_revision,
                authorization_revision, connection_type, intent_id,
                existing_connection_id, display_name, intent_payload,
                requested_scopes, redirect_uri, pkce_verifier_encrypted,
                provider_security_fingerprint, expires_at, consumed_at,
                created_at, updated_at
"""

_CONSUME_STATE_SQL = """
    UPDATE extension_oauth_authorization_states
    SET consumed_at = now(), updated_at = now()
    WHERE state_hash = %s
      AND configuration_id = %s
      AND tenant_id = %s
      AND account_id = %s
      AND project_id = %s
      AND extension_install_id = %s
      AND extension_id = %s
      AND provider_id = %s
      AND consumed_at IS NULL
      AND expires_at > now()
    RETURNING state_hash, tenant_id, account_id, project_id,
              extension_install_id, extension_id, extension_release_id, callback_id,
              provider_id, configuration_id, configuration_revision,
              authorization_revision, connection_type, intent_id,
              existing_connection_id, display_name, intent_payload,
              requested_scopes, redirect_uri, pkce_verifier_encrypted,
              provider_security_fingerprint, expires_at, consumed_at,
              created_at, updated_at
"""

_CONSUME_STATE_FOR_CALLBACK_SQL = """
    UPDATE extension_oauth_authorization_states
    SET consumed_at = now(), updated_at = now()
    WHERE state_hash = %s
      AND account_id = %s
      AND project_id = %s
      AND extension_id = %s
      AND callback_id = %s
      AND consumed_at IS NULL
      AND expires_at > now()
    RETURNING state_hash, tenant_id, account_id, project_id,
              extension_install_id, extension_id, extension_release_id, callback_id,
              provider_id, configuration_id, configuration_revision,
              authorization_revision, connection_type, intent_id,
              existing_connection_id, display_name, intent_payload,
              requested_scopes, redirect_uri, pkce_verifier_encrypted,
              provider_security_fingerprint, expires_at, consumed_at,
              created_at, updated_at
"""

#: The same row the consume targets, read without claiming it. A signed callback
#: has to be checked against the state it names before anything is spent, so an
#: attacker cannot burn a valid state by replaying it with a bad signature.
_LOAD_STATE_FOR_CALLBACK_SQL = """
    SELECT state_hash, tenant_id, account_id, project_id,
           extension_install_id, extension_id, extension_release_id, callback_id,
           provider_id, configuration_id, configuration_revision,
           authorization_revision, connection_type, intent_id,
           existing_connection_id, display_name, intent_payload,
           requested_scopes, redirect_uri, pkce_verifier_encrypted,
           provider_security_fingerprint, expires_at, consumed_at,
           created_at, updated_at
    FROM extension_oauth_authorization_states
    WHERE state_hash = %s
      AND account_id = %s
      AND project_id = %s
      AND extension_id = %s
      AND callback_id = %s
      AND consumed_at IS NULL
      AND expires_at > now()
"""


class OAuthStateRepositoryMixin:
    """Uses the public repository's supplied connection without owning transactions."""

    conn: Any

    def create_state(
        self,
        *,
        state_hash: str,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        extension_release_id: str,
        callback_id: str,
        provider_id: str,
        configuration_id: Any,
        configuration_revision: int,
        authorization_revision: int,
        connection_type: str,
        intent_id: str,
        existing_connection_id: str | None,
        display_name: str,
        intent_payload: Mapping[str, Any],
        requested_scopes: list[str],
        redirect_uri: str,
        pkce_verifier_encrypted: str,
        provider_security_fingerprint: str,
        expires_at: Any,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                _CREATE_STATE_SQL,
                (
                    state_hash,
                    tenant_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    extension_release_id,
                    callback_id,
                    provider_id,
                    configuration_id,
                    configuration_revision,
                    authorization_revision,
                    connection_type,
                    intent_id,
                    existing_connection_id,
                    display_name,
                    _json_payload(intent_payload),
                    _json_payload(requested_scopes),
                    redirect_uri,
                    pkce_verifier_encrypted,
                    provider_security_fingerprint,
                    expires_at,
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def consume_state(
        self,
        *,
        state_hash: str,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                _CONSUME_STATE_SQL,
                (
                    state_hash,
                    configuration_id,
                    tenant_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    provider_id,
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def consume_state_for_callback(
        self,
        *,
        state_hash: str,
        account_id: Any,
        project_id: Any,
        extension_id: str,
        callback_id: str,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                _CONSUME_STATE_FOR_CALLBACK_SQL,
                (state_hash, account_id, project_id, extension_id, callback_id),
            )
            return _row_to_dict(cur, cur.fetchone())

    def load_state_for_callback(
        self,
        *,
        state_hash: str,
        account_id: Any,
        project_id: Any,
        extension_id: str,
        callback_id: str,
    ) -> dict[str, Any] | None:
        """Read a live authorization state without consuming it."""
        with self.conn.cursor() as cur:
            cur.execute(
                _LOAD_STATE_FOR_CALLBACK_SQL,
                (state_hash, account_id, project_id, extension_id, callback_id),
            )
            return _row_to_dict(cur, cur.fetchone())
