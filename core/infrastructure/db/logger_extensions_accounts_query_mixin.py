# mypy: ignore-errors
from __future__ import annotations

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_extensions_accounts_common import (
    _as_dict,
    _default_account_id,
)


class PlatformLoggerExtensionsAccountsQueryMixin:
    def list_extensions(self, enabled_only: bool = False) -> list[dict]:
        if not self._table_exists("extension_bundles"):
            return []
        where_sql = "WHERE coalesce(ee.enabled, false) = true" if enabled_only else ""
        select_columns = self._extension_base_select_columns()
        select_columns.append(self._enabled_extension_config_select_column())
        select_columns.extend(self._extension_version_select_columns())
        select_columns.append(self._extension_bundle_account_select_column())

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    {", ".join(select_columns)}
                FROM extension_bundles eb
                LEFT JOIN extension_versions ev
                    ON ev.bundle_id = eb.id
                   AND ev.version = eb.latest_version
                LEFT JOIN enabled_extensions ee
                    ON ee.bundle_id = eb.id
                {where_sql}
                ORDER BY eb.kind, eb.extension_key
                """
            )
            rows = cur.fetchall()
        return [self._normalize_extension_row(dict(row)) for row in rows]

    @staticmethod
    def _extension_base_select_columns() -> list[str]:
        return [
            "eb.id AS bundle_id",
            "eb.extension_key",
            "eb.kind",
            "eb.display_name",
            "eb.description",
            "eb.bundle_root",
            "eb.manifest_path",
            "eb.latest_version",
            "eb.status",
            "eb.last_scan_error",
            "ev.id AS version_id",
            "ev.manifest_jsonb",
            "ev.features_jsonb",
            "ev.schemas_jsonb",
            "ev.healthcheck_jsonb",
            "ev.entrypoint_jsonb",
            "ev.checksum",
            "ev.scan_error",
            "ee.enabled",
            "ee.enabled_at",
            "ee.updated_at",
        ]

    def _enabled_extension_config_select_column(self) -> str:
        if self._column_exists("enabled_extensions", "config_jsonb"):
            return "ee.config_jsonb AS config_jsonb"
        return "'{}'::jsonb AS config_jsonb"

    def _extension_bundle_account_select_column(self) -> str:
        if self._column_exists("extension_bundles", "account_id"):
            return "eb.account_id"
        return f"'{_default_account_id()}'::text AS account_id"

    @staticmethod
    def _extension_version_select_specs() -> list[tuple[str, str, str]]:
        return [
            (
                "manifest_version",
                "ev.manifest_version AS manifest_version",
                "0 AS manifest_version",
            ),
            (
                "required_scopes_jsonb",
                "ev.required_scopes_jsonb AS required_scopes_jsonb",
                "'[]'::jsonb AS required_scopes_jsonb",
            ),
            (
                "supported_events_jsonb",
                "ev.supported_events_jsonb AS supported_events_jsonb",
                "'[]'::jsonb AS supported_events_jsonb",
            ),
            (
                "ui_manifest_jsonb",
                "ev.ui_manifest_jsonb AS ui_manifest_jsonb",
                "'{}'::jsonb AS ui_manifest_jsonb",
            ),
            (
                "query_definitions_jsonb",
                "ev.query_definitions_jsonb AS query_definitions_jsonb",
                "'[]'::jsonb AS query_definitions_jsonb",
            ),
            (
                "action_definitions_jsonb",
                "ev.action_definitions_jsonb AS action_definitions_jsonb",
                "'[]'::jsonb AS action_definitions_jsonb",
            ),
            (
                "compiled_manifest_jsonb",
                "ev.compiled_manifest_jsonb AS compiled_manifest_jsonb",
                "'{}'::jsonb AS compiled_manifest_jsonb",
            ),
            (
                "compile_errors_jsonb",
                "ev.compile_errors_jsonb AS compile_errors_jsonb",
                "'[]'::jsonb AS compile_errors_jsonb",
            ),
            (
                "compile_warnings_jsonb",
                "ev.compile_warnings_jsonb AS compile_warnings_jsonb",
                "'[]'::jsonb AS compile_warnings_jsonb",
            ),
            (
                "validation_errors_jsonb",
                "ev.validation_errors_jsonb AS validation_errors_jsonb",
                "'[]'::jsonb AS validation_errors_jsonb",
            ),
            (
                "validation_warnings_jsonb",
                "ev.validation_warnings_jsonb AS validation_warnings_jsonb",
                "'[]'::jsonb AS validation_warnings_jsonb",
            ),
            (
                "machine_auth_jsonb",
                "ev.machine_auth_jsonb AS machine_auth_jsonb",
                "'{}'::jsonb AS machine_auth_jsonb",
            ),
            (
                "account_scope_jsonb",
                "ev.account_scope_jsonb AS account_scope_jsonb",
                "'{}'::jsonb AS account_scope_jsonb",
            ),
        ]

    def _extension_version_select_columns(self) -> list[str]:
        select_columns: list[str] = []
        for column_name, present_sql, fallback_sql in self._extension_version_select_specs():
            if self._column_exists("extension_versions", column_name):
                select_columns.append(present_sql)
            else:
                select_columns.append(fallback_sql)
        return select_columns

    def get_extension(self, extension_key: str, *, account_id: str | None = None) -> dict | None:
        normalized_key = str(extension_key or "").strip()
        if not normalized_key:
            return None
        normalized_account_id = str(account_id or "").strip()
        rows = self.list_extensions(enabled_only=False)
        for row in rows:
            if str(row.get("extension_key") or "").strip() != normalized_key:
                continue
            if (
                normalized_account_id
                and str(row.get("account_id") or "").strip() != normalized_account_id
            ):
                continue
            return row
        return None

    def get_extension_by_install_id(
        self,
        install_id: str,
        *,
        account_id: str | None = None,
    ) -> dict | None:
        """Resolve an executable install; disabled installs are never returned."""
        return self._get_extension_install_by_id(
            install_id,
            account_id=account_id,
            active_only=True,
            include_global=True,
        )

    def get_extension_by_install_id_for_runtime_gate(
        self,
        install_id: str,
        *,
        account_id: str | None = None,
    ) -> dict | None:
        """Resolve active or disabled install metadata for the fail-closed runtime gate."""
        return self._get_extension_install_by_id(
            install_id,
            account_id=account_id,
            active_only=False,
            include_global=True,
        )

    def get_extension_install_for_operator(
        self,
        install_id: str,
        *,
        account_id: str,
    ) -> dict | None:
        """Resolve an account-owned install for configuration, including disabled state."""
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return None
        return self._get_extension_install_by_id(
            install_id,
            account_id=normalized_account_id,
            active_only=False,
            include_global=False,
        )

    def _get_extension_install_by_id(
        self,
        install_id: str,
        *,
        account_id: str | None,
        active_only: bool,
        include_global: bool,
    ) -> dict | None:
        normalized_install_id = str(install_id or "").strip()
        if not normalized_install_id:
            return None
        if not self._table_exists("extension_installs"):
            return None
        normalized_account_id = str(account_id or "").strip()
        has_releases = self._table_exists("extension_releases")
        state_filter = "AND ei.state = 'active'" if active_only else ""
        account_filter = ""
        params: list[str] = [normalized_install_id]
        if normalized_account_id:
            account_filter = (
                "AND (ei.account_id = %s OR ei.visibility = 'global')"
                if include_global
                else "AND ei.account_id = %s"
            )
            params.append(normalized_account_id)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            try:
                if has_releases:
                    cur.execute(
                        f"""
                        SELECT
                            ei.extension_id,
                            ei.account_id AS install_account_id,
                            ei.visibility,
                            ei.state,
                            ei.install_id,
                            ei.release_id,
                            ei.config_jsonb,
                            ei.runtime_mode,
                            ei.remote_base_url,
                            ei.handshake_status,
                            ei.health_status,
                            ei.runtime_status,
                            ei.last_error,
                            er.extension_id AS release_extension_id,
                            er.descriptor_jsonb,
                            er.descriptor_sha256
                        FROM extension_installs ei
                        LEFT JOIN extension_releases er ON er.release_id = ei.release_id
                        WHERE ei.install_id = %s
                          {state_filter}
                          {account_filter}
                        LIMIT 1
                        """,
                        params,
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT extension_id, account_id AS install_account_id, visibility,
                               state, install_id, release_id, config_jsonb
                        FROM extension_installs ei
                        WHERE ei.install_id = %s
                          {state_filter}
                          {account_filter}
                        LIMIT 1
                        """,
                        params,
                    )
                row = cur.fetchone() or {}
            except Exception:
                return None
        extension_key = str((row or {}).get("extension_id") or "").strip()
        if not extension_key:
            return None
        if has_releases and str((row or {}).get("release_id") or "").strip():
            release_extension_id = str((row or {}).get("release_extension_id") or "").strip()
            if release_extension_id != extension_key:
                return None
        descriptor = _as_dict((row or {}).get("descriptor_jsonb"))
        if descriptor:
            descriptor_extension_id = str(descriptor.get("extension_id") or "").strip()
            if descriptor_extension_id != extension_key:
                return None
            runtime_account_id = (
                normalized_account_id or str((row or {}).get("install_account_id") or "").strip()
            )
            return self._normalize_extension_row(
                {
                    "extension_key": extension_key,
                    "extension_id": extension_key,
                    "account_id": runtime_account_id,
                    "install_id": str((row or {}).get("install_id") or "").strip(),
                    "release_id": str((row or {}).get("release_id") or "").strip(),
                    "descriptor_sha256": str((row or {}).get("descriptor_sha256") or "").strip(),
                    "config_jsonb": (row or {}).get("config_jsonb"),
                    "bundle_root": str(descriptor.get("bundle_root") or "").strip(),
                    "runtime_mode": str((row or {}).get("runtime_mode") or "").strip(),
                    "remote_base_url": str((row or {}).get("remote_base_url") or "").strip(),
                    "handshake_status": str((row or {}).get("handshake_status") or "").strip(),
                    "health_status": str((row or {}).get("health_status") or "").strip(),
                    "runtime_status": str((row or {}).get("runtime_status") or "").strip(),
                    "last_error": str((row or {}).get("last_error") or "").strip(),
                    "enabled": str((row or {}).get("state") or "").strip() == "active",
                    "manifest": descriptor,
                    "compiled_manifest": descriptor,
                    "entrypoint": descriptor.get("entrypoint"),
                    "action_definitions": descriptor.get("action_definitions"),
                    "query_definitions": descriptor.get("query_definitions"),
                    "ui_manifest": descriptor.get("ui_manifest"),
                }
            )
        if str((row or {}).get("release_id") or "").strip():
            return None
        if (
            normalized_account_id
            and str((row or {}).get("install_account_id") or "").strip() != normalized_account_id
        ):
            return None
        return self.get_extension(extension_key, account_id=normalized_account_id or None)
