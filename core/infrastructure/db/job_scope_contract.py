"""Persistence identity invariants and structural input for scoped job queries."""

from __future__ import annotations

from typing import Protocol

JOB_PROJECT_IDENTITY_CONDITIONS = (
    "j.account_id = p.account_id",
    "j.tenant_id = p.tenant_id",
    "j.workspace_id = p.workspace_id",
)


class JobQueryScope(Protocol):
    account_id: str
    tenant_id: str | None
    workspace_id: str | None
    project_id: object | None
    workflow_id: str | None
    allowed_project_ids: frozenset[str]
    allowed_workflow_ids: frozenset[str]
    allowed_workspace_ids: frozenset[str]
