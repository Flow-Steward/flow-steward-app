# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

import hashlib
import json

from core.application.workflows.workflow_hook_contract import is_hook_storage

from .command_services_common import *  # NOSONAR

_MESSAGE_PROJECT_NOT_FOUND = "project not found"
_MESSAGE_MCP_CONNECTOR_NOT_FOUND = "mcp_connector not found"


class TrashLifecycleServiceRootsMixin:
    def _lifecycle_plan_version(
        self,
        bl: Any,
        root: Mapping[str, Any],
        *,
        action_type: str,
    ) -> str:
        workflow_dependencies: list[dict[str, Any]] = []
        for project_node in self._collect_project_root_nodes(root):
            project_id = str(project_node.get("entity_id") or "").strip()
            project = self._get_project_row_including_trashed(bl, project_id)
            account_id = str(project.get("account_id") or "").strip()
            for child in project_node.get("children") or []:
                if not self._is_project_workflow_node(child):
                    continue
                workflow_id = str(child.get("entity_id") or "").strip()
                workflow_dependencies.append(
                    {
                        "project_id": project_id,
                        "workflow_id": workflow_id,
                        "connections": sorted(
                            self._list_required_workflow_connection_refs(
                                bl,
                                workflow_id=workflow_id,
                                account_id=account_id,
                            )
                        ),
                        "datasets": sorted(
                            self._list_required_workflow_dataset_ids(
                                bl,
                                workflow_id=workflow_id,
                            )
                        ),
                    }
                )
        canonical = json.dumps(
            {
                "action_type": action_type,
                "tree": root,
                "workflow_dependencies": workflow_dependencies,
            },
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _require_lifecycle_root_in_scope(
        self,
        bl: Any,
        *,
        entity_type: str,
        entity_id: str,
        scope: ScopeContext,
    ) -> None:
        if entity_type == "project":
            project = self._get_project_row_including_trashed(bl, entity_id)
            require_project_access(scope, project)
            return
        if entity_type == "workspace":
            workspace = self._get_workspace_row_including_trashed_and_scoped(
                bl,
                entity_id,
                account_id=scope.account_id,
                tenant_id=scope.tenant_id,
                allowed_workspace_ids=scope.allowed_workspace_ids,
            )
            if not workspace:
                raise ServiceNotFoundError(WORKSPACE_NOT_FOUND_MESSAGE)
            return
        if entity_type == "mcp_connector":
            project_id = str(scope.project_id or "").strip()
            tenant_id = str(scope.tenant_id or "").strip()
            if not project_id or not tenant_id:
                raise ServiceNotFoundError(_MESSAGE_MCP_CONNECTOR_NOT_FOUND)
            if scope.allowed_project_ids and project_id not in scope.allowed_project_ids:
                raise ServiceNotFoundError(_MESSAGE_MCP_CONNECTOR_NOT_FOUND)
            with bl.conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM mcp_connectors
                        WHERE connector_id = %s AND account_id = %s
                          AND project_id = %s AND tenant_id = %s""",
                    (entity_id, str(scope.account_id), project_id, tenant_id),
                )
                if not cur.fetchone():
                    raise ServiceNotFoundError(_MESSAGE_MCP_CONNECTOR_NOT_FOUND)
            return
        if entity_type == "registry_workflow":
            with bl.conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM registry_workflows
                        WHERE workflow_id = %s
                          AND account_id = %s""",
                    (entity_id, str(scope.account_id or "")),
                )
                if not cur.fetchone():
                    raise ServiceNotFoundError("registry_workflow not found")
            return
        raise ServiceValidationError(
            "Lifecycle planning is currently available for workspaces, projects, MCP connectors, and registry workflows."
        )

    def _build_workspace_plan_root(
        self,
        bl: Any,
        *,
        workspace_id: str,
        action_type: str,
        scope: ScopeContext,
    ) -> dict[str, Any]:
        workspace = self._get_workspace_row(bl, workspace_id)
        projects = self._list_workspace_projects_including_trashed(bl, workspace_id)
        visible_projects: list[dict[str, Any]] = []
        hidden_project_count = 0
        for project in projects:
            project_id = str(project.get("id") or "").strip()
            if not project_id:
                continue
            try:
                require_project_access(scope, project)
            except ServiceForbiddenError:
                hidden_project_count += 1
                continue
            visible_projects.append(project)
        children = [
            self._build_project_plan_root(
                bl,
                project_id=str(project.get("id") or ""),
                action_type=action_type,
                include_root_metadata=False,
            )
            for project in visible_projects
            if str(project.get("id") or "").strip()
        ]
        root_selectable = hidden_project_count == 0
        return {
            "node_id": f"workspace:{workspace_id}",
            "entity_type": "workspace",
            "entity_id": workspace_id,
            "label": str(
                workspace.get("display_name") or workspace.get("workspace_key") or workspace_id
            ),
            "selectable": root_selectable,
            "selected": root_selectable,
            "status": self._row_lifecycle_status(workspace),
            "children": children,
            "metadata": {
                "root_kind": "workspace",
                "scope_filtered": hidden_project_count > 0,
                "hidden_project_count": hidden_project_count,
            },
        }

    def _build_project_plan_root(
        self,
        bl: Any,
        *,
        project_id: str,
        action_type: str,
        include_root_metadata: bool = True,
    ) -> dict[str, Any]:
        _ = action_type
        project = self._get_project_row_including_trashed(bl, project_id)
        dataset_catalog = self._require_project_dependency_projection(bl)
        workflows = self._list_project_workflow_nodes(bl, project_id)
        datasets = self._list_project_dataset_nodes(
            bl,
            project_id,
            dataset_catalog=dataset_catalog,
            account_id=str(project.get("account_id") or "").strip(),
        )
        connections = self._list_project_connection_nodes(bl, project_id)
        mcp_connectors = self._list_project_mcp_connector_nodes(
            bl,
            project_id=project_id,
            account_id=str(project.get("account_id") or "").strip(),
        )
        metadata: dict[str, Any] = {}
        if include_root_metadata:
            workspace_id = str(project.get("workspace_id") or "").strip()
            workspace_label = workspace_id
            parent_workspace_trashed = False
            if workspace_id:
                try:
                    workspace = self._get_workspace_row(bl, workspace_id)
                except ServiceNotFoundError:
                    workspace = {}
                workspace_label = str(
                    workspace.get("display_name") or workspace.get("workspace_key") or workspace_id
                ).strip()
                parent_workspace_trashed = (
                    self._row_lifecycle_status(workspace) == "trashed" if workspace else False
                )
            metadata = {
                "root_kind": "project",
                "parent_workspace_id": workspace_id,
                "parent_workspace_label": workspace_label,
                "parent_workspace_trashed": parent_workspace_trashed,
            }
        return {
            "node_id": f"project:{project_id}",
            "entity_type": "project",
            "entity_id": project_id,
            "label": str(project.get("name") or project_id),
            "selectable": True,
            "selected": True,
            "status": self._row_lifecycle_status(project),
            "children": workflows + datasets + connections + mcp_connectors,
            "metadata": metadata,
        }

    def _require_project_dependency_projection(self, bl: Any) -> Any:
        dataset_catalog = self._project_dataset_catalog() if self._project_dataset_catalog else bl
        missing = [
            name
            for name, owner in (
                ("get_project_workflows", bl),
                ("list_project_dataset_catalog", dataset_catalog),
                ("get_dataset", dataset_catalog),
                ("list_project_connections", bl),
            )
            if not callable(getattr(owner, name, None))
        ]
        if missing:
            raise ServiceUnavailableError(
                "project lifecycle dependency projection is unavailable: " + ", ".join(missing)
            )
        return dataset_catalog

    def _get_workspace_row(self, bl: Any, workspace_id: str) -> dict[str, Any]:
        getter = getattr(bl, "get_workspace", None)
        if callable(getter):
            row = getter(str(workspace_id), include_archived=True)
            if isinstance(row, dict):
                return dict(row)
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_key, display_name, status, deleted_at
                FROM workspaces
                WHERE id = %s
                LIMIT 1
                """,
                (str(workspace_id),),
            )
            row = cur.fetchone()
        if not row:
            raise ServiceNotFoundError(WORKSPACE_NOT_FOUND_MESSAGE)
        if isinstance(row, dict):
            return dict(row)
        return {
            "id": row[0],
            "workspace_key": row[1],
            "display_name": row[2],
            "status": row[3],
            "deleted_at": row[4],
        }

    def _get_workspace_row_including_trashed_and_scoped(
        self,
        bl: Any,
        workspace_id: str,
        *,
        account_id: str,
        tenant_id: str | None,
        allowed_workspace_ids: frozenset[str],
    ) -> dict[str, Any] | None:
        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id:
            return None
        if allowed_workspace_ids and normalized_workspace_id not in allowed_workspace_ids:
            raise ServiceForbiddenError("Workspace is outside allowed scope")
        normalized_account_id = str(account_id or "").strip()
        normalized_tenant_id = str(tenant_id or "").strip()
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_key, display_name, status, deleted_at, account_id, tenant_id
                FROM workspaces
                WHERE id = %s
                  AND account_id = %s
                  AND COALESCE(tenant_id::text, '') = %s
                LIMIT 1
                """,
                (normalized_workspace_id, normalized_account_id, normalized_tenant_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        if isinstance(row, dict):
            return dict(row)
        return {
            "id": row[0],
            "workspace_key": row[1],
            "display_name": row[2],
            "status": row[3],
            "deleted_at": row[4],
            "account_id": row[5],
            "tenant_id": row[6],
        }

    def _list_workspace_projects_including_trashed(
        self, bl: Any, workspace_id: str
    ) -> list[dict[str, Any]]:
        connection = getattr(bl, "conn", None)
        if not callable(getattr(connection, "cursor", None)):
            return []
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, deleted_at, workspace_id, account_id
                FROM projects
                WHERE workspace_id = %s
                ORDER BY name, id
                """,
                (str(workspace_id),),
            )
            rows = cur.fetchall() or []
        return [
            dict(row)
            if isinstance(row, dict)
            else {
                "id": row[0],
                "name": row[1],
                "deleted_at": row[2],
                "workspace_id": row[3],
                "account_id": row[4],
            }
            for row in rows
        ]

    def _list_workspace_projects_visible_in_scope(
        self,
        bl: Any,
        *,
        workspace_id: str,
        scope: ScopeContext,
    ) -> tuple[list[dict[str, Any]], int]:
        projects = self._list_workspace_projects_including_trashed(bl, workspace_id)
        visible_projects: list[dict[str, Any]] = []
        hidden_project_count = 0
        for project in projects:
            project_id = str(project.get("id") or "").strip()
            if not project_id:
                continue
            try:
                require_project_access(scope, project)
            except ServiceForbiddenError:
                hidden_project_count += 1
                continue
            visible_projects.append(project)
        return visible_projects, hidden_project_count

    def _list_workspace_reassignment_targets(
        self,
        bl: Any,
        *,
        source_workspace_id: str,
        account_id: str,
        tenant_id: str | None,
        allowed_workspace_ids: frozenset[str],
    ) -> list[dict[str, Any]]:
        normalized_account_id = str(account_id or "").strip()
        normalized_tenant_id = str(tenant_id or "").strip()
        normalized_source_id = str(source_workspace_id or "").strip()
        with bl.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_key, display_name, status, deleted_at
                FROM workspaces
                WHERE account_id = %s
                  AND COALESCE(tenant_id::text, '') = %s
                  AND id <> %s
                  AND deleted_at IS NULL
                  AND COALESCE(status, 'active') <> 'archived'
                ORDER BY display_name, workspace_key, id
                """,
                (normalized_account_id, normalized_tenant_id, normalized_source_id),
            )
            rows = cur.fetchall() or []
        targets: list[dict[str, Any]] = []
        for row in rows:
            item = (
                dict(row)
                if isinstance(row, dict)
                else {
                    "id": row[0],
                    "workspace_key": row[1],
                    "display_name": row[2],
                    "status": row[3],
                    "deleted_at": row[4],
                }
            )
            workspace_id = str(item.get("id") or "").strip()
            if not workspace_id:
                continue
            if allowed_workspace_ids and workspace_id not in allowed_workspace_ids:
                continue
            targets.append(
                {
                    "workspace_id": workspace_id,
                    "label": str(
                        item.get("display_name") or item.get("workspace_key") or workspace_id
                    ).strip(),
                }
            )
        return targets

    def _get_project_row_including_trashed(self, bl: Any, project_id: str) -> dict[str, Any]:
        if callable(getattr(bl, "get_project", None)):
            try:
                row = bl.get_project(str(project_id), include_trashed=True)
            except Exception as exc:
                if _is_invalid_uuid_lookup_error(exc):
                    raise ServiceNotFoundError(_MESSAGE_PROJECT_NOT_FOUND) from exc
                raise
            if isinstance(row, dict):
                return dict(row)
        with bl.conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT id, name, workspace_id, account_id, deleted_at
                    FROM projects
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (str(project_id),),
                )
            except Exception as exc:
                if _is_invalid_uuid_lookup_error(exc):
                    raise ServiceNotFoundError(_MESSAGE_PROJECT_NOT_FOUND) from exc
                raise
            row = cur.fetchone()
        if not row:
            raise ServiceNotFoundError(_MESSAGE_PROJECT_NOT_FOUND)
        if isinstance(row, dict):
            return dict(row)
        return {
            "id": row[0],
            "name": row[1],
            "workspace_id": row[2],
            "account_id": row[3],
            "deleted_at": row[4],
        }

    def _list_project_workflow_nodes(self, bl: Any, project_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        lister = getattr(bl, "list_project_workflows", None)
        if callable(lister):
            raw = lister(project_id=str(project_id), enabled_only=False)
            rows = [dict(item) for item in (raw or []) if isinstance(item, dict)]
        else:
            getter = getattr(bl, "get_project_workflows", None)
            if callable(getter):
                raw = getter(str(project_id), enabled_only=False)
                rows = [dict(item) for item in (raw or []) if isinstance(item, dict)]
        visible_rows = [
            row
            for row in rows
            if row
            and ("visible_in_catalog" not in row or bool(row.get("visible_in_catalog")))
            # A hook's storage workflow is a place inside another workflow.
            and not is_hook_storage(row)
        ]
        return [
            {
                "node_id": f"project_workflow:{project_id}:{str(row.get('workflow_id') or '').strip()}",
                "entity_type": "project_workflow",
                "entity_id": str(row.get("workflow_id") or "").strip(),
                "label": str(
                    row.get("display_name")
                    or row.get("workflow_display_name")
                    or row.get("workflow_id")
                    or "Workflow"
                ).strip(),
                "selectable": True,
                "selected": bool(row.get("enabled", True)) and row.get("deleted_at") is None,
                "status": "trashed" if row.get("deleted_at") else "active",
                "children": [],
                "metadata": {
                    "project_id": project_id,
                    "is_default": bool(row.get("is_default")),
                },
            }
            for row in visible_rows
            if str(row.get("workflow_id") or "").strip()
        ]

    def _list_project_dataset_nodes(
        self,
        bl: Any,
        project_id: str,
        *,
        dataset_catalog: Any,
        account_id: str,
    ) -> list[dict[str, Any]]:
        payload = dict(
            dataset_catalog.list_project_dataset_catalog(
                str(project_id),
                include_trashed=True,
                account_id=account_id,
            )
            or {}
        )
        available = [
            dict(item) for item in payload.get("available_datasets", []) if isinstance(item, dict)
        ]
        explicit_ids = {
            str(item).strip()
            for item in payload.get("explicit_dataset_ids", [])
            if str(item).strip()
        }
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for row in available:
            dataset_id = str(row.get("dataset_id") or "").strip()
            if not dataset_id or dataset_id not in explicit_ids:
                continue
            if "visible_in_catalog" in row and not bool(row.get("visible_in_catalog")):
                continue
            nodes_by_id[dataset_id] = {
                "node_id": f"project_dataset:{project_id}:{dataset_id}",
                "entity_type": "project_dataset",
                "entity_id": dataset_id,
                "label": str(row.get("display_name") or row.get("dataset_id") or "Dataset").strip(),
                "selectable": True,
                "selected": True,
                "status": "active",
                "children": [],
                "metadata": {"project_id": project_id},
            }
        for row in self._list_deleted_project_dataset_snapshots(bl, project_id):
            dataset_id = str(row.get("dataset_id") or "").strip()
            if not dataset_id or dataset_id in nodes_by_id:
                continue
            if "visible_in_catalog" in row and not bool(row.get("visible_in_catalog")):
                continue
            nodes_by_id[dataset_id] = {
                "node_id": f"project_dataset:{project_id}:{dataset_id}",
                "entity_type": "project_dataset",
                "entity_id": dataset_id,
                "label": str(row.get("display_name") or row.get("dataset_id") or "Dataset").strip(),
                "selectable": True,
                "selected": False,
                "status": "trashed",
                "children": [],
                "metadata": {"project_id": project_id},
            }
        return list(nodes_by_id.values())

    def _list_project_connection_nodes(
        self,
        bl: Any,
        project_id: str,
    ) -> list[dict[str, Any]]:
        lister = getattr(bl, "list_project_connections", None)
        if not callable(lister):
            return []
        rows = [
            dict(item)
            for item in (lister(str(project_id), enabled_only=False) or [])
            if isinstance(item, dict)
        ]
        nodes: list[dict[str, Any]] = []
        for row in rows:
            connection_id = str(row.get("connection_id") or "").strip()
            if not connection_id:
                continue
            dependency_refs = [
                ref
                for ref in dict.fromkeys(
                    [
                        connection_id,
                        str(row.get("extension_install_id") or "").strip(),
                        str(row.get("extension_id") or "").strip(),
                    ]
                )
                if ref
            ]
            lifecycle_deleted = self._is_lifecycle_deleted_project_connection(row)
            nodes.append(
                {
                    "node_id": f"project_connection:{project_id}:{connection_id}",
                    "entity_type": "project_connection",
                    "entity_id": connection_id,
                    "label": str(
                        row.get("display_name") or row.get("connection_id") or connection_id
                    ),
                    "selectable": True,
                    "selected": not lifecycle_deleted,
                    "status": "trashed" if lifecycle_deleted else "active",
                    "children": [],
                    "metadata": {
                        "project_id": project_id,
                        "dependency_refs": dependency_refs,
                        "enabled": bool(row.get("enabled", True)),
                        "lifecycle_deleted": lifecycle_deleted,
                    },
                }
            )
        return nodes

    def _list_project_mcp_connector_nodes(
        self,
        bl: Any,
        *,
        project_id: str,
        account_id: str,
    ) -> list[dict[str, Any]]:
        """Project lifecycle inventory for built-in MCP connectors and projections.

        These nodes are informational and mandatory: the project root owns their
        atomic purge, so the UI cannot independently delete a projection before
        the enclosing project transaction commits.
        """
        with bl.conn.cursor() as cur:
            cur.execute(
                """SELECT connector_id, display_name, deleted_at
                     FROM mcp_connectors
                    WHERE project_id = %s AND account_id = %s
                    ORDER BY connector_id""",
                (project_id, account_id),
            )
            connector_rows = cur.fetchall() or []
            cur.execute(
                """SELECT tool_id, display_name, implementation, deleted_at
                     FROM registry_tools
                    WHERE project_id = %s AND account_id = %s
                    ORDER BY tool_id""",
                (project_id, account_id),
            )
            tool_rows = cur.fetchall() or []

        tools_by_connector: dict[str, list[dict[str, Any]]] = {}
        for raw in tool_rows:
            row = (
                dict(raw)
                if isinstance(raw, Mapping)
                else {
                    "tool_id": raw[0],
                    "display_name": raw[1],
                    "implementation": raw[2],
                    "deleted_at": raw[3],
                }
            )
            implementation = row.get("implementation") or {}
            if isinstance(implementation, str):
                try:
                    implementation = json.loads(implementation)
                except Exception:
                    implementation = {}
            if not isinstance(implementation, Mapping):
                continue
            if str(implementation.get("type") or implementation.get("kind") or "").strip() != "mcp":
                continue
            if str(implementation.get("owner_project_id") or project_id).strip() != project_id:
                continue
            connector_id = str(implementation.get("connector_id") or "").strip()
            tools_by_connector.setdefault(connector_id, []).append(row)

        nodes: list[dict[str, Any]] = []
        known_connector_ids: set[str] = set()
        for raw in connector_rows:
            row = (
                dict(raw)
                if isinstance(raw, Mapping)
                else {
                    "connector_id": raw[0],
                    "display_name": raw[1],
                    "deleted_at": raw[2],
                }
            )
            connector_id = str(row.get("connector_id") or "").strip()
            if not connector_id:
                continue
            known_connector_ids.add(connector_id)
            children = self._project_mcp_tool_plan_nodes(
                project_id,
                tools_by_connector.get(connector_id, []),
            )
            nodes.append(
                {
                    "node_id": f"project_mcp_connector:{project_id}:{connector_id}",
                    "entity_type": "project_mcp_connector",
                    "entity_id": connector_id,
                    "label": str(row.get("display_name") or connector_id),
                    "selectable": False,
                    "selected": False,
                    "mandatory": True,
                    "status": "trashed" if row.get("deleted_at") else "active",
                    "children": children,
                    "metadata": {"project_id": project_id},
                }
            )
        orphan_tools = [
            row
            for connector_id, rows in tools_by_connector.items()
            if connector_id not in known_connector_ids
            for row in rows
        ]
        if orphan_tools:
            nodes.append(
                {
                    "node_id": f"project_mcp_connector:{project_id}:orphaned",
                    "entity_type": "project_mcp_connector",
                    "entity_id": "",
                    "label": "Orphaned MCP projections",
                    "selectable": False,
                    "selected": False,
                    "mandatory": True,
                    "status": "conflict",
                    "children": self._project_mcp_tool_plan_nodes(project_id, orphan_tools),
                    "metadata": {"project_id": project_id},
                }
            )
        return nodes

    @staticmethod
    def _project_mcp_tool_plan_nodes(
        project_id: str,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "node_id": f"project_mcp_tool:{project_id}:{str(row.get('tool_id') or '').strip()}",
                "entity_type": "project_mcp_tool",
                "entity_id": str(row.get("tool_id") or "").strip(),
                "label": str(row.get("display_name") or row.get("tool_id") or "MCP tool"),
                "selectable": False,
                "selected": False,
                "mandatory": True,
                "status": "trashed" if row.get("deleted_at") else "active",
                "children": [],
                "metadata": {"project_id": project_id},
            }
            for row in rows
            if str(row.get("tool_id") or "").strip()
        ]

    def _build_registry_workflow_plan_root(
        self,
        bl: Any,
        *,
        workflow_id: str,
        account_id: str,
    ) -> dict[str, Any]:
        with bl.conn.cursor() as cur:
            cur.execute(
                """SELECT workflow_id, display_name, deleted_at
                     FROM registry_workflows
                    WHERE workflow_id = %s
                      AND account_id = %s""",
                (workflow_id, account_id),
            )
            raw = cur.fetchone()
        if not raw:
            raise ServiceNotFoundError("registry_workflow not found")
        row = (
            dict(raw)
            if isinstance(raw, Mapping)
            else {
                "workflow_id": raw[0],
                "display_name": raw[1],
                "deleted_at": raw[2],
            }
        )
        return {
            "node_id": f"registry_workflow:{workflow_id}",
            "entity_type": "registry_workflow",
            "entity_id": workflow_id,
            "label": str(row.get("display_name") or workflow_id),
            "selectable": True,
            "selected": True,
            "mandatory": False,
            "status": self._row_lifecycle_status(row),
            "children": [],
            "metadata": {"root_kind": "registry_workflow"},
        }

    def _list_required_workflow_connection_refs(
        self,
        bl: Any,
        *,
        workflow_id: str,
        account_id: str,
    ) -> set[str]:
        rows = self._list_workflow_runtime_dependencies(
            bl,
            workflow_id=workflow_id,
            account_id=account_id,
        )
        return {
            str(row.get("dependency_ref") or "").strip()
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("dependency_kind") or "").strip().lower() == "project_connection"
            and bool(row.get("required_for_execution", True))
            and str(row.get("dependency_ref") or "").strip()
        }

    def _list_required_workflow_dataset_ids(self, bl: Any, *, workflow_id: str) -> set[str]:
        getter = getattr(bl, "get_workflow", None)
        if not callable(getter):
            return set()
        try:
            workflow = getter(str(workflow_id))
        except Exception:
            return set()
        if not isinstance(workflow, Mapping):
            return set()
        # Steps belong to the workflow (no phases); a dataset query anywhere in
        # the definition is a required dataset reference.
        steps = workflow.get("steps")
        if not isinstance(steps, list):
            return set()
        get_template = getattr(bl, "get_dataset_query_template", None)
        dataset_ids: set[str] = set()
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            step_kind = str(step.get("step_kind") or "").strip().lower()
            if step_kind != "dataset_query":
                continue
            override = step.get("override") if isinstance(step.get("override"), Mapping) else {}
            config = (
                override.get("dataset_query")
                if isinstance(override, Mapping)
                and isinstance(override.get("dataset_query"), Mapping)
                else {}
            )
            dataset_id = str(
                (config or {}).get("dataset_id") or step.get("dataset_id") or ""
            ).strip()
            query_template_id = str(
                (config or {}).get("query_template_id") or step.get("query_template_id") or ""
            ).strip()
            if not dataset_id and query_template_id and callable(get_template):
                with contextlib.suppress(Exception):
                    template = get_template(query_template_id)
                    if isinstance(template, Mapping):
                        dataset_id = str(template.get("dataset_id") or "").strip()
            if dataset_id:
                dataset_ids.add(dataset_id)
        return dataset_ids

    def _list_workflow_runtime_dependencies(
        self,
        bl: Any,
        *,
        workflow_id: str,
        account_id: str,
    ) -> list[dict[str, Any]]:
        lister = getattr(bl, "list_workflow_runtime_dependencies", None)
        if callable(lister):
            try:
                rows = lister(str(workflow_id), account_id=str(account_id))
            except TypeError:
                rows = lister(str(workflow_id), str(account_id))
            return [dict(item) for item in (rows or []) if isinstance(item, Mapping)]
        table_exists = getattr(bl, "_table_exists", None)
        if not callable(table_exists) or not bool(table_exists("workflow_runtime_dependencies")):
            return []
        params: list[Any] = [str(workflow_id)]
        account_clause = ""
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id and self._has_column(
            bl, "workflow_runtime_dependencies", "account_id"
        ):
            account_clause = " AND (account_id IS NULL OR account_id = %s)"
            params.append(normalized_account_id)
        with bl.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT dependency_kind, dependency_ref, required_for_execution
                  FROM workflow_runtime_dependencies
                 WHERE workflow_id = %s
                       {account_clause}
                 ORDER BY dependency_id
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
            description = cur.description
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                normalized.append(dict(row))
                continue
            if not isinstance(row, (tuple, list)):
                continue
            columns = [
                col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
                for col in (description or [])
            ]
            normalized.append(
                {
                    str(columns[idx]): row[idx]
                    for idx in range(min(len(columns), len(row)))
                    if str(columns[idx]).strip()
                }
            )
        return normalized


__all__ = [name for name in globals() if not name.startswith("__")]
