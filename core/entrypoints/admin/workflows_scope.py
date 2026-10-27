# ruff: noqa: F403, F405, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR


def _request_scope(request: Request):
    state_scope = getattr(request.state, "scope_context", None)
    auth_user = getattr(request.state, "auth_user", None)
    if state_scope is not None:
        scope = normalize_scope(state_scope)
    elif isinstance(auth_user, dict):
        scope = normalize_scope(
            auth_user,
            account_id=str(auth_user.get("account_id") or DEFAULT_ACCOUNT_ID).strip(),
            tenant_id=str(auth_user.get("tenant_id") or DEFAULT_TENANT_ID).strip(),
            actor_id=str(auth_user.get("username") or auth_user.get("id") or "").strip() or None,
            principal_type=str(auth_user.get("principal_type") or "").strip() or None,
        )
    else:
        scope = normalize_scope()
    require_default_registry_account(scope)
    return scope


def _raise_scope_forbidden_http_error(exc: ServiceForbiddenError) -> None:
    raise HTTPException(status_code=403, detail=str(exc)) from exc


def _enforce_workflow_scope(scope: Any, workflow_id: str) -> None:
    try:
        require_workflow_access(scope, str(workflow_id or "").strip())
    except ServiceForbiddenError as exc:
        _raise_scope_forbidden_http_error(exc)


def _enforce_project_scope(db: Any, scope: Any, project_id: str | None) -> str:
    normalized_project_id = _normalize_optional_entity_id(project_id)
    if not normalized_project_id:
        return ""
    project = db.get_project(normalized_project_id) if hasattr(db, "get_project") else None
    if isinstance(project, dict):
        try:
            require_project_access(scope, dict(project))
        except ServiceForbiddenError as exc:
            _raise_scope_forbidden_http_error(exc)
        return normalized_project_id
    allowed_project_ids = getattr(scope, "allowed_project_ids", None)
    if allowed_project_ids and normalized_project_id not in allowed_project_ids:
        raise HTTPException(status_code=403, detail=ACCESS_DENIED_VALUE)
    return normalized_project_id


def _resolve_scoped_project_id(db: Any, scope: Any, project_id: str | None) -> str | None:
    normalized_project_id = _enforce_project_scope(db, scope, project_id)
    return normalized_project_id or None


def _enforce_import_resolution_scope(db: Any, scope: Any, resolutions: list[dict]) -> None:
    for resolution in resolutions or []:
        if not isinstance(resolution, dict):
            continue
        action = _normalize_text(resolution.get("action"))
        dependency_type = _normalize_text(
            resolution.get("dependency_type") or resolution.get("type")
        )
        dependency_key = _normalize_text(resolution.get("dependency_key"))
        if (
            action == "create_connection_placeholder"
            or dependency_type == "project_connection"
            or dependency_key.startswith("project_connection:")
        ):
            project_id = _enforce_project_scope(db, scope, resolution.get("project_id"))
            if action in {"map_existing", "use_existing"}:
                target_connection_id = _normalize_optional_entity_id(
                    resolution.get("target_ref")
                    or resolution.get("target_id")
                    or resolution.get("target_connection_id")
                )
                if target_connection_id and not project_id:
                    # A connection is looked up within its project. Without one the
                    # empty id reached Postgres and surfaced as a bare 500, so say
                    # which field is missing instead.
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "A project_connection resolution that maps an existing "
                            "connection must include project_id"
                        ),
                    )
                _enforce_project_connection_scope(
                    db,
                    project_id=project_id,
                    connection_id=target_connection_id,
                )


def _enforce_project_connection_scope(
    db: Any,
    *,
    project_id: str,
    connection_id: str | None,
) -> str:
    normalized_connection_id = _normalize_optional_entity_id(connection_id)
    if not normalized_connection_id:
        return ""
    connection = None
    get_connection = getattr(db, "get_project_connection", None)
    if not callable(get_connection):
        connection_store = _platform_logger_for_admin_catalog(db)
        get_connection = getattr(connection_store, "get_project_connection", None)
    if callable(get_connection):
        try:
            connection = get_connection(
                project_id=project_id, connection_id=normalized_connection_id
            )
        except TypeError:
            connection = get_connection(connection_id=normalized_connection_id)
    if not isinstance(connection, dict):
        raise HTTPException(status_code=403, detail=ACCESS_DENIED_VALUE)
    if _normalize_optional_entity_id(connection.get("project_id")) != project_id:
        raise HTTPException(status_code=403, detail=ACCESS_DENIED_VALUE)
    return normalized_connection_id


def _workflow_visible_to_scope(scope: Any, workflow: dict[str, Any]) -> bool:
    workflow_id = _normalize_text(workflow.get("workflow_id") or workflow.get("id"))
    if workflow_id:
        try:
            require_workflow_access(scope, workflow_id)
        except ServiceForbiddenError:
            return False
    project_id = _normalize_optional_entity_id(workflow.get("project_id"))
    allowed_project_ids = getattr(scope, "allowed_project_ids", None)
    if project_id and allowed_project_ids and project_id not in allowed_project_ids:
        return False
    row_account_id = _normalize_text(workflow.get("account_id"))
    scope_account_id = _normalize_text(getattr(scope, "account_id", ""))
    return not (row_account_id and scope_account_id and row_account_id != scope_account_id)


def _enforce_workflow_row_scope(db: Any, scope: Any, workflow: dict[str, Any]) -> dict[str, Any]:
    payload = dict(workflow or {})
    workflow_id = _normalize_text(payload.get("workflow_id") or payload.get("id"))
    if workflow_id:
        _enforce_workflow_scope(scope, workflow_id)
    row_account_id = _normalize_text(payload.get("account_id"))
    scope_account_id = _normalize_text(getattr(scope, "account_id", ""))
    if row_account_id and scope_account_id and row_account_id != scope_account_id:
        raise HTTPException(status_code=403, detail=ACCESS_DENIED_VALUE)
    _enforce_project_scope(db, scope, payload.get("project_id"))
    return payload


def _assert_workflow_mutation_allowed(workflow: dict[str, Any]) -> None:
    """Reject writes to globally shared platform workflow templates."""
    payload = dict(workflow or {})
    if (
        _normalize_text(payload.get("account_id")) == ""
        and _normalize_text(payload.get("scope_kind")) == "system"
    ):
        raise HTTPException(status_code=409, detail="platform_workflow_requires_clone")


def _agent_visible_to_scope(scope: Any, agent: dict[str, Any]) -> bool:
    agent_id = _normalize_text(agent.get("agent_id") or agent.get("id"))
    if not agent_id:
        return False
    row_account_id = _normalize_text(agent.get("account_id"))
    scope_account_id = _normalize_text(getattr(scope, "account_id", ""))
    return not (row_account_id and scope_account_id and row_account_id != scope_account_id)


def _enforce_extension_contract_scope(scope: Any, contract: dict[str, Any] | None) -> None:
    if not isinstance(contract, dict):
        return
    row_account_id = _normalize_text(contract.get("account_id"))
    scope_account_id = _normalize_text(getattr(scope, "account_id", ""))
    if row_account_id and scope_account_id and row_account_id != scope_account_id:
        raise HTTPException(status_code=403, detail=ACCESS_DENIED_VALUE)
    contract_project_id = contract.get("project_id")
    if _normalize_optional_entity_id(contract_project_id):
        _enforce_project_scope(_get_db(), scope, contract_project_id)


def _bind_workflow_to_project_if_requested(
    db: Any,
    *,
    workflow_id: str,
    project_id: str | None,
) -> None:
    normalized_project_id = _normalize_optional_entity_id(project_id)
    normalized_workflow_id = _normalize_text(workflow_id)
    if not normalized_project_id or not normalized_workflow_id:
        return
    add_binding = getattr(db, "add_project_workflow_binding", None)
    if not callable(add_binding):
        raise RuntimeError("Workflow store does not support atomic project binding")
    add_binding(normalized_project_id, normalized_workflow_id)


def _viewer_can_manage_extensions(request: Request) -> bool:
    """Whether this viewer may install or turn on extensions: the admin role.

    The rule the Extensions pages apply (``administer_platform`` is the admin
    role), so a notice offers Install or Turn on only to someone who can open
    the page it links to.
    """
    for attribute in ("auth_principal", "auth_user"):
        principal = getattr(request.state, attribute, None)
        if principal is None:
            continue
        roles = (
            principal.get("roles")
            if isinstance(principal, Mapping)
            else getattr(principal, "roles", None)
        )
        if roles is None:
            continue
        return "admin" in {str(role or "").strip().lower() for role in roles or []}
    return False


def _with_viewer_extension_permission(payload: Any, request: Request) -> Any:
    """Say on an unavailable workflow whether this viewer can fix it themselves."""
    if not isinstance(payload, dict):
        return payload
    targets = [payload]
    if isinstance(payload.get("portability"), dict):
        targets.append(payload["portability"])
    for target in targets:
        details = target.get("missing_extension_details")
        if isinstance(details, list) and details:
            target["viewer_can_manage_extensions"] = _viewer_can_manage_extensions(request)
    return payload


def _history_snapshot_payload(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    snapshot = entry.get("snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _resolve_workflow_scope_target(
    db: Any,
    request: Request,
    workflow_id: str,
    *,
    history_revision: int | None = None,
    changelog_entry_id: str | None = None,
) -> tuple[dict[str, Any] | None, Any]:
    scope = _request_scope(request)
    workflow = _resolve_workflow_row(
        db,
        workflow_id,
        account_id=scope.account_id,
    )
    if isinstance(workflow, dict):
        return _enforce_workflow_row_scope(db, scope, workflow), scope
    if history_revision is not None and hasattr(db, "get_entity_revision"):
        snapshot = _history_snapshot_payload(
            db.get_entity_revision(
                entity_type="workflow",
                entity_id=workflow_id,
                revision=history_revision,
            )
        )
        if snapshot:
            return _enforce_workflow_row_scope(db, scope, snapshot), scope
    if changelog_entry_id and hasattr(db, "get_changelog_entry"):
        snapshot = _history_snapshot_payload(db.get_changelog_entry(changelog_entry_id))
        if snapshot:
            return _enforce_workflow_row_scope(db, scope, snapshot), scope
    if hasattr(db, "get_entity_history"):
        snapshot_rows = db.get_entity_history(
            entity_type="workflow",
            entity_id=workflow_id,
            limit=1,
        )
        if isinstance(snapshot_rows, list) and snapshot_rows:
            snapshot = _history_snapshot_payload(snapshot_rows[0])
            if snapshot:
                return _enforce_workflow_row_scope(db, scope, snapshot), scope
    return None, scope


def _resolve_active_workflow_scope_target(
    db: Any,
    request: Request,
    workflow_id: str,
) -> tuple[dict[str, Any] | None, Any]:
    """Resolve an active workflow without reviving a soft-deleted history snapshot."""
    scope = _request_scope(request)
    workflow = _resolve_workflow_row(
        db,
        workflow_id,
        account_id=scope.account_id,
    )
    if isinstance(workflow, dict):
        return _enforce_workflow_row_scope(db, scope, workflow), scope
    return None, scope


def _get_workflow_portability_service(
    db=None, *, account_id: str = ""
) -> WorkflowPortabilityService:
    resolved_db = db or _get_db()
    connection_store = _platform_logger_for_admin_catalog(resolved_db)
    return WorkflowPortabilityService(
        resolved_db,
        project_connection_store=connection_store,
        # AC17-R67: the same integration store already built above owns the extension-install
        # lookup that resolved_db (RegistryDBStore) does not have. Callers that resolve
        # extension dependencies pass the request's account; the rest keep the old behaviour.
        extension_install_store=connection_store,
        account_id=account_id,
        import_transaction_factory=(
            partial(workflow_import_transaction, resolved_db, connection_store)
            if connection_store is not None
            else None
        ),
    )


def _raise_portability_http_error(exc: WorkflowPortabilityError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


def _assert_workflow_policy(db, workflow_id: str, action: str, *, operation: str) -> None:
    """Refuse *operation* on a protected workflow, the projected row deciding first."""
    service = _get_workflow_portability_service(db)
    projected_workflow = _resolve_workflow_row(db, workflow_id)
    try:
        if isinstance(projected_workflow, dict):
            check = getattr(service, f"assert_{operation}_allowed_for")
            check(projected_workflow, action=action)
            return
        getattr(service, f"assert_{operation}_allowed")(workflow_id, action=action)
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)


def _assert_workflow_structure_change_allowed(db, workflow_id: str, action: str) -> None:
    """A protected author graph is read-only: no node, edge, config or note changes."""
    _assert_workflow_policy(db, workflow_id, action, operation="structure_change")


def _assert_workflow_step_test_run_allowed(db, workflow_id: str, action: str) -> None:
    """Running author internals in isolation is refused, whatever reading allows."""
    _assert_workflow_policy(db, workflow_id, action, operation="step_test_run")


def _raise_workflow_backend_unavailable(*, workflow_id: str, action: str, exc: Exception) -> None:
    logger.warning(
        "Workflow backend unavailable action=%s workflow_id=%s: %s",
        action,
        workflow_id,
        exc,
    )
    raise HTTPException(
        status_code=503,
        detail={
            "error": "Workflow registry backend unavailable",
            "workflow_id": workflow_id,
            "action": action,
            "message": "Workflow registry backend is temporarily unavailable. Retry in a few seconds.",
        },
    )


def _reload_registry_after_workflow_mutation(*, workflow_id: str, action: str) -> list[dict]:
    try:
        _get_registry().reload_all()
    except DATABASE_OPERATION_ERRORS as exc:
        logger.warning(
            "Workflow registry reload deferred action=%s workflow_id=%s: %s",
            action,
            workflow_id,
            exc,
        )
        return [
            {
                "code": "workflow.registry_reload_deferred",
                "message": (
                    "Workflow was saved, but registry refresh is temporarily unavailable. "
                    "Retry in a few seconds."
                ),
            }
        ]
    return []


def _refresh_workflow_cache_after_commit() -> None:
    """Best-effort narrow cache refresh that cannot change a committed response."""
    try:
        refresh = getattr(_get_registry(), "refresh_workflow_cache", None)
        if callable(refresh):
            refresh()
    except Exception:
        logger.warning("Workflow cache refresh failed after committed mutation")


__all__ = [
    "_request_scope",
    "_viewer_can_manage_extensions",
    "_with_viewer_extension_permission",
    "_raise_scope_forbidden_http_error",
    "_enforce_workflow_scope",
    "_enforce_project_scope",
    "_resolve_scoped_project_id",
    "_enforce_import_resolution_scope",
    "_enforce_project_connection_scope",
    "_workflow_visible_to_scope",
    "_enforce_workflow_row_scope",
    "_assert_workflow_mutation_allowed",
    "_agent_visible_to_scope",
    "_enforce_extension_contract_scope",
    "_bind_workflow_to_project_if_requested",
    "_history_snapshot_payload",
    "_resolve_workflow_scope_target",
    "_resolve_active_workflow_scope_target",
    "_get_workflow_portability_service",
    "_raise_portability_http_error",
    "_assert_workflow_step_test_run_allowed",
    "_assert_workflow_structure_change_allowed",
    "_raise_workflow_backend_unavailable",
    "_reload_registry_after_workflow_mutation",
    "_refresh_workflow_cache_after_commit",
]
