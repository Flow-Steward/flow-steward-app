# mypy: ignore-errors
from __future__ import annotations

import hashlib
import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.extension_release_membership_lock import (
    acquire_extension_release_membership_lock,
)
from core.infrastructure.db.logger_extensions_accounts_common import (
    ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL,
    REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL,
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    SQL_JOB_ID_EQ,
    _as_dict,
    _as_list,
    _coerce_bool,
    _default_account_id,
    _normalize_string_list,
    _redact_payload,
    _trace_id_for_job,
)
from core.infrastructure.db.platform_logger_common import _step_execution_id


class PlatformLoggerExtensionsAccountsRuntimeMixin:
    def _resolve_extension_bundle_version(
        self,
        cur,
        *,
        normalized_key: str,
        version: str | None,
    ) -> tuple[str, str, str]:
        cur.execute(
            "SELECT id, latest_version FROM extension_bundles WHERE extension_key = %s",
            (normalized_key,),
        )
        bundle_row = cur.fetchone()
        if not bundle_row:
            raise ValueError(f"Unknown extension '{normalized_key}'")
        bundle_id = str(bundle_row["id"]).strip()
        version_value = str(version or bundle_row.get("latest_version") or "").strip()
        if not version_value:
            raise ValueError(f"Extension '{normalized_key}' has no scanned version to enable")
        cur.execute(
            "SELECT id FROM extension_versions WHERE bundle_id = %s AND version = %s",
            (bundle_id, version_value),
        )
        version_row = cur.fetchone()
        if not version_row:
            raise ValueError(
                f"Extension '{normalized_key}' version '{version_value}' is not available"
            )
        version_id = str(version_row["id"]).strip()
        return bundle_id, version_id, version_value

    def _assert_extension_version_quality(
        self,
        cur,
        *,
        version_id: str,
        normalized_key: str,
        version_value: str,
    ) -> None:
        has_compile_error_col = self._column_exists("extension_versions", "compile_errors_jsonb")
        has_validation_error_col = self._column_exists(
            "extension_versions", "validation_errors_jsonb"
        )
        if not (has_compile_error_col or has_validation_error_col):
            return
        select_cols = []
        if has_compile_error_col:
            select_cols.append("compile_errors_jsonb")
        if has_validation_error_col:
            select_cols.append("validation_errors_jsonb")
        cur.execute(
            f"""
            SELECT {", ".join(select_cols)}
            FROM extension_versions
            WHERE id = %s
            """,
            (version_id,),
        )
        quality_row = cur.fetchone() or {}
        compile_errors = (
            _as_list(quality_row.get("compile_errors_jsonb")) if has_compile_error_col else []
        )
        validation_errors = (
            _as_list(quality_row.get("validation_errors_jsonb")) if has_validation_error_col else []
        )
        if compile_errors or validation_errors:
            raise ValueError(
                f"Extension '{normalized_key}' version '{version_value}' has compile/validation errors"
            )

    def _enabled_extension_upsert_payload(
        self,
        *,
        bundle_id: str,
        version_id: str,
        enabled: bool,
        config: dict,
    ) -> tuple[list[str], list[Any], list[str]]:
        enabled_columns = [
            "bundle_id",
            "version_id",
            "enabled",
            "config_jsonb",
        ]
        enabled_values: list[Any] = [
            bundle_id,
            version_id,
            bool(enabled),
            json.dumps(config),
        ]
        enabled_updates = [
            "version_id = EXCLUDED.version_id",
            "enabled = EXCLUDED.enabled",
            "config_jsonb = EXCLUDED.config_jsonb",
            "enabled_at = CASE WHEN EXCLUDED.enabled THEN now() ELSE enabled_extensions.enabled_at END",
        ]
        if self._column_exists("enabled_extensions", "account_id"):
            account_id_value = str(config.get("account_id") or _default_account_id()).strip()
            enabled_columns.append("account_id")
            enabled_values.append(account_id_value or _default_account_id())
            enabled_updates.append(ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL)
        if self._column_exists("enabled_extensions", "required_scopes_jsonb"):
            enabled_columns.append("required_scopes_jsonb")
            enabled_values.append(json.dumps(_normalize_string_list(config.get("required_scopes"))))
            enabled_updates.append(REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL)
        return enabled_columns, enabled_values, enabled_updates

    def _sync_preserved_extension_runtime_enablement(
        self,
        cur,
        *,
        bundle_id: str,
        version_id: str,
        enabled: bool,
        config: dict,
    ) -> None:
        if not self._table_exists("enabled_extensions"):
            return
        enabled_columns, enabled_values, _enabled_updates = self._enabled_extension_upsert_payload(
            bundle_id=bundle_id,
            version_id=version_id,
            enabled=enabled,
            config=config,
        )
        preserved_updates = [
            "version_id = EXCLUDED.version_id",
            "enabled = EXCLUDED.enabled",
            "enabled_at = CASE WHEN EXCLUDED.enabled THEN now() ELSE enabled_extensions.enabled_at END",
        ]
        if "account_id" in enabled_columns:
            preserved_updates.append(ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL)
        if "required_scopes_jsonb" in enabled_columns:
            preserved_updates.append(REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL)
        cur.execute(
            f"""
            INSERT INTO enabled_extensions (
                {", ".join(enabled_columns)}
            ) VALUES ({", ".join(["%s"] * len(enabled_columns))})
            ON CONFLICT (bundle_id) DO UPDATE SET
                {", ".join(preserved_updates)}
            """,
            tuple(enabled_values),
        )

    def sync_enabled_extension_record(
        self,
        transaction_conn,
        extension_key: str,
        *,
        version: str | None = None,
        enabled: bool = True,
        config: dict | None = None,
        preserve_existing_install: bool = False,
        lifecycle_state_authoritative: bool = False,
    ) -> tuple[str, list[dict]] | None:
        if not (
            self._table_exists("enabled_extensions") and self._table_exists("extension_bundles")
        ):
            return None

        normalized_key = str(extension_key or "").strip()
        if not normalized_key:
            raise ValueError("extension_key is required")
        normalized_config = _as_dict(config)
        with transaction_conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            bundle_id, version_id, version_value = self._resolve_extension_bundle_version(
                cur,
                normalized_key=normalized_key,
                version=version,
            )
            self._assert_extension_version_quality(
                cur,
                version_id=version_id,
                normalized_key=normalized_key,
                version_value=version_value,
            )
            enabled_columns, enabled_values, enabled_updates = (
                self._enabled_extension_upsert_payload(
                    bundle_id=bundle_id,
                    version_id=version_id,
                    enabled=enabled,
                    config=normalized_config,
                )
            )

            cur.execute(
                f"""
                INSERT INTO enabled_extensions (
                    {", ".join(enabled_columns)}
                ) VALUES ({", ".join(["%s"] * len(enabled_columns))})
                ON CONFLICT (bundle_id) DO UPDATE SET
                    {", ".join(enabled_updates)}
                """,
                tuple(enabled_values),
            )
            release_change = self._sync_extension_to_extension_install(
                cur,
                extension_key=normalized_key,
                bundle_id=bundle_id,
                version_id=version_id,
                version_value=version_value,
                enabled=enabled,
                config=normalized_config,
                preserve_existing_state=preserve_existing_install,
                lifecycle_state_authoritative=lifecycle_state_authoritative,
            )
        return normalized_key, [release_change] if release_change else []

    def _enriched_projection_release_id(
        self,
        cur,
        *,
        install_id: str,
        extension_key: str,
        version_value: str,
    ) -> str:
        """The one same-version enriched release this install's projections name, or ''."""
        if not install_id or not self._table_exists("extension_registry_projections"):
            return ""
        cur.execute(
            """
            SELECT DISTINCT projection.release_id
              FROM extension_registry_projections AS projection
              JOIN extension_releases AS release
                ON release.release_id = projection.release_id
             WHERE projection.install_id = %s
               AND release.extension_id = %s
               AND release.release_version = %s
               AND release.source_kind = 'extension_scan'
               AND EXISTS (
                   SELECT 1
                     FROM extension_artifact_snapshots AS snapshot
                    WHERE snapshot.release_id = projection.release_id
               )
            """,
            (install_id, extension_key, version_value),
        )
        rows = cur.fetchall() or []
        if len(rows) != 1:
            return ""
        row = rows[0]
        value = row.get("release_id") if isinstance(row, dict) else row[0]
        return str(value or "").strip()

    def _sync_extension_to_extension_install(
        self,
        cur,
        *,
        extension_key: str,
        bundle_id: str,
        version_id: str,
        version_value: str,
        enabled: bool,
        config: dict,
        preserve_existing_state: bool = False,
        lifecycle_state_authoritative: bool = False,
    ) -> dict | None:
        """Record local extension enablement in the canonical install tables."""
        if not (
            self._table_exists("extension_releases") and self._table_exists("extension_installs")
        ):
            return None

        cur.execute(
            """
            SELECT
                manifest_jsonb,
                compiled_manifest_jsonb
            FROM extension_versions
            WHERE id = %s
            """,
            (version_id,),
        )
        version_row = cur.fetchone() or {}
        manifest = _as_dict(version_row.get("manifest_jsonb"))
        compiled_manifest = _as_dict(version_row.get("compiled_manifest_jsonb"))
        # Extension runtime should receive compiled UI/actions/queries, but preserve
        # runtime extension contract blocks that are only present in manifest.
        descriptor = dict(compiled_manifest or manifest)
        runtime_manifest = _as_dict(manifest.get("runtime"))
        runtime_descriptor = _as_dict(descriptor.get("runtime"))
        if runtime_manifest and not runtime_descriptor:
            descriptor["runtime"] = runtime_manifest
        elif runtime_manifest and runtime_descriptor:
            merged_runtime = dict(runtime_descriptor)
            for key, value in runtime_manifest.items():
                if key not in merged_runtime:
                    merged_runtime[key] = value
            descriptor["runtime"] = merged_runtime
        descriptor_json = json.dumps(descriptor)
        descriptor_sha256 = hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest()
        # A scanned bundle carries no host-trusted origin: every extension is the same.
        descriptor_origin = "imported"
        release_provenance = {"origin": descriptor_origin}
        account_id = (
            str(config.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        release_id = str(version_id).strip()
        # Descriptor releases are shared by every account install that points at
        # the same scanned bundle version. The advisory lock prevents a phantom
        # late join/repoint while the existing rows are enumerated; row locks then
        # preserve the install -> release -> provider ordering used by discovery.
        acquire_extension_release_membership_lock(cur, release_id=release_id)
        cur.execute(
            """
            SELECT account_id, install_id
              FROM extension_installs
             WHERE release_id = %s
                OR (extension_id = %s AND account_id = %s)
          ORDER BY account_id, install_id
             FOR UPDATE
            """,
            (release_id, extension_key, account_id),
        )
        cur.fetchall()
        cur.execute(
            """
            SELECT install.install_id,
                   install.state,
                   install.remote_base_url,
                   install.release_id,
                   current_release.release_version AS current_release_version,
                   current_release.source_kind AS current_release_source_kind,
                   EXISTS (
                       SELECT 1
                         FROM extension_artifact_snapshots AS snapshot
                        WHERE snapshot.release_id = install.release_id
                   ) AS current_release_has_artifacts,
                   target_release.release_id AS target_release_id,
                   target_release.descriptor_sha256 AS release_descriptor_sha256
              FROM (SELECT 1) AS anchor
         LEFT JOIN extension_installs AS install
                ON install.extension_id = %s
               AND install.account_id = %s
         LEFT JOIN extension_releases AS current_release
                ON current_release.release_id = install.release_id
         LEFT JOIN extension_releases AS target_release
                ON target_release.release_id = %s
            LIMIT 1
            """,
            (extension_key, account_id, release_id),
        )
        existing_install_row = cur.fetchone() or {}
        existing_release_id = str(existing_install_row.get("release_id") or "").strip()
        # The release the descriptor install wrote (deterministic ``pr_`` id) owns
        # the artifact snapshots and every registry projection; this scan release
        # (the bundle version's id) carries none. Repointing the install at it
        # orphaned all projections: their release no longer matched the
        # install's, so every saved query and dataset an extension ships was
        # "unavailable for new runs". Enabling ran this without
        # ``preserve_existing_state`` right after the install, so the rule holds
        # for every sync, not only the state-preserving ones.
        preserve_enriched_release = (
            bool(existing_release_id)
            and existing_release_id != release_id
            and str(existing_install_row.get("current_release_version") or "").strip()
            == version_value
            and str(existing_install_row.get("current_release_source_kind") or "").strip().lower()
            == "extension_scan"
            and _coerce_bool(existing_install_row.get("current_release_has_artifacts"), False)
        )
        effective_release_id = existing_release_id if preserve_enriched_release else release_id
        if not preserve_enriched_release:
            # An install this sync repointed before the rule above existed still
            # names the artifact-less scan release while its projections name
            # the enriched one; return it to that release.
            effective_release_id = (
                self._enriched_projection_release_id(
                    cur,
                    install_id=str(existing_install_row.get("install_id") or "").strip(),
                    extension_key=extension_key,
                    version_value=version_value,
                )
                or effective_release_id
            )
        install_release_changed = (
            bool(str(existing_install_row.get("install_id") or "").strip())
            and existing_release_id != effective_release_id
        )
        previous_target_descriptor = str(
            existing_install_row.get("release_descriptor_sha256") or ""
        ).strip()
        target_release_exists = bool(
            str(existing_install_row.get("target_release_id") or "").strip()
        ) or (
            bool(str(existing_install_row.get("install_id") or "").strip())
            and str(existing_install_row.get("release_id") or "").strip() == release_id
        )
        target_descriptor_changed = target_release_exists and (
            previous_target_descriptor != descriptor_sha256
        )
        install_id = (
            str(existing_install_row.get("install_id") or "").strip() or str(bundle_id).strip()
        )
        preserved_state = str(existing_install_row.get("state") or "").strip().lower()
        entrypoint = _as_dict(manifest.get("entrypoint"))
        runtime_mode = str(entrypoint.get("mode") or "").strip() or None
        runtime_enabled = bool(enabled)
        if preserve_existing_state and preserved_state and not lifecycle_state_authoritative:
            runtime_enabled = preserved_state == "active"
        elif (
            preserve_existing_state
            and not lifecycle_state_authoritative
            and self._table_exists("enabled_extensions")
        ):
            cur.execute(
                """
                SELECT enabled
                FROM enabled_extensions
                WHERE bundle_id = %s
                LIMIT 1
                """,
                (bundle_id,),
            )
            existing_enabled_row = cur.fetchone() or {}
            if "enabled" in existing_enabled_row:
                runtime_enabled = _coerce_bool(existing_enabled_row.get("enabled"), False)
        if (
            runtime_mode == "remote_https"
            and not str(existing_install_row.get("remote_base_url") or "").strip()
        ):
            runtime_enabled = False
        install_state = "active" if runtime_enabled else "disabled"
        runtime_block = _as_dict(manifest.get("runtime"))
        contract_version = str(runtime_block.get("contract_version") or "").strip()
        execution_modes = [runtime_mode] if runtime_mode else []

        cur.execute(
            """
            INSERT INTO extension_releases (
                release_id, extension_id, release_version, descriptor_schema_version,
                descriptor_jsonb, descriptor_sha256, channel, execution_modes_jsonb,
                contract_version, provenance_jsonb, source_kind, source_ref,
                account_id, created_by
            ) VALUES (
                %s, %s, %s, 'extension_descriptor_v1',
                %s::jsonb, %s, 'private_byo', %s::jsonb,
                %s, %s::jsonb, 'extension_scan', %s,
                %s, 'extension_binding'
            )
            ON CONFLICT (release_id) DO UPDATE SET
                descriptor_jsonb = EXCLUDED.descriptor_jsonb,
                descriptor_sha256 = EXCLUDED.descriptor_sha256,
                release_version = EXCLUDED.release_version,
                provenance_jsonb = EXCLUDED.provenance_jsonb,
                execution_modes_jsonb = EXCLUDED.execution_modes_jsonb,
                contract_version = EXCLUDED.contract_version
            """,
            (
                release_id,
                extension_key,
                version_value or "1.0.0",
                descriptor_json,
                descriptor_sha256,
                json.dumps(execution_modes),
                contract_version,
                json.dumps(release_provenance),
                extension_key,
                account_id,
            ),
        )

        install_values = (
            extension_key,
            version_value or "1.0.0",
            account_id,
            install_id,
            effective_release_id,
            descriptor_origin,
            json.dumps(release_provenance),
            runtime_mode,
            str(existing_install_row.get("remote_base_url") or "").strip() or None,
            install_state,
            json.dumps(config),
        )
        if preserve_existing_state:
            state_update = (
                "state = EXCLUDED.state"
                if lifecycle_state_authoritative
                else "state = extension_installs.state"
            )
            cur.execute(
                f"""
                INSERT INTO extension_installs (
                    extension_id, version, account_id, install_id, release_id,
                    descriptor_schema_version, descriptor_origin, descriptor_source,
                    provenance_jsonb, runtime_mode, remote_base_url, handshake_status, health_status,
                    runtime_status, last_error, visibility, state, installed_by, config_jsonb
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    'extension_descriptor_v1', %s, 'extension_scan',
                    %s::jsonb, %s, %s, 'unknown', 'unknown',
                    'unknown', '', 'account', %s, 'extension_binding', %s::jsonb
                )
                ON CONFLICT (extension_id, account_id) DO UPDATE SET
                    install_id = EXCLUDED.install_id,
                    release_id = EXCLUDED.release_id,
                    version = EXCLUDED.version,
                    descriptor_origin = EXCLUDED.descriptor_origin,
                    provenance_jsonb = EXCLUDED.provenance_jsonb,
                    runtime_mode = EXCLUDED.runtime_mode,
                    remote_base_url = extension_installs.remote_base_url,
                    {state_update},
                    config_jsonb = EXCLUDED.config_jsonb || extension_installs.config_jsonb,
                    installed_at = extension_installs.installed_at
                """,
                install_values,
            )
        else:
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, version, account_id, install_id, release_id,
                    descriptor_schema_version, descriptor_origin, descriptor_source,
                    provenance_jsonb, runtime_mode, remote_base_url, handshake_status, health_status,
                    runtime_status, last_error, visibility, state, installed_by, config_jsonb
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    'extension_descriptor_v1', %s, 'extension_scan',
                    %s::jsonb, %s, %s, 'unknown', 'unknown',
                    'unknown', '', 'account', %s, 'extension_binding', %s::jsonb
                )
                ON CONFLICT (extension_id, account_id) DO UPDATE SET
                    install_id = EXCLUDED.install_id,
                    release_id = EXCLUDED.release_id,
                    version = EXCLUDED.version,
                    descriptor_origin = EXCLUDED.descriptor_origin,
                    provenance_jsonb = EXCLUDED.provenance_jsonb,
                    runtime_mode = EXCLUDED.runtime_mode,
                    remote_base_url = EXCLUDED.remote_base_url,
                    state = EXCLUDED.state,
                    config_jsonb = EXCLUDED.config_jsonb,
                    installed_at = now()
                """,
                install_values,
            )
        release_change = None
        if install_release_changed or target_descriptor_changed:
            release_change = {
                "kind": "release_changed",
                "account_id": account_id,
                "extension_install_id": install_id,
                "changed_release_id": release_id if target_descriptor_changed else "",
            }
        if preserve_existing_state:
            self._sync_preserved_extension_runtime_enablement(
                cur,
                bundle_id=bundle_id,
                version_id=version_id,
                enabled=runtime_enabled,
                config=config,
            )
        return release_change

    def log_extension_invocation(
        self,
        *,
        extension_key: str,
        invocation_type: str,
        status: str,
        request: dict | None = None,
        response: dict | None = None,
        error_text: str | None = None,
        account_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        request_context: dict | None = None,
        query_id: str | None = None,
        action_id: str | None = None,
        command_id: str | None = None,
        # S3 — structured audit context
        job_id: str | None = None,
        workflow_id: str | None = None,
        group_id: str | None = None,
        for_each_id: str | None = None,
        step_id: str | None = None,
        step_execution_id: str | None = None,
        external_effect_id: str | None = None,
    ) -> None:
        if not self._table_exists("extension_invocation_logs"):
            return
        extension = self.get_extension(extension_key)
        if not extension:
            return

        normalized_request_context = _as_dict(request_context)
        if job_id is not None:
            normalized_request_context.setdefault("trace_id", _trace_id_for_job(job_id))

        columns = [
            "bundle_id",
            "version_id",
            "invocation_type",
            "status",
            "request_jsonb",
            "response_jsonb",
            "error_text",
        ]
        values: list[Any] = [
            extension.get("bundle_id"),
            extension.get("version_id"),
            str(invocation_type or "").strip() or "health_check",
            str(status or "").strip() or "unknown",
            json.dumps(_redact_payload(_as_dict(request))),
            json.dumps(_redact_payload(_as_dict(response))),
            str(error_text or "").strip() or None,
        ]
        if self._column_exists("extension_invocation_logs", "account_id"):
            columns.append("account_id")
            values.append(
                str(account_id or extension.get("account_id") or _default_account_id()).strip()
                or _default_account_id()
            )
        if self._column_exists("extension_invocation_logs", "actor_type"):
            columns.append("actor_type")
            values.append(str(actor_type or "system_actor").strip() or "system_actor")
        if self._column_exists("extension_invocation_logs", "actor_id"):
            columns.append("actor_id")
            values.append(str(actor_id or "").strip() or None)
        if self._column_exists("extension_invocation_logs", "request_context_jsonb"):
            columns.append("request_context_jsonb")
            values.append(json.dumps(normalized_request_context))
        if self._column_exists("extension_invocation_logs", "query_id"):
            columns.append("query_id")
            values.append(str(query_id or "").strip() or None)
        if self._column_exists("extension_invocation_logs", "action_id"):
            columns.append("action_id")
            values.append(str(action_id or "").strip() or None)
        if self._column_exists("extension_invocation_logs", "command_id"):
            columns.append("command_id")
            values.append(str(command_id or "").strip() or None)
        if self._column_exists("extension_invocation_logs", "external_effect_id"):
            columns.append("external_effect_id")
            values.append(str(external_effect_id or "").strip() or None)
        columns.extend(
            ("job_id", "workflow_id", "group_id", "for_each_id", "step_id", "step_execution_id")
        )
        values.extend(
            (
                str(job_id or "").strip() or None,
                str(workflow_id or "").strip() or None,
                str(group_id or "").strip() or None,
                str(for_each_id or "").strip() or None,
                str(step_id or "").strip() or None,
                _step_execution_id(step_execution_id),
            )
        )

        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO extension_invocation_logs (
                    {", ".join(columns)}
                ) VALUES ({", ".join(["%s"] * len(columns))})
                """,
                tuple(values),
            )

    def list_extension_invocations(
        self,
        *,
        job_id: str | None = None,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        if not self._table_exists("extension_invocation_logs"):
            return []
        where: list[str] = []
        params: list[Any] = []
        if job_id is not None:
            if not self._column_exists("extension_invocation_logs", "job_id"):
                return []
            normalized_job_id = str(job_id).strip()
            if not normalized_job_id:
                return []
            where.append(SQL_JOB_ID_EQ)
            params.append(normalized_job_id)
        if account_id and self._column_exists("extension_invocation_logs", "account_id"):
            where.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id).strip())
        where_sql = f"WHERE {SQL_AND.join(where)}" if where else ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT *
                FROM extension_invocation_logs
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (*params, max(1, min(int(limit or 200), 2000))),
            )
            rows = cur.fetchall()
        return [self._normalize_extension_invocation_row(dict(row)) for row in rows]
