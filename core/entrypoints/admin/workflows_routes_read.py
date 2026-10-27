# ruff: noqa: F403, F405, I001, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.entrypoints.admin.workflows_connector_catalog import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR
from core.application.management.errors import ServiceNotFoundError
from core.application.workflows.execution_graph import EXECUTION_GRAPH_SETTING
from core.application.workflows.workflow_hook_contract import is_hook_storage
from core.application.registry.boundary_contracts import (
    project_workflow_boundary_contracts,
)
from core.application.registry.workflow_lookup import is_projection_anchor


@router.get(
    "/{workflow_id}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow(workflow_id: str, request: Request):
    """Get a single workflow with its steps, groups and execution graph."""
    _request_scope(request)
    db = _get_db()
    wf, _scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    wf = _get_workflow_portability_service(db).redact_protected_workflow(dict(wf))
    settings = wf.get("settings")
    if isinstance(settings, dict):
        wf["participants"] = _normalize_workflow_participants(settings.get("participants"))
    return _with_viewer_extension_permission(project_workflow_boundary_contracts(wf), request)


@router.get(
    "/{workflow_id}/export-policy",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_export_policy(workflow_id: str, request: Request):
    """Return backend-owned workflow export, clone, and source visibility policy."""
    _request_scope(request)
    db = _get_db()
    workflow, _scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    return _with_viewer_extension_permission(
        _get_workflow_portability_service(db).export_policy_for_workflow(dict(workflow)),
        request,
    )


@router.get(
    "/{workflow_id}/export.yaml",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def export_workflow_yaml(workflow_id: str, request: Request, include_children: bool = False):
    """Export a workflow YAML bundle or protected extension reference.

    ``include_children`` adds every workflow it invokes, so the file is a complete
    template; that is how the workflow catalog takes a parent and its children.
    """
    _request_scope(request)
    db = _get_db()
    workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    # AC17-R60 names an extension dependency by its portable `extension_id`, but it
    # resolves the install only inside an account. Without one the lookup is skipped
    # and the package carried the exporting machine's install UUID instead. The
    # installs a workflow uses belong to the workflow's own account.
    account_id = str(workflow.get("account_id") or scope.account_id or "").strip()
    try:
        body = _get_workflow_portability_service(db, account_id=account_id).export_yaml(
            workflow_id, include_children=include_children
        )
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    return Response(
        content=body,
        media_type="application/x-yaml",
        headers={
            "Content-Disposition": f'attachment; filename="{workflow_id}.workflow.yaml"',
        },
    )


@router.get(
    "/{workflow_id}/dag",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_dag(workflow_id: str, request: Request):
    """Return the workflow's steps and real groups with its execution-graph edges."""
    _request_scope(request)
    db = _get_db()
    wf, _scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    portability = _get_workflow_portability_service(db)
    policy = portability.export_policy_for_workflow(dict(wf))
    if policy.get("protected"):
        # A protected author graph is read-only, never hidden: its real
        # structure with sensitive configuration redacted.
        wf = portability.redact_protected_workflow(dict(wf))

    nodes = [_group_dag_node(group) for group in wf.get("groups") or [] if isinstance(group, dict)]
    nodes.extend(
        _for_each_dag_node(item) for item in wf.get("for_each") or [] if isinstance(item, dict)
    )
    nodes.extend(_step_dag_node(step) for step in wf.get("steps") or [] if isinstance(step, dict))
    settings = wf.get("settings") if isinstance(wf.get("settings"), dict) else {}
    graph = settings.get(EXECUTION_GRAPH_SETTING) if isinstance(settings, dict) else None
    raw_edges = graph.get("edges") if isinstance(graph, dict) else None
    edges = [
        _dag_edge(index, edge)
        for index, edge in enumerate(raw_edges if isinstance(raw_edges, list) else [])
        if isinstance(edge, dict)
    ]

    payload = {
        "workflow_id": wf["workflow_id"],
        "display_name": wf.get("display_name") or wf["workflow_id"],
        "nodes": nodes,
        "edges": edges,
    }
    if policy.get("protected"):
        payload.update(protected_workflow=True, read_only=True, portability=policy)
    return payload


@router.get(
    "/{workflow_id}/canvas-graph",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_canvas_graph(workflow_id: str, request: Request):
    """Return a step-level workflow canvas graph for the production builder."""
    scope = _request_scope(request)
    db = _get_db()
    wf, _scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    portability = _get_workflow_portability_service(db)
    policy = portability.export_policy_for_workflow(dict(wf))
    protected = bool(policy.get("protected"))
    try:
        if protected:
            # A protected author graph is read from its redacted copy.
            canvas_db = _WorkflowValidationDBView(
                db,
                workflow_id,
                portability.redact_protected_workflow(dict(wf)),
                account_id=scope.account_id,
            )
        elif isinstance(platform_row := db.get_workflow(workflow_id), dict) and not (
            is_projection_anchor(platform_row)
        ):
            # An installed extension's anchor row carries metadata only; its
            # definition, interface included, is the resolved projection (B21).
            canvas_db = db
        else:
            canvas_db = _WorkflowValidationDBView(
                db, workflow_id, dict(wf), account_id=scope.account_id
            )
        graph = build_workflow_canvas_graph(canvas_db, workflow_id)
        if not policy.get("structure_change_allowed"):
            graph.update(read_only=True, portability=policy)
        if protected:
            graph["protected_workflow"] = True
        return graph
    except LookupError as exc:
        if _normalize_text(wf.get("source_install_id")):
            reason = _normalize_text(policy.get("availability_reason_text") or policy.get("reason"))
            return _placeholder_workflow_canvas_graph(dict(wf), reason=reason)
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/{workflow_id}/contract",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_contract(workflow_id: str, request: Request):
    """Return the authoring contract for a workflow the caller may edit.

    This is the editor's read, not an execution path: the availability gate is
    deliberately not applied, because an author opening the builder for their
    own disabled draft is not asking to run it. Running and operator paths keep
    the gate. Scope is still enforced above, and existence below.
    """
    _request_scope(request)
    db = _get_db()
    wf, _scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    try:
        payload = (
            _workflow_operator_service(db=db)
            .workflow_contract(workflow_id, require_available=False)
            .dump()
        )
    except ServiceNotFoundError as exc:
        # Expected outcomes are mapped here rather than escaping as an ASGI
        # traceback: an unmapped one reached the builder as HTTP 500, twice per
        # page load, for a workflow the author is entitled to edit.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    interface = WorkflowInterfaceService().interface_from_definition(wf)
    payload["definition_issues"] = interface.definition_issues()
    return payload


@router.get(
    "/{workflow_id}/contract-validation",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_contract_validation(workflow_id: str, request: Request):
    """Validate workflow readiness for enable/test/invoke operations."""
    _request_scope(request)
    db = _get_db()
    workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    payload = _workflow_validation_payload_for_admin(
        db,
        workflow_id,
        account_id=scope.account_id,
    )
    return {"workflow_id": workflow_id, "validation": payload.get("validation", {})}


@router.post(
    "/{workflow_id}/validate",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def validate_workflow(workflow_id: str, request: Request):
    """Validate a workflow's contract — returns a list of validation issues.

    Alias for contract-validation that returns the issues array directly so
    UI and E2E tests can consume a stable ``[{type, message, ...}]`` shape.
    """
    _request_scope(request)
    db = _get_db()
    workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    payload = _workflow_validation_payload_for_admin(
        db,
        workflow_id,
        account_id=scope.account_id,
    )
    return {"workflow_id": workflow_id, "issues": payload.get("issues", [])}


@router.get(
    "/{workflow_id}/suggestions",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_suggestions(workflow_id: str, request: Request):
    """Return auto-repair suggestions for a workflow's contract issues.

    Derives suggestions from the contract-validation result.  Each suggestion
    describes an action that would resolve a detected issue.
    """
    _request_scope(request)
    db = _get_db()
    workflow, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    # Suggestions are edits; a protected author graph is read-only.
    _assert_workflow_structure_change_allowed(db, workflow_id, "workflow_suggestions")
    payload = _workflow_suggestions_payload_for_admin(
        db,
        workflow_id,
        account_id=scope.account_id,
    )
    return {"workflow_id": workflow_id, "suggestions": payload.get("suggestions", [])}


@router.get(
    "/meta/callable-workflows",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def list_callable_workflows(
    request: Request,
    enabled_only: Annotated[bool, Query()] = True,
    exclude_workflow_id: Annotated[str | None, Query()] = None,
):
    """List callable workflows for invoke_workflow step authoring."""
    scope = _request_scope(request)
    db = _get_db()
    # Definitions included: the files a child returns are its steps' settings.
    rows = db.list_workflows(include_definitions=True) or []
    excluded = str(exclude_workflow_id or "").strip()
    service = WorkflowInterfaceService()
    workflows: list[dict[str, Any]] = []
    for row in rows:
        item = _callable_workflow_row(row, scope, excluded, enabled_only, service)
        if item is not None:
            workflows.append(item)
    workflows.sort(
        key=lambda item: (str(item.get("display_name") or "").lower(), item["workflow_id"])
    )
    return {"workflows": workflows, "count": len(workflows)}


@router.get(
    "/meta/connector-catalog",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def list_connector_catalog(
    request: Request,
    project_id: Annotated[str, Query()] = "",
    locale: Annotated[str, Query(description="UI language for readable text")] = "",
):
    """Return connector rows with project-scoped connection hints for step authoring."""
    from core.application.settings.ui_i18n import resolve_ui_locale

    # resolve_ui_locale reads ?locale= first, then the user's profile language.
    _ = locale
    scope = _request_scope(request)
    db = _get_db()
    normalized_project_id = _enforce_project_scope(db, scope, project_id)
    catalog_scope = (
        normalize_scope(scope, project_id=normalized_project_id) if normalized_project_id else scope
    )
    connectors = _list_connector_catalog_rows(db, scope=catalog_scope)
    project_connections = _list_project_connections_for_catalog(
        db, project_id=normalized_project_id
    )
    registry_operation_index = _registry_tool_operation_index(db)
    ui_locale = resolve_ui_locale(request)

    normalized_connections = [
        _catalog_connection(row) for row in project_connections if isinstance(row, dict)
    ]

    catalog_rows: list[dict[str, Any]] = []
    grouped_mcp_tool_ids: set[str] = set()
    for connector in connectors:
        item = _catalog_connector(
            connector, normalized_connections, registry_operation_index, grouped_mcp_tool_ids
        )
        if item is not None:
            catalog_rows.append(item)
    seen_connector_ids = {
        str(item.get("connector_id") or "").strip()
        for item in catalog_rows
        if isinstance(item, dict)
    }
    _append_catalog_connectors(
        catalog_rows,
        _registry_tool_connector_rows(db, locale=ui_locale),
        seen_connector_ids,
        excluded_tool_ids=grouped_mcp_tool_ids,
    )
    _append_catalog_connectors(
        catalog_rows,
        _extension_action_connector_rows(
            db,
            project_id=normalized_project_id,
            scope=scope,
            locale=ui_locale,
        ),
        seen_connector_ids,
    )
    catalog_rows.sort(
        key=lambda item: (str(item.get("display_name") or "").lower(), item["connector_id"])
    )
    return {
        "connectors": catalog_rows,
        "count": len(catalog_rows),
        "project_id": normalized_project_id,
    }


__all__ = [
    "get_workflow",
    "get_workflow_export_policy",
    "export_workflow_yaml",
    "get_workflow_dag",
    "get_workflow_canvas_graph",
    "get_workflow_contract",
    "get_workflow_contract_validation",
    "validate_workflow",
    "get_workflow_suggestions",
    "list_callable_workflows",
    "list_connector_catalog",
]


def _dag_ref_id(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    if ref.get("kind") == "group_exit":
        return f"group:{ref.get('group_id')}:exit"
    if ref.get("kind") == "for_each_exit":
        return f"for_each:{ref.get('for_each_id')}:exit"
    return f"{ref.get('kind')}:{ref.get('id')}"


def _group_dag_node(group: dict[str, Any]) -> dict[str, Any]:
    group_id = str(group.get("group_id") or "").strip()
    raw_body = group.get("body")
    body = raw_body if isinstance(raw_body, dict) else {}
    return {
        "id": f"group:{group_id}",
        "type": "group",
        "group_id": group_id,
        "body_kind": str(body.get("kind") or "").strip(),
        "display_name": group.get("display_name") or group_id,
    }


def _for_each_dag_node(item: dict[str, Any]) -> dict[str, Any]:
    for_each_id = str(item.get("for_each_id") or "").strip()
    return {
        "id": f"for_each:{for_each_id}",
        "type": "for_each",
        "for_each_id": for_each_id,
        "group_id": str(item.get("group_id") or "").strip() or None,
        "display_name": item.get("display_name") or for_each_id,
        "over": str(item.get("over") or "").strip(),
        "item_name": str(item.get("item_name") or "").strip(),
        "index_name": str(item.get("index_name") or "").strip() or None,
    }


def _step_dag_node(step: dict[str, Any]) -> dict[str, Any]:
    step_id = str(step.get("step_id") or "").strip()
    return {
        "id": f"step:{step_id}",
        "type": "step",
        "step_id": step_id,
        "step_kind": step.get("step_kind", "agent"),
        "display_name": step.get("display_name") or step_id,
        "group_id": step.get("group_id"),
        "lane_id": step.get("lane_id"),
        "participant_key": step.get("participant_key", ""),
        "agent_id": step.get("agent_id", ""),
        "optional": bool(step.get("optional")),
        "disabled": bool(step.get("disabled")),
        "sort_order": step.get("sort_order", 0),
    }


def _dag_edge(index: int, edge: dict[str, Any]) -> dict[str, Any]:
    raw_outcome = edge.get("outcome")
    outcome: dict[str, Any] = raw_outcome if isinstance(raw_outcome, dict) else {}
    name = str(outcome.get("name") or "")
    label = outcome.get("case_id") or (None if name == "success" else name)
    if edge.get("rework"):
        edge_type = "rework"
    elif name == "error":
        edge_type = "failure"
    else:
        edge_type = "sequential"
    return {
        "id": f"edge:{index}:{_dag_ref_id(edge.get('source'))}",
        "source": _dag_ref_id(edge.get("source")),
        "target": _dag_ref_id(edge.get("target")),
        "type": edge_type,
        "outcome": outcome,
        "label": label,
    }


def _callable_workflow_row(
    row: Any, scope: Any, excluded: str, enabled_only: bool, service: WorkflowInterfaceService
) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    workflow_id = str(row.get("workflow_id") or "").strip()
    if not workflow_id:
        return None
    if not _workflow_visible_to_scope(scope, dict(row)):
        return None
    # A hook's steps are a place in another workflow, never an Invoke target (R2-11).
    if is_hook_storage(row):
        return None
    if excluded and workflow_id == excluded:
        return None
    if enabled_only and not bool(row.get("enabled", False)):
        return None
    interface = service.interface_from_definition(row)
    if not bool(interface.enabled):
        return None
    if "parent_workflow" not in set(interface.trigger_channels):
        return None
    contract_payload = service.public_contract_payload(interface)
    return _callable_workflow_payload(row, workflow_id, contract_payload)


def _callable_workflow_payload(
    row: dict[str, Any], workflow_id: str, contract_payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "display_name": str(row.get("display_name") or workflow_id).strip() or workflow_id,
        "description": str(row.get("description") or "").strip(),
        "enabled": bool(row.get("enabled", False)),
        "trigger_channels": contract_payload.get("trigger_channels") or [],
        "input_mode": contract_payload.get("input_mode") or "fields",
        "strict_inputs": bool(contract_payload.get("strict_inputs", True)),
        "output_mode": contract_payload.get("output_mode") or "none",
        "expected_inputs": _normalize_contract_rows(contract_payload.get("expected_inputs")),
        "expected_outputs": _normalize_contract_rows(contract_payload.get("expected_outputs")),
        "returned_files": contract_payload.get("returned_files") or [],
        "input_schema": contract_payload.get("input_schema") or {},
        "output_schema": contract_payload.get("output_schema") or {},
    }


def _catalog_connection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: str(row.get(key) or "").strip()
            for key in (
                "connection_id",
                "display_name",
                "extension_id",
                "extension_install_id",
                "connection_kind",
                "connection_type",
                "environment",
                "runtime_mode",
                "health_status",
                "runtime_status",
                "last_error",
            )
        },
        "credential_revision": row.get("credential_revision") or 1,
        "enabled": bool(row.get("enabled", True)),
        "connection_config": _json_object(row.get("connection_config")),
        "connection_routes": [
            dict(item) for item in (row.get("connection_routes") or []) if isinstance(item, dict)
        ],
        "tags": [
            str(item or "").strip() for item in (row.get("tags") or []) if str(item or "").strip()
        ],
        "_refs": _connection_ref_set(row),
    }


def _catalog_connector(
    connector: Any,
    normalized_connections: list[dict[str, Any]],
    registry_operation_index: dict[str, dict[str, Any]],
    grouped_mcp_tool_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(connector, dict):
        return None
    connector_id = str(connector.get("connector_id") or "").strip()
    if not connector_id:
        return None
    connector_refs = _connector_ref_set(connector)
    scoped_connections = _connector_scoped_connections(connector_refs, normalized_connections)
    operations = _connector_operations(connector, registry_operation_index, grouped_mcp_tool_ids)
    return {
        "connector_id": connector_id,
        "display_name": str(connector.get("display_name") or connector_id).strip() or connector_id,
        "description": str(connector.get("description") or "").strip(),
        "namespace_prefix": str(connector.get("namespace_prefix") or "").strip(),
        "enabled": bool(connector.get("enabled", True)),
        "status": str(connector.get("status") or "").strip(),
        "connector_install_id": "",
        "source": "mcp_connector",
        "catalog_item_id": str(connector.get("catalog_item_id") or "").strip(),
        "operations": operations,
        "connections": scoped_connections,
    }


def _connector_scoped_connections(
    connector_refs: set[str], normalized_connections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    scoped_connections: list[dict[str, Any]] = []
    for connection in normalized_connections:
        connection_refs = connection.get("_refs")
        if not isinstance(connection_refs, set):
            continue
        if not _connection_matches_connector(connector_refs, connection_refs):
            continue
        scoped_connections.append(
            {key: value for key, value in connection.items() if key != "_refs"}
        )
    return scoped_connections


def _append_catalog_connectors(
    catalog_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    seen_connector_ids: set[str],
    *,
    excluded_tool_ids: set[str] | None = None,
) -> None:
    for connector in candidates:
        connector_id = str(connector.get("connector_id") or "").strip()
        if excluded_tool_ids is not None:
            tool_id = str(connector.get("tool_id") or "").strip()
            if tool_id in excluded_tool_ids:
                continue
        if connector_id and connector_id not in seen_connector_ids:
            catalog_rows.append(connector)
            seen_connector_ids.add(connector_id)
