"""Caller-transaction-owned OAuth configuration persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.oauth_repository_values import _json_payload, _row_to_dict

_CREATE_CONFIGURATION_SQL = """
    INSERT INTO extension_oauth_configurations (
        configuration_id, tenant_id, account_id, project_id,
        extension_install_id, extension_id, extension_release_id, provider_id,
        display_name, configuration_revision, authorization_revision,
        status, non_secret_config, secret_refs, provider_security_fingerprint,
        provider_revocation_fingerprint, last_error
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s::jsonb, %s::jsonb, %s, %s, %s::jsonb
    ) RETURNING
        configuration_id, tenant_id, account_id, project_id,
        extension_install_id, extension_id, extension_release_id, provider_id, display_name,
        configuration_revision, authorization_revision, status,
        non_secret_config, secret_refs, provider_security_fingerprint,
        provider_revocation_fingerprint, last_error, created_at, updated_at
"""

_LIST_CONFIGURATIONS_SQL = """
    SELECT configuration_id, tenant_id, account_id, project_id,
           extension_install_id, extension_id, extension_release_id, provider_id, display_name,
           configuration_revision, authorization_revision, status,
           non_secret_config, secret_refs, provider_security_fingerprint,
           provider_revocation_fingerprint, last_error, created_at, updated_at
    FROM extension_oauth_configurations
    WHERE tenant_id = %s
      AND account_id = %s
      AND project_id = %s
      AND extension_install_id = %s
      AND extension_id = %s
      AND provider_id = %s
    ORDER BY display_name, created_at, configuration_id
"""

_GET_CONFIGURATION_SQL = """
    SELECT configuration_id, tenant_id, account_id, project_id,
           extension_install_id, extension_id, extension_release_id, provider_id, display_name,
           configuration_revision, authorization_revision, status,
           non_secret_config, secret_refs, provider_security_fingerprint,
           provider_revocation_fingerprint, last_error, created_at, updated_at
    FROM extension_oauth_configurations
    WHERE configuration_id = %s
      AND tenant_id = %s
      AND account_id = %s
      AND project_id = %s
      AND extension_install_id = %s
      AND extension_id = %s
      AND provider_id = %s
"""

_LOCK_CONFIGURATION_SQL = _GET_CONFIGURATION_SQL + " FOR UPDATE"

_UPDATE_CONFIGURATION_SQL = """
    UPDATE extension_oauth_configurations
    SET extension_release_id = %s,
        display_name = %s,
        configuration_revision = %s,
        authorization_revision = %s,
        status = %s,
        non_secret_config = %s::jsonb,
        secret_refs = %s::jsonb,
        provider_security_fingerprint = %s,
        provider_revocation_fingerprint = %s,
        last_error = %s::jsonb,
        updated_at = now()
    WHERE configuration_id = %s
      AND tenant_id = %s
      AND account_id = %s
      AND project_id = %s
      AND extension_install_id = %s
      AND extension_id = %s
      AND provider_id = %s
      AND configuration_revision = %s
    RETURNING configuration_id, tenant_id, account_id, project_id,
              extension_install_id, extension_id, extension_release_id, provider_id, display_name,
              configuration_revision, authorization_revision, status,
              non_secret_config, secret_refs, provider_security_fingerprint,
              provider_revocation_fingerprint, last_error, created_at, updated_at
"""


class OAuthConfigurationRepositoryMixin:
    """Uses the public repository's supplied connection without owning transactions."""

    conn: Any

    def create_configuration(
        self,
        *,
        configuration_id: Any,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        extension_release_id: str,
        provider_id: str,
        display_name: str,
        configuration_revision: int,
        authorization_revision: int,
        status: str,
        non_secret_config: Mapping[str, Any],
        secret_refs: Mapping[str, Any],
        provider_security_fingerprint: str,
        provider_revocation_fingerprint: str,
        last_error: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                _CREATE_CONFIGURATION_SQL,
                (
                    configuration_id,
                    tenant_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    extension_release_id,
                    provider_id,
                    display_name,
                    configuration_revision,
                    authorization_revision,
                    status,
                    _json_payload(non_secret_config),
                    _json_payload(secret_refs),
                    provider_security_fingerprint,
                    provider_revocation_fingerprint,
                    _json_payload(last_error),
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def list_configurations(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
    ) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                _LIST_CONFIGURATIONS_SQL,
                (
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

    def get_configuration_for_scope(
        self,
        *,
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
                _GET_CONFIGURATION_SQL,
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
            return _row_to_dict(cur, cur.fetchone())

    def list_workflow_steps_bound_to_connections(
        self, *, account_id: Any, connection_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Workflow steps of this account whose binding names one of these connections."""
        if not connection_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT workflow.workflow_id, workflow.display_name AS workflow_name,
                          grp.display_name AS group_name, step.step_id,
                          step.display_name AS step_name, bound.connection_id
                     FROM registry_workflow_steps AS step
                     JOIN registry_workflows AS workflow
                       ON workflow.workflow_id = step.owner_workflow_id
                      AND workflow.deleted_at IS NULL
                     LEFT JOIN registry_workflow_groups AS grp ON grp.id = step.group_id
                    CROSS JOIN LATERAL (
                         SELECT COALESCE(
                                    NULLIF(step.connection_binding_jsonb->>'connection_id', ''),
                                    step.override->'connector'->'connection_binding'
                                        ->>'connection_id'
                                ) AS connection_id
                    ) AS bound
                    WHERE workflow.account_id = %s
                      AND bound.connection_id = ANY(%s)
                 ORDER BY workflow.display_name, step.sort_order, step.step_id""",
                (account_id, list(connection_ids)),
            )
            return [
                payload for row in cur.fetchall() if (payload := _row_to_dict(cur, row)) is not None
            ]

    def repin_connections_to_release(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
        extension_release_id: str,
    ) -> int:
        """Pin one configuration's connections to the installed release."""
        del tenant_id
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections
                      SET oauth_extension_release_id = %s, updated_at = now()
                    WHERE oauth_configuration_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND oauth_provider_id = %s""",
                (
                    extension_release_id,
                    configuration_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    provider_id,
                ),
            )
            return int(cur.rowcount or 0)

    def get_configuration_for_scope_for_update(self, **scope: Any) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                _LOCK_CONFIGURATION_SQL,
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
            return _row_to_dict(cur, cur.fetchone())

    def update_configuration(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
        expected_configuration_revision: int,
        extension_release_id: str,
        display_name: str,
        configuration_revision: int,
        authorization_revision: int,
        status: str,
        non_secret_config: Mapping[str, Any],
        secret_refs: Mapping[str, Any],
        provider_security_fingerprint: str,
        provider_revocation_fingerprint: str,
        last_error: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                _UPDATE_CONFIGURATION_SQL,
                (
                    extension_release_id,
                    display_name,
                    configuration_revision,
                    authorization_revision,
                    status,
                    _json_payload(non_secret_config),
                    _json_payload(secret_refs),
                    provider_security_fingerprint,
                    provider_revocation_fingerprint,
                    _json_payload(last_error),
                    configuration_id,
                    tenant_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    provider_id,
                    expected_configuration_revision,
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def delete_configuration(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
        expected_configuration_revision: int,
    ) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """DELETE FROM extension_oauth_configurations
                    WHERE configuration_id = %s
                      AND tenant_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND provider_id = %s
                      AND configuration_revision = %s""",
                (
                    configuration_id,
                    tenant_id,
                    account_id,
                    project_id,
                    extension_install_id,
                    extension_id,
                    provider_id,
                    expected_configuration_revision,
                ),
            )
            return getattr(cur, "rowcount", 0) == 1

    def invalidate_pending_states(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE extension_oauth_authorization_states
                      SET consumed_at = now(), updated_at = now()
                    WHERE configuration_id = %s
                      AND tenant_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND provider_id = %s
                      AND consumed_at IS NULL""",
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
            return int(getattr(cur, "rowcount", 0) or 0)

    def mark_connections_reauthorization_required(self, **scope: Any) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE project_connections AS connection
                      SET connection_config =
                          COALESCE(connection.connection_config, '{}'::jsonb)
                          || jsonb_build_object(
                              'oauth',
                              (
                                  CASE
                                      WHEN jsonb_typeof(connection.connection_config->'oauth')
                                           = 'object'
                                      THEN connection.connection_config->'oauth'
                                      ELSE '{}'::jsonb
                                  END
                              ) || jsonb_build_object(
                                  'lifecycle_status', 'authorization_required'
                              )
                          ),
                          updated_at = now()
                     FROM extension_oauth_configurations AS configuration
                    WHERE configuration.configuration_id = %s
                      AND configuration.tenant_id = %s
                      AND configuration.account_id = %s
                      AND configuration.project_id = %s
                      AND configuration.extension_install_id = %s
                      AND configuration.extension_id = %s
                      AND configuration.provider_id = %s
                      AND connection.oauth_configuration_id = configuration.configuration_id
                      AND connection.account_id = configuration.account_id
                      AND connection.project_id = configuration.project_id
                      AND connection.extension_install_id = configuration.extension_install_id
                      AND connection.extension_id = configuration.extension_id
                      AND connection.oauth_provider_id = configuration.provider_id
                      AND COALESCE(
                          connection.connection_config->'oauth'->>'lifecycle_status', ''
                      ) <> 'disconnecting'""",
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
            return int(getattr(cur, "rowcount", 0) or 0)

    def delete_connections_for_configuration(self, **scope: Any) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """DELETE FROM project_connections AS connection
                      USING extension_oauth_configurations AS configuration
                     WHERE configuration.configuration_id = %s
                       AND configuration.tenant_id = %s
                       AND configuration.account_id = %s
                       AND configuration.project_id = %s
                       AND configuration.extension_install_id = %s
                       AND configuration.extension_id = %s
                       AND configuration.provider_id = %s
                       AND connection.oauth_configuration_id = configuration.configuration_id
                       AND connection.account_id = configuration.account_id
                       AND connection.project_id = configuration.project_id
                       AND connection.extension_install_id = configuration.extension_install_id
                       AND connection.extension_id = configuration.extension_id
                       AND connection.oauth_provider_id = configuration.provider_id""",
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
            return int(getattr(cur, "rowcount", 0) or 0)

    def mark_configuration_verified(self, **scope: Any) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE extension_oauth_configurations
                      SET status = 'verified', last_error = '{}'::jsonb, updated_at = now()
                    WHERE configuration_id = %s
                      AND tenant_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND provider_id = %s""",
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
            return int(getattr(cur, "rowcount", 0) or 0) == 1

    def mark_configuration_invalid_client(self, **scope: Any) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE extension_oauth_configurations
                      SET status = 'needs_attention',
                          configuration_revision = configuration_revision + 1,
                          last_error = '{"code":"invalid_client"}'::jsonb,
                          updated_at = now()
                    WHERE configuration_id = %s
                      AND tenant_id = %s
                      AND account_id = %s
                      AND project_id = %s
                      AND extension_install_id = %s
                      AND extension_id = %s
                      AND provider_id = %s
                      AND configuration_revision = %s
                RETURNING configuration_id, tenant_id, account_id, project_id,
                          extension_install_id, extension_id, extension_release_id,
                          provider_id, display_name, configuration_revision,
                          authorization_revision, status, non_secret_config,
                          secret_refs, provider_security_fingerprint,
                          provider_revocation_fingerprint, last_error,
                          created_at, updated_at""",
                (
                    scope["configuration_id"],
                    scope["tenant_id"],
                    scope["account_id"],
                    scope["project_id"],
                    scope["extension_install_id"],
                    scope["extension_id"],
                    scope["provider_id"],
                    scope["expected_configuration_revision"],
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def lock_oauth_install_for_update(self, **scope: Any) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT install_id, account_id, extension_id, release_id, state
                     FROM extension_installs
                    WHERE install_id = %s
                      AND account_id = %s
                      AND extension_id = %s
                    FOR UPDATE""",
                (
                    scope["extension_install_id"],
                    scope["account_id"],
                    scope["extension_id"],
                ),
            )
            return _row_to_dict(cur, cur.fetchone())
