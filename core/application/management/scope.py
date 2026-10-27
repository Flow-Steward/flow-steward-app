from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from core.application.auth.audit import request_actor_context
from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.auth.principal import (
    coerce_optional_project_id,
    coerce_optional_workflow_id,
    coerce_optional_workspace_id,
    principal_allowed_project_ids,
    principal_allowed_workflow_ids,
    principal_allowed_workspace_ids,
)


class ScopeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    tenant_id: str | None = None
    workspace_id: Any | None = None
    project_id: Any | None = None
    workflow_id: str | None = None
    actor_id: str | None = None
    principal_type: str | None = None
    # Trusted authentication mechanism (local session, PAT, machine token,
    # external provider).  This is deliberately separate from principal_type
    # so delegated HITL metadata can only be accepted for machine callers.
    auth_source: str | None = None
    allowed_workspace_ids: frozenset[str] = frozenset()
    allowed_project_ids: frozenset[str] = frozenset()
    allowed_workflow_ids: frozenset[str] = frozenset()
    token_scopes: frozenset[str] = frozenset()
    user_timezone: str | None = None
    project_timezone: str | None = None

    @field_validator("workspace_id", mode="before")
    @classmethod
    def _coerce_optional_identity(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @property
    def actor_name(self) -> str:
        actor = str(self.actor_id or "").strip()
        if actor:
            return actor
        if str(self.principal_type or "").strip().lower() == "service_account":
            return "service_account"
        if str(self.principal_type or "").strip().lower() == "system_actor":
            return "system"
        return "operator"

    def cache_key(self) -> str:
        return "|".join(
            [
                str(self.account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID,
                str(self.tenant_id or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID,
                str(self.workspace_id or "").strip(),
                str(self.project_id or ""),
                str(self.workflow_id or "").strip(),
                str(self.actor_id or "").strip(),
                str(self.principal_type or "").strip(),
            ]
        )


def scope_audit_fields(scope: ScopeContext | None) -> dict[str, Any]:
    """Extract audit-relevant scope fields for runtime context and logging."""
    if scope is None:
        return {}
    fields: dict[str, Any] = {}
    if scope.account_id:
        fields["account_id"] = scope.account_id
    if scope.project_id is not None:
        fields["project_id"] = scope.project_id
    if scope.workflow_id:
        fields["workflow_id"] = scope.workflow_id
    if scope.actor_id:
        fields["actor_id"] = scope.actor_id
    if scope.principal_type:
        fields["actor_type"] = scope.principal_type
    return fields


def normalize_scope(
    scope: ScopeContext | Mapping[str, Any] | None = None,
    *,
    account_id: str | None = None,
    tenant_id: str | None = None,
    workspace_id: Any = None,
    project_id: Any = None,
    workflow_id: Any = None,
    actor_id: str | None = None,
    principal_type: str | None = None,
    auth_source: str | None = None,
    allowed_workspace_ids: frozenset[str] | None = None,
    allowed_project_ids: frozenset[str] | None = None,
    allowed_workflow_ids: frozenset[str] | None = None,
    token_scopes: frozenset[str] | None = None,
    user_timezone: str | None = None,
    project_timezone: str | None = None,
) -> ScopeContext:
    base: dict[str, Any]
    if isinstance(scope, ScopeContext):
        base = scope.model_dump()
    elif isinstance(scope, Mapping):
        base = dict(scope)
    else:
        base = {}

    resolved_account_id = (
        str(account_id or base.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
        or DEFAULT_ACCOUNT_ID
    )
    tenant_value = tenant_id if tenant_id is not None else base.get("tenant_id")
    resolved_tenant_id = str(tenant_value).strip() if tenant_value is not None else ""
    resolved_tenant_id = resolved_tenant_id or None
    resolved_workspace_id = coerce_optional_workspace_id(
        workspace_id if workspace_id is not None else base.get("workspace_id")
    )
    resolved_project_id = coerce_optional_project_id(
        project_id if project_id is not None else base.get("project_id")
    )
    resolved_workflow_id = coerce_optional_workflow_id(
        workflow_id if workflow_id is not None else base.get("workflow_id")
    )
    resolved_actor_id = (
        str(
            actor_id
            if actor_id is not None
            else base.get("actor_id") or base.get("actor") or base.get("username") or ""
        ).strip()
        or None
    )
    resolved_principal_type = (
        str(principal_type or base.get("principal_type") or "").strip() or None
    )
    resolved_auth_source = (
        str(auth_source if auth_source is not None else base.get("auth_source") or "").strip()
        or None
    )

    resolved_allowed_workspace_ids = frozenset(
        str(item).strip()
        for item in (
            allowed_workspace_ids
            if allowed_workspace_ids is not None
            else base.get("allowed_workspace_ids") or frozenset()
        )
        if str(item).strip()
    )
    resolved_allowed_project_ids = frozenset(
        str(item).strip()
        for item in (
            allowed_project_ids
            if allowed_project_ids is not None
            else base.get("allowed_project_ids") or frozenset()
        )
        if str(item).strip()
    )
    resolved_allowed_workflow_ids = frozenset(
        str(item).strip()
        for item in (
            allowed_workflow_ids
            if allowed_workflow_ids is not None
            else base.get("allowed_workflow_ids") or frozenset()
        )
        if str(item).strip()
    )
    resolved_token_scopes = frozenset(
        str(item).strip()
        for item in (
            token_scopes if token_scopes is not None else base.get("token_scopes") or frozenset()
        )
        if str(item).strip()
    )

    resolved_user_tz = (
        str(
            user_timezone
            if user_timezone is not None
            else base.get("user_timezone") or base.get("timezone") or ""
        ).strip()
        or None
    )
    resolved_project_tz = (
        str(
            project_timezone
            if project_timezone is not None
            else base.get("project_timezone") or base.get("schedule_timezone") or ""
        ).strip()
        or None
    )

    return ScopeContext(
        account_id=resolved_account_id,
        tenant_id=resolved_tenant_id,
        workspace_id=resolved_workspace_id,
        project_id=resolved_project_id,
        workflow_id=resolved_workflow_id,
        actor_id=resolved_actor_id,
        principal_type=resolved_principal_type,
        auth_source=resolved_auth_source,
        allowed_workspace_ids=resolved_allowed_workspace_ids,
        allowed_project_ids=resolved_allowed_project_ids,
        allowed_workflow_ids=resolved_allowed_workflow_ids,
        token_scopes=resolved_token_scopes,
        user_timezone=resolved_user_tz,
        project_timezone=resolved_project_tz,
    )


def scope_from_principal(
    principal: Mapping[str, Any] | None,
    *,
    project_id: Any = None,
    workspace_id: Any = None,
    workflow_id: Any = None,
    actor_id: str | None = None,
) -> ScopeContext:
    principal_dict = dict(principal) if isinstance(principal, Mapping) else None
    actor_type, actor_name, account_id = request_actor_context(principal_dict)
    user_tz = str((principal_dict or {}).get("timezone") or "").strip() or None
    return normalize_scope(
        account_id=account_id,
        tenant_id=str((principal_dict or {}).get("tenant_id") or DEFAULT_TENANT_ID).strip()
        or DEFAULT_TENANT_ID,
        workspace_id=workspace_id
        if workspace_id is not None
        else (principal_dict or {}).get("workspace_id"),
        project_id=project_id,
        workflow_id=workflow_id,
        actor_id=actor_id if actor_id is not None else actor_name,
        principal_type=actor_type,
        auth_source=str((principal_dict or {}).get("auth_source") or "").strip() or None,
        allowed_workspace_ids=frozenset(principal_allowed_workspace_ids(principal_dict)),
        allowed_project_ids=frozenset(principal_allowed_project_ids(principal_dict)),
        allowed_workflow_ids=frozenset(principal_allowed_workflow_ids(principal_dict)),
        user_timezone=user_tz,
    )


def scope_from_state(
    state: Mapping[str, Any] | None,
    *,
    project_id: Any = None,
    workspace_id: Any = None,
    workflow_id: Any = None,
    actor_id: str | None = None,
) -> ScopeContext:
    if isinstance(state, Mapping):
        nested_scope = state.get("scope_context")
        if isinstance(nested_scope, Mapping):
            return normalize_scope(
                nested_scope,
                workspace_id=workspace_id
                if workspace_id is not None
                else state.get("workspace_id"),
                project_id=project_id if project_id is not None else state.get("project_id"),
                workflow_id=workflow_id if workflow_id is not None else state.get("workflow_id"),
                actor_id=actor_id,
            )

    state_obj = dict(state) if isinstance(state, Mapping) else {}
    return normalize_scope(
        state_obj,
        account_id=str(state_obj.get("account_id") or DEFAULT_ACCOUNT_ID).strip()
        or DEFAULT_ACCOUNT_ID,
        tenant_id=str(state_obj.get("tenant_id") or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID,
        workspace_id=workspace_id if workspace_id is not None else state_obj.get("workspace_id"),
        project_id=project_id if project_id is not None else state_obj.get("project_id"),
        workflow_id=workflow_id
        if workflow_id is not None
        else (
            state_obj.get("workflow_id")
            or state_obj.get("selected_workflow")
            or state_obj.get("selected_workflow_id")
        ),
        actor_id=actor_id
        if actor_id is not None
        else (
            state_obj.get("actor_id")
            or state_obj.get("actor")
            or state_obj.get("actor_username")
            or state_obj.get("username")
        ),
        principal_type=str(state_obj.get("principal_type") or "").strip() or None,
    )
