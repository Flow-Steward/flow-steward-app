# mypy: ignore-errors
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_extensions_accounts_common import (
    ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL,
    REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL,
    _as_dict,
    _as_list,
    _coerce_int,
    _default_account_id,
    _normalize_string_list,
)


class PlatformLoggerExtensionsAccountsSyncMixin:
    def extension_lifecycle_transaction(self):
        """Expose the technical transaction boundary used by application coordination."""
        from core.infrastructure.db.db_pool import transaction_connection

        return transaction_connection(self.conn)

    def sync_extension_scan_records(
        self,
        transaction_conn,
        scanned_extensions: list[dict] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        if not (
            self._table_exists("extension_bundles") and self._table_exists("extension_versions")
        ):
            return [], []

        has_bundle_account_id = self._column_exists("extension_bundles", "account_id")
        has_bundle_manifest_version = self._column_exists("extension_bundles", "manifest_version")
        has_bundle_compile_status = self._column_exists(
            "extension_bundles", "latest_compile_status"
        )
        has_bundle_validation_status = self._column_exists(
            "extension_bundles", "latest_validation_status"
        )

        has_version_manifest_version = self._column_exists("extension_versions", "manifest_version")
        has_version_required_scopes = self._column_exists(
            "extension_versions", "required_scopes_jsonb"
        )
        has_version_supported_events = self._column_exists(
            "extension_versions", "supported_events_jsonb"
        )
        has_version_ui_manifest = self._column_exists("extension_versions", "ui_manifest_jsonb")
        has_version_query_defs = self._column_exists(
            "extension_versions", "query_definitions_jsonb"
        )
        has_version_action_defs = self._column_exists(
            "extension_versions", "action_definitions_jsonb"
        )
        has_version_compiled_manifest = self._column_exists(
            "extension_versions", "compiled_manifest_jsonb"
        )
        has_version_compile_errors = self._column_exists(
            "extension_versions", "compile_errors_jsonb"
        )
        has_version_compile_warnings = self._column_exists(
            "extension_versions", "compile_warnings_jsonb"
        )
        has_version_validation_errors = self._column_exists(
            "extension_versions", "validation_errors_jsonb"
        )
        has_version_validation_warnings = self._column_exists(
            "extension_versions", "validation_warnings_jsonb"
        )
        has_version_machine_auth = self._column_exists("extension_versions", "machine_auth_jsonb")
        has_version_account_scope = self._column_exists("extension_versions", "account_scope_jsonb")
        has_enabled_extensions = self._table_exists("enabled_extensions")
        if has_bundle_account_id and (
            not self._table_exists("accounts") or not self.get_account(_default_account_id())
        ):
            return [], []
        scanned_extension_keys = {
            str(item.get("extension_key") or "").strip()
            for item in scanned_extensions or []
            if isinstance(item, dict) and str(item.get("extension_key") or "").strip()
        }

        rows: list[dict] = []
        lifecycle_changes: list[dict] = []
        with transaction_conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            for item in scanned_extensions or []:
                if not isinstance(item, dict):
                    continue
                extension_key = str(item.get("extension_key") or "").strip()
                kind = str(item.get("kind") or "").strip()
                status = str(item.get("status") or "invalid").strip() or "invalid"
                if extension_key and status != "valid":
                    lifecycle_changes.append({"kind": "invalid", "extension_id": extension_key})
                if not extension_key or not kind:
                    continue
                display_name = str(item.get("display_name") or extension_key).strip()
                description = str(item.get("description") or "").strip()
                bundle_root = str(item.get("bundle_root") or "").strip()
                manifest_path = str(item.get("manifest_path") or "").strip()
                version = str(item.get("version") or "").strip() or None
                scan_error = str(item.get("scan_error") or "").strip() or None

                compile_errors = _as_list(item.get("compile_errors"))
                validation_errors = _as_list(item.get("validation_errors"))
                lifecycle = _as_dict(item.get("lifecycle"))
                compile_status = (
                    "failed" if compile_errors else str(lifecycle.get("compile") or "ok")
                )
                validation_status = (
                    "failed" if validation_errors else str(lifecycle.get("validate") or "ok")
                )

                bundle_columns = [
                    "extension_key",
                    "kind",
                    "display_name",
                    "description",
                    "bundle_root",
                    "manifest_path",
                    "latest_version",
                    "status",
                    "last_scan_error",
                ]
                bundle_values: list[Any] = [
                    extension_key,
                    kind,
                    display_name,
                    description,
                    bundle_root,
                    manifest_path,
                    version,
                    status
                    if status in {"discovered", "valid", "invalid", "disabled"}
                    else "invalid",
                    scan_error,
                ]
                bundle_updates = [
                    "kind = EXCLUDED.kind",
                    "display_name = EXCLUDED.display_name",
                    "description = EXCLUDED.description",
                    "bundle_root = EXCLUDED.bundle_root",
                    "manifest_path = EXCLUDED.manifest_path",
                    "latest_version = EXCLUDED.latest_version",
                    "status = EXCLUDED.status",
                    "last_scan_error = EXCLUDED.last_scan_error",
                ]
                if has_bundle_account_id:
                    bundle_columns.append("account_id")
                    bundle_values.append(
                        str(item.get("account_id") or _default_account_id()).strip()
                        or _default_account_id()
                    )
                    bundle_updates.append(ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL)
                if has_bundle_manifest_version:
                    bundle_columns.append("manifest_version")
                    bundle_values.append(_coerce_int(item.get("manifest_version"), 0))
                    bundle_updates.append("manifest_version = EXCLUDED.manifest_version")
                if has_bundle_compile_status:
                    bundle_columns.append("latest_compile_status")
                    bundle_values.append(str(compile_status or "unknown").strip() or "unknown")
                    bundle_updates.append("latest_compile_status = EXCLUDED.latest_compile_status")
                if has_bundle_validation_status:
                    bundle_columns.append("latest_validation_status")
                    bundle_values.append(str(validation_status or "unknown").strip() or "unknown")
                    bundle_updates.append(
                        "latest_validation_status = EXCLUDED.latest_validation_status"
                    )

                cur.execute(
                    f"""
                    INSERT INTO extension_bundles (
                        {", ".join(bundle_columns)}
                    ) VALUES ({", ".join(["%s"] * len(bundle_columns))})
                    ON CONFLICT (extension_key) DO UPDATE SET
                        {", ".join(bundle_updates)}
                    RETURNING id
                    """,
                    tuple(bundle_values),
                )
                bundle_row = cur.fetchone()
                bundle_id = str(bundle_row["id"]).strip()
                if version:
                    version_columns = [
                        "bundle_id",
                        "version",
                        "manifest_jsonb",
                        "features_jsonb",
                        "schemas_jsonb",
                        "healthcheck_jsonb",
                        "entrypoint_jsonb",
                        "checksum",
                        "status",
                        "scan_error",
                    ]
                    version_values: list[Any] = [
                        bundle_id,
                        version,
                        json.dumps(_as_dict(item.get("manifest"))),
                        json.dumps(_normalize_string_list(item.get("features"))),
                        json.dumps(_as_dict(item.get("schemas"))),
                        json.dumps(_as_dict(item.get("healthcheck"))),
                        json.dumps(_as_dict(item.get("entrypoint"))),
                        str(item.get("checksum") or "").strip() or None,
                        "valid" if status == "valid" else "invalid",
                        scan_error,
                    ]
                    version_updates = [
                        "manifest_jsonb = EXCLUDED.manifest_jsonb",
                        "features_jsonb = EXCLUDED.features_jsonb",
                        "schemas_jsonb = EXCLUDED.schemas_jsonb",
                        "healthcheck_jsonb = EXCLUDED.healthcheck_jsonb",
                        "entrypoint_jsonb = EXCLUDED.entrypoint_jsonb",
                        "checksum = EXCLUDED.checksum",
                        "status = EXCLUDED.status",
                        "scan_error = EXCLUDED.scan_error",
                        "scanned_at = now()",
                    ]
                    if has_version_manifest_version:
                        version_columns.append("manifest_version")
                        version_values.append(_coerce_int(item.get("manifest_version"), 0))
                        version_updates.append("manifest_version = EXCLUDED.manifest_version")
                    if has_version_required_scopes:
                        version_columns.append("required_scopes_jsonb")
                        version_values.append(
                            json.dumps(_normalize_string_list(item.get("required_scopes")))
                        )
                        version_updates.append(
                            REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL
                        )
                    if has_version_supported_events:
                        version_columns.append("supported_events_jsonb")
                        version_values.append(
                            json.dumps(_normalize_string_list(item.get("supported_events")))
                        )
                        version_updates.append(
                            "supported_events_jsonb = EXCLUDED.supported_events_jsonb"
                        )
                    if has_version_ui_manifest:
                        version_columns.append("ui_manifest_jsonb")
                        version_values.append(json.dumps(_as_dict(item.get("ui_manifest"))))
                        version_updates.append("ui_manifest_jsonb = EXCLUDED.ui_manifest_jsonb")
                    if has_version_query_defs:
                        version_columns.append("query_definitions_jsonb")
                        version_values.append(json.dumps(_as_list(item.get("query_definitions"))))
                        version_updates.append(
                            "query_definitions_jsonb = EXCLUDED.query_definitions_jsonb"
                        )
                    if has_version_action_defs:
                        version_columns.append("action_definitions_jsonb")
                        version_values.append(json.dumps(_as_list(item.get("action_definitions"))))
                        version_updates.append(
                            "action_definitions_jsonb = EXCLUDED.action_definitions_jsonb"
                        )
                    if has_version_compiled_manifest:
                        version_columns.append("compiled_manifest_jsonb")
                        version_values.append(json.dumps(_as_dict(item.get("compiled_manifest"))))
                        version_updates.append(
                            "compiled_manifest_jsonb = EXCLUDED.compiled_manifest_jsonb"
                        )
                    if has_version_compile_errors:
                        version_columns.append("compile_errors_jsonb")
                        version_values.append(json.dumps(_as_list(item.get("compile_errors"))))
                        version_updates.append(
                            "compile_errors_jsonb = EXCLUDED.compile_errors_jsonb"
                        )
                    if has_version_compile_warnings:
                        version_columns.append("compile_warnings_jsonb")
                        version_values.append(json.dumps(_as_list(item.get("compile_warnings"))))
                        version_updates.append(
                            "compile_warnings_jsonb = EXCLUDED.compile_warnings_jsonb"
                        )
                    if has_version_validation_errors:
                        version_columns.append("validation_errors_jsonb")
                        version_values.append(json.dumps(_as_list(item.get("validation_errors"))))
                        version_updates.append(
                            "validation_errors_jsonb = EXCLUDED.validation_errors_jsonb"
                        )
                    if has_version_validation_warnings:
                        version_columns.append("validation_warnings_jsonb")
                        version_values.append(json.dumps(_as_list(item.get("validation_warnings"))))
                        version_updates.append(
                            "validation_warnings_jsonb = EXCLUDED.validation_warnings_jsonb"
                        )
                    if has_version_machine_auth:
                        version_columns.append("machine_auth_jsonb")
                        version_values.append(json.dumps(_as_dict(item.get("machine_auth"))))
                        version_updates.append("machine_auth_jsonb = EXCLUDED.machine_auth_jsonb")
                    if has_version_account_scope:
                        version_columns.append("account_scope_jsonb")
                        version_values.append(json.dumps(_as_dict(item.get("account_scope"))))
                        version_updates.append("account_scope_jsonb = EXCLUDED.account_scope_jsonb")

                    cur.execute(
                        f"""
                        INSERT INTO extension_versions (
                            {", ".join(version_columns)}
                        ) VALUES ({", ".join(["%s"] * len(version_columns))})
                        ON CONFLICT (bundle_id, version) DO UPDATE SET
                            {", ".join(version_updates)}
                        RETURNING id
                        """,
                        tuple(version_values),
                    )
                    version_row = cur.fetchone() or {}
                    version_id = str(version_row.get("id") or "").strip()
                    if status == "valid" and version_id:
                        release_change = self._sync_extension_to_extension_install(
                            cur,
                            extension_key=extension_key,
                            bundle_id=bundle_id,
                            version_id=version_id,
                            version_value=version,
                            enabled=False,
                            config={
                                **_as_dict(item.get("config")),
                                "account_id": str(
                                    item.get("account_id") or _default_account_id()
                                ).strip()
                                or _default_account_id(),
                            },
                            preserve_existing_state=True,
                        )
                        if release_change:
                            lifecycle_changes.append(release_change)
                rows.append(item)

            cur.execute(
                """
                SELECT extension_key, bundle_root, manifest_path
                FROM extension_bundles
                """
            )
            existing_rows = cur.fetchall() or []
            for existing in existing_rows:
                payload = _as_dict(existing)
                extension_key = str(payload.get("extension_key") or "").strip()
                if not extension_key or extension_key in scanned_extension_keys:
                    continue
                bundle_root = str(payload.get("bundle_root") or "").strip()
                manifest_path = str(payload.get("manifest_path") or "").strip()
                if not bundle_root or not manifest_path:
                    continue

                try:
                    bundle_exists = Path(bundle_root).is_dir()
                    manifest_exists = Path(manifest_path).is_file()
                except Exception:
                    bundle_exists = False
                    manifest_exists = False
                if bundle_exists and manifest_exists:
                    continue

                missing_message = (
                    "Extension bundle path is missing on disk; disabled by extension scan"
                )
                stale_updates = [
                    "status = 'disabled'",
                    "last_scan_error = %s",
                    "updated_at = now()",
                ]
                stale_params: list[Any] = [missing_message, extension_key]
                if has_bundle_compile_status:
                    stale_updates.append("latest_compile_status = 'failed'")
                if has_bundle_validation_status:
                    stale_updates.append("latest_validation_status = 'failed'")
                cur.execute(
                    f"""
                    UPDATE extension_bundles
                    SET {", ".join(stale_updates)}
                    WHERE extension_key = %s
                    """,
                    tuple(stale_params),
                )
                if has_enabled_extensions:
                    if self._column_exists("enabled_extensions", "extension_id"):
                        cur.execute(
                            """
                            UPDATE enabled_extensions
                            SET enabled = false,
                                updated_at = now()
                            WHERE extension_id = %s
                            """,
                            (extension_key,),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE enabled_extensions ee
                            SET enabled = false,
                                updated_at = now()
                            FROM extension_bundles eb
                            WHERE ee.bundle_id = eb.id
                              AND eb.extension_key = %s
                            """,
                            (extension_key,),
                        )
                lifecycle_changes.append({"kind": "invalid", "extension_id": extension_key})

            if self._table_exists("extension_installs"):
                cur.execute(
                    """
                    UPDATE extension_installs AS install
                    SET state = 'disabled',
                        state_reason = 'Extension bundle is missing on disk; disabled by extension scan',
                        disabled_at = COALESCE(disabled_at, now())
                    WHERE install.state = 'active'
                      AND install.descriptor_source = 'extension_scan'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM extension_bundles AS bundle
                          WHERE bundle.extension_key = install.extension_id
                            AND bundle.status = 'valid'
                      )
                    """
                )
        return rows, lifecycle_changes
