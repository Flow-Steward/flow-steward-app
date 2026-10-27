"""Dataset catalog scope and project-reachability predicates."""

from __future__ import annotations

from typing import Any


def _scope_matches_row(row: dict, scope: Any) -> bool:
    account_id = str(row.get("account_id") or "").strip()
    tenant_id = str(row.get("tenant_id") or "").strip()
    workspace_id = str(row.get("workspace_id") or "").strip()
    scope_account_id = str(getattr(scope, "account_id", "") or "").strip()
    scope_tenant_id = str(getattr(scope, "tenant_id", "") or "").strip()
    if scope_account_id and not account_id:
        return False
    if account_id and scope_account_id and account_id != scope_account_id:
        return False
    if tenant_id and scope_tenant_id and tenant_id != scope_tenant_id:
        return False
    allowed_workspace_ids = getattr(scope, "allowed_workspace_ids", frozenset()) or frozenset()
    return not (
        workspace_id and allowed_workspace_ids and workspace_id not in allowed_workspace_ids
    )


def _dataset_visible_to_project_restricted_scope(row: dict, scope) -> bool:
    """For a project-restricted principal, the dataset must be reachable from an
    allowed project.

    A principal with a non-empty ``allowed_project_ids`` set may only act on a
    dataset that is either (a) workspace-wide available
    (``availability_mode == 'all_workspace_projects'``) or (b) explicitly assigned
    to at least one of its allowed projects. This mirrors the workflow runtime
    ``dataset_query`` negative control, which rejects datasets not assigned to the
    runtime project.
    """
    allowed_project_ids = getattr(scope, "allowed_project_ids", frozenset()) or frozenset()
    if not allowed_project_ids:
        return True
    if str(row.get("availability_mode") or "").strip() == "all_workspace_projects":
        return True
    dataset_project_ids = {
        str(item or "").strip()
        for item in (row.get("project_ids") or [])
        if str(item or "").strip()
    }
    return bool(dataset_project_ids & set(allowed_project_ids))


def _template_matches_dataset(template: dict, dataset_id: str) -> bool:
    return not dataset_id or str(template.get("dataset_id") or "").strip() == dataset_id


def _template_declares_scope(template: dict) -> bool:
    return any(
        str(template.get(field) or "").strip()
        for field in ("account_id", "tenant_id", "workspace_id")
    )
