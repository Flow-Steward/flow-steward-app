"""Workspace operations for the composed trash lifecycle service."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from core.application.management.command_services_common import (
    WORKSPACE_NOT_FOUND_MESSAGE,
    ServiceNotFoundError,
    ServiceValidationError,
    _require_role,
    _service_scope,
)
from core.application.management.scope import ScopeContext


class TrashLifecycleServiceWorkspaceMixin:
    # Dependencies provided by the owning composed lifecycle service.
    _get_workspace_row_including_trashed_and_scoped: Callable[..., Any]
    _list_workspace_projects_visible_in_scope: Callable[..., Any]
    _list_workspace_reassignment_targets: Callable[..., Any]
    _logger: Callable[..., Any]
    _move_project_to_workspace_direct: Callable[..., Any]
    _row_lifecycle_status: Callable[..., Any]
    trash: Callable[..., Any]

    def workspace_delete_plan(
        self,
        workspace_id: str | int,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        resolved = _service_scope(scope)
        _require_role(resolved, "operator")
        bl = self._logger()
        workspace = self._get_workspace_row_including_trashed_and_scoped(
            bl,
            str(workspace_id),
            account_id=resolved.account_id,
            tenant_id=resolved.tenant_id,
            allowed_workspace_ids=resolved.allowed_workspace_ids,
        )
        if not workspace:
            raise ServiceNotFoundError(WORKSPACE_NOT_FOUND_MESSAGE)
        visible_projects, hidden_project_count = self._list_workspace_projects_visible_in_scope(
            bl,
            workspace_id=str(workspace_id),
            scope=resolved,
        )
        target_workspaces = self._list_workspace_reassignment_targets(
            bl,
            source_workspace_id=str(workspace_id),
            account_id=resolved.account_id,
            tenant_id=resolved.tenant_id,
            allowed_workspace_ids=resolved.allowed_workspace_ids,
        )
        datasets = self._list_workspace_delete_plan_datasets(bl, str(workspace_id))
        data_connections = self._list_workspace_delete_plan_data_connections(bl, str(workspace_id))
        warnings: list[str] = []
        validation_errors: list[str] = []
        if self._row_lifecycle_status(workspace) == "trashed":
            validation_errors.append("Workspace is already in trash.")
        if hidden_project_count:
            validation_errors.append(
                "Workspace contains projects outside your project scope. Delete planning must be run from a scope that can see every child project."
            )
        active_project_count = 0
        project_items: list[dict[str, Any]] = []
        for project in visible_projects:
            project_status = self._row_lifecycle_status(project)
            if project_status == "active":
                active_project_count += 1
            project_items.append(
                {
                    "project_id": str(project.get("id") or "").strip(),
                    "label": str(project.get("name") or project.get("id") or "Project").strip(),
                    "status": project_status,
                    "can_reassign": project_status == "active" and bool(target_workspaces),
                    "current_workspace_id": str(project.get("workspace_id") or "").strip(),
                    "current_workspace_label": str(
                        workspace.get("display_name")
                        or workspace.get("workspace_key")
                        or workspace_id
                    ).strip(),
                }
            )
        if active_project_count and not target_workspaces:
            warnings.append(
                "No alternative active workspaces are available. Every active child project will be deleted with this workspace."
            )
        return {
            "status": "planned",
            "action_type": "trash",
            "workspace": {
                "workspace_id": str(workspace.get("id") or workspace_id).strip(),
                "label": str(
                    workspace.get("display_name") or workspace.get("workspace_key") or workspace_id
                ).strip(),
                "status": self._row_lifecycle_status(workspace),
            },
            "projects": project_items,
            "datasets": datasets,
            "data_connections": data_connections,
            "target_workspaces": target_workspaces,
            "warnings": warnings,
            "validation_errors": validation_errors,
            "summary": {
                "active_project_count": active_project_count,
                "trashed_project_count": len(project_items) - active_project_count,
                "dataset_count": len(datasets),
                "data_connection_count": len(data_connections),
            },
        }

    @staticmethod
    def _fetch_plan_rows(cur: Any) -> list[dict[str, Any]]:
        """Return cursor rows as dicts regardless of the configured cursor factory."""
        rows = cur.fetchall() or []
        description = [str(item[0]) for item in (getattr(cur, "description", None) or [])]
        payload: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                payload.append(dict(row))
            elif isinstance(row, (tuple, list)) and description and len(description) == len(row):
                payload.append({description[idx]: row[idx] for idx in range(len(row))})
        return payload

    # DELETE-PLAN ENUMERATION — do not make these fail open.
    #
    # `_trash_workspace_with_plan_transactional` uses the rows returned here as
    # its only safety interlock: every live Data Connection must be given an
    # explicit block/reassign/delete action before the workspace can be deleted.
    # An empty list is therefore not a neutral "nothing to report" — it silently
    # disables that interlock and lets workspace deletion orphan live rows.
    #
    # Both readers must run on `bl.conn`, because the plan is re-read inside the
    # `transaction_connection(bl.conn)` block that performs the delete; reading
    # through a store that owns a separate connection would not observe the
    # in-transaction state. If the query itself fails, the error propagates: a
    # loud failure is the correct outcome for a safety interlock.
    def _list_workspace_delete_plan_datasets(
        self, bl: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.dataset_id,
                       d.display_name,
                       d.deleted_at
                  FROM registry_datasets d
                  LEFT JOIN workspace_data_connections wdc
                         ON wdc.connection_id::text = d.metadata_jsonb->>'connection_id'
                 WHERE COALESCE(
                           d.workspace_id::text,
                           d.metadata_jsonb->>'workspace_id',
                           wdc.workspace_id::text,
                           (SELECT p.workspace_id::text
                              FROM project_datasets pd
                              JOIN projects p ON p.id = pd.project_id
                             WHERE pd.dataset_id = d.dataset_id
                             LIMIT 1)
                       ) = %s
                 ORDER BY d.display_name, d.dataset_id
                """,
                (str(workspace_id),),
            )
            rows = self._fetch_plan_rows(cur)
        return [
            {
                "dataset_id": str(row.get("dataset_id") or "").strip(),
                "label": str(row.get("display_name") or row.get("dataset_id") or "Dataset").strip(),
                "status": self._row_lifecycle_status(row),
            }
            for row in rows
            if str(row.get("dataset_id") or "").strip()
        ]

    def _list_workspace_delete_plan_data_connections(
        self, bl: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.connection_id,
                       c.display_name,
                       c.deleted_at,
                       COALESCE(ds.linked_dataset_count, 0) AS linked_dataset_count
                  FROM workspace_data_connections c
                  LEFT JOIN (
                        SELECT metadata_jsonb->>'connection_id' AS connection_id,
                               COUNT(*) AS linked_dataset_count
                          FROM registry_datasets
                         WHERE metadata_jsonb ? 'connection_id'
                           AND deleted_at IS NULL
                         GROUP BY metadata_jsonb->>'connection_id'
                  ) ds ON ds.connection_id = c.connection_id::text
                 WHERE c.workspace_id::text = %s
                 ORDER BY c.updated_at DESC, c.display_name, c.connection_id
                """,
                (str(workspace_id),),
            )
            rows = self._fetch_plan_rows(cur)
        return [
            {
                "connection_id": str(row.get("connection_id") or "").strip(),
                "label": str(
                    row.get("display_name") or row.get("connection_id") or "Data connection"
                ).strip(),
                "status": self._row_lifecycle_status(row),
                "linked_dataset_count": int(row.get("linked_dataset_count") or 0),
                "allowed_actions": (
                    ["block", "reassign", "delete"]
                    if self._row_lifecycle_status(row) == "active"
                    else []
                ),
            }
            for row in rows
            if str(row.get("connection_id") or "").strip()
        ]

    def trash_workspace_with_plan(
        self,
        workspace_id: str | int,
        *,
        deleted_by: str = "system",
        project_actions: Sequence[Mapping[str, Any]] | None = None,
        connection_actions: Sequence[Mapping[str, Any]] | None = None,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        resolved = _service_scope(scope)
        _require_role(resolved, "operator")
        bl = self._logger()
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(bl.conn):
            return self._trash_workspace_with_plan_transactional(
                bl,
                str(workspace_id),
                deleted_by=deleted_by,
                project_actions=project_actions,
                connection_actions=connection_actions,
                scope=resolved,
            )

    def _trash_workspace_with_plan_transactional(
        self,
        bl: Any,
        workspace_id: str,
        *,
        deleted_by: str,
        project_actions: Sequence[Mapping[str, Any]] | None,
        connection_actions: Sequence[Mapping[str, Any]] | None,
        scope: ScopeContext,
    ) -> dict[str, Any]:
        plan = self.workspace_delete_plan(str(workspace_id), scope=scope)
        validation_errors = list(plan.get("validation_errors") or [])
        if validation_errors:
            raise ServiceValidationError("; ".join(validation_errors))

        project_rows = [
            dict(item) for item in (plan.get("projects") or []) if isinstance(item, Mapping)
        ]
        active_project_ids = {
            str(item.get("project_id") or "").strip()
            for item in project_rows
            if str(item.get("status") or "").strip().lower() == "active"
            and str(item.get("project_id") or "").strip()
        }
        target_workspace_ids = {
            str(item.get("workspace_id") or "").strip()
            for item in (plan.get("target_workspaces") or [])
            if isinstance(item, Mapping) and str(item.get("workspace_id") or "").strip()
        }
        connection_rows = {
            str(item.get("connection_id") or "").strip(): dict(item)
            for item in (plan.get("data_connections") or [])
            if isinstance(item, Mapping)
            and str(item.get("status") or "").strip().lower() == "active"
            and str(item.get("connection_id") or "").strip()
        }
        normalized_connection_actions: dict[str, dict[str, str]] = {}
        for raw_action in connection_actions or []:
            if not isinstance(raw_action, Mapping):
                continue
            connection_id = str(raw_action.get("connection_id") or "").strip()
            if not connection_id:
                continue
            if connection_id in normalized_connection_actions:
                raise ServiceValidationError(
                    f"Duplicate workspace delete action for Data Connection '{connection_id}'."
                )
            action = str(raw_action.get("action") or "").strip().lower()
            if action not in {"block", "reassign", "delete"}:
                raise ServiceValidationError(
                    f"Unsupported workspace delete action '{action}' for Data Connection '{connection_id}'."
                )
            normalized_connection_actions[connection_id] = {
                "action": action,
                "target_workspace_id": str(raw_action.get("target_workspace_id") or "").strip(),
            }
        if set(normalized_connection_actions) != set(connection_rows):
            raise ServiceValidationError(
                "Workspace delete requires an explicit action for every live Data Connection. Refresh the plan and choose block, reassign, or delete for each connection."
            )
        for connection_id, action_payload in normalized_connection_actions.items():
            action = action_payload["action"]
            if action == "block":
                raise ServiceValidationError(
                    f"Workspace delete is blocked by Data Connection '{connection_id}'."
                )
            linked_dataset_count = int(
                connection_rows[connection_id].get("linked_dataset_count") or 0
            )
            if linked_dataset_count:
                noun = "dataset" if linked_dataset_count == 1 else "datasets"
                raise ServiceValidationError(
                    f"Data Connection '{connection_id}' has {linked_dataset_count} linked {noun}; move or delete those datasets before {action}."
                )
            if action == "reassign":
                target_workspace_id = action_payload["target_workspace_id"]
                if not target_workspace_id:
                    raise ServiceValidationError(
                        f"Data Connection '{connection_id}' must choose a target workspace before reassignment."
                    )
                if target_workspace_id not in target_workspace_ids:
                    raise ServiceValidationError(
                        f"Target workspace '{target_workspace_id}' is not available for reassignment."
                    )
        normalized_actions: dict[str, dict[str, str]] = {}
        for raw_action in project_actions or []:
            if not isinstance(raw_action, Mapping):
                continue
            project_id = str(raw_action.get("project_id") or "").strip()
            if not project_id:
                continue
            if project_id in normalized_actions:
                raise ServiceValidationError(
                    f"Duplicate workspace delete action for project '{project_id}'."
                )
            action = str(raw_action.get("action") or "").strip().lower()
            if action not in {"trash", "reassign"}:
                raise ServiceValidationError(
                    f"Unsupported workspace delete action '{action}' for project '{project_id}'."
                )
            target_workspace_id = str(raw_action.get("target_workspace_id") or "").strip()
            normalized_actions[project_id] = {
                "action": action,
                "target_workspace_id": target_workspace_id,
            }
        stale_project_ids = sorted(set(normalized_actions).difference(active_project_ids))
        if stale_project_ids:
            raise ServiceValidationError(
                "Workspace delete selection is stale. Refresh the plan and try again."
            )

        reassigned_project_ids: list[str] = []
        for project_id in sorted(active_project_ids):
            action_payload = normalized_actions.get(project_id) or {
                "action": "trash",
                "target_workspace_id": "",
            }
            if action_payload["action"] != "reassign":
                continue
            target_workspace_id = str(action_payload.get("target_workspace_id") or "").strip()
            if not target_workspace_id:
                raise ServiceValidationError(
                    f"Project '{project_id}' must choose a target workspace before reassignment."
                )
            if target_workspace_id not in target_workspace_ids:
                raise ServiceValidationError(
                    f"Target workspace '{target_workspace_id}' is not available for reassignment."
                )
            self._move_project_to_workspace_direct(
                bl,
                project_id=project_id,
                workspace_id=target_workspace_id,
            )
            reassigned_project_ids.append(project_id)

        reassigned_connection_ids: list[str] = []
        deleted_connection_ids: list[str] = []
        for connection_id in sorted(connection_rows):
            action_payload = normalized_connection_actions[connection_id]
            if action_payload["action"] == "reassign":
                self._move_workspace_data_connection_direct(
                    bl,
                    connection_id=connection_id,
                    workspace_id=action_payload["target_workspace_id"],
                )
                reassigned_connection_ids.append(connection_id)
            else:
                self._soft_delete_workspace_data_connection_direct(
                    bl,
                    connection_id=connection_id,
                    deleted_by=deleted_by,
                )
                deleted_connection_ids.append(connection_id)

        payload = self.trash(
            "workspace",
            str(workspace_id),
            deleted_by=deleted_by,
            scope=scope,
        )
        trashed_project_ids = sorted(active_project_ids.difference(reassigned_project_ids))
        return {
            "status": payload.status,
            "entity_type": payload.entity_type,
            "entity_id": payload.entity_id,
            "reassigned_project_ids": sorted(reassigned_project_ids),
            "trashed_project_ids": trashed_project_ids,
            "reassigned_connection_ids": sorted(reassigned_connection_ids),
            "deleted_connection_ids": sorted(deleted_connection_ids),
        }

    @staticmethod
    def _move_workspace_data_connection_direct(
        bl: Any,
        *,
        connection_id: str,
        workspace_id: str,
    ) -> None:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workspace_data_connections
                   SET workspace_id = %s,
                       updated_at = now()
                 WHERE connection_id = %s
                   AND deleted_at IS NULL
                RETURNING connection_id
                """,
                (workspace_id, connection_id),
            )
            if not cur.fetchone():
                raise ServiceValidationError(
                    f"Data Connection '{connection_id}' changed while the delete plan was executing."
                )

    @staticmethod
    def _soft_delete_workspace_data_connection_direct(
        bl: Any,
        *,
        connection_id: str,
        deleted_by: str,
    ) -> None:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workspace_data_connections
                   SET deleted_at = now(),
                       updated_at = now(),
                       metadata_jsonb = COALESCE(metadata_jsonb, '{}'::jsonb)
                           || jsonb_build_object('deleted_by', %s::text)
                 WHERE connection_id = %s
                   AND deleted_at IS NULL
                RETURNING connection_id
                """,
                (deleted_by, connection_id),
            )
            if not cur.fetchone():
                raise ServiceValidationError(
                    f"Data Connection '{connection_id}' changed while the delete plan was executing."
                )
