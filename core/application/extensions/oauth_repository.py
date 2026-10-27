"""Caller-transaction-owned persistence for extension OAuth state."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from core.application.extensions.oauth_configuration_repository import (
    OAuthConfigurationRepositoryMixin,
)
from core.application.extensions.oauth_repository_values import _json_payload, _row_to_dict
from core.application.extensions.oauth_state_repository import OAuthStateRepositoryMixin

_LOCK_CONNECTION_SQL = """
    SELECT connection.connection_id, configuration.tenant_id,
           connection.project_id, connection.extension_id,
           connection.account_id, connection.extension_install_id,
           connection.display_name, connection.connection_kind,
           connection.connection_type, connection.environment,
           connection.credential_revision, connection.enabled,
           connection.connection_config, connection.secret_refs,
           connection.oauth_configuration_id, connection.oauth_provider_id,
           connection.oauth_extension_release_id,
           connection.oauth_authorization_revision,
           connection.oauth_requested_scopes, connection.oauth_granted_scopes,
           connection.oauth_provider_security_fingerprint,
           connection.oauth_provider_revocation_fingerprint,
           connection.created_at, connection.updated_at
    FROM project_connections AS connection
    JOIN extension_oauth_configurations AS configuration
      ON configuration.configuration_id = connection.oauth_configuration_id
     AND configuration.account_id = connection.account_id
     AND configuration.project_id = connection.project_id
     AND configuration.extension_install_id = connection.extension_install_id
     AND configuration.extension_id = connection.extension_id
     AND configuration.provider_id = connection.oauth_provider_id
    WHERE connection.connection_id = %s
      AND configuration.configuration_id = %s
      AND configuration.tenant_id = %s
      AND configuration.account_id = %s
      AND configuration.project_id = %s
      AND configuration.extension_install_id = %s
      AND configuration.extension_id = %s
      AND configuration.provider_id = %s
      AND connection.connection_type = %s
    FOR UPDATE OF connection
"""

_LIST_CONNECTIONS_SQL = """
    SELECT connection.connection_id, connection.project_id, connection.extension_id,
           connection.account_id, connection.extension_install_id,
           connection.display_name, connection.connection_kind,
           connection.connection_type, connection.environment,
           connection.credential_revision, connection.enabled,
           connection.connection_config, connection.secret_refs,
           connection.oauth_configuration_id, connection.oauth_provider_id,
           connection.oauth_extension_release_id,
           connection.oauth_authorization_revision,
           connection.oauth_requested_scopes, connection.oauth_granted_scopes,
           connection.oauth_provider_security_fingerprint,
           connection.oauth_provider_revocation_fingerprint,
           connection.created_at, connection.updated_at
    FROM project_connections AS connection
    JOIN extension_oauth_configurations AS configuration
      ON configuration.configuration_id = connection.oauth_configuration_id
     AND configuration.account_id = connection.account_id
     AND configuration.project_id = connection.project_id
     AND configuration.extension_install_id = connection.extension_install_id
     AND configuration.extension_id = connection.extension_id
     AND configuration.provider_id = connection.oauth_provider_id
    WHERE configuration.configuration_id = %s
      AND configuration.tenant_id = %s
      AND configuration.account_id = %s
      AND configuration.project_id = %s
      AND configuration.extension_install_id = %s
      AND configuration.extension_id = %s
      AND configuration.provider_id = %s
    ORDER BY connection.created_at, connection.connection_id
"""

_LOCK_CONNECTIONS_SQL = _LIST_CONNECTIONS_SQL + " FOR UPDATE OF connection"


class ExtensionOAuthRepository(OAuthConfigurationRepositoryMixin, OAuthStateRepositoryMixin):
    """Uses one supplied connection and never owns commit or rollback."""

    def __init__(
        self,
        conn: Any,
        *,
        refresh_claim_connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        refresh_claim_timeout_seconds: float = 5.0,
    ) -> None:
        self.conn = conn
        self._refresh_claim_connection_factory = refresh_claim_connection_factory
        self._refresh_claim_timeout_seconds = max(
            0.1, min(float(refresh_claim_timeout_seconds), 30.0)
        )

    def lock_connection(
        self,
        *,
        connection_id: str,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
        connection_type: str,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                _LOCK_CONNECTION_SQL,
                (
                    connection_id,
                    configuration_id,
                    tenant_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    provider_id,
                    connection_type,
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def lock_connection_for_lifecycle(
        self,
        *,
        connection_id: str,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        extension_release_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> dict[str, Any] | None:
        _ = extension_release_id
        configuration = self.get_configuration_for_scope_for_update(
            configuration_id=configuration_id,
            tenant_id=tenant_id,
            account_id=account_id,
            project_id=project_id,
            extension_install_id=extension_install_id,
            extension_id=extension_id,
            provider_id=provider_id,
        )
        if configuration is None:
            return None
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT connection.*
                     FROM project_connections AS connection
                    WHERE connection.connection_id = %s
                      AND connection.account_id = %s
                      AND connection.project_id = %s
                      AND connection.extension_install_id = %s
                      AND connection.extension_id = %s
                      AND connection.oauth_provider_id = %s
                      AND connection.oauth_configuration_id = %s
                    FOR UPDATE OF connection""",
                (
                    connection_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    provider_id,
                    configuration_id,
                ),
            )
            payload = _row_to_dict(cur, cur.fetchone())
        if payload is None:
            return None
        payload.update(
            configuration_release_id=configuration.get("extension_release_id"),
            configuration_revision=configuration.get("configuration_revision"),
            configuration_authorization_revision=configuration.get("authorization_revision"),
            configuration_non_secret_config=configuration.get("non_secret_config"),
            configuration_secret_refs=configuration.get("secret_refs"),
            configuration_security_fingerprint=configuration.get("provider_security_fingerprint"),
            configuration_revocation_fingerprint=configuration.get(
                "provider_revocation_fingerprint"
            ),
        )
        return payload

    def block_connection_disconnect(self, **scope: Any) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections
                      SET enabled = false,
                          credential_revision = credential_revision + 1,
                          connection_config = COALESCE(connection_config, '{}'::jsonb)
                              || jsonb_build_object(
                                  'oauth',
                                  COALESCE(connection_config->'oauth', '{}'::jsonb)
                                  || jsonb_build_object(
                                      'lifecycle_status', 'disconnecting',
                                      'disconnect_operation_id', %s::text,
                                      'disconnect_started_at', to_jsonb(now()),
                                      'disconnect_lease_expires_at',
                                          to_jsonb(now() + (%s::integer * interval '1 second'))
                                  )
                              ),
                          updated_at = now()
                    WHERE connection_id = %s
                      AND account_id = %s AND project_id = %s
                      AND extension_install_id = %s AND extension_id = %s
                      AND oauth_provider_id = %s AND oauth_configuration_id = %s
                      AND enabled = true
                      AND COALESCE(connection_config->'oauth'->>'lifecycle_status', '')
                          NOT IN ('disconnecting', 'disconnected')
                      AND credential_revision = %s""",
                (
                    scope["disconnect_operation_id"],
                    scope["lease_seconds"],
                    scope["connection_id"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["extension_install_id"],
                    scope["extension_id"],
                    scope["provider_id"],
                    scope["configuration_id"],
                    scope["expected_credential_revision"],
                ),
            )
            return getattr(cur, "rowcount", 0) == 1

    def claim_stale_connection_disconnect(self, **scope: Any) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections
                      SET credential_revision = credential_revision + 1,
                          connection_config = COALESCE(connection_config, '{}'::jsonb)
                              || jsonb_build_object(
                                  'oauth',
                                  COALESCE(connection_config->'oauth', '{}'::jsonb)
                                  || jsonb_build_object(
                                      'disconnect_operation_id', %s::text,
                                      'disconnect_started_at', to_jsonb(now()),
                                      'disconnect_lease_expires_at',
                                          to_jsonb(now() + (%s::integer * interval '1 second'))
                                  )
                              ),
                          updated_at = now()
                    WHERE connection_id = %s
                      AND account_id = %s AND project_id = %s
                      AND extension_install_id = %s AND extension_id = %s
                      AND oauth_provider_id = %s AND oauth_configuration_id = %s
                      AND EXISTS (
                          SELECT 1 FROM projects
                           WHERE projects.id = project_connections.project_id
                             AND projects.account_id = project_connections.account_id
                             AND projects.tenant_id = %s
                      )
                      AND credential_revision = %s AND enabled = false
                      AND connection_config->'oauth'->>'lifecycle_status' = 'disconnecting'
                      AND COALESCE(
                          connection_config->'oauth'->>'disconnect_operation_id', ''
                      ) = %s
                      AND (
                          COALESCE(
                              connection_config->'oauth'->>'disconnect_operation_id', ''
                          ) !~ '^[A-Za-z0-9_-]{43}$'
                          OR NOT pg_input_is_valid(
                              COALESCE(
                                  connection_config->'oauth'->>'disconnect_lease_expires_at', ''
                              ),
                              'timestamp with time zone'
                          )
                          OR CASE
                              WHEN pg_input_is_valid(
                                  COALESCE(
                                      connection_config->'oauth'->>'disconnect_lease_expires_at', ''
                                  ),
                                  'timestamp with time zone'
                              )
                              THEN (
                                  connection_config->'oauth'->>'disconnect_lease_expires_at'
                              )::timestamptz <= now()
                              ELSE true
                          END
                      )""",
                (
                    scope["disconnect_operation_id"],
                    scope["lease_seconds"],
                    scope["connection_id"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["extension_install_id"],
                    scope["extension_id"],
                    scope["provider_id"],
                    scope["configuration_id"],
                    scope["tenant_id"],
                    scope["expected_credential_revision"],
                    scope["expected_disconnect_operation_id"],
                ),
            )
            return getattr(cur, "rowcount", 0) == 1

    def lock_connection_for_disconnect_finalize(self, **scope: Any) -> dict[str, Any] | None:
        """Lock only the prepared connection; provider/configuration may be gone."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT connection.*
                     FROM project_connections AS connection
                    WHERE connection.connection_id = %s
                      AND connection.account_id = %s
                      AND connection.project_id = %s
                      AND connection.extension_install_id = %s
                      AND connection.extension_id = %s
                      AND connection.oauth_provider_id = %s
                      AND connection.oauth_configuration_id = %s
                    FOR UPDATE OF connection""",
                (
                    scope["connection_id"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["extension_install_id"],
                    scope["extension_id"],
                    scope["provider_id"],
                    scope["configuration_id"],
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def invalidate_connection_lifecycle_states(self, **scope: Any) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE extension_oauth_authorization_states
                      SET consumed_at = COALESCE(consumed_at, now()), updated_at = now()
                    WHERE tenant_id = %s AND account_id = %s AND project_id = %s
                      AND extension_install_id = %s AND extension_id = %s
                      AND provider_id = %s AND configuration_id = %s
                      AND existing_connection_id = %s AND consumed_at IS NULL""",
                (
                    scope["tenant_id"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["extension_install_id"],
                    scope["extension_id"],
                    scope["provider_id"],
                    scope["configuration_id"],
                    scope["connection_id"],
                ),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def finalize_connection_disconnect(self, **scope: Any) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections
                      SET enabled = true,
                          oauth_granted_scopes = '[]'::jsonb,
                          secret_refs = '{}'::jsonb,
                          connection_config = COALESCE(connection_config, '{}'::jsonb)
                              || jsonb_build_object(
                                  'oauth',
                                  (
                                      COALESCE(connection_config->'oauth', '{}'::jsonb)
                                      - 'disconnect_operation_id'
                                      - 'disconnect_started_at'
                                      - 'disconnect_lease_expires_at'
                                  ) || jsonb_build_object('lifecycle_status', 'disconnected')
                              ),
                          health_status = 'unknown', runtime_status = 'unavailable',
                          last_error = '', updated_at = now()
                    WHERE connection_id = %s
                      AND account_id = %s AND project_id = %s
                      AND extension_install_id = %s AND extension_id = %s
                      AND oauth_provider_id = %s AND oauth_configuration_id = %s
                      AND credential_revision = %s AND enabled = false
                      AND connection_config->'oauth'->>'lifecycle_status' = 'disconnecting'
                      AND connection_config->'oauth'->>'disconnect_operation_id' = %s""",
                (
                    scope["connection_id"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["extension_install_id"],
                    scope["extension_id"],
                    scope["provider_id"],
                    scope["configuration_id"],
                    scope["expected_credential_revision"],
                    scope["expected_disconnect_operation_id"],
                ),
            )
            return getattr(cur, "rowcount", 0) == 1

    def list_connections_for_configuration(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                _LIST_CONNECTIONS_SQL,
                (
                    configuration_id,
                    tenant_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    provider_id,
                ),
            )
            return [
                payload for row in cur.fetchall() if (payload := _row_to_dict(cur, row)) is not None
            ]

    def list_connections_for_configuration_for_update(self, **scope: Any) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                _LOCK_CONNECTIONS_SQL,
                (
                    scope["configuration_id"],
                    scope["tenant_id"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["extension_install_id"],
                    scope["extension_id"],
                    scope["provider_id"],
                ),
            )
            return [
                payload for row in cur.fetchall() if (payload := _row_to_dict(cur, row)) is not None
            ]

    def create_oauth_connection(self, **values: Any) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO project_connections (
                       connection_id, project_id, extension_id, account_id,
                       display_name, connection_kind, connection_type,
                       credential_revision, extension_install_id,
                       oauth_configuration_id, oauth_provider_id,
                       oauth_extension_release_id, oauth_authorization_revision,
                       oauth_requested_scopes, oauth_granted_scopes,
                       oauth_provider_security_fingerprint,
                       oauth_provider_revocation_fingerprint,
                       enabled, health_status, runtime_status, last_seen_ok_at,
                       last_error, connection_config, secret_refs
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb, %s, %s, true, %s, %s,
                       CASE WHEN %s THEN now() ELSE NULL END, %s, %s::jsonb, %s::jsonb
                   )
                   RETURNING connection_id, project_id, extension_id, account_id,
                             display_name, connection_kind, connection_type,
                             credential_revision, extension_install_id,
                             oauth_configuration_id, oauth_provider_id,
                             oauth_extension_release_id, oauth_authorization_revision,
                             oauth_requested_scopes, oauth_granted_scopes,
                             oauth_provider_security_fingerprint,
                             oauth_provider_revocation_fingerprint,
                             enabled, health_status, runtime_status, last_seen_ok_at,
                             connection_config, secret_refs, created_at, updated_at""",
                (
                    values["connection_id"],
                    values["project_id"],
                    values["extension_id"],
                    values["account_id"],
                    values["display_name"],
                    values["connection_kind"],
                    values["connection_type"],
                    values["extension_install_id"],
                    values["oauth_configuration_id"],
                    values["oauth_provider_id"],
                    values["oauth_extension_release_id"],
                    values["oauth_authorization_revision"],
                    _json_payload(values["oauth_requested_scopes"]),
                    _json_payload(values["oauth_granted_scopes"]),
                    values["oauth_provider_security_fingerprint"],
                    values["oauth_provider_revocation_fingerprint"],
                    values["health_status"],
                    values["runtime_status"],
                    values["mark_last_seen_ok"],
                    values["last_error"],
                    _json_payload(values["connection_config"]),
                    _json_payload(values["secret_refs"]),
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def update_oauth_connection(self, **values: Any) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections
                      SET display_name = %s,
                          credential_revision = credential_revision + 1,
                          oauth_extension_release_id = %s,
                          oauth_authorization_revision = %s,
                          oauth_requested_scopes = %s::jsonb,
                          oauth_granted_scopes = %s::jsonb,
                          oauth_provider_security_fingerprint = %s,
                          oauth_provider_revocation_fingerprint = %s,
                          enabled = true,
                          health_status = %s,
                          runtime_status = %s,
                          last_seen_ok_at = CASE
                              WHEN %s THEN now()
                              ELSE last_seen_ok_at
                          END,
                          last_error = %s,
                          connection_config = %s::jsonb,
                          secret_refs = %s::jsonb,
                          updated_at = now()
                    WHERE connection_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND oauth_configuration_id = %s
                      AND oauth_provider_id = %s
                      AND connection_type = %s
                      AND credential_revision = %s
                    RETURNING connection_id, project_id, extension_id, account_id,
                              display_name, connection_kind, connection_type,
                              credential_revision, extension_install_id,
                              oauth_configuration_id, oauth_provider_id,
                              oauth_extension_release_id, oauth_authorization_revision,
                              oauth_requested_scopes, oauth_granted_scopes,
                              oauth_provider_security_fingerprint,
                              oauth_provider_revocation_fingerprint,
                              enabled, health_status, runtime_status, last_seen_ok_at,
                              connection_config, secret_refs, created_at, updated_at""",
                (
                    values["display_name"],
                    values["oauth_extension_release_id"],
                    values["oauth_authorization_revision"],
                    _json_payload(values["oauth_requested_scopes"]),
                    _json_payload(values["oauth_granted_scopes"]),
                    values["oauth_provider_security_fingerprint"],
                    values["oauth_provider_revocation_fingerprint"],
                    values["health_status"],
                    values["runtime_status"],
                    values["mark_last_seen_ok"],
                    values["last_error"],
                    _json_payload(values["connection_config"]),
                    _json_payload(values["secret_refs"]),
                    values["connection_id"],
                    values["account_id"],
                    values["project_id"],
                    values["extension_install_id"],
                    values["extension_id"],
                    values["oauth_configuration_id"],
                    values["oauth_provider_id"],
                    values["connection_type"],
                    values["expected_credential_revision"],
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def update_oauth_post_connect(self, **values: Any) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections
                      SET display_name = %s,
                          credential_revision = credential_revision + 1,
                          health_status = %s,
                          runtime_status = %s,
                          last_seen_ok_at = CASE
                              WHEN %s THEN now()
                              ELSE last_seen_ok_at
                          END,
                          last_error = %s,
                          connection_config = %s::jsonb,
                          updated_at = now()
                    WHERE connection_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND oauth_configuration_id = %s
                      AND oauth_provider_id = %s
                      AND connection_type = %s
                      AND credential_revision = %s
                    RETURNING connection_id, project_id, extension_id, account_id,
                              display_name, connection_kind, connection_type,
                              credential_revision, extension_install_id,
                              oauth_configuration_id, oauth_provider_id,
                              oauth_extension_release_id, oauth_authorization_revision,
                              oauth_requested_scopes, oauth_granted_scopes,
                              oauth_provider_security_fingerprint,
                              oauth_provider_revocation_fingerprint,
                              enabled, health_status, runtime_status, last_seen_ok_at,
                              connection_config, secret_refs, created_at, updated_at""",
                (
                    values["display_name"],
                    values["health_status"],
                    values["runtime_status"],
                    values["mark_last_seen_ok"],
                    values["last_error"],
                    _json_payload(values["connection_config"]),
                    values["connection_id"],
                    values["account_id"],
                    values["project_id"],
                    values["extension_install_id"],
                    values["extension_id"],
                    values["oauth_configuration_id"],
                    values["oauth_provider_id"],
                    values["connection_type"],
                    values["expected_credential_revision"],
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def update_oauth_refresh(self, **values: Any) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections AS connection
                      SET credential_revision = connection.credential_revision + 1,
                          oauth_granted_scopes = %s::jsonb,
                          secret_refs = %s::jsonb,
                          connection_config = %s::jsonb,
                          updated_at = now()
                     FROM extension_oauth_configurations AS configuration
                    WHERE connection.connection_id = %s
                      AND connection.account_id = %s
                      AND connection.project_id = %s
                      AND connection.extension_install_id = %s
                      AND connection.extension_id = %s
                      AND connection.oauth_configuration_id = %s
                      AND connection.oauth_provider_id = %s
                      AND connection.connection_type = %s
                      AND connection.credential_revision = %s
                      AND connection.secret_refs->'refs'->>'_oauth_refresh_token' = %s
                      AND configuration.configuration_id = connection.oauth_configuration_id
                      AND configuration.tenant_id = %s
                      AND configuration.account_id = connection.account_id
                      AND configuration.project_id = connection.project_id
                      AND configuration.extension_install_id = connection.extension_install_id
                      AND configuration.extension_id = connection.extension_id
                      AND configuration.provider_id = connection.oauth_provider_id
                      AND configuration.configuration_revision = %s
                      AND configuration.status IN ('verified', 'ready_for_authorization')
                 RETURNING connection.connection_id, connection.project_id,
                           connection.extension_id, connection.account_id,
                           connection.display_name, connection.connection_kind,
                           connection.connection_type, connection.credential_revision,
                           connection.extension_install_id,
                           connection.oauth_configuration_id, connection.oauth_provider_id,
                           connection.oauth_extension_release_id,
                           connection.oauth_authorization_revision,
                           connection.oauth_requested_scopes, connection.oauth_granted_scopes,
                           connection.oauth_provider_security_fingerprint,
                           connection.oauth_provider_revocation_fingerprint,
                           connection.enabled, connection.connection_config,
                           connection.secret_refs""",
                (
                    _json_payload(values["oauth_granted_scopes"]),
                    _json_payload(values["secret_refs"]),
                    _json_payload(values["connection_config"]),
                    values["connection_id"],
                    values["account_id"],
                    values["project_id"],
                    values["extension_install_id"],
                    values["extension_id"],
                    values["configuration_id"],
                    values["provider_id"],
                    values["connection_type"],
                    values["expected_credential_revision"],
                    values["expected_refresh_ref"],
                    values["tenant_id"],
                    values["expected_configuration_revision"],
                ),
            )
            updated = _row_to_dict(cur, cur.fetchone())
            if updated is None:
                return None
            cur.execute(
                """UPDATE extension_oauth_configurations
                      SET status = 'verified', last_error = '{}'::jsonb, updated_at = now()
                    WHERE configuration_id = %s
                      AND tenant_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND provider_id = %s
                      AND configuration_revision = %s""",
                (
                    values["configuration_id"],
                    values["tenant_id"],
                    values["account_id"],
                    values["project_id"],
                    values["extension_install_id"],
                    values["extension_id"],
                    values["provider_id"],
                    values["expected_configuration_revision"],
                ),
            )
            return updated if int(getattr(cur, "rowcount", 0) or 0) == 1 else None

    @contextmanager
    def oauth_refresh_claim(self, **scope: Any) -> Iterator[None]:
        if self._refresh_claim_connection_factory is None:
            raise RuntimeError("OAuth refresh claim connection is unavailable")
        identity_fields = (
            "tenant_id",
            "account_id",
            "project_id",
            "extension_install_id",
            "extension_id",
            "provider_id",
            "configuration_id",
            "connection_id",
            "connection_type",
        )
        identity = [str(scope.get(field) or "").strip() for field in identity_fields]
        if any(not value for value in identity):
            raise ValueError("complete OAuth refresh scope is required")
        lock_key = "extension-oauth-refresh:" + "".join(
            f"{len(value)}:{value};" for value in identity
        )
        deadline = time.monotonic() + self._refresh_claim_timeout_seconds
        acquired = False
        with self._refresh_claim_connection_factory() as claim_conn:
            try:
                while True:
                    with claim_conn.cursor() as cur:
                        cur.execute(
                            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                            (lock_key,),
                        )
                        row = cur.fetchone()
                    if row and bool(row[0]):
                        acquired = True
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("OAuth refresh claim is busy")
                    time.sleep(0.05)
                yield
            finally:
                if acquired:
                    unlock_ok = False
                    try:
                        with claim_conn.cursor() as cur:
                            cur.execute(
                                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                                (lock_key,),
                            )
                            row = cur.fetchone()
                        unlock_ok = bool(row and row[0])
                    finally:
                        if not unlock_ok:
                            claim_conn.close()
                            raise RuntimeError("OAuth refresh claim release was uncertain")

    @contextmanager
    def oauth_provider_token_claim(self, *, configuration_id: str, subject: str) -> Iterator[None]:
        """Serialize every operation that mints a token for one subject.

        A grant and a refresh for the same application and subject are mutually
        destructive: a provider that keeps one token per pair revokes what the
        other just produced, and whichever finishes second writes a pair that is
        already dead. Two locks could not express that, so both take this one.

        The key is the configuration and the verified subject — not the
        connection — because the hazard belongs to the subject, not to the row that
        happens to hold its token.

        Held on its own session for the duration, rather than as a
        transaction-scoped lock, because the work it covers includes an outbound
        HTTP request and holding a database transaction open across that would be
        worse.
        """
        if self._refresh_claim_connection_factory is None:
            raise RuntimeError("OAuth provider token claim connection is unavailable")
        identity = [str(configuration_id or "").strip(), str(subject or "").strip()]
        if any(not value for value in identity):
            raise ValueError("complete OAuth provider token scope is required")
        lock_key = "extension-oauth-provider-token:" + "".join(
            f"{len(value)}:{value};" for value in identity
        )
        deadline = time.monotonic() + self._refresh_claim_timeout_seconds
        acquired = False
        with self._refresh_claim_connection_factory() as claim_conn:
            try:
                while True:
                    with claim_conn.cursor() as cur:
                        cur.execute(
                            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                            (lock_key,),
                        )
                        row = cur.fetchone()
                    if row and bool(row[0]):
                        acquired = True
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("OAuth provider token claim is busy")
                    time.sleep(0.05)
                yield
            finally:
                if acquired:
                    unlock_ok = False
                    try:
                        with claim_conn.cursor() as cur:
                            cur.execute(
                                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                                (lock_key,),
                            )
                            row = cur.fetchone()
                        unlock_ok = bool(row and row[0])
                    finally:
                        if not unlock_ok:
                            claim_conn.close()
                            raise RuntimeError("OAuth provider token claim release was uncertain")

    def update_oauth_refresh_failure(self, **values: Any) -> dict[str, Any] | None:
        if values["mark_configuration"]:
            with self.conn.cursor() as cur:
                cur.execute(
                    """UPDATE extension_oauth_configurations
                          SET configuration_revision = configuration_revision + 1,
                              status = 'needs_attention',
                              last_error = %s::jsonb,
                              updated_at = now()
                        WHERE configuration_id = %s
                          AND tenant_id = %s
                          AND account_id = %s
                          AND project_id = %s
                          AND extension_install_id = %s
                          AND extension_id = %s
                          AND provider_id = %s
                          AND configuration_revision = %s""",
                    (
                        _json_payload({"code": values["error_code"]}),
                        values["configuration_id"],
                        values["tenant_id"],
                        values["account_id"],
                        values["project_id"],
                        values["extension_install_id"],
                        values["extension_id"],
                        values["provider_id"],
                        values["expected_configuration_revision"],
                    ),
                )
                if int(getattr(cur, "rowcount", 0) or 0) != 1:
                    return None
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections
                      SET credential_revision = credential_revision + 1,
                          connection_config = %s::jsonb,
                          updated_at = now()
                    WHERE connection_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND oauth_configuration_id = %s
                      AND oauth_provider_id = %s
                      AND connection_type = %s
                      AND credential_revision = %s
                 RETURNING connection_id, project_id, extension_id, account_id,
                           display_name, connection_kind, connection_type,
                           credential_revision, extension_install_id,
                           oauth_configuration_id, oauth_provider_id,
                           oauth_extension_release_id, oauth_authorization_revision,
                           oauth_requested_scopes, oauth_granted_scopes,
                           oauth_provider_security_fingerprint,
                           oauth_provider_revocation_fingerprint, enabled,
                           connection_config, secret_refs""",
                (
                    _json_payload(values["connection_config"]),
                    values["connection_id"],
                    values["account_id"],
                    values["project_id"],
                    values["extension_install_id"],
                    values["extension_id"],
                    values["configuration_id"],
                    values["provider_id"],
                    values["connection_type"],
                    values["expected_credential_revision"],
                ),
            )
            return _row_to_dict(cur, cur.fetchone())
