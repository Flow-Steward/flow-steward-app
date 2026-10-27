# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .command_services_common import *  # NOSONAR


class TrashLifecycleServiceExecutionMixin:
    def _collect_incomplete_selected_container_roots(
        self,
        node: Mapping[str, Any],
        *,
        selected_node_ids: set[str],
    ) -> list[str]:
        incomplete: list[str] = []
        entity_type = str(node.get("entity_type") or "").strip()
        node_id = str(node.get("node_id") or "").strip()
        if entity_type in {"workspace", "project"} and node_id in selected_node_ids:
            descendants = self._collect_selectable_descendant_node_ids(node)
            if not descendants.issubset(selected_node_ids):
                incomplete.append(node_id)
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                incomplete.extend(
                    self._collect_incomplete_selected_container_roots(
                        child,
                        selected_node_ids=selected_node_ids,
                    )
                )
        return incomplete

    def _collect_direct_dependency_conflicts(
        self,
        bl: Any,
        node: Mapping[str, Any],
        *,
        action_type: str,
        selected_node_ids: set[str],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        if str(action_type or "").strip() not in {"restore", "hard_delete"}:
            return conflicts
        project_nodes = self._collect_project_root_nodes(node)
        for project_node in project_nodes:
            project_id = str(project_node.get("entity_id") or "").strip()
            if not project_id:
                continue
            project_row = self._get_project_row_including_trashed(bl, project_id)
            account_id = str(project_row.get("account_id") or "").strip()
            root_node_id = str(project_node.get("node_id") or "").strip()
            workflows = [
                child
                for child in (project_node.get("children") or [])
                if self._is_project_workflow_node(child)
            ]
            datasets = [
                child
                for child in (project_node.get("children") or [])
                if isinstance(child, Mapping)
                and str(child.get("entity_type") or "").strip() == "project_dataset"
            ]
            connections = [
                child
                for child in (project_node.get("children") or [])
                if isinstance(child, Mapping)
                and str(child.get("entity_type") or "").strip() == "project_connection"
            ]
            dataset_nodes = {
                str(dataset_node.get("entity_id") or "").strip(): dataset_node
                for dataset_node in datasets
                if str(dataset_node.get("entity_id") or "").strip()
            }
            connection_refs: dict[str, Mapping[str, Any]] = {}
            for connection_node in connections:
                metadata = (
                    connection_node.get("metadata") if isinstance(connection_node, Mapping) else {}
                )
                refs = metadata.get("dependency_refs") if isinstance(metadata, Mapping) else None
                normalized_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
                for ref in normalized_refs:
                    connection_refs[ref] = connection_node
            workflow_connection_dependencies: dict[str, set[str]] = {}
            workflow_dataset_dependencies: dict[str, set[str]] = {}
            for workflow_node in workflows:
                workflow_id = str(workflow_node.get("entity_id") or "").strip()
                if not workflow_id:
                    continue
                workflow_connection_dependencies[workflow_id] = (
                    self._list_required_workflow_connection_refs(
                        bl,
                        workflow_id=workflow_id,
                        account_id=account_id,
                    )
                )
                workflow_dataset_dependencies[workflow_id] = (
                    self._list_required_workflow_dataset_ids(
                        bl,
                        workflow_id=workflow_id,
                    )
                )

            if action_type == "restore":
                for workflow_node in workflows:
                    workflow_node_id = str(workflow_node.get("node_id") or "").strip()
                    if workflow_node_id not in selected_node_ids:
                        continue
                    workflow_id = str(workflow_node.get("entity_id") or "").strip()
                    for dependency_ref in sorted(
                        workflow_connection_dependencies.get(workflow_id) or set()
                    ):
                        connection_node = connection_refs.get(dependency_ref)
                        if connection_node is None:
                            conflicts.append(
                                {
                                    "node_id": workflow_node_id,
                                    "entity_id": workflow_id,
                                    "dependency_kind": "project_connection",
                                    "dependency_ref": dependency_ref,
                                    "message": (
                                        f"Workflow '{workflow_node.get('label') or workflow_id}' requires "
                                        f"project connection '{dependency_ref}', which is not available in this project."
                                    ),
                                }
                            )
                            continue
                        connection_node_id = str(connection_node.get("node_id") or "").strip()
                        if connection_node_id in selected_node_ids:
                            continue
                        if self._connection_node_is_available(connection_node):
                            continue
                        conflicts.append(
                            {
                                "node_id": workflow_node_id,
                                "entity_id": workflow_id,
                                "dependency_kind": "project_connection",
                                "dependency_ref": dependency_ref,
                                "blocking_node_id": connection_node_id,
                                "message": (
                                    f"Workflow '{workflow_node.get('label') or workflow_id}' requires "
                                    f"project connection '{connection_node.get('label') or dependency_ref}'. "
                                    "Select that connection too, or leave the workflow deleted."
                                ),
                            }
                        )
                    for dataset_id in sorted(
                        workflow_dataset_dependencies.get(workflow_id) or set()
                    ):
                        dataset_node = dataset_nodes.get(dataset_id)
                        if dataset_node is None:
                            conflicts.append(
                                {
                                    "node_id": workflow_node_id,
                                    "entity_id": workflow_id,
                                    "dependency_kind": "project_dataset",
                                    "dependency_ref": dataset_id,
                                    "message": (
                                        f"Workflow '{workflow_node.get('label') or workflow_id}' requires "
                                        f"dataset '{dataset_id}', which is not available in this project."
                                    ),
                                }
                            )
                            continue
                        dataset_node_id = str(dataset_node.get("node_id") or "").strip()
                        if dataset_node_id in selected_node_ids or self._dataset_node_is_available(
                            dataset_node
                        ):
                            continue
                        conflicts.append(
                            {
                                "node_id": workflow_node_id,
                                "entity_id": workflow_id,
                                "dependency_kind": "project_dataset",
                                "dependency_ref": dataset_id,
                                "blocking_node_id": dataset_node_id,
                                "message": (
                                    f"Workflow '{workflow_node.get('label') or workflow_id}' requires "
                                    f"dataset '{dataset_node.get('label') or dataset_id}'. "
                                    "Select that dataset too, or leave the workflow deleted."
                                ),
                            }
                        )
            elif action_type == "hard_delete":
                all_descendants_selected = self._collect_selectable_descendant_node_ids(
                    project_node
                ).intersection(selected_node_ids) == self._collect_selectable_descendant_node_ids(
                    project_node
                )
                root_selected = root_node_id in selected_node_ids
                if not (root_selected and all_descendants_selected):
                    active_workflows = [
                        workflow_node
                        for workflow_node in workflows
                        if str(workflow_node.get("status") or "").strip().lower() != "trashed"
                    ]
                    remaining_workflows = [
                        workflow_node
                        for workflow_node in active_workflows
                        if str(workflow_node.get("node_id") or "").strip() not in selected_node_ids
                    ]
                    if active_workflows:
                        if not remaining_workflows:
                            conflicts.append(
                                {
                                    "node_id": root_node_id,
                                    "entity_id": project_id,
                                    "dependency_kind": "project_workflow",
                                    "message": (
                                        f"Project '{project_node.get('label') or project_id}' must retain at least one "
                                        "linked workflow unless the project itself is fully hard-deleted."
                                    ),
                                }
                            )
                        elif not any(
                            bool((workflow_node.get("metadata") or {}).get("is_default"))
                            for workflow_node in remaining_workflows
                            if isinstance(workflow_node.get("metadata"), Mapping)
                        ):
                            conflicts.append(
                                {
                                    "node_id": root_node_id,
                                    "entity_id": project_id,
                                    "dependency_kind": "project_workflow",
                                    "message": (
                                        f"Project '{project_node.get('label') or project_id}' must retain a default "
                                        "workflow unless the project itself is fully hard-deleted."
                                    ),
                                }
                            )
                for connection_node in connections:
                    connection_node_id = str(connection_node.get("node_id") or "").strip()
                    if connection_node_id not in selected_node_ids:
                        continue
                    metadata = (
                        connection_node.get("metadata")
                        if isinstance(connection_node, Mapping)
                        else {}
                    )
                    refs = {
                        str(ref).strip()
                        for ref in ((metadata or {}).get("dependency_refs") or [])
                        if str(ref).strip()
                    }
                    if not refs:
                        refs = {str(connection_node.get("entity_id") or "").strip()}
                    for workflow_node in workflows:
                        workflow_node_id = str(workflow_node.get("node_id") or "").strip()
                        if workflow_node_id in selected_node_ids:
                            continue
                        if str(workflow_node.get("status") or "").strip().lower() == "trashed":
                            continue
                        workflow_id = str(workflow_node.get("entity_id") or "").strip()
                        if refs.isdisjoint(
                            workflow_connection_dependencies.get(workflow_id) or set()
                        ):
                            continue
                        conflicts.append(
                            {
                                "node_id": connection_node_id,
                                "entity_id": str(connection_node.get("entity_id") or "").strip(),
                                "dependency_kind": "project_connection",
                                "dependency_ref": str(
                                    connection_node.get("entity_id") or ""
                                ).strip(),
                                "blocking_node_id": workflow_node_id,
                                "message": (
                                    f"Project connection '{connection_node.get('label') or connection_node.get('entity_id')}' "
                                    f"is still required by workflow '{workflow_node.get('label') or workflow_id}'. "
                                    "Delete the workflow too, or keep the connection."
                                ),
                            }
                        )
                for dataset_node in datasets:
                    dataset_node_id = str(dataset_node.get("node_id") or "").strip()
                    if dataset_node_id not in selected_node_ids:
                        continue
                    dataset_id = str(dataset_node.get("entity_id") or "").strip()
                    for workflow_node in workflows:
                        workflow_node_id = str(workflow_node.get("node_id") or "").strip()
                        if workflow_node_id in selected_node_ids:
                            continue
                        if str(workflow_node.get("status") or "").strip().lower() == "trashed":
                            continue
                        workflow_id = str(workflow_node.get("entity_id") or "").strip()
                        if dataset_id not in (
                            workflow_dataset_dependencies.get(workflow_id) or set()
                        ):
                            continue
                        conflicts.append(
                            {
                                "node_id": dataset_node_id,
                                "entity_id": dataset_id,
                                "dependency_kind": "project_dataset",
                                "dependency_ref": dataset_id,
                                "blocking_node_id": workflow_node_id,
                                "message": (
                                    f"Dataset '{dataset_node.get('label') or dataset_id}' is still required by "
                                    f"workflow '{workflow_node.get('label') or workflow_id}'. "
                                    "Delete the workflow too, or keep the dataset."
                                ),
                            }
                        )
        return conflicts

    def _execute_lifecycle_selection(
        self,
        bl: Any,
        *,
        action_type: str,
        selected_node_ids: set[str],
        tree_node: Mapping[str, Any],
        account_id: str = "",
        actor: str = "system",
    ) -> dict[str, Any]:
        root_kind = str((tree_node.get("metadata") or {}).get("root_kind") or "").strip()
        if root_kind in {"workspace", "project"}:
            return self._execute_container_lifecycle_selection(
                bl,
                action_type=action_type,
                selected_node_ids=selected_node_ids,
                tree_node=tree_node,
                account_id=account_id,
                actor=actor,
            )
        return self._execute_leaf_lifecycle_selection(
            bl,
            action_type=action_type,
            selected_node_ids=selected_node_ids,
            tree_node=tree_node,
            account_id=account_id,
            actor=actor,
        )

    def _execute_container_lifecycle_selection(
        self,
        bl: Any,
        *,
        action_type: str,
        selected_node_ids: set[str],
        tree_node: Mapping[str, Any],
        account_id: str = "",
        actor: str = "system",
    ) -> dict[str, Any]:
        entity_type = str(tree_node.get("entity_type") or "").strip()
        entity_id = str(tree_node.get("entity_id") or "").strip()
        descendant_selectable = self._collect_selectable_descendant_node_ids(tree_node)
        selected_descendants = descendant_selectable.intersection(selected_node_ids)
        root_selected = str(tree_node.get("node_id") or "").strip() in selected_node_ids
        root_selectable = bool(tree_node.get("selectable"))
        root_applied = False

        if action_type == "restore" and selected_descendants and root_selectable:
            if entity_type == "workspace":
                self._restore_workspace_row(bl, entity_id)
                root_applied = True
            elif entity_type == "project":
                if (
                    str((tree_node.get("metadata") or {}).get("root_kind") or "").strip()
                    == "project"
                ):
                    self._restore_project_root(bl, entity_id)
                else:
                    self._restore_entity_direct(bl, "project", entity_id)
                root_applied = True

        for child in tree_node.get("children") or []:
            if isinstance(child, Mapping):
                self._execute_leaf_lifecycle_selection(
                    bl,
                    action_type=action_type,
                    selected_node_ids=selected_node_ids,
                    tree_node=child,
                    account_id=account_id,
                    actor=actor,
                )

        if (
            action_type == "restore"
            and not selected_descendants
            and root_selected
            and root_selectable
        ):
            if entity_type == "workspace":
                self._restore_workspace_row(bl, entity_id)
                root_applied = True
            elif entity_type == "project":
                if (
                    str((tree_node.get("metadata") or {}).get("root_kind") or "").strip()
                    == "project"
                ):
                    self._restore_project_root(bl, entity_id)
                else:
                    self._restore_entity_direct(bl, "project", entity_id)
                root_applied = True

        if action_type == "hard_delete":
            all_descendants_selected = descendant_selectable == selected_descendants
            if root_selected and all_descendants_selected:
                if entity_type == "workspace":
                    self._hard_delete_workspace(bl, entity_id)
                    root_applied = True
                elif entity_type == "project":
                    self._hard_delete_project_direct(bl, entity_id)
                    root_applied = True
        return {"root_applied": root_applied}

    def _execute_leaf_lifecycle_selection(
        self,
        bl: Any,
        *,
        action_type: str,
        selected_node_ids: set[str],
        tree_node: Mapping[str, Any],
        account_id: str = "",
        actor: str = "system",
    ) -> dict[str, Any]:
        entity_type = str(tree_node.get("entity_type") or "").strip()
        if entity_type in {"workspace", "project"}:
            return self._execute_container_lifecycle_selection(
                bl,
                action_type=action_type,
                selected_node_ids=selected_node_ids,
                tree_node=tree_node,
                account_id=account_id,
                actor=actor,
            )
        node_id = str(tree_node.get("node_id") or "").strip()
        root_applied = False
        if node_id and node_id in selected_node_ids:
            self._execute_tree_node_action(
                bl,
                action_type=action_type,
                tree_node=tree_node,
                account_id=account_id,
                actor=actor,
            )
            root_applied = (
                str((tree_node.get("metadata") or {}).get("root_kind") or "").strip()
                == "registry_workflow"
            )
        for child in tree_node.get("children") or []:
            if isinstance(child, Mapping):
                self._execute_leaf_lifecycle_selection(
                    bl,
                    action_type=action_type,
                    selected_node_ids=selected_node_ids,
                    tree_node=child,
                    account_id=account_id,
                    actor=actor,
                )
        return {"root_applied": root_applied}

    def _execute_tree_node_action(
        self,
        bl: Any,
        *,
        action_type: str,
        tree_node: Mapping[str, Any],
        account_id: str = "",
        actor: str = "system",
    ) -> None:
        entity_type = str(tree_node.get("entity_type") or "").strip()
        entity_id = str(tree_node.get("entity_id") or "").strip()
        metadata = dict(tree_node.get("metadata") or {})
        project_id = str(metadata.get("project_id") or "").strip()
        if entity_type == "project_workflow":
            if action_type == "restore":
                self._restore_project_workflow_link(
                    bl, project_id=project_id, workflow_id=entity_id
                )
            else:
                self._hard_delete_project_workflow_link(
                    bl, project_id=project_id, workflow_id=entity_id
                )
            return
        if entity_type == "project_dataset":
            if action_type == "restore":
                self._restore_project_dataset_link(
                    bl,
                    project_id=project_id,
                    dataset_id=entity_id,
                    author=actor,
                )
            else:
                self._hard_delete_project_dataset_link(
                    bl,
                    project_id=project_id,
                    dataset_id=entity_id,
                    author=actor,
                )
            return
        if entity_type == "project_connection":
            if action_type == "restore":
                self._restore_project_connection_link(
                    bl, project_id=project_id, connection_id=entity_id
                )
            else:
                self._hard_delete_project_connection_link(
                    bl, project_id=project_id, connection_id=entity_id
                )
            return
        if action_type == "restore":
            self._restore_entity_direct(bl, entity_type, entity_id)
        else:
            self._hard_delete_entity_direct(
                bl,
                entity_type,
                entity_id,
                account_id=account_id,
            )

    def _collect_selectable_descendant_node_ids(self, node: Mapping[str, Any]) -> set[str]:
        selected: set[str] = set()
        for child in node.get("children") or []:
            if not isinstance(child, Mapping):
                continue
            if bool(child.get("selectable")) and str(child.get("node_id") or "").strip():
                selected.add(str(child.get("node_id") or "").strip())
            selected.update(self._collect_selectable_descendant_node_ids(child))
        return selected

    def _restore_workspace_row(self, bl: Any, workspace_id: str) -> None:
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workspaces
                   SET deleted_at = NULL,
                       deleted_by = NULL,
                       updated_at = now()
                 WHERE id = %s
                   AND deleted_at IS NOT NULL
                """,
                (str(workspace_id),),
            )
            if not cur.rowcount:
                raise ServiceNotFoundError("workspace not found or not trashed")

    def _restore_workspace_row_if_trashed(self, bl: Any, workspace_id: str) -> None:
        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id:
            return
        workspace = self._get_workspace_row(bl, normalized_workspace_id)
        if self._row_lifecycle_status(workspace) != "trashed":
            return
        self._restore_workspace_row(bl, normalized_workspace_id)

    def _restore_project_root(self, bl: Any, project_id: str) -> None:
        project = self._get_project_row_including_trashed(bl, project_id)
        workspace_id = str(project.get("workspace_id") or "").strip()
        if workspace_id:
            self._restore_workspace_row_if_trashed(bl, workspace_id)
        self._restore_entity_direct(bl, "project", project_id)

    def _move_project_to_workspace_direct(
        self,
        bl: Any,
        *,
        project_id: str,
        workspace_id: str,
    ) -> None:
        supports_updated_at = self._has_column(bl, "projects", "updated_at")
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
                    (str(workspace_id), str(project_id)),
                )
            else:
                cur.execute(
                    """
                    UPDATE projects
                       SET workspace_id = %s
                     WHERE id = %s
                    RETURNING id
                    """,
                    (str(workspace_id), str(project_id)),
                )
            if not cur.fetchone():
                raise ServiceNotFoundError(PROJECT_NOT_FOUND_MESSAGE)


__all__ = [name for name in globals() if not name.startswith("__")]
