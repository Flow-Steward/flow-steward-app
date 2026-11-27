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
    principal_user_id,
)


class ScopeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    tenant_id: str | None = None
    workspace_id: Any | None = None
    project_id: Any | None = None
    workflow_id: str | None = None
    actor_id: str | None = None
    #: The ``users.id`` of the person acting: a signed-in user or a personal token or
    #: OAuth grant of that user. ``None`` for service accounts, machine tokens, the
    #: system actor, external callbacks and worker or other internal callers. Only
    #: :func:`scope_from_principal` derives it from an authenticated principal.
    user_id: str | None = None
    principal_type: str | None = None
    # Trusted authentication mechanism (local session, PAT, machine token,
    # external provider).  This is deliberately separate from principal_type
    # so delegated HITL metadata can only be accepted for machine callers.
    auth_source: str | None = None
    allowed_workspace_ids: frozenset[str] = frozenset()
    allowed_project_ids: frozenset[str] = frozenset()
    allowed_workflow_ids: frozenset[str] = frozenset()
    token_scopes: frozenset[str] = frozenset()
    #: The ADR 0076 permissions of a person or a person's token, computed once per
    #: request by the auth middleware; ``None`` for service accounts, extension runtime
    #: and system actors (their own rules apply) and for scopes built outside a request.
    permissions: frozenset[str] | None = None
    #: A person limited to chosen projects (ADR 0076 section 8): the permissions held in
    #: each reachable project, the lower of the global and the scoped role. ``None``
    #: when the person has no limit, and for every principal that is not a person.
    project_permissions: dict[str, frozenset[str]] | None = None
    #: A limited person's permissions in each visible workspace (ADR 0076 section 8a).
    workspace_permissions: dict[str, frozenset[str]] | None = None
    #: ``{project id: {workflow id: permissions}}`` of a limited person's workflow
    #: grants: in that workflow they replace the project's permissions.
    workflow_permissions: dict[str, dict[str, frozenset[str]]] | None = None
    #: ``{project id: workflow ids}``: projects a limited person reaches only through
    #: workflow grants; only these workflows and their runs are reachable there.
    project_workflow_ids: dict[str, frozenset[str]] | None = None
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
    user_id: str | None = None,
    principal_type: str | None = None,
    auth_source: str | None = None,
    allowed_workspace_ids: frozenset[str] | None = None,
    allowed_project_ids: frozenset[str] | None = None,
    allowed_workflow_ids: frozenset[str] | None = None,
    token_scopes: frozenset[str] | None = None,
    user_timezone: str | None = None,
    project_timezone: str | None = None,
    permissions: frozenset[str] | None = None,
    project_permissions: Mapping[str, Any] | None = None,
    workspace_permissions: Mapping[str, Any] | None = None,
    workflow_permissions: Mapping[str, Any] | None = None,
    project_workflow_ids: Mapping[str, Any] | None = None,
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
    resolved_user_id = (
        str(user_id if user_id is not None else base.get("user_id") or "").strip() or None
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

    resolved_permissions = _permission_keys(
        permissions if permissions is not None else base.get("permissions")
    )
    resolved_project_permissions = _project_permission_keys(
        project_permissions if project_permissions is not None else base.get("project_permissions")
    )

    resolved_workspace_permissions = _place_permission_keys(
        workspace_permissions
        if workspace_permissions is not None
        else base.get("workspace_permissions")
    )
    resolved_workflow_permissions = _workflow_permission_keys(
        workflow_permissions
        if workflow_permissions is not None
        else base.get("workflow_permissions")
    )
    resolved_project_workflow_ids = _project_workflow_ids(
        project_workflow_ids
        if project_workflow_ids is not None
        else base.get("project_workflow_ids")
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
        user_id=resolved_user_id,
        principal_type=resolved_principal_type,
        auth_source=resolved_auth_source,
        allowed_workspace_ids=resolved_allowed_workspace_ids,
        allowed_project_ids=resolved_allowed_project_ids,
        allowed_workflow_ids=resolved_allowed_workflow_ids,
        token_scopes=resolved_token_scopes,
        permissions=resolved_permissions,
        project_permissions=resolved_project_permissions,
        workspace_permissions=resolved_workspace_permissions,
        workflow_permissions=resolved_workflow_permissions,
        project_workflow_ids=resolved_project_workflow_ids,
        user_timezone=resolved_user_tz,
        project_timezone=resolved_project_tz,
    )


def _permission_keys(value: Any) -> frozenset[str] | None:
    """Permission keys from a principal or a scope; ``None`` when none were computed."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    return frozenset(str(item).strip() for item in value if str(item).strip())


def _project_permission_keys(value: Any) -> dict[str, frozenset[str]] | None:
    """``{project id: permission keys}`` of a limited person; ``None`` when not limited."""
    if not isinstance(value, Mapping):
        return None
    result: dict[str, frozenset[str]] = {}
    for project_id, keys in value.items():
        normalized = coerce_optional_project_id(project_id)
        if normalized is not None:
            result[normalized] = _permission_keys(keys) or frozenset()
    return result


def _place_permission_keys(value: Any) -> dict[str, frozenset[str]] | None:
    """``{place id: permission keys}``; ``None`` when not limited."""
    if not isinstance(value, Mapping):
        return None
    return {
        str(key).strip(): _permission_keys(keys) or frozenset()
        for key, keys in value.items()
        if str(key or "").strip()
    }


def _workflow_permission_keys(value: Any) -> dict[str, dict[str, frozenset[str]]] | None:
    """``{project id: {workflow id: permission keys}}``; ``None`` when not limited."""
    if not isinstance(value, Mapping):
        return None
    result: dict[str, dict[str, frozenset[str]]] = {}
    for project_id, flows in value.items():
        normalized = coerce_optional_project_id(project_id)
        if normalized is not None and isinstance(flows, Mapping):
            result[normalized] = _place_permission_keys(flows) or {}
    return result


def _project_workflow_ids(value: Any) -> dict[str, frozenset[str]] | None:
    """``{project id: workflow ids}`` of workflow-only projects; ``None`` when not limited."""
    if not isinstance(value, Mapping):
        return None
    result: dict[str, frozenset[str]] = {}
    for project_id, flows in value.items():
        normalized = coerce_optional_project_id(project_id)
        if normalized is not None and isinstance(flows, (list, tuple, set, frozenset)):
            result[normalized] = frozenset(str(item).strip() for item in flows if str(item).strip())
    return result


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
        user_id=principal_user_id(principal_dict) or "",
        principal_type=actor_type,
        auth_source=str((principal_dict or {}).get("auth_source") or "").strip() or None,
        allowed_workspace_ids=frozenset(principal_allowed_workspace_ids(principal_dict)),
        allowed_project_ids=frozenset(principal_allowed_project_ids(principal_dict)),
        allowed_workflow_ids=frozenset(principal_allowed_workflow_ids(principal_dict)),
        user_timezone=user_tz,
        permissions=_permission_keys((principal_dict or {}).get("permissions")),
        project_permissions=_project_permission_keys(
            (principal_dict or {}).get("project_permissions")
        ),
        workspace_permissions=(principal_dict or {}).get("workspace_permissions"),
        workflow_permissions=(principal_dict or {}).get("workflow_permissions"),
        project_workflow_ids=(principal_dict or {}).get("project_workflow_ids"),
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
            # Runtime state is never a person's authentication: no user id from it.
            return normalize_scope(
                nested_scope,
                user_id="",
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
        user_id="",
        principal_type=str(state_obj.get("principal_type") or "").strip() or None,
    )
