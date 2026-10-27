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
