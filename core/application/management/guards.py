"""Central scope-enforcement guards for the service layer."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import coerce_optional_project_id, coerce_optional_workflow_id
from core.application.management.scope import ScopeContext

from .errors import ServiceForbiddenError

# Sentinel value that never matches any real principal's account_id.
# Records with an explicitly falsy account_id are treated as inaccessible.
_UNOWNED_ACCOUNT = "__unowned__"
ACCESS_DENIED_MESSAGE = "Access denied"
WORKFLOW_CATALOG_ACCOUNT_MESSAGE = (
    "Workflow registry catalog is only supported for the default account"
)


def _normalize_account_id(value: Any) -> str:
    return str(value or "").strip()


def _record_account_id(record: dict[str, Any] | Any) -> str:
    if not isinstance(record, dict):
        return _UNOWNED_ACCOUNT
    # Key present but explicitly None/empty → fail-closed.
    if "account_id" in record:
        raw = record["account_id"]
        if not raw:
            return _UNOWNED_ACCOUNT
        return _normalize_account_id(raw) or _UNOWNED_ACCOUNT
    # Key absent entirely → fail closed.
    return _UNOWNED_ACCOUNT


def require_account_access(scope: ScopeContext, record: dict[str, Any]) -> None:
    """Raise ServiceForbiddenError if scope.account_id doesn't own *record*."""
    if _normalize_account_id(scope.account_id) != _record_account_id(record):
        raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)


def require_project_access(scope: ScopeContext, project: dict[str, Any]) -> None:
    """Guard that the principal's account owns this project and membership allows it.

    Checks:
    1. Account ownership — scope.account_id must match project.account_id
    2. Project membership — if scope.allowed_project_ids is non-empty, the
       project's id must be in that set (service-account / restricted-user case)
    """
    require_account_access(scope, project)
    if scope.allowed_workspace_ids:
        workspace_id = str((project or {}).get("workspace_id") or "").strip()
        if workspace_id and workspace_id not in scope.allowed_workspace_ids:
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
    if scope.allowed_project_ids:
        project_id = coerce_optional_project_id(project.get("id"))
        if project_id is not None and project_id not in scope.allowed_project_ids:
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)


#: The person's role in the project does not hold the permission the action needs.
PROJECT_ROLE_REFUSAL_MESSAGE = "Your role in this project does not allow this action"


def require_project_permission(
    scope: ScopeContext,
    project: dict[str, Any] | str | None,
    permission: str,
    *,
    workflow_id: str | None = None,
) -> None:
    """:func:`require_project_access`, then the permission in that project.

    ``project`` is the project record (account, workspace and allowlist are checked)
    or its id (only the allowlist and the per-project permission are checked; the
    caller checks the account when it loads the record). A person limited to chosen
    places (``scope.project_permissions`` set, ADR 0076 section 8a) must hold
    ``permission`` in that project; a project without an id is refused to them. With
    ``workflow_id`` the permission is the one in that workflow
    (:func:`require_workflow_permission`). Every other principal is checked exactly as
    by :func:`require_project_access`.
    """
    if isinstance(project, dict):
        require_project_access(scope, project)
        project_id = coerce_optional_project_id(project.get("id"))
    else:
        project_id = coerce_optional_project_id(project)
        if (
            scope.allowed_project_ids
            and project_id is not None
            and project_id not in scope.allowed_project_ids
        ):
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
    if scope.project_permissions is None:
        return
    if project_id is None:
        raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
    held = scoped_place_permissions(scope, project_id, workflow_id)
    if held is None:
        raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
    if permission not in held:
        raise ServiceForbiddenError(PROJECT_ROLE_REFUSAL_MESSAGE)


def scoped_place_permissions(
    scope: ScopeContext, project_id: str, workflow_id: str | None = None
) -> frozenset[str] | None:
    """A limited person's permissions in a project, or in one of its workflows.

    The most specific grant applies: workflow, then project, then workspace (the
    middleware already folded the workspace into the project's permissions). In a
    project reached only through workflow grants another workflow is out of reach
    (``None``). Not for a person without a limit: their global permissions apply.
    """
    held = (scope.project_permissions or {}).get(project_id)
    if held is None:
        return None
    normalized_workflow = coerce_optional_workflow_id(workflow_id) if workflow_id else None
    if not normalized_workflow:
        return held
    granted = (scope.workflow_permissions or {}).get(project_id, {}).get(normalized_workflow)
    if granted is not None:
        return granted
    restricted = (scope.project_workflow_ids or {}).get(project_id)
    if restricted is not None and normalized_workflow not in restricted:
        return None
    return held


def workflow_in_scope_limit(scope: ScopeContext, project_id: Any, workflow_id: Any) -> bool:
    """Whether a limited person reaches this workflow of this project; always for others.

    Only projects reached through workflow grants alone restrict their workflows; a
    run with no workflow in such a project is out of reach.
    """
    restricted_projects = scope.project_workflow_ids
    if not restricted_projects:
        return True
    normalized_project = coerce_optional_project_id(project_id)
    if normalized_project is None or normalized_project not in restricted_projects:
        return True
    normalized_workflow = coerce_optional_workflow_id(workflow_id) if workflow_id else None
    return (
        bool(normalized_workflow) and normalized_workflow in restricted_projects[normalized_project]
    )


def rows_in_workflow_scope_limit(
    scope: ScopeContext,
    project_id: Any,
    rows: Any,
    workflow_of: Any = None,
) -> list[Any]:
    """The rows of one project whose workflow a limited person reaches (ADR 0076 §8b).

    ``workflow_of(row)`` names a row's workflow (default: its ``workflow_id``). In a
    project reached only through workflow grants a row of another workflow, or of no
    workflow, is dropped; every row is kept for everybody else.
    """
    items = list(rows or [])
    restricted = scope.project_workflow_ids or {}
    normalized_project = coerce_optional_project_id(project_id)
    if normalized_project is None or normalized_project not in restricted:
        return items
    reader = workflow_of or (lambda row: (row or {}).get("workflow_id"))
    return [row for row in items if workflow_in_scope_limit(scope, normalized_project, reader(row))]


def require_workflow_permission(
    scope: ScopeContext,
    project: dict[str, Any] | str | None,
    workflow_id: str | None,
    permission: str,
) -> None:
    """The permission in one workflow of a project: workflow, project, then workspace grant.

    Also checks the token allowlist of workflows (:func:`require_workflow_access`).
    """
    if workflow_id:
        require_workflow_access(scope, workflow_id)
    require_project_permission(scope, project, permission, workflow_id=workflow_id)


def require_workspace_access(
    scope: ScopeContext, workspace_id: Any, permission: str | None = None
) -> None:
    """Guard a workspace: in the allowlist, and for a limited person, ``permission`` there.

    A limited person sees a workspace they hold a grant for, or one holding a
    project they reach; ``permission`` is checked against the workspace's grant
    (read only for a workspace reached through a grant inside it).
    """
    normalized = str(workspace_id or "").strip()
    if scope.allowed_workspace_ids and normalized not in scope.allowed_workspace_ids:
        raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
    if scope.workspace_permissions is None and scope.project_permissions is None:
        return
    held = (scope.workspace_permissions or {}).get(normalized)
    if held is None:
        raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
    if permission is not None and permission not in held:
        raise ServiceForbiddenError(PROJECT_ROLE_REFUSAL_MESSAGE)


def require_permission_in_every_project(scope: ScopeContext, permission: str) -> None:
    """For an action that spans projects: a limited person must hold it in all of theirs.

    A person without a limit and every other principal pass (their global permission
    was checked by the route). A limited person with no project is refused.
    """
    if scope.project_permissions is None:
        return
    if not scope.project_permissions or any(
        permission not in held for held in scope.project_permissions.values()
    ):
        raise ServiceForbiddenError(PROJECT_ROLE_REFUSAL_MESSAGE)


def require_permission_for_optional_project(
    scope: ScopeContext, project_id: Any, permission: str, *, workflow_id: str | None = None
) -> None:
    """For a person limited to chosen projects: :func:`require_project_permission` when
    the action names a project (in ``workflow_id`` when it names a workflow too), else
    :func:`require_permission_in_every_project`.

    Everybody else is left to the checks the action already makes.
    """
    if scope.project_permissions is None:
        return
    if coerce_optional_project_id(project_id) is not None:
        require_project_permission(scope, str(project_id), permission, workflow_id=workflow_id)
        return
    require_permission_in_every_project(scope, permission)


def require_workflow_access(scope: ScopeContext, workflow_id: str) -> None:
    """Guard that the principal is allowed to access this workflow.

    If scope.allowed_workflow_ids is non-empty, workflow_id must be in that set.
    """
    if scope.allowed_workflow_ids:
        normalized = coerce_optional_workflow_id(workflow_id)
        if not normalized or normalized not in scope.allowed_workflow_ids:
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)


def is_default_workflow_catalog_account(scope: ScopeContext) -> bool:
    """Return whether *scope* may access the OSS deployment-global workflow catalog."""
    account_id = _normalize_account_id(scope.account_id) or DEFAULT_ACCOUNT_ID
    return account_id == DEFAULT_ACCOUNT_ID


def require_default_workflow_catalog_account(scope: ScopeContext) -> None:
    """Fail closed when a dormant secondary account reaches the global catalog."""
    if not is_default_workflow_catalog_account(scope):
        raise ServiceForbiddenError(WORKFLOW_CATALOG_ACCOUNT_MESSAGE)


def require_event_access(scope: ScopeContext, event: dict[str, Any]) -> None:
    """Guard that the principal is allowed to access this event row."""
    require_account_access(scope, event)
    project_id = coerce_optional_project_id((event or {}).get("project_id"))
    if project_id is not None:
        require_project_access(
            scope,
            {
                "id": project_id,
                "account_id": _record_account_id(event),
            },
        )
    workflow_id = coerce_optional_workflow_id((event or {}).get("workflow_id"))
    if workflow_id:
        require_workflow_access(scope, workflow_id)
    if project_id is not None and not workflow_in_scope_limit(scope, project_id, workflow_id):
        raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
