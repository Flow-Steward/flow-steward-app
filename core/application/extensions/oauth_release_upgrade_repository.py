"""PostgreSQL persistence for approved OAuth release transitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.runtime_store_common import _row_to_dict
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql


def _text(value: Any) -> str:
    return str(value or "").strip()


class PostgresOAuthReleaseUpgradeRepository:
    """Lock and mutate exact install-owned OAuth release records."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def read_oauth_release_inventory(
        self, *, account_id: str, install_id: str, extension_id: str, old_release_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self._load_inventory(
            account_id=account_id,
            install_id=install_id,
            extension_id=extension_id,
            old_release_id=old_release_id,
            lock=False,
        )

    def lock_oauth_release_inventory(
        self, *, account_id: str, install_id: str, extension_id: str, old_release_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self._load_inventory(
            account_id=account_id,
            install_id=install_id,
            extension_id=extension_id,
            old_release_id=old_release_id,
            lock=True,
        )

    def _load_inventory(
        self,
        *,
        account_id: str,
        install_id: str,
        extension_id: str,
        old_release_id: str,
        lock: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        lock_sql = psycopg_sql.SQL(" FOR UPDATE") if lock else psycopg_sql.SQL("")
        with self._connection.cursor() as cursor:
            cursor.execute(
                psycopg_sql.SQL(
                    """SELECT configuration_id, provider_id
                     FROM extension_oauth_configurations
                    WHERE account_id = %s AND extension_install_id = %s AND extension_id = %s
                      AND extension_release_id = %s
                 ORDER BY configuration_id"""
                )
                + lock_sql,
                (account_id, install_id, extension_id, old_release_id),
            )
            configurations = [_row_to_dict(row, cursor.description) for row in cursor.fetchall()]
            configuration_by_id = {
                _text(row.get("configuration_id")): row for row in configurations
            }
            for row in configurations:
                row["pending_states"] = []
            if not configuration_by_id:
                return configurations, []
            self._append_pending_states(
                cursor,
                configurations=configuration_by_id,
                account_id=account_id,
                install_id=install_id,
                extension_id=extension_id,
                old_release_id=old_release_id,
                lock_sql=lock_sql,
            )
            cursor.execute(
                psycopg_sql.SQL(
                    """SELECT connection_id, oauth_configuration_id AS configuration_id,
                          oauth_provider_id AS provider_id, connection_type,
                          oauth_granted_scopes AS granted_scopes, enabled,
                          connection_config->'oauth'->>'lifecycle_status' AS lifecycle_status
                     FROM project_connections
                    WHERE account_id = %s AND extension_install_id = %s AND extension_id = %s
                      AND oauth_configuration_id = ANY(%s)
                 ORDER BY connection_id"""
                )
                + lock_sql,
                (account_id, install_id, extension_id, list(configuration_by_id)),
            )
            connections = [_row_to_dict(row, cursor.description) for row in cursor.fetchall()]
        return configurations, connections

    @staticmethod
    def _append_pending_states(cursor: Any, **scope: Any) -> None:
        cursor.execute(
            psycopg_sql.SQL(
                """SELECT state_hash, configuration_id, connection_type
                 FROM extension_oauth_authorization_states
                WHERE account_id = %s AND extension_install_id = %s AND extension_id = %s
                  AND extension_release_id = %s AND consumed_at IS NULL
             ORDER BY state_hash"""
            )
            + scope["lock_sql"],
            (
                scope["account_id"],
                scope["install_id"],
                scope["extension_id"],
                scope["old_release_id"],
            ),
        )
        states = [_row_to_dict(row, cursor.description) for row in cursor.fetchall()]
        for state in states:
            configuration = scope["configurations"].get(_text(state.get("configuration_id")))
            if configuration is not None:
                configuration["pending_states"].append(
                    {
                        "state_id": _text(state.get("state_hash")),
                        "connection_type": _text(state.get("connection_type")),
                    }
                )

    def apply_oauth_release_impact(self, **values: Any) -> None:
        with self._connection.cursor() as cursor:
            self._invalidate_states(cursor, values)
            self._mark_connections(cursor, values)
            self._advance_connection_contract_pins(cursor, values)
            self._update_configurations(cursor, values)

    @staticmethod
    def _invalidate_states(cursor: Any, values: Mapping[str, Any]) -> None:
        state_ids = tuple(values.get("invalidate_pending_state_ids") or ())
        if not state_ids:
            return
        cursor.execute(
            """UPDATE extension_oauth_authorization_states
                  SET consumed_at = COALESCE(consumed_at, now()), updated_at = now()
                WHERE account_id = %s AND extension_install_id = %s AND extension_id = %s
                  AND extension_release_id = %s AND state_hash = ANY(%s)
                  AND consumed_at IS NULL""",
            (
                values["account_id"],
                values["install_id"],
                values["extension_id"],
                values["old_release_id"],
                list(state_ids),
            ),
        )
        if int(cursor.rowcount or 0) != len(state_ids):
            raise RuntimeError("oauth_release_pending_state_drift")

    @staticmethod
    def _mark_connections(cursor: Any, values: Mapping[str, Any]) -> None:
        connection_ids = tuple(values.get("reauthorize_connection_ids") or ())
        if not connection_ids:
            return
        cursor.execute(
            """UPDATE project_connections
                  SET connection_config = COALESCE(connection_config, '{}'::jsonb)
                      || jsonb_build_object(
                          'oauth',
                          (CASE WHEN jsonb_typeof(connection_config->'oauth') = 'object'
                                THEN connection_config->'oauth' ELSE '{}'::jsonb END)
                          || jsonb_build_object('lifecycle_status', 'authorization_required')
                      ), updated_at = now()
                WHERE account_id = %s AND extension_install_id = %s AND extension_id = %s
                  AND connection_id = ANY(%s)""",
            (
                values["account_id"],
                values["install_id"],
                values["extension_id"],
                list(connection_ids),
            ),
        )
        if int(cursor.rowcount or 0) != len(connection_ids):
            raise RuntimeError("oauth_release_connection_drift")

    @staticmethod
    def _advance_connection_contract_pins(cursor: Any, values: Mapping[str, Any]) -> None:
        for provider_id, fingerprint_pair in sorted(values["provider_fingerprints"].items()):
            security_fingerprint, revocation_fingerprint = fingerprint_pair
            cursor.execute(
                """UPDATE project_connections AS connection
                      SET oauth_extension_release_id = %s,
                          oauth_provider_security_fingerprint = %s,
                          oauth_provider_revocation_fingerprint = %s,
                          updated_at = now()
                     FROM extension_oauth_configurations AS configuration
                    WHERE configuration.configuration_id = connection.oauth_configuration_id
                      AND configuration.account_id = connection.account_id
                      AND configuration.project_id = connection.project_id
                      AND configuration.extension_install_id = connection.extension_install_id
                      AND configuration.extension_id = connection.extension_id
                      AND configuration.provider_id = connection.oauth_provider_id
                      AND configuration.account_id = %s
                      AND configuration.extension_install_id = %s
                      AND configuration.extension_id = %s
                      AND configuration.provider_id = %s
                      AND (configuration.extension_release_id = %s
                           OR (configuration.extension_release_id <> %s
                               AND configuration.provider_security_fingerprint = %s
                               AND configuration.provider_revocation_fingerprint = %s))""",
                (
                    values["new_release_id"],
                    security_fingerprint,
                    revocation_fingerprint,
                    values["account_id"],
                    values["install_id"],
                    values["extension_id"],
                    provider_id,
                    values["old_release_id"],
                    values["new_release_id"],
                    security_fingerprint,
                    revocation_fingerprint,
                ),
            )

    # A setup left on an even earlier release (one whose update did not move it,
    # e.g. a hand-adopted bundle) used to be skipped by every later update, so
    # authorization stayed refused. It is moved too when it signs in exactly as
    # the new release does; any other difference still needs approval.
    @staticmethod
    def _update_configurations(cursor: Any, values: Mapping[str, Any]) -> None:
        for provider_id, fingerprint_pair in sorted(values["provider_fingerprints"].items()):
            security_fingerprint, revocation_fingerprint = fingerprint_pair
            cursor.execute(
                """UPDATE extension_oauth_configurations
                      SET extension_release_id = %s,
                          provider_security_fingerprint = %s,
                          provider_revocation_fingerprint = %s,
                          updated_at = now()
                    WHERE account_id = %s AND extension_install_id = %s AND extension_id = %s
                      AND provider_id = %s
                      AND (extension_release_id = %s
                           OR (extension_release_id <> %s
                               AND provider_security_fingerprint = %s
                               AND provider_revocation_fingerprint = %s))""",
                (
                    values["new_release_id"],
                    security_fingerprint,
                    revocation_fingerprint,
                    values["account_id"],
                    values["install_id"],
                    values["extension_id"],
                    provider_id,
                    values["old_release_id"],
                    values["new_release_id"],
                    security_fingerprint,
                    revocation_fingerprint,
                ),
            )
