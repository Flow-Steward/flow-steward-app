"""Application service for first-class workspaces."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_workspace_access

#: What changing a workspace needs where a person's access is limited to places.
WORKSPACE_MANAGE_PERMISSION = "workspaces.manage"
#: A principal limited to chosen workspaces may not add another one.
WORKSPACE_CREATE_REFUSAL_MESSAGE = (
    "A principal limited to chosen workspaces cannot create a workspace"
)


class WorkspaceConflictError(ValueError):
    """Raised when an insert-only workspace create conflicts with an existing key."""


def _workspace_reachable(scope: Any | None, workspace_id: Any, permission: str | None) -> bool:
    """Whether ``scope`` reaches this workspace (and, with ``permission``, may act there).

    A scope that is not a :class:`ScopeContext` (an internal caller) is not limited.
    """
    if scope is None or not hasattr(scope, "allowed_workspace_ids"):
        return True
    try:
        require_workspace_access(scope, workspace_id, permission)
    except ServiceForbiddenError:
        return False
    return True


def _require_workspace(scope: Any | None, workspace_id: Any, permission: str | None) -> None:
    """Workspace grants (ADR 0076 section 8a) and token allowlists: an unreachable
    workspace is answered as missing (``KeyError``), so its existence is not revealed."""
    if not _workspace_reachable(scope, workspace_id, permission):
        raise KeyError(str(workspace_id))


class WorkspaceService:
    """Thin application boundary over workspace persistence."""

    def __init__(self, store: Any):
        self._store = store

    @staticmethod
    def _scope(scope: Any | None) -> tuple[str, str]:
        account_id = str(getattr(scope, "account_id", "") or DEFAULT_ACCOUNT_ID).strip()
        tenant_id = str(getattr(scope, "tenant_id", "") or DEFAULT_TENANT_ID).strip()
        return (
            account_id or DEFAULT_ACCOUNT_ID,
            tenant_id or DEFAULT_TENANT_ID,
        )

    def list_workspaces(
        self,
        *,
        scope: Any | None = None,
        search: str = "",
        status: str = "",
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        account_id, tenant_id = self._scope(scope)
        rows = self._store.list_workspaces(
            account_id=account_id,
            tenant_id=tenant_id,
            search=search,
            status=status,
            include_archived=include_archived,
        )
        return [row for row in rows if _workspace_reachable(scope, (row or {}).get("id"), None)]

    def get_workspace(
        self, workspace_id: str, *, scope: Any | None = None, permission: str | None = None
    ) -> dict[str, Any]:
        _require_workspace(scope, workspace_id, permission)
        account_id, tenant_id = self._scope(scope)
        row = self._store.get_workspace(
            workspace_id,
            account_id=account_id,
            tenant_id=tenant_id,
            include_archived=True,
        )
        if not row:
            raise KeyError(workspace_id)
        return dict(row)

    def save_workspace(
        self, payload: dict[str, Any], *, scope: Any | None = None
    ) -> dict[str, Any]:
        account_id, tenant_id = self._scope(scope)
        workspace_id = str(payload.get("id") or "").strip()
        if workspace_id:
            existing = self.get_workspace(
                workspace_id, scope=scope, permission=WORKSPACE_MANAGE_PERMISSION
            )
        else:
            self._require_unlimited_create(scope)
            existing = {}
        requested_key = str(payload.get("workspace_key") or "").strip()
        existing_key = str(existing.get("workspace_key") or "").strip()
        if requested_key and existing_key and requested_key != existing_key:
            raise ValueError(
                "workspace_key is stable; use the workspace key-change preview and execute endpoints"
            )
        merged = {**existing, **payload, "workspace_key": existing_key or requested_key}
        return dict(
            self._store.save_workspace(
                merged,
                account_id=account_id,
                tenant_id=tenant_id,
                author=str(getattr(scope, "actor_name", "admin") or "admin"),
            )
        )

    def preview_workspace_key_change(
        self,
        workspace_id: str,
        workspace_key: str,
        *,
        scope: Any | None = None,
    ) -> dict[str, Any]:
        current = self.get_workspace(
            workspace_id, scope=scope, permission=WORKSPACE_MANAGE_PERMISSION
        )
        next_key = str(workspace_key or "").strip()
        rows = self.list_workspaces(scope=scope, include_archived=True)
        conflict = next(
            (
                row
                for row in rows
                if str(row.get("id") or "") != str(workspace_id)
                and str(row.get("workspace_key") or "").strip() == next_key
            ),
            None,
        )
        return {
            "workspace_id": str(workspace_id),
            "current_workspace_key": str(current.get("workspace_key") or ""),
            "requested_workspace_key": next_key,
            "conflict": bool(conflict),
            "conflict_workspace_id": str((conflict or {}).get("id") or ""),
            "impact": {
                "projects_count": int(current.get("projects_count") or 0),
                "jobs_count": int(current.get("jobs_count") or 0),
            },
            "confirmation_workspace_id": str(workspace_id),
        }

    def change_workspace_key(
        self,
        workspace_id: str,
        workspace_key: str,
        *,
        confirm_workspace_id: str,
        scope: Any | None = None,
    ) -> dict[str, Any]:
        if str(confirm_workspace_id or "").strip() != str(workspace_id):
            raise ValueError("confirm_workspace_id must exactly match the workspace id")
        preview = self.preview_workspace_key_change(
            workspace_id,
            workspace_key,
            scope=scope,
        )
        if preview["conflict"]:
            raise WorkspaceConflictError(
                f"workspace_key '{str(workspace_key or '').strip()}' already exists"
            )
        account_id, tenant_id = self._scope(scope)
        current = self.get_workspace(workspace_id, scope=scope)
        return dict(
            self._store.save_workspace(
                {**current, "id": str(workspace_id), "workspace_key": str(workspace_key).strip()},
                account_id=account_id,
                tenant_id=tenant_id,
                author=str(getattr(scope, "actor_name", "admin") or "admin"),
            )
        )

    def create_workspace(
        self, payload: dict[str, Any], *, scope: Any | None = None
    ) -> dict[str, Any]:
        self._require_unlimited_create(scope)
        account_id, tenant_id = self._scope(scope)
        row = self._store.create_workspace(
            payload,
            account_id=account_id,
            tenant_id=tenant_id,
            author=str(getattr(scope, "actor_name", "admin") or "admin"),
        )
        if not row:
            workspace_key = str(payload.get("workspace_key") or "").strip()
            raise WorkspaceConflictError(f"workspace_key '{workspace_key}' already exists")
        return dict(row)

    def restore_workspace(self, workspace_id: str, *, scope: Any | None = None) -> dict[str, Any]:
        _require_workspace(scope, workspace_id, WORKSPACE_MANAGE_PERMISSION)
        account_id, tenant_id = self._scope(scope)
        return dict(
            self._store.set_workspace_archived(
                workspace_id,
                archived=False,
                account_id=account_id,
                tenant_id=tenant_id,
                author=str(getattr(scope, "actor_name", "admin") or "admin"),
            )
        )

    def list_projects(self, workspace_id: str, *, scope: Any | None = None) -> list[dict[str, Any]]:
        _require_workspace(scope, workspace_id, None)
        account_id, tenant_id = self._scope(scope)
        rows = self._store.list_workspace_projects(
            workspace_id,
            account_id=account_id,
            tenant_id=tenant_id,
        )
        allowed = getattr(scope, "allowed_project_ids", None) or frozenset()
        if not allowed:
            return list(rows)
        return [row for row in rows if str((row or {}).get("id") or "") in allowed]

    @staticmethod
    def _require_unlimited_create(scope: Any | None) -> None:
        limited = bool(getattr(scope, "allowed_workspace_ids", None)) or (
            getattr(scope, "workspace_permissions", None) is not None
            or getattr(scope, "project_permissions", None) is not None
        )
        if limited:
            raise PermissionError(WORKSPACE_CREATE_REFUSAL_MESSAGE)
