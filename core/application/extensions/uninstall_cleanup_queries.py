from __future__ import annotations

from typing import Any

from core.application.settings.llm_provider_extension_service import credential_ref_key

from .callbacks import callback_capability_ref, callback_secret_ref
from .oauth_uninstall_cleanup import cleanup_extension_oauth_configurations
from .uninstall_inventory_queries import (
    callback_ownership_predicate,
    uninstall_inventory_row,
)
from .uninstall_models import RemovalTransactionState, UninstallContext, UninstallTarget


class ExtensionUninstallCleanupQueries:
    @staticmethod
    def _record_removed(
        cur: Any,
        removed_data: list[str],
        kind: str,
        *,
        count: int | None = None,
    ) -> None:
        affected_count = getattr(cur, "rowcount", 0) if count is None else count
        try:
            if int(affected_count) > 0:
                removed_data.append(kind)
        except (TypeError, ValueError):
            return

    def cleanup_install_dependencies(
        self,
        *,
        context: UninstallContext,
        target: UninstallTarget,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
        oauth_revocations: list[dict[str, Any]],
    ) -> None:
        self._cleanup_callbacks(context, target, state, cur, removed_data)
        self._cleanup_project_connections(
            context,
            target,
            state,
            cur,
            removed_data,
            oauth_revocations,
        )
        cleanup_extension_oauth_configurations(
            context,
            target,
            state,
            cur,
            removed_data,
            record_removed=self._record_removed,
        )
        self._cleanup_workspace_connections(context, state, cur, removed_data)
        if getattr(context.bl, "_table_exists", lambda *_args: False)("extension_project_state"):
            cur.execute(
                "DELETE FROM extension_project_state WHERE extension_install_id = %s",
                (state.install_id,),
            )
            self._record_removed(cur, removed_data, "project_state")

    def _cleanup_callbacks(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        if not getattr(context.bl, "_table_exists", lambda *_args: False)(
            "extension_callback_registrations"
        ):
            return
        clause, scope_params = callback_ownership_predicate(
            include_consumer_accounts=target.include_consumer_accounts,
            install_id=state.install_id,
            account_id=context.account_id,
        )
        params = (context.extension_id, *scope_params)
        cur.execute(
            f"SELECT c.id, c.project_id FROM extension_callback_registrations c JOIN projects p ON p.id = c.project_id WHERE c.extension_id = %s {clause} FOR UPDATE OF c",  # nosec B608
            params,
        )
        registrations = [
            uninstall_inventory_row(raw, ("id", "project_id")) for raw in cur.fetchall() or []
        ]
        registration_ids = [str(row.get("id") or "").strip() for row in registrations]
        if getattr(context.bl, "_table_exists", lambda *_args: False)("project_secrets"):
            for row, registration_id in zip(registrations, registration_ids, strict=True):
                if not registration_id:
                    continue
                cur.execute(
                    "DELETE FROM project_secrets WHERE project_id = %s AND secret_key = ANY(%s)",
                    (
                        row.get("project_id"),
                        [
                            callback_capability_ref(registration_id),
                            callback_secret_ref(registration_id),
                        ],
                    ),
                )
        registration_ids = [value for value in registration_ids if value]
        if registration_ids:
            cur.execute(
                "DELETE FROM extension_callback_registrations WHERE id = ANY(%s::uuid[]) AND extension_id = %s",
                (registration_ids, context.extension_id),
            )
        self._record_removed(cur, removed_data, "callback_registrations")

    def _cleanup_project_connections(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
        oauth_revocations: list[dict[str, Any]],
    ) -> None:
        if not getattr(context.bl, "_table_exists", lambda *_args: False)("project_connections"):
            return
        if target.include_consumer_accounts:
            cur.execute(
                "SELECT connection_id, account_id, project_id, secret_refs, extension_id, connection_type, connection_config FROM project_connections WHERE extension_install_id = %s OR (account_id::text = %s AND extension_install_id IS NULL AND extension_id = %s)",
                (state.install_id, context.account_id, context.extension_id),
            )
        else:
            cur.execute(
                "SELECT connection_id, account_id, project_id, secret_refs, extension_id, connection_type, connection_config FROM project_connections WHERE account_id::text = %s AND extension_install_id = %s AND extension_id = %s",
                (context.account_id, state.install_id, context.extension_id),
            )
        rows = cur.fetchall() or []
        from core.application.management.command_services_project_connection import (
            cleanup_project_connection_owned_data,
        )

        for raw in rows:
            row = uninstall_inventory_row(
                raw,
                (
                    "connection_id",
                    "account_id",
                    "project_id",
                    "secret_refs",
                    "extension_id",
                    "connection_type",
                    "connection_config",
                ),
            )
            if str(row.get("connection_id") or "").strip():
                cleanup_project_connection_owned_data(
                    context.bl,
                    connection=row,
                    cur=cur,
                    oauth_revocations=oauth_revocations,
                )
        self._record_removed(cur, removed_data, "connection_bindings", count=len(rows))

    def _cleanup_workspace_connections(
        self,
        context: UninstallContext,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        bl = context.bl
        if not getattr(bl, "_table_exists", lambda *_args: False)("workspace_data_connections"):
            return
        cur.execute(
            "SELECT account_id::text AS account_id, connection_id, secret_refs_jsonb FROM workspace_data_connections WHERE connector_ref_jsonb->>'install_id' = %s",
            (state.install_id,),
        )
        rows = [
            uninstall_inventory_row(raw, ("account_id", "connection_id", "secret_refs_jsonb"))
            for raw in cur.fetchall() or []
        ]
        ids = [str(row.get("connection_id") or "").strip() for row in rows]
        ids = [connection_id for connection_id in ids if connection_id]
        if ids and getattr(bl, "_table_exists", lambda *_args: False)("registry_datasets"):
            cur.execute(
                "DELETE FROM registry_datasets WHERE metadata_jsonb->>'connection_id' = ANY(%s)",
                (ids,),
            )
            self._record_removed(
                cur, removed_data, "workspace_connection_datasets", count=cur.rowcount
            )
        self._cleanup_workspace_secrets(bl, cur, rows)
        cur.execute(
            "DELETE FROM workspace_data_connections WHERE connector_ref_jsonb->>'install_id' = %s",
            (state.install_id,),
        )
        self._record_removed(cur, removed_data, "workspace_data_connections")

    @staticmethod
    def _cleanup_workspace_secrets(bl: Any, cur: Any, rows: list[dict[str, Any]]) -> None:
        if not getattr(bl, "_table_exists", lambda *_args: False)("secret_refs"):
            return
        for row in rows:
            refs = dict(dict(row.get("secret_refs_jsonb") or {}).get("refs") or {})
            ref_keys = [str(value or "").strip() for value in refs.values()]
            if ref_keys:
                cur.execute(
                    "DELETE FROM secret_refs WHERE account_id::text = %s AND ref_key = ANY(%s)",
                    (str(row.get("account_id") or "").strip(), ref_keys),
                )

    def cleanup_projection_dependencies(
        self,
        *,
        context: UninstallContext,
        target: UninstallTarget,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        self._detach_external_effects(context, target, state, cur, removed_data)
        self._cleanup_projection_credentials(context, target, state, cur, removed_data)
        self._cleanup_dataset_projections(context.bl, state.install_id, cur, removed_data)
        self._cleanup_workflow_projections(context, state.install_id, cur, removed_data)
        self._cleanup_provider_sources(context, target, state, cur, removed_data)

    def _detach_external_effects(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        if not getattr(context.bl, "_table_exists", lambda *_args: False)("external_effects"):
            return
        if target.include_consumer_accounts:
            scope = "extension_install_id = %s OR (account_id::text = %s AND extension_id = %s)"
            params = (state.install_id, context.account_id, context.extension_id)
        else:
            scope = "account_id::text = %s AND (extension_install_id = %s OR extension_id = %s)"
            params = (context.account_id, state.install_id, context.extension_id)
        if getattr(context.bl, "_table_exists", lambda *_args: False)("external_effect_replays"):
            replay_scope = (
                scope.replace("account_id", "effect.account_id")
                .replace("extension_install_id", "effect.extension_install_id")
                .replace("extension_id", "effect.extension_id")
            )
            cur.execute(
                f"UPDATE external_effect_replays AS replay SET replay_state = 'nonreplayable', replay_reason = 'extension_uninstalled', encrypted_payload = NULL, updated_at = now() FROM external_effects AS effect WHERE replay.effect_id = effect.effect_id AND ({replay_scope})",  # nosec B608
                params,
            )
            self._record_removed(cur, removed_data, "external_effect_replay_payloads")
        cur.execute(
            f"UPDATE external_effects SET extension_install_id = NULL, connection_id = NULL, source_ref_jsonb = '{{}}'::jsonb, artifact_refs_jsonb = '[]'::jsonb, response_summary_jsonb = '{{}}'::jsonb, error_text = NULL, status = CASE WHEN status = 'claimed' THEN 'expired' WHEN status = 'running' THEN 'timeout_unknown' ELSE status END, status_reason = CASE WHEN status IN ('claimed', 'running') THEN 'extension_uninstalled' ELSE status_reason END, completed_at = CASE WHEN status IN ('claimed', 'running') THEN now() ELSE completed_at END, claim_version = claim_version + 1, claim_token = gen_random_uuid()::text, updated_at = now() WHERE {scope}",  # nosec B608
            params,
        )
        self._record_removed(cur, removed_data, "external_effect_audit_detached")

    def _cleanup_projection_credentials(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        bl = context.bl
        if getattr(bl, "_table_exists", lambda *_args: False)("secret_refs"):
            base_ref = credential_ref_key(state.install_id)
            if target.include_consumer_accounts:
                cur.execute(
                    "DELETE FROM secret_refs WHERE ref_key = %s OR left(ref_key, length(%s) + 1) = %s || '_'",
                    (base_ref, base_ref, base_ref),
                )
            else:
                cur.execute(
                    "DELETE FROM secret_refs WHERE account_id = %s AND (ref_key = %s OR left(ref_key, length(%s) + 1) = %s || '_')",
                    (context.account_id, base_ref, base_ref, base_ref),
                )
            self._record_removed(cur, removed_data, "llm_credential_ref")
        if getattr(bl, "_table_exists", lambda *_args: False)("dataset_query_templates"):
            cur.execute(
                "DELETE FROM dataset_query_templates WHERE validation_result_jsonb->'projection_bridge'->>'install_id' = %s",
                (state.install_id,),
            )
            self._record_removed(cur, removed_data, "projected_saved_query_bridges")

    def _cleanup_dataset_projections(
        self,
        bl: Any,
        install_id: str,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        if not getattr(bl, "_table_exists", lambda *_args: False)("registry_datasets"):
            return
        if getattr(bl, "_table_exists", lambda *_args: False)("project_datasets"):
            cur.execute(
                "DELETE FROM project_datasets WHERE dataset_id IN (SELECT dataset_id FROM registry_datasets WHERE metadata_jsonb->'projection_bridge'->>'install_id' = %s)",
                (install_id,),
            )
            self._record_removed(cur, removed_data, "project_dataset_assignments")
        cur.execute(
            "DELETE FROM registry_datasets WHERE metadata_jsonb->'projection_bridge'->>'install_id' = %s",
            (install_id,),
        )
        self._record_removed(cur, removed_data, "projected_dataset_bridges")

    def _cleanup_workflow_projections(
        self,
        context: UninstallContext,
        install_id: str,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        bl = context.bl
        if not getattr(bl, "_table_exists", lambda *_args: False)("extension_registry_projections"):
            return
        if getattr(bl, "_table_exists", lambda *_args: False)("project_workflows"):
            cur.execute(
                "DELETE FROM project_workflows WHERE workflow_id IN (SELECT 'extension.' || regexp_replace(trim(install_id), '[^A-Za-z0-9_.-]+', '-', 'g') || '.' || regexp_replace(trim(artifact_id), '[^A-Za-z0-9_.-]+', '-', 'g') FROM extension_registry_projections WHERE install_id = %s AND artifact_kind = 'workflow')",
                (install_id,),
            )
            self._record_removed(cur, removed_data, "project_workflow_assignments")
        if getattr(bl, "_table_exists", lambda *_args: False)("registry_workflows"):
            from core.application.workflows.workflow_deletion_guard import (
                assert_workflow_has_no_nonterminal_jobs,
            )

            cur.execute(
                """SELECT workflow_id FROM registry_workflows
                    WHERE source_install_id = %s AND projection_mode = 'derived'""",
                (install_id,),
            )
            for row in cur.fetchall():
                workflow_id = str(
                    row.get("workflow_id") if isinstance(row, dict) else row[0]
                ).strip()
                assert_workflow_has_no_nonterminal_jobs(
                    bl,
                    workflow_id=workflow_id,
                    account_id=context.account_id,
                    cursor=cur,
                )
            cur.execute(
                "DELETE FROM registry_workflows WHERE source_install_id = %s AND projection_mode = 'derived'",
                (install_id,),
            )
            self._record_removed(cur, removed_data, "projected_workflow_anchors")
        cur.execute(
            "DELETE FROM extension_registry_projections WHERE install_id = %s", (install_id,)
        )
        self._record_removed(cur, removed_data, "registry_projections")

    def _cleanup_provider_sources(
        self,
        context: UninstallContext,
        target: UninstallTarget,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        if not getattr(context.bl, "_table_exists", lambda *_args: False)("llm_provider_sources"):
            return
        if target.include_consumer_accounts:
            cur.execute(
                "DELETE FROM llm_provider_sources WHERE extension_install_id = %s",
                (state.install_id,),
            )
        else:
            cur.execute(
                "DELETE FROM llm_provider_sources WHERE account_id::text = %s AND extension_install_id = %s",
                (context.account_id, state.install_id),
            )
        self._record_removed(cur, removed_data, "llm_provider_sources")

    def cleanup_runtime_records(
        self,
        *,
        context: UninstallContext,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        state.remove_shared_runtime_package = self._cleanup_install_record(
            context, state, cur, removed_data
        )
        self._cleanup_release_record(
            context.bl,
            release_id=state.release_id,
            extension_id=context.extension_id,
            remove_all_unreferenced=state.remove_shared_runtime_package,
            cur=cur,
            removed_data=removed_data,
        )
        if state.remove_shared_runtime_package:
            self._cleanup_shared_runtime_rows(context.bl, context.extension_id, cur, removed_data)

    def _cleanup_install_record(
        self,
        context: UninstallContext,
        state: RemovalTransactionState,
        cur: Any,
        removed_data: list[str],
    ) -> bool:
        bl = context.bl
        if not getattr(bl, "_table_exists", lambda *_args: False)("extension_installs"):
            return state.remove_shared_runtime_package
        cur.execute(
            "DELETE FROM extension_installs WHERE account_id = %s AND install_id = %s",
            (context.account_id, state.install_id),
        )
        self._record_removed(cur, removed_data, "install_runtime_records")
        cur.execute(
            "SELECT 1 FROM extension_installs WHERE extension_id = %s LIMIT 1",
            (context.extension_id,),
        )
        remaining = cur.fetchone()
        if isinstance(remaining, dict):
            exists = remaining.get("?column?")
        elif isinstance(remaining, (tuple, list)) and remaining:
            exists = remaining[0]
        else:
            exists = None
        return not bool(exists)

    @staticmethod
    def _cleanup_release_record(
        bl: Any,
        *,
        release_id: str,
        extension_id: str,
        remove_all_unreferenced: bool,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        if not getattr(bl, "_table_exists", lambda *_args: False)("extension_releases"):
            return
        if remove_all_unreferenced:
            cur.execute(
                "DELETE FROM extension_releases er WHERE er.extension_id = %s AND NOT EXISTS (SELECT 1 FROM extension_installs ei WHERE ei.release_id = er.release_id)",
                (extension_id,),
            )
        else:
            cur.execute(
                "DELETE FROM extension_releases er WHERE er.release_id = %s AND NOT EXISTS (SELECT 1 FROM extension_installs ei WHERE ei.release_id = er.release_id)",
                (release_id,),
            )
        if cur.rowcount:
            removed_data.append("release_runtime_records")

    def _cleanup_shared_runtime_rows(
        self,
        bl: Any,
        extension_id: str,
        cur: Any,
        removed_data: list[str],
    ) -> None:
        queries = (
            (
                "enabled_extensions",
                "DELETE FROM enabled_extensions WHERE bundle_id IN (SELECT id FROM extension_bundles WHERE extension_key = %s)",
                "enabled_state",
            ),
            (
                "extension_invocation_logs",
                "DELETE FROM extension_invocation_logs WHERE bundle_id IN (SELECT id FROM extension_bundles WHERE extension_key = %s)",
                "runtime_metadata",
            ),
            (
                "extension_versions",
                "DELETE FROM extension_versions WHERE bundle_id IN (SELECT id FROM extension_bundles WHERE extension_key = %s)",
                "version_records",
            ),
            (
                "extension_bundles",
                "DELETE FROM extension_bundles WHERE extension_key = %s",
                "bundle_records",
            ),
        )
        for table, query, kind in queries:
            if getattr(bl, "_table_exists", lambda *_args: False)(table):
                cur.execute(query, (extension_id,))
                self._record_removed(cur, removed_data, kind)
