"""Scope predicates shared by job aggregates and operational reports."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.scope import ScopeContext
from core.infrastructure.db.job_scope_contract import JOB_PROJECT_IDENTITY_CONDITIONS, JobQueryScope


def job_scope_conditions(scope: JobQueryScope) -> tuple[list[str], list[Any]]:
    """Return parameterized predicates for jobs j joined to projects p."""
    conditions = ["p.account_id = %s", "p.tenant_id = %s", *JOB_PROJECT_IDENTITY_CONDITIONS]
    params: list[Any] = [scope.account_id, scope.tenant_id or DEFAULT_TENANT_ID]
    for value, column in (
        (scope.project_id, "j.project_id"),
        (scope.workflow_id, "j.selected_workflow"),
        (scope.workspace_id, "p.workspace_id"),
    ):
        if value:
            conditions.append(f"{column} = %s")
            params.append(str(value))
    for allowed, column in (
        (scope.allowed_project_ids, "j.project_id"),
        (scope.allowed_workflow_ids, "j.selected_workflow"),
        (scope.allowed_workspace_ids, "p.workspace_id"),
    ):
        if allowed:
            conditions.append(f"{column} = ANY(%s)")
            params.append(sorted(allowed))
    return conditions, params


def job_row_in_scope(scope: ScopeContext, row: dict[str, Any]) -> bool:
    """Filter projected query rows; missing restricted identities fail closed."""
    if str(row.get("tenant_id") or "") != str(scope.tenant_id or DEFAULT_TENANT_ID):
        return False
    if str(row.get("account_id") or "") != scope.account_id:
        return False
    workflow = str(row.get("selected_workflow") or row.get("workflow") or "")
    for actual, selected, allowed in (
        (str(row.get("project_id") or ""), scope.project_id, scope.allowed_project_ids),
        (workflow, scope.workflow_id, scope.allowed_workflow_ids),
        (str(row.get("workspace_id") or ""), scope.workspace_id, scope.allowed_workspace_ids),
    ):
        if selected and actual != str(selected):
            return False
        if allowed and actual not in allowed:
            return False
    return True
