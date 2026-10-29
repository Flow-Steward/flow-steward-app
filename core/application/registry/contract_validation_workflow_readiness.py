# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from __future__ import annotations


from core.application.orchestration.invoke_workflow_step_node_common import (
    _resolve_invoke_workflow_max_depth,
)
from core.application.workflows.workflow_definition_service import definition_from_workflow
from core.application.workflows.workflow_hook_contract import is_hook_storage
from core.application.workflows.workflow_definition_validation import (
    ChildDefinition,
    validate_workflow_definition,
)
from core.application.workflows.workflow_step_config_contracts import (
    validate_step_config_keys,
)
from core.application.workflows.extension_connection_validation import (
    validate_extension_connector_step,
)
from core.application.registry.active_workflow_dependencies import workflow_registry_tool_issues
from core.application.orchestration.state_path_guards import is_reserved_state_target
from core.application.orchestration.workflow_state_selector_contract import (
    validate_workflow_state_selectors,
    discover_step_state_selectors,
)
from core.application.registry.data_connection_binding import (
    connection_extension_binding_issue,
    resolve_project_scoped_data_connection,
    runtime_extension_resolver,
)

from .contract_validation_entities import *  # noqa: F403,F401  # NOSONAR
from .state_selector_shape import StateSelectorShape
from .workflow_lookup import get_registry_artifact_row, get_workflow_row
from core.application.workflows.workflow_step_kinds import STEP_KIND_APPROVAL, STEP_KIND_WAIT


def _workflow_project_scopes(db, workflow_id: str) -> list[dict[str, str]]:
    list_bindings = getattr(db, "list_project_workflows", None)
    if not callable(list_bindings):
        return []
    try:
        rows = list_bindings(enabled_only=True)
    except TypeError:
        rows = list_bindings()
    get_project = getattr(db, "get_project", None)
    get_project_scope = getattr(db, "_get_project_scope", None)
    scopes: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows or []:
        binding = row if isinstance(row, dict) else {}
        if str(binding.get("workflow_id") or "").strip() != workflow_id:
            continue
        project_id = str(binding.get("project_id") or "").strip()
        if not project_id or project_id in seen:
            continue
        project = get_project(project_id) if callable(get_project) else None
        if not isinstance(project, dict) and callable(get_project_scope):
            project = get_project_scope(project_id)
        if not isinstance(project, dict):
            continue
        scopes.append(
            {
                "project_id": project_id,
                "account_id": str(project.get("account_id") or "").strip(),
                "tenant_id": str(project.get("tenant_id") or "").strip(),
                "workspace_id": str(project.get("workspace_id") or "").strip(),
            }
        )
        seen.add(project_id)
    return scopes


def _dataset_query_connection_binding_issue(db, workflow_id: str, template: dict) -> str:
    """Report a saved query whose data connection cannot run it.

    Core keeps abstract connections and dispatches one universal extension action;
    a connection with no Data Connector extension binding has no driver behind it.
    Resolving that here means the operator is told at authoring time instead of
    losing a job to `connector_runtime_unavailable` mid-run.
    """
    dataset_id = str(template.get("dataset_id") or "").strip()
    if not dataset_id:
        return ""
    dataset_getter = getattr(db, "get_dataset", None)
    dataset = dataset_getter(dataset_id) if callable(dataset_getter) else None
    if not isinstance(dataset, dict):
        return ""
    connection_id = str(dataset.get("connection_id") or "").strip()
    if not connection_id:
        metadata = dataset.get("metadata_jsonb")
        metadata = metadata if isinstance(metadata, dict) else {}
        connection_id = str(metadata.get("connection_id") or "").strip()
    if not connection_id:
        metadata = dataset.get("metadata_jsonb")
        metadata = metadata if isinstance(metadata, dict) else {}
        if str(metadata.get("connection_resolution") or "").strip() != "project_scope":
            return (
                "Dataset has no Data Connection binding. Rebind it to an enabled Data "
                "Connector extension-backed connection."
            )
        for scope in _workflow_project_scopes(db, workflow_id):
            connection, resolution_error = resolve_project_scoped_data_connection(
                db, dataset, scope
            )
            if not connection:
                return (
                    f"Dataset data connection is {resolution_error.replace('_', ' ')} for "
                    f"project '{scope['project_id']}'. Bind one enabled Data Connector "
                    "extension-backed connection in that project workspace."
                )
            issue = connection_extension_binding_issue(
                connection,
                resolve_extension=runtime_extension_resolver(db),
                require_healthy=True,
            )
            if issue:
                return issue
        return ""
    connection_getter = getattr(db, "get_workspace_data_connection", None)
    connection = connection_getter(connection_id) if callable(connection_getter) else None
    if not isinstance(connection, dict):
        return ""
    return connection_extension_binding_issue(
        connection,
        resolve_extension=runtime_extension_resolver(db),
        require_healthy=True,
    )


def _for_each_name_issues(item: dict, for_each_id: str, path: str) -> list[dict]:
    """Item and index names must not shadow a reserved runtime state field.

    The definition validator checks their shape; the reserved roots are a
    runtime contract, so readiness refuses them before a run publishes into one.
    """
    issues: list[dict] = []
    for field in ("item_name", "index_name"):
        name = str(item.get(field) or "").strip()
        if name and is_reserved_state_target(name):
            issues.append(
                _issue(
                    code="workflow.for_each_name_reserved",
                    message=(
                        f"For each '{for_each_id}' {field} '{name}' is a reserved runtime "
                        "state field and cannot hold the current item"
                    ),
                    path=f"{path}.{field}",
                )
            )
    return issues


def _approval_response_mapping_issues(step: dict, step_path: str) -> list[dict]:
    """Reject approval response mappings that target host-owned runtime state."""
    override = step.get("override")
    config = override.get("approval") if isinstance(override, dict) else None
    response_mapping = config.get("response_mapping") if isinstance(config, dict) else None
    if not isinstance(response_mapping, dict):
        return []
    raw_targets = (
        response_mapping.get("override_content_targets")
        or response_mapping.get("override_content")
        or response_mapping.get("writeback")
    )
    if isinstance(raw_targets, str):
        targets = [raw_targets]
    elif isinstance(raw_targets, list):
        targets = raw_targets
    else:
        return []
    return [
        _issue(
            code="workflow.approval_response_target_reserved",
            message="approval response mapping cannot write security-sensitive runtime state",
            path=(
                f"{step_path}.override.approval.response_mapping.override_content_targets[{index}]"
            ),
        )
        for index, target in enumerate(targets)
        if is_reserved_state_target(target)
    ]


def _output_names(outputs) -> set[str]:
    return {
        str(item.get("name") or "").strip()
        for item in outputs or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }


def _boundary_contract_issues(
    contract, *, owner_label: str, owner_path: str, produced: set[str]
) -> list[dict]:
    """Entry rows keep the expected-input format; exit keys must be produced.

    This is the check a phase's expected_inputs/required_outputs used to carry,
    now attached to the step or real group where the boundary actually is.
    """
    if not isinstance(contract, dict):
        return []
    issues: list[dict] = []
    _, entry_issues = validate_contract_items(
        contract.get("entry"),
        path_prefix=f"{owner_path}.boundary_contract.entry",
        default_required=False,
        allow_source=True,
    )
    issues.extend(entry_issues)
    exit_outputs = [
        _normalize_name(item) for item in (contract.get("exit") or []) if _normalize_name(item)
    ]
    if len(exit_outputs) != len(set(exit_outputs)):
        issues.append(
            _issue(
                code="workflow.boundary_exit_duplicates",
                message=f"{owner_label} has duplicate boundary exit outputs",
                path=f"{owner_path}.boundary_contract.exit",
            )
        )
    for name in exit_outputs:
        if name not in produced:
            issues.append(
                _issue(
                    code="workflow.boundary_exit_not_produced",
                    message=f"{owner_label} requires output '{name}' that no step produces",
                    path=f"{owner_path}.boundary_contract.exit",
                )
            )
    return issues


def _workflow_issue_path(path) -> str:
    text = str(path or "").strip()
    return f"workflow.{text}" if text else "workflow"


def _child_definition_loader(db):
    """Child definitions at the revision a run would launch, for the lane check."""

    def load(workflow_id: str) -> ChildDefinition | None:
        row = get_workflow_row(db, workflow_id)
        if not isinstance(row, dict):
            return None
        return ChildDefinition(
            workflow_id=workflow_id,
            revision=int(row.get("revision") or 0),
            definition=definition_from_workflow(row),
        )

    return load


def _coerce_bool_flag(value) -> bool:
    """Truthy coercion tolerant of JSON booleans and stringified flags from settings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_workflow_readiness(db, workflow_id: str) -> dict:
    wf = get_workflow_row(db, workflow_id)
    if not wf:
        return {
            "valid": False,
            "errors": [
                _issue(code="workflow.not_found", message=f"Workflow '{workflow_id}' not found")
            ],
            "warnings": [],
        }

    errors: list[dict] = []
    warnings: list[dict] = []
    errors.extend(validate_workflow_state_selectors(wf))
    workflow_interface = _workflow_interface_from_definition(wf)
    callable_enabled = bool(workflow_interface.enabled)
    callable_inputs_raw = [row.dump() for row in workflow_interface.expected_inputs]
    callable_outputs_raw = [row.dump() for row in workflow_interface.expected_outputs]
    callable_artifacts_raw = [row.dump() for row in workflow_interface.returned_files]
    callable_inputs: list[dict] = []
    callable_outputs: list[dict] = []
    if not callable_enabled and (
        any(
            row.get("name")
            for row in callable_inputs_raw + callable_outputs_raw + callable_artifacts_raw
        )
        or (workflow_interface.input_mode == "json_schema" and workflow_interface.input_schema)
    ):
        warnings.append(
            _issue(
                code="workflow.callable.inactive",
                message=(
                    "Workflow interface is off. Any declared outputs are not returned and "
                    "required-output checks are inactive until it is enabled. "
                    "Input forms may still validate declared inputs."
                ),
                path="workflow.callable.enabled",
                severity="warning",
            )
        )
    if callable_enabled:
        _append_issues_by_severity(
            issues=workflow_interface.definition_issues(),
            errors=errors,
            warnings=warnings,
        )
        callable_inputs, callable_input_issues = validate_contract_items(
            callable_inputs_raw,
            path_prefix=WORKFLOW_CALLABLE_EXPECTED_INPUTS_VALUE,
            default_required=False,
            allow_secret_input=True,
        )
        callable_outputs, callable_output_issues = validate_contract_items(
            callable_outputs_raw,
            path_prefix=PATH_WORKFLOW_EXPECTED_OUTPUTS,
            default_required=True,
        )
        _, callable_artifact_issues = validate_contract_items(
            callable_artifacts_raw,
            path_prefix=PATH_WORKFLOW_RETURNED_FILES,
            default_required=True,
        )
        errors.extend(callable_input_issues)
        errors.extend(callable_output_issues)
        errors.extend(callable_artifact_issues)
        settings = wf.get("settings") if isinstance(wf.get("settings"), dict) else {}
        callable_settings = (
            settings.get("callable") if isinstance(settings.get("callable"), dict) else {}
        )
        # A callable workflow may legitimately take no inputs (for example a demo that
        # invokes a child workflow, runs a fixed dataset query, or intentionally fails).
        # Authors can acknowledge this explicitly via settings.callable.allow_empty_inputs
        # to opt out of the advisory warning without fabricating unused input fields.
        allow_empty_inputs = _coerce_bool_flag(callable_settings.get("allow_empty_inputs"))
        if not callable_inputs and not allow_empty_inputs:
            warnings.append(
                _issue(
                    code="workflow.callable.inputs_empty",
                    message=(
                        "Callable workflow has no declared inputs "
                        "(settings.callable.expected_inputs)"
                    ),
                    path=WORKFLOW_CALLABLE_EXPECTED_INPUTS_VALUE,
                    severity="warning",
                )
            )

    raw_steps = wf.get("steps")
    raw_steps = raw_steps if isinstance(raw_steps, list) else []
    for step_idx, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            errors.append(
                _issue(
                    code="workflow.step_invalid_type",
                    message=(
                        f"Workflow step at index {step_idx} must be an object, "
                        f"got {type(step).__name__}"
                    ),
                    path=f"{WORKFLOW_STEPS_VALUE}[{step_idx}]",
                )
            )
    steps = _iter_workflow_steps(wf)
    if not steps:
        errors.append(
            _issue(
                code="workflow.steps_missing",
                message="Workflow must define at least one step before enabling/running",
                path=WORKFLOW_STEPS_VALUE,
            )
        )
        return {"valid": False, "errors": errors, "warnings": warnings}

    # Entry, ports, scopes, reachability, rework, placement and graph-owned
    # configs are judged once, on the whole definition.
    errors.extend(
        {**issue, "path": _workflow_issue_path(issue.get("path"))}
        for issue in validate_workflow_definition(
            definition_from_workflow(wf),
            load_child=_child_definition_loader(db),
            max_invoke_depth=_resolve_invoke_workflow_max_depth(),
        )
    )

    success_terminal_count, fail_terminal_count = _workflow_terminal_step_counts(steps)
    if _workflow_is_expected_failure(wf):
        if fail_terminal_count <= 0:
            errors.append(
                _issue(
                    code="workflow.terminal_fail_missing",
                    message=(
                        "Expected-failure workflow must include an explicit stop_fail "
                        "terminal step before it can be enabled or run"
                    ),
                    path=WORKFLOW_STEPS_VALUE,
                )
            )
    elif fail_terminal_count > 0 and success_terminal_count <= 0:
        errors.append(
            _issue(
                code="workflow.terminal_fail_intent_missing",
                message=(
                    "A workflow whose only terminal is stop_fail must declare explicit "
                    "expected-failure intent in settings.traits.expected_failure or tags"
                ),
                path="workflow.settings.traits.expected_failure",
            )
        )
    elif success_terminal_count <= 0:
        errors.append(
            _issue(
                code="workflow.terminal_success_missing",
                message=(
                    "Workflow must include an explicit stop_success terminal step before "
                    "it can be enabled or run"
                ),
                path=WORKFLOW_STEPS_VALUE,
            )
        )

    resolver = IOResolver(db)
    selector_shape = StateSelectorShape(resolver, wf)
    participant_bindings = _workflow_participant_bindings(wf)
    real_execution_step_count = 0
    if workflow_interface.variable_outputs_required() and callable_outputs:
        errors.extend(
            _validate_callable_outputs_produced(
                db=db,
                workflow=wf,
                callable_outputs=callable_outputs,
                path_prefix=PATH_WORKFLOW_EXPECTED_OUTPUTS,
                code_prefix="workflow.callable",
                warnings=warnings,
            )
        )
    for group in _iter_workflow_groups(wf):
        group_id = str(group.get("group_id") or "").strip()
        group_path = f"workflow.groups.{group_id}"
        group_outputs: set[str] = set()
        for member in steps:
            if str(member.get("group_id") or "").strip() == group_id:
                group_outputs.update(
                    _output_names(resolver.get_step_outputs(workflow_id, member["step_id"]))
                )
        errors.extend(
            _boundary_contract_issues(
                group.get("boundary_contract"),
                owner_label=f"Group '{group_id}'",
                owner_path=group_path,
                produced=group_outputs,
            )
        )

    for item in wf.get("for_each") or []:
        if isinstance(item, dict):
            for_each_id = str(item.get("for_each_id") or "").strip()
            errors.extend(
                _for_each_name_issues(item, for_each_id, f"workflow.for_each.{for_each_id}")
            )

    for step in steps:
        step_id = str(step.get("step_id") or "").strip()
        step_kind = _step_kind(step)
        if step_kind in REAL_EXECUTION_STEP_KINDS:
            real_execution_step_count += 1
        explicit_agent_id = str(step.get("agent_id") or step.get("agent") or "").strip()
        participant_key = str(step.get("participant_key") or "").strip()
        bound_agent_id = (
            str(participant_bindings.get(participant_key) or "").strip() if participant_key else ""
        )
        agent_id = bound_agent_id or explicit_agent_id
        cap_id = str(step.get("skill_id") or step.get("skill") or "").strip()
        step_path = f"{WORKFLOW_STEPS_VALUE}.{step_id}"
        errors.extend(
            _boundary_contract_issues(
                step.get("boundary_contract"),
                owner_label=f"Step '{step_id}'",
                owner_path=step_path,
                produced=_output_names(resolver.get_step_outputs(workflow_id, step_id))
                | _output_names(resolver.get_available_outputs_for_step(workflow_id, step_id)),
            )
        )
        errors.extend(_approval_response_mapping_issues(step, step_path))
        if step_kind in {"template", "transform"}:
            for reference in discover_step_state_selectors(step, step_path=step_path):
                problem = selector_shape.problem(step_id, reference.selector)
                if problem:
                    errors.append(
                        {
                            **_issue(
                                code="workflow.state_selector_shape_invalid",
                                message=problem,
                                path=reference.path,
                            ),
                            "selector": reference.selector,
                        }
                    )

        invoke_validation = validate_invoke_workflow_step(
            db,
            workflow_id=workflow_id,
            step=step,
            path_prefix=step_path,
        )
        errors.extend(invoke_validation.get("errors") or [])
        warnings.extend(invoke_validation.get("warnings") or [])
        errors.extend(
            _validate_step_published_block_binding(
                db,
                step=step,
                step_path=step_path,
                agent_id=agent_id,
                skill_id=cap_id,
            )
        )
        if step_kind not in SUPPORTED_STEP_KINDS:
            errors.append(
                _issue(
                    code="workflow.step_kind_unsupported",
                    message=(f"Step '{step_id}' has unsupported step_kind '{step_kind}'"),
                    path=f"{step_path}.step_kind",
                )
            )
            continue

        errors.extend(
            validate_step_config_keys(
                step_kind=step_kind,
                override=step.get("override"),
                step_path=step_path,
            )
        )
        errors.extend(_validate_step_state_write_targets(step, step_path))

        if step_kind == "connector":
            connector_id = str(step.get("connector_id") or "").strip()
            if not connector_id:
                errors.append(
                    _issue(
                        code="workflow.step_connector_missing",
                        message=(f"Step '{step_id}' is missing connector_id"),
                        path=f"{step_path}.connector_id",
                    )
                )
            elif _is_raw_mcp_connector_id(connector_id):
                errors.append(
                    _issue(
                        code="workflow.step_connector_raw_mcp_unsupported",
                        message=(
                            f"Step '{step_id}' uses raw MCP "
                            f"connector_id '{connector_id}'. Bind the projected registry "
                            "tool id (for example 'connector.<tool_id>') instead."
                        ),
                        path=f"{step_path}.connector_id",
                    )
                )
            errors.extend(
                validate_extension_connector_step(
                    db,
                    step=step,
                    step_path=step_path,
                    account_id=str(wf.get("account_id") or "").strip(),
                )
            )
            continue

        if step_kind == "invoke_workflow":
            continue

        if step_kind == STEP_KIND_DATASET_QUERY:
            override = step.get("override")
            override = override if isinstance(override, dict) else {}
            config = override.get(STEP_KIND_DATASET_QUERY)
            config = config if isinstance(config, dict) else {}
            query_template_id = str(
                config.get("query_template_id") or step.get("query_template_id") or ""
            ).strip()
            if not query_template_id:
                errors.append(
                    _issue(
                        code="workflow.step_dataset_query_template_missing",
                        message=(
                            f"Step '{step_id}' is missing override.dataset_query.query_template_id"
                        ),
                        path=f"{step_path}.override.dataset_query.query_template_id",
                    )
                )
                continue
            from core.application.registry.dataset_query_limits import (
                InvalidDatasetQueryLimitError,
                resolve_dataset_query_row_limit,
            )

            try:
                resolve_dataset_query_row_limit(
                    config.get("row_limit") if "row_limit" in config else step.get("row_limit")
                )
            except InvalidDatasetQueryLimitError as exc:
                errors.append(
                    _issue(
                        code="workflow.step_dataset_query_row_limit_invalid",
                        message=(f"Step '{step_id}' row_limit is not usable: {exc}"),
                        path=f"{step_path}.override.dataset_query.row_limit",
                    )
                )
            template_getter = getattr(db, "get_dataset_query_template", None)
            template = template_getter(query_template_id) if callable(template_getter) else None
            if not isinstance(template, dict):
                continue
            params_schema = _to_object(
                template.get("params_schema_jsonb") or template.get("param_schema_jsonb")
            )
            properties = _to_object(params_schema.get("properties"))
            required_params = {
                str(name).strip()
                for name in (params_schema.get("required") or [])
                if str(name or "").strip()
            }
            binding_issue = _dataset_query_connection_binding_issue(
                db, str(wf.get("workflow_id") or workflow_id).strip(), template
            )
            if binding_issue:
                errors.append(
                    _issue(
                        code="workflow.step_dataset_query_connection_unbound",
                        message=(
                            f"Step '{step_id}' uses saved query "
                            f"'{query_template_id}', but it cannot run: {binding_issue}"
                        ),
                        path=f"{step_path}.override.dataset_query.query_template_id",
                    )
                )
            params = _to_object(config.get("params") or config.get("input_params"))
            for parameter_name in sorted(required_params):
                if parameter_name not in params or params.get(parameter_name) in (None, ""):
                    errors.append(
                        _issue(
                            code=("workflow.step_dataset_query_required_parameter_unmapped"),
                            message=(
                                f"Step '{step_id}' must map required "
                                f"saved-query parameter '{parameter_name}'"
                            ),
                            path=(f"{step_path}.override.dataset_query.params.{parameter_name}"),
                        )
                    )
            if params_schema.get("additionalProperties") is False:
                for parameter_name in sorted(set(params) - set(properties)):
                    errors.append(
                        _issue(
                            code="workflow.step_dataset_query_parameter_unknown",
                            message=(
                                f"Step '{step_id}' maps unknown "
                                f"saved-query parameter '{parameter_name}'"
                            ),
                            path=(f"{step_path}.override.dataset_query.params.{parameter_name}"),
                        )
                    )
            continue

        if step_kind == STEP_KIND_TABULAR_FEED:
            # Without this branch `tabular_feed` fell through to the agent
            # fallback below and was rejected as "missing agent_id or
            # participant_key" — for a kind the Step kind selector offers and
            # whose editor has no Agent field at all (AUDIT-035). The config
            # contract already exists; readiness simply never reached it.
            from core.application.tabular_feed.validation import (
                validate_tabular_feed_config,
            )

            # `override` is bound inside the dataset_query branch above, so
            # it is not in scope here.
            step_override = step.get("override")
            step_override = step_override if isinstance(step_override, dict) else {}
            config = step_override.get(STEP_KIND_TABULAR_FEED)
            config = config if isinstance(config, dict) else {}
            for issue in validate_tabular_feed_config(
                config,
                path_prefix=f"{step_path}.override.{STEP_KIND_TABULAR_FEED}",
            ):
                errors.append(
                    _issue(
                        code=f"workflow.step_tabular_feed_{issue.get('code') or 'invalid'}",
                        message=(
                            f"Step '{step_id}' "
                            f"{issue.get('message') or 'tabular_feed configuration is invalid'}"
                        ),
                        path=str(
                            issue.get("path") or f"{step_path}.override.{STEP_KIND_TABULAR_FEED}"
                        ),
                    )
                )
            continue

        if step_kind in {STEP_KIND_APPROVAL, STEP_KIND_WAIT}:
            # Their config, outcomes and placement are checked by the definition
            # validator above; neither runs an agent.
            continue

        if step_kind in {
            STEP_KIND_HTTP_REQUEST,
            STEP_KIND_TRANSFORM,
            STEP_KIND_FILE_FROM_BASE64,
            STEP_KIND_TEMPLATE,
        } or is_control_flow_step_kind(step_kind):
            errors.extend(
                _validate_control_flow_step_config(
                    step_kind=step_kind,
                    step=step,
                    step_path=step_path,
                )
            )
            continue

        if participant_key and not bound_agent_id:
            errors.append(
                _issue(
                    code="workflow.step_participant_unbound",
                    message=(
                        f"Step '{step_id}' references participant_key "
                        f"'{participant_key}' without a workflow participant binding"
                    ),
                    path=f"{step_path}.participant_key",
                )
            )
            continue

        if (
            participant_key
            and explicit_agent_id
            and bound_agent_id
            and explicit_agent_id != bound_agent_id
        ):
            errors.append(
                _issue(
                    code="workflow.step_participant_agent_mismatch",
                    message=(
                        f"Step '{step_id}' sets agent_id '{explicit_agent_id}' "
                        f"but participant_key '{participant_key}' binds to '{bound_agent_id}'"
                    ),
                    path=f"{step_path}.agent_id",
                )
            )
            continue

        if not agent_id:
            errors.append(
                _issue(
                    code="workflow.step_agent_missing",
                    message=(f"Step '{step_id}' is missing agent_id or participant_key"),
                    path=f"{step_path}.agent_id",
                )
            )
            continue

        if not callable(getattr(db, "get_agent", None)):
            warnings.append(
                _issue(
                    code="workflow.step_contract.agent_validation_unavailable",
                    message=(
                        f"Step '{step_id}': "
                        "agent readiness validation unavailable on current DB adapter"
                    ),
                    path=step_path,
                    severity="warning",
                )
            )
            continue

        agent_row = get_registry_artifact_row(
            db,
            artifact_kind="agent",
            id_key="agent_id",
            artifact_id=agent_id,
            platform_getter="get_agent",
        )
        if isinstance(agent_row, dict) and not bool(agent_row.get("enabled", True)):
            errors.append(
                _issue(
                    code="workflow.step_contract.agent.disabled",
                    message=(
                        f"Step '{step_id}' references disabled "
                        f"agent '{agent_id}'. Enable the agent or update the workflow "
                        "before running it."
                    ),
                    path=step_path,
                    severity="error",
                )
            )
        agent_validation = validate_agent_readiness(
            db, agent_id, account_id=str(wf.get("account_id") or "").strip()
        )
        for issue in agent_validation.get("errors") or []:
            errors.append(
                _issue(
                    code=f"workflow.step_contract.{issue.get('code') or 'error'}",
                    message=str(issue.get("message") or "").strip(),
                    path=step_path,
                    severity="error",
                )
            )
        for issue in agent_validation.get("warnings") or []:
            warnings.append(
                _issue(
                    code=f"workflow.step_contract.{issue.get('code') or 'warning'}",
                    message=str(issue.get("message") or "").strip(),
                    path=step_path,
                    severity="warning",
                )
            )

    for step in steps:
        step_id = str(step.get("step_id") or "").strip()
        step_path = f"{WORKFLOW_STEPS_VALUE}.{step_id}"
        for idx, issue in enumerate(resolver.validate_step_io(workflow_id, step_id) or []):
            if not isinstance(issue, dict):
                continue
            sev = str(issue.get("severity") or "warning").strip().lower()
            bucket = errors if sev == "error" else warnings
            issue_path = str(issue.get("path") or "").strip()
            if issue_path.startswith("workflow."):
                path = issue_path
            else:
                path = f"{step_path}.{issue_path}".rstrip(".")
            normalized_issue = _issue(
                code=f"workflow.step_io.{sev}",
                message=str(issue.get("message") or "").strip()
                or f"Step '{step_id}' I/O issue #{idx + 1}",
                path=path,
                severity=sev,
            )
            for context_key in (
                "agent_id",
                "input_name",
                "issue_type",
                "message_key",
                "message_params",
                "skill_id",
                "source",
                "source_name",
                "source_step_id",
                "step_id",
            ):
                if context_key in issue:
                    normalized_issue[context_key] = issue.get(context_key)
            bucket.append(normalized_issue)

    # A hook's steps are a place inside another workflow (R2-11): a hook made only
    # of Set variable steps, or none, is complete, since it changes the hook's results.
    if real_execution_step_count <= 0 and not is_hook_storage(wf):
        errors.append(
            {
                **_issue(
                    code="workflow.execution_bindings_missing",
                    message=(
                        "Workflow must include at least one real execution step "
                        "(agent, connector, child workflow, HTTP request, dataset query, or transform) "
                        "before it can be enabled or run"
                    ),
                    path=WORKFLOW_STEPS_VALUE,
                ),
                "message_key": "react.workflows.execution_bindings_missing",
                "message_params": {},
            }
        )

    for tool_issue in workflow_registry_tool_issues(db, wf):
        tool_id = str(tool_issue.get("tool_id") or "")
        usage = str(tool_issue.get("usage") or "")
        reason = str(tool_issue.get("reason") or "unavailable")
        tool_validation_issue = _issue(
            code=(
                "workflow.step_connector_tool_unavailable"
                if usage == "direct"
                else "workflow.agent_tool_unavailable"
            ),
            message=(
                f"Workflow requires registry tool '{tool_id}', but it is {reason}. "
                "Restore or replace the tool before validating or starting the workflow."
            ),
            path=WORKFLOW_STEPS_VALUE,
        )
        tool_validation_issue["tool_id"] = tool_id
        tool_validation_issue["usage"] = usage
        errors.append(tool_validation_issue)

    errors = _unique_issues(errors)
    warnings = _unique_issues(warnings)
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _unique_issues(issues: list[dict]) -> list[dict]:
    """One issue per code, path and message.

    The definition validator and the per-step config check both judge the
    graph-owned configs (if, switch, Approval, Wait), so the same issue can
    arrive from each; the author needs to see it once.
    """
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for issue in issues:
        key = (
            str(issue.get("code") or ""),
            str(issue.get("path") or ""),
            str(issue.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def format_validation_error_detail(
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    validation: dict,
) -> dict:
    errors = list(validation.get("errors") or [])
    warnings = list(validation.get("warnings") or [])
    return {
        "error": "Contract validation failed",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "messages": [
            str(item.get("message") or "")
            for item in errors
            if str(item.get("message") or "").strip()
        ],
        "issues": errors + warnings,
        "validation": {
            "valid": bool(validation.get("valid", False)),
            "errors": errors,
            "warnings": warnings,
        },
    }


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
