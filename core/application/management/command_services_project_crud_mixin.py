# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR


class ProjectCommandServiceCrudMixin:
    @staticmethod
    def _require_project_name(name: str) -> None:
        if not str(name or "").strip():
            raise ServiceValidationError(
                "'name' is required and must be non-empty", status_code=422
            )

    def create_project(
        self,
        command: ProjectCreateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectMutationResultView:
        resolved = self._scope(scope)
        self._require_project_name(command.name)
        bl = self._logger()
        form_data = self._normalized_form_data(command.form_data)
        agents_config = self._create_agents_config(command, form_data)
        self._validate_agents_config_models(
            agents_config, conn=getattr(bl, "conn", None), account_id=resolved.account_id
        )
        self._enrich_agents_config_with_provider_ids(
            agents_config,
            conn=getattr(bl, "conn", None),
            account_id=resolved.account_id,
        )
        self._require_workspace_in_scope(
            bl,
            command.workspace_id,
            account_id=resolved.account_id,
            tenant_id=resolved.tenant_id,
            allowed_workspace_ids=resolved.allowed_workspace_ids,
        )
        selected_workflow_ids = self._selected_workflow_ids_for_create(
            command,
            account_id=resolved.account_id,
        )
        resolved_default_workflow = self._resolved_operator_default_workflow(
            command.operator_default_workflow_id,
            selected_workflow_ids,
        )
        project_policy = self._project_policy_payload(form_data)
        create_payload = self._create_project_payload(
            command=command,
            resolved=resolved,
            agents_config=agents_config,
            selected_workflow_ids=selected_workflow_ids,
            resolved_default_workflow=resolved_default_workflow,
            project_policy=project_policy,
        )
        create_payload["create_only"] = True
        extension_runtime_workflow_ids = self._extension_workflow_ids(
            account_id=resolved.account_id
        )
        self._validate_project_workflow_ids(
            bl=bl,
            workflow_ids=selected_workflow_ids,
            extension_workflow_ids=extension_runtime_workflow_ids,
            account_id=resolved.account_id,
        )
        selected_extension_workflow_ids = [
            workflow_id
            for workflow_id in selected_workflow_ids
            if workflow_id in extension_runtime_workflow_ids
        ]
        registry_store = getattr(bl, "db_store", None)
        dsn_value = getattr(bl, "dsn", "")
        dsn = dsn_value.strip() if isinstance(dsn_value, str) else ""
        if registry_store is None and dsn:
            from core.application.registry.db_store import RegistryDBStore

            registry_store = RegistryDBStore(dsn)

        def persist_project(transaction_conn: Any | None = None) -> str:
            self._ensure_extension_workflow_anchors(
                bl=bl,
                workflow_ids=selected_extension_workflow_ids,
                account_id=resolved.account_id,
                actor_id=resolved.actor_id or "api",
                transaction_conn=transaction_conn,
            )
            return self._persist_create_project(bl, create_payload)

        if selected_extension_workflow_ids and registry_store is not None and dsn:
            from core.infrastructure.db.db_pool import (
                active_transaction_connection,
                transaction_connection,
            )

            active_conn = active_transaction_connection(bl)
            if active_conn is not None:
                created_project_id = persist_project(active_conn)
            else:
                with transaction_connection(bl) as transaction_conn:
                    created_project_id = persist_project(transaction_conn)
        else:
            created_project_id = persist_project()
        self._set_initial_project_schedule_timezone(
            bl=bl,
            project_id=created_project_id,
            resolved=resolved,
        )
        assigned_data_connections = self._persist_project_data_connections(
            bl=bl,
            project_id=created_project_id,
            data_connection_ids=command.data_connection_ids,
        )

        self._schedule_project_profile_sync(
            self._project_profile_payload(
                project_id=created_project_id,
                account_id=resolved.account_id,
                name=str(command.name or "").strip(),
                agents_config=dict(create_payload.get("agents_config") or {}),
                project_rules=str(command.project_rules or "").strip(),
                assigned_data_connections=assigned_data_connections,
            )
        )
        self._create_project_revision(
            bl,
            created_project_id,
            author=command.author,
            action="create",
            source="project_create_form",
        )
        return ProjectMutationResultView(
            status="created",
            project_id=str(created_project_id),
            name=str(command.name or "").strip(),
        )

    def create_extension_project(
        self,
        command: ExtensionProjectCreateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionProjectCreateResultView:
        del command, scope
        raise ServiceValidationError("Extensions are managed through the Extensions surface.")

    def update_project(
        self,
        project_id: Any,
        command: ProjectUpdateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectMutationResultView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        existing_project = self._require_project_in_scope(bl, project_id, resolved)
        self._require_project_name(command.name)
        try:
            uuid.UUID(str(command.workspace_id).strip())
        except ValueError as exc:
            raise ServiceValidationError("Invalid workspace_id") from exc
        self._require_workspace_in_scope(
            bl,
            command.workspace_id,
            account_id=resolved.account_id,
            tenant_id=resolved.tenant_id,
            allowed_workspace_ids=resolved.allowed_workspace_ids,
        )
        previous_data_connection_ids = (
            self._data_connection_ids(bl.list_project_data_connections(str(project_id)))
            if hasattr(bl, "list_project_data_connections")
            else []
        )
        form_data = self._normalized_form_data(command.form_data)
        agents_config = self._update_agents_config(existing_project, command, form_data)
        self._validate_agents_config_models(
            agents_config, conn=getattr(bl, "conn", None), account_id=resolved.account_id
        )
        self._enrich_agents_config_with_provider_ids(
            agents_config,
            conn=getattr(bl, "conn", None),
            account_id=resolved.account_id,
        )
        selected_workflow_ids = self._selected_workflow_ids_for_update(command)
        resolved_default_workflow = self._resolved_operator_default_workflow(
            command.operator_default_workflow_id,
            selected_workflow_ids,
        )
        project_policy = (
            self._project_policy_payload(form_data)
            if self._project_policy_form_present(form_data)
            else None
        )
        data_connection_ids = (
            command.data_connection_ids
            if "data_connection_ids" in form_data
            else previous_data_connection_ids
        )
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(bl):
            update_payload = self._persist_update_project(
                bl=bl,
                project_id=project_id,
                command=command,
                agents_config=agents_config,
                selected_workflow_ids=selected_workflow_ids,
                resolved_default_workflow=resolved_default_workflow,
                project_policy=project_policy,
                account_id=resolved.account_id,
                actor_id=resolved.actor_id or "api",
                existing_project=dict(existing_project),
            )
            assigned_data_connections = self._persist_project_data_connections(
                bl=bl,
                project_id=project_id,
                data_connection_ids=data_connection_ids,
            )

        self._schedule_project_profile_sync(
            self._project_profile_payload(
                project_id=project_id,
                account_id=resolved.account_id,
                name=str(command.name or "").strip(),
                agents_config=agents_config,
                project_rules=str(update_payload.get("project_rules") or "").strip(),
                assigned_data_connections=assigned_data_connections,
            )
        )

        self._create_project_revision(
            bl,
            project_id,
            author=command.author,
            action="save",
            source="project_edit_form",
        )
        return ProjectMutationResultView(
            status="updated",
            project_id=str(project_id),
            name=str(command.name or "").strip(),
        )

    def move_project_to_workspace(
        self,
        project_id: Any,
        workspace_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectMutationResultView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        project = self._require_project_in_scope(bl, project_id, resolved)
        normalized_workspace_id = self._required_workspace_id(workspace_id)
        self._require_workspace_in_scope(
            bl,
            normalized_workspace_id,
            account_id=resolved.account_id,
            tenant_id=resolved.tenant_id,
            allowed_workspace_ids=resolved.allowed_workspace_ids,
        )
        if str(project.get("workspace_id") or "").strip() == normalized_workspace_id:
            return ProjectMutationResultView(
                status="moved",
                project_id=str(project_id),
                name=str(project.get("name") or "").strip(),
            )
        supports_updated_at = False
        column_exists = getattr(bl, "_column_exists", None)
        if callable(column_exists):
            try:
                supports_updated_at = bool(column_exists("projects", "updated_at"))
            except Exception:
                supports_updated_at = False
        with bl.conn.cursor() as cur:
            if supports_updated_at:
                cur.execute(
                    """
                    UPDATE projects
                       SET workspace_id = %s,
                           updated_at = now()
                     WHERE id = %s
                    RETURNING id
                    """,
                    (normalized_workspace_id, str(project_id)),
                )
            else:
                cur.execute(
                    """
                    UPDATE projects
                       SET workspace_id = %s
                     WHERE id = %s
                    RETURNING id
                    """,
                    (normalized_workspace_id, str(project_id)),
                )
            row = cur.fetchone()
        if not row:
            raise ServiceNotFoundError(PROJECT_NOT_FOUND_MESSAGE)
        return ProjectMutationResultView(
            status="moved",
            project_id=str(project_id),
            name=str(project.get("name") or "").strip(),
        )

    def delete_project(
        self, project_id: Any, *, scope: ScopeContext | None = None
    ) -> ProjectDeleteResultView:
        """Soft-delete (trash) a project.  Use TrashLifecycleService for restore / hard-delete."""
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        self._require_project_in_scope(bl, str(project_id), resolved)
        try:
            with bl.conn.cursor() as cur:
                cur.execute(
                    "UPDATE projects SET deleted_at = NOW(), deleted_by = %s "
                    "WHERE id = %s AND deleted_at IS NULL",
                    (resolved.actor_name, str(project_id)),
                )
        except Exception as exc:
            raise ServiceError("Failed to delete project") from exc
        return ProjectDeleteResultView(project_id=str(project_id))

    def update_notification_routes(
        self,
        project_id: Any,
        command: ProjectNotificationRoutesUpdateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationRoutesUpdateResultView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        self._require_project_in_scope(bl, str(project_id), resolved)
        if not hasattr(bl, "save_notification_routes"):
            raise ServiceUnavailableError("Notification routing persistence is not available")
        routes = [dict(item) for item in (command.routes or []) if isinstance(item, dict)]
        try:
            bl.save_notification_routes(str(project_id), routes)
            persisted = (
                bl.list_notification_routes(str(project_id), enabled_only=False)
                if hasattr(bl, "list_notification_routes")
                else []
            )
        except ValueError as exc:
            # The store marks a duplicate key as a conflict; everything else it
            # refuses is a value the caller can correct.
            if getattr(exc, "is_conflict", False):
                raise ServiceConflictError(str(exc)) from exc
            raise ServiceValidationError(str(exc)) from exc
        except Exception as exc:
            raise ServiceError("Failed to update notification routes") from exc
        return ProjectNotificationRoutesUpdateResultView(
            project_id=str(project_id),
            notification_routes=[dict(row) for row in (persisted or []) if isinstance(row, dict)],
        )

    def dispatch_project_notification(
        self,
        project_id: Any,
        command: ProjectNotificationDispatchInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationDispatchResultView:
        resolved = self._scope(
            scope,
            project_id=project_id,
            workflow_id=command.workflow_id,
        )
        bl = self._logger()
        self._require_project_in_scope(bl, str(project_id), resolved)

        dispatcher = self._dispatch_project_notification_event
        if dispatcher is None:
            from core.application.notifications.dispatch import dispatch_project_notification_event

            dispatcher = dispatch_project_notification_event

        try:
            deliveries = dispatcher(
                bl,
                project_id=str(project_id),
                event_type=str(command.event_type or "").strip(),
                severity=str(command.severity or "info").strip() or "info",
                workflow_id=str(command.workflow_id or "").strip() or None,
                job_id=str(command.job_id).strip() if command.job_id is not None else None,
                payload=dict(command.payload or {}),
                account_id=resolved.account_id,
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except Exception as exc:
            raise ServiceError("Failed to dispatch project notification") from exc

        return ProjectNotificationDispatchResultView(
            project_id=str(project_id),
            notification_deliveries=[
                dict(row) for row in (deliveries or []) if isinstance(row, dict)
            ],
        )

    @staticmethod
    def _current_operator_default_workflow_id(bl: Any, project_id: str) -> str:
        """Return the project's current operator default.

        Only read when the caller omitted the field, i.e. when the answer decides
        whether the existing default is preserved. Substituting "" on a failed read
        would delete it, so an unreadable store fails the request instead: refusing
        to link is recoverable, silently clearing someone else's default is not.
        """
        getter = getattr(bl, "get_project_workflows", None)
        if not callable(getter):
            raise ServiceUnavailableError(
                "Cannot read the project's current operator default workflow, so the "
                "request was refused rather than risk clearing it. Send "
                "operator_default_workflow_id explicitly to set or clear it."
            )
        try:
            rows = getter(project_id, enabled_only=False) or []
        except Exception as exc:
            raise ServiceUnavailableError(
                "Cannot read the project's current operator default workflow, so the "
                "request was refused rather than risk clearing it. Send "
                "operator_default_workflow_id explicitly to set or clear it."
            ) from exc
        return next(
            (
                str(row.get("workflow_id") or "").strip()
                for row in rows
                if isinstance(row, dict) and bool(row.get("is_default"))
            ),
            "",
        )

    def update_project_workflows(
        self,
        project_id: Any,
        command: ProjectWorkflowBindingsInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectWorkflowBindingsResultView:
        resolved = self._scope(
            scope, project_id=project_id, workflow_id=command.operator_default_workflow_id
        )
        _require_role(resolved, "admin")
        require_default_workflow_catalog_account(resolved)
        bl = self._logger()
        self._require_project_in_scope(bl, str(project_id), resolved)
        # An omitted operator default means "leave it alone". Passing it straight
        # through cleared the project's default every time someone linked a
        # workflow; the sibling project update endpoint already re-seeds it.
        requested_default = command.operator_default_workflow_id
        preserve_default = requested_default is None
        if preserve_default:
            requested_default = self._current_operator_default_workflow_id(bl, str(project_id))
        try:
            from core.infrastructure.db.db_pool import transaction_connection

            with transaction_connection(bl) as transaction_conn:
                self._persist_authoritative_project_workflow_bindings(
                    bl=bl,
                    project_id=str(project_id),
                    workflow_ids=command.workflow_ids or [],
                    operator_default_workflow_id=requested_default,
                    account_id=resolved.account_id,
                    actor_id=resolved.actor_id or "api",
                    transaction_conn=transaction_conn,
                    preserve_default=preserve_default,
                )
        except (ServiceUnavailableError, ServiceValidationError):
            raise
        except Exception as exc:
            raise ServiceError("Failed to update project workflows") from exc

        project_workflows = (
            bl.get_project_workflows(str(project_id), enabled_only=False, include_deleted=False)
            if hasattr(bl, "get_project_workflows")
            else []
        )
        workflow_ids = [
            str(item.get("workflow_id") or "").strip()
            for item in (project_workflows or [])
            if (
                isinstance(item, dict)
                and item.get("enabled") is not False
                and str(item.get("workflow_id") or "").strip()
            )
        ]
        operator_default_workflow_id = next(
            (
                str(item.get("workflow_id") or "").strip()
                for item in (project_workflows or [])
                if (
                    isinstance(item, dict)
                    and item.get("enabled") is not False
                    and bool(item.get("is_default"))
                )
            ),
            "",
        )
        return ProjectWorkflowBindingsResultView(
            project_id=str(project_id),
            workflow_ids=workflow_ids,
            operator_default_workflow_id=operator_default_workflow_id,
            project_workflows=list(project_workflows or []),
        )

    def update_project_policy(
        self,
        project_id: Any,
        command: ProjectPolicyUpdateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectPolicyResultView:
        resolved = self._scope(scope, project_id=project_id)
        _require_role(resolved, "admin")
        bl = self._logger()
        self._require_project_in_scope(bl, str(project_id), resolved)

        supplied = command.model_dump(exclude_none=True)
        project_config = (
            bl.get_project_run_config(str(project_id))
            if hasattr(bl, "get_project_run_config")
            else {}
        )
        # AC17-R58: the write below is an unconditional UPSERT of every policy column, so a
        # field the caller did not mention has to be carried over from what is stored rather
        # than filled from a model default. Otherwise `{"concurrency_cap": 2}` quietly resets
        # autonomy mode, both retry backoffs and the tool policy mode.
        stored_policy = (
            dict(project_config.get("project_policy") or {})
            if isinstance(project_config, dict)
            else {}
        )
        policy = {**stored_policy, **supplied}
        try:
            if hasattr(bl, "save_project_policy"):
                bl.save_project_policy(str(project_id), policy)
        except Exception as exc:
            raise ServiceError("Failed to update project policy") from exc

        return ProjectPolicyResultView(
            project_id=str(project_id),
            policy=bl.get_project_policy(str(project_id))
            if hasattr(bl, "get_project_policy")
            else {},
        )

    def restore_project_settings_revision(
        self,
        project_id: Any,
        revision_id: Any,
        command: ProjectSettingsRestoreInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectSettingsRestoreResultView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        self._require_project_in_scope(bl, str(project_id), resolved)
        if not hasattr(bl, "restore_project_settings_revision"):
            raise ServiceUnavailableError(
                "Project settings history is not available in this schema"
            )
        try:
            result = bl.restore_project_settings_revision(
                str(project_id),
                str(revision_id),
                author=str(command.author or "system").strip() or "system",
            )
        except ValueError as exc:
            raise ServiceNotFoundError(str(exc)) from exc
        except Exception as exc:
            raise ServiceError("Failed to restore project settings revision") from exc
        payload = result if isinstance(result, dict) else {}
        return ProjectSettingsRestoreResultView(**payload)


__all__ = [name for name in globals() if not name.startswith("__")]
