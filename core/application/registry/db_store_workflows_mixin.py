# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from core.application.registry.step_skill_resolution import (
    agent_primary_skill_id,
    skill_is_host_owned,
)
from core.application.workflows.workflow_canvas_annotation_contract import (
    ANNOTATIONS_REVISION_SETTING,
    ANNOTATIONS_SETTING,
    WorkflowAnnotationsRevisionConflictError,
)
from core.application.workflows.workflow_history_snapshot import history_snapshot
from core.application.workflows.workflow_reaction_scope_repository import (
    retained_reaction_scope_issues,
)

from .db_store_common import *  # NOSONAR
from .workflow_definition_rows import (
    WorkflowDefinitionRows,
    _definition_for_each_payload,
    _definition_group_payload,
    _definition_payload,
    _definition_step_payload,
    load_workflow_definition_rows,
)


class WorkflowRevisionConflictError(RuntimeError):
    """Raised when a workflow compare-and-swap update loses a revision race."""

    def __init__(self, workflow_id: str, expected_revision: int):
        self.workflow_id = str(workflow_id or "").strip()
        self.expected_revision = int(expected_revision)
        super().__init__(
            f"Workflow '{self.workflow_id}' revision conflict (expected {self.expected_revision})"
        )


class WorkflowCreateConflictError(ValueError):
    """Atomic create found an occupied workflow identity."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"Workflow '{workflow_id}' already exists")
        self.workflow_id = workflow_id


class RegistryDBStoreWorkflowsMixin:
    def get_workflow(self, workflow_id: str, include_trashed: bool = False) -> dict | None:
        with self._cursor() as cur:
            clauses = ["workflow_id = %s"]
            if not include_trashed and self._column_exists("registry_workflows", "deleted_at"):
                clauses.append(SQL_DELETED_AT_IS_NULL)
            cur.execute(
                f"SELECT * FROM registry_workflows WHERE {SQL_AND.join(clauses)}",
                (workflow_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            wf = dict(row)
            self._attach_workflow_definitions(cur, [wf])
            return wf

    def list_workflows(
        self,
        enabled_only: bool = False,
        include_definitions: bool = True,
        include_trashed: bool = False,
    ) -> list[dict]:
        with self._cursor() as cur:
            conditions: list[str] = []
            if not include_trashed and self._column_exists("registry_workflows", "deleted_at"):
                conditions.append(SQL_DELETED_AT_IS_NULL)
            if enabled_only:
                conditions.append(SQL_ENABLED_TRUE)
            where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""
            cur.execute(f"SELECT * FROM registry_workflows {where_sql} ORDER BY workflow_id")
            workflows = [dict(r) for r in cur.fetchall()]
            if include_definitions:
                self._attach_workflow_definitions(cur, workflows)
            else:
                for wf in workflows:
                    wf["steps"] = []
                    wf["groups"] = []
                    wf["for_each"] = []
            return workflows

    def list_workflows_summary(
        self,
        enabled_only: bool = False,
        *,
        account_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        has_builder_metadata = self._column_exists("registry_workflows", "builder_metadata_jsonb")
        has_account_id = self._column_exists("registry_workflows", "account_id")
        has_project_id = self._column_exists("registry_workflows", "project_id")
        has_scope_kind = self._column_exists("registry_workflows", "scope_kind")
        has_source_install_id = self._column_exists("registry_workflows", "source_install_id")
        has_availability_status = self._column_exists("registry_workflows", "availability_status")
        has_new_runs_blocked = self._column_exists("registry_workflows", "new_runs_blocked")
        has_visible_in_catalog = self._column_exists("registry_workflows", "visible_in_catalog")
        clauses: list[str] = []
        if self._column_exists("registry_workflows", "deleted_at"):
            clauses.append(W_DELETED_AT_IS_NULL_VALUE)
        if enabled_only:
            clauses.append("w.enabled = true")
        normalized_account_id = str(account_id or "").strip()
        normalized_project_id = str(project_id or "").strip()
        if has_account_id and normalized_account_id:
            clauses.append(
                f"({_nullable_scope_sql('w.account_id')} OR w.account_id = %(account_id)s)"
            )
        if has_project_id:
            if normalized_project_id:
                clauses.append(
                    f"({_nullable_scope_sql('w.project_id')} OR w.project_id = %(project_id)s)"
                )
            else:
                clauses.append(_nullable_scope_sql("w.project_id"))
        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""

        with self._cursor() as cur:
            cur.execute(
                f"""SELECT
                        w.workflow_id,
                        w.version,
                        w.display_name,
                        LEFT(COALESCE(w.description, ''), 180) AS description_preview,
                        w.enabled,
                        COALESCE(w.trigger->>'confidence_threshold', '') AS confidence_threshold,
                        COALESCE(step_counts.step_count, 0) AS step_count
                        {", w.account_id" if has_account_id else ""}
                        {", w.project_id" if has_project_id else ""}
                        {", COALESCE(w.scope_kind, '') AS scope_kind" if has_scope_kind else ""}
                        {", COALESCE(w.source_install_id, '') AS source_install_id" if has_source_install_id else ""}
                        {", COALESCE(w.availability_status, '') AS availability_status" if has_availability_status else ""}
                        {", COALESCE(w.new_runs_blocked, false) AS new_runs_blocked" if has_new_runs_blocked else ""}
                        {", COALESCE(w.visible_in_catalog, true) AS visible_in_catalog" if has_visible_in_catalog else ""}
                        {", COALESCE(w.builder_metadata_jsonb, '{}'::jsonb) AS builder_metadata_jsonb" if has_builder_metadata else ""}
                    FROM registry_workflows w
                    LEFT JOIN (
                        SELECT owner_workflow_id, COUNT(*)::INT AS step_count
                        FROM registry_workflow_steps
                        GROUP BY owner_workflow_id
                    ) step_counts ON step_counts.owner_workflow_id = w.workflow_id
                    {where_sql}
                    ORDER BY w.workflow_id""",
                {
                    "account_id": normalized_account_id or None,
                    "project_id": normalized_project_id or None,
                },
            )
            return [dict(r) for r in cur.fetchall()]

    def list_project_workflows(
        self,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if project_id is not None:
            clauses.append("pw.project_id = %s")
            params.append(str(project_id))
        if project_name:
            clauses.append("p.name = %s")
            params.append(str(project_name).strip())
        if enabled_only:
            clauses.append("pw.enabled = true")
            clauses.append("pw.deleted_at IS NULL")
            clauses.append("w.enabled = true")

        where_sql = f"WHERE {SQL_AND.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"""SELECT
                        pw.project_id,
                        p.name AS project_name,
                        pw.workflow_id,
                        w.display_name AS workflow_display_name,
                        pw.is_default,
                        pw.priority,
                        pw.enabled,
                        pw.deleted_at
                    FROM project_workflows pw
                    JOIN projects p ON p.id = pw.project_id
                    JOIN registry_workflows w ON w.workflow_id = pw.workflow_id
                    {where_sql}
                    ORDER BY pw.project_id, pw.is_default DESC, pw.priority DESC, pw.workflow_id""",
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def save_project_workflows(
        self,
        project_id: str,
        workflow_ids: list[str],
        operator_default_workflow_id: str = "",
    ) -> None:
        normalized_ids = [
            str(wf or "").strip() for wf in (workflow_ids or []) if str(wf or "").strip()
        ]
        deduped_ids = list(dict.fromkeys(normalized_ids))
        default_id = str(operator_default_workflow_id or "").strip()
        if not deduped_ids:
            raise ValueError("At least one workflow_id is required for project_workflows")

        with self._transaction() as cur:
            # Disable links not present in the new selection. A hook's storage
            # workflow is never listed, so no selection names it: its link stays.
            cur.execute(
                """UPDATE project_workflows
                   SET enabled = false, is_default = false
                   WHERE project_id = %s
                     AND workflow_id <> ALL(%s)
                     AND NOT EXISTS (SELECT 1 FROM registry_workflows hook_rw
                                     WHERE hook_rw.workflow_id = project_workflows.workflow_id
                                       AND hook_rw.settings ? 'hook_storage')""",
                (project_id, deduped_ids),
            )

            # Upsert selected links.
            for idx, workflow_id in enumerate(deduped_ids):
                cur.execute(
                    """INSERT INTO project_workflows
                       (project_id, workflow_id, is_default, priority, enabled, deleted_at)
                       VALUES (%s, %s, false, %s, true, NULL)
                       ON CONFLICT (project_id, workflow_id) DO UPDATE SET
                           priority = EXCLUDED.priority,
                           enabled = true,
                           deleted_at = NULL""",
                    (project_id, workflow_id, max(0, len(deduped_ids) - idx)),
                )

            cur.execute(
                "UPDATE project_workflows SET is_default = false WHERE project_id = %s",
                (project_id,),
            )
            if default_id:
                cur.execute(
                    """UPDATE project_workflows
                       SET is_default = true
                       WHERE project_id = %s
                         AND workflow_id = %s
                         AND enabled = true
                         AND deleted_at IS NULL""",
                    (project_id, default_id),
                )

    def get_extension_connection_metadata(self, connection_id: str) -> dict[str, Any] | None:
        """Non-secret connection metadata for workflow readiness validation.

        Readiness runs with this store and needs to know whether a selected
        connection is usable — which extension and install it belongs to, its
        type, whether it is enabled and healthy, and its OAuth lifecycle and
        granted scopes. It deliberately does not expose credentials,
        `secret_refs`, or the unrestricted connection configuration: none of
        that is needed to answer "can this step run".
        """
        from core.infrastructure.db.project_connection_store import (
            get_project_connection_runtime_metadata,
        )

        return get_project_connection_runtime_metadata(self.conn, str(connection_id or "").strip())

    def add_project_workflow_binding(self, project_id: str, workflow_id: str) -> None:
        """Add one enabled binding without rewriting any sibling binding."""
        with self._transaction() as cur:
            cur.execute(
                """INSERT INTO project_workflows
                   (project_id, workflow_id, is_default, priority, enabled, deleted_at)
                   VALUES (%s, %s, false, 0, true, NULL)
                   ON CONFLICT (project_id, workflow_id) DO UPDATE SET
                       enabled = true,
                       deleted_at = NULL""",
                (project_id, workflow_id),
            )

    def _attach_workflow_definitions(self, cur, workflows: list[dict]) -> None:
        workflow_ids = [
            str((wf or {}).get("workflow_id") or "").strip()
            for wf in (workflows or [])
            if str((wf or {}).get("workflow_id") or "").strip()
        ]
        definitions = self._load_workflow_definition_map(cur, workflow_ids)
        for wf in workflows or []:
            workflow_id = str((wf or {}).get("workflow_id") or "").strip()
            rows = definitions.get(workflow_id) or WorkflowDefinitionRows.empty()
            wf["steps"] = rows.steps
            wf["groups"] = rows.groups
            wf["for_each"] = rows.for_each

    def _load_workflow_definition_map(
        self, cur, workflow_ids: list[str]
    ) -> dict[str, WorkflowDefinitionRows]:
        """Steps, Groups and For each containers per workflow, keyed by business ids."""
        return load_workflow_definition_rows(
            cur,
            workflow_ids,
            derive_skill=lambda agent_id: agent_primary_skill_id(self, agent_id),
        )

    def _load_workflow_definition(self, cur, workflow_id: str) -> WorkflowDefinitionRows:
        return (
            self._load_workflow_definition_map(cur, [workflow_id]).get(
                str(workflow_id or "").strip()
            )
            or WorkflowDefinitionRows.empty()
        )

    def save_workflow_canvas_annotations(
        self,
        workflow_id: str,
        *,
        account_id: str,
        annotations: list[dict],
        expected_revision: int,
        expected_annotations_revision: int,
    ) -> int:
        """Write presentation only, sharing the definition writer's row lock.

        Notes have their own CAS and never create a definition revision, sync
        runtime dependencies, or add a full definition to the changelog.
        """
        with self._transaction() as cur:
            cur.execute(
                "SELECT revision, settings FROM registry_workflows "
                "WHERE workflow_id = %s AND deleted_at IS NULL "
                "AND (account_id = %s OR account_id IS NULL) FOR UPDATE",
                (workflow_id, account_id),
            )
            current = cur.fetchone()
            if current is None or current["revision"] != expected_revision:
                raise WorkflowRevisionConflictError(workflow_id, expected_revision)
            notes_revision = int((current["settings"] or {}).get(ANNOTATIONS_REVISION_SETTING, 0))
            if notes_revision != expected_annotations_revision:
                raise WorkflowAnnotationsRevisionConflictError(
                    workflow_id, expected_annotations_revision
                )
            next_revision = notes_revision + 1
            cur.execute(
                "UPDATE registry_workflows SET settings = "
                "jsonb_set(jsonb_set(settings, '{canvas_annotations}', %s::jsonb), "
                "'{canvas_annotations_revision}', %s::jsonb), updated_at = NOW() "
                "WHERE workflow_id = %s",
                (json.dumps(annotations), json.dumps(next_revision), workflow_id),
            )
        return next_revision

    def save_workflow(
        self,
        data: dict,
        author: str = "admin",
        *,
        expected_revision: int | None = None,
        create_only: bool = False,
    ) -> int | None:
        workflow_id = str(data.get("workflow_id") or data.get("workflow_key") or "").strip()
        if not workflow_id:
            raise ValueError("workflow_id is required")
        if create_only and expected_revision is not None:
            raise ValueError("create_only cannot use expected_revision")
        is_new = create_only or not self._exists("registry_workflows", "workflow_id", workflow_id)
        definition_payload = _definition_payload(data)
        workflow_payload_for_dependencies = None
        if definition_payload is not None:
            workflow_payload_for_dependencies = dict(data)
            workflow_payload_for_dependencies["steps"] = definition_payload.steps
            workflow_payload_for_dependencies["groups"] = definition_payload.groups
            workflow_payload_for_dependencies["for_each"] = definition_payload.for_each

        with self._transaction() as cur:
            has_revision_col = self._column_exists("registry_workflows", "revision")
            if expected_revision is not None:
                # CAS is update-only. An UPSERT predicate protects only its
                # conflict branch and would otherwise recreate a missing row.
                if not has_revision_col:
                    raise WorkflowRevisionConflictError(workflow_id, expected_revision)
                cur.execute(
                    "SELECT revision FROM registry_workflows "
                    "WHERE workflow_id = %s AND deleted_at IS NULL FOR UPDATE",
                    (workflow_id,),
                )
                locked = cur.fetchone()
                if locked is None or int(locked["revision"]) != int(expected_revision):
                    raise WorkflowRevisionConflictError(workflow_id, expected_revision)
                is_new = False
            incoming_settings = dict(data.get("settings") or {})
            incoming_settings.pop(ANNOTATIONS_REVISION_SETTING, None)
            if ANNOTATIONS_SETTING in incoming_settings:
                incoming_settings[ANNOTATIONS_REVISION_SETTING] = 0
            if not is_new:
                # A shared child never changes under its callers silently.
                from core.application.workflows.workflow_consumer_guard import (
                    assert_consumers_fit,
                )

                assert_consumers_fit(
                    self,
                    cur,
                    workflow_id=workflow_id,
                    settings=incoming_settings,
                    definition=(
                        {"steps": definition_payload.steps}
                        if definition_payload is not None
                        else None
                    ),
                )
            payload = {
                "workflow_key": workflow_id,
                "version": data.get("version", "1.0.0"),
                "display_name": data.get("display_name", workflow_id),
                "description": data.get("description", ""),
                "enabled": data.get("enabled", True),
                "trigger": json.dumps(data.get("trigger", {})),
                "settings": json.dumps(incoming_settings),
                "tags": data.get("tags", []),
                "builder_metadata_jsonb": json.dumps(
                    normalize_builder_metadata(data.get("builder_metadata_jsonb"))
                ),
            }
            columns = [
                "workflow_key",
                "version",
                "display_name",
                "description",
                "enabled",
                "trigger",
                "settings",
                "tags",
            ]
            updates = [
                VERSION_EXCLUDED_VERSION_SQL,
                DISPLAY_NAME_EXCLUDED_DISPLAY_NAME_SQL,
                DESCRIPTION_EXCLUDED_DESCRIPTION_SQL,
                ENABLED_EXCLUDED_ENABLED_SQL,
                "trigger = EXCLUDED.trigger",
                # The UPDATE row lock protects presentation saved after the caller
                # read its definition. Only the note writer may replace these keys.
                "settings = (EXCLUDED.settings - 'canvas_annotations' - 'canvas_annotations_revision') "
                "|| (SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) "
                "FROM jsonb_each(registry_workflows.settings) "
                "WHERE key IN ('canvas_annotations', 'canvas_annotations_revision'))",
                TAGS_EXCLUDED_TAGS_SQL,
            ]
            optional_columns = [
                ("builder_metadata_jsonb", payload.get("builder_metadata_jsonb")),
                ("account_id", _system_registry_account_id(data)),
                ("scope_kind", str(data.get("scope_kind") or "system").strip() or "system"),
                ("source_install_id", str(data.get("source_install_id") or "").strip() or None),
                ("source_release_id", str(data.get("source_release_id") or "").strip() or None),
                ("source_projection_id", data.get("source_projection_id")),
                (
                    "local_workflow_id",
                    str(data.get("local_workflow_id") or workflow_id).strip() or workflow_id,
                ),
                (
                    "projection_mode",
                    str(data.get("projection_mode") or "manual").strip() or "manual",
                ),
                ("derived_from_kind", str(data.get("derived_from_kind") or "").strip()),
                ("derived_from_ref", str(data.get("derived_from_ref") or "").strip()),
                ("sync_status", str(data.get("sync_status") or "active").strip() or "active"),
                (
                    "availability_status",
                    str(data.get("availability_status") or "available").strip() or "available",
                ),
                (
                    "availability_reason_code",
                    str(data.get("availability_reason_code") or "none").strip() or "none",
                ),
                (
                    "availability_reason_text",
                    str(data.get("availability_reason_text") or "").strip(),
                ),
                ("new_runs_blocked", bool(data.get("new_runs_blocked", False))),
                ("visible_in_catalog", bool(data.get("visible_in_catalog", True))),
            ]
            for column_name, column_value in optional_columns:
                if not self._column_exists("registry_workflows", column_name):
                    continue
                columns.append(column_name)
                updates.append(f"{column_name} = EXCLUDED.{column_name}")
                payload[column_name] = column_value

            if has_revision_col:
                updates.append("revision = registry_workflows.revision + 1")

            # Saving a workflow asserts it exists and is live. delete_workflow is a
            # SOFT delete (sets deleted_at); without clearing it here, re-creating a
            # previously-deleted workflow_id would update the trashed row but leave
            # deleted_at set, so the workflow stays invisible to get_workflow /
            # list_workflows. Clear it on the upsert's UPDATE path so re-creating a
            # deleted id resurrects it. (No-op for an already-live row.)
            if self._column_exists("registry_workflows", "deleted_at"):
                updates.append(DELETED_AT_NULL_SQL)
            if self._column_exists("registry_workflows", "deleted_by"):
                updates.append("deleted_by = NULL")

            placeholders = ", ".join(f"%({column})s" for column in columns)
            col_sql = ", ".join(columns)
            update_sql = ", ".join(updates)
            revision_predicate = ""
            returning_revision = ""
            if has_revision_col:
                returning_revision = " RETURNING revision, settings"
                if expected_revision is not None:
                    payload["expected_revision"] = max(1, int(expected_revision))
                    revision_predicate = (
                        " WHERE registry_workflows.revision = %(expected_revision)s"
                    )
            # Both workflow_key and its generated workflow_id are unique.
            # Atomic create must ignore conflicts on either identity constraint.
            conflict_target = "" if create_only else " (workflow_key)"
            conflict_action = (
                "DO NOTHING" if create_only else f"DO UPDATE SET {update_sql}{revision_predicate}"  # nosec B608 -- fixed columns and predicate; payload values are bound.
            )
            cur.execute(
                f"""INSERT INTO registry_workflows ({col_sql})
                    VALUES ({placeholders})
                    ON CONFLICT{conflict_target} {conflict_action}{returning_revision}""",
                payload,
            )
            # The SQL statement owns the conflict check, including writers that
            # never participate in any advisory lock or optimistic existence read.
            if create_only and cur.rowcount == 0:
                raise WorkflowCreateConflictError(workflow_id)
            saved_revision = None
            if has_revision_col:
                saved_row = cur.fetchone()
                if saved_row is None and expected_revision is not None:
                    raise WorkflowRevisionConflictError(workflow_id, expected_revision)
                if saved_row is not None:
                    saved_revision = int(saved_row.get("revision") or 0) or None
            # Sync steps and groups (skip without a definition — metadata-only update)
            if definition_payload is not None:
                reaction_issues = retained_reaction_scope_issues(
                    cur,
                    workflow_id,
                    steps=definition_payload.steps,
                    groups=definition_payload.groups,
                )
                if reaction_issues:
                    raise ValueError(reaction_issues[0]["message"])
                self._sync_workflow_definition_cur(cur, workflow_id, definition_payload)
            # History records what is stored now, read back in this transaction:
            # a settings-only payload carries no definition, and its revision is
            # the one the caller loaded.
            stored = self.get_workflow(workflow_id)
            if not isinstance(stored, dict):
                raise LookupError(f"Workflow '{workflow_id}' was not stored")
            self._log_change_cur(
                cur,
                "workflow",
                workflow_id,
                "create" if is_new else "update",
                history_snapshot(stored),
                author,
            )
            self._sync_workflow_runtime_dependencies(
                workflow_id,
                workflow_payload=workflow_payload_for_dependencies,
            )
        self._trigger_public_contract_projection_sync()
        return saved_revision

    def _sync_workflow_definition_cur(
        self, cur, workflow_id: str, definition: WorkflowDefinitionRows
    ) -> None:
        """Write the definition so it matches the payload, keeping row UUIDs.

        Groups, For each containers and steps are upserted on their business
        ids, so an unchanged node keeps its row. Steps move before containers
        are dropped: a step taken out of a removed container must not be
        cascaded away with it.
        """
        group_row_ids: dict[str, Any] = {}
        for index, group in enumerate(definition.groups):
            group_id = str(group.get("group_id") or "").strip()
            if not group_id:
                raise ValueError("Every group needs a group_id")
            cur.execute(
                """INSERT INTO registry_workflow_groups
                       (workflow_id, group_id, display_name, description, sort_order,
                        body_jsonb, error_policy_jsonb, boundary_contract_jsonb)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (workflow_id, group_id) DO UPDATE SET
                       display_name = EXCLUDED.display_name,
                       description = EXCLUDED.description,
                       sort_order = EXCLUDED.sort_order,
                       body_jsonb = EXCLUDED.body_jsonb,
                       error_policy_jsonb = EXCLUDED.error_policy_jsonb,
                       boundary_contract_jsonb = EXCLUDED.boundary_contract_jsonb
                   RETURNING id""",
                (
                    workflow_id,
                    group_id,
                    str(group.get("display_name") or group_id).strip(),
                    str(group.get("description") or ""),
                    index,
                    json.dumps(group.get("body") or {}),
                    json.dumps(group.get("error_policy") or {}),
                    _json_or_none(group.get("boundary_contract")),
                ),
            )
            group_row_ids[group_id] = cur.fetchone()["id"]

        for_each_row_ids: dict[str, Any] = {}
        for index, item in enumerate(definition.for_each):
            for_each_id = str(item.get("for_each_id") or "").strip()
            if not for_each_id:
                raise ValueError("Every For each needs a for_each_id")
            parent = str(item.get("group_id") or "").strip() or None
            if parent is not None and parent not in group_row_ids:
                raise ValueError(f"For each '{for_each_id}' names unknown group '{parent}'")
            cur.execute(
                """INSERT INTO registry_workflow_for_each
                       (workflow_id, for_each_id, group_id, display_name, description,
                        sort_order, over_selector, item_name, index_name, body_jsonb,
                        error_policy_jsonb, boundary_contract_jsonb)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (workflow_id, for_each_id) DO UPDATE SET
                       group_id = EXCLUDED.group_id,
                       display_name = EXCLUDED.display_name,
                       description = EXCLUDED.description,
                       sort_order = EXCLUDED.sort_order,
                       over_selector = EXCLUDED.over_selector,
                       item_name = EXCLUDED.item_name,
                       index_name = EXCLUDED.index_name,
                       body_jsonb = EXCLUDED.body_jsonb,
                       error_policy_jsonb = EXCLUDED.error_policy_jsonb,
                       boundary_contract_jsonb = EXCLUDED.boundary_contract_jsonb
                   RETURNING id""",
                (
                    workflow_id,
                    for_each_id,
                    group_row_ids.get(parent) if parent else None,
                    str(item.get("display_name") or for_each_id).strip(),
                    str(item.get("description") or ""),
                    index,
                    str(item.get("over") or "").strip(),
                    str(item.get("item_name") or "").strip(),
                    str(item.get("index_name") or "").strip() or None,
                    json.dumps(item.get("body") or {}),
                    json.dumps(item.get("error_policy") or {}),
                    _json_or_none(item.get("boundary_contract")),
                ),
            )
            for_each_row_ids[for_each_id] = cur.fetchone()["id"]

        step_ids: list[str] = []
        for index, step in enumerate(definition.steps):
            step_id = str(step.get("step_id") or "").strip()
            if not step_id:
                raise ValueError("Every step needs a step_id")
            step_ids.append(step_id)
            group_id = str(step.get("group_id") or "").strip() or None
            if group_id is not None and group_id not in group_row_ids:
                raise ValueError(f"Step '{step_id}' names unknown group '{group_id}'")
            for_each_id = str(step.get("for_each_id") or "").strip() or None
            if for_each_id is not None and for_each_id not in for_each_row_ids:
                raise ValueError(f"Step '{step_id}' names unknown For each '{for_each_id}'")
            cur.execute(
                """INSERT INTO registry_workflow_steps
                       (owner_workflow_id, group_id, for_each_id, lane_id, step_id, step_kind,
                        agent_id, skill_id, sort_order, optional, disabled, context_inject,
                        override, participant_key, connector_id, connector_install_id,
                        workflow_id, block_key, display_name, input_mapping_jsonb,
                        output_mapping_jsonb, connection_binding_jsonb, error_policy_jsonb,
                        boundary_contract_jsonb)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (owner_workflow_id, step_id) DO UPDATE SET
                       group_id = EXCLUDED.group_id,
                       for_each_id = EXCLUDED.for_each_id,
                       lane_id = EXCLUDED.lane_id,
                       step_kind = EXCLUDED.step_kind,
                       agent_id = EXCLUDED.agent_id,
                       skill_id = EXCLUDED.skill_id,
                       sort_order = EXCLUDED.sort_order,
                       optional = EXCLUDED.optional,
                       disabled = EXCLUDED.disabled,
                       context_inject = EXCLUDED.context_inject,
                       override = EXCLUDED.override,
                       participant_key = EXCLUDED.participant_key,
                       connector_id = EXCLUDED.connector_id,
                       connector_install_id = EXCLUDED.connector_install_id,
                       workflow_id = EXCLUDED.workflow_id,
                       block_key = EXCLUDED.block_key,
                       display_name = EXCLUDED.display_name,
                       input_mapping_jsonb = EXCLUDED.input_mapping_jsonb,
                       output_mapping_jsonb = EXCLUDED.output_mapping_jsonb,
                       connection_binding_jsonb = EXCLUDED.connection_binding_jsonb,
                       error_policy_jsonb = EXCLUDED.error_policy_jsonb,
                       boundary_contract_jsonb = EXCLUDED.boundary_contract_jsonb""",
                (
                    workflow_id,
                    group_row_ids.get(group_id) if group_id else None,
                    for_each_row_ids.get(for_each_id) if for_each_id else None,
                    str(step.get("lane_id") or "").strip() or None,
                    step_id,
                    *_step_row_values(self, step, step_id=step_id, index=index),
                ),
            )
        cur.execute(
            "DELETE FROM registry_workflow_steps"
            " WHERE owner_workflow_id = %s AND NOT (step_id = ANY(%s))",
            (workflow_id, step_ids),
        )
        cur.execute(
            "DELETE FROM registry_workflow_for_each"
            " WHERE workflow_id = %s AND NOT (for_each_id = ANY(%s))",
            (workflow_id, list(for_each_row_ids)),
        )
        cur.execute(
            "DELETE FROM registry_workflow_groups"
            " WHERE workflow_id = %s AND NOT (group_id = ANY(%s))",
            (workflow_id, list(group_row_ids)),
        )

    def delete_workflow(self, workflow_id: str, author: str = "admin") -> None:
        snapshot = self.get_workflow(workflow_id)
        if not snapshot:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        from core.application.management.errors import ServiceNotFoundError
        from core.application.workflows.workflow_trigger_lifecycle import retire_workflow

        try:
            with self._transaction() as cur:
                retire_workflow(
                    cur,
                    store=self,
                    workflow_id=workflow_id,
                    account_id=str(snapshot.get("account_id") or ""),
                    author=author,
                    record_change=lambda cursor, locked_snapshot: self._log_change_cur(
                        cursor, "workflow", workflow_id, "delete", locked_snapshot, author
                    ),
                )
        except ServiceNotFoundError as exc:
            raise KeyError(f"Workflow '{workflow_id}' not found") from exc
        self._trigger_public_contract_projection_sync()

    def toggle_workflow(self, workflow_id: str) -> bool:
        with self._transaction() as cur:
            cur.execute(
                "UPDATE registry_workflows SET enabled = NOT enabled, revision = revision + 1 WHERE workflow_id = %s RETURNING enabled",
                (workflow_id,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Workflow '{workflow_id}' not found")
            enabled = row["enabled"]
        self._trigger_public_contract_projection_sync()
        return enabled

    def clone_workflow(
        self,
        source_workflow_id: str,
        target_workflow_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        account_id: str,
        scope_kind: str,
        author: str = "admin",
        canvas_annotations: list[dict] | None = None,
    ) -> str:
        """Clone workflow metadata, steps and groups into a new workflow_id, disabled.

        A clone into another account or project can miss what the source had (a tool,
        a connection); taking the source's enabled state, it ran with no readiness check.
        Like an import it starts disabled, and its author enables it when it is ready.
        """
        source = self.get_workflow(source_workflow_id)
        if not source:
            raise KeyError(f"Workflow '{source_workflow_id}' not found")
        if self.get_workflow(target_workflow_id):
            raise ValueError(f"Workflow '{target_workflow_id}' already exists")

        groups = [_definition_group_payload(group) for group in source.get("groups") or []]
        for_each = [_definition_for_each_payload(item) for item in source.get("for_each") or []]
        steps = [_definition_step_payload(step) for step in source.get("steps") or []]

        settings = dict(source.get("settings") or {})
        if "canvas_annotations" in settings:
            # Binary references are workflow-owned; the application supplies copied images.
            settings["canvas_annotations"] = (
                canvas_annotations
                if canvas_annotations is not None
                else [
                    {key: value for key, value in note.items() if key != "image"}
                    for note in settings["canvas_annotations"]
                ]
            )
        clone_payload = {
            "workflow_id": target_workflow_id,
            "version": source.get("version", "1.0.0"),
            "display_name": display_name
            or f"{source.get('display_name') or source_workflow_id} (Copy)",
            "description": description
            if description is not None
            else source.get("description", ""),
            "enabled": False,
            "account_id": account_id,
            "scope_kind": scope_kind,
            "trigger": source.get("trigger") or {},
            "settings": settings,
            "tags": list(source.get("tags") or []),
            "steps": steps,
            "groups": groups,
            "for_each": for_each,
        }
        self.save_workflow(clone_payload, author=author, create_only=True)
        return target_workflow_id


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _step_row_values(store, step: dict, *, step_id: str, index: int) -> tuple:
    """Column values of one step, in the order of the step upsert."""
    step_kind = str(step.get("step_kind") or "agent").strip() or "agent"
    raw_agent_id = step.get("agent_id")
    if raw_agent_id is None:
        raw_agent_id = step.get("agent", "")
    raw_skill_id = step.get("skill_id")
    if raw_skill_id is None:
        raw_skill_id = step.get("skill", "")
    step_skill_id = str(raw_skill_id or "").strip() or None
    # An extension's skill is advertised through the same catalog as the
    # host's own, but it lives in extension_registry_projections and this
    # column's foreign key points at registry_skills. Writing it there turns
    # an ordinary authoring request into an empty 500. Nothing is lost by
    # leaving the column empty because the effective skill is derived from
    # the selected agent.
    extension_skill_selected = bool(step_skill_id and not skill_is_host_owned(store, step_skill_id))
    if step_kind != "agent" or extension_skill_selected:
        step_skill_id = None
    return (
        step_kind,
        str(raw_agent_id or "").strip(),
        step_skill_id,
        index,
        bool(step.get("optional", False)),
        step.get("disabled") is True,
        json.dumps(step.get("context_inject")) if step.get("context_inject") else None,
        json.dumps(step.get("override")) if step.get("override") else None,
        str(step.get("participant_key") or "").strip(),
        str(step.get("connector_id") or "").strip() or None,
        str(step.get("connector_install_id") or "").strip(),
        str(step.get("workflow_id") or step.get("child_workflow_id") or "").strip() or None,
        str(step.get("block_key") or "").strip(),
        str(step.get("display_name") or step.get("name") or step_id).strip(),
        json.dumps(step.get("input_mapping_jsonb") or step.get("input_mapping") or {}),
        json.dumps(step.get("output_mapping_jsonb") or step.get("output_mapping") or {}),
        json.dumps(step.get("connection_binding_jsonb") or step.get("connection_binding") or {}),
        json.dumps(step.get("error_policy_jsonb") or step.get("error_policy") or {}),
        _json_or_none(step.get("boundary_contract", step.get("boundary_contract_jsonb"))),
    )


__all__ = [
    "RegistryDBStoreWorkflowsMixin",
    "WorkflowDefinitionRows",
    "load_workflow_definition_rows",
]
