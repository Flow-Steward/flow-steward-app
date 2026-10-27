# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR

_MESSAGE_ACCESS_DENIED = "Access denied"


class TrashLifecycleServicePublicMixin:
    """Unified trash / restore / hard-delete for approved entity types."""

    def __init__(
        self,
        get_logger: Callable[[], Any],
        artifact_plane: Any | None = None,
        get_project_dataset_catalog: Callable[[], Any] | None = None,
        get_audit_store: Callable[[], Any] | None = None,
        delete_project_queues: Callable[[str], Any] | None = None,
        begin_project_executor_quiescence: Callable[[str], bool] | None = None,
        end_project_executor_quiescence: Callable[[str], Any] | None = None,
        workflow_secret_default_service: Any | None = None,
    ):
        self._logger = get_logger
        self._audit_store = get_audit_store or get_logger
        self._artifact_plane = artifact_plane
        self._project_dataset_catalog = get_project_dataset_catalog
        self._delete_project_queues = delete_project_queues
        self._begin_project_executor_quiescence = begin_project_executor_quiescence
        self._end_project_executor_quiescence = end_project_executor_quiescence
        self._workflow_secret_default_service = workflow_secret_default_service

    @staticmethod
    def _reload_workflow_registry() -> None:
        from core.application.registry.registry_manager import RegistryManager

        RegistryManager.instance().reload_all()

    def _record_hard_delete_audit(
        self,
        bl: Any,
        *,
        entity_type: str,
        entity_id: str | int,
        scope: ScopeContext,
        status: str,
    ) -> None:
        del bl
        log_action = getattr(self._audit_store(), "log_action", None)
        if not callable(log_action):
            raise ServiceUnavailableError("Lifecycle audit persistence is unavailable")
        normalized_entity_type = str(entity_type or "").strip()
        normalized_entity_id = str(entity_id)
        actor_id = str(scope.actor_id or scope.actor_name or "system").strip() or "system"
        log_action(
            project_id=None,
            job_id=None,
            task_id=f"lifecycle:{normalized_entity_type}:{normalized_entity_id}",
            task_summary=f"Hard delete {normalized_entity_type}",
            agent=actor_id,
            action_type="lifecycle_hard_delete",
            status=status,
            payload={
                "entity_type": normalized_entity_type,
                "entity_id": normalized_entity_id,
            },
            result={"status": status},
            meta={
                "audit_category": "entity_lifecycle",
                "account_id": str(scope.account_id or DEFAULT_ACCOUNT_ID),
                "actor_id": actor_id,
                "principal_type": str(scope.principal_type or "system_actor"),
            },
        )

    def _require_direct_entity_in_scope(
        self,
        bl: Any,
        *,
        cfg: Mapping[str, Any],
        entity_type: str,
        entity_id: str,
        scope: ScopeContext,
    ) -> None:
        if entity_type in {"project", "workspace"}:
            self._require_lifecycle_root_in_scope(
                bl,
                entity_type=entity_type,
                entity_id=str(entity_id),
                scope=scope,
            )
            return

        table = str(cfg.get("table") or "").strip()
        id_col = str(cfg.get("id_col") or "").strip()
        if not table or not id_col or not getattr(bl, "conn", None):
            raise ServiceNotFoundError(f"{entity_type} not found")

        ownership_cols = [
            col
            for col in ("account_id", "project_id", "workspace_id", "tenant_id")
            if self._has_column(bl, table, col)
        ]
        deployment_global = bool(cfg.get("deployment_global"))
        if not ownership_cols and not deployment_global:
            raise ServiceForbiddenError(_MESSAGE_ACCESS_DENIED)

        columns = [id_col, *ownership_cols]
        query = "".join(
            [
                "SELECT ",
                ", ".join(columns),
                " FROM ",
                table,
                SQL_WHERE_PREFIX,
                id_col,
                " = %s LIMIT 1",
            ]
        )
        with bl.conn.cursor() as cur:
            try:
                cur.execute(query, (entity_id,))
            except Exception as exc:
                if _is_invalid_uuid_lookup_error(exc):
                    raise ServiceNotFoundError(f"{entity_type} not found or not trashed") from exc
                raise
            row = cur.fetchone()
            description = [str(item[0]) for item in (getattr(cur, "description", None) or [])]
        if not row:
            raise ServiceNotFoundError(f"{entity_type} not found")
        if isinstance(row, Mapping):
            payload = dict(row)
        elif isinstance(row, (tuple, list)) and description and len(description) == len(row):
            payload = {description[idx]: row[idx] for idx in range(len(row))}
        else:
            return

        account_id = str(payload.get("account_id") or "").strip()
        tenant_id = str(payload.get("tenant_id") or "").strip()
        project_id = str(payload.get("project_id") or "").strip()
        workspace_id = str(payload.get("workspace_id") or "").strip()

        if "account_id" in payload and not account_id and not deployment_global:
            raise ServiceForbiddenError(_MESSAGE_ACCESS_DENIED)
        if not (account_id or project_id or workspace_id):
            scope_account_id = str(scope.account_id or DEFAULT_ACCOUNT_ID).strip()
            if not deployment_global or scope_account_id != DEFAULT_ACCOUNT_ID:
                raise ServiceForbiddenError(_MESSAGE_ACCESS_DENIED)
            return
        if account_id and account_id != str(scope.account_id or "").strip():
            raise ServiceForbiddenError(_MESSAGE_ACCESS_DENIED)
        if tenant_id and tenant_id != str(scope.tenant_id or "").strip():
            raise ServiceForbiddenError(_MESSAGE_ACCESS_DENIED)
        if project_id:
            project = self._get_project_row_including_trashed(bl, project_id)
            require_project_access(scope, project)
        if workspace_id:
            workspace = self._get_workspace_row_including_trashed_and_scoped(
                bl,
                workspace_id,
                account_id=scope.account_id,
                tenant_id=scope.tenant_id,
                allowed_workspace_ids=scope.allowed_workspace_ids,
            )
            if not workspace:
                raise ServiceForbiddenError(_MESSAGE_ACCESS_DENIED)

    def plan(
        self,
        entity_type: str,
        entity_id: str | int,
        *,
        action_type: str,
        scope: ScopeContext | None = None,
    ) -> LifecyclePlanView:
        normalized_action = str(action_type or "").strip().lower().replace("-", "_")
        if normalized_action not in {"restore", "hard_delete"}:
            raise ServiceValidationError("Unsupported lifecycle action")
        resolved = _service_scope(scope)
        _require_role(resolved, "admin")
        bl = self._logger()
        self._require_lifecycle_root_in_scope(
            bl,
            entity_type=str(entity_type or "").strip(),
            entity_id=str(entity_id),
            scope=resolved,
        )
        plan_payload = self._build_lifecycle_plan(
            bl,
            entity_type=str(entity_type or "").strip(),
            entity_id=str(entity_id),
            action_type=normalized_action,
            scope=resolved,
        )
        return LifecyclePlanView(**plan_payload)

    def execute_plan(
        self,
        entity_type: str,
        entity_id: str | int,
        *,
        action_type: str,
        selection: Mapping[str, Any] | None = None,
        scope: ScopeContext | None = None,
    ) -> RestoreResultView | HardDeleteResultView:
        normalized_action = str(action_type or "").strip().lower().replace("-", "_")
        if normalized_action not in {"restore", "hard_delete"}:
            raise ServiceValidationError("Unsupported lifecycle action")
        resolved = _service_scope(scope)
        _require_role(resolved, "admin")
        bl = self._logger()
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(bl.conn):
            execution_result = self._validate_and_execute_lifecycle_plan(
                bl,
                entity_type=str(entity_type or "").strip(),
                entity_id=str(entity_id),
                action_type=normalized_action,
                selection=selection,
                scope=resolved,
            )
            if (
                normalized_action == "restore"
                and str(entity_type or "").strip() == "registry_workflow"
                and execution_result.get("root_applied")
            ):
                self._record_lifecycle_changelog(
                    bl,
                    entity_type="registry_workflow",
                    entity_id=str(entity_id),
                    action="restore",
                    actor=resolved.actor_name,
                    scope=resolved,
                )
        if str(entity_type or "").strip() == "mcp_connector":
            from core.application.registry.registry_manager import RegistryManager

            RegistryManager.instance().reload_all()
        if normalized_action == "restore":
            return RestoreResultView(
                status="restored" if bool(execution_result.get("root_applied")) else "updated",
                entity_type=entity_type,
                entity_id=entity_id,
            )
        status = "hard_deleted" if bool(execution_result.get("root_applied")) else "updated"
        self._record_hard_delete_audit(
            bl,
            entity_type=entity_type,
            entity_id=entity_id,
            scope=resolved,
            status=status,
        )
        return HardDeleteResultView(
            status=status,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    def _validate_and_execute_lifecycle_plan(
        self,
        bl: Any,
        *,
        entity_type: str,
        entity_id: str,
        action_type: str,
        selection: Mapping[str, Any] | None,
        scope: ScopeContext,
    ) -> dict[str, Any]:
        self._require_lifecycle_root_in_scope(
            bl,
            entity_type=entity_type,
            entity_id=entity_id,
            scope=scope,
        )
        plan_payload = self._build_lifecycle_plan(
            bl,
            entity_type=entity_type,
            entity_id=entity_id,
            action_type=action_type,
            scope=scope,
        )
        validation_errors = list(plan_payload.get("validation_errors") or [])
        if validation_errors:
            raise ServiceValidationError("; ".join(validation_errors))
        submitted_plan_version = str((selection or {}).get("plan_version") or "").strip()
        current_plan_version = str(plan_payload.get("plan_version") or "").strip()
        if submitted_plan_version and submitted_plan_version != current_plan_version:
            raise ServiceValidationError(
                "Lifecycle plan is stale. Refresh the plan before making changes."
            )
        plan_root = dict(plan_payload.get("tree") or {})
        if entity_type == "mcp_connector":
            expected_ids = self._collect_selected_node_ids(plan_root)
            submitted_ids = self._normalize_selected_node_ids(selection)
            if submitted_ids and submitted_ids != expected_ids:
                raise ServiceValidationError(
                    "The MCP connector and every projected tool are mandatory lifecycle items."
                )
            return self._execute_mcp_connector_lifecycle(
                bl,
                connector_id=entity_id,
                action_type=action_type,
                tree=plan_root,
                scope=scope,
            )
        selectable_node_ids = self._collect_selectable_node_ids(plan_root)
        actual_selected = self._collect_selected_node_ids(plan_root)
        submitted_selected = self._normalize_selected_node_ids(selection)
        if submitted_selected and not submitted_selected.issubset(selectable_node_ids):
            raise ServiceValidationError(
                "Lifecycle selection is stale. Refresh the plan and try again."
            )
        effective_selected = submitted_selected or actual_selected
        if action_type == "hard_delete":
            incomplete_roots = self._collect_incomplete_selected_container_roots(
                plan_root,
                selected_node_ids=effective_selected,
            )
            if incomplete_roots:
                raise ServiceValidationError(
                    "Hard-delete root selection requires a complete descendant selection. "
                    "Refresh the plan and select every descendant, or deselect the root."
                )
        dependency_conflicts = self._collect_direct_dependency_conflicts(
            bl,
            plan_root,
            action_type=action_type,
            selected_node_ids=effective_selected,
        )
        if dependency_conflicts:
            raise ServiceValidationError(
                "; ".join(
                    str(
                        item.get("message")
                        or "Lifecycle selection has unresolved direct dependencies."
                    )
                    for item in dependency_conflicts
                    if isinstance(item, Mapping)
                )
                or "Lifecycle selection has unresolved direct dependencies."
            )
        return self._execute_lifecycle_selection(
            bl,
            action_type=action_type,
            selected_node_ids=effective_selected,
            tree_node=plan_root,
            account_id=str(scope.account_id or ""),
            actor=scope.actor_name,
        )

    def trash(
        self,
        entity_type: str,
        entity_id: str | int,
        *,
        deleted_by: str = "system",
        scope: ScopeContext | None = None,
    ) -> TrashResultView:
        cfg = self._entity_config(entity_type)
        bl = self._logger()
        resolved = _service_scope(scope)
        # Soft-deleting a user or a registry/global entity matches admin-only
        # management surfaces (/api/auth/users, /api/agents, /api/tools, ...), so it
        # must require admin — not the operator default used for project-scoped rows.
        normalized_entity_type = str(entity_type or "").strip()
        admin_only_entity_types = {
            "user",
            "registry_agent",
            "registry_tool",
            "registry_skill",
            "registry_workflow",
            "registry_guardrail",
            "registry_data_contract",
        }
        required_role = "admin" if normalized_entity_type in admin_only_entity_types else "operator"
        _require_role(resolved, required_role)
        self._require_direct_entity_in_scope(
            bl,
            cfg=cfg,
            entity_type=str(entity_type or "").strip(),
            entity_id=str(entity_id),
            scope=resolved,
        )
        if normalized_entity_type == "mcp_connector":
            raise ServiceConflictError(
                "Delete the MCP connector through its confirmed impact plan so projected tools move to Trash together"
            )
        if entity_type == "registry_tool":
            with bl.conn.cursor() as cur:
                cur.execute(
                    "SELECT implementation FROM registry_tools WHERE tool_id = %s AND deleted_at IS NULL",
                    (str(entity_id),),
                )
                raw = cur.fetchone()
            implementation = {}
            if isinstance(raw, Mapping):
                implementation = raw.get("implementation")
            elif raw:
                implementation = raw[0]
            if isinstance(implementation, str):
                try:
                    implementation = json.loads(implementation)
                except Exception:
                    implementation = {}
            if (
                str(
                    (implementation or {}).get("type") or (implementation or {}).get("kind") or ""
                ).strip()
                == "mcp"
                and str((implementation or {}).get("connector_id") or "").strip()
            ):
                raise ServiceConflictError(
                    "MCP projected tool deletion requires a confirmed workflow-impact plan",
                    payload={
                        "error": "mcp_projected_tool_delete_plan_required",
                        "tool_id": str(entity_id),
                    },
                )
        conn = bl.conn
        table = cfg["table"]
        id_col = cfg["id_col"]
        has_deleted_by = self._has_column(bl, table, "deleted_by")

        if entity_type == "registry_skill":
            self._require_skill_without_dependents(
                bl,
                str(entity_id),
                action="trashing",
            )
        if entity_type == "registry_workflow":
            from core.application.workflows.workflow_trigger_lifecycle import retire_workflow
            from core.infrastructure.db.db_pool import transaction_connection

            with transaction_connection(conn) as physical, physical.cursor() as cur:
                retire_workflow(
                    cur,
                    store=bl,
                    workflow_id=str(entity_id),
                    account_id=str(resolved.account_id or ""),
                    author=deleted_by,
                    record_change=lambda cursor, locked_snapshot: (
                        self._log_registry_changelog_entry(
                            cursor,
                            entity_type="workflow",
                            entity_id=str(entity_id),
                            action="trash",
                            author=resolved.actor_name,
                            snapshot={
                                **locked_snapshot,
                                "lifecycle_action": "trash",
                                "entity_type": "workflow",
                                "entity_id": str(entity_id),
                                "actor": resolved.actor_name,
                            },
                        )
                    ),
                )
            self._reload_workflow_registry()
            return TrashResultView(entity_type=entity_type, entity_id=entity_id)

        if entity_type == "workspace":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE workspaces
                       SET status = 'active',
                           deleted_at = NOW(),
                           deleted_by = %s,
                           updated_at = now()
                     WHERE id = %s
                       AND deleted_at IS NULL
                    RETURNING id
                    """,
                    (deleted_by, entity_id),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE projects
                           SET deleted_at = NOW(),
                               deleted_by = %s
                         WHERE workspace_id = %s
                           AND deleted_at IS NULL
                        """,
                        (deleted_by, entity_id),
                    )
            if not row:
                raise ServiceNotFoundError("workspace not found or already trashed")
            return TrashResultView(entity_type=entity_type, entity_id=entity_id)

        with conn.cursor() as cur:
            try:
                if has_deleted_by:
                    trash_query = "".join(
                        [
                            SQL_UPDATE_PREFIX,
                            table,
                            " SET deleted_at = NOW(), deleted_by = %s WHERE ",
                            id_col,
                            " = %s AND deleted_at IS NULL RETURNING ",
                            id_col,
                        ]
                    )
                    cur.execute(
                        trash_query,
                        (deleted_by, entity_id),
                    )
                else:
                    trash_query = "".join(
                        [
                            SQL_UPDATE_PREFIX,
                            table,
                            " SET deleted_at = NOW() WHERE ",
                            id_col,
                            " = %s AND deleted_at IS NULL RETURNING ",
                            id_col,
                        ]
                    )
                    cur.execute(
                        trash_query,
                        (entity_id,),
                    )
            except Exception as exc:
                if _is_invalid_uuid_lookup_error(exc):
                    raise ServiceNotFoundError(
                        f"{entity_type} not found or already trashed"
                    ) from exc
                raise
            row = cur.fetchone()
        if not row:
            raise ServiceNotFoundError(f"{entity_type} not found or already trashed")
        self._record_lifecycle_changelog(
            bl,
            entity_type=entity_type,
            entity_id=str(entity_id),
            action="trash",
            actor=resolved.actor_name,
            scope=resolved,
        )
        return TrashResultView(entity_type=entity_type, entity_id=entity_id)

    def restore(
        self,
        entity_type: str,
        entity_id: str | int,
        *,
        scope: ScopeContext | None = None,
    ) -> RestoreResultView:
        cfg = self._entity_config(entity_type)
        bl = self._logger()
        resolved = _service_scope(scope)
        _require_role(resolved, "admin")
        self._require_direct_entity_in_scope(
            bl,
            cfg=cfg,
            entity_type=str(entity_type or "").strip(),
            entity_id=str(entity_id),
            scope=resolved,
        )
        if str(entity_type or "").strip() == "mcp_connector":
            raise ServiceConflictError(
                "Restore the MCP connector through its lifecycle plan so its projected tools are restored together"
            )
        if entity_type == "registry_tool":
            from core.application.management.mcp_projected_tool_lifecycle import (
                McpProjectedToolLifecycleService,
                mcp_projected_tool_connector_id,
            )
            from core.application.registry.registry_manager import RegistryManager

            with bl.conn.cursor() as cur:
                cur.execute(
                    "SELECT implementation, deleted_at FROM registry_tools WHERE tool_id = %s",
                    (str(entity_id),),
                )
                raw = cur.fetchone()
            implementation = {}
            if isinstance(raw, Mapping):
                implementation = raw.get("implementation")
            elif raw:
                implementation = raw[0]
            row = {"implementation": implementation, "deleted_at": True}
            if mcp_projected_tool_connector_id(row):
                manager = RegistryManager.instance()
                McpProjectedToolLifecycleService(
                    db_store=manager.db_store,
                    reload_registry=manager.reload_all,
                ).restore_from_trash(
                    str(entity_id),
                    account_id=str(resolved.account_id),
                    project_id=str(resolved.project_id or ""),
                    tenant_id=str(resolved.tenant_id or ""),
                )
                return RestoreResultView(entity_type=entity_type, entity_id=entity_id)

        conn = bl.conn
        table = cfg["table"]
        id_col = cfg["id_col"]
        has_deleted_by = self._has_column(bl, table, "deleted_by")
        set_clause = (
            "deleted_at = NULL, deleted_by = NULL" if has_deleted_by else "deleted_at = NULL"
        )

        if entity_type == "workspace":
            self._restore_workspace_row(bl, str(entity_id))
            return RestoreResultView(entity_type=entity_type, entity_id=entity_id)
        if entity_type == "project":
            try:
                self._restore_project_root(bl, str(entity_id))
            except Exception as exc:
                if _is_invalid_uuid_lookup_error(exc):
                    raise ServiceNotFoundError(f"{entity_type} not found or not trashed") from exc
                raise
            return RestoreResultView(entity_type=entity_type, entity_id=entity_id)

        restore_query = "".join(
            [
                SQL_UPDATE_PREFIX,
                table,
                " SET ",
                set_clause,
                SQL_WHERE_PREFIX,
                id_col,
                " = %s AND deleted_at IS NOT NULL RETURNING ",
                id_col,
            ]
        )
        with conn.cursor() as cur:
            try:
                cur.execute(
                    restore_query,
                    (entity_id,),
                )
            except Exception as exc:
                if _is_invalid_uuid_lookup_error(exc):
                    raise ServiceNotFoundError(f"{entity_type} not found or not trashed") from exc
                raise
            row = cur.fetchone()
        if not row:
            raise ServiceNotFoundError(f"{entity_type} not found or not trashed")
        self._record_lifecycle_changelog(
            bl,
            entity_type=entity_type,
            entity_id=str(entity_id),
            action="restore",
            actor=resolved.actor_name,
            scope=resolved,
        )
        return RestoreResultView(entity_type=entity_type, entity_id=entity_id)

    def hard_delete(
        self,
        entity_type: str,
        entity_id: str | int,
        *,
        scope: ScopeContext | None = None,
    ) -> HardDeleteResultView:
        cfg = self._entity_config(entity_type)
        bl = self._logger()
        resolved = _service_scope(scope)
        _require_role(resolved, "admin")
        try:
            self._require_direct_entity_in_scope(
                bl,
                cfg=cfg,
                entity_type=str(entity_type or "").strip(),
                entity_id=str(entity_id),
                scope=resolved,
            )
        except ServiceNotFoundError as exc:
            raise ServiceValidationError(
                f"{entity_type} must be trashed before hard delete"
            ) from exc
        if str(entity_type or "").strip() == "mcp_connector":
            raise ServiceConflictError(
                "Purge the MCP connector through its lifecycle plan so its projected tools are purged together"
            )
        if entity_type == "registry_tool":
            from core.application.management.mcp_projected_tool_lifecycle import (
                McpProjectedToolLifecycleService,
                mcp_projected_tool_connector_id,
            )
            from core.application.registry.registry_manager import RegistryManager

            with bl.conn.cursor() as cur:
                cur.execute(
                    "SELECT implementation, deleted_at FROM registry_tools WHERE tool_id = %s",
                    (str(entity_id),),
                )
                raw = cur.fetchone()
            implementation = {}
            if isinstance(raw, Mapping):
                implementation = raw.get("implementation")
            elif raw:
                implementation = raw[0]
            row = {"implementation": implementation, "deleted_at": True}
            if mcp_projected_tool_connector_id(row):
                manager = RegistryManager.instance()
                McpProjectedToolLifecycleService(
                    db_store=manager.db_store,
                    reload_registry=manager.reload_all,
                ).purge_from_trash(
                    str(entity_id),
                    account_id=str(resolved.account_id),
                    project_id=str(resolved.project_id or ""),
                    tenant_id=str(resolved.tenant_id or ""),
                )
                self._record_hard_delete_audit(
                    bl,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    scope=resolved,
                    status="hard_deleted",
                )
                return HardDeleteResultView(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    status="hard_deleted",
                )

        conn = bl.conn
        table = cfg["table"]
        id_col = cfg["id_col"]
        select_query = "".join(
            [
                "SELECT ",
                id_col,
                " FROM ",
                table,
                SQL_WHERE_PREFIX,
                id_col,
                " = %s AND deleted_at IS NOT NULL",
            ]
        )

        # Eligibility: must be trashed first
        with conn.cursor() as cur:
            try:
                cur.execute(
                    select_query,
                    (entity_id,),
                )
            except Exception as exc:
                if _is_invalid_uuid_lookup_error(exc):
                    raise ServiceValidationError(
                        f"{entity_type} must be trashed before hard delete"
                    ) from exc
                raise
            row = cur.fetchone()
        if not row:
            raise ServiceValidationError(f"{entity_type} must be trashed before hard delete")

        if entity_type == "registry_skill":
            self._require_skill_without_dependents(
                bl,
                str(entity_id),
                action="hard deleting",
            )

        if entity_type == "registry_workflow":
            from core.application.workflows.workflow_deletion_guard import (
                assert_workflow_has_no_nonterminal_jobs,
            )

            assert_workflow_has_no_nonterminal_jobs(
                bl,
                workflow_id=str(entity_id),
                account_id=resolved.account_id,
            )

        if entity_type == "registry_agent":
            self._purge_agent_published_blocks(bl, str(entity_id))

        delete_query = "".join(
            [
                "DELETE FROM ",
                table,
                SQL_WHERE_PREFIX,
                id_col,
                " = %s AND deleted_at IS NOT NULL",
            ]
        )
        if entity_type == "job" and callable(getattr(bl, "delete_job", None)):
            job = bl.get_job(str(entity_id)) if callable(getattr(bl, "get_job", None)) else None
            conflict_message = _job_delete_conflict_message(
                str(entity_id),
                job,
                action="hard delete",
            )
            if conflict_message:
                raise ServiceConflictError(conflict_message)
            purged_job = self._purge_job_artifacts_before_delete(bl, str(entity_id))
            deleted = bool(bl.delete_job(str(entity_id), allow_active=False))
            if not deleted:
                raise ServiceNotFoundError(f"{entity_type} not found or not trashed")
            self._finalize_job_artifact_purge(purged_job)
        elif entity_type == "project":
            self._hard_delete_project_direct(bl, str(entity_id))
        elif entity_type == "workspace":
            # A workspace owns rows that carry no database-level foreign key back
            # to it (workspace_data_connections). Deleting the workspaces row on
            # its own leaves those rows pointing at a workspace that no longer
            # exists, so the workspace-specific purge must run here too — not only
            # on the trash-tree bulk path.
            self._hard_delete_workspace(bl, str(entity_id))
        elif entity_type == "registry_workflow":
            from core.infrastructure.db.db_pool import transaction_connection

            sample_workflow = self._workflow_with_sample_files(conn, str(entity_id))
            with transaction_connection(conn) as transaction:
                self._purge_workflow_secrets(
                    workflow_id=str(entity_id),
                    account_id=str(resolved.account_id or ""),
                    transaction=transaction,
                )
                with conn.cursor() as cur:
                    try:
                        cur.execute(
                            delete_query,
                            (entity_id,),
                        )
                    except Exception as exc:
                        if _is_foreign_key_violation(exc):
                            raise ServiceConflictError(
                                f"{entity_type} '{entity_id}' still has dependent records. "
                                "Remove those dependencies before hard deleting it."
                            ) from exc
                        raise
                    if not cur.rowcount:
                        raise ServiceValidationError(
                            f"{entity_type} must be trashed before hard delete"
                        )
            self._delete_workflow_sample_files(sample_workflow)
        else:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        delete_query,
                        (entity_id,),
                    )
                except Exception as exc:
                    if _is_foreign_key_violation(exc):
                        raise ServiceConflictError(
                            f"{entity_type} '{entity_id}' still has dependent records. "
                            "Remove those dependencies before hard deleting it."
                        ) from exc
                    raise
        self._record_hard_delete_audit(
            bl,
            entity_type=entity_type,
            entity_id=entity_id,
            scope=resolved,
            status="hard_deleted",
        )
        return HardDeleteResultView(entity_type=entity_type, entity_id=entity_id)

    @staticmethod
    def _purge_agent_published_blocks(bl: Any, agent_id: str) -> None:
        """Remove the published-block contracts an agent owns, before it is purged.

        A published block is derived from its agent, but hard-deleting the agent
        left the block behind. The orphan then counted as a dependent of the
        agent's skill forever, so that skill could never be trashed or deleted
        through any surface. Removing the derived rows with their owner stops new
        orphans from being created; the dependency query separately ignores
        orphans that already exist.
        """
        normalized = str(agent_id or "").strip()
        if not normalized:
            return
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM registry_published_block_contracts
                 WHERE source_kind = 'agent'
                   AND split_part(source_ref, '::', 1) = %s
                """,
                (normalized,),
            )

    @staticmethod
    def _require_skill_without_dependents(
        bl: Any,
        skill_id: str,
        *,
        action: str,
    ) -> None:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT dependency_kind, dependency_id
                FROM (
                    SELECT 'agent'::text AS dependency_kind,
                           a.agent_id::text AS dependency_id
                    FROM registry_agent_skills relation
                    JOIN registry_agents a ON a.agent_id = relation.agent_id
                    WHERE relation.skill_id = %s

                    UNION

                    SELECT 'workflow_step'::text AS dependency_kind,
                           CONCAT(s.owner_workflow_id, '/', s.step_id)::text
                               AS dependency_id
                    FROM registry_workflow_steps s
                    WHERE s.skill_id = %s

                    UNION

                    -- Only blocks whose owning agent still exists may hold a
                    -- skill. The agent branch above drops deleted agents through
                    -- its JOIN; without the matching guard here, an agent's
                    -- published-block contract outlived the agent's hard delete
                    -- and blocked its skill permanently, through every product
                    -- surface, with no way for an operator to clear it.
                    SELECT 'published_block'::text AS dependency_kind,
                           block_key::text AS dependency_id
                    FROM registry_published_block_contracts blocks
                    WHERE source_kind = 'agent'
                      AND split_part(source_ref, '::', 2) = %s
                      AND EXISTS (
                          SELECT 1
                          FROM registry_agents owner
                          WHERE owner.agent_id = split_part(blocks.source_ref, '::', 1)
                      )
                ) dependencies
                ORDER BY dependency_kind, dependency_id
                LIMIT 26
                """,
                (skill_id, skill_id, skill_id),
            )
            rows = cur.fetchall() or []
        dependent_ids: list[str] = []
        for row in rows[:25]:
            if isinstance(row, Mapping):
                kind = str(row.get("dependency_kind") or "agent").strip() or "agent"
                identity = str(row.get("dependency_id") or "").strip()
            else:
                values = list(row or [])
                kind = str(values[0] if len(values) > 1 else "agent").strip() or "agent"
                raw_identity = values[0] if values else ""
                if len(values) > 1:
                    raw_identity = values[1]
                identity = str(raw_identity).strip()
            if identity:
                dependent_ids.append(f"{kind}:{identity}")
        if dependent_ids:
            truncated = len(rows) > 25
            operator_action = (
                f"Remove or replace every listed dependency before {action} the skill."
            )
            raise ServiceConflictError(
                f"Skill '{skill_id}' still has dependent registry objects: "
                f"{', '.join(dependent_ids)}. {operator_action}",
                payload={
                    "error": "registry_dependency_conflict",
                    "entity_type": "registry_skill",
                    "entity_id": skill_id,
                    "dependent_ids": dependent_ids,
                    "action": operator_action,
                    **({"dependent_ids_truncated": True} if truncated else {}),
                },
            )

    @staticmethod
    def _require_skill_without_enabled_agent_dependents(bl: Any, skill_id: str) -> None:
        TrashLifecycleServicePublicMixin._require_skill_without_dependents(
            bl,
            skill_id,
            action="hard deleting",
        )


__all__ = [name for name in globals() if not name.startswith("__")]
